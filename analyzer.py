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

# The "Add your own" box turns one free-text request into one widget. It's a single interactive
# call where a smart interpretation matters far more than latency, so it LEADS with the strongest
# local model and only drops to the fast one if the big model is unavailable/too slow.
WIDGET_MODELS = ["qwen2.5:14b", "qwen2.5-coder:14b", "llama3.2:3b", "qwen2.5:1.5b"]

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


# Numeric columns that are NOT real measures: identifiers, codes, years.
# Summing or averaging these ("Total EmployeeID", "Average Year") is nonsense —
# only count / nunique / range mean anything.
_ID_TOKENS = {"id", "ids", "code", "codes", "zip", "zipcode", "ssn", "uuid", "guid",
              "account", "acct", "pin", "postal", "sku", "isbn", "phone", "mobile"}
_YEAR_TOKENS = {"year", "yr", "fy"}


def _name_tokens(name):
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(name))      # split camelCase
    return set(re.split(r"[^a-zA-Z0-9]+", s.lower())) - {""}


def is_measure(name, series):
    """True only when sum/mean over this numeric column is meaningful. False for
    identifiers (name says id/code/zip…, or near-unique integers) and year columns."""
    nv = numeric_view(series).dropna()
    if not len(nv):
        return False
    toks = _name_tokens(name)
    if toks & _ID_TOKENS or toks & _YEAR_TOKENS:
        return False
    integral = bool((nv % 1 == 0).all())
    if integral and nv.between(1900, 2100).mean() > 0.8:    # looks like a year column
        return False
    # Auto-increment IDs are near-unique integers that form a (near-)CONTIGUOUS run — i.e. the
    # distinct values nearly fill their own min..max span (1001,1002,…). A real measure like a
    # weight or salary is also distinct but sparse within its range, so it stays a measure.
    nun = series.nunique(dropna=True)
    if integral and len(series) >= 8 and nun / max(1, len(series)) > 0.9:
        span = float(nv.max() - nv.min()) + 1
        if span > 0 and nun / span > 0.9:
            return False
    return True


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
#  3b. ASK THE LOCAL MODEL for a few clarifying questions about THIS sheet
# --------------------------------------------------------------------------- #
QUESTION_INSTRUCTIONS = """You are a data analyst helping someone decide what their dashboard should
emphasise. Given a JSON PROFILE of a spreadsheet, write %(n)d short clarifying questions whose answers
would change what the dashboard highlights. Each question must be answerable in a few words and grounded
in THIS data — reference the real column names / values from the profile. Respond with ONLY a JSON
object, no prose:
{"questions":[{"question":"...","options":["option 1","option 2","option 3"]}]}
Give 2-4 concrete options per question, picked from real columns/values where it makes sense.
Do NOT ask which metric or which column to group by (those are already handled). Output JSON only."""


def ask_questions(profile, n=2):
    """Local-model-written, data-grounded clarifying questions. Returns [] if the model is down or
    produces junk — the deterministic questions in build_questions always carry the flow regardless."""
    instr = QUESTION_INSTRUCTIONS % {"n": n}
    prompt = instr + "\n\nPROFILE:\n" + json.dumps(profile)[:12000]
    for model in [MODEL] + FALLBACK_MODELS:
        try:
            r = requests.post(OLLAMA_URL, json={
                "model": model, "prompt": prompt, "stream": False,
                "format": "json", "options": {"temperature": 0.3, "num_ctx": 8192},
            }, timeout=180)
            r.raise_for_status()
            data = json.loads(r.json()["response"])
            out = []
            for q in (data.get("questions") or [])[:n]:
                text = str(q.get("question", "")).strip()
                if not text:
                    continue
                opts = [str(o).strip() for o in (q.get("options") or []) if str(o).strip()][:4]
                out.append({"question": text, "options": opts})
            if out:
                return out
        except Exception as e:
            print(f"[questions {model}] failed: {e}")
            continue
    return []


