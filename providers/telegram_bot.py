import time
import os
import requests
import json
import threading
import hashlib
import mimetypes
import re
from datetime import datetime

import crypto_env

_TOKEN = None
_LAST_UPDATE_ID = 0
_ACTIVE_CHAT_ID = None
_OWNER_CHAT_ID = None
_RUNNING = False
_UPDATE_OFFSET_FILE = None

# Every send opened its own TLS connection and had no retry, so a reset by the
# far end -- ConnectionResetError(10054), which Telegram does routinely on a
# long-lived NAT -- lost the message outright. Observed on every single reply
# in a real session: "[Telegram] Failed to send message: ('Connection
# aborted.', ConnectionResetError(10054 ...))" after the answer had already
# been spoken, so the phone got nothing and nothing said so.
#
# One pooled session with a retry policy fixes both halves: the pool keeps the
# connection warm, and a reset is retried instead of dropped. Retrying a send
# is safe here because Telegram's sendMessage is not transactional -- the
# failure mode of a retry is a duplicate message, which is strictly better
# than a lost one.
_SESSION_LOCK = threading.Lock()
_SESSION = None
SEND_ATTEMPTS = 3
_FILE_INDEX_LOCK = threading.Lock()
DEFAULT_MAX_FILE_MB = 50


def _public_error(exc):
    """Format a network error without ever echoing the bot token."""
    text = str(exc)
    if _TOKEN:
        text = text.replace(str(_TOKEN), "[redacted]")
    return text[:500]


