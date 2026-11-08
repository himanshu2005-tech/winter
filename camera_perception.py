"""Parallel local perception for a physical webcam frame.

YOLO26s answers what common objects are present. InsightFace answers whether a
visible face matches an explicitly enrolled Person. The vision LLM still sees
the original image for text, relationships and meaning. Running the two local
models in parallel keeps their extra grounding from becoming serial latency.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import time


@dataclass(frozen=True)
class CameraPerception:
    object_context: str
    face_context: str
    face_matches: tuple
    elapsed_ms: int
    warnings: tuple[str, ...] = ()
    strategy: str = "objects"
    send_image_to_vlm: bool = False
    detector_ok: bool = True

    @property
    def model_context(self):
        return "\n\n".join(part for part in
                           (self.object_context, self.face_context) if part)


def analyze(image_path, image_b64, request="", requested_strategy="auto"):
    """Ground one camera frame; a failed optional model never loses the photo."""
    started = time.monotonic()
    warnings = []

    def objects():
        from vision_pipeline import prepare_image
        packet = prepare_image(request, image_b64,
                               requested_strategy=requested_strategy)
        return packet

    def faces():
        import face_identity
        matched = face_identity.identify(image_path)
        return matched, face_identity.context_for_matches(matched)

    object_context, face_context, matches = "", "", ()
    object_packet = None
    with ThreadPoolExecutor(max_workers=2,
                            thread_name_prefix="vanta-camera") as pool:
        object_future = pool.submit(objects)
        face_future = pool.submit(faces)
        try:
            object_packet = object_future.result()
            object_context = object_packet.local_context
        except Exception as exc:
            warnings.append(f"object detector unavailable: {type(exc).__name__}")
        try:
            matches, face_context = face_future.result()
        except Exception as exc:
            warnings.append(f"face identity unavailable: {type(exc).__name__}")

    print(f"[Camera Perception] objects={'yes' if object_context else 'no'}; "
          f"faces={len(matches)}; warnings={len(warnings)}; "
          f"elapsed={(time.monotonic() - started) * 1000:.0f}ms.")
    return CameraPerception(
        object_context=object_context, face_context=face_context,
        face_matches=tuple(matches),
        elapsed_ms=int((time.monotonic() - started) * 1000),
        warnings=tuple(warnings),
        strategy=getattr(object_packet, "strategy", "vlm"),
        send_image_to_vlm=bool(getattr(object_packet, "send_image_to_vlm", True)),
        detector_ok=bool(getattr(object_packet, "detector_ok", False)))
