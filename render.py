"""render.py — render an analyze() result as a spreadsheet-native dashboard.

Design identity (grounded in the subject — a tool that turns a sheet into insight):
  · faint CELL-GRID background        · a real FORMULA-BAR hero  (fx =DASHBOARD(...))
  · section markers are COLUMN LETTERS (A,B,C…) — true to a sheet, not arbitrary 01/02/03
  · monospace tabular numerals wherever a number lives (ledger/terminal feel)
Type: Bricolage Grotesque (display) · IBM Plex Sans (body) · IBM Plex Mono (data).
Themes swap only the palette; the identity above is shared, executed once, well."""
import json

# palette + surface tokens only — type, layout and signature are constant
THEMES = {
    "grid": {  # default — spreadsheet green on cool bone
        "label": "Grid", "dark": False,
        "bg": "#e8ebe4", "panel": "#ffffff", "panel2": "#f3f5ef", "ink": "#131a16",
        "muted": "#69716a", "line": "#d4dacd", "grid": "rgba(19,26,22,.05)",
        "accent": "#0e7a4f", "accent_ink": "#ffffff", "seg": "#ffffff",
        "palette": ["#0e7a4f", "#16a06a", "#19b39a", "#2f93cf", "#4060c4",
                    "#dca019", "#c2671e", "#7d54c2", "#5c6b63", "#9aa6a0"],
    },
    "carbon": {  # cool deep slate, electric-blue accent (no acid-green-on-black)
        "label": "Carbon", "dark": True,
        "bg": "#0c1014", "panel": "#141a20", "panel2": "#1a212a", "ink": "#e6ecf2",
        "muted": "#8b96a3", "line": "#232c36", "grid": "rgba(255,255,255,.045)",
        "accent": "#4ea1ff", "accent_ink": "#06121f", "seg": "#141a20",
        "palette": ["#4ea1ff", "#37d2b0", "#7c8cff", "#f2b441", "#ff7a8a",
                    "#56c2e8", "#9b8cff", "#34d399", "#f59e0b", "#8aa0b4"],
    },
    "indigo": {  # crisp cool-white, indigo accent
        "label": "Indigo", "dark": False,
        "bg": "#eceef4", "panel": "#ffffff", "panel2": "#f4f5fa", "ink": "#181a2a",
        "muted": "#6a6e85", "line": "#dcdfeb", "grid": "rgba(24,26,42,.05)",
        "accent": "#4f46e5", "accent_ink": "#ffffff", "seg": "#ffffff",
        "palette": ["#4f46e5", "#0ea5a3", "#2563eb", "#db2777", "#d97706",
                    "#7c3aed", "#059669", "#e11d48", "#6366f1", "#64748b"],
    },
    "ember": {  # warm near-black, amber/copper accent
        "label": "Ember", "dark": True,
        "bg": "#141110", "panel": "#1d1917", "panel2": "#241f1c", "ink": "#f0e9e3",
        "muted": "#a3978d", "line": "#332c27", "grid": "rgba(255,255,255,.04)",
        "accent": "#f0923a", "accent_ink": "#1a1109", "seg": "#1d1917",
        "palette": ["#f0923a", "#e4c05a", "#5ec8c0", "#c97bd6", "#8fb55a",
                    "#e8755a", "#69a8e0", "#d9a441", "#b58cf0", "#9a8f86"],
    },
}

_FONTS = ("Bricolage+Grotesque:opsz,wght@12..96,500;12..96,600;12..96,700"
          "&family=IBM+Plex+Mono:wght@400;500;600"
          "&family=IBM+Plex+Sans:wght@400;500;600;700")

