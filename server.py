"""server.py — localhost web app: drop a sheet, answer a few questions, get a tailored dashboard."""
import os
import json
import time
import secrets
import traceback

from flask import Flask, request, send_file, abort

from analyzer import (analyze, load_tables, build_profile, build_questions,
                      answers_to_prefs)
from render import render_dashboard

BASE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(BASE, "generated")
os.makedirs(GEN, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB

# In-memory session store: maps a token from /analyze to the uploaded bytes + the
# questions it produced, so /generate can rebuild the dashboard with the answers.
# Local single-user app, so a plain dict (capped) is plenty.
SESSIONS = {}
SESSION_CAP = 24


def _remember(raw, filename, focus, questions):
    sid = secrets.token_hex(8)
    if len(SESSIONS) >= SESSION_CAP:                       # evict oldest
        for old in list(SESSIONS)[: len(SESSIONS) - SESSION_CAP + 1]:
            SESSIONS.pop(old, None)
    SESSIONS[sid] = {"raw": raw, "filename": filename, "focus": focus, "questions": questions}
    return sid

UPLOAD_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Sheet → Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet"/>
<style>
:root{--bg:#f6f3ec;--panel:#fffefb;--panel2:#f1ece1;--ink:#211d18;--muted:#7a7165;
 --line:#e6dfd2;--accent:#b8502d;--fh:'Fraunces',Georgia,serif;--fb:'Inter',system-ui,sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100vh;display:grid;place-items:center;font-family:var(--fb);color:var(--ink);
 background:var(--bg);-webkit-font-smoothing:antialiased;padding:30px}
.card{width:min(620px,94vw);background:var(--panel);border:1px solid var(--line);border-radius:18px;
 padding:38px 38px 32px;box-shadow:0 1px 2px rgba(33,29,24,.05),0 18px 50px rgba(33,29,24,.08)}
.kick{font-size:.7rem;font-weight:600;letter-spacing:.18em;text-transform:uppercase;color:var(--accent)}
h1{font-family:var(--fh);font-weight:600;font-size:2.1rem;letter-spacing:-.01em;margin-top:8px}
p.sub{color:var(--muted);margin:10px 0 24px;font-size:.95rem;line-height:1.55;max-width:54ch}
#drop{display:block;margin-top:6px;border:1.5px dashed #c9bfac;border-radius:14px;
 padding:34px 20px;text-align:center;cursor:pointer;transition:.18s;background:var(--panel2)}
#drop.hov{border-color:var(--accent);background:#f3e9df}
#drop .ico{width:34px;height:34px;color:var(--accent);margin:0 auto}
#drop .t{margin-top:12px;font-weight:600;font-size:.96rem}
#drop .h{color:var(--muted);font-size:.8rem;margin-top:5px}
#file{display:none}
.fname{margin-top:12px;font-size:.85rem;color:var(--accent);text-align:center;min-height:18px;font-weight:500}
.opts{margin-top:22px;display:grid;gap:16px}
label.fld{display:block;font-size:.74rem;font-weight:600;letter-spacing:.04em;
 text-transform:uppercase;color:var(--muted);margin-bottom:7px}
input.txt,select{width:100%;padding:11px 13px;border:1px solid var(--line);border-radius:10px;
 background:var(--panel);color:var(--ink);font-family:var(--fb);font-size:.9rem}
input.txt:focus,select:focus{outline:none;border-color:var(--accent)}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.themes{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}
.theme{cursor:pointer;border:1.5px solid var(--line);border-radius:10px;padding:9px 6px 8px;
 text-align:center;font-size:.74rem;font-weight:600;transition:.15s;background:var(--panel)}