def build_questions(tables, profile, focus=""):
    """Deterministic, column-grounded questions (always reliable) + a couple of local-model questions.
    Each question: {id, question, type:'single'|'text', options:[...], role}. role drives how the answer
    is applied in analyze(): metric -> KPI priority, dimension -> chart, goal/freeform -> focus text."""
    nums, cats = [], []
    for _, df in tables:
        for c in df.columns:
            kind = col_kind(df[c])
            if kind == "numeric" and str(c) not in nums:
                nums.append(str(c))
            elif kind == "categorical" and str(c) not in cats:
                cats.append(str(c))
    questions = []
    if nums:
        questions.append({"id": "metric", "role": "metric", "type": "single",
                          "question": "Which number matters most to you?",
                          "options": nums[:6] + ["No preference"]})
    if cats:
        questions.append({"id": "dimension", "role": "dimension", "type": "single",
                          "question": "What should the data be broken down by?",
                          "options": cats[:6] + ["No preference"]})
    questions.append({"id": "goal", "role": "goal", "type": "single",
                      "question": "What's the goal of this dashboard?",
                      "options": ["Track totals & headline numbers", "Compare segments",
                                  "Spot outliers & problems", "See the top performers"]})
    for i, mq in enumerate(ask_questions(profile, n=2)):
        questions.append({"id": f"ai{i}", "role": "freeform",
                          "type": "single" if mq["options"] else "text",
                          "question": mq["question"],
                          "options": (mq["options"] + ["Other / not sure"]) if mq["options"] else []})
    return questions


_SKIP_ANSWERS = {"", "no preference", "other / not sure", "n/a", "none"}


def answers_to_prefs(questions, answers):
    """Fold the user's answers into dashboard preferences: a chosen metric column, a chosen grouping
    dimension, a goal, and a human-readable focus string fed to the model. Unknown/blank answers skip."""
    answers = answers or {}
    metric = dim = goal = None
    parts = []
    for q in questions or []:
        a = answers.get(q.get("id"))
        if a is None or str(a).strip().lower() in _SKIP_ANSWERS:
            continue
        a = str(a).strip()
        role = q.get("role")
        if role == "metric":
            metric = a
        elif role == "dimension":
            dim = a
        elif role == "goal":
            goal = a
        parts.append(f'{q.get("question", "")} -> {a}')
    return {"metric": metric, "dim": dim, "goal": goal, "focus": " | ".join(parts)}


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
    # ...and summing/averaging an identifier or year column is nonsense too
    if agg in ("sum", "mean", "median") and not is_measure(col, df[col]):
        return None
    val = {"sum": nv.sum, "mean": nv.mean, "median": nv.median,
           "min": nv.min, "max": nv.max}.get(agg, nv.sum)()
    return _fmt(float(val))


def compute_chart(tables, chart):
    df, dim = _find_col(tables, chart.get("dimension"))
    if df is None:
        return None
    agg = (chart.get("agg") or "count").lower()
    limit = int(chart.get("limit") or 12)
    mdf, meas = _find_col(tables, chart.get("measure"))
    work = pd.DataFrame({"dim": df[dim].astype(str)})
    if meas and mdf is df and agg in ("sum", "mean", "max", "min") and is_measure(meas, df[meas]):
        work["m"] = numeric_view(df[meas])
        g = work.dropna(subset=["m"]).groupby("dim")["m"]
        grouped = {"sum": g.sum, "mean": g.mean, "max": g.max, "min": g.min}[agg]()
    else:
        grouped = work.groupby("dim").size()
        agg = "count"
    if not len(grouped):
        return None

    def slice_of(ser):
        return {"labels": [str(x) for x in ser.index.tolist()],
                "values": [round(float(x), 3) for x in ser.values.tolist()]}

    # "min"/"lightest" defaults to smallest-first; everything else ranks largest-first.
    default = grouped.sort_values(ascending=(agg == "min")).head(limit)
    if not len(default):
        return None
    ctype = chart.get("type", "bar") if chart.get("type") in \
        ("bar", "line", "pie", "doughnut") else "bar"
    out = {"title": chart.get("title") or f"{dim}", "type": ctype,
           **slice_of(default), "dimension": dim, "measure": meas, "agg": agg}
    # bar/line charts carry both ends so the page can toggle Top ⇄ Bottom client-side.
    if ctype in ("bar", "line"):
        ranked = grouped.sort_values(ascending=False)
        out["top"] = slice_of(ranked.head(limit))
        out["bottom"] = slice_of(ranked.tail(limit).iloc[::-1])
        out["rankable"] = bool(len(ranked) > limit)
    return out


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
            if col_kind(df[c]) == "numeric" and is_measure(c, df[c]):
                pool.append({"label": f"Total {c}", "column": str(c), "agg": "sum"})
                pool.append({"label": f"Average {c}", "column": str(c), "agg": "mean"})
                pool.append({"label": f"Peak {c}", "column": str(c), "agg": "max"})
    for _, df in tables:
        for c in df.columns:
            # categoricals + identifier/year numerics → a distinct-count headline
            if col_kind(df[c]) == "categorical" or \
                    (col_kind(df[c]) == "numeric" and not is_measure(c, df[c])):
                pool.append({"label": f"Unique {c}", "column": str(c), "agg": "nunique"})
    return pool


