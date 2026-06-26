"""server.py — localhost web app: drop a sheet, get a dashboard built by a local model."""
import os
import time
import traceback

from flask import Flask, request, send_file, abort

from analyzer import analyze
from render import render_dashboard

BASE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(BASE, "generated")
os.makedirs(GEN, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB

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
@media(max-width:560px){.themes{grid-template-columns:repeat(2,1fr)}.row{grid-template-columns:1fr}}
</style></head><body>
<div class="card">
 <div class="kick">Local · offline · model-built</div>
 <h1>Sheet → Dashboard</h1>
 <p class="sub">Drop an Excel-exported <b>.html</b> (or .xlsx / .csv). A local model reads the data
  and designs an interactive dashboard — KPIs, charts, statistics and insights. Works on a
  different sheet every time, and you control the focus and look below.</p>
 <form id="form">
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

   <div class="opts">
     <div>
       <label class="fld">What should it focus on? (optional)</label>
       <input class="txt" id="focus" placeholder="e.g. revenue by region, top performers, costs over time"/>
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

   <button class="go" id="go" type="submit" disabled>Generate dashboard</button>
   <div class="status" id="status"><span class="spin"></span><span id="stxt">Analysing locally…</span></div>
 </form>
 <div class="foot">100% offline · powered by your local Ollama models</div>
</div>
<script>
const $=id=>document.getElementById(id);
const drop=$('drop'),file=$('file'),fname=$('fname'),go=$('go'),form=$('form'),
 status=$('status'),stxt=$('stxt');
let theme='grid';
document.querySelectorAll('.theme').forEach(el=>el.addEventListener('click',()=>{
  document.querySelectorAll('.theme').forEach(t=>t.classList.remove('on'));
  el.classList.add('on');theme=el.dataset.theme;}));
file.addEventListener('change',()=>{if(file.files.length){fname.textContent='✓ '+file.files[0].name;go.disabled=false;}});
['dragover','dragenter'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('hov');}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('hov');}));
drop.addEventListener('drop',ev=>{if(ev.dataTransfer.files.length){file.files=ev.dataTransfer.files;file.dispatchEvent(new Event('change'));}});
form.addEventListener('submit',async ev=>{
  ev.preventDefault();if(!file.files.length)return;
  go.disabled=true;status.style.display='block';
  const msgs=['Extracting tables…','Profiling columns…','Asking the local model…','Computing statistics…','Rendering dashboard…'];
  let mi=0;const tick=setInterval(()=>{stxt.textContent=msgs[mi=(mi+1)%msgs.length];},2200);
  const fd=new FormData();fd.append('file',file.files[0]);
  fd.append('focus',$('focus').value);fd.append('detail',$('detail').value);
  fd.append('density',$('density').value);fd.append('theme',theme);
  try{
    const r=await fetch('/generate',{method:'POST',body:fd});
    clearInterval(tick);
    if(!r.ok){const t=await r.text();throw new Error(t||('HTTP '+r.status));}
    const {url}=await r.json();stxt.textContent='Opening dashboard…';
    window.location.href=url;
  }catch(e){
    clearInterval(tick);status.innerHTML='<span style="color:#b8502d">⚠ '+e.message+'</span>';go.disabled=false;
  }
});
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


@app.route("/generate", methods=["POST"])
def generate():
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400, "No file uploaded.")
    raw = f.read()
    focus = request.form.get("focus", "")
    detail = request.form.get("detail", "standard")
    theme = request.form.get("theme", "grid")
    if request.form.get("density") == "more" and detail != "detailed":
        detail = "detailed"
    try:
        data = analyze(raw, f.filename, focus=focus, detail=detail, theme=theme)
        html = render_dashboard(data)
    except Exception as e:
        traceback.print_exc()
        abort(400, f"Could not build dashboard: {e}")
    name = f"dashboard_{int(time.time())}.html"
    out = os.path.join(GEN, name)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"[ok] {f.filename} -> {out}  ({len(data['charts'])} charts, "
          f"{len(data['kpis'])} kpis, model={data['meta']['model']})")
    return {"url": f"/g/{name}"}, 200


if __name__ == "__main__":
    print("\n  Sheet → Dashboard  ·  http://localhost:8077\n")
    app.run(host="127.0.0.1", port=8077, debug=False)
