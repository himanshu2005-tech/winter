"""Offline 3D drawing; animation renders only while the user presses Play."""
MEDIA_CSS = r"""
.report-image img{display:block;max-width:100%;max-height:650px;margin:auto;object-fit:contain;border-radius:10px}
.report-image figcaption,.scene3d figcaption{color:var(--dim);font-size:13px;margin:12px 0}
.scene3d{margin:28px 0;border:1px solid var(--line);border-radius:14px;padding:16px}
.scene3d canvas{display:block;width:100%;height:380px;touch-action:none;cursor:grab;background:#0c1016;border-radius:10px}
.scene3d canvas:active{cursor:grabbing}.scene-tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px}
.scene-tools button{background:var(--hover);color:var(--text);border:1px solid var(--line);border-radius:7px;padding:7px 10px;cursor:pointer}
.scene-tools span,.image-error{font-size:12px;color:var(--dim)}
@media print{.scene-tools{display:none}}
"""

MEDIA_JS = r"""
function reportMedia(root){
  root.querySelectorAll('.report-image img').forEach(function(img){
    function failed(){img.hidden=true;img.parentNode.querySelector('.image-error').hidden=false}
    img.addEventListener('error',failed);if(img.complete&&!img.naturalWidth)failed();
  });
  root.querySelectorAll('.scene3d').forEach(function(figure){
    try{
      var scene=JSON.parse(figure.dataset.scene),canvas=figure.querySelector('canvas'),ctx=canvas.getContext('2d');
      if(!ctx)throw Error('Canvas unavailable');
      var low=[Infinity,Infinity,Infinity],high=[-Infinity,-Infinity,-Infinity];
      scene.objects.forEach(function(o){o.vertices.forEach(function(p){p.forEach(function(v,i){low[i]=Math.min(low[i],v);high[i]=Math.max(high[i],v)})})});
      var center=low.map(function(v,i){return (v+high[i])/2}),extent=Math.max.apply(null,high.map(function(v,i){return v-low[i]}))||1;
      var yaw=-.55,pitch=.35,zoom=1,wire=false,drag=null,animationTime=0;
      scene.objects.forEach(function(o){o.center=[0,0,0];o.vertices.forEach(function(p){p.forEach(function(v,i){o.center[i]+=v/o.vertices.length})})});
      function sample(track){var k=track.keyframes;if(animationTime<=k[0].time)return k[0].value;
        var b=k.findIndex(function(v){return v.time>=animationTime});if(b<0)return k[k.length-1].value;
        var a=k[b-1],z=k[b],f=(animationTime-a.time)/(z.time-a.time);
        return Array.isArray(a.value)?a.value.map(function(v,i){return v+(z.value[i]-v)*f}):a.value+(z.value-a.value)*f;}
      function pose(o){var p={position:[0,0,0],rotation:[0,0,0],scale:[1,1,1],opacity:1};
        (o.tracks||[]).forEach(function(t){p[t.property]=sample(t)});return p;}
      function transform(v,o,p){var q=v.map(function(x,i){return (x-o.center[i])*p.scale[i]});
        [0,1,2].forEach(function(axis){var a=p.rotation[axis]*Math.PI/180,c=Math.cos(a),s=Math.sin(a),i=(axis+1)%3,j=(axis+2)%3,x=q[i],y=q[j];q[i]=x*c-y*s;q[j]=x*s+y*c});
        return q.map(function(x,i){return x+o.center[i]+p.position[i]});}
      // Fit the full translation/scale envelope so movement remains in view.
      scene.objects.forEach(function(o){if(!(o.tracks||[]).length)return;var positions=[[0,0,0]],scale=1;
        o.tracks.forEach(function(t){if(t.property==='position')positions=positions.concat(t.keyframes.map(function(k){return k.value}));
          if(t.property==='scale')t.keyframes.forEach(function(k){scale=Math.max(scale,Math.max.apply(null,k.value))})});
        var radius=0;o.vertices.forEach(function(v){radius=Math.max(radius,Math.hypot(v[0]-o.center[0],v[1]-o.center[1],v[2]-o.center[2]))});
        positions.forEach(function(p){p.forEach(function(v,i){low[i]=Math.min(low[i],o.center[i]+v-radius*scale);high[i]=Math.max(high[i],o.center[i]+v+radius*scale)})});
      });center=low.map(function(v,i){return(v+high[i])/2});extent=Math.max.apply(null,high.map(function(v,i){return v-low[i]}))||1;
      function rotate(p){var x=(p[0]-center[0])/extent,y=(p[1]-center[1])/extent,z=(p[2]-center[2])/extent;
        var a=x*Math.cos(yaw)+z*Math.sin(yaw),b=-x*Math.sin(yaw)+z*Math.cos(yaw);
        return [a,y*Math.cos(pitch)-b*Math.sin(pitch),y*Math.sin(pitch)+b*Math.cos(pitch)];}
      function draw(){
        var w=canvas.clientWidth||600,h=canvas.clientHeight||380,dpr=Math.min(devicePixelRatio||1,2);
        canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);
        ctx.fillStyle='#0c1016';ctx.fillRect(0,0,w,h);var scale=Math.min(w,h)*.68*zoom,polygons=[],lines=[],labels=[];
        scene.objects.forEach(function(o){var state=pose(o);if(state.opacity<=0)return;var points=o.vertices.map(function(p){var q=rotate(transform(p,o,state));return [w/2+q[0]*scale,h/2-q[1]*scale,q[2]]});
          o.faces.forEach(function(f){polygons.push({points:f.map(function(i){return points[i]}),depth:f.reduce(function(s,i){return s+points[i][2]},0)/f.length,color:o.color,opacity:state.opacity})});
          o.edges.forEach(function(e){lines.push({a:points[e[0]],b:points[e[1]],color:o.color,opacity:state.opacity})});
          if(o.label){var c=[0,0];points.forEach(function(p){c[0]+=p[0]/points.length;c[1]+=p[1]/points.length});labels.push({p:c,text:o.label})}
        });
        polygons.sort(function(a,b){return a.depth-b.depth}).forEach(function(f){ctx.beginPath();f.points.forEach(function(p,i){i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1])});ctx.closePath();
          if(!wire){ctx.fillStyle=f.color;ctx.globalAlpha=.84*f.opacity;ctx.fill()}ctx.globalAlpha=f.opacity;ctx.strokeStyle=wire?f.color:'#17212e';ctx.lineWidth=wire?1.1:.7;ctx.stroke();ctx.globalAlpha=1});
        lines.forEach(function(l){ctx.beginPath();ctx.moveTo(l.a[0],l.a[1]);ctx.lineTo(l.b[0],l.b[1]);ctx.strokeStyle=l.color;ctx.lineWidth=2;ctx.globalAlpha=l.opacity;ctx.stroke();ctx.globalAlpha=1});
        ctx.font='13px system-ui';ctx.textAlign='center';labels.forEach(function(l){var m=ctx.measureText(l.text);ctx.fillStyle='#0c1016dd';ctx.fillRect(l.p[0]-m.width/2-5,l.p[1]-15,m.width+10,21);ctx.fillStyle='#fff';ctx.fillText(l.text,l.p[0],l.p[1])});
        canvas.dataset.rendered='true';canvas.dataset.yaw=String(yaw);canvas.dataset.zoom=String(zoom);
      }
      canvas.addEventListener('pointerdown',function(e){drag=[e.clientX,e.clientY];canvas.setPointerCapture(e.pointerId)});
      canvas.addEventListener('pointermove',function(e){if(!drag)return;yaw+=(e.clientX-drag[0])*.01;pitch=Math.max(-1.5,Math.min(1.5,pitch+(e.clientY-drag[1])*.01));drag=[e.clientX,e.clientY];draw()});
      ['pointerup','pointercancel','lostpointercapture'].forEach(function(name){canvas.addEventListener(name,function(){drag=null})});
      canvas.addEventListener('wheel',function(e){e.preventDefault();zoom=Math.max(.2,Math.min(5,zoom*Math.exp(-e.deltaY*.001)));draw()},{passive:false});
      canvas.addEventListener('keydown',function(e){if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','+','-','='].includes(e.key))return;e.preventDefault();
        if(e.key==='ArrowLeft')yaw-=.1;if(e.key==='ArrowRight')yaw+=.1;if(e.key==='ArrowUp')pitch-=.1;if(e.key==='ArrowDown')pitch+=.1;
        if(e.key==='+'||e.key==='=')zoom=Math.min(5,zoom*1.1);if(e.key==='-')zoom=Math.max(.2,zoom/1.1);draw()});
      figure.querySelector('[data-view="reset"]').onclick=function(){yaw=-.55;pitch=.35;zoom=1;draw()};
      figure.querySelector('[data-view="wire"]').onclick=function(){wire=!wire;this.setAttribute('aria-pressed',String(wire));draw()};
      // The observer belongs to the element, and is disconnected on report replacement.
      var stopPlayback=reportPlayback(figure,scene.duration||0,function(time){animationTime=time;draw()});
      var observer=new ResizeObserver(draw);observer.observe(canvas);figure._dispose=function(){observer.disconnect();stopPlayback()};draw();
    }catch(e){var message=document.createElement('p');message.textContent='3D viewer unavailable: '+e.message;figure.appendChild(message)}
  });
}
"""