def build_kpis(tables, spec, target, lead=None):
    """Merge the model's KPIs (nice labels, first) with the deterministic pool, de-duped.
    `lead` KPIs (e.g. the user's chosen metric) are tried first so they head the board."""
    seen, out = set(), []
    for k in list(lead or []) + list(spec.get("kpis", [])) + auto_kpis(tables):
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
#  6b. GROUNDED insights — every number comes from the computed data, never the LLM
# --------------------------------------------------------------------------- #
def derive_insights(tables, charts):
    """Build observations straight from the computed charts/columns so the numbers are
    always correct. The model is only allowed to add digit-free qualitative color later."""
    out = []
    # Concentration: read the leading chart's distribution.
    for ch in charts:
        vals, labels = ch.get("values") or [], ch.get("labels") or []
        tot = sum(vals)
        if len(vals) >= 2 and tot > 0:
            meas = ch.get("measure") if ch.get("agg") in ("sum", "mean") else None
            tail = f"of total {meas}" if meas else "of all records"
            out.append(f"‘{labels[0]}’ leads {ch.get('dimension')} at "
                       f"{_fmt(vals[0])} ({vals[0] / tot * 100:.0f}% {tail}).")
            if len(vals) >= 3:
                s3 = sum(vals[:3]) / tot * 100
                if s3 >= 60:
                    out.append(f"The top 3 {ch.get('dimension')} values make up "
                               f"{s3:.0f}% of the total — a concentrated distribution.")
            break
    # Spread / outliers on real measures.
    seen = set()
    for _, df in tables:
        for c in df.columns:
            key = str(c).lower()
            if key in seen or col_kind(df[c]) != "numeric" or not is_measure(c, df[c]):
                continue
            nv = numeric_view(df[c]).dropna()
            if len(nv) < 3:
                continue
            mean, mx, mn, sd = nv.mean(), nv.max(), nv.min(), nv.std()
            if sd and (mx - mean) / sd >= 2:
                out.append(f"{c} has a standout high of {_fmt(float(mx))}, far above the "
                           f"average of {_fmt(float(mean))}.")
            else:
                out.append(f"{c} ranges {_fmt(float(mn))}–{_fmt(float(mx))} "
                           f"(average {_fmt(float(mean))}).")
            seen.add(key)
            if len(seen) >= 2:
                break
        if len(seen) >= 2:
            break
    # Data completeness — surfaces the nulls we already profile but never showed.
    total = sum(int(df.size) for _, df in tables)
    nulls = sum(int(df.isna().sum().sum()) for _, df in tables)
    if total and nulls:
        complete = (1 - nulls / total) * 100
        if complete < 99.5:
            out.append(f"The sheet is {complete:.0f}% complete "
                       f"({nulls:,} empty cells across {total:,}).")
    return out


# --------------------------------------------------------------------------- #
#  7. ORCHESTRATE
# --------------------------------------------------------------------------- #
def _lead_kpis(tables, metric):
    """KPI specs for the user's chosen metric so it heads the board."""
    if not metric:
        return []
    df, col = _find_col(tables, metric)
    if df is None or col is None:
        return []
    if col_kind(df[col]) == "numeric" and is_measure(col, df[col]):
        return [{"label": f"Total {col}", "column": col, "agg": "sum"},
                {"label": f"Average {col}", "column": col, "agg": "mean"},
                {"label": f"Peak {col}", "column": col, "agg": "max"}]
    return [{"label": f"Unique {col}", "column": col, "agg": "nunique"}]


