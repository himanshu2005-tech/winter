# -*- coding: utf-8 -*-
"""Google Drive provider for VANTA.

The provider returns compact receipts and never decides whether an operation is
authorized.  That decision belongs to ``autonomy.py`` and ``actions.py``.
Remote IDs, rather than names, are used for mutation so duplicate filenames
cannot make VANTA alter the wrong object.
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from providers.google_api import get_credentials

FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_PREFIX = "application/vnd.google-apps."
EXPORTS = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}
FIELDS = "id,name,mimeType,size,modifiedTime,createdTime,parents,webViewLink,md5Checksum,trashed,owners(displayName,emailAddress),capabilities(canDownload,canEdit,canDelete)"


def _service(base_dir):
    return build("drive", "v3", credentials=get_credentials(base_dir),
                 cache_discovery=False)


def _escape_query(value):
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def _item(raw):
    return {key: raw.get(key) for key in (
        "id", "name", "mimeType", "size", "modifiedTime", "createdTime",
        "parents", "webViewLink", "md5Checksum", "trashed", "owners",
        "capabilities") if raw.get(key) is not None}


def _format(items, heading="Google Drive files"):
    if not items:
        return f"{heading}: none found."
    lines = [f"{heading} ({len(items)}):"]
    for row in items:
        kind = "folder" if row.get("mimeType") == FOLDER_MIME else "file"
        size = f", {int(row['size']):,} bytes" if str(row.get("size", "")).isdigit() else ""
        lines.append(f"- {row.get('name', 'Untitled')} [{kind}; id={row.get('id', '')}{size}]"
                     f" modified={row.get('modifiedTime', 'unknown')}")
    return "\n".join(lines)


def list_files(base_dir, folder_id="root", limit=50, page_token=None):
    limit = max(1, min(int(limit or 50), 200))
    query = f"'{_escape_query(folder_id or 'root')}' in parents and trashed = false"
    result = _service(base_dir).files().list(
        q=query, pageSize=limit, pageToken=page_token or None,
        fields=f"nextPageToken,files({FIELDS})",
        orderBy="folder,name_natural", spaces="drive",
    ).execute()
    rows = [_item(value) for value in result.get("files", [])]
    return {"files": rows, "next_page_token": result.get("nextPageToken"),
            "text": _format(rows, "Google Drive folder")}


def search_files(base_dir, query, limit=30, mime_type=None, folder_id=None):
    if not str(query or "").strip():
        raise ValueError("A non-empty Drive search query is required.")
    clauses = [f"name contains '{_escape_query(query.strip())}'", "trashed = false"]
    if mime_type:
        clauses.append(f"mimeType = '{_escape_query(mime_type)}'")
    if folder_id:
        clauses.append(f"'{_escape_query(folder_id)}' in parents")
    result = _service(base_dir).files().list(
        q=" and ".join(clauses), pageSize=max(1, min(int(limit or 30), 100)),
        fields=f"files({FIELDS})", orderBy="modifiedTime desc", spaces="drive",
    ).execute()
    rows = [_item(value) for value in result.get("files", [])]
    return {"files": rows, "text": _format(rows, f"Drive search for {query!r}")}


def file_info(base_dir, file_id):
    raw = _service(base_dir).files().get(fileId=file_id, fields=FIELDS,
                                         supportsAllDrives=True).execute()
    row = _item(raw)
    return {"file": row, "text": _format([row], "Google Drive item")}


def _safe_destination(base_dir, destination, name):
    root = (Path(base_dir) / "vault" / "downloads" / "google_drive").resolve()
    root.mkdir(parents=True, exist_ok=True)
    candidate = Path(destination).expanduser() if destination else root / name
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    # Explicit absolute paths are allowed for user-requested downloads; a
    # relative path is always confined to the managed Drive download folder.
    if not Path(destination or "").is_absolute() and root not in candidate.parents and candidate != root:
        raise ValueError("Download destination escapes the managed Drive folder.")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def download_file(base_dir, file_id, destination="", overwrite=False):
    service = _service(base_dir)
    meta = service.files().get(fileId=file_id, fields=FIELDS,
                               supportsAllDrives=True).execute()
    mime = meta.get("mimeType", "")
    if mime == FOLDER_MIME:
        raise ValueError("Folders cannot be downloaded as one file. List the folder and download its files.")
    name = meta.get("name") or file_id
    if mime.startswith(GOOGLE_PREFIX):
        if mime not in EXPORTS:
            raise ValueError(f"This Google-native type cannot be exported automatically: {mime}")
        export_mime, suffix = EXPORTS[mime]
        if not name.lower().endswith(suffix):
            name += suffix
        request = service.files().export_media(fileId=file_id, mimeType=export_mime)
    else:
        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    target = _safe_destination(base_dir, destination, name)
    if target.exists() and not overwrite:
        raise FileExistsError(f"{target} already exists; set overwrite=true to replace it.")
    temporary = target.with_name(target.name + ".part")
    try:
        with temporary.open("wb") as handle:
            downloader = MediaIoBaseDownload(handle, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return f"[Success] Downloaded {meta.get('name', file_id)} to {target}."


def upload_file(base_dir, path, parent_id="root", name="", mime_type=""):
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Local upload file does not exist: {source}")
    guessed = mime_type or mimetypes.guess_type(str(source))[0] or "application/octet-stream"
    media = MediaFileUpload(str(source), mimetype=guessed, resumable=True,
                            chunksize=8 * 1024 * 1024)
    body = {"name": name or source.name, "parents": [parent_id or "root"]}
    created = _service(base_dir).files().create(
        body=body, media_body=media, fields=FIELDS, supportsAllDrives=True).execute()
    return f"[Success] Uploaded {source.name} as {created.get('name')} [id={created.get('id')}]."


def create_folder(base_dir, name, parent_id="root"):
    if not str(name or "").strip():
        raise ValueError("Folder name is required.")
    created = _service(base_dir).files().create(
        body={"name": name.strip(), "mimeType": FOLDER_MIME,
              "parents": [parent_id or "root"]}, fields=FIELDS,
        supportsAllDrives=True).execute()
    return f"[Success] Created Drive folder {created.get('name')} [id={created.get('id')}]."


def rename_item(base_dir, file_id, name):
    updated = _service(base_dir).files().update(
        fileId=file_id, body={"name": name}, fields=FIELDS,
        supportsAllDrives=True).execute()
    return f"[Success] Renamed Drive item to {updated.get('name')} [id={file_id}]."


def move_item(base_dir, file_id, parent_id):
    service = _service(base_dir)
    current = service.files().get(fileId=file_id, fields="id,name,parents",
                                  supportsAllDrives=True).execute()
    old = ",".join(current.get("parents", []))
    updated = service.files().update(
        fileId=file_id, addParents=parent_id, removeParents=old or None,
        fields=FIELDS, supportsAllDrives=True).execute()
    return f"[Success] Moved {updated.get('name')} to Drive folder {parent_id}."


def copy_item(base_dir, file_id, name="", parent_id=""):
    body = {}
    if name:
        body["name"] = name
    if parent_id:
        body["parents"] = [parent_id]
    created = _service(base_dir).files().copy(
        fileId=file_id, body=body, fields=FIELDS,
        supportsAllDrives=True).execute()
    return f"[Success] Copied Drive item as {created.get('name')} [id={created.get('id')}]."


def trash_item(base_dir, file_id):
    updated = _service(base_dir).files().update(
        fileId=file_id, body={"trashed": True}, fields="id,name,trashed",
        supportsAllDrives=True).execute()
    return f"[Success] Moved {updated.get('name')} to Google Drive trash."


def share_item(base_dir, file_id, email, role="reader"):
    role = str(role or "reader").lower()
    if role not in {"reader", "commenter", "writer"}:
        raise ValueError("Drive sharing role must be reader, commenter, or writer.")
    permission = _service(base_dir).permissions().create(
        fileId=file_id, body={"type": "user", "role": role,
                              "emailAddress": email}, sendNotificationEmail=True,
        fields="id", supportsAllDrives=True).execute()
    return f"[Success] Shared Drive item with {email} as {role} [permission={permission.get('id')}]."


def restore_item(base_dir, file_id):
    """Take an item back out of the trash (the undo for DRIVE_TRASH)."""
    updated = _service(base_dir).files().update(
        fileId=file_id, body={"trashed": False}, fields="id,name,trashed",
        supportsAllDrives=True).execute()
    return f"[Success] Restored {updated.get('name')} from the Drive trash [id={updated.get('id')}]."


def recent_files(base_dir, limit=15, days=14, mime_type=None):
    """What changed lately, newest first -- "what was I working on?"."""
    import datetime
    limit = max(1, min(int(limit or 15), 50))
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=max(1, int(days or 14)))).strftime("%Y-%m-%dT%H:%M:%SZ")
    clauses = ["trashed = false", f"modifiedTime > '{since}'",
               f"mimeType != '{FOLDER_MIME}'"]
    if mime_type:
        clauses.append(f"mimeType = '{_escape_query(mime_type)}'")
    response = _service(base_dir).files().list(
        q=" and ".join(clauses), orderBy="modifiedTime desc", pageSize=limit,
        fields=f"files({FIELDS})", supportsAllDrives=True,
        includeItemsFromAllDrives=True).execute()
    rows = [_item(raw) for raw in response.get("files", [])]
    return {"files": rows,
            "text": _format(rows, f"Drive files changed in the last {int(days or 14)} days")}


#: Text a person can be read aloud: Google Docs/Slides export as plain text,
#: Sheets as CSV. Binary formats (PDF, images, Office) are not read here --
#: DRIVE_DOWNLOAD fetches them and the usual file readers take over.
READ_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}
READ_LIMIT_BYTES = 2_000_000
READ_LIMIT_CHARS = 12_000


def read_file(base_dir, file_id, max_chars=READ_LIMIT_CHARS):
    """The text of a Drive document, without saving it to disk.

    Reading is what makes "summarise my project notes" possible; before this
    VANTA could only list names and download files it then could not open.
    The text is trimmed for the model, with the true size reported so a long
    document is never presented as if the excerpt were all of it.
    """
    service = _service(base_dir)
    meta = service.files().get(fileId=file_id, fields="id,name,mimeType,size",
                               supportsAllDrives=True).execute()
    mime = meta.get("mimeType", "")
    if mime == FOLDER_MIME:
        return {"text": "[Error] That is a folder; use DRIVE_LIST to see inside it."}
    if mime in READ_EXPORTS:
        request = service.files().export_media(fileId=file_id, mimeType=READ_EXPORTS[mime])
    elif mime.startswith("text/") or mime in {"application/json", "application/xml"}:
        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    else:
        return {"text": (f"[Error] {meta.get('name')} is a {mime} file, which cannot "
                         f"be read as text. Use DRIVE_DOWNLOAD to save it instead.")}
    if str(meta.get("size", "")).isdigit() and int(meta["size"]) > READ_LIMIT_BYTES:
        return {"text": (f"[Error] {meta.get('name')} is {int(meta['size']):,} bytes, "
                         f"too large to read inline. Use DRIVE_DOWNLOAD.")}
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request, chunksize=1024 * 1024)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
        if buffer.tell() > READ_LIMIT_BYTES:
            break
    text = buffer.getvalue().decode("utf-8", errors="replace").lstrip("﻿")
    total = len(text)
    limit = max(500, int(max_chars or READ_LIMIT_CHARS))
    shown = text[:limit]
    note = f" (showing the first {limit:,} of {total:,} characters)" if total > limit else ""
    return {"name": meta.get("name"), "characters": total,
            "text": f"Contents of {meta.get('name')} [id={file_id}]{note}:\n{shown}"}
