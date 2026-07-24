import os
import json
import logging
import datetime
from googleapiclient.discovery import build
from canvasapi import Canvas
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(BASE_DIR, '..', 'token.json')
ARCHIVE_DIR = os.path.join(BASE_DIR, '..', 'offline_archive')

os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

def get_google_creds():
    # Delegate to the canonical credential loader in google_scraper, which
    # auto-refreshes an expired token via its refresh_token (with retries) and
    # caches the result. Rolling our own here previously returned an expired
    # token as-is, causing the nightly "Expired access token" 403 cascade.
    from scrapers.google_scraper import get_google_credentials
    return get_google_credentials()

STATE_FILE = os.path.join(ARCHIVE_DIR, 'export_state.json')
DELTA_FILE = os.path.join(ARCHIVE_DIR, 'delta_export.txt')

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"google_docs": [], "classroom": [], "canvas": [], "gmail": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def append_to_delta(text):
    with open(DELTA_FILE, "a") as f:
        f.write(text + "\n")

def export_all_google_docs():
    logger.info("Exporting NEW Google Docs...")
    creds = get_google_creds()
    if not creds: return
    
    drive_service = build('drive', 'v3', credentials=creds, cache_discovery=False)
    docs_service = build('docs', 'v1', credentials=creds, cache_discovery=False)
    
    thirty_days_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).isoformat().replace("+00:00", "Z")
    query = f"mimeType='application/vnd.google-apps.document' and modifiedTime > '{thirty_days_ago}' and trashed=false"
    page_token = None
    state = load_state()
    new_docs = 0
    
    while True:
        results = drive_service.files().list(
            q=query, 
            fields="nextPageToken, files(id, name)", 
            pageSize=100, 
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives"
        ).execute()
        items = results.get('files', [])
        
        for item in items:
            doc_id = item['id']
            if doc_id in state["google_docs"]:
                continue
                
            try:
                doc = docs_service.documents().get(documentId=doc_id).execute()
                content = ""
                for element in doc.get('body', {}).get('content', []):
                    if 'paragraph' in element:
                        for pe in element['paragraph']['elements']:
                            if 'textRun' in pe:
                                content += pe['textRun']['content']
                append_to_delta(f"--- Google Doc: {item['name']} ---\n{content}\n")
                state["google_docs"].append(doc_id)
                new_docs += 1
            except Exception as e:
                logger.error(f"Error fetching Google Doc {item.get('name')}: {e}")
                
        page_token = results.get('nextPageToken')
        if not page_token:
            break
            
    save_state(state)
    logger.info(f"Exported {new_docs} NEW Google Docs.")

def export_all_classroom():
    logger.info("Exporting NEW Google Classroom data...")
    creds = get_google_creds()
    if not creds: return
    
    service = build('classroom', 'v1', credentials=creds)
    courses = service.courses().list(courseStates=['ACTIVE']).execute().get('courses', [])
    state = load_state()
    
    def _process_materials(materials):
        for m in materials:
            if 'driveFile' in m:
                f_id = m['driveFile'].get('driveFile', {}).get('id')
                f_title = m['driveFile'].get('driveFile', {}).get('title', '')
                if f_id and f_title.lower().endswith('.pdf'):
                    import tempfile
                    import pypdf
                    import sys
                    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
                    from scrapers.google_scraper import download_drive_file
                    
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                        path = tmp.name
                    if download_drive_file(f_id, path):
                        try:
                            reader = pypdf.PdfReader(path)
                            text = ""
                            for page in reader.pages:
                                text += page.extract_text() + "\n"
                                
                            if len(text.strip()) <= 50:
                                # Fallback to OCR for scanned images
                                import pytesseract
                                from pdf2image import convert_from_path
                                logger.info(f"Running OCR on {f_title}...")
                                images = convert_from_path(path)
                                text = ""
                                for img in images:
                                    text += pytesseract.image_to_string(img) + "\n"
                                    
                            if len(text.strip()) > 50:
                                append_to_delta(f"\n\n=== EXPORTED PDF (HISTORICAL): {f_title} ===\n{text}")
                        except Exception as e:
                            logger.error(f"Failed historical PDF {f_title}: {e}")
                    try:
                        os.remove(path)
                    except Exception: pass

    for course in courses:
        # Announcements
        try:
            announcements = service.courses().announcements().list(courseId=course['id']).execute().get('announcements', [])
            for a in announcements:
                a_id = a.get('id')
                if a_id and a_id not in state["classroom"]:
                    append_to_delta(f"=== CLASSROOM COURSE: {course['name']} ===\nAnnouncement: {a.get('text', '')}")
                    _process_materials(a.get('materials', []))
                    state["classroom"].append(a_id)
        except Exception: pass
        
        # Coursework
        try:
            works = service.courses().courseWork().list(courseId=course['id']).execute().get('courseWork', [])
            for w in works:
                w_id = w.get('id')
                if w_id and w_id not in state["classroom"]:
                    append_to_delta(f"=== CLASSROOM COURSE: {course['name']} ===\nAssignment: {w.get('title', '')}\nDesc: {w.get('description', '')}")
                    _process_materials(w.get('materials', []))
                    state["classroom"].append(w_id)
        except Exception: pass
        
    save_state(state)

