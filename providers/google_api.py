import os.path
import base64
import json
import mimetypes
from pathlib import Path
from email.message import EmailMessage
from email.utils import parseaddr
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/calendar',
    # Read-write contacts for the People API actions, plus the auto-saved
    # "other contacts" (people only ever emailed). A token granted before this
    # was widened keeps working: credentials are loaded with the scopes they
    # were issued with (see get_credentials), and a contact write on such a
    # token says "reauthorize" instead of failing the refresh for everything.
    'https://www.googleapis.com/auth/contacts',
    'https://www.googleapis.com/auth/contacts.other.readonly',
    'https://www.googleapis.com/auth/tasks',
    # Full Drive access is intentional: Himanshu asked VANTA to find and
    # manage files that already exist in his Drive, not only files created by
    # this application.  Mutating operations remain governed by VANTA's code
    # policy; an OAuth scope is capability, never authorization to act.
    'https://www.googleapis.com/auth/drive'
]

USE_KEYRING_FALLBACK = True
class GoogleReauthorizationRequired(RuntimeError):
    """The stored Google grant is no longer usable and needs user consent."""


def _store_credentials(base_dir, creds):
    token_path = os.path.join(base_dir, 'token.json')
    try:
        from security.keyring_manager import set_secret_chunked
        set_secret_chunked("WINTER_SYSTEM", "google_oauth_token", creds.to_json())
        print("[OAuth] Successfully saved token to Windows Credential Manager.")
    except Exception as exc:
        print(f"[OAuth] Failed to write to Keyring: {exc}")
        if USE_KEYRING_FALLBACK:
            with open(token_path, 'w', encoding='utf-8') as token:
                token.write(creds.to_json())


def reauthorize_google(base_dir):
    """Start an explicit local OAuth consent flow and persist the new grant."""
    creds_path = os.path.join(base_dir, 'credentials.json')
    if not os.path.exists(creds_path):
        raise FileNotFoundError(f"Missing {creds_path}. Google OAuth cannot start.")
    flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    _store_credentials(base_dir, creds)
    return "Google access was reauthorized successfully."

def get_credentials(base_dir):
    creds = None
    token_path = os.path.join(base_dir, 'token.json')
    creds_path = os.path.join(base_dir, 'credentials.json')

    # Stored credentials are loaded with the scopes they were ISSUED with. Passing
    # the wider SCOPES list here would make every refresh request scopes the
    # user never consented to, and Google answers that with invalid_scope --
    # breaking Gmail and Calendar for the sake of a Contacts upgrade.
    # 1. Try Keyring First
    try:
        import sys
        if base_dir not in sys.path:
            sys.path.insert(0, base_dir)
        from security.keyring_manager import get_secret_chunked, set_secret_chunked
        keyring_token = get_secret_chunked("WINTER_SYSTEM", "google_oauth_token")
        if keyring_token:
            import json
            creds = Credentials.from_authorized_user_info(json.loads(keyring_token))
    except Exception as e:
        print(f"[OAuth] Keyring read failed: {e}")

    # 2. Fallback to token.json if allowed
    if not creds and USE_KEYRING_FALLBACK and os.path.exists(token_path):
        print("[OAuth] Falling back to token.json")
        creds = Credentials.from_authorized_user_file(token_path)
    
    # 3. Refresh or Auth
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                raise GoogleReauthorizationRequired(
                    "Google access expired, was revoked, or is missing a required scope. Reauthorize Google from VANTA before using Gmail, Calendar, Contacts, Tasks, or Drive."
                ) from exc
        else:
            # For new auth, we still need credentials.json
            if not os.path.exists(creds_path):
                raise FileNotFoundError(f"Missing {creds_path}. Please place credentials.json in {base_dir}")
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        
        _store_credentials(base_dir, creds)

    return creds

def read_emails(base_dir, max_results=5, query=""):
    """Fetches the latest emails, optionally matching a query."""
    creds = get_credentials(base_dir)
    try:
        service = build('gmail', 'v1', credentials=creds)
        
        kwargs = {'userId': 'me', 'maxResults': max_results}
        if query:
            kwargs['q'] = query
        else:
            kwargs['labelIds'] = ['INBOX']
            
        results = service.users().messages().list(**kwargs).execute()
        messages = results.get('messages', [])

        if not messages:
            return "No emails found in INBOX."

        output = []
        for msg in messages:
            msg_data = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
            payload = msg_data.get('payload', {})
            headers = payload.get('headers', [])
            
            subject = "No Subject"
            sender = "Unknown"
            extra_headers = {"To": "", "Cc": "", "Reply-To": "", "Date": ""}
            for header in headers:
                if header['name'] == 'Subject':
                    subject = header['value']
                if header['name'] == 'From':
                    sender = header['value']
                if header['name'] in extra_headers:
                    extra_headers[header['name']] = header['value']
            
            snippet = msg_data.get('snippet', '')
            unread = 'UNREAD' in (msg_data.get('labelIds') or [])
            attachments = []

            def collect_attachments(part):
                part = part if isinstance(part, dict) else {}
                filename = str(part.get('filename') or '').strip()
                body = part.get('body') or {}
                attachment_id = str(body.get('attachmentId') or '').strip()
                if filename or attachment_id:
                    try:
                        size = max(0, int(body.get('size') or 0))
                    except (TypeError, ValueError):
                        size = 0
                    attachments.append({
                        'name': filename or '(unnamed attachment)',
                        'mime': str(part.get('mimeType') or
                                    'application/octet-stream'),
                        'size': size,
                        # The id is safe operational metadata used by a future
                        # explicit download action; content is never logged.
                        'id': attachment_id,
                    })
                for child in part.get('parts') or []:
                    collect_attachments(child)

            collect_attachments(payload)
            attachment_line = "None"
            if attachments:
                attachment_line = "; ".join(
                    f"{item['name']} ({item['mime']}, {item['size']} bytes, "
                    f"attachment_id={item['id']})"
                    for item in attachments)
            # The id is what MARK_EMAIL_READ and ARCHIVE_EMAIL act on.
            output.append(
                f"[{msg['id']}]{' (unread)' if unread else ''}\n"
                f"From: {sender}\nTo: {extra_headers['To']}\nCc: {extra_headers['Cc']}\n"
                f"Reply-To: {extra_headers['Reply-To']}\nDate: {extra_headers['Date']}\n"
                f"Thread-ID: {msg_data.get('threadId', '')}\n"
                f"Labels: {','.join(msg_data.get('labelIds') or [])}\n"
                f"Subject: {subject}\nAttachments: {attachment_line}\n"
                f"Snippet: {snippet}\n---"
            )
            
        return "\n".join(output)
    except Exception as e:
        return f"[Error] Could not read emails: {e}"


