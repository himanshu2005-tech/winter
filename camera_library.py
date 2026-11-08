"""Local camera archive with graph metadata and bounded retention."""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import uuid


RETENTION_DAYS = 30


def root(base_dir):
    return os.path.abspath(os.path.join(base_dir, "captures", "camera"))


def new_path(base_dir, extension="jpg"):
    now = dt.datetime.now().astimezone()
    folder = os.path.join(root(base_dir), now.strftime("%Y"), now.strftime("%m"), now.strftime("%d"))
    os.makedirs(folder, exist_ok=True)
    stamp = now.strftime("%Y%m%d_%H%M%S_%f")
    return os.path.join(
        folder, f"camera_{stamp}_{uuid.uuid4().hex[:8]}.{extension.lstrip('.')}")


def record(base_dir, path, *, width, height, device_index, mode, reason):
    absolute = os.path.abspath(path)
    archive_root = root(base_dir)
    if os.path.commonpath((archive_root, absolute)) != archive_root:
        raise ValueError("camera capture is outside the managed archive")
    with open(absolute, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    capture_id = f"camera_{digest[:24]}"
    try:
        from memory.neo4j_store import store
        store.query(
            """
            MERGE (h:Entity {key:'himanshu'})
            ON CREATE SET h.name='Himanshu',h.entity_type='Person',h.created_at=datetime()
            MERGE (c:CameraCapture {id:$id})
            ON CREATE SET c.created_at=datetime()
            SET c.path=$path,c.sha256=$sha,c.width=$width,c.height=$height,
                c.device_index=$device,c.mode=$mode,c.reason=$reason,
                c.captured_at=datetime(),c.expires_at=datetime()+duration('P30D'),
                c.sensitivity='restricted',c.active=true
            MERGE (h)-[r:HAS_CAMERA_CAPTURE]->(c)
            SET r.sensitivity='restricted',r.updated_at=datetime()
            """, id=capture_id, path=absolute, sha=digest, width=int(width),
            height=int(height), device=int(device_index), mode=str(mode or "look"),
            reason=" ".join(str(reason or "").split())[:300])
    except Exception as exc:
        # The image is still safely on disk; graph downtime must not turn a
        # successful capture into a false failure.
        print(f"[Camera Library] Graph metadata deferred: {exc}")
    return capture_id


def purge_expired(base_dir, now=None):
    archive_root = root(base_dir)
    cutoff = (now or dt.datetime.now().astimezone()).timestamp() - RETENTION_DAYS * 86400
    removed = []
    if os.path.isdir(archive_root):
        for current, _dirs, files in os.walk(archive_root):
            for name in files:
                path = os.path.abspath(os.path.join(current, name))
                if os.path.commonpath((archive_root, path)) != archive_root:
                    continue
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                        removed.append(path)
                except OSError as exc:
                    print(f"[Camera Library] Could not expire {path}: {exc}")
    try:
        from memory.neo4j_store import store
        store.query(
            """
            MATCH (c:CameraCapture)
            WHERE c.expires_at < datetime() OR c.path IN $paths
            SET c.active=false,c.deleted_at=datetime(),c.path=NULL
            """, paths=removed)
    except Exception as exc:
        print(f"[Camera Library] Retention metadata deferred: {exc}")
    return removed


def list_recent(limit=20):
    from memory.neo4j_store import store
    return store.query(
        """
        MATCH (:Entity {key:'himanshu'})-[:HAS_CAMERA_CAPTURE]->(c:CameraCapture)
        WHERE coalesce(c.active,true)=true AND c.expires_at >= datetime()
        RETURN c.id AS id,c.path AS path,toString(c.captured_at) AS captured_at,
               c.width AS width,c.height AS height,c.reason AS reason
        ORDER BY c.captured_at DESC LIMIT $limit
        """, limit=max(1, min(int(limit), 100)))


def resolve(identifier="latest"):
    rows = list_recent(100)
    wanted = str(identifier or "latest").strip().lower()
    if wanted in {"", "latest", "last", "recent", "newest"}:
        row = rows[0] if rows else None
    else:
        row = next((item for item in rows if str(item.get("id", "")).lower() == wanted), None)
    if not row or not row.get("path") or not os.path.exists(row["path"]):
        return None
    return row


def describe(limit=20):
    rows = list_recent(limit)
    if not rows:
        return "No retained camera captures."
    return "\n".join(
        f"{row['id']} | {row['captured_at']} | {row['width']}x{row['height']} | {row.get('reason') or 'camera capture'}"
        for row in rows)
