# -*- coding: utf-8 -*-
"""
winter_mark.py -- VANTA's identity mark, defined once.

The mark is a V inside a hexagon: the letterform for the name, the hexagon
for the six-fold symmetry every ice crystal grows on. A literal snowflake was
the obvious first answer and the wrong one -- it reads as a weather glyph and
turns to mush below about 32px, while this holds its shape down to 16.

It is monoline and, in the letterform, always monochrome. Glow, bevels,
gradients and cyan sci-fi trim are what make an assistant UI look like a
mock-up rather than something you would leave running all day, so none appear
here. State colour is carried by the hexagon alone, which lets the idle orb
change meaning without the identity flickering underneath it.

Every surface that shows the mark -- the idle orb, the Control Center header,
the taskbar icon, the .ico compiled into the executable -- is generated from
the geometry below, so the identity cannot drift between places.
``draw_on_canvas`` renders it live into Tk; ``render_png`` renders it through
PIL with 8x supersampling, which is the only way to get clean diagonals out
of a library with no anti-aliased line drawing.
"""

import math
import os

_V_WIDTH = 1.16
_V_HEIGHT = 0.88
MARK_VERSION = "vanta-v2-v"


def _hexagon(radius=1.0, rotation=0.0):
    """Flat-top hexagon: vertices left and right, so the V gets its width.

    Six-fold symmetry means a rotation of 60 degrees lands back on itself,
    so spinning this is seamless in either direction -- which is what the
    orb uses to say "still working" without a spinner.
    """
    points = [(math.cos(math.radians(rotation + i * 60)) * radius,
               math.sin(math.radians(rotation + i * 60)) * radius)
              for i in range(6)]
    return [(points[i], points[(i + 1) % 6]) for i in range(6)]


def _letter():
    """The V as two clean strokes with a shared lower vertex."""
    top, bottom = -_V_HEIGHT / 2, _V_HEIGHT / 2
    points = [(-_V_WIDTH / 2, top), (0.0, bottom), (_V_WIDTH / 2, top)]
    return [(points[0], points[1]), (points[1], points[2])]


def geometry(ring_rotation=0.0, ring_radius=1.0):
    """The whole mark as ``(hexagon_segments, letter_segments)``.

    Both are lists of ``((x0, y0), (x1, y1))`` in a unit circle with y
    pointing down, ready to be scaled into any coordinate system. They are
    returned separately because the hexagon and the letterform are allowed
    to differ in colour; nothing else about the mark is.
    """
    return _hexagon(ring_radius, ring_rotation), _letter()


def stroke_width(radius):
    """Optically sized stroke: delicate when large, legible when small.

    A single ratio cannot serve both ends -- the weight that looks refined at
    256px disappears at 16px, and the weight that survives 16px looks clumsy
    at 256. Icon families solve this with optical sizing, so this does too.
    """
    if radius <= 0:
        return 1.0
    ratio = 0.075 if radius < 14 else (0.062 if radius < 32 else 0.048)
    return max(1.0, radius * ratio)


def draw_on_canvas(canvas, cx, cy, radius, color="#f5f5f7", ring_color=None,
                   width=None, tag="vanta-mark", ring_rotation=0.0,
                   ring_scale=1.0):
    """Draw the mark onto a Tk canvas, replacing any previous copy.

    ``ring_color``, ``ring_rotation`` and ``ring_scale`` affect only the
    hexagon. The V never rotates, scales or takes a colour: an identity that
    moves is not an identity, and a letterform tumbling in the corner of the
    eye reads as an error rather than as activity.
    """
    canvas.delete(tag)
    if radius <= 0:
        return
    line_width = width if width is not None else stroke_width(radius)
    hexagon, letter = geometry(ring_rotation, ring_scale)
    for segments, paint in ((hexagon, ring_color or color), (letter, color)):
        for (x0, y0), (x1, y1) in segments:
            canvas.create_line(
                cx + x0 * radius, cy + y0 * radius,
                cx + x1 * radius, cy + y1 * radius,
                fill=paint, width=line_width, capstyle="round",
                joinstyle="round", tags=tag,
            )


def render_png(size, color="#f5f5f7", ring_color=None, background=None,
               padding=0.14):
    """Render the mark to a PIL image, supersampled so the edges are smooth."""
    from PIL import Image, ImageDraw

    scale = 8
    canvas_size = size * scale
    image = Image.new("RGBA", (canvas_size, canvas_size),
                      background or (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    centre = canvas_size / 2.0
    radius = centre * (1.0 - padding)
    # Weight is chosen for the size the icon will actually be seen at, not
    # for the supersampled canvas it is drawn on.
    line_width = max(1, int(stroke_width(size / 2.0) * scale))
    cap = line_width / 2.0

    hexagon, letter = geometry()
    for segments, paint in ((hexagon, ring_color or color), (letter, color)):
        for (x0, y0), (x1, y1) in segments:
            draw.line([centre + x0 * radius, centre + y0 * radius,
                       centre + x1 * radius, centre + y1 * radius],
                      fill=paint, width=line_width, joint="curve")
            # Round caps and joins by hand: PIL draws neither, and without
            # them every vertex of the V shows a notch.
            for px, py in ((x0, y0), (x1, y1)):
                draw.ellipse([centre + px * radius - cap, centre + py * radius - cap,
                              centre + px * radius + cap, centre + py * radius + cap],
                             fill=paint)

    return image.resize((size, size), Image.LANCZOS)


ICON_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)


def ensure_icons(base_dir, color="#f5f5f7", force=False):
    """Generate assets/winter-*.png and assets/winter.ico if absent.

    Returns the assets directory, or None if nothing could be written. A
    missing icon is cosmetic, so every caller treats None as "skip the
    icon", never as a startup failure.
    """
    assets = os.path.join(base_dir, "assets")
    icon = os.path.join(assets, "winter.ico")
    version_file = os.path.join(assets, ".vanta-mark-version")
    try:
        os.makedirs(assets, exist_ok=True)
        try:
            with open(version_file, encoding="utf-8") as handle:
                current_version = handle.read().strip()
        except OSError:
            current_version = ""
        stale = current_version != MARK_VERSION
        if force or stale or not os.path.exists(icon):
            images = [render_png(size, color=color) for size in ICON_SIZES]
            images[-1].save(icon, format="ICO",
                            sizes=[(size, size) for size in ICON_SIZES])
        for size in (32, 64, 128, 256):
            png = os.path.join(assets, f"vanta-{size}.png")
            if force or stale or not os.path.exists(png):
                render_png(size, color=color).save(png, format="PNG")
        with open(version_file, "w", encoding="utf-8") as handle:
            handle.write(MARK_VERSION)
        return assets
    except Exception:
        return None


if __name__ == "__main__":
    where = ensure_icons(os.path.dirname(os.path.abspath(__file__)), force=True)
    print(f"Wrote icons to {where}" if where else "Icon generation failed.")