def download_email_attachment(base_dir, message_id, attachment_id="",
                              filename="", destination=""):
    """Download one exact Gmail attachment without exposing its bytes.

    The message and attachment identifiers come from ``READ_EMAILS``.  A
    filename may be used when the user refers to an attachment by name, but a
    duplicate name is rejected rather than guessed.
    """
    message_id = str(message_id or "").strip()
    attachment_id = str(attachment_id or "").strip()
    requested_name = str(filename or "").strip()
    if not message_id:
        return "[Error] DOWNLOAD_EMAIL_ATTACHMENT needs a Gmail message_id."
    if not attachment_id and not requested_name:
        return ("[Error] Provide the attachment_id or exact filename returned "
                "by READ_EMAILS.")
    try:
        service = build('gmail', 'v1', credentials=get_credentials(base_dir))
        message = service.users().messages().get(
            userId='me', id=message_id, format='full').execute()
        matches = []

        def visit(part):
            part = part if isinstance(part, dict) else {}
            body = part.get('body') or {}
            candidate_id = str(body.get('attachmentId') or '').strip()
            candidate_name = str(part.get('filename') or '').strip()
            id_match = attachment_id and candidate_id == attachment_id
            name_match = (not attachment_id and requested_name
                          and candidate_name.casefold() == requested_name.casefold())
            if candidate_id and (id_match or name_match):
                matches.append((candidate_id, candidate_name or requested_name,
                                int(body.get('size') or 0)))
            for child in part.get('parts') or []:
                visit(child)

        visit(message.get('payload') or {})
        if not matches:
            return "[Error] That attachment was not found on the specified email."
        if len(matches) > 1:
            return ("[Error] More than one attachment has that filename. Use "
                    "the exact attachment_id returned by READ_EMAILS.")
        exact_id, exact_name, declared_size = matches[0]
        if declared_size > 25 * 1024 * 1024:
            return "[Error] Gmail attachment is larger than VANTA's 25 MB limit."
        payload = service.users().messages().attachments().get(
            userId='me', messageId=message_id, id=exact_id).execute()
        encoded = str(payload.get('data') or '')
        if not encoded:
            return "[Error] Gmail returned no attachment data."
        raw = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
        if len(raw) > 25 * 1024 * 1024:
            return "[Error] Gmail attachment exceeds VANTA's 25 MB limit."
        target_dir = Path(destination).expanduser() if destination else (
            Path(base_dir) / 'vault' / 'inbox')
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(exact_name or 'attachment.bin').name
        target = target_dir / safe_name
        stem, suffix, index = target.stem, target.suffix, 1
        while target.exists():
            target = target_dir / f"{stem} ({index}){suffix}"
            index += 1
        target.write_bytes(raw)
        return (f"[Success] Downloaded Gmail attachment {safe_name!r} to "
                f"{target.resolve()} ({len(raw)} bytes).")
    except Exception as exc:
        return f"[Error] Could not download Gmail attachment: {exc}"

