# -*- coding: utf-8 -*-
"""The report page: a single quiet column of content."""

from web_style import BASE_CSS
from report_scene import MEDIA_CSS, MEDIA_JS
from report_animation import ANIMATION_CSS, ANIMATION_JS
from report_forms import FORM_CSS, FORM_JS
from report_learning_shell import LEARNING_CSS, LEARNING_JS

REPORT_CSS = r"""
body{overflow:hidden}
.shell{position:relative;display:flex;height:100vh}
main{flex:1;min-width:0;overflow:auto;scroll-behavior:smooth}
header{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:2px;padding:12px 26px 12px 34px;
  background:linear-gradient(var(--bg) 55%,transparent);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
.wordmark{font-size:11px;font-weight:600;letter-spacing:.2em;color:var(--faint);flex:1}
.link{background:none;border:0;color:var(--dim);font-size:13px;padding:6px 11px;border-radius:8px;cursor:pointer;
  transition:color .2s,background .2s}
.link:hover{color:var(--text);background:var(--hover)}
.page{max-width:700px;margin:0 auto;padding:30px 34px 110px;transition:opacity .22s var(--ease-in-out),transform .22s var(--ease-in-out)}
.page.swap-out{opacity:0;transform:translateY(-6px)}
.page .title{margin-top:6px}
#body>*:first-child{margin-top:34px}
/* outline: a drawer, closed by default */
nav{position:fixed;left:0;top:0;bottom:0;width:252px;padding:64px 12px 24px 26px;z-index:8;overflow:auto;
  background:var(--bg);border-right:1px solid var(--line);transform:translateX(-100%);
  transition:transform .4s var(--ease)}
nav.open{transform:none}
nav a{display:block;color:var(--dim);padding:7px 10px;font-size:13.5px;border-radius:8px;transition:color .2s,background .2s}
nav a.l2{padding-left:24px;font-size:13px} nav a:hover{color:var(--text);background:var(--hover);opacity:1}
nav a.on{color:var(--text)}
.scrim{position:fixed;inset:0;z-index:7;background:rgba(0,0,0,.3);opacity:0;pointer-events:none;transition:opacity .4s var(--ease)}
.scrim.on{opacity:1;pointer-events:auto}
.explain-term{display:inline;padding:0 2px;border:0;border-bottom:1px dotted var(--accent);background:var(--accent-soft);color:var(--accent);font:inherit;cursor:pointer;border-radius:3px}
.explain-term:hover,.explain-term:focus-visible{background:var(--accent-soft);outline:1px solid var(--accent)}
#stream-status{font-size:12px;color:var(--dim);margin-right:10px}
@media(max-width:560px){.page{padding-left:22px;padding-right:22px}}
@media print{header,nav,.scrim,.actions{display:none}body{overflow:visible;background:#fff;color:#000}
  :root,:root[data-theme="light"]{--bg:#fff;--surface:#fff;--raise:#f2f2f5;--text:#1c1c1e;--dim:#555;--faint:#666;
    --line:#ddd;--line2:#ccc;--accent:#245fa5;--accent-soft:#eef4fb}
  .shell{display:block;height:auto}main{overflow:visible}.reveal{opacity:1;transform:none;animation:none}}
"""