_LETTERS = ["A", "B", "C", "D", "E", "F", "G", "H"]


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_dashboard(data: dict) -> str:
    t = THEMES.get(data["meta"].get("theme"), THEMES["grid"])
    pal = t["palette"]
    m = data["meta"]

    kpis = "".join(
        f"""<div class="kpi" style="--k:{pal[i % len(pal)]}">
              <div class="kpi-val" data-val="{_esc(k['value'])}">{_esc(k['value'])}</div>
              <div class="kpi-lbl">{_esc(k['label'])}</div>
              {f'<div class="kpi-sub">{_esc(k["sub"])}</div>' if k.get('sub') else ''}
            </div>"""
        for i, k in enumerate(data["kpis"]))

    charts = "".join(
        f"""<figure class="chart">
              <figcaption>{_esc(c['title'])}</figcaption>
              <div class="cwrap"><canvas id="chart{i}"></canvas></div>
            </figure>"""
        for i, c in enumerate(data["charts"]))

    insights = "".join(f"<li>{_esc(x)}</li>" for x in data["insights"]) \
        or "<li>No further observations.</li>"

    stats = _stats_html(data.get("stats", {"numeric": [], "categorical": []}))

    pv = data["preview"]
    thead = "".join(f"<th>{_esc(c)}</th>" for c in pv["columns"])
    tbody = "".join(
        f"<tr><td class='rn'>{n + 1}</td>" +
        "".join(f"<td>{_esc(v)}</td>" for v in row) + "</tr>"
        for n, row in enumerate(pv["rows"]))

    # the signature formula bar — encodes the real inputs, in spreadsheet syntax
    src = _esc(m.get("source", "data"))
    focus = m.get("focus", "")
    formula = (f'=DASHBOARD(<span class="s">&quot;{src}&quot;</span>'
               + (f'<span class="a">, focus:</span><span class="s">&quot;{_esc(focus)}&quot;</span>'
                  if focus else '') + ')')

    root = (f":root{{--bg:{t['bg']};--panel:{t['panel']};--panel2:{t['panel2']};"
            f"--ink:{t['ink']};--muted:{t['muted']};--line:{t['line']};"
            f"--grid:{t['grid']};--accent:{t['accent']};--accent-ink:{t['accent_ink']};"
            f"--seg:{t['seg']};}}")

    repl = {
        "__FONTS__": _FONTS, "__ROOT__": root,
        "__TITLE__": _esc(data["title"]), "__SUBTITLE__": _esc(data["subtitle"]),
        "__CELL__": f"{pv['total_rows']}×{pv['total_cols']}",
        "__SOURCE__": src, "__FORMULA__": formula,
        "__EB_A__": _eyebrow("A", "Key metrics"),
        "__EB_B__": _eyebrow("B", "Observations"),
        "__EB_C__": _eyebrow("C", "Visual breakdown"),
        "__EB_D__": _eyebrow("D", "Detailed statistics"),
        "__EB_E__": _eyebrow("E", "Data preview"),
        "__KPIS__": kpis, "__CHARTS__": charts, "__INSIGHTS__": insights, "__STATS__": stats,
        "__TABLE_LABEL__": _esc(pv["label"]),
        "__TABLE_SHOWN__": str(len(pv["rows"])), "__TABLE_TOTAL__": str(pv["total_rows"]),
        "__THEAD__": thead, "__TBODY__": tbody,
        "__MODEL__": _esc(m["model"]),
        "__CHARTS_JSON__": json.dumps(data["charts"]),
        "__PALETTE_JSON__": json.dumps(pal),
        "__GRIDC__": t["grid"], "__MUTED__": t["muted"], "__SEGC__": t["seg"],
        "__ACCENT__": t["accent"],
    }
    html = TEMPLATE
    for k, v in repl.items():
        html = html.replace(k, v)
    return html


def _eyebrow(letter, label):
    return (f'<div class="eb"><span class="col">{letter}</span>'
            f'<span class="eb-l">{label}</span></div>')


def _stats_html(stats):
    out = []
    num = stats.get("numeric", [])
    if num:
        head = "".join(f"<th>{h}</th>" for h in
                       ["", "Mean", "Median", "Min", "Max", "Std", "Sum"])
        rows = "".join(
            "<tr><td class='c'>" + _esc(r["column"]) + "</td>" +
            "".join(f"<td>{_esc(r['metrics'][k])}</td>"
                    for k in ["Mean", "Median", "Min", "Max", "Std", "Sum"]) + "</tr>"
            for r in num)
        out.append(f"""<div class="stat"><div class="stat-h">Numeric fields</div>
          <div class="scroll"><table class="led"><thead><tr>{head}</tr></thead>
          <tbody>{rows}</tbody></table></div></div>""")
    cat = stats.get("categorical", [])
    if cat:
        head = "".join(f"<th>{h}</th>" for h in
                       ["", "Distinct", "Most common", "Count", "Share"])
        rows = "".join(
            "<tr><td class='c'>" + _esc(r["column"]) + "</td>" +
            "".join(f"<td>{_esc(r['metrics'][k])}</td>"
                    for k in ["Distinct", "Most common", "Count", "Share"]) + "</tr>"
            for r in cat)
        out.append(f"""<div class="stat"><div class="stat-h">Categorical fields</div>
          <div class="scroll"><table class="led"><thead><tr>{head}</tr></thead>
          <tbody>{rows}</tbody></table></div></div>""")
    return "".join(out)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=__FONTS__&display=swap" rel="stylesheet"/>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
