# -*- coding: utf-8 -*-
"""The console page: VANTA's dashboard, drawn as a document rather than as widgets."""

from web_style import BASE_CSS

CONSOLE_CSS = r"""
body{overflow:hidden}
.app{display:grid;grid-template-columns:228px 1fr;height:100vh}
aside{display:flex;flex-direction:column;padding:26px 14px 20px;border-right:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:12px;padding:0 10px 26px;font-weight:620;letter-spacing:.18em;font-size:12px;color:var(--dim)}
.nav{display:flex;flex-direction:column;gap:2px}
.nav button{display:flex;align-items:center;gap:11px;text-align:left;background:none;border:0;color:var(--dim);
  padding:9px 12px;border-radius:9px;cursor:pointer;font-size:14px;transition:color .2s,background .25s var(--ease)}
.nav button:hover{background:var(--hover);color:var(--text)}
.nav button.on{background:var(--raise);color:var(--text)}
.nav svg{width:17px;height:17px;stroke:currentColor;fill:none;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;flex:none}
.side-foot{margin-top:auto;padding:0 10px}
.state-row{display:flex;align-items:center;gap:12px}
.state-name{font-size:13.5px;font-weight:550} .state-sub{font-size:12px;color:var(--faint)}
/* the soul, in CSS: three soft lights drifting inside a dark sphere */
.soul{position:relative;width:40px;height:40px;border-radius:50%;overflow:hidden;flex:none;background:#0d0d10;
  box-shadow:inset 0 0 0 1px var(--line)}
.soul i{position:absolute;width:26px;height:26px;border-radius:50%;filter:blur(7px);opacity:.9;mix-blend-mode:screen;
  transition:background 1.2s var(--ease),opacity 1.2s var(--ease)}
.soul i:nth-child(1){left:2px;top:3px;background:var(--s1);animation:o1 11s ease-in-out infinite alternate}
.soul i:nth-child(2){left:11px;top:11px;background:var(--s2);animation:o2 14s ease-in-out infinite alternate}
.soul i:nth-child(3){left:5px;top:12px;background:var(--s3);animation:o3 17s ease-in-out infinite alternate}
@keyframes o1{to{transform:translate(9px,7px) scale(1.15)}} @keyframes o2{to{transform:translate(-8px,-9px) scale(.9)}}
@keyframes o3{to{transform:translate(10px,-6px) scale(1.2)}}
.soul[data-state=idle]{--s1:#5b8cff;--s2:#8a6dff;--s3:#3dc6e6;opacity:.9}
.soul:not([data-identity=verified]):not([data-identity=override])[data-state=idle]{--s1:#6b7078;--s2:#7a7f88;--s3:#5f656e;opacity:.55;filter:saturate(.3)}
.soul:not([data-identity=verified]):not([data-identity=override])[data-state=idle] i{animation-duration:60s}
.soul i{transition:background .9s var(--ease)}
.soul[data-state=listening]{--s1:#59b0ff;--s2:#7fe9ff;--s3:#9a8bff}
.soul[data-state=thinking]{--s1:#9a6dff;--s2:#ff6fb5;--s3:#ffb066}
.soul[data-state=thinking] i{animation-duration:5s}
.soul[data-state=speaking]{--s1:#3dd6c6;--s2:#5aa8ff;--s3:#7af0a6}
.soul[data-state=listening] i,.soul[data-state=speaking] i{animation-duration:6s}
main{overflow:auto;scroll-behavior:smooth;position:relative}
.view{max-width:880px;margin:0 auto;padding:46px 44px 90px;animation:viewin .5s var(--ease) both}
@keyframes viewin{from{opacity:0;transform:translateY(10px)}}
.view.leaving{animation:viewout .18s var(--ease-in-out) forwards}
@keyframes viewout{to{opacity:0;transform:translateY(-6px)}}
.head{display:flex;align-items:baseline;gap:16px;margin-bottom:6px}
.head .when{margin-left:auto;font-size:13px;color:var(--faint);font-variant-numeric:tabular-nums}
.lede{color:var(--dim);margin:6px 0 30px}
.section{margin-top:44px}
.label{font-size:12px;color:var(--dim);font-weight:550;margin-bottom:12px;letter-spacing:.01em}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.metric{padding:18px 20px 18px 0} .metric+.metric{padding-left:20px;border-left:1px solid var(--line)}
.metric .n{font-size:28px;font-weight:620;letter-spacing:-.025em;font-variant-numeric:tabular-nums;transition:color .3s}
.metric .d{font-size:12px;color:var(--faint);margin-top:1px}
.bar{height:3px;border-radius:3px;background:var(--line);margin-top:12px;overflow:hidden}
.bar>b{display:block;height:100%;width:0;background:var(--accent);border-radius:3px;transition:width .9s var(--ease),background .4s}
.seg{display:inline-flex;background:var(--raise);padding:3px;border-radius:11px;gap:2px;flex-wrap:wrap}
.seg button{background:none;border:0;padding:7px 16px;border-radius:8px;color:var(--dim);cursor:pointer;font-size:13.5px;
  transition:background .3s var(--ease),color .2s}
.seg button:hover{color:var(--text)} .seg button.on{background:var(--bg);color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.25)}
.hint{font-size:12.5px;color:var(--faint);margin-top:10px;min-height:18px}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{background:var(--raise);border:0;color:var(--text);padding:8px 15px;border-radius:999px;font-size:13.5px;cursor:pointer;
  transition:background .25s var(--ease),transform .25s var(--ease)}
.chip:hover{background:var(--line2)} .chip:active{transform:scale(.96)}
.chip.quiet{background:none;box-shadow:inset 0 0 0 1px var(--line2);color:var(--dim)}
.answer{font-size:15.5px}
.answer>*:first-child{margin-top:0} .answer h2{font-size:18px;margin:26px 0 8px} .answer h3{font-size:15.5px;margin:20px 0 6px}
.answer .reveal{animation:none;opacity:1;transform:none}
.feed{font-size:13.5px} .feed div{display:flex;gap:16px;padding:9px 0;border-bottom:1px solid var(--line)}
.feed time{color:var(--faint);font-variant-numeric:tabular-nums;flex:none;width:46px}
.feed span{color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.empty{color:var(--faint);font-size:14px;padding:6px 0}
.row{display:flex;align-items:center;gap:18px;padding:16px 0;border-bottom:1px solid var(--line)}
.row .k{width:120px;font-size:14px} .row .v{font-size:13px;color:var(--dim);min-width:44px;text-align:right;font-variant-numeric:tabular-nums}
.row .grow{flex:1}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:20px;background:none;cursor:pointer}
input[type=range]::-webkit-slider-runnable-track{height:3px;border-radius:3px;background:var(--line2)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;background:var(--text);margin-top:-6.5px;
  box-shadow:0 1px 4px rgba(0,0,0,.4);transition:transform .2s var(--ease)}
input[type=range]:active::-webkit-slider-thumb{transform:scale(1.15)}
.btn{background:var(--raise);border:0;border-radius:9px;padding:8px 16px;font-size:13.5px;cursor:pointer;color:var(--text);
  transition:background .25s var(--ease)} .btn:hover{background:var(--line2)}
.field{background:var(--raise);border:1px solid transparent;border-radius:10px;padding:10px 14px;color:var(--text);font:inherit;width:100%;
  outline:none;transition:border-color .25s var(--ease),background .25s}
.field:focus{border-color:var(--accent)}
.procs{width:100%;border-collapse:collapse;font-size:13.5px} .procs td{padding:9px 0;border-bottom:1px solid var(--line)}
.procs td:last-child{text-align:right;color:var(--dim);font-variant-numeric:tabular-nums}
.chat{display:flex;flex-direction:column;height:100vh;max-width:760px;margin:0 auto;padding:0 44px}
.thread{flex:1;overflow:auto;padding:40px 0 20px;display:flex;flex-direction:column;gap:18px}
.msg{max-width:88%;animation:msgin .45s var(--ease) both} @keyframes msgin{from{opacity:0;transform:translateY(8px)}}
.msg.you{align-self:flex-end;background:var(--raise);padding:10px 15px;border-radius:16px 16px 4px 16px}
.msg.vanta{align-self:flex-start}
.msg.vanta>*:first-child{margin-top:0} .msg.vanta p{margin:6px 0}
.composer{padding:10px 0 26px;display:flex;gap:10px}
.mono{font-family:var(--mono);font-size:12.5px;color:var(--dim)}
.panel-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:0 40px}
.panel h3{margin:0 0 4px;font-size:15px} .panel .note{font-size:13px;color:var(--faint);margin-bottom:8px}
.kv{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--line);font-size:14px}
.kv span:first-child{color:var(--dim)}
.toast{position:fixed;bottom:26px;left:50%;transform:translate(-50%,12px)}
@media(max-width:820px){.app{grid-template-columns:64px 1fr}.nav button span,.brand span,.state-sub,.state-name{display:none}
  .metrics{grid-template-columns:repeat(2,1fr)}.panel-grid{grid-template-columns:1fr}}
"""