def _attachment_paths(value):
    """Normalise one or many attachment paths without guessing filenames."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        raw = str(value or '').strip()
        if not raw:
            return []
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = None
        if isinstance(decoded, list):
            candidates = decoded
        else:
            # Windows paths contain ':' and may contain spaces, so commas are
            # not separators.  Semicolons and newlines are unambiguous here.
            candidates = [part for part in raw.replace('\r', '\n').split('\n')
                          for part in part.split(';')]
    result = []
    for candidate in candidates:
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(
            str(candidate or '').strip().strip('"'))))
        if path and path not in result:
            result.append(path)
    return result


def _validate_attachment_paths(attachments):
    """Return validated attachment paths or a user-facing error string."""
    paths = _attachment_paths(attachments)
    total_size = 0
    for path in paths:
        if not os.path.isfile(path):
            return [], (f"[Error] Email attachment does not exist or is not "
                        f"a file: {path}")
        size = os.path.getsize(path)
        total_size += size
        if size > 25 * 1024 * 1024:
            return [], (f"[Error] Email attachment is larger than Gmail's "
                        f"25 MB limit: {os.path.basename(path)}")
    if total_size > 25 * 1024 * 1024:
        return [], "[Error] Email attachments exceed Gmail's 25 MB total limit."
    return paths, ""


def _attach_files(message, paths):
    for path in paths:
        guessed, _encoding = mimetypes.guess_type(path)
        mime = guessed or 'application/octet-stream'
        maintype, subtype = mime.split('/', 1)
        with open(path, 'rb') as file_handle:
            message.add_attachment(
                file_handle.read(), maintype=maintype, subtype=subtype,
                filename=os.path.basename(path))


def _attachment_receipt(paths):
    return (
        "; attachments=" + ", ".join(os.path.basename(path) for path in paths)
        if paths else "; attachments=none")


def send_email(base_dir, to_email, subject, body, attachments=None):
    """Send one MIME email and return the Gmail provider's message id.

    Attachment paths are checked before the Gmail call, so approval can never
    be consumed by a missing file or an oversized payload.  The receipt names
    files but never prints their content.
    """
    try:
        paths, error = _validate_attachment_paths(attachments)
        if error:
            return error

        creds = get_credentials(base_dir)
        service = build('gmail', 'v1', credentials=creds)
        message = EmailMessage()
        message.set_content(body)
        message['To'] = to_email
        message['From'] = "me"
        message['Subject'] = subject

        _attach_files(message, paths)

        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {'raw': encoded_message}
        send_message = service.users().messages().send(userId="me", body=create_message).execute()
        return (f"[Success] Email sent successfully to {to_email}; "
                f"subject={subject!r}; id={send_message['id']}"
                f"{_attachment_receipt(paths)}")
    except Exception as e:
        return f"[Error] Could not send email: {e}"


def reply_email(base_dir, message_id, body, attachments=None):
    """Reply to one exact Gmail message and keep the reply in its thread.

    ``message_id`` is Gmail's provider id from :func:`read_emails`, not a
    sender or a subject.  Resolving the recipient and RFC reply headers from
    that exact message prevents a same-subject email from receiving the reply.
    """
    try:
        message_id = str(message_id or '').strip()
        if not message_id:
            return "[Error] REPLY_EMAIL needs the exact Gmail message_id."
        body = str(body or '')
        if not body.strip():
            return "[Error] REPLY_EMAIL needs a non-empty reply body."

        paths, error = _validate_attachment_paths(attachments)
        if error:
            return error

        creds = get_credentials(base_dir)
        service = build('gmail', 'v1', credentials=creds)
        original = service.users().messages().get(
            userId='me', id=message_id, format='metadata',
            metadataHeaders=['From', 'Reply-To', 'Subject', 'Message-ID',
                             'References']).execute()
        headers = {
            str(item.get('name') or '').lower(): str(item.get('value') or '')
            for item in (original.get('payload', {}).get('headers') or [])
        }
        recipient_raw = headers.get('reply-to') or headers.get('from')
        recipient = parseaddr(recipient_raw)[1] or recipient_raw.strip()
        if not recipient:
            return (f"[Error] Gmail message {message_id} has no usable From "
                    f"or Reply-To address.")
        thread_id = str(original.get('threadId') or '').strip()
        if not thread_id:
            return f"[Error] Gmail message {message_id} has no thread id."

        subject = headers.get('subject', '').strip() or '(no subject)'
        if not subject.lower().startswith('re:'):
            subject = f"Re: {subject}"
        rfc_message_id = headers.get('message-id', '').strip()

        reply = EmailMessage()
        reply.set_content(body)
        reply['To'] = recipient
        reply['From'] = 'me'
        reply['Subject'] = subject
        if rfc_message_id:
            reply['In-Reply-To'] = rfc_message_id
            prior = headers.get('references', '').strip()
            reply['References'] = (f"{prior} {rfc_message_id}".strip()
                                   if rfc_message_id not in prior else prior)
        _attach_files(reply, paths)

        encoded = base64.urlsafe_b64encode(reply.as_bytes()).decode()
        sent = service.users().messages().send(
            userId='me', body={'raw': encoded, 'threadId': thread_id}).execute()
        sent_id = str(sent.get('id') or '')
        sent_thread = str(sent.get('threadId') or thread_id)
        return (f"[Success] Email reply accepted by Gmail; "
                f"original_id={message_id}; id={sent_id}; "
                f"thread_id={sent_thread}; recipient={recipient}; "
                f"subject={subject!r}{_attachment_receipt(paths)}")
    except Exception as e:
        return f"[Error] Could not reply to email {message_id}: {e}"

def read_calendar(base_dir, max_results=5, days_back=0, days_ahead=None):
    """Fetches upcoming calendar events."""
    creds = get_credentials(base_dir)
    try:
        import when
        service = build('calendar', 'v3', credentials=creds)
        import datetime
        now_dt = datetime.datetime.utcnow() - datetime.timedelta(days=int(days_back or 0))
        time_min = now_dt.isoformat() + 'Z'
        list_kwargs = dict(
            calendarId='primary', timeMin=time_min,
            maxResults=max_results, singleEvents=True,
            orderBy='startTime'
        )
        if days_ahead is not None:
            time_max = (now_dt + datetime.timedelta(days=int(days_back or 0) + int(days_ahead))).isoformat() + 'Z'
            list_kwargs['timeMax'] = time_max
        events_result = service.events().list(**list_kwargs).execute()
        events = events_result.get('items', [])
        
        if not events:
            return "No upcoming events found in your Google Calendar."
            
        # days_back pulls finished events into a list headed "upcoming".
        # Keep the two apart -- see list_calendar_events_with_ids.
        past, future = [], []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            summary = event.get('summary', 'No Title')
            line = f"- {when.describe(start)}: {summary}"
            all_day = 'T' not in str(start)
            if not all_day and when.is_past(start):
                past.append(line)
            else:
                future.append(line)

        blocks = []
        if past:
            blocks.append("Already happened -- report these in the past tense "
                          "and do not move them to another day:\n"
                          + "\n".join(past))
        if future:
            blocks.append("Still to come:\n" + "\n".join(future))
        return "\n\n".join(blocks)
    except Exception as e:
        return f"An error occurred reading calendar: {e}"
def recent_completed_events(base_dir, hours_back=48, max_results=30):
    """Structured recently-ended primary-calendar events for follow-up logic."""
    creds = get_credentials(base_dir)
    import datetime
    service = build('calendar', 'v3', credentials=creds)
    ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    now = datetime.datetime.now(ist)
    start = now - datetime.timedelta(hours=max(1, int(hours_back)))
    response = service.events().list(
        calendarId='primary', timeMin=start.isoformat(), timeMax=now.isoformat(),
        maxResults=int(max_results), singleEvents=True, orderBy='startTime',
    ).execute()
    rows = []
    for event in response.get('items', []):
        raw_start = event.get('start', {}).get('dateTime') or event.get('start', {}).get('date')
        raw_end = event.get('end', {}).get('dateTime') or event.get('end', {}).get('date') or raw_start
        if not raw_start:
            continue
        rows.append({
            'id': event.get('id', ''),
            'summary': event.get('summary', 'Untitled event'),
            'start': raw_start,
            'end': raw_end,
            'description': str(event.get('description') or '')[:300],
            'location': str(event.get('location') or '')[:200],
            'all_day': 'T' not in raw_start,
            'status': event.get('status', 'confirmed'),
        })
    return rows


def google_connection_status(base_dir):
    """Verify the shared OAuth grant without exposing token material."""
    try:
        creds = get_credentials(base_dir)
        if not creds or not creds.valid:
            return {'ok': False, 'reauthorization_required': True,
                    'message': 'Google authorization is not valid.'}
        # A refresh proves only that the token is alive.  It does not prove an
        # older grant includes a newly-added Drive scope.  Exercise the least
        # expensive Drive read so CHECK_GOOGLE_CONNECTION reports the real
        # capability rather than a healthy-but-incomplete token.
        try:
            drive = build('drive', 'v3', credentials=creds, cache_discovery=False)
            drive.files().list(pageSize=1, fields='files(id)', spaces='drive').execute()
        except HttpError as exc:
            if getattr(exc, 'resp', None) is not None and exc.resp.status in (401, 403):
                detail = str(exc).lower()
                if ('accessnotconfigured' in detail or 'servicedisabled' in detail
                        or 'drive api has not been used' in detail):
                    return {
                        'ok': False, 'reauthorization_required': False,
                        'message': ('Google Drive API is not enabled for this OAuth project. '
                                    'Enable Google Drive API in Google Cloud Console, then check again.'),
                    }
                return {
                    'ok': False, 'reauthorization_required': True,
                    'message': ('Google is connected, but the grant does not include usable '
                                'Drive access. Reauthorize Google to add the new Drive scope.'),
                }
            raise
        return {'ok': True, 'reauthorization_required': False,
                'message': 'Google authorization is valid for Gmail, Calendar, Contacts, Tasks, and Drive.'}
    except GoogleReauthorizationRequired as exc:
        return {'ok': False, 'reauthorization_required': True, 'message': str(exc)}
    except Exception as exc:
        return {'ok': False, 'reauthorization_required': False,
                'message': f'Google connection check failed: {exc}'}

def _created_event_receipt(event):
    """What the server actually stored, as the receipt for creating it.

    The receipt used to be the htmlLink and nothing else. The event was right,
    but nothing downstream could tell: a planner asking for "the title, the
    date, the time and the timezone" could never be satisfied, the critic
    failed the turn twice, and the model went hunting for the evidence --
    eventually opening the event URL in a browser, where a bare
    google.com/calendar/event?eid=... lands on the Google Calendar product
    page. Fifteen calls and 118,900 tokens to verify a write that had
    succeeded on the first attempt.

    Read back from the API's own response rather than echoing the request, so
    the receipt is evidence of what exists rather than a restatement of what
    was asked for. The two differ whenever the server adjusts anything.
    """
    event = event if isinstance(event, dict) else {}
    start = event.get('start') or {}
    end = event.get('end') or {}
    when_stored = start.get('dateTime') or start.get('date') or 'unknown time'
    end_stored = end.get('dateTime') or end.get('date') or 'unknown time'
    zone = start.get('timeZone') or end.get('timeZone')
    # Google commonly omits timeZone when RFC3339 values carry an offset. The
    # offset remains authoritative server-returned timezone evidence.
    if not zone and isinstance(when_stored, str):
        import re as _re
        match = _re.search(r"([+-]\d\d:\d\d|Z)$", when_stored)
        zone = match.group(1) if match else None
    duration = ""
    try:
        import datetime as _datetime
        start_dt = _datetime.datetime.fromisoformat(
            str(when_stored).replace("Z", "+00:00"))
        end_dt = _datetime.datetime.fromisoformat(
            str(end_stored).replace("Z", "+00:00"))
        minutes = int((end_dt - start_dt).total_seconds() // 60)
        if minutes >= 0:
            duration = f"{minutes} minutes"
    except (TypeError, ValueError):
        pass
    parts = [
        f"Event created: {event.get('summary') or '(no title)'}",
        f"start={when_stored}",
        f"end={end_stored}",
    ]
    if zone:
        parts.append(f"timezone={zone}")
    if duration:
        parts.append(f"duration={duration}")
    if event.get('location'):
        parts.append(f"location={event['location']}")
    if event.get('description'):
        parts.append(f"description={str(event['description'])[:240]}")
    parts.append(f"id={event.get('id') or 'unknown'}")
    link = event.get('htmlLink')
    if link:
        parts.append(str(link))
    return " ".join(parts)


def add_calendar_event(base_dir, start_time, end_time, summary, event_date="",
                       location="", description=""):
    creds = get_credentials(base_dir)
    try:
        from googleapiclient.discovery import build
        import when
        service = build('calendar', 'v3', credentials=creds)
        event = {'summary': summary}
        if location:
            event['location'] = location
        if description:
            event['description'] = description
        
        import datetime
        # Fix logic for parsing time and date combinations
        if not event_date and len(start_time) <= 8:
            # LLM provided time but no date, assume today
            event_date = datetime.date.today().isoformat()
            
        if event_date and len(start_time) <= 8:
            if start_time:
                # Ensure seconds are included for valid RFC 3339
                if len(start_time) == 5:
                    start_time += ":00"
                if end_time and len(end_time) == 5:
                    end_time += ":00"
                    
                start_dt = f"{event_date}T{start_time}+05:30"
                end_dt = f"{event_date}T{end_time or start_time}+05:30"
                event['start'] = {'dateTime': start_dt}
                event['end'] = {'dateTime': end_dt}
            else:
                event['start'] = {'date': event_date}
                # Google Calendar's all-day end is exclusive.  Using the same
                # date produces a zero-day event, even though the API may
                # accept the payload.
                next_day = (datetime.date.fromisoformat(event_date)
                            + datetime.timedelta(days=1)).isoformat()
                event['end'] = {'date': next_day}
        elif len(start_time) == 10 and '-' in start_time:
             # Provided just a date like 2026-09-21 in start_time
             event['start'] = {'date': start_time}
             event['end'] = {'date': end_time or start_time}
        else:
             # Assuming full ISO string
             start_time = start_time.replace(' ', 'T')
             if end_time:
                 end_time = end_time.replace(' ', 'T')
             if 'T' in start_time and '+' not in start_time and 'Z' not in start_time:
                 start_time += '+05:30'
             if end_time and 'T' in end_time and '+' not in end_time and 'Z' not in end_time:
                 end_time += '+05:30'
                 
             event['start'] = {'dateTime': start_time}
             event['end'] = {'dateTime': end_time or start_time}
             
        event = service.events().insert(calendarId='primary', body=event).execute()
        return _created_event_receipt(event)
    except Exception as e:
        return f"An error occurred adding calendar event: {e}"


def update_calendar_event(base_dir, event_id, start_time="", end_time="", summary="",
                          location="", description=""):
    """Reschedules or edits an existing event. Only supplied fields change."""
    if not str(event_id or "").strip():
        return "Error: update_calendar_event needs an event_id. Use LIST_CALENDAR_EVENTS first."
    creds = get_credentials(base_dir)
    try:
        import when
        service = build('calendar', 'v3', credentials=creds)
        event = service.events().get(calendarId='primary', eventId=event_id).execute()
        changed = []
        if summary:
            event['summary'] = summary; changed.append('summary')
        if location:
            event['location'] = location; changed.append('location')
        if description:
            event['description'] = description; changed.append('description')
        import datetime
        import re

        original_start = dict(event.get('start') or {})
        original_end = dict(event.get('end') or {})

        def _stored_moment(part):
            raw = part.get('dateTime') or part.get('date') or ''
            return when.parse(raw)

        def _calendar_part(value, existing, fallback):
            """Normalize a date, a full timestamp, or a spoken-time value."""
            raw = str(value or '').strip().replace(' ', 'T')
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
                return {'date': raw}
            old = _stored_moment(existing) or _stored_moment(fallback)
            if re.fullmatch(r'\d{1,2}:\d{2}(?::\d{2})?', raw):
                if old is None:
                    raise ValueError(
                        "a time-only update needs the existing event date")
                clock = datetime.time.fromisoformat(raw)
                moment = datetime.datetime.combine(old.date(), clock,
                                                   tzinfo=old.tzinfo)
                part = {'dateTime': moment.isoformat()}
                if existing.get('timeZone'):
                    part['timeZone'] = existing['timeZone']
                return part
            moment = when.parse(raw, default_tz=(old.tzinfo if old else None))
            if moment is None:
                raise ValueError(f"invalid calendar time: {value}")
            part = {'dateTime': moment.isoformat()}
            if existing.get('timeZone'):
                part['timeZone'] = existing['timeZone']
            return part

        if start_time:
            new_start = _calendar_part(start_time, original_start,
                                       original_end)
            event['start'] = new_start
            changed.append('start')
            # Rescheduling only the start should not collapse or stretch the
            # meeting. Preserve its server-stored duration automatically.
            if not end_time:
                old_start = _stored_moment(original_start)
                old_end = _stored_moment(original_end)
                new_start_dt = _stored_moment(new_start)
                if old_start and old_end and new_start_dt:
                    shifted_end = new_start_dt + (old_end - old_start)
                    if 'date' in new_start:
                        event['end'] = {'date': shifted_end.date().isoformat()}
                    else:
                        event['end'] = {'dateTime': shifted_end.isoformat()}
                        if original_end.get('timeZone'):
                            event['end']['timeZone'] = original_end['timeZone']
        if end_time:
            new_end = _calendar_part(end_time, original_end,
                                     event.get('start') or original_start)
            # "Move it to 11 PM and end at 1 AM" crosses midnight. Google
            # needs the next day's timestamp, not an end before the start.
            start_dt = _stored_moment(event.get('start') or original_start)
            end_dt = _stored_moment(new_end)
            if (start_dt and end_dt and 'dateTime' in new_end
                    and end_dt <= start_dt
                    and re.fullmatch(r'\d{1,2}:\d{2}(?::\d{2})?',
                                     str(end_time).strip())):
                end_dt += datetime.timedelta(days=1)
                new_end = {'dateTime': end_dt.isoformat()}
                if original_end.get('timeZone'):
                    new_end['timeZone'] = original_end['timeZone']
            event['end'] = new_end
            changed.append('end')
        if not changed:
            return "Error: nothing to update. Provide summary, location, description, start_time or end_time."
        updated = service.events().update(
            calendarId='primary', eventId=event_id, body=event).execute()
        stored_start = (updated.get('start') or {}).get('dateTime') or (updated.get('start') or {}).get('date')
        stored_end = (updated.get('end') or {}).get('dateTime') or (updated.get('end') or {}).get('date')
        return (f"Event updated: {updated.get('summary') or '(no title)'} "
                f"changed={','.join(changed)} start={stored_start or 'unknown'} "
                f"end={stored_end or 'unknown'} id={updated.get('id') or event_id} "
                f"{updated.get('htmlLink') or ''}").strip()
    except Exception as e:
        return f"An error occurred updating the event: {e}"


def find_free_slots(base_dir, days_ahead=7, duration_minutes=60,
                    day_start_hour=9, day_end_hour=21):
    """Open gaps in the calendar, so scheduling does not need guesswork."""
    creds = get_credentials(base_dir)
    try:
        import datetime
        import when
        service = build('calendar', 'v3', credentials=creds)
        ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        now = datetime.datetime.now(ist)
        horizon = now + datetime.timedelta(days=int(days_ahead))
        busy = service.freebusy().query(body={
            "timeMin": now.isoformat(),
            "timeMax": horizon.isoformat(),
            "items": [{"id": "primary"}],
        }).execute()["calendars"]["primary"]["busy"]

        blocks = []
        for span in busy:
            blocks.append((
                datetime.datetime.fromisoformat(span["start"].replace("Z", "+00:00")).astimezone(ist),
                datetime.datetime.fromisoformat(span["end"].replace("Z", "+00:00")).astimezone(ist),
            ))
        blocks.sort()

        need = datetime.timedelta(minutes=int(duration_minutes))
        free = []
        for day in range(int(days_ahead) + 1):
            date = (now + datetime.timedelta(days=day)).date()
            cursor = max(
                now,
                datetime.datetime.combine(date, datetime.time(int(day_start_hour)), tzinfo=ist),
            )
            day_end = datetime.datetime.combine(date, datetime.time(int(day_end_hour)), tzinfo=ist)
            for start, end in blocks:
                if end <= cursor or start >= day_end:
                    continue
                if start - cursor >= need:
                    free.append((cursor, start))
                cursor = max(cursor, end)
            if day_end - cursor >= need:
                free.append((cursor, day_end))

        if not free:
            return f"No free block of {duration_minutes} minutes in the next {days_ahead} days."
        lines = [f"Free blocks of at least {duration_minutes} minutes (IST):"]
        for start, end in free[:12]:
            lines.append(f"- {start.strftime('%a %d %b %H:%M')} to {end.strftime('%H:%M')}")
        return "\n".join(lines)
    except Exception as e:
        return f"An error occurred finding free time: {e}"


def modify_email(base_dir, message_id, mark_read=False, archive=False):
    """Triage an email: mark it read and/or take it out of the inbox."""
    if not str(message_id or "").strip():
        return "Error: a message_id is required. Use READ_EMAILS first."
    creds = get_credentials(base_dir)
    try:
        service = build('gmail', 'v1', credentials=creds)
        remove = []
        if mark_read:
            remove.append('UNREAD')
        if archive:
            remove.append('INBOX')
        if not remove:
            return "Error: nothing to change. Ask for mark_read or archive."
        service.users().messages().modify(
            userId='me', id=message_id, body={'removeLabelIds': remove}).execute()
        done = " and ".join(
            part for part in [
                "marked read" if mark_read else "",
                "archived" if archive else "",
            ] if part
        )
        return f"Email {message_id} {done}."
    except Exception as e:
        return f"An error occurred modifying the email: {e}"


def delete_calendar_event(base_dir, event_id):
    """Deletes a calendar event by its ID."""
    creds = get_credentials(base_dir)
    try:
        from googleapiclient.discovery import build
        import when
        service = build('calendar', 'v3', credentials=creds)
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        return f"Event {event_id} deleted successfully."
    except Exception as e:
        return f"An error occurred deleting calendar event: {e}"

def list_calendar_events_with_ids(base_dir, max_results=10, days_back=0, days_ahead=14):
    """Lists upcoming calendar events including their IDs for deletion."""
    creds = get_credentials(base_dir)
    try:
        from googleapiclient.discovery import build
        import datetime
        import when
        service = build('calendar', 'v3', credentials=creds)
        # Start of day (IST) rather than the exact current instant, so events
        # earlier today (e.g. a 7:30 AM reminder checked at noon) aren't excluded.
        now_ist = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
        start_of_day_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        now_dt = (start_of_day_ist - datetime.timedelta(hours=5, minutes=30)) - datetime.timedelta(days=int(days_back))
        time_min = now_dt.isoformat() + 'Z'
        time_max = (now_dt + datetime.timedelta(days=int(days_back) + int(days_ahead))).isoformat() + 'Z'
        
        events_result = service.events().list(
            calendarId='primary', timeMin=time_min, timeMax=time_max,
            maxResults=int(max_results), singleEvents=True,
            orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])
        if not events:
            return "No upcoming events found."
        # The window starts at midnight, so this list mixes events that have
        # already happened today with ones still to come. Labelling the whole
        # thing "upcoming" is what kept the 07:30 reminder being announced as
        # due tomorrow: the model trusted the heading over the line it sat
        # above, and the only reading that satisfies both is tomorrow morning.
        # Split them so there is nothing left to reconcile.
        past, future = [], []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            summary = event.get('summary', 'No Title')
            event_id = event.get('id', '')
            # The day is worked out here, not left for the model to infer
            # from an ISO string. That inference is how a reminder which had
            # already passed this morning got announced as due tomorrow.
            line = f"- [{event_id}] {when.describe(start)}: {summary}"
            # An all-day event today has no clock to be past of; it parses to
            # midnight, which would file every one of them under "finished".
            all_day = 'T' not in str(start)
            if not all_day and when.is_past(start):
                past.append(line)
            else:
                future.append(line)

        blocks = []
        if past:
            blocks.append("Already happened today -- report these in the past "
                          "tense and do not move them to another day:\n"
                          + "\n".join(past))
        if future:
            blocks.append("Still to come:\n" + "\n".join(future))
        return "\n\n".join(blocks)
    except Exception as e:
        return f"An error occurred: {e}"


_JUNK_ORGANIZATION = frozenset({"a", "-", "na", "n/a", "none", "nil", "."})


def _sync_contact_details(connections, add_edge):
    """Emails and organizations from the address book, as graph facts.

    An organization is kept only if it reads like one: the real address book
    returns 96 "organizations" that are two distinct values, one of them the
    letter "a". A one-character employer is not information, and linking
    everyone who has it to each other would be worse than linking nobody.
    """
    import re as _re
    added = 0
    for person in connections:
        names = person.get('names') or []
        if not names:
            continue
        name = (names[0].get('displayName') or '').strip().lower()
        if not name:
            continue
        for entry in (person.get('emailAddresses') or [])[:3]:
            address = (entry.get('value') or '').strip().lower()
            if _re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
                add_edge(name, "HAS_EMAIL", address, confidence=1.0,
                         source_id="google_contacts_sync")
                added += 1
        for entry in (person.get('organizations') or [])[:2]:
            organization = (entry.get('name') or '').strip()
            if len(organization) >= 3 and organization.lower() not in _JUNK_ORGANIZATION:
                add_edge(name, "AFFILIATED_WITH", organization, confidence=0.9,
                         source_id="google_contacts_sync")
                added += 1
    return added


def sync_contacts(base_dir):
    """Fetch Google Contacts without allowing auth failures to crash a worker."""
    try:
        creds = get_credentials(base_dir)
        from googleapiclient.discovery import build
        import json
        service = build('people', 'v1', credentials=creds)
        results = service.people().connections().list(
            resourceName='people/me',
            pageSize=1000,
            # Emails and organizations as well. The sync asked for names and
            # phone numbers only, so the graph could say who a person was
            # and nothing about where to find or place them -- which left
            # an email from someone he knows with nothing to be linked to.
            personFields='names,phoneNumbers,emailAddresses,organizations'
        ).execute()
        
        connections = results.get('connections', [])
        contacts_dict = {}
        for person in connections:
            names = person.get('names', [])
            phones = person.get('phoneNumbers', [])
            if names and phones:
                name = names[0].get('displayName')
                # Grab the first available phone number
                phone = phones[0].get('canonicalForm') or phones[0].get('value')
                # Clean up phone number: remove spaces and non-digits (keep +)
                import re
                phone = re.sub(r'[^\d+]', '', phone)
                contacts_dict[name.lower()] = phone
                
        # --- PHASE 3: Sync to Graph DB ---
        from memory.graph_store import add_edge
        from memory.entities import get_canonical_name, add_entity
        synced = 0
        graph_blocked = False
        for name, phone in contacts_dict.items():
            try:
                canonical = get_canonical_name(name)
                if canonical == name:
                    add_entity(name, "Person")
                add_edge("Himanshu", "HAS_CONTACT", name, confidence=1.0, source_id="google_contacts_sync")
                add_edge(name, "HAS_PHONE", phone, confidence=1.0, source_id="google_contacts_sync")
                synced += 1
            except Exception as e:
                # A malformed query or unavailable graph affects the batch,
                # not one person's data. Stop issuing the same failing write
                # for every contact, and avoid logging their private names.
                code = str(getattr(e, 'code', '') or '')
                systemic = (isinstance(e, ConnectionError) or
                            code.endswith(('Statement.SyntaxError', 'Security.Unauthorized')) or
                            type(e).__name__ in {'ServiceUnavailable', 'SessionExpired'})
                print(f"[Contacts Sync] Contact write failed: {type(e).__name__}")
                if systemic:
                    graph_blocked = True
                    print('[Contacts Sync] Graph sync paused; remaining contacts will be retried on the next sync.')
                    break
        
        # What else the address book knows about them. Separate from the
        # phone sync above, and never allowed to fail it: the phones are what
        # everything else depends on.
        #
        # Includes people with no phone number. The loop above keeps only
        # contacts that have one, so a contact with just an email address was
        # dropped without a word -- one of the 167 in the real address book.
        try:
            added = 0 if graph_blocked else _sync_contact_details(connections, add_edge)
            if added:
                print(f"[Contacts] Linked {added} email address(es) and "
                      f"organization(s) to the people who have them.")
        except Exception as detail_error:
            print(f"[Contacts] Could not sync emails/organizations: {detail_error}")

        # Clear the TTL cache to force a refresh on next read
        try:
            import memory.contacts
            memory.contacts._cache_timestamp = 0
        except Exception:
            pass
            
        failed = len(contacts_dict) - synced
        print(f"[Contacts] Synced {synced}/{len(contacts_dict)} phone contacts; {failed} pending or failed.")
        return synced
    except Exception as e:
        print(f"[Contacts Error] {e}")
        return 0

def _tasks_service(base_dir):
    from googleapiclient.discovery import build
    return build('tasks', 'v1', credentials=get_credentials(base_dir))


def list_task_lists(base_dir):
    """All task lists, not just @default."""
    try:
        service = _tasks_service(base_dir)
        items = service.tasklists().list(maxResults=50).execute().get('items', [])
        if not items:
            return "No task lists found."
        return "Your Google Task lists:\n" + "\n".join(
            f"- [{item.get('id')}] {item.get('title', 'Untitled')}" for item in items
        )
    except Exception as e:
        return f"An error occurred reading task lists: {e}"


def _resolve_tasklist(service, tasklist):
    """Accept a list id, a list name, or nothing (meaning @default)."""
    wanted = str(tasklist or "").strip()
    if not wanted or wanted == "@default":
        return "@default", "Default"
    try:
        items = service.tasklists().list(maxResults=50).execute().get('items', [])
    except Exception:
        return wanted, wanted
    for item in items:
        if item.get('id') == wanted:
            return wanted, item.get('title', wanted)
    for item in items:
        if item.get('title', '').strip().casefold() == wanted.casefold():
            return item.get('id'), item.get('title')
    return wanted, wanted


def list_google_tasks(base_dir, max_results=10, tasklist="", include_completed=False, all_lists=False):
    """Lists tasks with their IDs and notes.

    IDs are required for completing or rescheduling anything, and the notes
    field often carries the actual detail of what needs doing.
    """
    try:
        service = _tasks_service(base_dir)
        if all_lists:
            targets = [
                (item.get('id'), item.get('title', 'Untitled'))
                for item in service.tasklists().list(maxResults=50).execute().get('items', [])
            ] or [("@default", "Default")]
        else:
            targets = [_resolve_tasklist(service, tasklist)]

        blocks = []
        for list_id, list_name in targets:
            items = service.tasks().list(
                tasklist=list_id,
                maxResults=int(max_results),
                showCompleted=bool(include_completed),
                showHidden=bool(include_completed),
            ).execute().get('items', [])
            if not items:
                blocks.append(f"Google Tasks ({list_name}): no tasks.")
                continue
            lines = [f"Google Tasks ({list_name}) - list_id={list_id}:"]
            for item in items:
                title = item.get('title', 'Untitled')
                due = item.get('due', '')
                status = item.get('status', '')
                mark = "x" if status == "completed" else " "
                line = f"- [{mark}] [{item.get('id')}] {title}"
                if due:
                    line += f" (due {due[:10]})"
                notes = (item.get('notes') or "").strip()
                if notes:
                    indented = "\n".join(f"      {n}" for n in notes.splitlines() if n.strip())
                    line += f"\n    notes:\n{indented}"
                lines.append(line)
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)
    except Exception as e:
        return f"An error occurred reading Google Tasks: {e}"


def add_google_task(base_dir, title, notes="", due_date="", tasklist=""):
    try:
        service = _tasks_service(base_dir)
        list_id, list_name = _resolve_tasklist(service, tasklist)
        task = {'title': title, 'notes': notes}
        if due_date:
            # Google Tasks expects an RFC 3339 timestamp.
            task['due'] = due_date if 'T' in due_date else f"{due_date}T00:00:00.000Z"
        result = service.tasks().insert(tasklist=list_id, body=task).execute()
        return f"Task created in {list_name}: {result.get('title')} (id={result.get('id')})"
    except Exception as e:
        return f"An error occurred adding Google Task: {e}"


def complete_google_task(base_dir, task_id, tasklist=""):
    """Marks a task done. Get the id from LIST_TASKS."""
    if not str(task_id or "").strip():
        return "Error: complete_google_task needs a task_id. Use LIST_TASKS first."
    try:
        service = _tasks_service(base_dir)
        list_id, list_name = _resolve_tasklist(service, tasklist)
        result = service.tasks().patch(
            tasklist=list_id, task=task_id, body={'status': 'completed'}
        ).execute()
        return f"Completed in {list_name}: {result.get('title')}"
    except Exception as e:
        return f"An error occurred completing Google Task: {e}"


def update_google_task(base_dir, task_id, title="", notes="", due_date="", tasklist=""):
    """Reschedules or edits an existing task."""
    if not str(task_id or "").strip():
        return "Error: update_google_task needs a task_id. Use LIST_TASKS first."
    body = {}
    if title:
        body['title'] = title
    if notes:
        body['notes'] = notes
    if due_date:
        body['due'] = due_date if 'T' in due_date else f"{due_date}T00:00:00.000Z"
    if not body:
        return "Error: nothing to update. Provide title, notes, or due."
    try:
        service = _tasks_service(base_dir)
        list_id, list_name = _resolve_tasklist(service, tasklist)
        result = service.tasks().patch(tasklist=list_id, task=task_id, body=body).execute()
        changed = ", ".join(sorted(body))
        return f"Updated {changed} in {list_name}: {result.get('title')}"
    except Exception as e:
        return f"An error occurred updating Google Task: {e}"


def delete_google_task(base_dir, task_id, tasklist=""):
    """Delete one exact Google Task after its ID has been listed."""
    if not str(task_id or "").strip():
        return "[Error] DELETE_TASK needs a task_id. Use LIST_TASKS first."
    try:
        service = _tasks_service(base_dir)
        list_id, list_name = _resolve_tasklist(service, tasklist)
        service.tasks().delete(tasklist=list_id, task=task_id).execute()
        return f"[Success] Deleted task {task_id} from {list_name}."
    except Exception as exc:
        return f"[Error] Could not delete Google Task: {exc}"