def _relevant(chart, metric, dim, tables):
    """Does this computed chart touch the user's chosen metric or dimension?"""
    _, mcol = _find_col(tables, metric) if metric else (None, None)
    _, dcol = _find_col(tables, dim) if dim else (None, None)
    return (dcol and chart.get("dimension") == dcol) or (mcol and chart.get("measure") == mcol)


def analyze(raw_bytes, filename, focus="", detail="standard", theme="grid",
            primary_metric=None, primary_dim=None, goal="", extra_focus="", scope="full"):
    tables = load_tables(raw_bytes, filename)
    if not tables:
        raise ValueError("No tabular data could be extracted from this file.")
    detailed = (detail == "detailed")
    focused = (scope == "focused")
    nkpi_target = 6 if focused else (16 if detailed else 9)
    nchart_target = 3 if focused else (7 if detailed else 5)

    full_focus = " | ".join(p for p in (focus.strip(), (extra_focus or "").strip()) if p)

    profile = build_profile(tables)
    valid_cols = [str(c) for _, df in tables for c in df.columns]
    spec = ask_model(profile, valid_cols, focus=full_focus,
                     nkpi=nkpi_target, nchart=nchart_target) \
        or fallback_spec(tables, profile)

    kpis = build_kpis(tables, spec, nkpi_target, lead=_lead_kpis(tables, primary_metric))

    charts = []
    for ch in spec.get("charts", [])[:nchart_target + 2]:
        c = compute_chart(tables, ch)
        if c:
            charts.append(c)
    if not charts:
        charts = [c for c in (compute_chart(tables, ch)
                  for ch in fallback_spec(tables, profile)["charts"]) if c]

    # Guarantee a chart on the user's chosen dimension (measured by their metric if given).
    if primary_dim:
        ddf, dcol = _find_col(tables, primary_dim)
        if dcol and not any(c.get("dimension") == dcol for c in charts):
            _, mcol = _find_col(tables, primary_metric) if primary_metric else (None, None)
            lead_chart = compute_chart(tables, {
                "title": f"{dcol} breakdown", "type": "bar", "dimension": dcol,
                "measure": mcol, "agg": "sum" if mcol else "count", "limit": 12})
            if lead_chart:
                charts.insert(0, lead_chart)

    # Order so answer-relevant charts come first; in focused mode keep only those (then trim).
    if primary_metric or primary_dim:
        charts.sort(key=lambda c: 0 if _relevant(c, primary_metric, primary_dim, tables) else 1)
        if focused:
            rel = [c for c in charts if _relevant(c, primary_metric, primary_dim, tables)]
            charts = rel or charts
    charts = charts[:nchart_target]

    stats = compute_stats(tables)

    # Insights: computed numbers come first (always correct); keep only the model's
    # digit-free qualitative observations — it is never trusted to emit a number.
    grounded = derive_insights(tables, charts)
    model_color = [s for s in spec.get("insights", []) if not re.search(r"\d", str(s))]
    insights = (grounded + model_color)[:6] or spec.get("insights", [])[:6]

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
        "insights": insights,
        "stats": stats,
        "preview": preview,
        "meta": {
            "model": spec.get("_model", MODEL),
            "tables": len(tables),
            "theme": theme,
            "focus": full_focus,
            "detail": detail,
            "scope": scope,
            "goal": goal or "",
            "source": filename or "data",
        },
    }