ICONS = {
    "Home": '<svg viewBox="0 0 24 24"><path d="M4 11l8-7 8 7v8a1 1 0 01-1 1h-4v-6H9v6H5a1 1 0 01-1-1z"/></svg>',
    "Control": '<svg viewBox="0 0 24 24"><path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/></svg>',
    "Chat": '<svg viewBox="0 0 24 24"><path d="M5 5h14a1 1 0 011 1v9a1 1 0 01-1 1H10l-5 4v-4a1 1 0 01-1-1V6a1 1 0 011-1z"/></svg>',
    "Activity": '<svg viewBox="0 0 24 24"><path d="M3 12h4l3-7 4 14 3-7h4"/></svg>',
    "Output": '<svg viewBox="0 0 24 24"><path d="M7 3h8l4 4v13a1 1 0 01-1 1H7a1 1 0 01-1-1V4a1 1 0 011-1z"/><path d="M14 3v5h5M9 13h6M9 17h6"/></svg>',
    "Brain": '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="2.5"/><circle cx="5" cy="6" r="1.6"/><circle cx="19" cy="7" r="1.6"/><circle cx="6" cy="18" r="1.6"/><circle cx="18" cy="18" r="1.6"/><path d="M10 11L6 7M14 11l4-3M10 14l-3 3M14 14l3 3"/></svg>',
}