def _session():
    """A pooled requests session with connection-level retries."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None:
            return _SESSION
        session = requests.Session()
        try:
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            retry = Retry(
                total=SEND_ATTEMPTS, connect=SEND_ATTEMPTS,
                read=SEND_ATTEMPTS, status=2,
                backoff_factor=0.6,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["GET", "POST"]),
                raise_on_status=False)
            adapter = HTTPAdapter(max_retries=retry, pool_connections=4,
                                  pool_maxsize=8)
            session.mount("https://", adapter)
        except Exception as exc:
            print(f"[Telegram] Retry policy unavailable ({exc}); "
                  f"sends will not be retried.")
        _SESSION = session
        return _SESSION

def init_telegram(token):
    global _TOKEN
    _TOKEN = token


def _runtime_base_dir():
    import sys
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _safe_filename(value, fallback="telegram_file"):
    """Turn Telegram-supplied names into one harmless Windows filename."""
    name = os.path.basename(str(value or '').replace('\\', '/')).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).rstrip('. ')
    if not name:
        name = fallback
    stem = name.split('.', 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(
            r"(?:COM|LPT)[1-9]", stem or ""):
        name = "_" + name
    return name[:180]


def _received_root(base_dir=None):
    return os.path.join(os.path.abspath(base_dir or _runtime_base_dir()),
                        "vault", "telegram", "inbox")


def _index_path(base_dir=None):
    return os.path.join(os.path.abspath(base_dir or _runtime_base_dir()),
                        "vault", "telegram", "received.jsonl")


def _append_received(record, base_dir=None):
    path = _index_path(base_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with _FILE_INDEX_LOCK:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def list_received_files(base_dir=None, limit=10):
    """List Telegram files already saved locally, newest first."""
    path = _index_path(base_dir)
    try:
        with open(path, encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
    except FileNotFoundError:
        rows = []
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return f"[Error] Could not read the Telegram file index: {exc}"
    present = [row for row in reversed(rows)
               if os.path.isfile(str(row.get("path") or ""))]
    present = present[:max(1, min(int(limit or 10), 50))]
    if not present:
        return "[Success] 0 received Telegram files are available locally."
    lines = [f"[Success] {len(present)} recent Telegram file(s):"]
    for row in present:
        lines.append(
            f"- {row.get('name')} | {row.get('size', 0)} bytes | "
            f"{row.get('mime', 'application/octet-stream')} | "
            f"path={row.get('path')} | received={row.get('received_at')}")
    return "\n".join(lines)


def _load_update_offset(path):
    try:
        with open(path, "r", encoding="ascii") as handle:
            return max(0, int(handle.read().strip()))
    except Exception:
        return 0


def _save_update_offset(value):
    path = _UPDATE_OFFSET_FILE
    if not path:
        return
    temporary = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(temporary, "w", encoding="ascii") as handle:
            handle.write(str(max(0, int(value))))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass

def start_telegram_worker(token, input_queue):
    global _RUNNING, _TOKEN, _ACTIVE_CHAT_ID, _OWNER_CHAT_ID
    global _LAST_UPDATE_ID, _UPDATE_OFFSET_FILE
    if not token:
        return
    _TOKEN = token
    
    base_dir = _runtime_base_dir()
        
    chat_id_file = os.path.join(base_dir, "vault", "telegram_chat_id.txt")
    _UPDATE_OFFSET_FILE = os.path.join(
        base_dir, "vault", "telegram_update_offset.txt")
    _LAST_UPDATE_ID = max(
        _LAST_UPDATE_ID, _load_update_offset(_UPDATE_OFFSET_FILE))
    if os.path.exists(chat_id_file):
        try:
            with open(chat_id_file, "r") as f:
                _ACTIVE_CHAT_ID = int(f.read().strip())
                _OWNER_CHAT_ID = _ACTIVE_CHAT_ID
        except Exception:
            pass
            
    if _RUNNING:
        return
    _RUNNING = True
    threading.Thread(target=_poll_loop, args=(input_queue,), daemon=True).start()

def send_telegram_message(text):
    if not _TOKEN or not _ACTIVE_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
    payload = {
        "chat_id": _ACTIVE_CHAT_ID,
        "text": text
    }
    try:
        # 5 seconds was too tight for a cold connection on a slow link, and a
        # timeout here is indistinguishable from a reset: the message is gone
        # either way.
        response = _session().post(url, json=payload, timeout=15)
        if response.status_code != 200:
            print(f"[Telegram] Message rejected: {response.status_code} "
                  f"{response.text[:160]}")
            return False
        return True
    except Exception as e:
        print(f"[Telegram] Failed to send message after "
              f"{SEND_ATTEMPTS} attempts: {_public_error(e)}")
        return False


def send_telegram_document(file_path, caption=None):
    """Send one local file to the paired owner and return a provider receipt."""
    if not _TOKEN or not _ACTIVE_CHAT_ID:
        return "[Error] Telegram is not paired with an owner chat."
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(
        str(file_path or '').strip().strip('"'))))
    if not path or not os.path.isfile(path):
        return f"[Error] Telegram file does not exist or is not a file: {path}"
    size = os.path.getsize(path)
    max_bytes = max(1, int(os.getenv("VANTA_TELEGRAM_MAX_FILE_MB",
                                     DEFAULT_MAX_FILE_MB))) * 1024 * 1024
    if size > max_bytes:
        return (f"[Error] Telegram file is {size} bytes, above VANTA's "
                f"configured {max_bytes}-byte send limit: {path}")
    url = f"https://api.telegram.org/bot{_TOKEN}/sendDocument"
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    try:
        with open(path, "rb") as handle:
            payload = {"chat_id": _ACTIVE_CHAT_ID}
            if caption:
                payload["caption"] = str(caption)[:1024]
            files = {"document": (os.path.basename(path), handle, mime)}
            response = _session().post(url, data=payload, files=files,
                                       timeout=120)
        data = response.json() if response.content else {}
        if response.status_code != 200 or not data.get("ok"):
            description = str(data.get("description") or response.text)[:200]
            return (f"[Error] Telegram rejected {os.path.basename(path)}: "
                    f"HTTP {response.status_code}; {description}")
        message_id = (data.get("result") or {}).get("message_id", "")
        return (f"[Success] Telegram accepted file={os.path.basename(path)}; "
                f"size={size}; message_id={message_id}; recipient=paired owner")
    except Exception as exc:
        return (f"[Error] Could not send Telegram file "
                f"{os.path.basename(path)}: {_public_error(exc)}")


def download_telegram_file(file_id, file_name, mime_type="", base_dir=None,
                           update_id=None):
    """Download one Telegram attachment atomically into the private vault."""
    if not _TOKEN:
        return {"ok": False, "error": "Telegram is not initialized."}
    safe_name = _safe_filename(file_name, f"telegram_{file_id}")
    root = _received_root(base_dir)
    day_dir = os.path.join(root, datetime.now().strftime("%Y-%m-%d"))
    os.makedirs(day_dir, exist_ok=True)
    target = os.path.join(day_dir, safe_name)
    stem, suffix = os.path.splitext(target)
    number = 2
    while os.path.exists(target) or os.path.exists(target + ".part"):
        target = f"{stem}_{number}{suffix}"
        number += 1
    temporary = target + ".part"
    maximum = max(1, int(os.getenv("VANTA_TELEGRAM_MAX_FILE_MB",
                                   DEFAULT_MAX_FILE_MB))) * 1024 * 1024
    try:
        metadata_response = _session().get(
            f"https://api.telegram.org/bot{_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=20)
        metadata = metadata_response.json() if metadata_response.content else {}
        remote = (metadata.get("result") or {}).get("file_path")
        if metadata_response.status_code != 200 or not metadata.get("ok") or not remote:
            description = str(metadata.get("description") or
                              metadata_response.text)[:200]
            return {"ok": False,
                    "error": f"Telegram could not resolve the file: {description}"}
        file_response = _session().get(
            f"https://api.telegram.org/file/bot{_TOKEN}/{remote}",
            stream=True, timeout=120)
        if file_response.status_code != 200:
            return {"ok": False,
                    "error": f"Telegram file download returned HTTP {file_response.status_code}."}
        announced = int(file_response.headers.get("Content-Length") or 0)
        if announced > maximum:
            return {"ok": False,
                    "error": f"Telegram file is above the configured {maximum}-byte limit."}
        digest, size = hashlib.sha256(), 0
        with open(temporary, "wb") as handle:
            for chunk in file_response.iter_content(chunk_size=128 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > maximum:
                    raise ValueError(
                        f"Telegram file exceeded the configured {maximum}-byte limit")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        record = {
            "file_id": str(file_id), "update_id": update_id,
            "name": os.path.basename(target), "path": os.path.abspath(target),
            "mime": str(mime_type or mimetypes.guess_type(target)[0] or
                        "application/octet-stream"),
            "size": size, "sha256": digest.hexdigest(),
            "received_at": datetime.now().astimezone().isoformat(),
        }
        _append_received(record, base_dir)
        return {"ok": True, **record}
    except Exception as exc:
        try:
            os.remove(temporary)
        except OSError:
            pass
        return {"ok": False, "error": _public_error(exc)}


def _incoming_attachment(message):
    """Return normalized attachment metadata for a Telegram message."""
    document = message.get("document")
    if isinstance(document, dict) and document.get("file_id"):
        return {
            "file_id": document["file_id"],
            "name": document.get("file_name") or
                    f"document_{document.get('file_unique_id', '')}",
            "mime": document.get("mime_type") or "application/octet-stream",
        }
    photos = message.get("photo") or []
    if photos:
        photo = photos[-1]
        return {"file_id": photo.get("file_id"),
                "name": f"photo_{photo.get('file_unique_id', '')}.jpg",
                "mime": "image/jpeg"}
    for kind, default_ext, default_mime in (
            ("video", ".mp4", "video/mp4"),
            ("audio", ".mp3", "audio/mpeg"),
            ("voice", ".ogg", "audio/ogg")):
        item = message.get(kind)
        if isinstance(item, dict) and item.get("file_id"):
            return {"file_id": item["file_id"],
                    "name": item.get("file_name") or
                            f"{kind}_{item.get('file_unique_id', '')}{default_ext}",
                    "mime": item.get("mime_type") or default_mime}
    return None


def _handle_incoming_attachment(message, input_queue, base_dir, update_id=None):
    """Save and enqueue a Telegram attachment; return whether one existed."""
    global _ACTIVE_CHAT_ID, _OWNER_CHAT_ID
    attachment = _incoming_attachment(message)
    if not attachment:
        return False
    chat_id = (message.get("chat") or {}).get("id")
    if _OWNER_CHAT_ID is not None and chat_id != _OWNER_CHAT_ID:
        print("[Telegram] Ignored a file from a chat that is not the paired owner.")
        return True
    if _OWNER_CHAT_ID is None and chat_id is not None:
        _OWNER_CHAT_ID = _ACTIVE_CHAT_ID = chat_id
        chat_id_file = os.path.join(os.path.abspath(base_dir), "vault",
                                    "telegram_chat_id.txt")
        os.makedirs(os.path.dirname(chat_id_file), exist_ok=True)
        with open(chat_id_file, "w", encoding="ascii") as handle:
            handle.write(str(chat_id))
    result = download_telegram_file(
        attachment["file_id"], attachment["name"], attachment["mime"],
        base_dir=base_dir, update_id=update_id)
    if not result.get("ok"):
        send_telegram_message("I couldn't download that file: " +
                              str(result.get("error") or "unknown error"))
        return True
    caption = str(message.get("caption") or "").strip()
    send_telegram_message(
        f"Received {result['name']} ({result['size']} bytes).")
    prompt = (f"[Telegram]: I sent you a file. Local path: {result['path']}. "
              f"Filename: {result['name']}. MIME: {result['mime']}. "
              f"Size: {result['size']} bytes. SHA-256: {result['sha256']}.")
    if caption:
        prompt += f" My caption/instruction: {caption}"
    input_queue.put(prompt)
    print(f"[Telegram] Saved incoming file: {result['name']} "
          f"({result['size']} bytes)")
    return True

def send_telegram_audio(audio_path, caption=None):
    """Send spoken audio to the chat, so a reply can be listened to.

    sendVoice wants OGG/Opus and there is no ffmpeg here to produce it, so this
    uses sendAudio with the MP3 edge-tts already generates. Telegram plays it
    inline either way; the difference is that it appears as an audio file rather
    than a round voice bubble.

    Returns True only if Telegram accepted it. The caller has to know, because
    the alternative is Himanshu sitting somewhere else waiting for a reply that
    was spoken to an empty room.
    """
    if not _TOKEN or not _ACTIVE_CHAT_ID:
        return False
    if not audio_path or not os.path.exists(audio_path):
        return False
    url = f"https://api.telegram.org/bot{_TOKEN}/sendAudio"
    try:
        with open(audio_path, "rb") as handle:
            payload = {"chat_id": _ACTIVE_CHAT_ID, "title": "VANTA"}
            if caption:
                payload["caption"] = caption[:1000]
            files = {"audio": ("vanta.mp3", handle, "audio/mpeg")}
            response = _session().post(url, data=payload, files=files,
                                       timeout=60)
        if response.status_code == 200 and response.json().get("ok"):
            return True
        print(f"[Telegram] Audio rejected: {response.status_code} "
              f"{response.text[:160]}")
        return False
    except Exception as exc:
        print(f"[Telegram] Failed to send audio: {exc}")
        return False


def send_telegram_photo(text, photo_path):
    if not _TOKEN or not _ACTIVE_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{_TOKEN}/sendPhoto"
    
    try:
        with open(photo_path, 'rb') as photo:
            payload = {"chat_id": _ACTIVE_CHAT_ID, "caption": text}
            files = {"photo": photo}
            _session().post(url, data=payload, files=files, timeout=30)
    except Exception as e:
        print(f"[Telegram] Failed to send photo: {e}")

def _poll_loop(input_queue):
    global _LAST_UPDATE_ID, _ACTIVE_CHAT_ID, _OWNER_CHAT_ID
    
    print("[Telegram] Worker started, listening for messages...")
    
    while _RUNNING:
        try:
            url = f"https://api.telegram.org/bot{_TOKEN}/getUpdates"
            params = {"timeout": 30, "offset": _LAST_UPDATE_ID + 1}
            response = _session().get(url, params=params, timeout=40)
            
            if response.status_code == 200:
                data = response.json()
                if not data.get("ok"):
                    time.sleep(2)
                    continue
                    
                for update in data.get("result", []):
                    update_id = update.get("update_id")
                    if update_id and update_id > _LAST_UPDATE_ID:
                        _LAST_UPDATE_ID = update_id
                        # Persist before any text can enter another queue. A
                        # preflight password must never be replayed as an
                        # ordinary assistant message in the process launched
                        # immediately after authentication.
                        _save_update_offset(_LAST_UPDATE_ID)
                        
                    message = update.get("message")
                    if message:
                        # Telegram file messages do not contain a `text`
                        # field.  The old text-only condition silently threw
                        # every document away before VANTA could see it.
                        if _handle_incoming_attachment(
                                message, input_queue, _runtime_base_dir(),
                                update_id=update_id):
                            continue
                    if message and "text" in message:
                        text = message["text"]
                        chat_id = message["chat"]["id"]
                        
                        # Lifecycle passwords are intercepted before logging,
                        # persistence, or the agent queue. Only the previously
                        # paired owner chat is eligible to unlock the machine.
                        try:
                            import authentication_gate
                            if authentication_gate.handle_telegram(
                                    text, chat_id, _OWNER_CHAT_ID):
                                continue
                        except Exception as exc:
                            print(f"[Telegram] Authentication gate error: "
                                  f"{type(exc).__name__}.")

                        if _OWNER_CHAT_ID is not None and chat_id != _OWNER_CHAT_ID:
                            print("[Telegram] Ignored text from a chat that is not the paired owner.")
                            continue
                        _ACTIVE_CHAT_ID = chat_id
                        if _OWNER_CHAT_ID is None:
                            _OWNER_CHAT_ID = chat_id
                        
                        try:
                            base_dir = _runtime_base_dir()
                            chat_id_file = os.path.join(base_dir, "vault", "telegram_chat_id.txt")
                            with open(chat_id_file, "w") as f:
                                f.write(str(chat_id))
                        except Exception:
                            pass
                            
                        
                        
                        # Handle /help command
                        if text.lower() == "/help":
                            help_msg = (
                                "❄️ *VANTA Commands*\n\n"
                                "You can dynamically update keys by sending:\n"
                                "`update AIROUTER: <key>`\n"
                                "`update TAVILY: <key>`\n"
                                "`update AUTH_PASSWORD: <password>`\n"
                            )
                            send_telegram_message(help_msg)
                            continue

                        # Handle dynamic API key updates
                        if text.lower().startswith("update "):
                            parts = text.split(":", 1)
                            if len(parts) == 2:
                                cmd_part = parts[0].strip().lower()
                                new_value = parts[1].strip()
                                
                                env_key = None
                                if cmd_part == "update airouter":
                                    env_key = "AIROUTER_API_KEY"
                                elif cmd_part == "update tavily":
                                    env_key = "TAVILY_API_KEY"
                                elif cmd_part == "update auth_password":
                                    env_key = "AUTH_PASSWORD"
                                    if not (new_value.startswith('"') and new_value.endswith('"')):
                                        new_value = f'"{new_value}"'
                                
                                if env_key:
                                    try:
                                        import sys
                                        if getattr(sys, 'frozen', False):
                                            base_dir = os.path.dirname(sys.executable)
                                        else:
                                            base_dir = os.path.join(os.path.dirname(__file__), "..")
                                        config_path = os.path.join(base_dir, "config.bin")
                                        success = crypto_env.update_encrypted_env_key(config_path, env_key, new_value)
                                        if success:
                                            os.environ[env_key] = new_value.strip('"')
                                            send_telegram_message(f"✅ {env_key} successfully updated!")
                                        else:
                                            send_telegram_message(f"❌ Failed to update {env_key}: config.bin missing")
                                    except Exception as e:
                                        send_telegram_message(f"❌ Failed to update {env_key}: {e}")
                                    continue

                        print(f"\n[Telegram] Received: {text}")
                        
                        # Prepend [Telegram] so agent knows context
                        formatted_msg = f"[Telegram]: {text}"
                        input_queue.put(formatted_msg)
                        
                        
        except requests.exceptions.RequestException:
            time.sleep(5)
        except Exception as e:
            print(f"[Telegram] Polling error: {e}")
            time.sleep(5)