# --------------------------------------------------------------------------- #
#  8. INTERPRET a free-text "add a KPI / chart" request into ONE computed widget
#     (used by the live "Add your own" box at the bottom of a rendered dashboard)
# --------------------------------------------------------------------------- #
WIDGET_INSTRUCTIONS = """You convert a user's plain-English request into ONE dashboard widget for a
specific spreadsheet. You are given the column PROFILE and the user's REQUEST. Respond with ONLY a
JSON object, no prose, in ONE of these two shapes:

KPI (a single headline number):
{"kind":"kpi","label":"short label","column":"<exact column name>","agg":"sum|mean|min|max|count|nunique"}

CHART:
{"kind":"chart","title":"short title","type":"bar|line|pie|doughnut","dimension":"<categorical column>",
 "measure":"<numeric column, or null to count rows>","agg":"sum|mean|count","limit":12}

Use EXACT column names from the profile. sum/mean/min/max need a numeric column; count = number of
rows, nunique = distinct values. If the user asks to see something "by" a category, or asks for a
chart / graph / breakdown / trend, return a chart; otherwise return a KPI.

SUPERLATIVES & RANKINGS — important: a request like "heaviest player", "top earners", "oldest
customers", "highest-scoring teams" means the user wants to compare entities BY a numeric column.
Return a CHART with dimension = the entity column (e.g. the name) and measure = the relevant numeric
column (it is sorted automatically) — OR a KPI with max/min on that numeric column. NEVER count how
often each name appears. Only leave "measure" null when the user explicitly asks for frequencies or
counts. Always set "measure" to a numeric column when the request implies a quantity. Output JSON only."""


def _widget_spec_ok(spec, tables):
    """Accept a model's widget spec only if it's well-formed AND points at a column that really
    exists — otherwise a confident-but-wrong answer (bad column / hallucinated field) gets accepted
    and silently computes to nothing. Rejecting it lets the next model (or the parser) try."""
    if not isinstance(spec, dict):
        return False
    if spec.get("kind") == "chart":
        df, _ = _find_col(tables, spec.get("dimension"))
        return df is not None
    if spec.get("kind") == "kpi":
        df, _ = _find_col(tables, spec.get("column"))
        return df is not None
    return False


def _ask_widget_model(request_text, profile, tables):
    prompt = (WIDGET_INSTRUCTIONS + f'\n\nREQUEST: "{request_text.strip()}"\n\nPROFILE:\n'
              + json.dumps(profile)[:9000])
    for model in WIDGET_MODELS:
        try:
            r = requests.post(OLLAMA_URL, json={
                "model": model, "prompt": prompt, "stream": False,
                "format": "json", "options": {"temperature": 0.1, "num_ctx": 8192},
            }, timeout=200)
            r.raise_for_status()
            spec = json.loads(r.json()["response"])
            if _widget_spec_ok(spec, tables):
                print(f"[widget {model}] ok: {spec.get('kind')} :: {request_text!r}")
                return spec
            print(f"[widget {model}] spec referenced no real column; trying next")
        except Exception as e:
            print(f"[widget {model}] failed: {e}")
            continue
    return None


_AGG_WORDS = [
    (("median",), "median"),
    (("average", "avg", "mean"), "mean"),
    (("distinct", "unique", "how many different"), "nunique"),
    (("count", "number of", "how many", "records", "rows"), "count"),
    (("maximum", "max ", "peak", "highest", "largest", "biggest", "top"), "max"),
    (("minimum", "min ", "lowest", "smallest"), "min"),
    (("total", "sum"), "sum"),
]
_CHART_WORDS = ("chart", "graph", "plot", "pie", "bar", "line", "trend", "breakdown",
                "distribution", "histogram", " by ", "over time", "per ")
_SUPERLATIVE_MAX = ("heaviest", "tallest", "oldest", "largest", "biggest", "highest", "longest",
                    "most", "top", "richest", "strongest", "fastest", "greatest", "maximum")
_SUPERLATIVE_MIN = ("lightest", "shortest", "youngest", "smallest", "lowest", "least", "cheapest",
                    "slowest", "weakest", "minimum")


def _col_kind_of(name, tables):
    for _, df in tables:
        for c in df.columns:
            if str(c) == name:
                return col_kind(df[c])
    return "text"


