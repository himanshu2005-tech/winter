"""Local interactions for learning blocks; no eval, external scripts or model calls."""
LEARNING_CSS = r"""
.report-explorer,.report-walkthrough{margin:28px 0;border:1px solid var(--line2);border-radius:12px;padding:20px;min-width:0}
.report-explorer figcaption,.report-lesson>h3,.report-walkthrough>h3{font-weight:600;margin:0 0 16px}
.report-explorer canvas{display:block;width:100%;height:340px;border-radius:8px}
.explorer-controls{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin-top:18px}
.explorer-controls label{font-size:13px}.explorer-controls output{float:right;color:var(--accent);font-variant-numeric:tabular-nums}
.explorer-controls input{width:100%;accent-color:var(--accent);display:block;margin-top:8px}
.explorer-outputs{display:flex;gap:22px;flex-wrap:wrap;margin-top:18px}.explorer-outputs span{display:block;color:var(--dim);font-size:12px}
.explorer-outputs strong{font-variant-numeric:tabular-nums}.explorer-status,.lesson-hint{color:var(--dim);font-size:12px}
.explorer-legend{display:flex;gap:18px;flex-wrap:wrap;color:var(--dim);font-size:12px;margin-top:12px}
.explorer-legend i{display:inline-block;width:14px;height:3px;margin-right:7px;vertical-align:middle}
.report-lesson{margin:30px 0;min-width:0}.report-lesson [role=tablist]{display:flex;gap:5px;overflow:auto;border-bottom:1px solid var(--line2);margin-bottom:20px}
.report-lesson [role=tab]{flex-shrink:0;border:0;border-bottom:2px solid transparent;padding:10px 14px;color:var(--dim);background:none;cursor:pointer}
.report-lesson [role=tab][aria-selected=true]{border-bottom-color:var(--accent);color:var(--text)}
.report-lesson details{border-top:1px solid var(--line2);padding:14px 0}.report-lesson summary{font-weight:550;cursor:pointer;padding:5px 0}
.lesson-content{padding:10px 0}.report-lesson[data-layout=flashcards] details{border:1px solid var(--line2);border-radius:10px;padding:18px;margin:12px 0;background:var(--surface)}
.report-lesson [hidden],.report-walkthrough [hidden]{display:none!important}
.walk-code{position:relative;padding:14px 14px 14px 44px;background:var(--surface);max-height:300px;overflow:auto;border:1px solid var(--line);border-radius:8px}
.walk-code li{padding:2px 8px;white-space:pre;font:12px/1.6 var(--mono);border-left:2px solid transparent}
.walk-code li::marker{color:var(--faint)}.walk-code code{padding:0;background:transparent;font-size:inherit}
.walk-code li.active{background:var(--accent-soft);border-left-color:var(--accent)}
.walk-controls{display:flex;gap:14px;align-items:center;justify-content:space-between;margin-top:18px}.walk-controls output{font-size:13px;color:var(--dim)}
.walk-controls button{padding:8px 12px;color:var(--text);background:var(--raise);border:1px solid var(--line2);border-radius:8px;cursor:pointer}
.walk-controls button:disabled{opacity:.4;cursor:default}.walk-state{width:100%;border-collapse:collapse;font-size:13px}
.walk-state th,.walk-state td{text-align:left;padding:8px;border-bottom:1px solid var(--line)}.walk-state th{color:var(--dim);font-weight:500}
.report-lesson :focus-visible,.walk-controls :focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@media print{.report-lesson [role=tablist],.walk-controls{display:none}.report-lesson [role=tabpanel][hidden],.report-walkthrough .walk-step[hidden]{display:block!important}.walk-code{max-height:none}}
"""

