"""V5 — landing dashboard, /articles tracker, /publish queue, /brew gallery.

Blueprint registered by app.py. Reads the livery-pipeline SQLite snapshot at
data/pipeline.db (synced from the Mac); serves organized images from
data/output/. published_status is authoritative ON THE SERVER: 'unpublished'
(= Queueing), 'published', 'discarded' — data/sync_merge.py preserves it when
a fresh snapshot arrives from the Mac.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time

import requests as _rq
from flask import Blueprint, jsonify, request, send_file, abort

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "pipeline.db")
LOCAL_PREFIX = "/Users/ianzhang/livery-pipeline/output/"
SERVER_OUTPUT = os.path.join(BASE, "data", "output")
BREW_DIR = os.path.join(BASE, "data", "brew_jobs")
BREWS = os.path.join(BASE, "data", "brews")
REFS = os.path.join(BASE, "data", "refs")   # Bing reference images (batch download)
os.makedirs(BREWS, exist_ok=True)
os.makedirs(BREW_DIR, exist_ok=True)
os.makedirs(REFS, exist_ok=True)


def _bot_secret():
    """Shared secret for the bot's internal hook = the WeCom callback Token."""
    try:
        for line in open("/root/wecom-bot/wecom.env"):
            if line.startswith("WECOM_TOKEN="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""

v5bp = Blueprint("v5", __name__)


def db():
    c = sqlite3.connect(DB, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=20000")
    return c


def map_path(p):
    if not p:
        return None
    if p.startswith(LOCAL_PREFIX):
        p = os.path.join(SERVER_OUTPUT, p[len(LOCAL_PREFIX):])
    return p if os.path.exists(p) else None


def rowdict(r):
    d = dict(r)
    d["queue_state"] = ("discarded" if d.get("published_status") == "discarded"
                        else "published" if d.get("published_status") == "published"
                        else "queueing" if d.get("article_status") == "done"
                        else d.get("article_status") or "pending")
    return d


# ------------------------------- APIs --------------------------------------
COLS = ("id, airline_name, registration, source_folder, source_credit, license,"
        " is_fictional, ocr_status, classify_status, article_status,"
        " published_status, article_title, article_path, image_path, updated_at")


@v5bp.route("/api/v5/records")
def records():
    c = db()
    rows = [rowdict(r) for r in c.execute(f"SELECT {COLS} FROM images ORDER BY id")]
    c.close()
    return jsonify(rows)


@v5bp.route("/api/v5/next")
def next_queued():
    """Autolocate: first real-airline entry with a finished article, still queueing."""
    c = db()
    r = c.execute(f"""SELECT {COLS} FROM images
        WHERE is_fictional=0 AND article_status='done'
          AND (published_status IS NULL OR published_status NOT IN ('published','discarded'))
        ORDER BY id LIMIT 1""").fetchone()
    c.close()
    return jsonify(rowdict(r) if r else None)


@v5bp.route("/api/v5/status", methods=["POST"])
def set_status():
    b = request.get_json(force=True, silent=True) or {}
    rid, status = int(b.get("id", 0)), (b.get("status") or "").strip().lower()
    if status not in ("unpublished", "published", "discarded"):
        return jsonify({"error": "status must be unpublished|published|discarded"}), 400
    c = db()
    c.execute("UPDATE images SET published_status=? WHERE id=?", (status, rid))
    c.commit()
    c.close()
    return jsonify({"ok": True, "id": rid, "published_status": status})


@v5bp.route("/api/v5/article/<int:rid>")
def article(rid):
    c = db()
    r = c.execute("SELECT article_title, article_path FROM images WHERE id=?",
                  (rid,)).fetchone()
    c.close()
    p = map_path(r["article_path"]) if r else None
    if not p:
        return jsonify({"error": "article not available on server"}), 404
    text = open(p, encoding="utf-8").read().strip()
    title = (r["article_title"] or "").strip()
    # body must never repeat the title: drop a leading '# …' H1 OR a bare first
    # line matching the stored title (regenerated articles save a bare title)
    lines = text.split("\n")
    first = lines[0].lstrip("#").strip().strip("《》").strip() if lines else ""
    t = title.strip("《》").strip()
    body = "\n".join(lines[1:]).lstrip("\n").strip() if (lines and (lines[0].startswith("#") or (t and first == t))) else text
    return jsonify({"id": rid, "title": title, "body": body, "full": text})


@v5bp.route("/api/v5/img/<int:rid>")
def img(rid):
    c = db()
    r = c.execute("SELECT image_path FROM images WHERE id=?", (rid,)).fetchone()
    c.close()
    p = map_path(r["image_path"]) if r else None
    if not p:
        abort(404)
    return send_file(p)


# ------------------------------ brew system --------------------------------
def _manifest_path(bid):
    return os.path.join(BREWS, str(int(bid)), "manifest.json")


def _load_manifest(bid):
    p = _manifest_path(bid)
    return json.load(open(p)) if os.path.exists(p) else {"files": [], "done": False}


def _latest_brew_id():
    p = os.path.join(BREWS, "latest.txt")
    return open(p).read().strip() if os.path.exists(p) else None


@v5bp.route("/api/v5/brew_job/<int:bid>")
def brew_job(bid):
    p = os.path.join(BREW_DIR, f"{bid}.json")
    if not os.path.exists(p):
        abort(404)
    return jsonify(json.load(open(p)))


@v5bp.route("/api/v5/brew_result/<int:bid>", methods=["POST"])
def brew_result(bid):
    f = request.files.get("image")
    name = os.path.basename(request.form.get("name") or (f.filename if f else ""))
    seq = int(request.form.get("seq") or 0)
    if not f or not name:
        return jsonify({"error": "image+name required"}), 400
    d = os.path.join(BREWS, str(bid))
    os.makedirs(d, exist_ok=True)
    fname = f"{seq:02d}-{name}"
    f.save(os.path.join(d, fname))
    m = _load_manifest(bid)
    kind = "datasheet" if "_datasheet" in name else "poster"
    mm = re.search(r"_([a-z]+)_(?:poster|datasheet)\.png$", name)
    m["files"] = [x for x in m["files"] if x["file"] != fname]
    m["files"].append({"file": fname, "seq": seq, "kind": kind,
                       "style": mm.group(1) if mm else f"style{seq}"})
    m["files"].sort(key=lambda x: x["seq"])
    json.dump(m, open(_manifest_path(bid), "w"))
    return jsonify({"ok": True, "saved": fname})


@v5bp.route("/api/v5/brew_done/<int:bid>", methods=["POST"])
def brew_done(bid):
    m = _load_manifest(bid)
    m["done"] = True
    m["finished_at"] = time.time()
    # number the styles in order of first appearance
    order, styles = [], {}
    for x in m["files"]:
        if x["style"] not in styles:
            order.append(x["style"])
            styles[x["style"]] = len(order)
        x["num"] = styles[x["style"]]
    json.dump(m, open(_manifest_path(bid), "w"))
    open(os.path.join(BREWS, "latest.txt"), "w").write(str(bid))
    # prune: keep the newest 10 brew folders so data/brews can't eat the disk
    try:
        dirs = sorted((d for d in os.listdir(BREWS)
                       if d.isdigit() and os.path.isdir(os.path.join(BREWS, d))),
                      key=lambda d: os.path.getmtime(os.path.join(BREWS, d)))
        import shutil as _sh
        for old in dirs[:-10]:
            _sh.rmtree(os.path.join(BREWS, old), ignore_errors=True)
    except Exception:
        pass
    # poke the bot so it messages the owner (guarded by the shared secret)
    try:
        _rq.post("http://127.0.0.1:8642/internal/brew_done",
                 json={"entry_id": bid, "count": len(m["files"]),
                       "secret": _bot_secret()}, timeout=8)
    except Exception:
        pass
    return jsonify({"ok": True, "styles": len(order), "files": len(m["files"])})


@v5bp.route("/api/v5/brew_manifest/<bid>")
def brew_manifest(bid):
    if bid == "latest":
        bid = _latest_brew_id() or "0"
    m = _load_manifest(bid)
    m["id"] = int(bid)
    return jsonify(m)


PREVIEW_MAX = 960   # px long side for on-page infographic previews


def _brew_preview(full_path, bid, fname):
    """Small JPEG preview of a rendered infographic, cached under the brew's
    .prev/ dir. The page shows these so it loads fast; the full-size PNG still
    goes to WeCom on a number reply (owner 2026-07-14)."""
    prev = os.path.join(BREWS, str(bid), ".prev",
                        os.path.splitext(os.path.basename(fname))[0] + ".jpg")
    if os.path.exists(prev) and os.path.getmtime(prev) >= os.path.getmtime(full_path):
        return prev
    os.makedirs(os.path.dirname(prev), exist_ok=True)
    from PIL import Image
    with Image.open(full_path) as im:
        im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > PREVIEW_MAX:
            s = PREVIEW_MAX / max(w, h)
            im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
        im.save(prev, format="JPEG", quality=80, optimize=True)
    return prev


@v5bp.route("/api/v5/brew_img/<int:bid>/<path:fname>")
def brew_img(bid, fname):
    p = os.path.join(BREWS, str(bid), os.path.basename(fname))
    if not os.path.exists(p):
        abort(404)
    if request.args.get("p"):          # ?p=1 → compressed preview for page display
        try:
            return send_file(_brew_preview(p, bid, fname), mimetype="image/jpeg")
        except Exception:
            pass                        # any trouble → fall through to full-size
    return send_file(p)


# ------------------------------- pages -------------------------------------
STYLE = """<style>
:root{--bg:#0a0c0a;--panel:#101310;--panel2:#161a16;--line:#262c26;--text:#e7e5e4;
--dim:#78716b;--green:#37f712;--blue:#00a6f4;--danger:#ff2157;--warning:#fe9900;
--fmono:"Space Mono",ui-monospace,Menlo,monospace;}
*{box-sizing:border-box;margin:0;padding:0;border-radius:0!important}
body{background:var(--bg);color:var(--text);font-family:var(--fmono);min-height:100vh;font-size:14px}
::selection{background:var(--green);color:#000}
header{padding:20px 32px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px}
header h1{font-size:16px;font-weight:700;letter-spacing:1px;text-transform:uppercase}
header h1::before{content:"▮ ";color:var(--green)}
a{color:var(--blue);text-decoration:none} .grn{color:var(--green)}
main{padding:28px 32px;max-width:1280px;margin:0 auto}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
.tile{background:var(--panel);border:1px solid var(--line);padding:26px 22px;display:block;color:var(--text)}
.tile:hover{border-color:var(--green)} .tile h2{font-size:15px;letter-spacing:1px;text-transform:uppercase}
.tile h2::before{content:"▮ ";color:var(--green)} .tile p{color:var(--dim);margin-top:8px;font-size:12px;line-height:1.6}
.tile .num{color:var(--green);font-size:26px;font-weight:700;margin-top:10px}
table{border-collapse:collapse;width:100%;margin-top:14px}
th,td{border-bottom:1px solid var(--line);padding:7px 9px;text-align:left;font-size:12px;vertical-align:middle}
th{color:var(--dim);text-transform:uppercase;font-size:10px;letter-spacing:1px}
tr.next{outline:2px solid var(--green)} img.th{height:36px;background:#fff}
button{background:var(--panel2);border:1px solid var(--line);color:var(--text);font-family:var(--fmono);
padding:6px 12px;cursor:pointer;font-size:12px} button:hover{border-color:var(--green)}
button.p{color:var(--green)} button.d{color:var(--danger)} button.q{color:var(--warning)}
.badge{font-size:10px;padding:1px 7px;border:1px solid var(--line);text-transform:uppercase;letter-spacing:1px}
.badge.queueing{color:var(--warning);border-color:var(--warning)}
.badge.published{color:var(--green);border-color:var(--green)}
.badge.discarded{color:var(--danger);border-color:var(--danger)}
.badge.skipped,.badge.pending,.badge.deferred,.badge.insufficient{color:var(--dim)}
.box{background:var(--panel);border:1px solid var(--line);padding:18px;margin-top:16px;white-space:pre-wrap;line-height:1.7}
.bar{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px;align-items:center}
input.flt{background:var(--panel2);border:1px solid var(--line);color:var(--text);padding:7px 10px;font-family:var(--fmono)}
</style>"""

DASH = """<!doctype html><html><head><meta charset="utf-8">
<title>IZhang Aviation · Mission Control</title>{style}
<style>
/* dash-only: fixed full-viewport 2×2 grid, no scrolling — each tile is a
   quarter of the page (100dvh handles iOS toolbar chrome) */
html,body{{height:100%;overflow:hidden}}
body{{display:flex;flex-direction:column;height:100dvh}}
main{{flex:1;min-height:0;padding:0;max-width:none;margin:0}}
.tiles{{height:100%;display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;gap:0}}
.tile{{display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;
 padding:0 26px;border:none;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}}
.tile:nth-child(2n){{border-right:none}}
.tile:nth-child(n+3){{border-bottom:none}}
.tile:hover{{background:var(--panel2);border-color:var(--line)}}
.tile h2{{font-size:17px;line-height:1.5}}
.tile p{{max-width:420px}}
#quota{{position:fixed;right:14px;bottom:10px;font-size:11px;color:var(--dim);letter-spacing:.5px}}
#quota b{{color:var(--green);font-weight:700}}
</style></head><body>
<header><h1>IZhang Aviation — Mission Control <span class="grn">V5.7</span></h1></header>
<main><div class="tiles">
 <a class="tile" href="/aircraft"><h2>✈ Aircraft Infographic<br>飞机信息图工坊</h2>
   <p>Side-profile → 10-style poster + datasheet generator (the classic workbench).</p></a>
 <a class="tile" href="/rocket"><h2>🚀 Rocket Infographic<br>火箭信息图工坊</h2>
   <p>Launch-vehicle spec → poster + tall datasheet.</p></a>
 <a class="tile" href="/publish"><h2>📤 Publish Next<br>下一篇待发布</h2>
   <p id="nx">Loading queue…</p><div class="num" id="qn"></div></a>
 <a class="tile" href="/articles"><h2>🗂 Article Tracker<br>图文数据库</h2>
   <p id="cnt">Loading…</p></a>
</div></main>
<div id="quota">BG-removal quota: …</div>
<div id="dsbal" style="position:fixed;right:14px;bottom:28px;font-size:11px;color:var(--dim);letter-spacing:.5px"></div>
<script>
// live queued count — re-poll so the dashboard tracks the Mac→server DB sync
// instead of freezing at whatever it was on first page load.
function refreshCounts(){{
 fetch('/api/v5/records').then(r=>r.json()).then(rs=>{{
  const q=rs.filter(r=>r.queue_state==='queueing'), p=rs.filter(r=>r.queue_state==='published');
  document.getElementById('qn').textContent=q.length+' queueing';
  document.getElementById('nx').textContent=q[0]?('Next: '+(q[0].airline_name||'?')+' '+(q[0].registration||'')):'Queue empty';
  document.getElementById('cnt').textContent=rs.length+' records · '+p.length+' published · '+
    rs.filter(r=>r.is_fictional==1).length+' fictional';
 }}).catch(()=>{{}});
}}
refreshCounts(); setInterval(refreshCounts, 15000);
fetch('/api/removebg_quota').then(r=>r.json()).then(j=>{{
 document.getElementById('quota').innerHTML = j.error ? 'BG-removal quota: unavailable'
  : 'BG-removal quota (used / free): <b>'+j.quota_used+' / '+j.quota_free+'</b>'
    +(j.balance!=null?' · balance <b>$'+j.balance+'</b>':'');
}}).catch(()=>{{}});
fetch('/api/deepseek_balance').then(r=>r.json()).then(j=>{{
 const el=document.getElementById('dsbal'); if(!el) return;
 el.innerHTML = j.error ? '' : 'DeepSeek balance: <b>'+j.total_balance+' '+(j.currency||'')+'</b>';
}}).catch(()=>{{}});
</script></body></html>"""

ARTICLES = """<!doctype html><html><head><meta charset="utf-8">
<title>Article Tracker · IZhang Aviation</title>{style}</head><body>
<header><h1>Article Tracker <span class="grn">V5.7</span></h1>
 <a style="margin-left:auto" href="/">← Mission Control</a></header>
<main>
<div class="bar">
 <button onclick="F='all';draw()">All</button>
 <button class="q" onclick="F='queueing';draw()">Queueing</button>
 <button class="p" onclick="F='published';draw()">Published</button>
 <button class="d" onclick="F='discarded';draw()">Discarded</button>
 <button onclick="F='fictional';draw()">Fictional</button>
 <input class="flt" id="s" placeholder="search airline / reg" oninput="draw()">
 <span id="n" style="color:var(--dim)"></span>
</div>
<table><thead><tr><th></th><th>id</th><th>airline</th><th>reg</th><th>credit</th>
<th>license</th><th>state</th><th>set status</th><th>article</th><th></th></tr></thead>
<tbody id="tb"></tbody></table></main>
<script>
let R=[],F='all',LIM=250;
function esc(s){{return (s||'').replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]))}}
async function load(){{R=await (await fetch('/api/v5/records')).json();draw();locate();}}
function draw(){{
 const s=document.getElementById('s').value.toLowerCase();
 const all=R.filter(r=> (F==='all'||(F==='fictional'?r.is_fictional==1:r.queue_state===F))
   && ((r.airline_name||'')+' '+(r.registration||'')).toLowerCase().includes(s));
 const rows=all.slice(0,LIM);
 document.getElementById('n').innerHTML=rows.length+' shown / '+all.length+' matched / '+R.length
   +(all.length>LIM?' &nbsp;<a onclick="LIM+=500;draw()">load +500</a>':'');
 document.getElementById('tb').innerHTML=rows.map(r=>
  '<tr id="row'+r.id+'"'+(r.queue_state==='queueing'&&r.id===nextId()?' class="next"':'')+'>'
  +'<td><a href="/aircraft?dbimg='+r.id+'" title="open in generator"><img class="th" loading="lazy" src="/api/v5/img/'+r.id+'" onerror="this.style.display=\\'none\\'"></a></td>'
  +'<td>'+r.id+'</td><td>'+esc(r.airline_name)+'</td><td>'+esc(r.registration)+'</td>'
  +'<td>'+esc(r.source_credit||'—')+'</td><td>'+esc(r.license||'')+'</td>'
  +'<td><span class="badge '+r.queue_state+'">'+r.queue_state+'</span></td>'
  +'<td><button class="q" onclick="st('+r.id+',\\'unpublished\\')">Q</button> '
      +'<button class="p" onclick="st('+r.id+',\\'published\\')">P</button> '
      +'<button class="d" onclick="st('+r.id+',\\'discarded\\')">D</button></td>'
  +'<td>'+(r.article_title?('<a href="/publish?id='+r.id+'">'+esc(r.article_title.slice(0,26))+'…</a>'):'—')+'</td>'
  +'<td><a href="/aircraft?dbimg='+r.id+'">→ generator</a></td></tr>').join('');
}}
function nextId(){{const q=R.filter(r=>r.queue_state==='queueing');return q[0]?q[0].id:-1}}
function locate(){{const el=document.getElementById('row'+nextId()); if(el) el.scrollIntoView({{block:'center'}});}}
async function st(id,s){{await fetch('/api/v5/status',{{method:'POST',headers:{{'Content-Type':'application/json'}},
 body:JSON.stringify({{id:id,status:s}})}}); await load();}}
load();
// keep the queued count + list live (server DB syncs from the Mac); preserve
// scroll so an auto-refresh never yanks the page while you're reading.
async function refresh(){{try{{const y=window.scrollY;
 R=await (await fetch('/api/v5/records')).json();draw();window.scrollTo(0,y);}}catch(e){{}}}}
setInterval(refresh, 15000);
</script></body></html>"""

PUBLISH = """<!doctype html><html><head><meta charset="utf-8">
<title>Publish Next · IZhang Aviation</title>{style}</head><body>
<header><h1>Publish Next <span class="grn">V5.7</span></h1>
 <a style="margin-left:auto" href="/articles">tracker</a>&nbsp;·&nbsp;<a href="/">← Mission Control</a></header>
<main id="m">Loading…</main>
<script>
const qid=new URLSearchParams(location.search).get('id');
function esc(s){{return (s||'').replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]))}}
async function load(){{
 let rec = qid ? (await (await fetch('/api/v5/records')).json()).find(r=>r.id==qid)
               : await (await fetch('/api/v5/next')).json();
 const m=document.getElementById('m');
 if(!rec){{m.innerHTML='<div class="box">🎉 Queue empty — nothing waiting to publish.</div>';return}}
 let art={{title:'',body:''}};
 try{{const a=await fetch('/api/v5/article/'+rec.id); if(a.ok) art=await a.json();}}catch(e){{}}
 m.innerHTML =
  '<div class="bar"><span class="badge '+rec.queue_state+'">'+rec.queue_state+'</span>'
  +'<b>'+esc(rec.airline_name)+'</b> '+esc(rec.registration)
  +' <span style="color:var(--dim)">credit: '+esc(rec.source_credit||'none')+' · '+esc(rec.license||'')+'</span>'
  +'<a style="margin-left:auto" href="/aircraft?dbimg='+rec.id+'">→ open in generator</a></div>'
  +'<div class="box" style="text-align:center"><img style="max-width:100%;max-height:38vh;background:#fff" src="/api/v5/img/'+rec.id+'" onerror="this.outerHTML=\\'(image not on server yet)\\'"></div>'
  +'<div class="bar"><button onclick="cp(TITLE,this)">⧉ COPY TITLE</button>'
  +'<button onclick="cp(BODY,this)">⧉ COPY BODY</button>'
  +'<button class="p" onclick="st('+rec.id+',\\'published\\')">✔ MARK PUBLISHED</button>'
  +'<button class="d" onclick="st('+rec.id+',\\'discarded\\')">✖ DISCARD</button></div>'
  +'<div class="box"><b class="grn">TITLE</b>\\n'+esc(art.title||'(article not on server yet)')+'</div>'
  +'<div class="box"><b class="grn">BODY</b>\\n'+esc(art.body||'')+'</div>';
 window.TITLE=art.title||''; window.BODY=art.body||'';
}}
function cp(t,b){{navigator.clipboard.writeText(t).then(()=>{{b.textContent='✓ copied';setTimeout(()=>b.textContent=b.textContent.replace('✓ copied','⧉ copied'),900)}})}}
async function st(id,s){{await fetch('/api/v5/status',{{method:'POST',headers:{{'Content-Type':'application/json'}},
 body:JSON.stringify({{id:id,status:s}})}}); location.href='/publish';}}
load();
</script></body></html>"""


# NB: uses __STYLE__/.replace instead of .format — the JS below is brace-heavy
# and doubling every {} is how templates rot.
BREW_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Brew Gallery · IZhang Aviation</title>__STYLE__
<style>
 main{max-width:720px;padding:16px}
 .set{margin-bottom:30px}
 .sethead{display:flex;align-items:center;gap:10px;margin-bottom:2px}
 .sethead .numtag{background:var(--green);color:#000;font-weight:700;font-size:18px;padding:2px 12px}
 .sname{font-size:13px;text-transform:uppercase;letter-spacing:1px}
 .sethead button{margin-left:auto;white-space:nowrap}
 .cap{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:1px;margin:10px 0 3px}
 .bcard{background:var(--panel);border:1px solid var(--line)}
 .bcard img{width:100%;display:block}
 .hint{color:var(--dim);font-size:12px;margin:10px 0;line-height:1.6}
 header button{white-space:nowrap}
 .art{background:var(--panel);border:1px solid var(--line);margin-bottom:22px}
 .art summary{cursor:pointer;padding:12px 14px;font-size:13px;list-style:none;display:flex;align-items:center;gap:10px}
 .art summary::-webkit-details-marker{display:none}
 .art .cp{margin-left:auto;background:var(--panel2);border:1px solid var(--line);color:var(--green);
  font-family:var(--fmono);font-size:11px;padding:5px 12px;cursor:pointer;white-space:nowrap}
 .art .body{padding:0 14px 14px;white-space:pre-wrap;line-height:1.85;font-size:14px;
  border-top:1px solid var(--line);max-height:60vh;overflow:auto}
 .art .tt{color:var(--green);font-weight:700}
</style></head><body>
<header><h1>Brew Gallery <span class="grn" id="bid"></span></h1>
 <button class="p" style="margin-left:auto" onclick="saveAll(this)">⬇ SAVE ALL</button></header>
<main>
<div id="wxhint" class="hint" style="display:none;border:1px solid var(--warning);color:var(--warning);padding:10px">
 ⚠️ WeCom's built-in browser can't batch-save to Photos. Tap <b>···</b> (top-right) →
 <b>Open in Safari/Browser</b>, then SAVE ALL / SAVE work. (Or long-press a single image to save it here.)</div>
<div id="artbox"></div>
<div class="hint">Reply the <b style="color:var(--green)">number</b> in WeCom to receive that
style's full-resolution set — or use <b style="color:var(--green)">SAVE</b>: on iPhone the share
sheet opens with “Save Images”, which drops them straight into your photo gallery.
This page always shows the latest brew (auto-refreshes).</div>
<div class="g" id="g">Loading…</div></main>
<script>
const pathBid = location.pathname.split('/').filter(Boolean)[1] || 'latest';
let last='', M=null;
const fileCache={};   // label -> File[]  (so a retry tap shares instantly)
function imgUrl(f){ return '/api/v5/brew_img/'+M.id+'/'+encodeURIComponent(f.file); }
function previewUrl(f){ return imgUrl(f)+'?p=1'; }   // small, fast-loading preview
async function collect(files,label){
 if(fileCache[label]) return fileCache[label];
 const out=[];
 for(const f of files){
  const b=await (await fetch(imgUrl(f))).blob();   // images already shown → HTTP-cache hit
  out.push(new File([b], f.file.replace(/[^\\w.\\-]+/g,'_'),
                    {type:b.type||'image/png', lastModified:Date.now()}));
 }
 fileCache[label]=out; return out;
}
const IN_WX = /wxwork|micromessenger/i.test(navigator.userAgent);
async function saveFiles(files,label,btn){
 const old=btn.textContent; btn.textContent='…'; btn.disabled=true;
 try{
  const fs=await collect(files,label);
  if(navigator.canShare && navigator.canShare({files:fs})){
   await navigator.share({files:fs, title:'Brew #'+M.id+' '+label});
  }else if(IN_WX){             // WeCom webview blocks the share-sheet — guide to Safari
   const h=document.getElementById('wxhint'); if(h){h.style.display='block'; window.scrollTo(0,0);}
  }else{                       // desktop fallback: plain downloads
   fs.forEach(f=>{const a=document.createElement('a');
    a.href=URL.createObjectURL(f); a.download=f.name; a.click();});
  }
 }catch(e){
  if(e.name==='NotAllowedError'){ alert('iOS timed the tap out while fetching — tap the button once more (images are ready now).'); }
  else if(e.name!=='AbortError'){ alert('Save failed: '+e); }
 }
 btn.textContent=old; btn.disabled=false;
}
function saveSet(num,btn){
 saveFiles((M.files||[]).filter(f=>String(f.num)===String(num)),'style '+num,btn);
}
function saveAll(btn){ if(M&&M.files&&M.files.length) saveFiles(M.files,'all styles',btn); }
function render(){
 const groups={};
 (M.files||[]).forEach(f=>{ (groups[f.num]=groups[f.num]||[]).push(f); });
 document.getElementById('g').innerHTML = Object.keys(groups).sort((a,b)=>a-b).map(n=>{
  const g=groups[n];
  return '<div class="set"><div class="sethead"><span class="numtag">'+n+'</span>'
   +'<span class="sname">'+g[0].style+'</span>'
   +'<button onclick="saveSet('+n+',this)">⬇ SAVE</button></div>'
   +g.map(f=>'<div class="cap">'+f.kind+'</div><div class="bcard">'
     +'<img loading="lazy" src="'+previewUrl(f)+'"></div>').join('')
   +'</div>';
 }).join('') || '<div class="hint">No images yet — rendering in progress.</div>';
}
function copyText(t,btn){
 const done=()=>{const o=btn.textContent;btn.textContent='✓ copied';setTimeout(()=>btn.textContent=o,1500);};
 if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(done,()=>fallback(t,done));}
 else fallback(t,done);
}
function fallback(t,done){const ta=document.createElement('textarea');ta.value=t;ta.style.position='fixed';ta.style.opacity=0;
 document.body.appendChild(ta);ta.focus();ta.select();try{document.execCommand('copy');done();}catch(e){}ta.remove();}
let artLoaded=false;
async function loadArticle(id){
 if(artLoaded||!id) return; artLoaded=true;
 try{
  const r=await fetch('/api/v5/article/'+id); if(!r.ok) return;
  const j=await r.json(); if(!j.title && !j.body) return;
  const esc=s=>(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;');
  document.getElementById('artbox').innerHTML=
   '<details class="art" open><summary><span class="tt">📄 '+esc(j.title)+'</span>'
   +'<button class="cp" onclick="event.preventDefault();copyText(TITLE,this)">Copy title</button>'
   +'<button class="cp" onclick="event.preventDefault();copyText(BODY,this)">Copy body</button>'
   +'</summary><div class="body">'+esc(j.body)+'</div></details>';
  window.TITLE=j.title||''; window.BODY=j.body||'';
 }catch(e){}
}
async function load(){
 const m = await (await fetch('/api/v5/brew_manifest/'+pathBid)).json();
 loadArticle(m.id);
 const sig = JSON.stringify(m.files ? m.files.map(f=>f.file) : []);
 if(sig===last) return; last=sig; M=m;
 for(const k in fileCache) delete fileCache[k];
 document.getElementById('bid').textContent = '#'+(m.id||'—') + (m.done?'':' · rendering…');
 render();
}
if(IN_WX) window.addEventListener('load',()=>{const h=document.getElementById('wxhint'); if(h) h.style.display='block';});
load(); setInterval(load, 10000);
</script></body></html>"""


@v5bp.route("/brew")
@v5bp.route("/brew/<int:bid>")
def brew_page(bid=None):
    return BREW_PAGE.replace("__STYLE__", STYLE)


# --------------------------- Bing reference images -------------------------
@v5bp.route("/api/v5/ref_manifest/<bid>")
def ref_manifest(bid):
    d = os.path.join(REFS, os.path.basename(str(bid)))
    if not os.path.isdir(d):
        return jsonify({"id": bid, "files": []})
    files = sorted(f for f in os.listdir(d)
                   if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")))
    return jsonify({"id": bid, "files": files})


@v5bp.route("/api/v5/ref_img/<bid>/<path:fname>")
def ref_img(bid, fname):
    p = os.path.join(REFS, os.path.basename(str(bid)), os.path.basename(fname))
    if not os.path.exists(p):
        abort(404)
    return send_file(p)




REFS_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reference Images · IZhang Aviation</title>__STYLE__
<style>
 main{max-width:720px;padding:16px} .hint{color:var(--dim);font-size:12px;margin:10px 0;line-height:1.6}
 .rcard{background:var(--panel);border:1px solid var(--line);margin-bottom:14px}
 .rcard img{width:100%;display:block}
 header button{white-space:nowrap}
</style></head><body>
<header><h1>Reference Images <span class="grn" id="rid"></span></h1>
 <button class="p" style="margin-left:auto" onclick="saveAll(this)">⬇ SAVE ALL</button></header>
<main>
<div id="wxhint" class="hint" style="display:none;border:1px solid var(--warning);color:var(--warning);padding:10px">
 ⚠️ WeCom's built-in browser can't batch-save to Photos. Tap <b>···</b> (top-right) →
 <b>Open in Safari/Browser</b>, then SAVE ALL drops the whole set into Photos in one tap.
 (Or long-press any single image to save it here.)</div>
<div class="hint">Bing reference photos for this livery. Tap <b style="color:var(--green)">SAVE ALL</b> —
on iPhone (Safari) the share sheet's “Save Images” saves the whole set into Photos in one go.</div>
<div id="g">Loading…</div></main>
<script>
const bid = location.pathname.split('/').filter(Boolean)[1];
const IN_WX = /wxwork|micromessenger/i.test(navigator.userAgent);
let FILES=[];
function imgUrl(f){ return '/api/v5/ref_img/'+bid+'/'+encodeURIComponent(f); }
function canShareFiles(fs){ return navigator.canShare && navigator.canShare({files:fs}); }
async function saveAll(btn){
 if(!FILES.length) return;
 const old=btn.textContent; btn.textContent='…'; btn.disabled=true;
 try{
  const fs=[];
  for(const f of FILES){ const b=await (await fetch(imgUrl(f))).blob();
   fs.push(new File([b], f.replace(/[^\\w.\\-]+/g,'_'), {type:b.type||'image/jpeg'})); }
  if(canShareFiles(fs)){ await navigator.share({files:fs, title:'Reference images'}); }
  else {
   // WeCom webview blocks the file share-sheet — DON'T spray open tabs; guide to Safari
   document.getElementById('wxhint').style.display='block';
   window.scrollTo(0,0);
  }
 }catch(e){ if(e.name!=='AbortError') alert('Save failed: '+e); }
 btn.textContent=old; btn.disabled=false;
}
async function load(){
 if(IN_WX) document.getElementById('wxhint').style.display='block';
 const m=await (await fetch('/api/v5/ref_manifest/'+bid)).json();
 FILES=m.files||[];
 document.getElementById('rid').textContent='#'+bid+' · '+FILES.length+' imgs';
 document.getElementById('g').innerHTML = FILES.length
  ? FILES.map(f=>'<div class="rcard"><img loading="lazy" src="'+imgUrl(f)+'"></div>').join('')
  : '<div class="hint">No reference images yet.</div>';
}
load();
</script></body></html>"""


@v5bp.route("/refs/<bid>")
def refs_page(bid):
    return REFS_PAGE.replace("__STYLE__", STYLE)


@v5bp.route("/")
def dashboard():
    return DASH.format(style=STYLE)


@v5bp.route("/articles")
def articles_page():
    return ARTICLES.format(style=STYLE)


@v5bp.route("/publish")
def publish_page():
    return PUBLISH.format(style=STYLE)
