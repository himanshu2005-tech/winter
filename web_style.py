# -*- coding: utf-8 -*-
"""
web_style.py -- one visual language for every VANTA window drawn in HTML.

The first report design was decorated: an aurora behind the page, gradient
titles, glowing tiles, pulsing marks, a card around everything. This one takes
things away until what is left is the content.

PRINCIPLES
----------
* One accent. Everything else is a grey of the same hue family. Status colours
  (green, amber, red) exist only where a status exists.
* Structure comes from space and hairlines, not boxes. A table is rows and a
  rule; a number is a number with a small label, not a card with a glow.
* Type does the work: tight tracking on large sizes, generous line height on
  body text, small uppercase labels only where something needs naming.
* Motion explains and never decorates. Everything that moves uses the same
  ease-out curve (decelerating, like something with weight coming to rest),
  nothing loops, and nothing animates for its own sake. Entrances are a short
  rise and fade; state changes are 200-350 ms; charts grow once.

Not a copy of any system's look -- the palette, the cool neutral background and
the single ice-blue accent are VANTA's own -- but the same restraint.
"""

BASE_CSS = r"""
:root{
  color-scheme:dark;
  --bg:#0b0b0d; --surface:#141417; --raise:#1c1c20; --hover:rgba(255,255,255,.045);
  --line:rgba(255,255,255,.075); --line2:rgba(255,255,255,.13);
  --text:#f5f5f7; --dim:#9b9ba4; --faint:#85858f;
  --accent:#7db4ff; --accent-soft:rgba(125,180,255,.12);
  --ok:#62d391; --warn:#f2b866; --bad:#f07178;
  --c0:#7db4ff; --c1:#8b8b95; --c2:#4a86d4; --c3:#c4c4cc; --c4:#62d391; --c5:#f2b866;
  --ease:cubic-bezier(.22,1,.36,1); --ease-in-out:cubic-bezier(.65,0,.35,1);
  --font:"Segoe UI Variable Display","Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
  --mono:"Cascadia Code","Consolas",ui-monospace,monospace;
  --r:12px;
}
:root[data-theme="light"]{
  color-scheme:light;
  --bg:#fbfbfc; --surface:#ffffff; --raise:#f2f2f5; --hover:rgba(0,0,0,.04);
  --line:rgba(0,0,0,.075); --line2:rgba(0,0,0,.14);
  --text:#1c1c1e; --dim:#6e6e76; --faint:#6e6e78; --accent:#246ac3; --accent-soft:rgba(36,106,195,.1);
  --ok:#2e9e5e; --warn:#c77d12; --bad:#d6454d;
  --c0:#2f7de1; --c1:#9a9aa3; --c2:#6aa6ec; --c3:#c9c9d0; --c4:#2e9e5e; --c5:#c77d12;
}
*{box-sizing:border-box} html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--text);font:15px/1.6 var(--font);-webkit-font-smoothing:antialiased;
  text-rendering:optimizeLegibility;overflow:hidden}
button{font:inherit;color:inherit}
::selection{background:var(--accent-soft)}
::-webkit-scrollbar{width:12px;height:12px}
::-webkit-scrollbar-thumb{background:var(--line2);border-radius:10px;border:3px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:var(--faint);background-clip:padding-box;border:3px solid transparent}

/* ---- text ---- */
.title{font-size:34px;line-height:1.12;font-weight:650;letter-spacing:-.028em;margin:0}
.sub{color:var(--dim);font-size:14px;margin-top:8px}
h2,h3,h4{margin:0;scroll-margin-top:72px}
h2{font-size:22px;font-weight:620;letter-spacing:-.018em;margin:52px 0 14px}
h3{font-size:17px;font-weight:620;letter-spacing:-.01em;margin:36px 0 10px}
h4{font-size:11.5px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--dim);margin:30px 0 8px}
p{margin:12px 0;color:var(--text)}
strong{font-weight:650} em{font-style:italic;color:var(--text)}
mark{background:var(--accent-soft);color:inherit;border-radius:4px;padding:1px 4px}
del{color:var(--faint)}
a{color:var(--accent);text-decoration:none;transition:opacity .2s}
a:hover{opacity:.75}
code{font-family:var(--mono);font-size:.875em;background:var(--raise);padding:2px 6px;border-radius:6px}
.formula{margin:26px 0;padding:20px 24px;border-left:2px solid var(--accent);background:var(--surface);
  overflow:auto;text-align:center}
.formula img{display:block;max-width:100%;height:auto;min-height:28px;margin:0 auto;filter:invert(1)}
:root[data-theme="light"] .formula img{filter:none}
@media print{.formula img{filter:none}}
.formula figcaption{margin-top:12px;color:var(--faint);font:11px/1.45 var(--mono);word-break:break-word}
.formula-source{color:var(--text);text-align:left}.formula-source code{white-space:pre-wrap;background:transparent}
hr{border:0;border-top:1px solid var(--line);margin:40px 0}
blockquote{margin:22px 0;padding:0 0 0 20px;border-left:2px solid var(--line2);color:var(--dim);font-size:17px;line-height:1.55}

/* ---- lists ---- */
.list ul,.list ol{margin:10px 0;padding-left:22px} .list li{margin:6px 0;padding-left:2px}
.list ul>li::marker{color:var(--faint)} .list ol>li::marker{color:var(--dim);font-variant-numeric:tabular-nums}
.list ul.todo{list-style:none;padding-left:0}
.todo-item{display:flex;gap:12px;align-items:flex-start}
.check{flex:none;width:17px;height:17px;margin-top:4px;border-radius:50%;border:1.5px solid var(--faint);
  position:relative;transition:background .35s var(--ease),border-color .35s var(--ease)}
.check.on{background:var(--accent);border-color:var(--accent)}
.check.on::after{content:"";position:absolute;left:5px;top:2.5px;width:4px;height:8px;border:solid var(--bg);
  border-width:0 1.8px 1.8px 0;transform:rotate(45deg)}
.todo-item.done{color:var(--faint)} .todo-item.done>*:not(.check){text-decoration:line-through;text-decoration-color:var(--line2)}

/* ---- tables: rows and a rule, no box ---- */
.table-wrap{margin:22px -4px;overflow:auto}
table.data{width:100%;border-collapse:collapse;font-size:14.5px}
table.data th{text-align:left;padding:10px 12px;font-size:12px;font-weight:550;color:var(--dim);
  border-bottom:1px solid var(--line2);white-space:nowrap;cursor:pointer;user-select:none;transition:color .2s}
table.data th:hover{color:var(--text)} th .sort{margin-left:5px;opacity:.7;font-size:9px}
th.asc .sort::after{content:"\25B2"} th.desc .sort::after{content:"\25BC"}
table.data td{padding:13px 12px;border-bottom:1px solid var(--line)}
table.data tbody tr{transition:background .2s} table.data tbody tr:hover{background:var(--hover)}
table.data tr:last-child td{border-bottom:0}
.right{text-align:right;font-variant-numeric:tabular-nums} .center{text-align:center}
.badge{display:inline-flex;align-items:center;gap:7px;font-size:13px;color:var(--dim)}
.badge::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--faint)}
.badge.ok::before{background:var(--ok)} .badge.warn::before{background:var(--warn)}
.badge.bad::before{background:var(--bad)} .badge.info::before{background:var(--accent)}
.badge.ok,.badge.warn,.badge.bad,.badge.info{color:var(--text)}

/* ---- numbers: label over value, separated by space ---- */
.tiles{display:flex;gap:0;margin:26px 0;flex-wrap:wrap;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.tile{flex:1 1 140px;padding:18px 22px 18px 0;margin-left:0}
.tile+.tile{padding-left:22px;border-left:1px solid var(--line)}
.tile-label{font-size:12px;color:var(--dim)}
.tile-value{font-size:30px;font-weight:620;letter-spacing:-.025em;margin-top:2px;font-variant-numeric:tabular-nums}
dl.facts{display:grid;grid-template-columns:max-content 1fr;gap:10px 28px;margin:22px 0}
dl.facts dt{color:var(--dim);font-size:14px} dl.facts dd{margin:0}

/* ---- callouts, progress, code ---- */
.callout{margin:22px 0;padding:16px 18px;border-radius:var(--r);background:var(--accent-soft);display:flex;gap:14px}
.callout b{flex:none;font-size:12px;font-weight:620;color:var(--accent);padding-top:3px}
.callout.warning,.callout.warn{background:color-mix(in srgb,var(--warn) 12%,transparent)}
.callout.warning b,.callout.warn b{color:var(--warn)}
.callout.danger,.callout.error,.callout.important{background:color-mix(in srgb,var(--bad) 12%,transparent)}
.callout.danger b,.callout.error b,.callout.important b{color:var(--bad)}
.callout.success,.callout.done,.callout.tip{background:color-mix(in srgb,var(--ok) 12%,transparent)}
.callout.success b,.callout.done b,.callout.tip b{color:var(--ok)}
.progress{margin:18px 0} .progress-label{display:flex;justify-content:space-between;font-size:13.5px;color:var(--dim);margin-bottom:8px}
.progress-label b{color:var(--text);font-weight:600;font-variant-numeric:tabular-nums}
.track{height:4px;border-radius:4px;background:var(--line)}
.fill{height:100%;width:0;border-radius:4px;background:var(--accent);transition:width 1.1s var(--ease)}
.code{margin:22px 0;border-radius:var(--r);background:var(--surface);border:1px solid var(--line);overflow:hidden}
.code-head{display:flex;justify-content:space-between;align-items:center;padding:9px 14px 0;font-size:11.5px;color:var(--faint)}
.copy{background:none;border:0;color:var(--faint);cursor:pointer;padding:2px 6px;border-radius:6px;transition:color .2s}
.copy:hover{color:var(--text)}
pre{margin:0;padding:10px 16px 16px;overflow:auto;font-family:var(--mono);font-size:13px;line-height:1.65}
pre code{background:none;padding:0;border-radius:0}

/* ---- charts and diagrams: ink on paper ---- */
figure{margin:28px 0;padding:0}
.chart-title{font-size:14px;font-weight:600;margin-bottom:14px}
.svg-chart,.svg-flow{width:100%;height:auto;display:block;overflow:visible}
.grid{stroke:var(--line);stroke-width:1} .axis{fill:var(--faint);font-size:11px} .val{fill:var(--dim);font-size:11px}
.f0{fill:var(--c0)}.f1{fill:var(--c1)}.f2{fill:var(--c2)}.f3{fill:var(--c3)}.f4{fill:var(--c4)}.f5{fill:var(--c5)}
.k0{stroke:var(--c0)}.k1{stroke:var(--c1)}.k2{stroke:var(--c2)}.k3{stroke:var(--c3)}.k4{stroke:var(--c4)}.k5{stroke:var(--c5)}
.b0{background:var(--c0)}.b1{background:var(--c1)}.b2{background:var(--c2)}.b3{background:var(--c3)}.b4{background:var(--c4)}.b5{background:var(--c5)}
.bar{transform-box:fill-box;transform-origin:bottom;animation:grow .8s var(--ease) both;animation-delay:var(--d)}
.bar.h{transform-origin:left;animation-name:growx}
@keyframes grow{from{transform:scaleY(0)}} @keyframes growx{from{transform:scaleX(0)}}
.line{stroke-dasharray:1;stroke-dashoffset:1;animation:draw 1.2s var(--ease) .1s forwards}
@keyframes draw{to{stroke-dashoffset:0}} .area{opacity:.1;animation:fade .9s var(--ease) .3s both}
.dot,.slice,.node{transform-box:fill-box;transform-origin:center;animation:pop .5s var(--ease) both;animation-delay:var(--d)}
@keyframes pop{from{opacity:0;transform:scale(.88)}} @keyframes fade{from{opacity:0}}
.svg-chart .track{fill:var(--line)}
.legend{display:flex;gap:20px;flex-wrap:wrap;margin-top:14px;font-size:12.5px;color:var(--dim)}
.legend i,.pie-legend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px}
.pie-row{display:flex;align-items:center;gap:44px;flex-wrap:wrap}
.svg-pie{width:200px;height:200px} .pie-legend{list-style:none;margin:0;padding:0;font-size:14px}
.pie-legend li{margin:9px 0;display:flex;align-items:center;color:var(--dim)} .pie-legend b{margin-left:14px;color:var(--text);font-weight:550}
.svg-flow .node-shape{fill:var(--surface);stroke:var(--line2);stroke-width:1.2}
.svg-flow .tone0 .node-shape{stroke:var(--accent)}
.node-text{fill:var(--text);font-size:13px;font-weight:520}
.edge{fill:none;stroke:var(--faint);stroke-width:1.3;stroke-dasharray:1;stroke-dashoffset:1;
  animation:draw .9s var(--ease) both;animation-delay:var(--d)}
.edge.dash{stroke-dasharray:3 5;stroke-dashoffset:0;animation:fade .6s var(--ease) both;animation-delay:var(--d)}
.edge.thick{stroke-width:2.2;stroke:var(--accent)} .arrowhead{fill:var(--faint)}
.edge-label{fill:var(--dim);font-size:11.5px} .lifeline{stroke:var(--line);stroke-dasharray:3 5}

/* ---- actions ---- */
.actions{display:flex;gap:10px;flex-wrap:wrap;margin:34px 0}
.action{border:0;border-radius:999px;padding:9px 20px;font-weight:550;cursor:pointer;color:var(--bg);background:var(--text);
  transition:transform .25s var(--ease),opacity .2s}
.action:hover{transform:translateY(-1px);opacity:.9} .action:active{transform:scale(.97)}

/* ---- motion ---- */
.reveal{opacity:0;transform:translateY(10px);animation:rise .55s var(--ease) forwards;animation-delay:calc(var(--i,0)*40ms)}
@keyframes rise{to{opacity:1;transform:none}}
.toast{position:fixed;bottom:26px;left:50%;transform:translate(-50%,12px);opacity:0;background:var(--text);color:var(--bg);
  padding:8px 16px;border-radius:999px;font-size:13px;font-weight:550;transition:opacity .3s var(--ease),transform .3s var(--ease);z-index:50}
.toast.on{opacity:1;transform:translate(-50%,0)}
.static *,.static *::before,.static *::after{animation:none!important;transition:none!important}
.static .reveal{opacity:1;transform:none}.static .fill{width:var(--w)}.static .line{stroke-dashoffset:0}
.static .edge{stroke-dashoffset:0}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}
  .reveal{opacity:1;transform:none}}
"""
