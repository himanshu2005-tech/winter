"""Generic scene primitives and static transforms, shared by renderer and catalogue."""
import math

TYPES = ('box', 'sphere', 'ellipsoid', 'cylinder', 'cone', 'torus', 'plane',
         'line', 'polyline', 'mesh', 'group')
ALIASES = {'cube': 'box', 'cuboid': 'box', 'boxgeometry': 'box',
           'spheregeometry': 'sphere', 'cylindergeometry': 'cylinder',
           'conegeometry': 'cone', 'torusgeometry': 'torus', 'planegeometry': 'plane'}


def contract():
    return ("3D scene contract: supported types are " + ', '.join(TYPES) + '. '
            'Compose any named object from these primitives or a custom mesh; '
            'a semantic name is a label, not a primitive type. All positions, rotations and scales '
            'are XYZ vectors; static rotation uses degrees, scale must be positive. '
            'cylinder: radius (or radiusTop/radiusBottom), height, segments (3–64), Y-axis. '
            'cone: radius, height, segments, tip along +Y. torus: radius, tube, segments, '
            'tube_segments (3–32), lies in XZ. ellipsoid: radii XYZ. plane: width, depth, lies in XZ. '
            'group: children plus static position/rotation/scale; do not animate groups. '
            'box: size XYZ; sphere: radius; line/polyline: points; mesh: vertices/faces or existing OBJ path. '
            'Colors use #RRGGBB. Stay within 128 total objects, 8000 vertices and 12000 faces.')


def transform(points, obj):
    from report_media import _number, _vector
    position = _vector(obj.get('position', [0, 0, 0]))
    rotation = [_number(v)*math.pi/180 for v in _vector(obj.get('rotation', [0, 0, 0]))]
    scale = _vector(obj.get('scale', [1, 1, 1]))
    if min(scale) <= 0 or max(scale) > 100:
        raise ValueError('Static scale must be positive and at most 100')
    result = []
    for value in points:
        p = [v*s for v,s in zip(_vector(value), scale)]
        for axis, angle in enumerate(rotation):
            i, j = (axis+1)%3, (axis+2)%3
            x, y = p[i], p[j]
            p[i], p[j] = x*math.cos(angle)-y*math.sin(angle), x*math.sin(angle)+y*math.cos(angle)
        result.append(_vector([a+b for a,b in zip(p, position)]))
    return result


def flatten(objects):
    result, count = [], 0

    def visit(items, parents=(), inherited_color=None):
        nonlocal count
        if not isinstance(items, list) or not items:
            raise ValueError('Groups need a nonempty children array')
        if len(parents) > 6:
            raise ValueError('Scene groups exceed six levels')
        for raw in items:
            count += 1
            if count > 128 or not isinstance(raw, dict):
                raise ValueError('Scene limit: 128 objects, including groups; each must be an object')
            obj = dict(raw)
            kind = ALIASES.get(str(obj.get('type', 'mesh')).strip().lower(), str(obj.get('type', 'mesh')).strip().lower())
            obj['type'] = kind
            if inherited_color is not None:
                obj.setdefault('color', inherited_color)
            if kind == 'group':
                if obj.get('animation'):
                    raise ValueError('Animate child objects, not group transforms')
                transform([[0, 0, 0]], obj)  # Validate even empty/degenerate group transforms.
                visit(obj.get('children'), (obj,) + parents, obj.get('color'))
            else:
                obj['_parents'] = parents
                result.append(obj)
    visit(objects)
    return result


def primitive(kind, obj):
    from report_media import _number, _vector

    def positive(key, default):
        value = _number(obj.get(key, default))
        if value <= 0:
            raise ValueError(f'{key} must be positive')
        return value

    def segments(key, default, maximum):
        value = _number(obj.get(key, default))
        if value != int(value) or not 3 <= value <= maximum:
            raise ValueError(f'{key} must be an integer from 3 to {maximum}')
        return int(value)

    vertices, faces = [], []
    if kind in {'cylinder', 'cone'}:
        radius = positive('radius', .5)
        top = _number(obj.get('radiusTop', obj.get('radius_top', 0 if kind == 'cone' else radius)))
        bottom = _number(obj.get('radiusBottom', obj.get('radius_bottom', radius)))
        if min(top, bottom) < 0 or max(top, bottom) <= 0:
            raise ValueError('Cylinder radii must be nonnegative, with at least one positive')
        height, n = positive('height', 1), segments('segments', 20, 64)
        for y, r in ((-height/2, bottom), (height/2, top)):
            vertices.extend([[r*math.cos(math.tau*j/n), y, r*math.sin(math.tau*j/n)] for j in range(n)])
        faces = [[j, (j+1)%n, n+(j+1)%n, n+j] for j in range(n)]
        if bottom:
            faces.append(list(reversed(range(n))))
        if top:
            faces.append(list(range(n, 2*n)))
    elif kind == 'torus':
        radius, tube = positive('radius', .5), positive('tube', .12)
        if tube >= radius:
            raise ValueError('Torus tube must be smaller than its radius')
        n, m = segments('segments', 24, 64), segments('tube_segments', 8, 32)
        for i in range(n):
            a = math.tau*i/n
            for j in range(m):
                b = math.tau*j/m
                vertices.append([(radius+tube*math.cos(b))*math.cos(a), tube*math.sin(b),
                                 (radius+tube*math.cos(b))*math.sin(a)])
        faces = [[i*m+j, ((i+1)%n)*m+j, ((i+1)%n)*m+(j+1)%m, i*m+(j+1)%m]
                 for i in range(n) for j in range(m)]
    elif kind == 'ellipsoid':
        radii = _vector(obj.get('radii', [.5, .5, .5]))
        if min(radii) <= 0:
            raise ValueError('Ellipsoid radii must be positive')
        for i in range(9):
            for j in range(16):
                a, b = math.pi*i/8, math.tau*j/16
                vertices.append([radii[0]*math.sin(a)*math.cos(b), radii[1]*math.cos(a),
                                 radii[2]*math.sin(a)*math.sin(b)])
        faces = [[i*16+j, i*16+(j+1)%16, (i+1)*16+(j+1)%16, (i+1)*16+j]
                 for i in range(8) for j in range(16)]
    elif kind == 'plane':
        width, depth = positive('width', 1), positive('depth', 1)
        vertices = [[-width/2, 0, -depth/2], [width/2, 0, -depth/2],
                    [width/2, 0, depth/2], [-width/2, 0, depth/2]]
        faces = [[0, 1, 2, 3]]
    else:
        raise ValueError(f'Unsupported 3D object type {kind!r}. {contract()}')
    return vertices, faces
