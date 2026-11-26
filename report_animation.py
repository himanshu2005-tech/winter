"""Bounded animation descriptions, rendered through the existing visual grammar."""
import html
import json
import math


def tracks(value):
    if value is None:
        return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or len(value) > 4:
        raise ValueError('Use at most four animation tracks per object')
    result, seen = [], set()
    for track in value:
        if not isinstance(track, dict):
            raise ValueError('Animation tracks must be objects')
        prop = track.get('property')
        if prop not in {'position', 'rotation', 'scale', 'opacity'} or prop in seen:
            raise ValueError('Animation properties: position, rotation (degrees), scale, opacity; no duplicates')
        seen.add(prop)
        keys = track.get('keyframes')
        if not isinstance(keys, list) or not 2 <= len(keys) <= 64:
            raise ValueError('Each animation track needs 2–64 keyframes')
        normalized, previous = [], -1
        for key in keys:
            if not isinstance(key, dict):
                raise ValueError('Keyframes need time and value')
            time = float(key['time'])
            if not math.isfinite(time) or not 0 <= time <= 300 or time <= previous:
                raise ValueError('Keyframe times must increase, between 0 and 300 seconds')
            previous = time
            raw = key['value']
            if prop == 'opacity':
                raw = float(raw)
                if not math.isfinite(raw) or not 0 <= raw <= 1:
                    raise ValueError('Opacity must be between zero and one')
            else:
                from report_media import _vector
                raw = _vector(raw)
                if prop == 'scale' and (min(raw) <= 0 or max(raw) > 100):
                    raise ValueError('Animated scale must be positive and at most 100')
            normalized.append({'time': time, 'value': raw})
        result.append({'property': prop, 'keyframes': normalized})
    return result


def controls(duration):
    return ('<div class="animation-tools"><button type="button" data-animate="play">Play</button>'
            f'<input type="range" min="0" max="{duration}" step="0.01" value="0" aria-label="Animation time" />'
            '<output>0.0 s</output><button type="button" data-animate="restart">Restart</button></div>')


def render_sequence(payload):
    from report_html import blocks_from_payload, render_report
    frames = payload.get('frames')
    if not isinstance(frames, list) or not 1 <= len(frames) <= 20:
        raise ValueError('An animation needs 1–20 frames')
    parts, times, elapsed, total = [], [], 0., 0
    allowed = {'markdown', 'text', 'table', 'diagram', 'math', 'formula', 'latex',
               'chart', 'code', 'image', 'scene3d', 'model3d', 'explorer', 'lesson', 'walkthrough'}
    for i, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise ValueError('Animation frames must be objects')
        duration = float(frame.get('duration', 2))
        if not math.isfinite(duration) or not .1 <= duration <= 60:
            raise ValueError('Frame durations must be between 0.1 and 60 seconds')
        blocks = frame.get('blocks', [])
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= 12 or any(
                not isinstance(b, dict) or b.get('type', b.get('kind', 'markdown')) not in allowed for b in blocks):
            raise ValueError('Each frame needs 1–12 visual blocks; nested animations and actions are unsupported')
        markdown, _ = blocks_from_payload({'blocks': blocks})
        content = render_report(markdown)['html']
        total += len(content)
        elapsed += duration
        if total > 4_000_000 or elapsed > 300:
            raise ValueError('Animation exceeds 4 MB or 300 seconds')
        times.append(elapsed)
        caption = html.escape(str(frame.get('caption') or f'Step {i+1}'))
        parts.append(f'<section class="animation-frame" {"hidden" if i else ""}><h3>{caption}</h3>{content}</section>')
    title = html.escape(str(payload.get('title') or 'Visual walkthrough'))
    return (f'<figure class="report-animation reveal" data-times="{html.escape(json.dumps(times), quote=True)}">'
            f'<figcaption>{title}</figcaption>{"".join(parts)}{controls(elapsed)}</figure>')


ANIMATION_CSS = r"""
.animation-tools{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:14px}
.animation-tools button{background:var(--hover);color:var(--text);border:1px solid var(--line);border-radius:7px;padding:7px 10px;cursor:pointer}
.animation-tools input{min-width:80px;flex:1;accent-color:#75baff}.animation-tools output{font-size:12px;color:var(--dim)}
.report-animation{border:1px solid var(--line);border-radius:12px;padding:18px;margin:26px 0}
.report-animation figcaption{font-size:13px;color:var(--dim)}.animation-frame[hidden]{display:none!important}
@media print{.animation-tools{display:none}.animation-frame[hidden]{display:block!important}}
"""

ANIMATION_JS = r"""
function reportPlayback(figure,duration,update){
 var controls=[].slice.call(figure.children).find(function(e){return e.classList.contains('animation-tools')});if(!controls)return function(){};
 var slider=controls.querySelector('input'),play=controls.querySelector('[data-animate="play"]'),output=controls.querySelector('output');
 var time=0,playing=false,request=0,last=0;
 function paint(){slider.value=time;output.textContent=time.toFixed(1)+' s';update(time);figure.dataset.time=String(time)}
 function pause(){playing=false;cancelAnimationFrame(request);request=0;play.textContent='Play';play.setAttribute('aria-pressed','false')}
 function tick(now){if(!playing)return;if(document.hidden||!figure.isConnected){pause();return}
   if(now-last>=32){time=Math.min(duration,time+(now-last)/1000);last=now;paint()}
   if(time>=duration){pause();return}request=requestAnimationFrame(tick)}
 play.onclick=function(){if(playing){pause();return}if(time>=duration)time=0;playing=true;last=performance.now();play.textContent='Pause';play.setAttribute('aria-pressed','true');request=requestAnimationFrame(tick)};
 slider.oninput=function(){pause();time=+slider.value;paint()};
 controls.querySelector('[data-animate="restart"]').onclick=function(){pause();time=0;paint()};
 function visibility(){if(document.hidden)pause()}document.addEventListener('visibilitychange',visibility);
 var visible=new IntersectionObserver(function(rows){if(!rows[0].isIntersecting)pause()});visible.observe(figure);
 paint();return function(){pause();visible.disconnect();document.removeEventListener('visibilitychange',visibility)};
}
function reportAnimations(root){root.querySelectorAll('.report-animation').forEach(function(figure){
 var times=JSON.parse(figure.dataset.times),frames=[].slice.call(figure.children).filter(function(e){return e.classList.contains('animation-frame')});
 figure._dispose=reportPlayback(figure,times[times.length-1],function(time){var index=times.findIndex(function(t){return time<t});if(index<0)index=times.length-1;
   frames.forEach(function(f,i){f.hidden=i!==index});figure.dataset.frame=String(index)});
})}
"""