REPORT_JS = r"""
(function(){
"use strict";
var QS=new URLSearchParams(location.search),TOKEN=QS.get("t")||"",version=-1,source="",currentStream="",polling=false,renderEpoch=0;
if(QS.get("static"))document.documentElement.classList.add("static");
var $=function(id){return document.getElementById(id)};
function api(path,opts){opts=opts||{};opts.headers=Object.assign({"X-Report-Token":TOKEN},opts.headers||{});
  opts.cache="no-store";return fetch(path,opts).then(function(r){return r.json().then(function(body){if(!r.ok)throw Error(body.error||'Request failed ('+r.status+')');return body})})}
function toast(t){var e=$("toast");e.textContent=t;e.classList.add("on");setTimeout(function(){e.classList.remove("on")},1500)}
function stagger(root){var n=0;root.querySelectorAll(".reveal").forEach(function(el){el.style.setProperty("--i",Math.min(n++,16))})}
function fills(root){requestAnimationFrame(function(){requestAnimationFrame(function(){
  root.querySelectorAll(".fill").forEach(function(f){f.style.width=f.style.getPropertyValue("--w")})})})}
function sortable(root){root.querySelectorAll("table.data").forEach(function(t){
  t.querySelectorAll("th").forEach(function(th){th.addEventListener("click",function(){
    var i=+th.dataset.col,asc=!th.classList.contains("asc"),rows=[].slice.call(t.tBodies[0].rows);
    rows.sort(function(a,b){var x=a.cells[i].textContent.trim(),y=b.cells[i].textContent.trim(),
      nx=parseFloat(x.replace(/[^0-9.\-]/g,"")),ny=parseFloat(y.replace(/[^0-9.\-]/g,""));
      var c=(!isNaN(nx)&&!isNaN(ny))?nx-ny:x.localeCompare(y);return asc?c:-c});
    t.querySelectorAll("th").forEach(function(h){h.classList.remove("asc","desc")});
    th.classList.add(asc?"asc":"desc");rows.forEach(function(r){t.tBodies[0].appendChild(r)})})})})}
function copies(root){root.querySelectorAll(".copy").forEach(function(b){b.addEventListener("click",function(){
  var code=b.closest(".code").querySelector("code").textContent;
  (navigator.clipboard?navigator.clipboard.writeText(code):Promise.reject()).then(function(){b.textContent="Copied"},
    function(){b.textContent="Ctrl+C"});setTimeout(function(){b.textContent="Copy"},1300)})})}
function actions(root){root.querySelectorAll(".action").forEach(function(b){b.addEventListener("click",function(){
  b.disabled=true;
  api("/api/command",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({command:b.dataset.command})}).then(function(){toast("Sent")},function(){toast("Could not send")}).finally(function(){b.disabled=false})})});
  root.querySelectorAll('.explain-term').forEach(function(b){b.addEventListener('click',function(){b.disabled=true;
    api('/api/explain',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({term:b.dataset.explain})})
      .then(function(){toast('Preparing explanation')},function(){toast('Could not request explanation')}).finally(function(){b.disabled=false})})})}
function outline(items){var box=$("toc");box.textContent="";items.forEach(function(it){
  var a=document.createElement("a");a.href="#"+it.id;a.textContent=it.text;a.className="l"+it.level;
  a.addEventListener("click",function(e){e.preventDefault();var t=document.getElementById(it.id);
    if(t)t.scrollIntoView({behavior:"smooth"});closeNav()});box.appendChild(a)});
  $("btn-toc").style.display=items.length>=3?"":"none"}
function openNav(){$("nav").classList.add("open");$("scrim").classList.add("on")}
function closeNav(){$("nav").classList.remove("open");$("scrim").classList.remove("on")}
function dispose(node){var items=[].slice.call(node.querySelectorAll('.scene3d,.report-animation,.report-explorer'));if(node.matches&&node.matches('.scene3d,.report-animation,.report-explorer'))items.push(node);items.forEach(function(f){if(f._dispose)f._dispose()})}
function patchContent(markup,preserve){
  var body=$('body'),template=document.createElement('template');template.innerHTML=markup;
  if(!preserve){[].slice.call(body.children).forEach(dispose);body.replaceChildren()}
  var incoming=[].slice.call(template.content.children);incoming.forEach(function(next,i){var raw=next.outerHTML,old=body.children[i];
    if(old&&(old._reportMarkup===raw||(next.dataset.reportKey&&old.dataset.reportKey===next.dataset.reportKey)))return;
    if(old&&old.classList.contains('code')&&next.classList.contains('code')){
      old.querySelector('code').textContent=next.querySelector('code').textContent;old.querySelector('.code-head span').textContent=next.querySelector('.code-head span').textContent;old._reportMarkup=raw;return;}
    next._reportMarkup=raw;var holder=document.createElement('div');holder.appendChild(next);
    stagger(holder);fills(holder);reportMedia(holder);reportAnimations(holder);reportExplorers(holder);reportLessons(holder);reportWalkthroughs(holder);reportQuestionnaires(holder,api);sortable(holder);copies(holder);actions(holder);
    if(old){dispose(old);old.replaceWith(next)}else body.appendChild(next);
  });
  while(body.children.length>incoming.length){dispose(body.lastElementChild);body.lastElementChild.remove()}
}
function apply(r){
  var epoch=++renderEpoch;
  var preserve=!!r.stream_id&&r.stream_id===currentStream,main=$('main'),follow=main.scrollHeight-main.scrollTop-main.clientHeight<90;
  var page=document.querySelector(".page"),run=function(){
    if(epoch!==renderEpoch)return;
    document.title=r.title;$("title").textContent=r.title;
    $("sub").textContent=r.subtitle||new Date().toLocaleString([],{weekday:"long",day:"numeric",month:"long",hour:"2-digit",minute:"2-digit"});
    patchContent(r.html,preserve);source=r.source||"";currentStream=r.stream_id||'';
    $('stream-status').textContent=({writing:'Writing · draft',awaiting_review:'Draft · checking',interrupted:'Draft · interrupted'})[r.phase]||'';
    outline(r.toc||[]);if(!preserve)main.scrollTo({top:0});else if(follow)main.scrollTop=main.scrollHeight;
    page.classList.remove("swap-out")};
  if(version>=0&&!preserve&&!r.phase){page.classList.add("swap-out");setTimeout(run,230)}else run()}
function poll(){if(polling)return;polling=true;api("/api/version").then(function(v){if(v.version!==version){
  return api("/api/report").then(function(r){apply(r);version=v.version})}}).catch(function(){}).finally(function(){polling=false})}
$("btn-copy").onclick=function(){(navigator.clipboard?navigator.clipboard.writeText(source):Promise.reject()).then(function(){toast("Copied")},function(){toast("Clipboard blocked")})};
$("btn-print").onclick=function(){window.print()};
$("btn-toc").onclick=openNav;$("scrim").onclick=closeNav;
document.addEventListener("keydown",function(e){if(e.key==="Escape")closeNav()});
$("btn-theme").onclick=function(){var d=document.documentElement;d.dataset.theme=d.dataset.theme==="light"?"dark":"light";
  try{localStorage.setItem("vanta-theme",d.dataset.theme)}catch(e){}};
try{var saved=localStorage.getItem("vanta-theme");if(saved)document.documentElement.dataset.theme=saved}catch(e){}
poll();setInterval(poll,350);
})();
"""

SHELL = ("""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VANTA</title>
<style>""" + BASE_CSS + REPORT_CSS + MEDIA_CSS + ANIMATION_CSS + FORM_CSS + LEARNING_CSS + """</style></head>
<body>
<div class="scrim" id="scrim"></div>
<nav id="nav"><div id="toc"></div></nav>
<div class="shell"><main id="main">
  <header><span class="wordmark">VANTA</span>
    <span id="stream-status" role="status" aria-live="polite"></span>
    <button class="link" id="btn-toc" style="display:none">Outline</button>
    <button class="link" id="btn-copy">Copy</button>
    <button class="link" id="btn-print">Print</button>
    <button class="link" id="btn-theme">Theme</button></header>
  <div class="page"><h1 class="title" id="title"></h1><div class="sub" id="sub"></div><div id="body"></div></div>
</main></div>
<div class="toast" id="toast"></div>
<script>""" + ANIMATION_JS + MEDIA_JS + FORM_JS + LEARNING_JS + REPORT_JS + """</script></body></html>
""")
