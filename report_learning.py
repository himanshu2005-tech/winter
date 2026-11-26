"""Composable learning reports: mathematical explorers, lesson sections and traces."""
import ast
import hashlib
import html
import json
import math
import keyword

BLOCK_TYPES = {'explorer', 'lesson', 'walkthrough'}
FUNCTIONS = {'sin': (1, 1), 'cos': (1, 1), 'tan': (1, 1), 'sqrt': (1, 1),
             'exp': (1, 1), 'log': (1, 1), 'log10': (1, 1), 'abs': (1, 1),
             'floor': (1, 1), 'ceil': (1, 1), 'min': (2, 4), 'max': (2, 4),
             'atan': (1, 1), 'asin': (1, 1), 'acos': (1, 1)}


def content_key(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()[:24]


def text(value, limit=500):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f'Text must be a string of at most {limit} characters')
    return value


def number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or abs(value) > 1e6:
        raise ValueError('Use finite numeric values within ±1e6')
    return value


def expression(value, names):
    """Compile an arithmetic grammar into data, never Python/JavaScript code."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ['constant', number(value)]
    source = text(value, 240)
    try:
        tree = ast.parse(source, mode='eval')
    except (SyntaxError, RecursionError) as exc:
        raise ValueError('Invalid mathematical expression') from exc
    if len(list(ast.walk(tree))) > 80:
        raise ValueError('Expression exceeds 80 syntax nodes')
    ops = {ast.Add: 'add', ast.Sub: 'sub', ast.Mult: 'mul', ast.Div: 'div', ast.Pow: 'pow',
           ast.Mod: 'mod', ast.FloorDiv: 'floor_div', ast.Lt: 'lt', ast.LtE: 'le',
           ast.Gt: 'gt', ast.GtE: 'ge', ast.Eq: 'eq', ast.NotEq: 'ne'}

    def visit(node, depth=0):
        if depth > 12:
            raise ValueError('Expression exceeds 12 levels')
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            return ['constant', number(node.value)]
        if isinstance(node, ast.Name) and node.id in names:
            return ['variable', node.id]
        child = lambda n: visit(n, depth+1)
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return [ops[type(node.op)], child(node.left), child(node.right)]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            return ['neg' if isinstance(node.op, ast.USub) else 'positive', child(node.operand)]
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in ops:
            return [ops[type(node.ops[0])], child(node.left), child(node.comparators[0])]
        if isinstance(node, ast.IfExp):
            return ['if', child(node.test), child(node.body), child(node.orelse)]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS:
            low, high = FUNCTIONS[node.func.id]
            if not node.keywords and low <= len(node.args) <= high:
                return ['call', node.func.id, *[child(arg) for arg in node.args]]
        raise ValueError('Use only arithmetic, comparisons, conditionals and documented math functions; unknown name or operation')
    return visit(tree.body)


def explorer(payload):
    if not isinstance(payload, dict):
        raise ValueError('Explorer must be a JSON object')
    params = payload.get('parameters', [])
    if not isinstance(params, list) or len(params) > 8:
        raise ValueError('Use at most eight explorer parameters')
    names, normalized = {'x', 't', 'pi', 'e'}, []
    for p in params:
        if not isinstance(p, dict):
            raise ValueError('Parameters need id, min, max and value')
        ident = p.get('id')
        if not isinstance(ident, str) or not ident.isascii() or not ident.isidentifier() or keyword.iskeyword(ident) or ident.startswith('__') or ident in {'constructor','prototype'} or len(ident)>32 or ident in names or ident in FUNCTIONS:
            raise ValueError('Parameter IDs must be unique ASCII names, excluding x/t/pi/e and math functions')
        names.add(ident)
        low, high = number(p.get('min', 0)), number(p.get('max', 1))
        step, value = number(p.get('step', (high-low)/100)), number(p.get('value', low))
        if low >= high or step <= 0 or not low <= value <= high:
            raise ValueError('Invalid parameter range, step or initial value')
        slots = (value-low)/step
        if not math.isfinite(slots) or not math.isclose(slots, round(slots), abs_tol=1e-7):
            raise ValueError('Initial parameter value must match its step')
        normalized.append({'id': ident, 'label': text(p.get('label', ident), 100),
                           'min': low, 'max': high, 'step': step, 'value': value})
    plot = payload.get('plot', {})
    if not isinstance(plot, dict):
        raise ValueError('plot must be an object')
    bounds = {}
    for axis in ('x', 'y'):
        r = plot.get(axis+'_range', [-5, 5])
        if not isinstance(r, list) or len(r) != 2 or number(r[0]) >= number(r[1]) or r[1]-r[0]<1e-9:
            raise ValueError('Plot ranges need two increasing finite numbers')
        bounds[axis+'_range'] = r
        bounds[axis+'_label'] = text(plot.get(axis+'_label', axis), 100)
    bounds['axes'] = plot.get('axes', True)
    if not isinstance(bounds['axes'], bool):
        raise ValueError('axes must be a boolean')
    curves, shapes, outputs = [], [], []
    from report_media import _color
    raw_curves, raw_shapes, raw_outputs = payload.get('curves', []), payload.get('shapes', []), payload.get('outputs', [])
    if not all(isinstance(v, list) for v in (raw_curves, raw_shapes, raw_outputs)) or len(raw_curves)>8 or len(raw_shapes)>60 or len(raw_outputs)>12:
        raise ValueError('Explorer limits: 8 curves, 60 shapes, 12 outputs')
    for c in raw_curves:
        if not isinstance(c, dict):
            raise ValueError('Curve requires an expression')
        curves.append({'label': text(c.get('label', ''), 100), 'color': _color(c['color']) if c.get('color') is not None else None,
                       'expression': expression(c.get('expression'), names)})
    fields = {'point': ('x','y'), 'label': ('x','y'), 'circle': ('x','y','radius'),
              'line': ('x','y','x2','y2'), 'arrow': ('x','y','x2','y2'), 'rect': ('x','y','width','height')}
    for shape in raw_shapes:
        if not isinstance(shape, dict) or shape.get('type') not in fields:
            raise ValueError('Explorer shapes: point, label, circle, line, arrow, rect')
        s = {'type': shape['type'], 'label': text(shape.get('label', ''), 120), 'color': _color(shape['color']) if shape.get('color') is not None else None}
        for key in fields[s['type']]:
            s[key] = expression(shape.get(key), names-{'x'})
        shapes.append(s)
    for output in raw_outputs:
        if not isinstance(output, dict):
            raise ValueError('Outputs need label and expression')
        outputs.append({'label': text(output.get('label', ''), 100), 'unit': text(output.get('unit', ''), 40),
                        'expression': expression(output.get('expression'), names-{'x'})})
    if not curves and not shapes:
        raise ValueError('An explorer needs at least one curve or shape')
    duration = number(payload.get('duration', 0))
    if not 0 <= duration <= 300:
        raise ValueError('Explorer duration must be between 0 and 300 seconds')
    return {'title': text(payload.get('title', 'Explore the concept'), 160), 'parameters': normalized,
            'plot': bounds, 'curves': curves, 'shapes': shapes, 'outputs': outputs, 'duration': duration}


def render_explorer(payload):
    spec = explorer(payload)
    esc = lambda v: html.escape(str(v), quote=True)
    controls = ''.join(f'<label>{esc(p["label"])} <output>{esc(p["value"])}</output>'
                       f'<input type="range" data-parameter="{p["id"]}" aria-label="{esc(p["label"])}" '
                       f'min="{p["min"]}" max="{p["max"]}" step="{p["step"]}" value="{p["value"]}" /></label>' for p in spec['parameters'])
    from report_animation import controls as playback
    return (f'<figure class="report-explorer reveal" data-report-key="{content_key(spec)}" data-explorer="{esc(json.dumps(spec))}">'
            f'<figcaption>{esc(spec["title"])}</figcaption><canvas role="img" aria-label="{esc(spec["title"])}"></canvas>'
            '<div class="explorer-legend">' + ''.join(f'<span><i style="background:{c["color"] or "var(--accent)"}"></i>{esc(c["label"])}</span>' for c in spec['curves']) + '</div>'
            f'<div class="explorer-controls">{controls}</div><div class="explorer-outputs"></div>'
            '<p class="explorer-status" role="status"></p>' + (playback(spec['duration']) if spec['duration'] else '') + '</figure>')


def _child_source(blocks):
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 16 or any(not isinstance(b, dict) for b in blocks):
        raise ValueError('Each section needs 1–16 report blocks')
    from report_html import blocks_from_payload, render_report
    allowed = {'markdown','text','math','latex','formula','image','scene3d','model3d','animation','questionnaire',
               'explorer','lesson','walkthrough','diagram','chart','code','table','actions'}
    if any(b.get('type', b.get('kind', 'markdown')) not in allowed for b in blocks):
        raise ValueError('Section contains an unsupported report block')
    markdown, actions = blocks_from_payload({'blocks': blocks})
    if not markdown.strip() and not actions:
        raise ValueError('Learning section has no content')
    return markdown, actions


def _children(blocks):
    from report_html import render_report
    markdown, actions = _child_source(blocks)
    result = render_report(markdown, actions=actions)['html']
    if len(result) > 2_000_000:
        raise ValueError('Learning section exceeds 2 MB')
    return result


def lesson(payload, render=True, depth=0, in_animation=False):
    if not isinstance(payload, dict) or payload.get('layout', 'tabs') not in {'tabs', 'accordion', 'flashcards'}:
        raise ValueError('Lesson layout: tabs, accordion or flashcards')
    sections = payload.get('sections')
    if not isinstance(sections, list) or not 1 <= len(sections) <= 12:
        raise ValueError('Lesson needs 1–12 sections')
    esc = lambda v: html.escape(str(v), quote=True)
    layout, panels, buttons, size = payload.get('layout', 'tabs'), [], [], 0
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    for i, section in enumerate(sections):
        if not isinstance(section, dict):
            raise ValueError('Sections must be objects with title and blocks')
        title = text(section.get('title', f'Section {i+1}'), 160)
        body = _children(section.get('blocks')) if render else ''
        size += len(body)
        if size > 4_000_000:
            raise ValueError('Lesson exceeds 4 MB')
        if not render:
            from report_html import blocks_from_payload, validate_scenes
            nested, _ = _child_source(section.get('blocks'))
            errors = validate_scenes(nested, depth=depth+1, in_animation=in_animation)
            if errors:
                raise ValueError(' | '.join(errors))
        pid, bid = f'lesson-{identity}-{i}', f'tab-{identity}-{i}'
        if layout == 'tabs':
            buttons.append(f'<button type="button" role="tab" id="{bid}" aria-controls="{pid}" aria-selected="{"false" if i else "true"}" tabindex="{"-1" if i else "0"}">{esc(title)}</button>')
            panels.append(f'<section role="tabpanel" tabindex="0" id="{pid}" aria-labelledby="{bid}" {"hidden" if i else ""}>{body}</section>')
        else:
            panels.append(f'<details><summary>{esc(title)}</summary><div class="lesson-content">{body}</div></details>')
    heading = esc(text(payload.get('title', 'Explore the idea'), 160))
    return (f'<section class="report-lesson reveal" data-report-key="{content_key(payload)}" data-layout="{layout}"><h3>{heading}</h3>'
            + ('<p class="lesson-hint">Select a card to reveal its explanation.</p>' if layout == 'flashcards' else '')
            + ('<div role="tablist" aria-label="Learning sections">'+''.join(buttons)+'</div>' if buttons else '') + ''.join(panels) + '</section>')


def walkthrough(payload, render=True, depth=0, in_animation=False):
    if not isinstance(payload, dict):
        raise ValueError('Walkthrough must be an object')
    code, steps = text(payload.get('code', ''), 24000).splitlines(), payload.get('steps')
    if not code or not any(line.strip() for line in code) or len(code)>300 or not isinstance(steps, list) or not 1 <= len(steps) <= 50:
        raise ValueError('Walkthrough needs code (up to 300 lines) and 1–50 steps')
    esc = lambda v: html.escape(str(v), quote=True)
    panels, size = [], 0
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError('Walkthrough steps must be objects')
        lines = step.get('lines', [])
        if not isinstance(lines, list) or len(lines)>300 or any(type(n) is not int or not 1 <= n <= len(code) for n in lines):
            raise ValueError('Highlighted lines must be valid one-based code line numbers')
        variables = step.get('variables', {})
        if not isinstance(variables, dict) or len(variables)>30:
            raise ValueError('A step may show at most 30 variables')
        rows = ''.join(f'<tr><td>{esc(text(k, 80))}</td><td><code>{esc(text(str(v), 1000))}</code></td></tr>' for k,v in variables.items())
        content = step.get('blocks', [{'type':'markdown','text':text(step.get('explanation',''), 4000)}])
        if not content or (len(content)==1 and content[0].get('type')=='markdown' and not content[0].get('text','').strip()):
            explanation = ''
        elif render:
            explanation = _children(content)
        else:
            from report_html import validate_scenes
            nested, _ = _child_source(content)
            errors = validate_scenes(nested, depth=depth+1, in_animation=in_animation)
            if errors:
                raise ValueError(' | '.join(errors))
            explanation = ''
        size += len(explanation) + len(rows)
        if size > 4_000_000:
            raise ValueError('Walkthrough exceeds 4 MB')
        panels.append(f'<section class="walk-step" data-lines="{esc(json.dumps(lines))}" {"hidden" if i else ""}>'
                      f'<h4>{esc(text(step.get("title", f"Step {i+1}"),160))}</h4>{explanation}'
                      + ('<div class="table-wrap"><table class="walk-state"><thead><tr><th>Variable</th><th>Value</th></tr></thead><tbody>'+rows+'</tbody></table></div>' if rows else '')+'</section>')
    return (f'<section class="report-walkthrough reveal" data-report-key="{content_key(payload)}"><h3>'+esc(text(payload.get('title', 'Walk through the code'),160))
            + '</h3><p class="lesson-hint">Illustrative walkthrough · code is not executed by this report.</p>'
            '<ol class="walk-code">' + ''.join(f'<li><code>{esc(line) or " "}</code></li>' for line in code) + '</ol>'
            + ''.join(panels) + '<div class="walk-controls"><button type="button" data-walk="back">Back</button>'
            '<output></output><button type="button" data-walk="next">Next step</button></div></section>')


def render(kind, payload):
    if kind == 'explorer':
        return render_explorer(payload)
    if kind == 'lesson':
        return lesson(payload)
    return walkthrough(payload)


def validate(kind, payload, depth=0, in_animation=False):
    if kind == 'explorer':
        return explorer(payload)
    if kind == 'lesson':
        return lesson(payload, render=False, depth=depth, in_animation=in_animation)
    return walkthrough(payload, render=False, depth=depth, in_animation=in_animation)


def contract():
    return ('Learning blocks: explorer, lesson, walkthrough. An explorer is JSON with title, '
            'parameters [{id,label,min,max,step,value}], plot {x_range:[min,max],y_range:[min,max],x_label,y_label,axes}, '
            'curves [{label,expression,color}], shapes [{type,x,y,...,label,color}], outputs [{label,expression,unit}], '
            'and optional duration (seconds, enables paused playback with variable t). '
            'Shapes: point/label (x,y), circle (x,y,radius), line/arrow (x,y,x2,y2), rect (x,y,width,height). '
            'Numeric fields may be expressions using parameter IDs and t; curves additionally use x. '
            'Expressions support + - * / // % **, comparisons, Python a if condition else b, '
            'pi/e and sin cos tan asin acos atan sqrt exp log log10 abs floor ceil min max; trig uses radians. '
            'No code, imports, attributes or arbitrary scripts. Invalid math-domain values are shown as undefined. '
            'Lesson JSON: {title,layout:"tabs"|"accordion"|"flashcards",sections:[{title,blocks:[existing report blocks]}]}. '
            'Walkthrough JSON: {title,code,steps:[{title,explanation,lines:[one-based code lines],variables:{name:value}}]}. '
            'These are illustrative traces; get actual execution evidence from build tools before claiming code ran. '
            'All work as fenced explorer/lesson/walkthrough JSON or structured blocks with those types. '
            'Use a few complementary views appropriate to the concept, not every widget for every answer.')