LEARNING_JS = r"""
function reportEvaluate(node,variables){
 var op=node[0],result=null,a,b;
 if(op==='constant')return node[1];if(op==='variable')return Object.prototype.hasOwnProperty.call(variables,node[1])?variables[node[1]]:null;
 if(op==='if'){a=reportEvaluate(node[1],variables);return a===null?null:reportEvaluate(a?node[2]:node[3],variables)}
 if(op==='call'){var args=node.slice(2).map(function(n){return reportEvaluate(n,variables)});if(args.some(function(n){return n===null}))return null;
   var functions={sin:Math.sin,cos:Math.cos,tan:Math.tan,asin:Math.asin,acos:Math.acos,atan:Math.atan,sqrt:Math.sqrt,exp:Math.exp,log:Math.log,log10:Math.log10,abs:Math.abs,floor:Math.floor,ceil:Math.ceil,min:Math.min,max:Math.max};
   if(!Object.prototype.hasOwnProperty.call(functions,node[1]))return null;result=functions[node[1]].apply(null,args);
 }else{
   a=reportEvaluate(node[1],variables);if(a===null)return null;
   if(op==='neg')result=-a;else if(op==='positive')result=a;else{
     b=reportEvaluate(node[2],variables);if(b===null)return null;
     switch(op){case 'add':result=a+b;break;case 'sub':result=a-b;break;case 'mul':result=a*b;break;case 'div':result=a/b;break;
       case 'pow':result=Math.pow(a,b);break;case 'mod':result=((a%b)+b)%b;break;case 'floor_div':result=Math.floor(a/b);break;
       case 'lt':result=+(a<b);break;case 'le':result=+(a<=b);break;case 'gt':result=+(a>b);break;case 'ge':result=+(a>=b);break;case 'eq':result=+(a===b);break;case 'ne':result=+(a!==b);break;}
   }
 }
 return Number.isFinite(result)&&Math.abs(result)<=1e12?result:null;
}
function reportNumber(value){return value===null?'Undefined':String(Number(value.toPrecision(5)))}
function reportExplorers(root){root.querySelectorAll('.report-explorer').forEach(function(figure){
 var spec=JSON.parse(figure.dataset.explorer),canvas=figure.querySelector('canvas'),ctx=canvas.getContext('2d'),env={pi:Math.PI,e:Math.E,t:0},dead=false;
 spec.parameters.forEach(function(p){env[p.id]=p.value});
 var outputBox=figure.querySelector('.explorer-outputs');spec.outputs.forEach(function(o){var box=document.createElement('div'),label=document.createElement('span'),value=document.createElement('strong');label.textContent=o.label;box.append(label,value);outputBox.appendChild(box)});
 function draw(){if(dead)return;var css=getComputedStyle(figure),color=function(name){return css.getPropertyValue(name).trim()},w=canvas.clientWidth||600,h=340,dpr=Math.min(devicePixelRatio||1,2);
  canvas.width=Math.round(w*dpr);canvas.height=h*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);ctx.fillStyle=color('--surface');ctx.fillRect(0,0,w,h);
  var left=spec.plot.axes?48:20,top=20,pw=Math.max(80,w-left-20),ph=h-top-(spec.plot.axes?44:20),xr=spec.plot.x_range,yr=spec.plot.y_range;
  var X=function(x){return left+(x-xr[0])/(xr[1]-xr[0])*pw},Y=function(y){return top+ph-(y-yr[0])/(yr[1]-yr[0])*ph};
  ctx.font='11px system-ui';ctx.lineWidth=1;
  if(spec.plot.axes){for(var tick=0;tick<=5;tick++){var x=xr[0]+tick*(xr[1]-xr[0])/5,y=yr[0]+tick*(yr[1]-yr[0])/5;
    ctx.strokeStyle=color('--line2');ctx.beginPath();ctx.moveTo(X(x),top);ctx.lineTo(X(x),top+ph);ctx.moveTo(left,Y(y));ctx.lineTo(left+pw,Y(y));ctx.stroke();
    ctx.fillStyle=color('--dim');ctx.textAlign='center';ctx.fillText(reportNumber(x),X(x),top+ph+17);ctx.textAlign='right';ctx.fillText(reportNumber(y),left-7,Y(y)+4)}
    ctx.textAlign='center';ctx.fillText(spec.plot.x_label,left+pw/2,h-6);ctx.save();ctx.translate(12,top+ph/2);ctx.rotate(-Math.PI/2);ctx.fillText(spec.plot.y_label,0,0);ctx.restore();}
  var undefinedCount=0;ctx.save();ctx.beginPath();ctx.rect(left,top,pw,ph);ctx.clip();
  spec.curves.forEach(function(curve){ctx.strokeStyle=curve.color||color('--accent');ctx.lineWidth=2.2;ctx.beginPath();var pen=false,last=null,samples=spec.duration?120:180;
    for(var i=0;i<=samples;i++){env.x=xr[0]+i*(xr[1]-xr[0])/samples;var value=reportEvaluate(curve.expression,env);if(value===null){undefinedCount++;pen=false;continue}
      if(last!==null&&Math.abs(last-value)>2*(yr[1]-yr[0]))pen=false;var px=X(env.x),py=Math.max(-10*h,Math.min(10*h,Y(value)));
      if(pen)ctx.lineTo(px,py);else ctx.moveTo(px,py);pen=true;last=value;}ctx.stroke();});
  spec.shapes.forEach(function(shape){var v={},invalid=false;Object.keys(shape).forEach(function(key){if(Array.isArray(shape[key])){v[key]=reportEvaluate(shape[key],env);if(v[key]===null)invalid=true}});
    if(invalid){undefinedCount++;return}var x=X(v.x),y=Y(v.y);ctx.strokeStyle=ctx.fillStyle=shape.color||color('--accent');ctx.lineWidth=2;ctx.beginPath();
    if(shape.type==='point'){ctx.arc(x,y,5,0,Math.PI*2);ctx.fill()}
    if(shape.type==='circle'){if(v.radius<=0){undefinedCount++;return}ctx.ellipse(x,y,v.radius*pw/(xr[1]-xr[0]),v.radius*ph/(yr[1]-yr[0]),0,0,Math.PI*2);ctx.stroke()}
    if(shape.type==='line'||shape.type==='arrow'){var x2=X(v.x2),y2=Y(v.y2);ctx.moveTo(x,y);ctx.lineTo(x2,y2);ctx.stroke();if(shape.type==='arrow'){var a=Math.atan2(y2-y,x2-x);ctx.beginPath();ctx.moveTo(x2,y2);ctx.lineTo(x2-9*Math.cos(a-.4),y2-9*Math.sin(a-.4));ctx.lineTo(x2-9*Math.cos(a+.4),y2-9*Math.sin(a+.4));ctx.closePath();ctx.fill()}}
    if(shape.type==='rect'){ctx.globalAlpha=.2;ctx.fillRect(x,Y(v.y+v.height),v.width*pw/(xr[1]-xr[0]),v.height*ph/(yr[1]-yr[0]));ctx.globalAlpha=1;ctx.strokeRect(x,Y(v.y+v.height),v.width*pw/(xr[1]-xr[0]),v.height*ph/(yr[1]-yr[0]))}
    if(shape.label){ctx.font='12px system-ui';ctx.textAlign='left';ctx.fillText(shape.label,x+8,y-8)}
  });ctx.restore();delete env.x;
  spec.outputs.forEach(function(o,i){var value=reportEvaluate(o.expression,env);if(value===null)undefinedCount++;outputBox.children[i].querySelector('strong').textContent=reportNumber(value)+(value!==null&&o.unit?' '+o.unit:'')});
  figure.querySelector('.explorer-status').textContent=undefinedCount?'Some values are undefined for these settings; those points are omitted.':'';
  canvas.dataset.rendered='true';canvas.dataset.parameters=JSON.stringify(env);
 }
 figure.querySelectorAll('[data-parameter]').forEach(function(input){input.oninput=function(){env[input.dataset.parameter]=Number(input.value);input.parentNode.querySelector('output').textContent=reportNumber(env[input.dataset.parameter]);draw()}});
 var stop=reportPlayback(figure,spec.duration,function(time){env.t=time;draw()});
 var resize=new ResizeObserver(draw);resize.observe(canvas);var theme=new MutationObserver(draw);theme.observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
 figure._dispose=function(){dead=true;stop();resize.disconnect();theme.disconnect()};draw();
})}
function reportLessons(root){root.querySelectorAll('.report-lesson[data-layout=tabs]').forEach(function(lesson){
 var tabs=[].slice.call(lesson.querySelector('[role=tablist]').children),panels=[].slice.call(lesson.children).filter(function(n){return n.getAttribute('role')==='tabpanel'});
 function select(index){tabs.forEach(function(tab,i){tab.setAttribute('aria-selected',String(i===index));tab.tabIndex=i===index?0:-1;panels[i].hidden=i!==index})}
 tabs.forEach(function(tab,i){tab.onclick=function(){select(i)};tab.onkeydown=function(e){var next=i;if(e.key==='ArrowRight')next=(i+1)%tabs.length;else if(e.key==='ArrowLeft')next=(i+tabs.length-1)%tabs.length;else if(e.key==='Home')next=0;else if(e.key==='End')next=tabs.length-1;else return;e.preventDefault();select(next);tabs[next].focus()}});
})}
function reportWalkthroughs(root){root.querySelectorAll('.report-walkthrough').forEach(function(walk){
 var steps=[].slice.call(walk.children).filter(function(e){return e.classList.contains('walk-step')}),index=0,code=walk.querySelector('.walk-code'),lines=[].slice.call(code.children),back=walk.querySelector('[data-walk=back]'),next=walk.querySelector('[data-walk=next]');
 function show(){steps.forEach(function(s,i){s.hidden=i!==index});var selected=JSON.parse(steps[index].dataset.lines);lines.forEach(function(line,i){line.classList.toggle('active',selected.includes(i+1))});
  back.disabled=index===0;next.disabled=index===steps.length-1;walk.querySelector('.walk-controls output').textContent='Step '+(index+1)+' of '+steps.length;
  var active=lines.find(function(line){return line.classList.contains('active')});if(active)code.scrollTop=Math.max(0,active.offsetTop-code.clientHeight/2);walk.dataset.step=String(index);}
 back.onclick=function(){index=Math.max(0,index-1);show()};next.onclick=function(){index=Math.min(steps.length-1,index+1);show()};show();
})}
"""
