# -*- coding: utf-8 -*-
"""
ocr_worker.py — reads text from an image, in its own process.

EasyOCR loads native Torch and OpenCV code that has been observed to terminate
the host process on some Windows machines without raising anything Python can
catch. That is why OCR was switched off by default, and why VANTA had no way
to read a web page: Chrome exposes no page content to UI Automation, so OCR is
the only route to a website's text, and it was unavailable.

Isolating it in a subprocess resolves both halves. A native crash takes down
this process and nothing else; the caller sees a non-zero exit code and reports
an ordinary error. Model load happens per call, which is slow, but a capability
that occasionally costs two seconds is worth more than one that occasionally
kills the assistant.

Usage:  python ocr_worker.py <image-path> [min_confidence]
Prints one JSON object: {"ok": true, "items": [{text, confidence, box}]}
"""

import json
import sys


def _build_reader():
    import easyocr
    return easyocr.Reader(["en"], gpu=False, verbose=False)


def _read(reader, path, threshold):
    import numpy as np
    from PIL import Image
    image = np.array(Image.open(path).convert("RGB"))
    raw = reader.readtext(image, detail=1, paragraph=False)
    items = []
    for entry in raw:
        try:
            points, text, confidence = entry[0], str(entry[1]), float(entry[2])
        except (IndexError, ValueError, TypeError):
            continue
        if confidence < threshold or not text.strip():
            continue
        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
        items.append({
            "text": text.strip(),
            "confidence": round(confidence, 3),
            "box": [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))],
        })
    return items


def serve():
    """Stay resident and answer one request per stdin line.

    Loading the models costs over a minute, which is fine once and unusable per
    call. The parent keeps this process alive, so the first read is slow and
    every read after it is fast — while a native crash still kills only this
    process.
    """
    try:
        reader = _build_reader()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), flush=True)
        return 1
    print(json.dumps({"ok": True, "ready": True}), flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line or line == "quit":
            break
        try:
            request = json.loads(line)
            items = _read(reader, request["path"], float(request.get("min_confidence", 0.3)))
            print(json.dumps({"ok": True, "items": items}), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), flush=True)
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        return serve()
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "no image path given"}))
        return 2
    path = sys.argv[1]
    try:
        threshold = float(sys.argv[2]) if len(sys.argv) > 2 else 0.3
    except ValueError:
        threshold = 0.3

    try:
        import easyocr
        import numpy as np
        from PIL import Image

        reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        image = np.array(Image.open(path).convert("RGB"))
        raw = reader.readtext(image, detail=1, paragraph=False)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1

    items = []
    for entry in raw:
        try:
            points, text, confidence = entry[0], str(entry[1]), float(entry[2])
        except (IndexError, ValueError, TypeError):
            continue
        if confidence < threshold or not text.strip():
            continue
        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
        items.append({
            "text": text.strip(),
            "confidence": round(confidence, 3),
            # left, top, right, bottom in the image's own pixels
            "box": [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))],
        })
    print(json.dumps({"ok": True, "items": items}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
