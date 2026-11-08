import base64
from io import BytesIO
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
from vision import screen_regions_from_b64


def run():
    image = Image.new("RGB", (1366, 768), "white")
    buf = BytesIO()
    image.save(buf, "JPEG", quality=90)
    encoded = base64.b64encode(buf.getvalue()).decode()
    regions = screen_regions_from_b64(encoded)
    assert [label for label, _ in regions] == ["left side", "right side"]
    for _, region_b64 in regions:
        region = Image.open(BytesIO(base64.b64decode(region_b64)))
        assert region.width >= 1000
        assert region.height > 700
    assert screen_regions_from_b64("") == []
    print("Vision region checks passed")


if __name__ == "__main__":
    run()