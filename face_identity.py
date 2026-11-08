"""Local, graph-backed face identity for VANTA.

Detection and alignment use SCRFD-10GF and recognition uses the ArcFace
ResNet50 model in InsightFace's ``buffalo_l`` package. This is deliberately
separate from YOLO: YOLO finds common objects; this service answers the much
narrower and more sensitive identity question.

Security boundaries:
* enrollment does not retain a source photograph;
* 512-D templates are AES-GCM encrypted before entering Neo4j;
* templates use a restricted ``FaceTemplate`` label, outside ordinary RAG;
* a match never authorizes an action or proves physical identity;
* uncertain and ambiguous matches remain unknown.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import threading
import time

import numpy as np


MODEL_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "weights", "insightface")

# Two recognisers are supported. antelopev2 (ArcFace ResNet-100 trained on
# Glint360K) is the more accurate and is used whenever it is installed;
# buffalo_l (ResNet-50) is the earlier one and the fallback. A template is only
# comparable with templates from the same model, so each is stored with its
# model's name and matched only against its own kind.
_MODELS = {
    "antelopev2": dict(file="glintr100.onnx", detector="SCRFD-10GF",
                       recognizer="ArcFace-ResNet100@Glint360K"),
    "buffalo_l": dict(file="w600k_r50.onnx", detector="SCRFD-10GF",
                      recognizer="ArcFace-ResNet50@WebFace600K"),
}


def _model_installed(name):
    spec = _MODELS.get(name)
    return bool(spec and os.path.exists(
        os.path.join(MODEL_ROOT, "models", name, spec["file"])))


def _choose_model():
    wanted = os.getenv("VANTA_FACE_MODEL", "").strip()
    if wanted in _MODELS:
        return wanted
    return "antelopev2" if _model_installed("antelopev2") else "buffalo_l"


MODEL_NAME = _choose_model()
DETECTOR_NAME = _MODELS[MODEL_NAME]["detector"]
RECOGNIZER_NAME = _MODELS[MODEL_NAME]["recognizer"]


def _use_model(name):
    global MODEL_NAME, DETECTOR_NAME, RECOGNIZER_NAME
    MODEL_NAME = name
    DETECTOR_NAME = _MODELS[name]["detector"]
    RECOGNIZER_NAME = _MODELS[name]["recognizer"]

#: The owner, whose face the presence checks look for.
PRIMARY_PERSON = os.getenv("VANTA_PRIMARY_PERSON", "Himanshu")

MATCH_THRESHOLD = float(os.getenv("VANTA_FACE_MATCH_THRESHOLD", "0.50"))
AMBIGUITY_MARGIN = float(os.getenv("VANTA_FACE_AMBIGUITY_MARGIN", "0.08"))
MIN_DETECTION_SCORE = float(os.getenv("VANTA_FACE_DET_THRESHOLD", "0.60"))
MIN_FACE_PIXELS = int(os.getenv("VANTA_FACE_MIN_PIXELS", "80"))
MAX_TEMPLATES_PER_PERSON = int(os.getenv("VANTA_FACE_MAX_TEMPLATES", "5"))
LICENSE = "non-commercial-research"

_app = None
_app_error = ""
_app_lock = threading.RLock()
_template_lock = threading.RLock()
_template_cache = (0.0, ())
_KEY_SERVICE = "VANTA.FaceIdentity"
_KEY_USER = "template-aes-gcm-v1"
_AAD = b"vanta-face-template|buffalo_l|arcface|v1"

# Neo4j is the authoritative biometric store once VANTA is running, but it
# cannot also be the credential needed to decide whether Neo4j may start.  The
# boot gate therefore keeps only the primary user's already-encrypted template
# records in a signed, user-private cache.  No source image or plaintext vector
# is written here.  A normal post-authentication startup reconciles this cache
# from Neo4j, and enrol/forget updates it immediately.
_BOOT_DIR = os.path.join(
    os.getenv("LOCALAPPDATA") or os.path.expanduser("~"), "VANTA", "identity")
BOOT_CACHE_PATH = os.path.join(_BOOT_DIR, "boot_templates.json")
BOOT_TICKET_PATH = os.path.join(_BOOT_DIR, "startup_ticket.json")
_BOOT_CACHE_VERSION = 1
_BOOT_TICKET_TTL_SECONDS = 240.0


@dataclass(frozen=True)
class DetectedFace:
    bbox: tuple[int, int, int, int]
    normalized_box: tuple[float, float, float, float]
    detection_score: float
    quality: float
    embedding: np.ndarray


@dataclass(frozen=True)
class FaceMatch:
    status: str
    bbox: tuple[int, int, int, int]
    normalized_box: tuple[float, float, float, float]
    detection_score: float
    person: str = ""
    similarity: float = 0.0
    runner_up: float = 0.0

    @property
    def recognized(self):
        return self.status == "recognized" and bool(self.person)


def _enabled():
    return os.getenv("VANTA_FACE_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }


def _providers():
    configured = [value.strip() for value in
                  os.getenv("VANTA_FACE_PROVIDERS", "").split(",")
                  if value.strip()]
    if configured:
        return configured
    try:
        import onnxruntime as ort
        available = set(ort.get_available_providers())
        if "CUDAExecutionProvider" in available:
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    except Exception:
        pass
    return ["CPUExecutionProvider"]


def _load_app():
    global _app, _app_error
    if _app is not None:
        return _app
    if not _enabled():
        _app_error = "disabled by VANTA_FACE_ENABLED"
        return None
    with _app_lock:
        if _app is not None:
            return _app
        try:
            from insightface.app import FaceAnalysis
            os.makedirs(MODEL_ROOT, exist_ok=True)
            _app = FaceAnalysis(
                name=MODEL_NAME, root=MODEL_ROOT,
                allowed_modules=["detection", "recognition"],
                providers=_providers())
            _app.prepare(ctx_id=0, det_thresh=MIN_DETECTION_SCORE,
                         det_size=(640, 640))
            _app_error = ""
            print(f"[Face Identity] {MODEL_NAME} ready with "
                  f"{', '.join(_providers())}.")
        except Exception as exc:
            _app_error = f"{type(exc).__name__}: {exc}"[:500]
            _app = None
            print(f"[Face Identity] {MODEL_NAME} unavailable ({_app_error}).")
            if MODEL_NAME != "buffalo_l" and _model_installed("buffalo_l"):
                print("[Face Identity] Falling back to buffalo_l.")
                _use_model("buffalo_l")
                _invalidate_cache()
                return _load_app()
        return _app


def status():
    return {
        "enabled": _enabled(), "loaded": _app is not None,
        "error": _app_error, "model": MODEL_NAME,
        "detector": DETECTOR_NAME, "recognizer": RECOGNIZER_NAME,
        "providers": _providers(), "model_root": MODEL_ROOT,
        "license": LICENSE, "match_threshold": MATCH_THRESHOLD,
        "ambiguity_margin": AMBIGUITY_MARGIN,
    }


def warm(delay=45.0):
    time.sleep(max(0.0, float(delay)))
    app = _load_app()
    if app is None:
        return
    # Exercise SCRFD once without touching a user image. Session creation and
    # graph optimisation are the expensive part of the first camera request.
    # A black frame contains no biometric information and is never persisted.
    try:
        with _app_lock:
            app.get(np.zeros((640, 640, 3), dtype=np.uint8), max_num=1)
        print(f"[Face Identity] {MODEL_NAME} warm-up complete.")
    except Exception as exc:
        print(f"[Face Identity] Warm-up skipped: {type(exc).__name__}: {exc}")
    notice = migration_notice()
    if notice:
        print(f"[Face Identity] {notice}")


def _read_image(path):
    import cv2
    image = cv2.imread(os.path.abspath(path))
    if image is None:
        raise ValueError(f"could not read image: {path}")
    return image


def detect_image(image):
    """Return usable ArcFace embeddings from an in-memory BGR frame.

    Keeping lifecycle authentication in memory lets its camera preview remain
    live while recognition runs on a copy of the latest frame.  File-backed
    callers still use :func:`detect` below.
    """
    app = _load_app()
    if app is None:
        raise RuntimeError(_app_error or "InsightFace is unavailable")
    image = np.asarray(image)
    if image.ndim != 3 or image.shape[2] < 3:
        raise ValueError("face detection requires a BGR colour image")
    image = np.ascontiguousarray(image[:, :, :3])
    height, width = image.shape[:2]
    with _app_lock:
        raw_faces = app.get(image, max_num=10)
    if not raw_faces:
        # SCRFD can miss a face that is structurally present but underexposed.
        # Retry once on a geometry-preserving, local-only enhancement. The
        # source photograph is neither replaced nor sent anywhere.
        try:
            import vision_pipeline
            from PIL import Image
            rgb = Image.fromarray(image[:, :, ::-1])
            enhanced, luminance = vision_pipeline._low_light_variant(rgb)
            if enhanced is not None:
                enhanced_bgr = np.asarray(enhanced)[:, :, ::-1].copy()
                with _app_lock:
                    raw_faces = app.get(enhanced_bgr, max_num=10)
                print(f"[Face Identity] Low-light local retry "
                      f"(luma={luminance:.0f}, faces={len(raw_faces)}).")
        except Exception as exc:
            print(f"[Face Identity] Low-light retry unavailable: "
                  f"{type(exc).__name__}: {exc}")
    faces = []
    for face in raw_faces:
        bbox = np.asarray(face.bbox, dtype=np.float32)
        x1, y1, x2, y2 = [int(round(value)) for value in bbox]
        face_width, face_height = max(0, x2 - x1), max(0, y2 - y1)
        score = float(getattr(face, "det_score", 0.0) or 0.0)
        raw_embedding = getattr(face, "normed_embedding", None)
        if raw_embedding is None:
            continue
        embedding = np.asarray(raw_embedding, dtype=np.float32)
        if (embedding.ndim != 1 or embedding.size < 128
                or score < MIN_DETECTION_SCORE
                or min(face_width, face_height) < MIN_FACE_PIXELS):
            continue
        norm = float(np.linalg.norm(embedding))
        if not np.isfinite(norm) or norm <= 0:
            continue
        embedding = embedding / norm
        # Quality is confidence plus usable resolution, not an estimate of
        # attractiveness, age, gender or any other personal attribute.
        resolution = min(1.0, min(face_width, face_height) / 160.0)
        quality = round(score * resolution, 4)
        faces.append(DetectedFace(
            bbox=(x1, y1, x2, y2),
            normalized_box=(round(x1 / max(width, 1), 4),
                            round(y1 / max(height, 1), 4),
                            round(x2 / max(width, 1), 4),
                            round(y2 / max(height, 1), 4)),
            detection_score=score, quality=quality, embedding=embedding))
    return tuple(faces)


def detect(path):
    """Return usable, normalized ArcFace embeddings from an image file."""
    return detect_image(_read_image(path))


def _key():
    import keyring
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    encoded = keyring.get_password(_KEY_SERVICE, _KEY_USER)
    if not encoded:
        raw = AESGCM.generate_key(bit_length=256)
        encoded = base64.urlsafe_b64encode(raw).decode("ascii")
        keyring.set_password(_KEY_SERVICE, _KEY_USER, encoded)
    raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
    if len(raw) != 32:
        raise RuntimeError(
            "the face-template key in Credential Manager is invalid")
    return raw


def _encrypt_embedding(embedding):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    value = np.asarray(embedding, dtype="<f4")
    nonce = secrets.token_bytes(12)
    sealed = AESGCM(_key()).encrypt(nonce, value.tobytes(), _AAD)
    return "v1:" + base64.urlsafe_b64encode(nonce + sealed).decode("ascii")


def _decrypt_embedding(payload, dimension):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    version, separator, encoded = str(payload or "").partition(":")
    if separator != ":" or version != "v1":
        raise ValueError("unsupported face-template encryption version")
    packed = base64.urlsafe_b64decode(encoded.encode("ascii"))
    raw = AESGCM(_key()).decrypt(packed[:12], packed[12:], _AAD)
    vector = np.frombuffer(raw, dtype="<f4").copy()
    if vector.size != int(dimension):
        raise ValueError("face-template dimension mismatch")
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm <= 0:
        raise ValueError("invalid face-template vector")
    return vector / norm


def _signed_document(kind, payload):
    """Return a deterministic HMAC envelope without exposing biometric data."""
    body = {
        "kind": str(kind),
        "version": _BOOT_CACHE_VERSION,
        "payload": payload,
    }
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    body["hmac_sha256"] = hmac.new(
        _key(), b"vanta-boot-identity|" + canonical,
        hashlib.sha256).hexdigest()
    return body


def _restrict_boot_file(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name != "nt":
        return
    # Remove inherited ACLs and grant the current Windows identity full access.
    # Failure is non-fatal because the payload remains AES-GCM encrypted and
    # HMAC authenticated with a key held in Windows Credential Manager.
    try:
        who = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get(
            "USERNAME", "")
        who = who.strip("\\")
        if who:
            subprocess.run(
                ["icacls", path, "/inheritance:r", "/grant:r", f"{who}:F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def _write_signed(path, kind, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    document = _signed_document(kind, payload)
    temporary = path + f".{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        _restrict_boot_file(temporary)
        os.replace(temporary, path)
        _restrict_boot_file(path)
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass


def _read_signed(path, expected_kind):
    with open(path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    signature = str(document.pop("hmac_sha256", ""))
    if (document.get("kind") != expected_kind
            or int(document.get("version") or 0) != _BOOT_CACHE_VERSION
            or not signature):
        raise ValueError("invalid boot identity document")
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")
    expected = hmac.new(
        _key(), b"vanta-boot-identity|" + canonical,
        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("boot identity signature mismatch")
    return document.get("payload")


def _safe_boot_row(person, row):
    """The restricted subset permitted outside the graph."""
    return {
        "person": str(person or ""),
        "template_id": str(row.get("template_id") or ""),
        "ciphertext": str(row.get("ciphertext") or ""),
        "model": str(row.get("model") or ""),
        "embedding_dim": int(row.get("embedding_dim") or 0),
    }


def _read_boot_rows():
    payload = _read_signed(BOOT_CACHE_PATH, "primary-face-cache")
    if not isinstance(payload, dict) or not isinstance(
            payload.get("templates"), list):
        raise ValueError("invalid boot template payload")
    return list(payload["templates"])


def _write_boot_rows(rows):
    payload = {
        "primary_person": PRIMARY_PERSON,
        "updated_at": time.time(),
        "templates": list(rows),
    }
    _write_signed(BOOT_CACHE_PATH, "primary-face-cache", payload)


def _upsert_boot_template(person, row):
    if not _is_primary(person):
        return
    safe = _safe_boot_row(person, row)
    try:
        rows = _read_boot_rows()
    except Exception:
        rows = []
    rows = [item for item in rows
            if item.get("template_id") != safe["template_id"]]
    rows.append(safe)
    _write_boot_rows(rows[-MAX_TEMPLATES_PER_PERSON:])


def sync_boot_templates(rows=None):
    """Reconcile the restricted pre-Neo4j cache from graph ciphertext."""
    if rows is None:
        from memory.neo4j_store import store
        rows = store.active_face_templates()
    selected = []
    for row in rows or ():
        person = str(row.get("person") or "")
        if not _is_primary(person):
            continue
        safe = _safe_boot_row(person, row)
        if (safe["template_id"] and safe["ciphertext"]
                and safe["model"] and safe["embedding_dim"] > 0):
            selected.append(safe)
    _write_boot_rows(selected[-MAX_TEMPLATES_PER_PERSON:])
    return len(selected)


def _boot_templates():
    loaded = []
    for row in _read_boot_rows():
        if (not _is_primary(row.get("person"))
                or row.get("model") != MODEL_NAME):
            continue
        vector = _decrypt_embedding(
            row.get("ciphertext"), row.get("embedding_dim"))
        loaded.append((PRIMARY_PERSON, str(row.get("template_id") or ""),
                       vector))
    if not loaded:
        raise RuntimeError(
            f"no {MODEL_NAME} primary face template is available for startup")
    return tuple(loaded)


def boot_cache_status():
    try:
        templates = _boot_templates()
        return {"ready": True, "templates": len(templates),
                "model": MODEL_NAME, "error": ""}
    except Exception as exc:
        return {"ready": False, "templates": 0, "model": MODEL_NAME,
                "error": f"{type(exc).__name__}: {exc}"[:300]}


def issue_startup_ticket():
    payload = {"issued_at": time.time(), "nonce": secrets.token_hex(24)}
    _write_signed(BOOT_TICKET_PATH, "startup-ticket", payload)
    return True


def discard_startup_ticket():
    try:
        os.remove(BOOT_TICKET_PATH)
    except OSError:
        pass


def consume_startup_ticket(max_age=_BOOT_TICKET_TTL_SECONDS):
    """Accept one recent signed preflight result, deleting it on every read."""
    try:
        payload = _read_signed(BOOT_TICKET_PATH, "startup-ticket")
    except Exception:
        payload = None
    finally:
        discard_startup_ticket()
    if not isinstance(payload, dict):
        return False
    try:
        age = time.time() - float(payload.get("issued_at"))
    except (TypeError, ValueError):
        return False
    return 0.0 <= age <= max(1.0, float(max_age)) and bool(
        payload.get("nonce"))


def _invalidate_cache():
    global _template_cache
    with _template_lock:
        _template_cache = (0.0, ())


def _templates(force=False):
    global _template_cache
    with _template_lock:
        stamped, cached = _template_cache
        if cached and not force and time.monotonic() - stamped < 30.0:
            return cached
        from memory.neo4j_store import store
        loaded = []
        for row in store.active_face_templates():
            if row.get("model") != MODEL_NAME:
                continue
            try:
                vector = _decrypt_embedding(
                    row.get("ciphertext"), row.get("embedding_dim"))
            except Exception as exc:
                # Never print ciphertext or vector values.
                print(f"[Face Identity] Skipped unreadable template "
                      f"{row.get('template_id', '?')}: {type(exc).__name__}.")
                continue
            loaded.append((str(row.get("person") or ""),
                           str(row.get("template_id") or ""), vector))
        _template_cache = (time.monotonic(), tuple(loaded))
        return _template_cache[1]


def _score_people(embedding, templates):
    best_by_person = {}
    for person, template_id, stored in templates:
        score = float(np.dot(embedding, stored))
        previous = best_by_person.get(person)
        if previous is None or score > previous[0]:
            best_by_person[person] = (score, template_id)
    return sorted(((score, person, template_id)
                   for person, (score, template_id)
                   in best_by_person.items()), reverse=True)


def identify(path):
    """Identify every usable face without forcing a best-match guess."""
    faces = detect(path)
    templates = _templates()
    matches = []
    for face in faces:
        ranked = _score_people(face.embedding, templates)
        top_score, top_person = ((ranked[0][0], ranked[0][1])
                                 if ranked else (0.0, ""))
        runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
        if top_score < MATCH_THRESHOLD:
            status_name, top_person = "unknown", ""
        elif len(ranked) > 1 and top_score - runner_up < AMBIGUITY_MARGIN:
            status_name, top_person = "ambiguous", ""
        else:
            status_name = "recognized"
        matches.append(FaceMatch(
            status=status_name, bbox=face.bbox,
            normalized_box=face.normalized_box,
            detection_score=face.detection_score, person=top_person,
            similarity=round(top_score, 4),
            runner_up=round(runner_up, 4)))
    return tuple(matches)


def _verify_primary_faces(faces):
    """Score already-detected faces against primary-person templates."""
    primary_templates = tuple(
        row for row in _templates() if _is_primary(row[0]))
    best_similarity = 0.0
    best_detection = 0.0
    best_box = ()
    for face in faces:
        ranked = _score_people(face.embedding, primary_templates)
        similarity = ranked[0][0] if ranked else 0.0
        if similarity > best_similarity:
            best_similarity = similarity
            best_detection = face.detection_score
            best_box = face.normalized_box
    return {
        "face_found": bool(faces),
        "matched": bool(best_similarity >= MATCH_THRESHOLD),
        "similarity": round(float(best_similarity), 4),
        "detection_score": round(float(best_detection), 4),
        "box": tuple(best_box),
    }


def verify_primary(path):
    """Return the primary-only match and alignment evidence for a file."""
    return _verify_primary_faces(detect(path))


def verify_primary_image(image):
    """Return primary match evidence for an in-memory BGR camera frame."""
    return _verify_primary_faces(detect_image(image))


def verify_primary_image_boot(image):
    """Verify the owner before Neo4j or the assistant runtime is imported."""
    faces = detect_image(image)
    primary_templates = _boot_templates()
    best_similarity = 0.0
    best_detection = 0.0
    best_box = ()
    for face in faces:
        ranked = _score_people(face.embedding, primary_templates)
        similarity = ranked[0][0] if ranked else 0.0
        if similarity > best_similarity:
            best_similarity = similarity
            best_detection = face.detection_score
            best_box = face.normalized_box
    return {
        "face_found": bool(faces),
        "matched": bool(best_similarity >= MATCH_THRESHOLD),
        "similarity": round(float(best_similarity), 4),
        "detection_score": round(float(best_detection), 4),
        "box": tuple(best_box),
    }


def unload():
    """Release model references; process shutdown completes native cleanup."""
    global _app
    with _app_lock:
        _app = None
    try:
        import gc
        gc.collect()
    except Exception:
        pass


def _is_primary(name):
    return " ".join(str(name or "").split()).casefold() == PRIMARY_PERSON.casefold()


def _primary_rows():
    from memory.neo4j_store import store
    return [row for row in store.active_face_templates()
            if _is_primary(row.get("person"))]


def primary_enrolled():
    """Is the owner enrolled under the model in use?

    Until he is, nobody can be recognised as him, so nobody can be told apart
    from him either: treating every visitor as a guest then locks him out of his
    own assistant, including out of enrolling. If the templates cannot be read
    the answer is True, which keeps the stricter behaviour.
    """
    try:
        return any(_is_primary(person) for person, _id, _vec in _templates())
    except Exception:
        return True


def migration_notice():
    """A message when the owner is enrolled only under an earlier model."""
    try:
        rows = _primary_rows()
    except Exception:
        return ""
    if rows and not any(row.get("model") == MODEL_NAME for row in rows):
        return (f"{PRIMARY_PERSON} is enrolled under an earlier model, so no one "
                f"is recognised yet. Say \"remember my face\" once, facing the "
                f"camera in good light, to re-enrol with {MODEL_NAME}.")
    return ""


def _phone_status(name, phone=""):
    """Link a number to the person if one was given; say whether one is on file.

    The face template hangs off the same graph node as the contact, so a number
    already synced from Google Contacts is linked the moment the face is saved.
    This never returns the number itself: it ends up in a receipt on screen.
    """
    import re
    cleaned = re.sub(r"[^\d+]", "", str(phone or ""))
    if phone and len(re.sub(r"\D", "", cleaned)) < 7:
        return "The number given did not look like a phone number, so it was not linked."
    try:
        from memory.neo4j_store import store
        if cleaned:
            store.add_fact(name, "HAS_PHONE", cleaned, source_id="face_enrollment")
        rows = store.facts_for(name, predicate="HAS_PHONE", limit=1)
    except Exception as exc:
        print(f"[Face Identity] Could not check the phone link: {type(exc).__name__}.")
        return ""
    if rows:
        return "A phone number is linked to this person."
    return ("No phone number is on file for this person yet; give me it and "
            "I will link it.")


def resolve_person_node(name):
    """(node name, note, error) -- the graph node a face should hang on.

    A person who is already in the graph (a synced contact with a phone number,
    say) keeps that node, so the face, the number and everything else known
    about them are one thing. Failing that, a single contact whose name contains
    the given one is used and the note says so; several candidates are not
    guessed between; nobody found means a new Person node.
    """
    try:
        from memory.neo4j_store import store
        rows = store.find_person_nodes(name)
    except Exception as exc:
        print(f"[Face Identity] Could not look up the person node: {type(exc).__name__}.")
        return name, "", ""
    exact = [row for row in rows if row.get("exact")]
    if exact:
        return exact[0]["name"], "", ""
    if len(rows) == 1:
        return rows[0]["name"], f" Linked to the existing contact \"{rows[0]['name']}\".", ""
    if rows:
        options = ", ".join(f"\"{row['name']}\"" for row in rows[:5])
        return None, "", (f"[Error] More than one contact could be {name}: {options}. "
                          "Say which one, using its exact name.")
    return name, "", ""


def enroll_person(person, image_path, phone=""):
    """Enroll exactly one visible face under an existing/new Person node.

    Only one face may be in the frame, so nobody else is saved by standing
    beside the person being enrolled. The face is attached to the person's
    existing graph node when there is one (see resolve_person_node).
    """
    name = " ".join(str(person or "").strip().split())
    if not name:
        return "[Error] REMEMBER_FACE needs a person's name."
    faces = detect(image_path)
    if not faces:
        return ("[Error] No sufficiently clear face was found. Face the "
                "camera in even light and move closer before trying again.")
    if len(faces) != 1:
        return (f"[Error] Enrollment needs exactly one clear face; found "
                f"{len(faces)}. Other people should leave the frame.")
    face = faces[0]
    existing = _templates(force=True)
    ranked = _score_people(face.embedding, existing)
    if ranked and ranked[0][0] >= max(MATCH_THRESHOLD + 0.10, 0.60):
        matched_person = ranked[0][1]
        if matched_person.casefold() != name.casefold():
            return (f"[Error] This face already strongly matches "
                    f"{matched_person} ({ranked[0][0]:.2f}); it was not "
                    f"linked to a second identity.")
    node, link_note, problem = resolve_person_node(name)
    if problem:
        return problem
    if node != name:
        print(f"[Face Identity] Linking {name!r} to the existing node {node!r}.")
        name = node
    from memory.neo4j_store import store
    current = [row for row in store.known_face_people()
               if str(row.get("person") or "").casefold() == name.casefold()]
    if (current and int(current[0].get("templates") or 0)
            >= MAX_TEMPLATES_PER_PERSON):
        return (f"[Error] {name} already has {MAX_TEMPLATES_PER_PERSON} face "
                "templates. Forget that face before replacing them.")
    raw = np.asarray(face.embedding, dtype="<f4").tobytes()
    template_id = "face_" + hashlib.sha256(
        name.casefold().encode("utf-8") + b"\x00" + raw).hexdigest()[:32]
    with open(image_path, "rb") as source:
        image_hash = hashlib.sha256(source.read()).hexdigest()
    template_row = {
        "template_id": template_id,
        "ciphertext": _encrypt_embedding(face.embedding),
        "model": MODEL_NAME, "detector": DETECTOR_NAME,
        "embedding_dim": int(face.embedding.size),
        "image_hash": image_hash, "det_score": face.detection_score,
        "quality": face.quality,
    }
    store.upsert_face_template(name, template_row)
    if _is_primary(name):
        try:
            _upsert_boot_template(name, template_row)
        except Exception as exc:
            print(f"[Face Identity] Startup cache refresh failed: "
                  f"{type(exc).__name__}.")
    _invalidate_cache()
    note = _phone_status(name, phone)
    return (f"[Success] Remembered {name}'s face as encrypted template "
            f"id={template_id}; model={MODEL_NAME}; "
            f"quality={face.quality:.2f}.{link_note}"
            + (f" {note}" if note else ""))


def forget_person(person):
    name = " ".join(str(person or "").strip().split())
    if not name:
        return "[Error] FORGET_FACE needs a person's name."
    from memory.neo4j_store import store
    removed = store.delete_face_templates(name)
    if _is_primary(name):
        try:
            _write_boot_rows([])
        except Exception as exc:
            print(f"[Face Identity] Startup cache removal failed: "
                  f"{type(exc).__name__}.")
    _invalidate_cache()
    if not removed:
        return f"[Success] Removed 0 face templates for {name}; none existed."
    return (f"[Success] Permanently removed {removed} face template(s) "
            f"for {name}.")


def _details_for(name):
    """What the graph holds about a person that is worth saying aloud."""
    try:
        from memory.neo4j_store import store
        rows = store.facts_for(name, limit=40)
    except Exception as exc:
        print(f"[Face Identity] Could not read details: {type(exc).__name__}.")
        return {"phones": [], "emails": [], "facts": []}
    key = " ".join(str(name or "").casefold().split())
    phones, emails, facts = [], [], []
    for row in rows:
        subject = " ".join(str(row.get("subject") or "").casefold().split())
        predicate = str(row.get("predicate") or "").upper()
        value = str(row.get("object") or "")
        if subject != key:
            continue
        if predicate == "HAS_PHONE":
            phones.append(value)
        elif predicate in ("PRIMARY_EMAIL", "HAS_EMAIL"):
            emails.append(value)
        elif predicate not in _CONTEXT_PII and len(facts) < 4:
            facts.append(f"{predicate.replace('_', ' ').lower()} {value}")
    return {"phones": list(dict.fromkeys(phones)), "emails": list(dict.fromkeys(emails)),
            "facts": facts}


def who_is_this(image_path):
    """Name every face in the frame, with the phone number linked to it.

    Recognised faces come back with their graph node's details. An unrecognised
    face is reported as unknown, never guessed, with how to save it.
    """
    matches = identify(image_path)
    if not matches:
        return ("[Error] I could not see a clear face. Face the camera in even "
                "light and move a little closer.")
    lines = []
    for index, match in enumerate(matches, 1):
        label = f"Face {index}: " if len(matches) > 1 else ""
        if match.recognized:
            details = _details_for(match.person)
            parts = [f"{label}{match.person} (match {match.similarity:.2f})"]
            if details["phones"]:
                parts.append("phone " + ", ".join(details["phones"][:2]))
            else:
                parts.append("no phone number on file")
            if details["emails"]:
                parts.append("email " + details["emails"][0])
            if details["facts"]:
                parts.append("also: " + "; ".join(details["facts"]))
            lines.append(" - ".join(parts))
        elif match.status == "ambiguous":
            lines.append(f"{label}Someone I can't tell apart from two people I know "
                         f"(best match {match.similarity:.2f}); not naming either.")
        else:
            lines.append(f"{label}A face I don't know (best match {match.similarity:.2f}). "
                         "Tell me their name and I'll remember it.")
    return "[Success] " + "\n".join(lines)


def known_people():
    from memory.neo4j_store import store
    rows = store.known_face_people()
    if not rows:
        return "[Success] Known faces: 0."
    details = ", ".join(
        f"{row['person']} ({int(row.get('templates') or 0)} template(s))"
        for row in rows)
    return f"[Success] Known faces: {len(rows)} person(s): {details}."


_CONTEXT_PII = {"HAS_PHONE", "PRIMARY_EMAIL", "MENTIONS_EMAIL_ADDRESS",
                "HAS_BANK_ACCOUNT", "HAS_ADDRESS"}


def _person_knowledge(person, limit=6):
    """Small safe subset relevant to context; never restricted PII."""
    try:
        from memory.neo4j_store import store
        rows = store.facts_for(person, limit=20)
    except Exception as exc:
        print(f"[Face Identity] Person context unavailable: "
              f"{type(exc).__name__}.")
        return ()
    selected = []
    for row in rows:
        if str(row.get("sensitivity") or "private").lower() not in {
                "public", "private"}:
            continue
        if str(row.get("review_status") or "accepted").lower() not in {
                "accepted", "reviewed"}:
            continue
        predicate = str(row.get("predicate") or "RELATED_TO").upper()
        if predicate in _CONTEXT_PII:
            continue
        selected.append((str(row.get("subject") or ""), predicate,
                         str(row.get("object") or "")))
        if len(selected) >= limit:
            break
    return tuple(selected)


def context_for_matches(matches):
    """Grounded prompt context; no embeddings or biometric identifiers."""
    if not matches:
        return ("LOCAL FACE IDENTITY: no sufficiently clear face detected. "
                "Do not infer that nobody is present.")
    lines = [
        "LOCAL FACE IDENTITY (SCRFD + ArcFace; probabilistic evidence only):"
    ]
    seen = set()
    for index, match in enumerate(matches, 1):
        if match.recognized:
            lines.append(
                f"- Face {index}: recognized as {match.person}; "
                f"similarity={match.similarity:.2f}; "
                f"box={match.normalized_box}.")
            if match.person not in seen:
                facts = _person_knowledge(match.person)
                if facts:
                    lines.append(
                        f"  Current graph knowledge about {match.person}:")
                    for subject, predicate, obj in facts:
                        lines.append(
                            f"  * {subject} "
                            f"{predicate.replace('_', ' ').lower()} {obj}")
                seen.add(match.person)
        elif match.status == "ambiguous":
            lines.append(
                f"- Face {index}: unknown (ambiguous match); "
                f"box={match.normalized_box}.")
        else:
            lines.append(
                f"- Face {index}: unknown; box={match.normalized_box}.")
    lines.append(
        "Never use a face match to authorize an action, reveal restricted "
        "information, or claim certainty. Do not guess an unknown face.")
    return "\n".join(lines)