__ROOT__
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{font-family:'IBM Plex Sans',system-ui,sans-serif;background:var(--bg);color:var(--ink);
     -webkit-font-smoothing:antialiased;line-height:1.5;position:relative}
/* ambient spreadsheet cell-grid */
body::before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;
     background-image:linear-gradient(var(--grid) 1px,transparent 1px),
                      linear-gradient(90deg,var(--grid) 1px,transparent 1px);
     background-size:30px 30px}
.mono{font-family:'IBM Plex Mono',ui-monospace,monospace;font-variant-numeric:tabular-nums}
.wrap{position:relative;z-index:1;max-width:1200px;margin:0 auto;padding:40px 26px 90px}

/* ── hero ─────────────────────────────────────────────── */
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:18px}
.tag{font-family:'IBM Plex Mono',monospace;font-size:.72rem;letter-spacing:.04em;
     color:var(--muted);display:flex;gap:7px;align-items:center}
.tag b{color:var(--accent);font-weight:600}
.dl{display:inline-flex;align-items:center;gap:7px;cursor:pointer;
     font-family:'IBM Plex Mono',monospace;font-size:.78rem;font-weight:500;color:var(--ink);
     background:var(--panel);border:1px solid var(--line);padding:8px 13px;border-radius:7px;
     transition:border-color .15s,transform .12s}
.dl:hover{border-color:var(--accent);transform:translateY(-1px)}
.dl svg{width:14px;height:14px}
h1{font-family:'Bricolage Grotesque','IBM Plex Sans',sans-serif;font-weight:700;
     font-size:3rem;line-height:1.02;letter-spacing:-.025em;margin:20px 0 0;max-width:18ch}
.sub{color:var(--muted);font-size:1.04rem;margin-top:12px;max-width:60ch}
/* formula bar — the signature */
.formula{display:flex;align-items:stretch;margin-top:22px;border:1px solid var(--line);
     border-radius:9px;overflow:hidden;background:var(--panel);max-width:760px;
     box-shadow:0 1px 0 var(--line)}
.formula .fx{display:flex;align-items:center;padding:0 14px;background:var(--accent);
     color:var(--accent-ink);font-family:'IBM Plex Mono',monospace;font-size:.82rem;
     font-style:italic;font-weight:600}
.formula code{font-family:'IBM Plex Mono',monospace;font-size:.84rem;padding:11px 15px;
     color:var(--ink);overflow-x:auto;white-space:nowrap}
.formula code .s{color:var(--accent)}
.formula code .a{color:var(--muted)}

/* ── section eyebrow (column letter) ──────────────────── */
.section{margin-top:52px}
.eb{display:flex;align-items:center;gap:12px;margin-bottom:20px}
.eb .col{width:24px;height:24px;flex:none;display:grid;place-items:center;border-radius:6px;
     background:var(--accent);color:var(--accent-ink);font-family:'IBM Plex Mono',monospace;
     font-weight:600;font-size:.82rem}
.eb-l{font-family:'IBM Plex Mono',monospace;font-size:.8rem;font-weight:500;
     letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.eb::after{content:"";flex:1;height:1px;background:var(--line)}

/* ── kpis ─────────────────────────────────────────────── */
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(176px,1fr));gap:1px;
     background:var(--line);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.kpi{background:var(--panel);padding:18px 18px 16px;position:relative}
.kpi::after{content:"";position:absolute;left:18px;top:0;width:22px;height:3px;background:var(--k)}
.kpi-val{font-family:'IBM Plex Mono',monospace;font-variant-numeric:tabular-nums;
     font-weight:600;font-size:1.62rem;letter-spacing:-.02em;line-height:1.15;margin-top:6px}
.kpi-lbl{font-size:.84rem;font-weight:600;margin-top:9px;line-height:1.3}
.kpi-sub{font-family:'IBM Plex Mono',monospace;font-size:.66rem;color:var(--muted);
     margin-top:4px;text-transform:uppercase;letter-spacing:.08em}

/* ── observations ─────────────────────────────────────── */
.notes{border:1px solid var(--line);border-radius:12px;background:var(--panel);overflow:hidden}
.notes ul{list-style:none}
.notes li{position:relative;padding:16px 20px 16px 40px;border-bottom:1px solid var(--line);
     font-size:.97rem}