def _parse_widget(request_text, tables):
    """Deterministic fallback when the model is down: scan the request for column names and an
    aggregation keyword, and decide KPI vs chart. Always grounded in real columns."""
    text = " " + request_text.lower().strip() + " "
    names = sorted({str(c) for _, df in tables for c in df.columns}, key=len, reverse=True)
    mentioned = [c for c in names if c.lower() in text]
    agg = next((a for words, a in _AGG_WORDS if any(w in text for w in words)), "sum")
    nums = [c for c in mentioned if _col_kind_of(c, tables) == "numeric"]
    cats = [c for c in mentioned if _col_kind_of(c, tables) == "categorical"]
    wants_chart = any(w in text for w in _CHART_WORDS)
    sup_max = any(w in text for w in _SUPERLATIVE_MAX)
    sup_min = any(w in text for w in _SUPERLATIVE_MIN)

    # "heaviest player", "youngest customer" with no explicit chart → the extreme value of the
    # relevant measure (a single clear number), never a count of how often a name appears.
    if (sup_max or sup_min) and not wants_chart:
        big = max((df for _, df in tables), key=lambda d: len(d))
        meas = nums[0] if nums else _pick_measure(request_text, big)
        if meas:
            return {"kind": "kpi", "label": request_text.strip()[:40],
                    "column": meas, "agg": "max" if sup_max else "min"}

    if wants_chart:
        dim = cats[0] if cats else None
        if not dim:                                   # fall back to any categorical column
            for _, df in tables:
                for c in df.columns:
                    if col_kind(df[c]) == "categorical":
                        dim = str(c)
                        break
                if dim:
                    break
        if not dim:
            return None
        meas = nums[0] if nums else None
        ctype = ("pie" if "pie" in text else
                 "doughnut" if ("doughnut" in text or "donut" in text) else
                 "line" if ("line" in text or "trend" in text or "over time" in text) else "bar")
        cagg = "mean" if agg == "mean" else ("sum" if meas else "count")
        return {"kind": "chart", "title": request_text.strip()[:60], "type": ctype,
                "dimension": dim, "measure": meas, "agg": cagg, "limit": 12}

    if nums:
        col = nums[0]
    elif mentioned:
        col = mentioned[0]
        if agg in ("sum", "mean", "min", "max"):       # non-numeric → distinct count
            agg = "nunique"
    else:
        return None
    return {"kind": "kpi", "label": request_text.strip()[:40], "column": col, "agg": agg}


def _is_near_unique(series):
    """A column where almost every value is distinct (player names, IDs) — grouping it and counting
    occurrences is meaningless, so a count-chart over it is almost always a misinterpretation."""
    n = len(series)
    return n >= 8 and series.nunique(dropna=True) / n > 0.7


def _pick_measure(request_text, df):
    """Choose the numeric column to rank/aggregate by: prefer one whose name overlaps the request
    (e.g. 'kg'/'weight'), else the first real measure in the table. Returns a column name or None."""
    text = " " + request_text.lower() + " "
    measures = [str(c) for c in df.columns if col_kind(df[c]) == "numeric" and is_measure(c, df[c])]
    if not measures:
        measures = [str(c) for c in df.columns if col_kind(df[c]) == "numeric"]
    if not measures:
        return None
    best, best_n = None, 0
    for name in measures:
        toks = set(name.lower().replace("(", " ").replace(")", " ").split())
        n = sum(1 for tk in toks if tk and tk in text)
        if n > best_n:
            best_n, best = n, name
    return best or measures[0]


def _repair_chart(spec, request_text, tables):
    """If a chart would group a near-unique entity (names/IDs) and just COUNT rows, attach the real
    numeric measure the user means so it ranks entities by value instead of by frequency."""
    ddf, dcol = _find_col(tables, spec.get("dimension"))
    if dcol is None:
        return spec
    _, mcol = _find_col(tables, spec.get("measure")) if spec.get("measure") else (None, None)
    counting = (spec.get("agg") or "count").lower() == "count" or mcol is None
    if counting and _is_near_unique(ddf[dcol]):
        meas = _pick_measure(request_text, ddf)
        if meas:
            spec = dict(spec, measure=meas,
                        agg="mean" if (spec.get("agg") == "mean") else "sum")
    return spec


