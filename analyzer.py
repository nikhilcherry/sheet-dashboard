"""
analyzer.py  — extraction + local-model intelligence + chart computation.

Design contract:
  * RAW DATA is extracted deterministically with pandas (LLMs hallucinate numbers).
  * The LOCAL MODEL (Ollama) decides *what the dashboard shows* — title, KPIs,
    which charts/groupings are meaningful, and the written insights — by reading
    a compact profile of whatever sheet was uploaded. Nothing is hardcoded to a
    particular report, so a brand-new sheet every time still produces a sensible board.
"""
import io
import json
import re
import numpy as np
import pandas as pd
import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.2:3b"          # fast & fully local; falls back to bigger models on junk output
FALLBACK_MODELS = ["qwen2.5:14b", "qwen2.5-coder:14b", "qwen2.5:1.5b"]

PALETTE = ["#6366f1", "#06b6d4", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
           "#ec4899", "#14b8a6", "#f97316", "#3b82f6", "#84cc16", "#a855f7"]


# --------------------------------------------------------------------------- #
#  1. LOAD TABLES from whatever the user dropped in
# --------------------------------------------------------------------------- #
def load_tables(raw_bytes: bytes, filename: str):
    name = (filename or "").lower()
    tables = []
    if name.endswith((".xlsx", ".xls")):
        xls = pd.ExcelFile(io.BytesIO(raw_bytes))
        for sheet in xls.sheet_names:
            df = xls.parse(sheet)
            if df.shape[0] and df.shape[1]:
                tables.append((sheet, df))
    elif name.endswith(".csv"):
        tables.append(("CSV", pd.read_csv(io.BytesIO(raw_bytes))))
    else:  # html / htm / anything else -> treat as HTML
        text = raw_bytes.decode("utf-8", errors="ignore")
        try:
            dfs = pd.read_html(io.StringIO(text))
        except ValueError:
            dfs = []
        for i, df in enumerate(dfs):
            df = _flatten_header(df)
            if df.shape[0] >= 1 and df.shape[1] >= 1:
                tables.append((f"Table {i+1}", df))
    # clean every table
    cleaned = []
    for label, df in tables:
        df = _promote_header(_flatten_header(df))
        df = _clean_frame(df)
        if df.shape[0] and df.shape[1]:
            cleaned.append((label, df))
    return cleaned


def _promote_header(df):
    """Excel-as-HTML often emits header cells as <td>, so pandas auto-numbers the
    columns and the real labels sit in row 0. Detect that and promote row 0."""
    cols = list(df.columns)
    auto = all(re.match(r"^(\d+|Unnamed.*)$", str(c)) for c in cols)
    if not auto or df.shape[0] < 2:
        return df
    vals = [str(x).strip() for x in df.iloc[0].tolist()]
    nonempty = [v for v in vals if v and v.lower() != "nan"]
    distinct = len(set(nonempty)) == len(nonempty)
    if len(nonempty) >= max(2, 0.6 * len(vals)) and distinct:
        df = df.copy()
        df.columns = [v if v and v.lower() != "nan" else f"col{i}"
                      for i, v in enumerate(vals)]
        df = df.iloc[1:].reset_index(drop=True)
    return df