CONSOLE_JS = r"""
(function(){
"use strict";
var QS=new URLSearchParams(location.search),TOKEN=QS.get("t")||"";
if(QS.get("static"))document.documentElement.classList.add("static");
var PAGES=["Home","Control","Chat","Activity","Output","Brain"],page=QS.get("page")||"Home",S={},ver=-1,sig={};
var $=function(i){return document.getElementById(i)};
function api(p,o){o=o||{};o.headers=Object.assign({"X-Report-Token":TOKEN},o.headers||{});o.cache="no-store";return fetch(p,o).then(function(r){return r.json()})}
function ctl(op,args){return api("/api/control",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({op:op,args:args||{}})})}
function toast(t){var e=$("toast");e.textContent=t;e.classList.add("on");setTimeout(function(){e.classList.remove("on")},1400)}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]})}
function pct(v){return v==null?"—":Math.round(v)+"%"}
function greeting(){var h=new Date().getHours();return h<5?"Late night":h<12?"Good morning":h<17?"Good afternoon":"Good evening"}
function ago(ts){return new Date(ts*1000).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"})}
var STATE_LABEL={idle:"Ready",listening:"Listening",thinking:"Working",speaking:"Speaking",loading:"Starting"};

/* ---------- views ---------- */
var views={
Home:function(){var m=S.metrics||{},modes=S.modes||[],cur=S.mode||"";
 var tiles=[["Processor",pct(m.cpu),"utilisation",m.cpu],["Memory",pct(m.ram),m.ram_used_gb!=null?m.ram_used_gb.toFixed(1)+" of "+Math.round(m.ram_total_gb)+" GB":"",m.ram],
  ["Storage",pct(m.disk),m.disk_free_gb!=null?Math.round(m.disk_free_gb)+" GB free":"",m.disk],
  ["Battery",m.battery==null?"AC":pct(m.battery),m.battery==null?"no battery":(m.charging?"charging":"on battery"),m.battery]];
 return '<div class="head"><h1 class="title">'+greeting()+'</h1><span class="when" id="clock"></span></div>'
 +'<p class="lede">'+esc(S.headline||"Here is where things stand.")+'</p>'
 +'<div class="metrics">'+tiles.map(function(t,i){return '<div class="metric"><div class="label" style="margin:0">'+t[0]+'</div><div class="n" data-m="'+i+'">'+t[1]+'</div><div class="d" data-d="'+i+'">'+esc(t[2])+'</div><div class="bar"><b data-b="'+i+'" style="width:'+(t[3]||0)+'%"></b></div></div>'}).join("")+'</div>'
 +'<div class="section"><div class="label">Mode</div><div class="seg" id="modes">'+modes.map(function(x){return '<button data-mode="'+esc(x.id)+'" class="'+(x.id===cur?"on":"")+'">'+esc(x.label)+'</button>'}).join("")+'</div><div class="hint" id="modehint">'+esc((modes.filter(function(x){return x.id===cur})[0]||{}).blurb||"")+'</div></div>'
 +'<div class="section"><div class="label">Latest answer</div><div class="answer" id="answer">'+(S.latest_html||'<div class="empty">Nothing yet this session.</div>')+'</div></div>'
 +'<div class="section"><div class="label">Quick actions</div><div class="chips">'+[["Plan my day","plan my day"],["Unread mail","read my unread emails"],["Today","what is on my calendar today"],["Weather","what is the weather like today"]].map(function(c){return '<button class="chip" data-cmd="'+esc(c[1])+'">'+c[0]+'</button>'}).join("")+'</div></div>'
 +((S.panels||[]).length?'<div class="section"><div class="label">Boards</div><div class="panel-grid">'+S.panels.map(panelHtml).join("")+'</div></div>':'')
 +'<div class="section"><div class="label">Recent</div><div class="feed" id="recent">'+feed((S.activity||[]).slice(-6).reverse())+'</div></div>'},
Control:function(){var a=S.audio||{},d=S.display||{},n=S.network||{},m=S.metrics||{};
 return '<div class="head"><h1 class="title">Control</h1></div><p class="lede">The machine, within reach.</p>'
 +'<div class="row"><div class="k">Volume</div><div class="grow"><input type="range" id="vol" min="0" max="100" value="'+(a.volume==null?50:a.volume)+'"></div><div class="v" id="volv">'+(a.volume==null?"—":a.volume+"%")+'</div><button class="btn" id="mute">'+(a.muted?"Unmute":"Mute")+'</button></div>'
 +'<div class="row"><div class="k">Brightness</div><div class="grow"><input type="range" id="bri" min="0" max="100" value="'+(d.brightness==null?50:d.brightness)+'"'+(d.brightness==null?" disabled":"")+'></div><div class="v" id="briv">'+(d.brightness==null?"n/a":d.brightness+"%")+'</div></div>'
 +'<div class="row"><div class="k">Wi‑Fi</div><div class="grow" id="wifi">'+esc(n.wifi||"Reading…")+'</div><div class="v" style="min-width:0" id="wifid">'+esc(n.wifi_detail||"")+'</div></div>'
 +'<div class="row"><div class="k">Bluetooth</div><div class="grow" id="bt">'+esc(n.bluetooth||"Reading…")+'</div></div>'
 +'<div class="row"><div class="k">Network</div><div class="grow mono" id="net">↓ '+esc(m.net_down_h||"0 B")+'/s   ↑ '+esc(m.net_up_h||"0 B")+'/s</div></div>'
 +'<div class="section"><div class="label">Power</div><div class="chips"><button class="chip" data-op="lock">Lock</button><button class="chip" data-op="sleep">Sleep</button><button class="chip" data-op="screenshot">Screenshot</button></div></div>'
 +'<div class="section"><div class="label">Open</div><div style="display:flex;gap:10px"><input class="field" id="launch" placeholder="An app, a file, or a link"><button class="btn" id="go">Open</button></div></div>'
 +'<div class="section"><div class="label">Using the most memory</div><table class="procs" id="procs">'+procs()+'</table></div>'},
Chat:function(){return '<div class="chat"><div class="thread" id="thread">'+chatHtml()+'</div><div class="composer"><input class="field" id="say" placeholder="Ask VANTA anything" autocomplete="off"><button class="btn" id="send">Send</button></div></div>'},
Activity:function(){return '<div class="head"><h1 class="title">Activity</h1></div><p class="lede">What VANTA has been doing.</p><div class="feed" id="recent">'+feed((S.activity||[]).slice().reverse())+'</div>'},
Output:function(){return '<div class="head"><h1 class="title">Output</h1></div><p class="lede">The last thing that needed reading.</p><div class="answer" id="answer">'+(S.latest_html||'<div class="empty">Nothing yet.</div>')+'</div>'},
Brain:function(){return '<div class="head"><h1 class="title">Brain</h1></div><p class="lede">Everything VANTA knows, as a map you can move through.</p><button class="chip" data-op="brain">Open the map</button>'}
};
function feed(rows){return rows.length?rows.map(function(r){return '<div><time>'+ago(r.t)+'</time><span title="'+esc(r.text)+'">'+esc(r.text)+'</span></div>'}).join(""):'<div class="empty">Nothing yet this session.</div>'}
function procs(){var p=S.processes||[];return p.length?p.map(function(r){return '<tr><td>'+esc(r.name)+'</td><td>'+esc(r.mem)+'</td></tr>'}).join(""):'<tr><td class="empty">Reading…</td></tr>'}
function chatHtml(){var c=S.chat||[];return c.length?c.map(function(m){return m.role==="you"?'<div class="msg you">'+esc(m.text)+'</div>':'<div class="msg vanta">'+(m.html||esc(m.text))+'</div>'}).join(""):'<div class="empty">Say something to begin.</div>'}
function panelHtml(p){return '<div class="panel"><h3>'+esc(p.title)+'</h3>'+(p.note?'<div class="note">'+esc(p.note)+'</div>':'')+(p.rows||[]).map(function(r){return '<div class="kv"><span>'+esc(r[0])+'</span><span>'+esc(r[1])+'</span></div>'}).join("")+'</div>'}

/* ---------- wiring ---------- */
function bind(){var root=$("view");
 root.querySelectorAll("[data-mode]").forEach(function(b){b.onclick=function(){ctl("mode",{name:b.dataset.mode});toast("Switching to "+b.textContent)}});
 root.querySelectorAll("[data-cmd]").forEach(function(b){b.onclick=function(){ctl("command",{text:b.dataset.cmd});toast("Sent")}});
 root.querySelectorAll("[data-op]").forEach(function(b){b.onclick=function(){ctl(b.dataset.op,{});toast("Done")}});
 var v=$("vol");if(v){v.oninput=function(){$("volv").textContent=v.value+"%"};v.onchange=function(){ctl("volume",{level:+v.value})}}
 var b=$("bri");if(b){b.oninput=function(){$("briv").textContent=b.value+"%"};b.onchange=function(){ctl("brightness",{level:+b.value})}}
 if($("mute"))$("mute").onclick=function(){ctl("mute",{})};
 var go=$("go");if(go)go.onclick=function(){var t=$("launch").value.trim();if(t){ctl("launch",{target:t});$("launch").value="";toast("Opening")}};
 var say=$("say");if(say){var send=function(){var t=say.value.trim();if(t){ctl("command",{text:t});say.value=""}};
  say.onkeydown=function(e){if(e.key==="Enter")send()};$("send").onclick=send;say.focus();
  var th=$("thread");th.scrollTop=th.scrollHeight}}
function show(name,instant){var root=$("view");page=name;
 document.querySelectorAll(".nav button").forEach(function(b){b.classList.toggle("on",b.dataset.page===name)});
 var go=function(){root.className=name==="Chat"?"":"view";root.innerHTML=views[name]();sig={};bind();tick(true)};
 if(instant){go();return}root.classList.add("leaving");setTimeout(go,170)}
/* live updates touch only what changed, so nothing under the pointer is rebuilt */
function tick(force){
 var st=S.state||"idle";$("soul").dataset.state=st;$("sname").textContent=STATE_LABEL[st]||st;$("soul").dataset.identity=S.identity||"unknown";$("ssub").textContent=(S.mode_label||"")+({verified:" \u00b7 Verified",guarded:" \u00b7 Not verified"}[S.identity]||"");
 var c=$("clock");if(c)c.textContent=new Date().toLocaleString([],{weekday:"long",hour:"2-digit",minute:"2-digit"});
 var m=S.metrics||{};
 if(page==="Home"){[[pct(m.cpu),"utilisation",m.cpu],[pct(m.ram),m.ram_used_gb!=null?m.ram_used_gb.toFixed(1)+" of "+Math.round(m.ram_total_gb)+" GB":"",m.ram],
   [pct(m.disk),m.disk_free_gb!=null?Math.round(m.disk_free_gb)+" GB free":"",m.disk],[m.battery==null?"AC":pct(m.battery),m.battery==null?"no battery":(m.charging?"charging":"on battery"),m.battery]].forEach(function(t,i){
   var n=document.querySelector('[data-m="'+i+'"]'),d=document.querySelector('[data-d="'+i+'"]'),b=document.querySelector('[data-b="'+i+'"]');
   if(n)n.textContent=t[0];if(d)d.textContent=t[1];if(b)b.style.width=(t[2]||0)+"%"});
  swap("answer",S.latest_html||'<div class="empty">Nothing yet this session.</div>',true);
  swap("recent",feed((S.activity||[]).slice(-6).reverse()));
  var ms=$("modes"),msig=(S.modes||[]).map(function(x){return x.id+x.label}).join();
  if(ms&&sig.modes!==msig){sig.modes=msig;ms.innerHTML=(S.modes||[]).map(function(x){return '<button data-mode="'+esc(x.id)+'">'+esc(x.label)+'</button>'}).join("");
   ms.querySelectorAll("[data-mode]").forEach(function(b){b.onclick=function(){ctl("mode",{name:b.dataset.mode});toast("Switching to "+b.textContent)}})}
  document.querySelectorAll("#modes button").forEach(function(b){b.classList.toggle("on",b.dataset.mode===S.mode)});
  var h=$("modehint"),cur=(S.modes||[]).filter(function(x){return x.id===S.mode})[0];if(h&&cur)h.textContent=cur.blurb||""}
 if(page==="Activity")swap("recent",feed((S.activity||[]).slice().reverse()));
 if(page==="Output")swap("answer",S.latest_html||'<div class="empty">Nothing yet.</div>',true);
 if(page==="Control"){swap("procs",procs());var n=S.network||{},d=S.display||{};
  if($("wifi"))$("wifi").textContent=n.wifi||"Reading…";if($("wifid"))$("wifid").textContent=n.wifi_detail||"";
  if($("bt"))$("bt").textContent=n.bluetooth||"Reading…";
  if($("net"))$("net").textContent="↓ "+(m.net_down_h||"0 B")+"/s   ↑ "+(m.net_up_h||"0 B")+"/s";
  var br=$("bri");if(br&&d.brightness!=null){br.disabled=false;if(document.activeElement!==br){br.value=d.brightness;$("briv").textContent=d.brightness+"%"}}
  var v=$("vol");if(v&&document.activeElement!==v&&S.audio&&S.audio.volume!=null){v.value=S.audio.volume;$("volv").textContent=S.audio.volume+"%"}
  if($("mute")&&S.audio)$("mute").textContent=S.audio.muted?"Unmute":"Mute"}
 if(page==="Chat"){var th=$("thread");if(th){var html=chatHtml();if(sig.thread!==html){var atEnd=th.scrollHeight-th.scrollTop-th.clientHeight<80;sig.thread=html;th.innerHTML=html;if(atEnd)th.scrollTo({top:th.scrollHeight,behavior:"smooth"})}}}}
function swap(id,html,animate){var e=$(id);if(!e||sig[id]===html)return;sig[id]=html;e.innerHTML=html}
var gotoN=0;
function follow(){var g=S.goto;if(g&&g.n!==gotoN){gotoN=g.n;if(g.page!==page&&views[g.page])show(g.page)}}
function poll(){api("/api/console").then(function(r){if(r.version!==ver){ver=r.version;S=r.state||{};follow();tick()}}).catch(function(){})}
var nav=$("nav");PAGES.forEach(function(p){var b=document.createElement("button");b.dataset.page=p;b.innerHTML=ICONS[p]+"<span>"+p+"</span>";b.onclick=function(){if(p!==page)show(p)};nav.appendChild(b)});
document.addEventListener("keydown",function(e){if(e.key==="Escape")ctl("close",{})});
try{var th=localStorage.getItem("vanta-theme");if(th)document.documentElement.dataset.theme=th}catch(e){}
api("/api/console").then(function(r){S=r.state||{};ver=r.version;gotoN=(S.goto||{}).n||0;show(page,true)}).catch(function(){show(page,true)});
setInterval(poll,1000);setInterval(function(){var c=$("clock");if(c)c.textContent=new Date().toLocaleString([],{weekday:"long",hour:"2-digit",minute:"2-digit"})},15000);
})();
"""

import json as _json

SHELL = ("""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>VANTA</title><style>""" + BASE_CSS + CONSOLE_CSS + """</style></head>
<body><div class="app">
<aside><div class="brand"><span>VANTA</span></div><div class="nav" id="nav"></div>
  <div class="side-foot"><div class="state-row"><div class="soul" id="soul" data-state="idle"><i></i><i></i><i></i></div>
  <div><div class="state-name" id="sname">Ready</div><div class="state-sub" id="ssub"></div></div></div></div></aside>
<main id="main"><div id="view" class="view"></div></main></div>
<div class="toast" id="toast"></div>
<script>var ICONS=""" + _json.dumps(ICONS) + ";" + CONSOLE_JS + """</script></body></html>
""")