.theme:hover{border-color:#cbb8a4}
.theme.on{border-color:var(--accent)}
.theme .sw{display:flex;gap:3px;justify-content:center;margin-bottom:6px}
.theme .sw i{width:12px;height:12px;border-radius:3px;display:block}
button.go{margin-top:24px;width:100%;padding:14px;border:none;border-radius:11px;cursor:pointer;
 font-family:var(--fb);font-size:.98rem;font-weight:600;color:#fff;background:var(--accent);
 transition:filter .15s}
button.go:hover{filter:brightness(1.06)}
button.go:disabled{opacity:.5;cursor:default}
.status{margin-top:16px;font-size:.86rem;color:var(--muted);text-align:center;display:none}
.spin{width:16px;height:16px;border:2.5px solid var(--line);border-top-color:var(--accent);
 border-radius:50%;display:inline-block;vertical-align:middle;margin-right:8px;animation:s 1s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
.foot{margin-top:20px;font-size:.74rem;text-align:center;color:var(--muted)}
h2.qh{font-family:var(--fh);font-weight:600;font-size:1.55rem;letter-spacing:-.01em;margin-top:6px}
.qblock{margin-top:20px}
.qblock .qq{font-size:.92rem;font-weight:600;color:var(--ink);margin-bottom:9px}
.opt-row{display:flex;flex-wrap:wrap;gap:8px}
.opt{cursor:pointer;border:1.5px solid var(--line);border-radius:9px;padding:8px 13px;
 font-size:.84rem;font-weight:500;background:var(--panel);color:var(--ink);transition:.15s}
.opt:hover{border-color:#cbb8a4}
.opt.on{border-color:var(--accent);background:#f3e9df;color:var(--accent)}
.scopes{display:grid;grid-template-columns:1fr 1fr;gap:9px}
.scope{cursor:pointer;border:1.5px solid var(--line);border-radius:10px;padding:12px 10px;
 text-align:center;font-size:.84rem;font-weight:600;background:var(--panel);transition:.15s}
.scope:hover{border-color:#cbb8a4}.scope.on{border-color:var(--accent);background:#f3e9df}
.scope small{display:block;font-weight:400;color:var(--muted);font-size:.72rem;margin-top:3px}
.back{display:block;text-align:center;margin-top:14px;font-size:.82rem;color:var(--muted);
 text-decoration:none;cursor:pointer}.back:hover{color:var(--accent)}
@media(max-width:560px){.themes{grid-template-columns:repeat(2,1fr)}.row{grid-template-columns:1fr}}
</style></head><body>
<div class="card">

 <!-- ───────────── PHASE 1 · upload ───────────── -->
 <div id="phase1">
   <div class="kick">Local · offline · model-built</div>
   <h1>Sheet → Dashboard</h1>
   <p class="sub">Drop an Excel-exported <b>.html</b> (or .xlsx / .csv). A local model reads the data,
    asks you a couple of quick questions, then builds an interactive dashboard tailored to your answers.</p>
   <label id="drop" for="file">
     <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
       stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/>
       <rect x="7" y="11" width="3" height="6"/><rect x="12" y="7" width="3" height="10"/>
       <rect x="17" y="13" width="3" height="4"/></svg>
     <div class="t">Click or drop your file here</div>
     <div class="h">.html · .htm · .xlsx · .xls · .csv</div>
   </label>
   <input id="file" type="file" accept=".html,.htm,.xlsx,.xls,.csv"/>
   <div class="fname" id="fname"></div>
   <div class="opts"><div>
     <label class="fld">What should it focus on? (optional)</label>
     <input class="txt" id="focus" placeholder="e.g. revenue by region, top performers, costs over time"/>
   </div></div>
   <button class="go" id="analyzeBtn" type="button" disabled>Analyse sheet →</button>
 </div>

 <!-- ───────────── PHASE 2 · refine ───────────── -->
 <div id="phase2" style="display:none">
   <div class="kick">Step 2 · refine</div>
   <h2 class="qh">A few quick questions</h2>
   <p class="sub" id="qsub">Your answers decide what the dashboard leads with. Skip any you don't care about.</p>
   <div id="questions"></div>

   <div class="opts">
     <div>
       <label class="fld">Output</label>
       <div class="scopes" id="scopes">
         <div class="scope on" data-scope="full">Full dashboard<small>everything, your answers first</small></div>
         <div class="scope" data-scope="focused">Focused<small>only what your answers ask for</small></div>
       </div>
     </div>
     <div class="row">
       <div>
         <label class="fld">Detail level</label>
         <select id="detail">
           <option value="standard">Standard · ~9 KPIs</option>
           <option value="detailed">Detailed · ~16 KPIs + full stats</option>
         </select>
       </div>
       <div>
         <label class="fld">Chart density</label>
         <select id="density">
           <option value="auto">Auto</option>
           <option value="more">More charts</option>
         </select>
       </div>
     </div>
     <div>
       <label class="fld">Theme</label>
       <div class="themes" id="themes">
         <div class="theme on" data-theme="grid">
           <div class="sw"><i style="background:#0e7a4f"></i><i style="background:#2f93cf"></i><i style="background:#dca019"></i></div>Grid</div>
         <div class="theme" data-theme="carbon">
           <div class="sw"><i style="background:#4ea1ff"></i><i style="background:#37d2b0"></i><i style="background:#f2b441"></i></div>Carbon</div>
         <div class="theme" data-theme="indigo">
           <div class="sw"><i style="background:#4f46e5"></i><i style="background:#0ea5a3"></i><i style="background:#db2777"></i></div>Indigo</div>
         <div class="theme" data-theme="ember">
           <div class="sw"><i style="background:#f0923a"></i><i style="background:#5ec8c0"></i><i style="background:#c97bd6"></i></div>Ember</div>
       </div>
     </div>
   </div>

   <button class="go" id="go" type="button">Generate dashboard</button>
   <a class="back" id="back">← start over</a>
 </div>

 <div class="status" id="status"><span class="spin"></span><span id="stxt">Analysing locally…</span></div>
 <div class="foot">100% offline · powered by your local Ollama models</div>
</div>
<script>
const $=id=>document.getElementById(id);
const drop=$('drop'),file=$('file'),fname=$('fname'),analyzeBtn=$('analyzeBtn'),go=$('go'),
 status=$('status'),stxt=$('stxt'),phase1=$('phase1'),phase2=$('phase2');
let theme='grid',scope='full',sid=null;
const answers={};                       // {questionId: answerValue}

// theme + scope pickers (phase 2)
document.querySelectorAll('#themes .theme').forEach(el=>el.addEventListener('click',()=>{
  document.querySelectorAll('#themes .theme').forEach(t=>t.classList.remove('on'));
  el.classList.add('on');theme=el.dataset.theme;}));
document.querySelectorAll('#scopes .scope').forEach(el=>el.addEventListener('click',()=>{
  document.querySelectorAll('#scopes .scope').forEach(t=>t.classList.remove('on'));
  el.classList.add('on');scope=el.dataset.scope;}));

// file picker
file.addEventListener('change',()=>{if(file.files.length){fname.textContent='✓ '+file.files[0].name;analyzeBtn.disabled=false;}});
['dragover','dragenter'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('hov');}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('hov');}));
drop.addEventListener('drop',ev=>{if(ev.dataTransfer.files.length){file.files=ev.dataTransfer.files;file.dispatchEvent(new Event('change'));}});

function spinner(msgs){
  status.style.display='block';status.innerHTML='<span class="spin"></span><span id="stxt"></span>';
  const st=$('stxt');let mi=0;st.textContent=msgs[0];
  return setInterval(()=>{st.textContent=msgs[mi=(mi+1)%msgs.length];},2000);
}
function showError(msg){status.innerHTML='<span style="color:#b8502d">⚠ '+msg+'</span>';}

// build the questions UI from the /analyze response
function renderQuestions(qs){
  const wrap=$('questions');wrap.innerHTML='';
  qs.forEach(q=>{
    const block=document.createElement('div');block.className='qblock';
    const h=document.createElement('div');h.className='qq';h.textContent=q.question;block.appendChild(h);
    if(q.type==='single'&&q.options&&q.options.length){
      const row=document.createElement('div');row.className='opt-row';
      q.options.forEach(opt=>{
        const b=document.createElement('div');b.className='opt';b.textContent=opt;
        b.addEventListener('click',()=>{
          row.querySelectorAll('.opt').forEach(o=>o.classList.remove('on'));
          b.classList.add('on');answers[q.id]=opt;});
        row.appendChild(b);});
      block.appendChild(row);
    }else{
      const inp=document.createElement('input');inp.className='txt';inp.placeholder='Type your answer…';
      inp.addEventListener('input',()=>{answers[q.id]=inp.value;});
      block.appendChild(inp);
    }
    wrap.appendChild(block);
  });
}

// PHASE 1 -> /analyze
analyzeBtn.addEventListener('click',async()=>{
  if(!file.files.length)return;
  analyzeBtn.disabled=true;
  const tick=spinner(['Extracting tables…','Profiling columns…','Thinking up questions…']);
  const fd=new FormData();fd.append('file',file.files[0]);fd.append('focus',$('focus').value);
  try{
    const r=await fetch('/analyze',{method:'POST',body:fd});
    clearInterval(tick);
    if(!r.ok){throw new Error((await r.text())||('HTTP '+r.status));}
    const data=await r.json();sid=data.id;
    renderQuestions(data.questions||[]);
    status.style.display='none';
    phase1.style.display='none';phase2.style.display='block';
  }catch(e){clearInterval(tick);showError(e.message);analyzeBtn.disabled=false;}
});

// PHASE 2 -> /generate
go.addEventListener('click',async()=>{
  go.disabled=true;
  const tick=spinner(['Asking the local model…','Computing statistics…','Rendering dashboard…']);
  const fd=new FormData();
  fd.append('id',sid);fd.append('answers',JSON.stringify(answers));fd.append('scope',scope);
  fd.append('detail',$('detail').value);fd.append('density',$('density').value);fd.append('theme',theme);
  try{
    const r=await fetch('/generate',{method:'POST',body:fd});
    clearInterval(tick);
    if(!r.ok){throw new Error((await r.text())||('HTTP '+r.status));}
    const {url}=await r.json();$('stxt')&&($('stxt').textContent='Opening dashboard…');
    window.location.href=url;
  }catch(e){clearInterval(tick);showError(e.message);go.disabled=false;}
});

$('back').addEventListener('click',()=>window.location.reload());
</script></body></html>"""


@app.route("/")
def index():
    return UPLOAD_PAGE


@app.route("/g/<name>")
def serve_generated(name):
    path = os.path.join(GEN, os.path.basename(name))
    if not os.path.exists(path):
        abort(404)
    return send_file(path)


@app.route("/analyze", methods=["POST"])
def analyze_sheet():
    """Phase 1: extract the sheet and return a few tailored questions to refine the dashboard."""
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400, "No file uploaded.")
    raw = f.read()
    focus = request.form.get("focus", "")
    try:
        tables = load_tables(raw, f.filename)
        if not tables:
            raise ValueError("No tabular data could be extracted from this file.")
        profile = build_profile(tables)
        questions = build_questions(tables, profile, focus=focus)
    except Exception as e:
        traceback.print_exc()
        abort(400, f"Could not analyse this file: {e}")
    sid = _remember(raw, f.filename, focus, questions)
    print(f"[analyze] {f.filename} -> {len(questions)} questions (session {sid})")
    return {"id": sid, "filename": f.filename, "questions": questions}, 200


@app.route("/generate", methods=["POST"])
def generate():
    """Phase 2: rebuild the dashboard, tailored by the answers to the phase-1 questions."""
    sid = request.form.get("id", "")
    detail = request.form.get("detail", "standard")
    theme = request.form.get("theme", "grid")
    scope = request.form.get("scope", "full")
    if request.form.get("density") == "more" and detail != "detailed":
        detail = "detailed"
    try:
        answers = json.loads(request.form.get("answers") or "{}")
    except (ValueError, TypeError):
        answers = {}

    sess = SESSIONS.get(sid)
    if sess:                                               # normal two-phase path
        raw, filename = sess["raw"], sess["filename"]
        focus, questions = sess.get("focus", ""), sess.get("questions", [])
    else:                                                  # fallback: direct upload, no questions
        f = request.files.get("file")
        if not f or not f.filename:
            abort(400, "Session expired — please upload the file again.")
        raw, filename = f.read(), f.filename
        focus, questions = request.form.get("focus", ""), []

    prefs = answers_to_prefs(questions, answers)
    try:
        data = analyze(raw, filename, focus=focus, detail=detail, theme=theme,
                       primary_metric=prefs["metric"], primary_dim=prefs["dim"],
                       goal=prefs["goal"], extra_focus=prefs["focus"], scope=scope)
        html = render_dashboard(data)
    except Exception as e:
        traceback.print_exc()
        abort(400, f"Could not build dashboard: {e}")
    name = f"dashboard_{int(time.time())}.html"
    out = os.path.join(GEN, name)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    SESSIONS.pop(sid, None)
    print(f"[ok] {filename} -> {out}  (scope={scope}, {len(data['charts'])} charts, "
          f"{len(data['kpis'])} kpis, model={data['meta']['model']})")
    return {"url": f"/g/{name}"}, 200


if __name__ == "__main__":
    print("\n  Sheet → Dashboard  ·  http://localhost:8077\n")
    app.run(host="127.0.0.1", port=8077, debug=False)