.notes li:last-child{border-bottom:none}
.notes li::before{content:"";position:absolute;left:20px;top:22px;width:7px;height:7px;
     border-radius:2px;background:var(--accent)}

/* ── charts ───────────────────────────────────────────── */
.grid2{display:grid;grid-template-columns:repeat(2,1fr);gap:18px}
.chart{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px}
.chart figcaption{font-family:'Bricolage Grotesque',sans-serif;font-weight:600;font-size:1.04rem;
     margin-bottom:16px;letter-spacing:-.01em}
.cwrap{position:relative;height:298px}

/* ── statistics (ledger) ──────────────────────────────── */
.grid2 .stat{min-width:0}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.stat-h{font-family:'Bricolage Grotesque',sans-serif;font-weight:600;font-size:.98rem;
     padding:16px 18px 12px}
.scroll{overflow:auto}
table.led{width:100%;border-collapse:collapse;font-family:'IBM Plex Mono',monospace;
     font-size:.8rem;font-variant-numeric:tabular-nums}
table.led th{text-align:right;font-weight:500;color:var(--muted);font-size:.68rem;
     letter-spacing:.06em;text-transform:uppercase;padding:8px 12px;
     border-top:1px solid var(--line);border-bottom:1px solid var(--line);white-space:nowrap}
