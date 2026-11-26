"""Adaptive local-object + vision-language perception for VANTA.

One screenshot is captured once and then routed by evidence need:

* objects: YOLO26s runs locally and only its structured detections reach the
  language model. Useful for presence, count and approximate location.
* vlm: the screenshot goes to the configured vision-capable LLM. Useful for
  UI, text, code, diagrams, charts and meaning.
* hybrid: YOLO26s supplies grounded boxes/counts and the same screenshot goes
  to the VLM for relationships and semantics.

The cognitive router may request a strategy. This module validates it and has
deterministic fallbacks, so a malformed model decision never disables vision.
YOLO is deliberately not used as a screen-control detector: COCO object labels
cannot replace accessibility/UIA, OCR, or browser DOM controls.
"""
from __future__ import annotations

import base64
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
import os
import threading
import time

from PIL import Image, ImageEnhance, ImageOps, ImageStat


VALID_STRATEGIES = frozenset({"none", "auto", "objects", "vlm", "hybrid"})
MODEL_NAME = "yolo26s.pt"
MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "weights", MODEL_NAME)
_model = None
_model_error = ""
_model_lock = threading.RLock()


def image_luminance(image_or_b64):
    """Return mean perceptual luminance (0..255) without retaining pixels."""
    image = image_or_b64
    if isinstance(image_or_b64, str):
        image = Image.open(BytesIO(base64.b64decode(image_or_b64)))
    grey = image.convert("L").resize((64, 64))
    return float(ImageStat.Stat(grey).mean[0])


def _low_light_variant(image, threshold=None):
    """Create a local detection aid when a frame is genuinely dark.

    The original remains the evidence shown/saved.  This variant is used only
    by local detectors and keeps the same geometry, so boxes remain valid.
    """
    try:
        cutoff = float(threshold if threshold is not None else
                       os.getenv("VANTA_LOW_LIGHT_LUMA", "58"))
    except (TypeError, ValueError):
        cutoff = 58.0
    luminance = image_luminance(image)
    if luminance >= max(10.0, min(cutoff, 120.0)):
        return None, luminance
    lifted = _tone_map_dark(image, luminance)
    if lifted is not None:
        return lifted, luminance
    # Without OpenCV: autocontrast recovers the usable range; bounded
    # brightness then lifts shadows without turning noise into a white frame.
    enhanced = ImageOps.autocontrast(image.convert("RGB"), cutoff=1)
    factor = min(3.0, max(1.25, 85.0 / max(luminance, 12.0)))
    enhanced = ImageEnhance.Brightness(enhanced).enhance(factor)
    return enhanced, luminance


def _tone_map_dark(image, luminance, target=105.0):
    """Lift a dark frame on its brightness channel only.

    The previous enhancement stretched each colour channel separately, so in
    a room lit by a monitor the noise in red, green and blue was amplified
    independently and a face came out purple and red -- which a face
    recogniser reads as somebody else. Here brightness is lifted with a gamma
    curve, local contrast is restored with CLAHE, and the colour channels are
    pulled toward neutral in proportion to how much gain was applied, since
    chroma is where the noise lives.

    Returns None if OpenCV is unavailable.
    """
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    rgb = np.asarray(image.convert("RGB"))
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    # Light denoise BEFORE lifting, or the lift amplifies the grain.
    bgr = cv2.fastNlMeansDenoisingColored(bgr, None, 6, 6, 5, 15)
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    lightness, a_channel, b_channel = cv2.split(lab)
    mean = max(1.0, float(lightness.mean()))
    # gamma < 1 brightens; chosen so the mean lands near `target`.
    gamma = np.log(max(target, 2.0) / 255.0) / np.log(min(mean, 250.0) / 255.0)
    gamma = float(min(1.0, max(0.25, gamma)))
    table = (np.linspace(0, 1, 256) ** gamma * 255.0).astype("uint8")
    lightness = cv2.LUT(lightness, table)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    lightness = clahe.apply(lightness)
    gain = max(1.0, target / max(mean, 1.0))
    keep = float(min(1.0, max(0.25, 1.4 / np.sqrt(gain))))
    a_channel = (128 + (a_channel.astype("float32") - 128) * keep).astype("uint8")
    b_channel = (128 + (b_channel.astype("float32") - 128) * keep).astype("uint8")
    out = cv2.cvtColor(cv2.merge([lightness, a_channel, b_channel]), cv2.COLOR_LAB2RGB)
    return Image.fromarray(out)


def _iou(left, right):
    ax1, ay1, ax2, ay2 = left.box
    bx1, by1, bx2, by2 = right.box
    intersection = max(0, min(ax2, bx2) - max(ax1, bx1)) * \
        max(0, min(ay2, by2) - max(ay1, by1))
    if not intersection:
        return 0.0
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    return intersection / max(1, area_a + area_b - intersection)


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    box: tuple[int, int, int, int]
    normalized_box: tuple[float, float, float, float]


