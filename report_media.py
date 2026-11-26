"""Data-only report images and small interactive 3D scenes; no generated code runs."""
import base64
import html
import io
import json
import math
import re
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit

_MEDIA_DEPTH = ContextVar('report_media_depth', default=0)
_MEDIA_ANCESTORS = ContextVar('report_media_ancestors', default=())


def _number(value):
    result = float(value)
    if not math.isfinite(result) or abs(result) > 1e9:
        raise ValueError("Coordinates must be finite and within ±1e9")
    return result


def _vector(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("A position needs three coordinates")
    return [_number(v) for v in value]


def _color(value):
    value = str(value or "#75baff")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("Colors must use #RRGGBB")
    return value


def _obj(path):
    path = Path(path)
    if path.suffix.lower() != ".obj" or path.stat().st_size > 1_000_000:
        raise ValueError("Use an OBJ file smaller than 1 MB, or inline mesh data")
    vertices, faces = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = line.split()
        if cells and cells[0] == "v":
            vertices.append(_vector(cells[1:4]))
        elif cells and cells[0] == "f":
            face = []
            for cell in cells[1:]:
                index = int(cell.split("/")[0])
                if index == 0:
                    raise ValueError("OBJ indices start at one")
                face.append(index - 1 if index > 0 else len(vertices) + index)
            faces.append(face)
    return vertices, faces


def scene_data(payload):
    """Normalize bounded primitive/mesh geometry for the offline canvas viewer."""
    if not isinstance(payload, dict):
        raise ValueError("scene3d requires a JSON object")
    objects = payload.get("objects", [])
    if not isinstance(objects, list) or not 1 <= len(objects) <= 128:
        raise ValueError("A scene needs 1–128 objects")
    from report_geometry import flatten, primitive, transform
    objects = flatten(objects)
    result, count_v, count_f = [], 0, 0
    for obj in objects:
        if not isinstance(obj, dict):
            raise ValueError("Each scene object must be an object")
        kind = str(obj.get("type", "mesh")).lower()
        vertices, faces, edges = [], [], []
        if kind == "box":
            size = _vector(obj.get("size", [obj.get('width', 1), obj.get('height', 1), obj.get('depth', 1)]))
            if min(size) <= 0:
                raise ValueError("Box sizes must be positive")
            vertices = [[x*size[0]/2, y*size[1]/2, z*size[2]/2]
                        for x, y, z in [(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),
                                        (-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)]]
            faces = [[0,3,2,1],[4,5,6,7],[0,1,5,4],[3,7,6,2],[0,4,7,3],[1,2,6,5]]
        elif kind == "sphere":
            radius = _number(obj.get("radius", .5))
            if radius <= 0:
                raise ValueError("Sphere radius must be positive")
            for i in range(7):
                for j in range(10):
                    a, b = math.pi*i/6, math.tau*j/10
                    vertices.append([radius*math.sin(a)*math.cos(b), radius*math.cos(a),
                                     radius*math.sin(a)*math.sin(b)])
            faces = [[i*10+j, i*10+(j+1)%10, (i+1)*10+(j+1)%10, (i+1)*10+j]
                     for i in range(6) for j in range(10)]
        elif kind in ("line", "polyline"):
            vertices = obj.get("points", [])
            if not isinstance(vertices, list) or not 2 <= len(vertices) <= 8000:
                raise ValueError("Lines need 2–8000 points")
            edges = [[i, i+1] for i in range(len(vertices)-1)]
        elif kind == "mesh":
            if obj.get("path"):
                vertices, faces = _obj(obj["path"])
            else:
                vertices, faces = obj.get("vertices", []), obj.get("faces", [])
        else:
            vertices, faces = primitive(kind, obj)
        if not isinstance(vertices, list) or not isinstance(faces, list):
            raise ValueError("Mesh vertices and faces must be arrays")
        count_v += len(vertices)
        count_f += len(faces)
        if not vertices or count_v > 8000 or count_f > 12000:
            raise ValueError("Scene limit: 8000 vertices and 12000 faces")
        if not faces and not edges:
            raise ValueError("A mesh needs faces to draw; use line objects for connections")
        points = transform(vertices, obj)
        for parent in obj['_parents']:
            points = transform(points, parent)
        for face in faces:
            if not isinstance(face, list) or not 3 <= len(face) <= 64 or any(
                    type(i) is not int or i < 0 or i >= len(points) for i in face):
                raise ValueError("Invalid mesh face indices (use zero-based indices)")
        from report_animation import tracks
        result.append({"vertices": points, "faces": faces, "edges": edges,
                       "tracks": tracks(obj.get('animation')),
                       "color": _color(obj.get("color")), "label": str(obj.get("label", ""))[:120]})
    duration = max((t['keyframes'][-1]['time'] for o in result for t in o['tracks']), default=0)
    return {"objects": result, "duration": duration}


def render_scene(payload):
    scene = scene_data(payload)
    encoded = html.escape(json.dumps(scene, allow_nan=False, separators=(",", ":")), quote=True)
    title = html.escape(str(payload.get("title") or "Interactive 3D model"))
    from report_animation import controls
    animation_controls = controls(scene['duration']) if scene['duration'] else ''
    return (f'<figure class="scene3d reveal" data-scene="{encoded}">'
            f'<figcaption>{title}</figcaption><canvas tabindex="0" role="img" '
            f'aria-label="{title}. Drag or use arrow keys to rotate; plus and minus to zoom."></canvas>'
            '<div class="scene-tools"><button type="button" data-view="reset">Reset view</button>'
            '<button type="button" data-view="wire" aria-pressed="false">Wireframe</button>'
            '<span>Drag to rotate · Scroll to zoom</span></div>' + animation_controls + '</figure>')


def render_image(payload):
    """Embed local raster images; HTTPS sources are explicit, never server-fetched."""
    source = str(payload.get("path") or payload.get("src") or "")
    if source.startswith("https://"):
        parsed = urlsplit(source)
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Invalid HTTPS image address")
    else:
        from PIL import Image, ImageOps
        if source.startswith("data:"):
            if not re.match(r"^data:image/(png|jpeg|webp|gif);base64,", source):
                raise ValueError("Only raster image data URLs are supported")
            if len(source) > 12_000_000:
                raise ValueError("Image exceeds size limit")
            raw = base64.b64decode(source.split(",", 1)[1], validate=True)
        else:
            path = Path(source)
            if not path.is_file() or path.stat().st_size > 8_000_000:
                raise ValueError("Image missing or larger than 8 MB")
            raw = path.read_bytes()
        try:
            opened = Image.open(io.BytesIO(raw))
        except Image.DecompressionBombError as exc:
            raise ValueError("Image exceeds safe pixel limits") from exc
        with opened as image:
            if image.width * image.height > 25_000_000:
                raise ValueError("Image exceeds 25 megapixels")
            image = ImageOps.exif_transpose(image)
            image.thumbnail((2000, 2000))
            output = io.BytesIO()
            image.convert("RGBA").save(output, format="PNG")
        source = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    caption = str(payload.get("caption") or payload.get("alt") or "")[:500]
    return ('<figure class="report-image reveal"><img loading="lazy" referrerpolicy="no-referrer" '
            f'src="{html.escape(source, quote=True)}" alt="{html.escape(caption, quote=True)}" />'
            '<p class="image-error" hidden>Image could not be loaded.</p>'
            f'<figcaption>{html.escape(caption)}</figcaption></figure>')


def render_media(language, source):
    depth = _MEDIA_DEPTH.get()
    ancestors = _MEDIA_ANCESTORS.get()
    if depth >= 4 or (language in {'animation', 'questionnaire'} and 'animation' in ancestors) or (language=='walkthrough' and 'walkthrough' in ancestors):
        return '<aside class="callout warn reveal">Could not render media: nested animation is unsupported.</aside>'
    nesting = _MEDIA_DEPTH.set(depth + 1)
    ancestry = _MEDIA_ANCESTORS.set(ancestors + (language,))
    try:
        if len(source) > 12_000_000:
            raise ValueError("Media block exceeds 12 MB")
        payload = json.loads(source)
        if not isinstance(payload, dict):
            raise ValueError("Media content must be a JSON object")
        if language == 'questionnaire':
            from report_forms import render
            return render(payload)
        if language in {'explorer', 'lesson', 'walkthrough'}:
            from report_learning import render
            return render(language, payload)
        if language == 'animation':
            from report_animation import render_sequence
            return render_sequence(payload)
        return render_image(payload) if language == "image" else render_scene(payload)
    except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError) as exc:
        # Show an honest failure in the document rather than a blank viewer.
        return '<aside class="callout warn reveal">Could not render media: ' + html.escape(str(exc)) + '</aside>'
    finally:
        _MEDIA_DEPTH.reset(nesting)
        _MEDIA_ANCESTORS.reset(ancestry)