def _flatten_header(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [" ".join(str(x) for x in tup if str(x) != "nan").strip()
                      for tup in df.columns]
    return df


def _clean_frame(df):
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    # drop fully empty rows/cols
    df = df.dropna(axis=1, how="all").dropna(axis=0, how="all")
    # de-duplicate column names
    seen, cols = {}, []
    for c in df.columns:
        if c in seen:
            seen[c] += 1
            cols.append(f"{c}.{seen[c]}")
        else:
            seen[c] = 0
            cols.append(c)
    df.columns = cols
    return df.reset_index(drop=True)


def numeric_view(series: pd.Series) -> pd.Series:
    """Best-effort coercion to numbers. Strips formatting (commas, %, currency,
    parentheses-negatives) but NOT letters — so 'Q1' or 'Alice' stay non-numeric."""
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")
    s = series.astype(str).str.strip()
    s = s.str.replace(r"^\((.*)\)$", r"-\1", regex=True)       # (123) -> -123
    s = s.str.replace(r"[,$%€£₹\s]", "", regex=True)
    s = s.replace({"": np.nan, "-": np.nan, "nan": np.nan})
    return pd.to_numeric(s, errors="coerce")


def col_kind(series: pd.Series):
    nv = numeric_view(series)
    ratio = nv.notna().mean() if len(nv) else 0
    if ratio >= 0.7:
        return "numeric"
    nun = series.nunique(dropna=True)
    if nun and nun <= max(40, len(series) * 0.5):
        return "categorical"
    return "text"


# --------------------------------------------------------------------------- #
#  2. PROFILE the data so the model can reason cheaply
# --------------------------------------------------------------------------- #
def build_profile(tables):
    profile = {"tables": []}
    for label, df in tables:
        cols = []
        for c in df.columns:
            kind = col_kind(df[c])
            info = {"name": c, "kind": kind, "nulls": int(df[c].isna().sum())}
            if kind == "numeric":
                nv = numeric_view(df[c]).dropna()
                if len(nv):
                    info["stats"] = {
                        "min": round(float(nv.min()), 3),
                        "max": round(float(nv.max()), 3),
                        "mean": round(float(nv.mean()), 3),
                        "sum": round(float(nv.sum()), 3),
                    }
            elif kind == "categorical":
                vc = df[c].astype(str).value_counts().head(6)
                info["top_values"] = {str(k): int(v) for k, v in vc.items()}
            cols.append(info)
        profile["tables"].append({
            "label": label,
            "rows": int(df.shape[0]),
            "columns": cols,
            "sample_rows": df.head(3).astype(str).to_dict(orient="records"),
        })
    return profile


# --------------------------------------------------------------------------- #
#  3. ASK THE LOCAL MODEL for a dashboard spec
# --------------------------------------------------------------------------- #
SPEC_INSTRUCTIONS = """You are a senior data-visualization analyst. You are given a JSON PROFILE of a
spreadsheet (column names, types, statistics, sample rows). Design the best possible executive
dashboard for THIS specific data. Respond with ONLY a JSON object, no prose, with this schema:

{
  "title": "short dashboard title derived from the data",
  "subtitle": "one descriptive sentence",
  "kpis": [           // %(nkpi)d headline numbers, each a DIFFERENT column/metric
    {"label":"...", "column":"<column name>", "agg":"sum|mean|min|max|count|nunique"}
  ],
  // agg meaning: count = number of ROWS in the table (use this for "Total <items>",
  // "Number of records/people/orders"). nunique = number of DISTINCT values in a column
  // (use ONLY for "Number of distinct/unique X"). sum/mean/min/max REQUIRE a numeric column.
  "charts": [         // %(nchart)d charts, varied types and dimensions
    {"title":"...", "type":"bar|line|pie|doughnut",
     "dimension":"<categorical or text column to group by>",
     "measure":"<numeric column to aggregate, or null to count rows>",
     "agg":"sum|mean|count", "limit":12}
  ],
  "insights": ["3 to 5 short, specific bullet observations about the data"]
}

Rules: use EXACT column names from the profile. KPI columns must be numeric (except count/nunique).
Chart 'dimension' must be categorical/text with a reasonable number of distinct values. Pick charts
that tell a story. %(focus)sOutput JSON only."""


def _spec_is_usable(spec, valid_cols):
    """A small model is only trusted if its spec references REAL columns —
    otherwise charts/KPIs would silently compute to nothing."""
    if not isinstance(spec, dict):
        return False
    low = {c.lower() for c in valid_cols}
    good_charts = sum(1 for c in spec.get("charts", [])
                      if str(c.get("dimension", "")).lower() in low)
    good_kpis = sum(1 for k in spec.get("kpis", [])
                    if str(k.get("column", "")).lower() in low)
    return good_charts >= 1 and good_kpis >= 1


def ask_model(profile, valid_cols, focus="", nkpi=8, nchart=5):
    focus_txt = (f'IMPORTANT — the user wants the dashboard to focus on: "{focus.strip()}". '
                 f'Prioritise KPIs, charts and insights related to that. ') if focus.strip() else ""
    instr = SPEC_INSTRUCTIONS % {"nkpi": nkpi, "nchart": nchart, "focus": focus_txt}
    prompt = instr + "\n\nPROFILE:\n" + json.dumps(profile)[:12000]
    for model in [MODEL] + FALLBACK_MODELS:
        try:
            r = requests.post(OLLAMA_URL, json={
                "model": model, "prompt": prompt, "stream": False,
                "format": "json", "options": {"temperature": 0.2, "num_ctx": 8192},
            }, timeout=240)
            r.raise_for_status()
            spec = json.loads(r.json()["response"])
            if _spec_is_usable(spec, valid_cols):
                spec["_model"] = model
                return spec
            print(f"[model {model}] returned unusable spec; trying next model")
        except Exception as e:
            print(f"[model {model}] failed: {e}")
            continue
    return None


# --------------------------------------------------------------------------- #
#  4. COMPUTE real chart data from the spec (deterministic)
# --------------------------------------------------------------------------- #
def _find_col(tables, name):
    """Resolve a (possibly approximate) column name to a real column. Small models
    often write 'Salary' for a 'Salary (INR)' column, so fall back to substring and
    token-overlap matching rather than silently dropping to a row count."""
    if not name:
        return None, None
    for _, df in tables:                      # exact
        for c in df.columns:
            if c == name:
                return df, c
    low = str(name).strip().lower()
    for _, df in tables:                      # case-insensitive exact
        for c in df.columns:
            if str(c).strip().lower() == low:
                return df, c
    for _, df in tables:                      # substring either direction
        for c in df.columns:
            cl = str(c).strip().lower()
            if cl and (low in cl or cl in low):
                return df, c
    toks = set(low.replace("(", " ").replace(")", " ").split())
    best, best_n = (None, None), 0
    for _, df in tables:                      # token overlap
        for c in df.columns:
            ct = set(str(c).strip().lower().replace("(", " ").replace(")", " ").split())
            n = len(toks & ct)
            if n > best_n:
                best_n, best = n, (df, c)
    return best if best_n else (None, None)


def _fmt(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    av = abs(v)
    if av >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if av >= 1_000:
        return f"{v/1_000:.1f}K"
    if float(v).is_integer():
        return f"{int(v):,}"
    return f"{v:,.2f}"


def compute_kpi(tables, kpi):
    df, col = _find_col(tables, kpi.get("column"))
    agg = (kpi.get("agg") or "sum").lower()
    if df is None:
        return None
    if agg == "count":
        return _fmt(len(df))
    if agg == "nunique":
        return _fmt(df[col].nunique())
    nv = numeric_view(df[col]).dropna()
    # sum/mean/min/max are meaningless on a non-numeric column -> drop the KPI
    if not len(nv) or col_kind(df[col]) != "numeric":
        return None
    val = {"sum": nv.sum, "mean": nv.mean, "min": nv.min,
           "max": nv.max}.get(agg, nv.sum)()
    return _fmt(float(val))


def compute_chart(tables, chart):
    df, dim = _find_col(tables, chart.get("dimension"))
    if df is None:
        return None
    agg = (chart.get("agg") or "count").lower()
    limit = int(chart.get("limit") or 12)
    mdf, meas = _find_col(tables, chart.get("measure"))
    work = pd.DataFrame({"dim": df[dim].astype(str)})
    if meas and mdf is df and agg in ("sum", "mean"):
        work["m"] = numeric_view(df[meas])
        g = work.dropna(subset=["m"]).groupby("dim")["m"]
        ser = (g.sum() if agg == "sum" else g.mean())
    else:
        ser = work.groupby("dim").size()
        agg = "count"
    ser = ser.sort_values(ascending=False).head(limit)
    if not len(ser):
        return None
    return {
        "title": chart.get("title") or f"{dim}",
        "type": chart.get("type", "bar") if chart.get("type") in
                ("bar", "line", "pie", "doughnut") else "bar",
        "labels": [str(x) for x in ser.index.tolist()],
        "values": [round(float(x), 3) for x in ser.values.tolist()],
        "dimension": dim, "measure": meas, "agg": agg,
    }


# --------------------------------------------------------------------------- #
#  5. Deterministic FALLBACK spec (model down / bad output)
# --------------------------------------------------------------------------- #
def fallback_spec(tables, profile):
    label, df = max(tables, key=lambda t: t[1].size)
    nums = [c for c in df.columns if col_kind(df[c]) == "numeric"]
    cats = [c for c in df.columns if col_kind(df[c]) == "categorical"]
    kpis = [{"label": f"Total {c}", "column": c, "agg": "sum"} for c in nums[:3]]
    kpis.insert(0, {"label": "Records", "column": df.columns[0], "agg": "count"})
    charts = []
    for i, cat in enumerate(cats[:3]):
        meas = nums[i % len(nums)] if nums else None
        charts.append({"title": f"{cat} breakdown", "type": ["bar", "doughnut", "pie"][i % 3],
                       "dimension": cat, "measure": meas,
                       "agg": "sum" if meas else "count", "limit": 12})
    if not charts and nums:
        charts.append({"title": f"{nums[0]} distribution", "type": "bar",
                       "dimension": df.columns[0], "measure": nums[0],
                       "agg": "sum", "limit": 12})
    return {
        "title": f"{label} Dashboard",
        "subtitle": f"{df.shape[0]} rows × {df.shape[1]} columns auto-analysed locally",
        "kpis": kpis, "charts": charts,
        "insights": [f"Largest numeric column '{nums[0]}' summed to "
                     f"{_fmt(float(numeric_view(df[nums[0]]).sum()))}." if nums else
                     "No numeric columns detected; showing category counts."],
        "_model": "deterministic-fallback",
    }


# --------------------------------------------------------------------------- #
#  6. Rich deterministic STATISTICS + a deep KPI pool
# --------------------------------------------------------------------------- #
_AGG_SUB = {"sum": "total", "mean": "average", "min": "minimum", "max": "maximum",
            "count": "row count", "nunique": "distinct values", "median": "median"}


def _agg_sub(k):
    return _AGG_SUB.get((k.get("agg") or "sum").lower(), "")


def compute_stats(tables):
    """Full descriptive statistics for every column — the 'more different stats' section."""
    numeric, categorical = [], []
    for label, df in tables:
        for c in df.columns:
            kind = col_kind(df[c])
            if kind == "numeric":
                nv = numeric_view(df[c]).dropna()
                if not len(nv):
                    continue
                numeric.append({"column": str(c), "table": label, "metrics": {
                    "Sum": _fmt(float(nv.sum())), "Mean": _fmt(float(nv.mean())),
                    "Median": _fmt(float(nv.median())), "Min": _fmt(float(nv.min())),
                    "Max": _fmt(float(nv.max())),
                    "Std": _fmt(float(nv.std())) if len(nv) > 1 else "—",
                    "Count": _fmt(len(nv))}})
            elif kind == "categorical":
                s = df[c].astype(str)
                vc = s.value_counts()
                if not len(vc):
                    continue
                categorical.append({"column": str(c), "table": label, "metrics": {
                    "Distinct": _fmt(int(s.nunique())),
                    "Most common": str(vc.index[0])[:26],
                    "Count": _fmt(int(vc.iloc[0])),
                    "Share": f"{vc.iloc[0] / len(s) * 100:.0f}%"}})
    return {"numeric": numeric, "categorical": categorical}


def auto_kpis(tables):
    """A broad deterministic KPI pool so the board always has plenty of headline numbers."""
    pool = []
    label, big = max(tables, key=lambda t: t[1].size)
    pool.append({"label": "Total Records", "column": str(big.columns[0]),
                 "agg": "count", "sub": label})
    for _, df in tables:
        for c in df.columns:
            if col_kind(df[c]) == "numeric":
                pool.append({"label": f"Total {c}", "column": str(c), "agg": "sum"})
                pool.append({"label": f"Average {c}", "column": str(c), "agg": "mean"})
                pool.append({"label": f"Peak {c}", "column": str(c), "agg": "max"})
    for _, df in tables:
        for c in df.columns:
            if col_kind(df[c]) == "categorical":
                pool.append({"label": f"Unique {c}", "column": str(c), "agg": "nunique"})
    return pool


def build_kpis(tables, spec, target):
    """Merge the model's KPIs (nice labels, first) with the deterministic pool, de-duped."""
    seen, out = set(), []
    for k in list(spec.get("kpis", [])) + auto_kpis(tables):
        key = (str(k.get("column", "")).lower(), (k.get("agg") or "sum").lower())
        if key in seen:
            continue
        val = compute_kpi(tables, k)
        if val is None:
            continue
        seen.add(key)
        out.append({"label": k.get("label") or str(k.get("column", "")),
                    "value": val, "sub": k.get("sub") or _agg_sub(k)})
        if len(out) >= target:
            break
    return out


# --------------------------------------------------------------------------- #
#  7. ORCHESTRATE
# --------------------------------------------------------------------------- #
def analyze(raw_bytes, filename, focus="", detail="standard", theme="grid"):
    tables = load_tables(raw_bytes, filename)
    if not tables:
        raise ValueError("No tabular data could be extracted from this file.")
    detailed = (detail == "detailed")
    nkpi_target = 16 if detailed else 9
    nchart_target = 7 if detailed else 5

    profile = build_profile(tables)
    valid_cols = [str(c) for _, df in tables for c in df.columns]
    spec = ask_model(profile, valid_cols, focus=focus,
                     nkpi=nkpi_target, nchart=nchart_target) \
        or fallback_spec(tables, profile)

    kpis = build_kpis(tables, spec, nkpi_target)

    charts = []
    for ch in spec.get("charts", [])[:nchart_target]:
        c = compute_chart(tables, ch)
        if c:
            charts.append(c)
    if not charts:
        charts = [c for c in (compute_chart(tables, ch)
                  for ch in fallback_spec(tables, profile)["charts"]) if c]

    stats = compute_stats(tables)

    label, big = max(tables, key=lambda t: t[1].size)
    preview = {
        "label": label,
        "columns": [str(c) for c in big.columns][:14],
        "rows": [r[:14] for r in big.head(60).astype(str).values.tolist()],
        "total_rows": int(big.shape[0]),
        "total_cols": int(big.shape[1]),
    }

    return {
        "title": spec.get("title", "Data Dashboard"),
        "subtitle": spec.get("subtitle", ""),
        "kpis": kpis,
        "charts": charts,
        "insights": spec.get("insights", [])[:6],
        "stats": stats,
        "preview": preview,
        "meta": {
            "model": spec.get("_model", MODEL),
            "tables": len(tables),
            "theme": theme,
            "focus": focus.strip(),
            "detail": detail,
            "source": filename or "data",
        },
    }