@dataclass(frozen=True)
class VisionPacket:
    image_b64: str
    element_map: str
    strategy: str
    local_context: str
    detections: tuple[Detection, ...]
    send_image_to_vlm: bool
    detector_ok: bool
    elapsed_ms: int = 0
    luminance: float | None = None


def _enabled():
    return os.getenv("VANTA_YOLO_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }


def choose_strategy(request, requested="auto"):
    """Validate the model's choice and settle ``auto`` cheaply."""
    choice = str(requested or "auto").strip().lower()
    if choice not in VALID_STRATEGIES:
        choice = "auto"
    if choice != "auto":
        return choice

    text = " ".join(str(request or "").casefold().split())
    semantic = (
        "read", "text", "word", "code", "error", "website", "page",
        "diagram", "chart", "graph", "table", "document", "article",
        "explain", "teach", "button", "form", "screen control", "click",
    )
    objects = (
        "object", "person", "people", "face", "chair", "bottle", "car",
        "vehicle", "phone", "laptop", "dog", "cat", "what is in front",
        "how many", "where is the", "can you see",
    )
    has_semantic = any(marker in text for marker in semantic)
    has_objects = any(marker in text for marker in objects)
    if has_semantic and has_objects:
        return "hybrid"
    if has_semantic:
        return "vlm"
    if has_objects:
        return "objects"
    # A generic "what is on my screen?" benefits from grounded inventory and
    # semantic context; it is exactly the case where either model alone is
    # incomplete.
    return "hybrid"


def _device():
    """Return the detector device without probing unsafe native GPU code.

    CUDA availability only proves that PyTorch can see a driver.  It does not
    prove that the process can load the matching cuDNN symbols; on Windows a
    mismatch can terminate the interpreter before Python can catch an
    exception.  VANTA therefore uses the dependable CPU path by default.
    Operators may opt into a validated GPU stack with
    ``VANTA_YOLO_DEVICE=0`` (or another Ultralytics device value).
    """
    configured = os.getenv("VANTA_YOLO_DEVICE", "").strip()
    return configured or "cpu"


def _load_model():
    global _model, _model_error
    if _model is not None:
        return _model
    if not _enabled():
        _model_error = "disabled by VANTA_YOLO_ENABLED"
        return None
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from ultralytics import YOLO
            os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
            # Ultralytics downloads the official checkpoint on first use when
            # it is absent. Keeping the requested path under weights prevents
            # model files from appearing in the project root.
            _model = YOLO(MODEL_PATH)
            _model_error = ""
            print(f"[Vision Router] Loaded {MODEL_NAME} on {_device()}.")
        except Exception as exc:
            _model_error = str(exc)[:300]
            print(f"[Vision Router] YOLO26s unavailable ({_model_error}); "
                  "falling back to the vision LLM.")
            _model = None
        return _model


def detector_status():
    return {
        "enabled": _enabled(), "loaded": _model is not None,
        "model": MODEL_NAME, "path": MODEL_PATH, "error": _model_error,
    }


def warm(delay=30.0):
    """Pay model cold-start cost after boot, away from a user turn."""
    if not _enabled():
        return
    time.sleep(max(0.0, float(delay)))
    try:
        image = Image.new("RGB", (640, 384), "black")
        buffer = BytesIO()
        image.save(buffer, "JPEG")
        detect(base64.b64encode(buffer.getvalue()).decode())
        print("[Vision Router] YOLO26s warm-up complete.")
    except Exception as exc:
        print(f"[Vision Router] Warm-up skipped: {exc}")


def detect(image_b64, confidence=None):
    """Return typed COCO detections from one base64 JPEG/PNG."""
    model = _load_model()
    if model is None:
        return (), False
    try:
        threshold = float(confidence if confidence is not None else
                          os.getenv("VANTA_YOLO_CONF", "0.30"))
    except (TypeError, ValueError):
        threshold = 0.30
    image = Image.open(BytesIO(base64.b64decode(image_b64))).convert("RGB")
    width, height = image.size
    started = time.monotonic()
    try:
        # Share the existing GPU vision lock with OmniParser if it is enabled.
        try:
            import vision
            inference_lock = vision._vision_lock
        except Exception:
            inference_lock = _model_lock
        def _predict(candidate):
            with inference_lock:
                results = model.predict(
                    candidate, conf=max(0.05, min(threshold, 0.95)), imgsz=640,
                    device=_device(), verbose=False, max_det=100)
            detected = []
            if not results:
                return detected
            result = results[0]
            names = result.names or {}
            boxes = getattr(result, "boxes", None)
            if boxes is not None:
                coords = boxes.xyxy.detach().cpu().tolist()
                scores = boxes.conf.detach().cpu().tolist()
                classes = boxes.cls.detach().cpu().tolist()
                for raw_box, score, class_id in zip(coords, scores, classes):
                    x1, y1, x2, y2 = (int(round(value)) for value in raw_box)
                    detected.append(Detection(
                        label=str(names.get(int(class_id), int(class_id))),
                        confidence=float(score), box=(x1, y1, x2, y2),
                        normalized_box=(
                            round(x1 / max(width, 1), 3),
                            round(y1 / max(height, 1), 3),
                            round(x2 / max(width, 1), 3),
                            round(y2 / max(height, 1), 3),
                        )))
            return detected

        found = _predict(image)
        enhanced, luminance = _low_light_variant(image)
        used_enhancement = enhanced is not None
        if enhanced is not None:
            # A second local pass is cheaper and more private than escalating
            # a dark frame to a cloud vision model. Merge overlapping results,
            # retaining the stronger confidence.
            for candidate in _predict(enhanced):
                duplicate = next((item for item in found
                                  if item.label == candidate.label
                                  and _iou(item, candidate) >= 0.55), None)
                if duplicate is None:
                    found.append(candidate)
                elif candidate.confidence > duplicate.confidence:
                    found[found.index(duplicate)] = candidate
        found.sort(key=lambda item: item.confidence, reverse=True)
        print(f"[Vision Router] YOLO26s found {len(found)} objects in "
              f"{(time.monotonic() - started) * 1000:.0f}ms"
              + (f" (low-light local pass, luma={luminance:.0f})."
                 if used_enhancement else "."))
        return tuple(found), True
    except Exception as exc:
        global _model_error
        _model_error = str(exc)[:300]
        print(f"[Vision Router] YOLO26s inference failed ({_model_error}); "
              "using the vision LLM.")
        return (), False


def summarize(detections, detector_ok=True):
    if not detector_ok:
        return ("LOCAL OBJECT DETECTOR: unavailable. Do not infer absence from "
                "this; use the screenshot with the vision model.")
    if not detections:
        return ("LOCAL OBJECT DETECTOR (YOLO26s): no supported common objects "
                "were detected. This detector does not read text, UI controls, "
                "diagrams, or arbitrary concepts.")
    counts = Counter(item.label for item in detections)
    lines = [
        "LOCAL OBJECT DETECTOR (YOLO26s):",
        "Counts: " + ", ".join(f"{label}={count}"
                                 for label, count in counts.most_common()),
        "Grounded detections (box coordinates are normalized 0..1):",
    ]
    for item in detections[:40]:
        lines.append(
            f"- {item.label}: confidence={item.confidence:.2f}, "
            f"box={item.normalized_box}")
    lines.append(
        "Treat these as detector evidence, not a full description. The model "
        "cannot identify people, read the screen, or prove an object is absent.")
    return "\n".join(lines)


def prepare_image(request, image_b64, requested_strategy="auto",
                  element_map=""):
    """Route one already-captured image without capturing it again."""
    started = time.monotonic()
    strategy = choose_strategy(request, requested_strategy)
    detections, ok = (), True
    if strategy in {"objects", "hybrid"}:
        detections, ok = detect(image_b64)
        if not ok:
            # A local dependency/download/GPU failure must lose performance,
            # never the user's ability to see an answer.
            strategy = "vlm"
    try:
        luminance = image_luminance(image_b64)
    except Exception:
        luminance = None
    context = summarize(detections, ok) if requested_strategy != "none" \
        and strategy in {"objects", "hybrid"} else ""
    try:
        low_light_cutoff = float(os.getenv("VANTA_LOW_LIGHT_LUMA", "58"))
    except (TypeError, ValueError):
        low_light_cutoff = 58.0
    if context and luminance is not None and luminance < low_light_cutoff:
        context += (
            f"\nLIGHTING: low (mean luminance {luminance:.0f}/255). A local "
            "enhancement pass was attempted. Treat missing detections as "
            "uncertain. For an explicit user-requested camera check, you may "
            "use the available screen-light capability, recapture, and turn "
            "the light off; never do that for passive awareness sampling.")
    return VisionPacket(
        image_b64=image_b64, element_map=element_map,
        strategy=strategy, local_context=context,
        detections=tuple(detections),
        send_image_to_vlm=strategy in {"vlm", "hybrid"},
        detector_ok=ok,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        luminance=luminance,
    )


def prepare_screen(request, base_dir, requested_strategy="auto"):
    """Capture once, run only the needed local model, and return a packet."""
    from vision import take_screenshot_b64
    image_b64, element_map = take_screenshot_b64(base_dir)
    return prepare_image(request, image_b64, requested_strategy,
                         element_map=element_map)
