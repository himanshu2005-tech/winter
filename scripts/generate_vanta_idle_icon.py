"""Generate the Windows icon from the same Soul renderer used by VANTA's idle UI.

The desktop shell cannot animate an ``.ico`` file, so this captures one
representative frame of the verified, resting Soul.  Keeping generation here
prevents the launcher icon and the in-app identity from becoming unrelated
artwork again.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from soul import Soul  # noqa: E402
from theme import SURFACE  # noqa: E402


ASSETS = ROOT / "assets"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def build_idle_frame(size: int = 512) -> Image.Image:
    soul = Soul("idle")
    soul.set_guarded(False)

    # Move away from the perfectly symmetrical first frame while remaining in
    # the same quiet idle mood users see in the top indicator.
    for _ in range(12):
        soul.step(0.25)

    frame = soul.render(size, SURFACE, supersample=2).convert("RGBA")

    # Soul.render composites onto the notch surface because Tk uses an opaque
    # canvas.  A desktop icon needs a transparent exterior, while retaining
    # the same dark-glass sphere and its soft halo.
    alpha = Image.new("L", (size, size), 0)
    inset = max(2, round(size * 0.008))
    ImageDraw.Draw(alpha).ellipse(
        (inset, inset, size - inset - 1, size - inset - 1), fill=255
    )
    alpha = alpha.filter(ImageFilter.GaussianBlur(max(1, size * 0.006)))
    frame.putalpha(alpha)
    return frame


def generate() -> Path:
    ASSETS.mkdir(parents=True, exist_ok=True)
    master = build_idle_frame()
    resampling = getattr(Image, "Resampling", Image).LANCZOS

    for size in (32, 64, 128, 256):
        master.resize((size, size), resampling).save(
            ASSETS / f"vanta-idle-{size}.png"
        )

    icon_path = ASSETS / "vanta-idle.ico"
    master.save(icon_path, format="ICO", sizes=[(n, n) for n in SIZES])
    return icon_path


if __name__ == "__main__":
    print(generate())
