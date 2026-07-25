import os
import logging
import warnings
import time as _time
import threading
import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from config import TOKEN_PATH, CREDENTIALS_PATH

os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '0'

# ── Cached credentials (avoid re-reading token.json on every call) ────────────
_google_creds = None
_google_creds_refreshed_at = 0
# Lock to prevent race conditions in token refresh
_creds_lock = threading.Lock()

CREDS = TOKEN_PATH  # alias for backward compatibility
# Suppress the "Not all requested scopes were granted" oauthlib warning —
# the token has all needed scopes; the warning fires because Google's auth
# library logs it at WARNING level on every credential load.
warnings.filterwarnings('ignore', message='.*Not all requested scopes.*')
logging.getLogger('google_auth_oauthlib').setLevel(logging.ERROR)
logging.getLogger('google.auth').setLevel(logging.ERROR)

logger = logging.getLogger(__name__)


def _classroom_work_is_actionable(work: dict, now: datetime.datetime | None = None) -> bool:
    """Exclude old coursework before it reaches the task extractor."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    overdue_grace = max(0, int(os.getenv("GOOGLE_CLASSROOM_ASSIGNMENT_OVERDUE_GRACE_DAYS", "7")))
    undated_update_days = max(0, int(os.getenv("GOOGLE_CLASSROOM_NO_DUE_UPDATE_DAYS", "30")))
    due = work.get("dueDate", {}) or {}
    try:
        due_day = datetime.date(int(due["year"]), int(due["month"]), int(due["day"]))
    except (KeyError, TypeError, ValueError):
        due_day = None

    if due_day is not None:
        return due_day >= now.date() - datetime.timedelta(days=overdue_grace)

    update_time = work.get("updateTime", "")
    try:
        updated = datetime.datetime.fromisoformat(update_time.replace("Z", "+00:00"))
        return updated >= now - datetime.timedelta(days=undated_update_days)
    except (AttributeError, ValueError):
        return False

# If modifying these scopes, delete the file token.json.
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.student-submissions.me.readonly',
    'https://www.googleapis.com/auth/classroom.announcements.readonly',
    'https://www.googleapis.com/auth/documents.readonly',
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/calendar.events'
    # TODO: add classroom.coursework.me.readonly in Google Cloud Console
    # → https://console.cloud.google.com/apis/credentials/consent → project unique-sentinel-486019-r1
    # then re-add: 'https://www.googleapis.com/auth/classroom.coursework.me.readonly',
]

CREDENTIALS_PATH = os.path.join(os.path.dirname(__file__), '..', 'credentials.json')
TOKEN_PATH = os.path.join(os.path.dirname(__file__), '..', 'token.json')

def get_google_credentials():
    """Get Google credentials with caching and retry on refresh.

    Thread-safe: uses _creds_lock to prevent concurrent refreshes.
    """
    global _google_creds, _google_creds_refreshed_at

    # Fast path: return cached creds if still valid (cache for 5 minutes)
    # This check is outside the lock for performance - stale check is OK
    if _google_creds and _google_creds.valid and (_time.time() - _google_creds_refreshed_at) < 300:
        return _google_creds

    # Slow path: need to refresh or re-authenticate - use lock
    with _creds_lock:
        # Re-check inside lock (double-checked locking pattern)
        if _google_creds and _google_creds.valid and (_time.time() - _google_creds_refreshed_at) < 300:
            return _google_creds

        creds = None
        if os.path.exists(TOKEN_PATH):
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                # Retry refresh up to 3 times with backoff
                for attempt in range(3):
                    try:
                        creds.refresh(Request())
                        break
                    except Exception as e:
                        if attempt < 2:
                            _time.sleep(2 ** attempt)
                        else:
                            logger.error(
                                f"Failed to refresh Google token after 3 attempts: {e}. "
                                "The refresh token may have been revoked. "
                                "Run 'python3 google_auth_setup.py' on a machine with a browser to regenerate token.json."
                            )
                            return None

            if not creds or not creds.valid:
                # Token refresh failed — cannot auto-re-auth on headless server.
                # Run google_auth_setup.py on a machine with a browser, then copy
                # token.json back to the server.
                logger.error(
                    "Google token expired and cannot be auto-refreshed on a headless server. "
                    "Run 'python3 google_auth_setup.py' on a machine with a browser, "
                    "then copy token.json to the server and restart the bot."
                )
                return None

        if not creds:
            logger.error(
                "Google credentials unavailable. Ensure token.json exists and is valid."
            )
            return None

        # Update cache
        _google_creds = creds
        _google_creds_refreshed_at = _time.time()
        return creds


def get_unread_emails() -> str:
    """Fetch up to 5 unread emails from Gmail."""
    creds = get_google_credentials()
    if not creds:
        return "Google API credentials not configured."

    try:
        service = build('gmail', 'v1', credentials=creds)
        results = service.users().messages().list(userId='me', labelIds=['UNREAD'], maxResults=5).execute()
        messages = results.get('messages', [])

        if not messages:
            return "No unread emails."

        output = []
        for msg in messages:
            msg_data = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
            headers = msg_data['payload'].get('headers', [])
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
            sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
            date = next((h['value'] for h in headers if h['name'] == 'Date'), 'Unknown Date')
            output.append(f"From: {sender}\nSubject: {subject}\nDate: {date}\n---")

        return "\n".join(output)
    except Exception as e:
        logger.error(f"Error fetching emails: {e}")
        return f"Error connecting to Gmail: {e}"


def get_classroom_assignments() -> str:
    """Fetch assignments from Google Classroom (last 30 days)."""
    creds = get_google_credentials()
    if not creds:
        return "Google API credentials not configured."

    try:
        service = build('classroom', 'v1', credentials=creds)
        results = service.courses().list(courseStates=['ACTIVE']).execute()
        courses = results.get('courses', [])

        if not courses:
            return "No active Google Classroom courses found."

        output = []
        for course in courses:
            try:
                coursework = service.courses().courseWork().list(
                    courseId=course['id'],
                    courseWorkStates=['PUBLISHED'],
                    orderBy='updateTime desc',
                    pageSize=20
                ).execute()
                works = coursework.get('courseWork', [])

                for work in works:
                    if not _classroom_work_is_actionable(work):
                        continue

                    title = work.get('title', 'Untitled')
                    due_date = work.get('dueDate', {})
                    due_str = ""
                    if due_date:
                        try:
                            due_str = f" (Due: {due_date.get('year', '')}-{due_date.get('month', ''):02d}-{due_date.get('day', ''):02d})"
                        except Exception:
                            pass

                    output.append(f"  [{course['name']}] {title}{due_str}")

            except Exception as e:
                logger.warning(f"Error fetching coursework for {course['name']}: {e}")

        if not output:
            return "No recent assignments found."

        return "Google Classroom Assignments:\n" + "\n".join(output)

    except Exception as e:
        return f"Error: {e}"


def get_classroom_announcements() -> str:
    """Fetch announcements from Google Classroom (last 30 days)."""
    creds = get_google_credentials()
    if not creds:
        return "Google API credentials not configured."

    try:
        service = build('classroom', 'v1', credentials=creds)
        results = service.courses().list(courseStates=['ACTIVE']).execute()
        courses = results.get('courses', [])

        if not courses:
            return "No active Google Classroom courses found."

        import datetime
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)

        output = []
        for course in courses:
            try:
                announcements = service.courses().announcements().list(
                    courseId=course['id'],
                    orderBy='updateTime desc',
                    pageSize=10
                ).execute()
                anns = announcements.get('announcements', [])

                for ann in anns:
                    update_time = ann.get('updateTime', '')
                    if update_time:
                        try:
                            updated = datetime.datetime.fromisoformat(update_time.replace('Z', '+00:00'))
                            if updated < cutoff:
                                continue
                        except Exception:
                            pass

                    text = ann.get('text', 'No content')
                    text = text[:200] + "..." if len(text) > 200 else text
                    output.append(f"  [{course['name']}] {text}")

            except Exception as e:
                logger.warning(f"Error fetching announcements for {course['name']}: {e}")

        if not output:
            return "No recent announcements found."

        return "Google Classroom Announcements:\n" + "\n".join(output)

    except Exception as e:
        return f"Error: {e}"


def get_recent_google_docs() -> str:
    """Fetch recently modified Google Docs from Drive (last 30 days)."""
    creds = get_google_credentials()
    if not creds:
        return "Google API credentials not configured."

    try:
        service = build('drive', 'v3', credentials=creds)
        import datetime
        cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).strftime('%Y-%m-%d')
        query = f"mimeType='application/vnd.google-apps.document' and modifiedTime > '{cutoff}' and trashed=false"
        results = service.files().list(
            q=query,
            orderBy='modifiedTime desc',
            pageSize=10,
            fields="files(id, name, modifiedTime, webViewLink)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        files = results.get('files', [])

        if not files:
            return "No recent Google Docs found."

        output = []
        for f in files:
            output.append(f"  {f['name']} — Modified: {f['modifiedTime'][:10]} — {f.get('webViewLink', '')}")

        return "Recent Google Docs:\n" + "\n".join(output)

    except Exception as e:
        logger.error(f"Error fetching Google Docs: {e}")
        return f"Error connecting to Google Drive/Docs: {e}"


def download_drive_file(file_id: str, output_path: str) -> bool:
    """Downloads a file from Google Drive by file ID. Automatically exports Google Docs/Workspace files."""
    from googleapiclient.http import MediaIoBaseDownload
    import io
    creds = get_google_credentials()
    if not creds:
        logger.error("No credentials available to download file.")
        return False
    try:
        service = build('drive', 'v3', credentials=creds)

        # 1. Get file metadata to check mimeType (must support shared drives)
        file_meta = service.files().get(fileId=file_id, fields="mimeType,name", supportsAllDrives=True).execute()
        mime_type = file_meta.get("mimeType", "")
        file_name = file_meta.get("name", "")

        # Google Workspace types that MUST be exported (cannot be downloaded directly)
        workspace_types = {
            "application/vnd.google-apps.document": "application/pdf",
            "application/vnd.google-apps.spreadsheet": "application/pdf",
            "application/vnd.google-apps.presentation": "application/pdf",
            "application/vnd.google-apps.drawing": "application/pdf",
            "application/vnd.google-apps.script": "application/vnd.google-apps.script+json",
            "application/vnd.google-apps.form": "application/pdf",
            "application/vnd.google-apps.site": "application/pdf",
            "application/vnd.google-apps.map": "application/pdf",
            "application/vnd.google-apps.fusiontable": "application/pdf",
            "application/vnd.google-apps.jam": "application/pdf",
            "application/vnd.google-apps.photo": "image/jpeg",
            "application/vnd.google-apps.shortcut": None,  # shortcuts need target resolution
            "application/vnd.google-apps.audio": "audio/mpeg",
            "application/vnd.google-apps.video": "video/mp4",
        }

        request = None
        if mime_type in workspace_types:
            export_mime = workspace_types[mime_type]
            if export_mime is None:
                logger.warning(f"File {file_id} ({mime_type}) is a shortcut, skipping")
                return False
            logger.info(f"Exporting {file_name} ({mime_type}) as {export_mime}")
            request = service.files().export_media(fileId=file_id, mimeType=workspace_types[mime_type])
        else:
            # Try direct download for binary files (PDFs, images, etc.)
            try:
                request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
            except Exception as e:
                logger.warning(f"Direct download failed for {file_id}, trying export as PDF: {e}")
                try:
                    request = service.files().export_media(fileId=file_id, mimeType="application/pdf")
                except Exception as e2:
                    logger.error(f"Export also failed for {file_id}: {e2}")
                    return False

        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()

        with open(output_path, 'wb') as f:
            f.write(fh.getvalue())
        return True
    except Exception as e:
        logger.error(f"Failed to download/export file {file_id}: {e}")
        return False


def download_classroom_pdfs(output_dir: str = "classroom_pdfs") -> str:
    """Download all PDF attachments from recent Classroom assignments, OCR them, and save as text."""
    creds = get_google_credentials()
    if not creds:
        return "Google API credentials not configured."

    os.makedirs(output_dir, exist_ok=True)
    downloaded = []
    skipped = 0
    failed_downloads = []

    try:
        service = build('classroom', 'v1', credentials=creds)
        results = service.courses().list(courseStates=['ACTIVE']).execute()
        courses = results.get('courses', [])

        if not courses:
            return "No active Google Classroom courses found."

        import datetime
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)

        for course in courses:
            try:
                coursework = service.courses().courseWork().list(
                    courseId=course['id'],
                    courseWorkStates=['PUBLISHED'],
                    orderBy='updateTime desc',
                    pageSize=20
                ).execute()
                works = coursework.get('courseWork', [])

                for work in works:
                    # Skip old assignments
                    update_time = work.get('updateTime', '')
                    if update_time:
                        try:
                            updated = datetime.datetime.fromisoformat(update_time.replace('Z', '+00:00'))
                            if updated < cutoff:
                                skipped += 1
                                continue
                        except Exception:
                            pass

                    title = work.get('title', 'Untitled')
                    materials = work.get('materials', [])

                    for mat in materials:
                        if 'driveFile' in mat and 'driveFile' in mat['driveFile']:
                            df = mat['driveFile']['driveFile']
                            file_title = df.get('title', 'untitled')
                            file_id = df.get('id')
                            mime_type = df.get('mimeType', '')

                            if not file_id:
                                continue

                            # Check if we can download/export this file
                            try:
                                drive_service = build('drive', 'v3', credentials=creds)
                                file_meta = drive_service.files().get(
                                    fileId=file_id,
                                    fields="mimeType,name,capabilities/canDownload",
                                    supportsAllDrives=True
                                ).execute()
                                mime_type = file_meta.get('mimeType', '')
                                can_download = file_meta.get('capabilities', {}).get('canDownload', False)
                            except Exception as e:
                                logger.warning(f"Could not check file metadata for {file_title}: {e}")
                                skipped += 1
                                continue

                            # Skip if already downloaded
                            safe_name = "".join(c for c in file_title if c.isalnum() or c in (' ', '-', '_')).rstrip()
                            if not safe_name.lower().endswith('.pdf'):
                                safe_name += '.pdf'
                            # Sanitize course name for filesystem
                            safe_course_name = "".join(c for c in course['name'] if c.isalnum() or c in (' ', '-', '_', '@')).rstrip()
                            pdf_path = os.path.join(output_dir, f"{safe_course_name}_{safe_name}")
                            txt_path = pdf_path.replace('.pdf', '.txt')

                            # Ensure output directory exists
                            os.makedirs(output_dir, exist_ok=True)

                            if os.path.exists(pdf_path):
                                skipped += 1
                                continue

                            # Determine how to handle this file based on mime type
                            is_workspace = mime_type.startswith('application/vnd.google-apps.')
                            is_pdf = mime_type == 'application/pdf'

                            downloaded_this = False

                            # Wrap all mime type handling in try/except to catch download errors
                            try:
                                if is_workspace:
                                    # Google Workspace files (Docs, Sheets, Slides) - always export as PDF
                                    logger.info(f"Exporting {file_title} ({mime_type}) as PDF")
                                    try:
                                        if download_drive_file(file_id, pdf_path):
                                            try:
                                                import subprocess as _sp
                                                _sp.run(['pdftotext', '-layout', pdf_path, txt_path], check=True, timeout=60)
                                                downloaded.append(f"  {course['name']}/{file_title} (Google Doc → PDF + OCR)")
                                            except Exception as e:
                                                logger.warning(f"OCR failed for {file_title}: {e}")
                                                downloaded.append(f"  {course['name']}/{file_title} (Google Doc → PDF, OCR failed)")
                                            downloaded_this = True
                                        else:
                                            logger.warning(f"Export failed for {file_title} ({mime_type})")
                                    except Exception as e:
                                        if "cannotDownloadFile" in str(e):
                                            logger.info(f"Skipping {file_title} - cannot download (permission denied)")
                                            skipped += 1
                                        else:
                                            logger.warning(f"Export error for {file_title}: {e}")
                                elif mime_type == 'application/pdf':
                                    # PDF file - check if downloadable
                                    if can_download:
                                        if download_drive_file(file_id, pdf_path):
                                            try:
                                                import subprocess as _sp
                                                _sp.run(['pdftotext', '-layout', pdf_path, txt_path], check=True, timeout=60)
                                                downloaded.append(f"  {course['name']}/{file_title} (PDF + OCR)")
                                            except Exception as e:
                                                logger.warning(f"OCR failed for {file_title}: {e}")
                                                downloaded.append(f"  {course['name']}/{file_title} (PDF only, OCR failed)")
                                            downloaded_this = True
                                    else:
                                        skipped += 1
                                        logger.info(f"Skipping {file_title} - cannot download (view-only)")
                                else:
                                    # Other file types - try to download if allowed
                                    if can_download:
                                        if download_drive_file(file_id, pdf_path):
                                            downloaded.append(f"  {course['name']}/{file_title}")
                                            downloaded_this = True
                                    else:
                                        skipped += 1
                                        logger.info(f"Skipping {file_title} - cannot download (view-only)")

                                if not downloaded_this:
                                    failed_downloads.append(f"{course['name']}/{file_title}")

                            except Exception as e:
                                # Handle specific Google Drive API errors
                                if "cannotDownloadFile" in str(e) or "403" in str(e):
                                    logger.warning(f"Cannot download {file_title} (permission restricted): {e}")
                                    failed_downloads.append(f"{course['name']}/{file_title} (permission denied)")
                                else:
                                    logger.warning(f"Error downloading from {course['name']}: {e}")
                                    failed_downloads.append(f"{course['name']}/{file_title} (error: {str(e)[:50]})")

            except Exception as e:
                logger.warning(f"Error processing course {course['name']}: {e}")

    except Exception as e:
        return f"Error: {e}"

    result_parts = []
    if downloaded:
        result_parts.append(f"Downloaded {len(downloaded)} Classroom PDFs to {output_dir}/:")
        result_parts.extend(downloaded)
    if skipped:
        result_parts.append(f"\nSkipped {skipped} files (already downloaded or view-only)")
    if failed_downloads:
        result_parts.append(f"\nFailed to download {len(failed_downloads)} files (permission issues):")
        result_parts.extend(failed_downloads[:20])  # Limit output

    if not downloaded and not failed_downloads:
        return f"No new PDFs downloaded (skipped {skipped} already downloaded or view-only)."

    return "\n".join(result_parts)


if __name__ == "__main__":
    print("Testing Gmail API...")
    print(get_unread_emails())
    print("\nTesting Google Classroom Assignments...")
    print(get_classroom_assignments())
    print("\nTesting Google Classroom Announcements...")
    print(get_classroom_announcements())