table.led th:first-child,table.led td.c{text-align:left}
table.led td{text-align:right;padding:9px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
table.led td.c{font-family:'IBM Plex Sans',sans-serif;font-weight:600}
table.led tbody tr:nth-child(even){background:var(--panel2)}
table.led tbody tr:last-child td{border-bottom:none}

/* ── data preview (looks like a sheet) ────────────────── */
.preview{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px}
.pv-meta{font-family:'IBM Plex Mono',monospace;font-size:.74rem;color:var(--muted);margin-bottom:12px}
.search{width:min(320px,100%);padding:9px 13px;border:1px solid var(--line);border-radius:8px;
     background:var(--panel2);color:var(--ink);font-family:'IBM Plex Mono',monospace;
     font-size:.82rem;margin-bottom:14px}
.search:focus{outline:none;border-color:var(--accent)}
.dscroll{overflow:auto;max-height:520px;border:1px solid var(--line);border-radius:9px}
table.data{border-collapse:collapse;width:100%;font-size:.82rem}
table.data th{position:sticky;top:0;background:var(--panel2);text-align:left;padding:10px 13px;
     border-bottom:1px solid var(--line);white-space:nowrap;font-weight:600;z-index:1}
table.data td{padding:9px 13px;border-bottom:1px solid var(--line);white-space:nowrap;
     max-width:260px;overflow:hidden;text-overflow:ellipsis;
     font-variant-numeric:tabular-nums}
table.data td.rn{font-family:'IBM Plex Mono',monospace;color:var(--muted);text-align:right;
     background:var(--panel2);position:sticky;left:0;font-size:.72rem;width:1%}
table.data tbody tr:hover td{background:var(--panel2)}
table.data tbody tr:hover td.rn{background:var(--line)}

/* ── live "add your own" box (never exported) ─────────── */
.eb .col.plus{background:var(--panel2);color:var(--accent);border:1px solid var(--line);font-size:1rem}
.adder{display:flex;gap:10px;flex-wrap:wrap}
.addq{flex:1;min-width:260px;padding:12px 14px;border:1px solid var(--line);border-radius:9px;
     background:var(--panel);color:var(--ink);font-family:'IBM Plex Mono',monospace;font-size:.85rem}
.addq:focus{outline:none;border-color:var(--accent)}
.addBtn{padding:12px 22px;border:1px solid var(--accent);border-radius:9px;cursor:pointer;
     background:var(--accent);color:var(--accent-ink);font-family:'IBM Plex Mono',monospace;
     font-size:.84rem;font-weight:600;transition:filter .15s,transform .12s}
.addBtn:hover{filter:brightness(1.07);transform:translateY(-1px)}
.addBtn:disabled{opacity:.55;cursor:default;transform:none}
.addmsg{margin-top:12px;font-family:'IBM Plex Mono',monospace;font-size:.76rem;color:var(--muted);
     min-height:18px;line-height:1.5}
.addmsg.err{color:#e0564a}
.flash{animation:flash 1.1s ease-out}
@keyframes flash{0%{box-shadow:0 0 0 2px var(--accent)}100%{box-shadow:0 0 0 0 transparent}}
footer{margin-top:54px;padding-top:20px;border-top:1px solid var(--line);
     font-family:'IBM Plex Mono',monospace;color:var(--muted);font-size:.74rem;
     display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
@media(max-width:860px){.grid2{grid-template-columns:1fr}h1{font-size:2.2rem}}
@media(prefers-reduced-motion:reduce){*{animation:none!important}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="tag">SHEET <b>__SOURCE__</b> · __CELL__ cells</div>
    <button class="dl" id="dlBtn">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0 4-4m-4 4-4-4"/>
           <path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>Download</button>
  </div>
  <h1>__TITLE__</h1>
  <p class="sub">__SUBTITLE__</p>
  <div class="formula"><span class="fx">fx</span><code>__FORMULA__</code></div>

  <section class="section">__EB_A__<div class="kpis" id="kpiGrid">__KPIS__</div></section>
  <section class="section">__EB_B__<div class="notes"><ul>__INSIGHTS__</ul></div></section>
  <section class="section">__EB_C__<div class="grid2" id="chartGrid">__CHARTS__</div></section>
  <section class="section">__EB_D__<div class="grid2">__STATS__</div></section>
  <section class="section">__EB_E__
    <div class="preview">
      <div class="pv-meta">__TABLE_LABEL__ · rows 1–__TABLE_SHOWN__ of __TABLE_TOTAL__</div>
      <input class="search" id="q" placeholder="filter rows…"/>
      <div class="dscroll"><table class="data" id="dt">
        <thead><tr><th class="rn">#</th>__THEAD__</tr></thead><tbody>__TBODY__</tbody></table></div>
    </div>
  </section>

  <!-- live "add your own" box — stripped from the downloaded file (.no-export) -->
  <section class="section no-export" id="adder">
    <div class="eb"><span class="col plus">+</span><span class="eb-l">Add your own</span></div>
    <div class="adder">
      <input id="addq" class="addq" autocomplete="off"
        placeholder='What other KPI or chart do you want? — e.g. “average revenue by region as a pie”'/>
      <button id="addBtn" class="addBtn" type="button">Add →</button>
    </div>
    <div class="addmsg" id="addmsg">Type a metric or chart in plain English — it's computed from your real data and added above. This box isn't included when you download.</div>
  </section>

  <footer><span>Built locally — no data left this machine.</span><span>model · __MODEL__</span></footer>
</div>

<script>
const CHARTS=__CHARTS_JSON__, PALETTE=__PALETTE_JSON__;
const GRID="__GRIDC__", TICK="__MUTED__", SEG="__SEGC__", ACCENT="__ACCENT__";
Chart.defaults.color=TICK;
Chart.defaults.font.family="'IBM Plex Mono',monospace";
Chart.defaults.font.size=11;

function buildChart(el,c,i){
  const circ=c.type==="pie"||c.type==="doughnut";
  const cols=c.labels.map((_,j)=>PALETTE[j%PALETTE.length]);
  return new Chart(el,{type:c.type,data:{labels:c.labels,datasets:[{label:c.title,data:c.values,
      backgroundColor:circ?cols:(c.type==="line"?PALETTE[i%PALETTE.length]+"22":cols),
      borderColor:circ?SEG:(c.type==="line"?PALETTE[i%PALETTE.length]:cols),
      borderWidth:circ?2:(c.type==="line"?2.5:0),borderRadius:c.type==="bar"?3:0,
      fill:c.type==="line",tension:.34,pointRadius:c.type==="line"?0:0,
      pointHoverRadius:4,pointBackgroundColor:PALETTE[i%PALETTE.length]}]},
    options:{responsive:true,maintainAspectRatio:false,
      animation:{duration:800,easing:"easeOutCubic"},
      plugins:{legend:{display:circ,position:"bottom",
          labels:{boxWidth:9,boxHeight:9,padding:13,usePointStyle:true,
            font:{family:"'IBM Plex Sans',sans-serif",size:11}}},
        tooltip:{padding:9,cornerRadius:6,titleFont:{family:"'IBM Plex Mono',monospace"},
          bodyFont:{family:"'IBM Plex Mono',monospace"}}},
      scales:circ?{}:{x:{grid:{display:false},border:{color:GRID},
          ticks:{maxRotation:40,autoSkip:true,
            callback(v){const s=this.getLabelForValue(v);return s.length>13?s.slice(0,12)+"…":s;}}},
        y:{grid:{color:GRID},border:{display:false},beginAtZero:true}}}});
}
CHARTS.forEach((c,i)=>{const el=document.getElementById("chart"+i);if(el)buildChart(el,c,i);});

document.querySelectorAll(".kpi-val").forEach(el=>{
  const raw=el.dataset.val, num=parseFloat(raw.replace(/[^0-9.\-]/g,""));
  if(isNaN(num)) return;
  const suf=raw.replace(/[0-9.,\-\s]/g,"");
  let cur=0; const inc=num/34;
  const t=setInterval(()=>{cur+=inc;
    if((inc>=0&&cur>=num)||(inc<0&&cur<=num)){cur=num;clearInterval(t);}
    el.textContent=(Math.abs(num)>=1000?Math.round(cur).toLocaleString()
        :(Number.isInteger(num)?Math.round(cur):cur.toFixed(2)))+suf;},16);
});

const dl=document.getElementById("dlBtn");
dl&&dl.addEventListener("click",async()=>{
  // Download the persisted dashboard, but strip the interactive "add your own" box
  // (.no-export) so the typing UI never ships in the saved file — added widgets stay.
  let el;
  try{el=new DOMParser().parseFromString(await(await fetch(location.href)).text(),"text/html").documentElement;}
  catch(e){el=document.documentElement.cloneNode(true);}
  el.querySelectorAll(".no-export").forEach(n=>n.remove());
  const html="<!DOCTYPE html>\n"+el.outerHTML;
  const a=document.createElement("a");
  a.href=URL.createObjectURL(new Blob([html],{type:"text/html"}));
  a.download=(document.title||"dashboard").replace(/[^\w]+/g,"_")+".html";
  document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href),4000);
});

const q=document.getElementById("q");
q&&q.addEventListener("input",e=>{const s=e.target.value.toLowerCase();
  document.querySelectorAll("#dt tbody tr").forEach(tr=>{
    tr.style.display=tr.textContent.toLowerCase().includes(s)?"":"none";});});

/* ── live "add your own" KPI / chart ──────────────────── */
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;")
  .replace(/>/g,"&gt;").replace(/"/g,"&quot;");}
let kpiN=document.querySelectorAll("#kpiGrid .kpi").length;
let chartN=CHARTS.length;
function addKpi(k){
  const d=document.createElement("div");d.className="kpi flash";
  d.style.setProperty("--k",PALETTE[kpiN%PALETTE.length]);
  d.innerHTML='<div class="kpi-val">'+esc(k.value)+'</div><div class="kpi-lbl">'+esc(k.label)+'</div>'
    +(k.sub?'<div class="kpi-sub">'+esc(k.sub)+'</div>':'');
  document.getElementById("kpiGrid").appendChild(d);kpiN++;
  d.scrollIntoView({behavior:"smooth",block:"center"});
}
function addChart(c){
  const i=chartN++;const fig=document.createElement("figure");fig.className="chart flash";
  fig.innerHTML='<figcaption>'+esc(c.title)+'</figcaption><div class="cwrap"><canvas id="chart'+i+'"></canvas></div>';
  document.getElementById("chartGrid").appendChild(fig);
  buildChart(fig.querySelector("canvas"),c,i);
  fig.scrollIntoView({behavior:"smooth",block:"center"});
}
const addBtn=document.getElementById("addBtn"),addq=document.getElementById("addq"),
      addmsg=document.getElementById("addmsg");
async function submitAdd(){
  const text=(addq.value||"").trim();
  if(!text){addmsg.className="addmsg err";addmsg.textContent="Type what you'd like to add.";return;}
  addBtn.disabled=true;addmsg.className="addmsg";addmsg.textContent="Building from your data…";
  try{
    const id=location.pathname.split("/").filter(Boolean).pop();
    const fd=new FormData();fd.append("id",id);fd.append("request",text);
    const r=await fetch("/add_widget",{method:"POST",body:fd});
    const w=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error(w.error||("HTTP "+r.status));
    if(w.kind==="kpi"){addKpi(w.kpi);addmsg.textContent='✓ Added KPI “'+w.kpi.label+'”. Ask for another?';}
    else{addChart(w.chart);addmsg.textContent='✓ Added chart “'+w.chart.title+'”. Ask for another?';}
    addq.value="";
  }catch(e){addmsg.className="addmsg err";addmsg.textContent="⚠ "+e.message;}
  addBtn.disabled=false;
}
addBtn&&addBtn.addEventListener("click",submitAdd);
addq&&addq.addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();submitAdd();}});
</script>
</body>
</html>"""