def export_all_canvas():
    logger.info("Exporting NEW Canvas data...")
    load_dotenv(os.path.join(BASE_DIR, '..', '.env'))
    API_URL = os.getenv("CANVAS_API_URL", "https://canvas.instructure.com")
    API_TOKEN = os.getenv("CANVAS_API_TOKEN")
    if not API_TOKEN: return
    
    canvas = Canvas(API_URL, API_TOKEN)
    user = canvas.get_current_user()
    courses = list(user.get_favorite_courses())
    state = load_state()
    
    for c in courses:
        try:
            for assgn in c.get_assignments():
                a_id = str(getattr(assgn, 'id', ''))
                if a_id and a_id not in state["canvas"]:
                    desc = getattr(assgn, 'description', '')
                    clean = ""
                    if desc:
                        import re
                        clean = re.sub('<[^<]+?>', '', desc)
                    append_to_delta(f"=== CANVAS COURSE: {c.name} ===\nAssignment: {getattr(assgn, 'name', '')} (Due: {getattr(assgn, 'due_at', '')})\nDescription: {clean}")
                    state["canvas"].append(a_id)
        except Exception: pass
        
        try:
            for page in c.get_pages():
                p_id = str(getattr(page, 'url', ''))
                if p_id and p_id not in state["canvas"]:
                    try:
                        p = c.get_page(p_id)
                        import re
                        clean = re.sub('<[^<]+?>', '', getattr(p, 'body', '') or '')
                        append_to_delta(f"=== CANVAS COURSE: {c.name} ===\nPage: {page.title}\n{clean}")
                        state["canvas"].append(p_id)
                    except Exception: pass
        except Exception: pass
        
    save_state(state)

def export_all_gmail():
    logger.info("Exporting NEW Gmail...")
    creds = get_google_creds()
    if not creds: return
    
    service = build('gmail', 'v1', credentials=creds)
    results = service.users().messages().list(userId='me', q='newer_than:30d', maxResults=500).execute()
    messages = results.get('messages', [])
    state = load_state()
    
    for msg in messages:
        m_id = msg['id']
        if m_id not in state["gmail"]:
            try:
                msg_data = service.users().messages().get(userId='me', id=m_id, format='metadata', metadataHeaders=['Subject', 'From', 'Date']).execute()
                headers = msg_data['payload']['headers']
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
                sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
                append_to_delta(f"Email from {sender}: {subject}")
                state["gmail"].append(m_id)
            except Exception: pass
            
    save_state(state)

def run_all_exports():
    if not os.path.exists(ARCHIVE_DIR):
        os.makedirs(ARCHIVE_DIR)
    # Add project root to sys path so google_scraper can be imported
    import sys
    sys.path.append(BASE_DIR)
    
    export_all_google_docs()
    export_all_classroom()
    export_all_canvas()
    export_all_gmail()
    logger.info("Historical Export Complete.")

if __name__ == "__main__":
    run_all_exports()
