"""
Report writer for DupliCompare.

A report is a folder, not one giant HTML file:

    <report>/index.html        small page: summary cards, tabs, search, paging
    <report>/data/<tab>-N.js   the rows, in chunks of CHUNK_ROWS (loaded on demand)
    <report>/<tab>.csv         the same rows for Excel / scripts / an AI to process

The page only draws one page of rows at a time (100-500), so it stays fast
with hundreds of thousands of rows, and every tab can be searched, sorted and
exported to CSV from the page.
"""

import csv
import json
import os
from datetime import datetime

CHUNK_ROWS = 5000

# Column types: text | num | size (bytes, shown human readable) | lines (list of strings)


def _csv_cell(value, col_type):
    if col_type == "lines":
        return " | ".join(value)
    if value is None:
        return ""
    return value


def write_report(out_dir, title, info, cards, tabs):
    """Write a report folder and return the path of its index.html.

    info:  list of (label, text) shown under the title (folders, legend...).
    cards: list of (label, text) summary numbers.
    tabs:  list of dicts: id, label, note, cols=[(key, label, type)], rows=[[...]].
    """
    data_dir = os.path.join(out_dir, "data")
    os.makedirs(data_dir, exist_ok=True)

    meta_tabs = []
    for tab in tabs:
        rows = tab["rows"]
        chunks = (len(rows) + CHUNK_ROWS - 1) // CHUNK_ROWS
        for i in range(chunks):
            part = rows[i * CHUNK_ROWS:(i + 1) * CHUNK_ROWS]
            with open(os.path.join(data_dir, f"{tab['id']}-{i}.js"), "w", encoding="utf-8") as f:
                f.write(f"__chunk({json.dumps(tab['id'])},{i},")
                json.dump(part, f, ensure_ascii=True, separators=(",", ":"))
                f.write(");")

        with open(os.path.join(out_dir, f"{tab['id']}.csv"), "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([label for _k, label, _t in tab["cols"]])
            for row in rows:
                writer.writerow([_csv_cell(v, tab["cols"][c][2]) for c, v in enumerate(row)])

        meta_tabs.append({
            "id": tab["id"],
            "label": tab["label"],
            "note": tab.get("note", ""),
            "count": len(rows),
            "chunks": chunks,
            "cols": [{"k": k, "label": label, "t": t} for k, label, t in tab["cols"]],
        })

    meta = {
        "title": title,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "info": [list(p) for p in info],
        "cards": [list(p) for p in cards],
        "tabs": meta_tabs,
    }
    meta_json = json.dumps(meta, ensure_ascii=True).replace("<", "\\u003c")
    page = _PAGE.replace("__META__", meta_json).replace("__TITLE__", _escape(title))
    index_path = os.path.join(out_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(page)
    return index_path


def _escape(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#fafafa;--fg:#222;--card:#fff;--bd:#ddd;--mut:#666;--acc:#1a56db;--hd:#333;--hdfg:#fff;--alt:#f4f4f4}
@media(prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e6e6e6;--card:#20232a;--bd:#363b45;--mut:#9aa0aa;--acc:#7aa2ff;--hd:#2b303a;--hdfg:#e6e6e6;--alt:#1b1e24}}
*{box-sizing:border-box}
body{font-family:Segoe UI,Arial,sans-serif;margin:0;padding:16px;background:var(--bg);color:var(--fg)}
h1{font-size:20px;margin:0 0 4px}
.meta{color:var(--mut);font-size:12px;margin:2px 0}
.info{margin:10px 0;font-size:13px;word-break:break-all}
.info b{display:inline-block;min-width:90px}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0}
.card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:8px 14px;min-width:130px}
.card .v{font-size:18px;font-weight:600}.card .l{font-size:11px;color:var(--mut)}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0 8px}
.tabs button{border:1px solid var(--bd);background:var(--card);color:var(--fg);padding:6px 12px;border-radius:6px;cursor:pointer;font-size:13px}
.tabs button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.tabs button:disabled{opacity:.45;cursor:default}
.note{font-size:12px;color:var(--mut);margin:4px 0 8px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0}
.bar input[type=search]{flex:1;min-width:200px;padding:6px 10px;border:1px solid var(--bd);border-radius:6px;background:var(--card);color:var(--fg)}
.bar select,.bar button{padding:6px 10px;border:1px solid var(--bd);border-radius:6px;background:var(--card);color:var(--fg);cursor:pointer}
.wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;background:var(--card)}
th,td{border:1px solid var(--bd);padding:5px 9px;font-size:13px;text-align:left;vertical-align:top;word-break:break-all}
th{background:var(--hd);color:var(--hdfg);cursor:pointer;white-space:nowrap;user-select:none}
tr:nth-child(even) td{background:var(--alt)}
td.n{text-align:right;white-space:nowrap}
.pager{display:flex;gap:8px;align-items:center;margin:10px 0;font-size:13px}
.pager button{padding:4px 10px;border:1px solid var(--bd);border-radius:6px;background:var(--card);color:var(--fg);cursor:pointer}
.pager button:disabled{opacity:.4;cursor:default}
.empty{padding:20px;text-align:center;color:#2e7d32;font-weight:600}
#status{color:var(--mut);font-size:12px}
</style></head><body>
<h1 id="title"></h1>
<div class="meta" id="gen"></div>
<div class="info" id="info"></div>
<div class="cards" id="cards"></div>
<div class="tabs" id="tabs"></div>
<div class="note" id="note"></div>
<div id="main">
 <div class="bar">
  <input type="search" id="q" placeholder="Search (several words = all must match)...">
  <select id="per"><option>100</option><option selected>200</option><option>500</option></select>
  <button id="csv">Download CSV (filtered)</button>
  <span id="status"></span>
 </div>
 <div class="wrap"><table id="tbl"></table></div>
 <div class="pager" id="pager"></div>
</div>
<script id="meta" type="application/json">__META__</script>
<script>
const META=JSON.parse(document.getElementById('meta').textContent);
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const coll=new Intl.Collator(undefined,{numeric:true,sensitivity:'base'});
function human(n){if(n==null||n<0)return '-';const u=['B','KB','MB','GB','TB','PB'];let i=0;while(n>=1024&&i<u.length-1){n/=1024;i++;}return (i?n.toFixed(2):n)+' '+u[i];}
const tabs=META.tabs;tabs.forEach(t=>{t.rows=[];t.hay=[];});
window.__chunk=(id,n,rows)=>{const t=tabs.find(x=>x.id===id);for(const r of rows)t.rows.push(r);};
let cur=null,q='',page=0,per=200,view=[],sort={c:-1,dir:1};

$('#title').textContent=META.title;
$('#gen').textContent='Generated: '+META.generated;
$('#info').innerHTML=META.info.map(p=>'<div><b>'+esc(p[0])+'</b> '+esc(p[1])+'</div>').join('');
$('#cards').innerHTML=META.cards.map(p=>'<div class="card"><div class="v">'+esc(p[1])+'</div><div class="l">'+esc(p[0])+'</div></div>').join('');
$('#tabs').innerHTML=tabs.map((t,i)=>'<button data-i="'+i+'"'+(t.count?'':' disabled')+'>'+esc(t.label)+' ('+t.count.toLocaleString()+')</button>').join('');
$('#tabs').addEventListener('click',e=>{const b=e.target.closest('button');if(b&&!b.disabled)openTab(tabs[+b.dataset.i]);});

function loadTab(t){
  if(t.p)return t.p;
  t.p=(async()=>{
    for(let i=0;i<t.chunks;i++){
      await new Promise((ok,bad)=>{const s=document.createElement('script');s.src='data/'+t.id+'-'+i+'.js';s.onload=ok;s.onerror=()=>bad(new Error('Cannot load '+s.src));document.head.appendChild(s);});
      if(cur===t)$('#status').textContent='Loading '+t.rows.length.toLocaleString()+' / '+t.count.toLocaleString()+' ...';
    }
    t.hay=t.rows.map(r=>r.map(v=>Array.isArray(v)?v.join(' '):String(v)).join(' ').toLowerCase());
  })();
  return t.p;
}
async function openTab(t){
  cur=t;q='';$('#q').value='';sort={c:-1,dir:1};
  document.querySelectorAll('#tabs button').forEach((b,i)=>b.classList.toggle('on',tabs[i]===t));
  $('#note').textContent=t.note||'';
  $('#status').textContent='Loading ...';$('#tbl').innerHTML='';$('#pager').innerHTML='';
  try{await loadTab(t);}catch(err){$('#status').textContent=err.message+' (keep the data folder next to index.html)';return;}
  if(cur!==t)return;
  refilter();
}
function refilter(){
  const terms=q.toLowerCase().split(/\s+/).filter(Boolean);
  const idx=[];
  for(let i=0;i<cur.rows.length;i++){const h=cur.hay[i];if(terms.every(x=>h.includes(x)))idx.push(i);}
  if(sort.c>=0){
    const c=cur.cols[sort.c],num=c.t==='num'||c.t==='size';
    const key=i=>{let v=cur.rows[i][sort.c];if(Array.isArray(v))v=v[0]||'';return v==null?-1:v;};
    idx.sort((a,b)=>{const x=key(a),y=key(b);return (num?x-y:coll.compare(String(x),String(y)))*sort.dir;});
  }
  view=idx;page=0;render();
}
function cell(c,v){
  if(c.t==='lines')return '<td>'+v.map(s=>'<div>'+esc(s)+'</div>').join('')+'</td>';
  if(c.t==='size')return '<td class="n">'+human(v)+'</td>';
  if(c.t==='num')return '<td class="n">'+(v==null?'-':Number(v).toLocaleString())+'</td>';
  return '<td>'+esc(v==null?'':v)+'</td>';
}
function render(){
  const t=cur,pages=Math.max(1,Math.ceil(view.length/per));
  $('#status').textContent=view.length.toLocaleString()+' of '+t.rows.length.toLocaleString()+' rows';
  const head='<tr>'+t.cols.map((c,i)=>'<th data-c="'+i+'">'+esc(c.label)+(sort.c===i?(sort.dir>0?' ▲':' ▼'):'')+'</th>').join('')+'</tr>';
  const slice=view.slice(page*per,(page+1)*per);
  const body=slice.length?slice.map(i=>'<tr>'+t.cols.map((c,j)=>cell(c,t.rows[i][j])).join('')+'</tr>').join(''):'<tr><td colspan="'+t.cols.length+'" class="empty">Nothing matches.</td></tr>';
  $('#tbl').innerHTML=head+body;
  $('#pager').innerHTML='<button id="p0"'+(page?'':' disabled')+'>&laquo;</button><button id="p1"'+(page?'':' disabled')+'>&lsaquo; Prev</button><span>Page '+(page+1)+' / '+pages+'</span><button id="p2"'+(page<pages-1?'':' disabled')+'>Next &rsaquo;</button><button id="p3"'+(page<pages-1?'':' disabled')+'>&raquo;</button>';
  $('#p0').onclick=()=>{page=0;render();};$('#p1').onclick=()=>{page--;render();};
  $('#p2').onclick=()=>{page++;render();};$('#p3').onclick=()=>{page=pages-1;render();};
}
$('#tbl').addEventListener('click',e=>{const th=e.target.closest('th');if(!th||!cur)return;const c=+th.dataset.c;sort=sort.c===c?{c,dir:-sort.dir}:{c,dir:1};refilter();});
let timer=null;
$('#q').addEventListener('input',e=>{clearTimeout(timer);timer=setTimeout(()=>{q=e.target.value;if(cur)refilter();},200);});
$('#per').addEventListener('change',e=>{per=+e.target.value;page=0;if(cur)render();});
$('#csv').addEventListener('click',()=>{
  if(!cur)return;
  const q2=s=>'"'+String(s).replace(/"/g,'""')+'"';
  const lines=[cur.cols.map(c=>q2(c.label)).join(',')];
  for(const i of view){lines.push(cur.rows[i].map(v=>q2(Array.isArray(v)?v.join(' | '):(v==null?'':v))).join(','));}
  const a=document.createElement('a');a.href=URL.createObjectURL(new Blob(['﻿'+lines.join('\r\n')],{type:'text/csv'}));
  a.download=cur.id+'-filtered.csv';a.click();
});
const first=tabs.find(t=>t.count>0);
if(first)openTab(first);else{$('#main').innerHTML='<div class="empty">Nothing to show — no differences found.</div>';}
</script></body></html>
"""