def interpret_widget(request_text, tables, profile):
    """Turn a free-text request into ONE computed widget. Model first, deterministic parse as
    fallback. Returns {"kind":"kpi","kpi":{label,value,sub}} or {"kind":"chart","chart":{...}},
    or None if it can't be grounded in the real columns."""
    spec = _ask_widget_model(request_text, profile, tables) or _parse_widget(request_text, tables)
    if not isinstance(spec, dict):
        return None

    if spec.get("kind") == "chart":
        spec = _repair_chart(spec, request_text, tables)
        c = compute_chart(tables, spec)
        if not c:                                      # model picked a bad column → try parser
            spec = _parse_widget(request_text, tables)
            spec = _repair_chart(spec, request_text, tables) if spec else spec
            c = compute_chart(tables, spec) if spec and spec.get("kind") == "chart" else None
        if not c:
            return None
        if spec.get("title"):
            c["title"] = spec["title"]
        return {"kind": "chart", "chart": c}

    val = compute_kpi(tables, spec)
    if val is None:                                    # e.g. asked to sum an identifier → blocked
        return None
    _, col = _find_col(tables, spec.get("column"))
    agg = (spec.get("agg") or "sum").lower()
    label = spec.get("label") or (f"{_AGG_SUB.get(agg, '').title()} {col}".strip() if col else "Metric")
    return {"kind": "kpi", "kpi": {"label": label, "value": val, "sub": _agg_sub(spec)}}


# --------------------------------------------------------------------------- #
#  9. EXPORT FOR POWER BI — cleaned/typed data (.xlsx) + ready-to-paste DAX
# --------------------------------------------------------------------------- #
def clean_for_export(df):
    """A copy of the table with numeric columns coerced to real numbers, so Power BI types them
    as numeric on import (instead of leaving '$48,000' / '32%' as text). Other columns are unchanged."""
    out = df.copy()
    for c in out.columns:
        if col_kind(out[c]) == "numeric":
            out[c] = numeric_view(out[c])
    return out


def _safe_sheet_name(label, used):
    """Excel sheet name: ≤31 chars, none of []:*?/\\, unique. This name becomes the Power BI
    query/table name, so the generated DAX references it verbatim."""
    name = re.sub(r"[\[\]:*?/\\]", " ", str(label)).strip() or "Sheet"
    name = name[:31]
    base, i = name, 1
    while name.lower() in used:
        suf = f" {i}"
        name = base[: 31 - len(suf)] + suf
        i += 1
    used.add(name.lower())
    return name


_DAX_AGG = [("Total", "SUM"), ("Average", "AVERAGE"), ("Max", "MAX"), ("Min", "MIN")]


def build_dax_measures(named_tables):
    """Generate a starter palette of DAX measures from the columns: SUM/AVERAGE/MAX/MIN for real
    measures, DISTINCTCOUNT for categories & identifiers, a row count per table. Names are unique
    across the model (Power BI requires it). `named_tables` is [(sheet_name, df), …]."""
    out = [
        "// Power BI / DAX measures — generated by Sheet → Dashboard",
        "//",
        "// 1) Power BI Desktop → Get Data → Excel workbook → pick the .xlsx in this zip,",
        "//    tick every sheet, then Load.",
        "// 2) For each line below: right-click the matching table in the Fields pane →",
        "//    New measure → paste the line. Table names match the .xlsx sheet names.",
        "",
    ]
    seen = set()

    def uniq(name):
        n, i = name, 2
        while n.lower() in seen:
            n = f"{name} {i}"
            i += 1
        seen.add(n.lower())
        return n

    for sheet, df in named_tables:
        t = sheet.replace("'", "")
        block = [f"{uniq(t + ' Row Count')} = COUNTROWS('{t}')"]
        for c in df.columns:
            col = str(c).replace("[", "").replace("]", "")
            kind = col_kind(df[c])
            if kind == "numeric" and is_measure(c, df[c]):
                for word, fn in _DAX_AGG:
                    block.append(f"{uniq(word + ' ' + col)} = {fn}('{t}'[{col}])")
            elif kind in ("numeric", "categorical"):     # non-measure numeric or category
                block.append(f"{uniq('Distinct ' + col)} = DISTINCTCOUNT('{t}'[{col}])")
        if len(block) > 1:
            out.append(f"// ===== Table: {t} =====")
            out.extend(block)
            out.append("")
    return "\n".join(out)


def build_powerbi_export(tables):
    """Return (xlsx_bytes, dax_text). The .xlsx holds the cleaned/typed tables; the DAX references
    those exact sheet names — generated together so the two always stay in sync."""
    buf = io.BytesIO()
    used, named = set(), []
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for label, df in tables:
            sheet = _safe_sheet_name(label, used)
            clean_for_export(df).to_excel(xw, sheet_name=sheet, index=False)
            named.append((sheet, df))
    return buf.getvalue(), build_dax_measures(named)
