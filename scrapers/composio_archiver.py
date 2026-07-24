"""
composio_archiver.py - Full archive of ALL data from Canvas, Classroom, Gmail, Drive, Docs, Calendar to HDD.

Exports literally everything accessible via Composio MCP to /mnt/bulk/data-archive/.
Incremental: tracks seen IDs so subsequent runs only add new content.
"""
import os, json, logging, http.client, re, datetime, time, subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# ── Paths ──
HDD_BASE = "/mnt/bulk"
ARCHIVE_DIR = os.path.join(HDD_BASE, "data-archive")
STATE_FILE = os.path.join(ARCHIVE_DIR, "export_state.json")
DELTA_FILE = os.path.join(ARCHIVE_DIR, "delta_export.txt")
CANVAS_FILE = os.path.join(ARCHIVE_DIR, "canvas_export.txt")
CLASSROOM_FILE = os.path.join(ARCHIVE_DIR, "classroom_export.txt")
GMAIL_FILE = os.path.join(ARCHIVE_DIR, "gmail_export.txt")
DOCS_FILE = os.path.join(ARCHIVE_DIR, "google_docs_export.txt")
DRIVE_FILE = os.path.join(ARCHIVE_DIR, "google_drive_export.txt")
CALENDAR_FILE = os.path.join(ARCHIVE_DIR, "calendar_export.txt")
SUMMARY_FILE = os.path.join(ARCHIVE_DIR, "archive_summary.json")
DOWNLOAD_DIR = os.path.join(ARCHIVE_DIR, "downloads")
DRIVE_DL_DIR = os.path.join(DOWNLOAD_DIR, "drive")
CLASSROOM_DL_DIR = os.path.join(DOWNLOAD_DIR, "classroom")
DOWNLOAD_STATE_FILE = os.path.join(ARCHIVE_DIR, "download_state.json")

COMPOSIO_TOKEN_PATH = os.path.expanduser("~/.hermes/mcp-tokens/composio.json")
ENTITY_CANVAS = "canvas_ionone-arided"

# ── Token helpers ──
_TOKEN_CACHE = None

def _get_token():
    global _TOKEN_CACHE
    if _TOKEN_CACHE is None:
        with open(COMPOSIO_TOKEN_PATH) as f:
            _TOKEN_CACHE = json.load(f).get("access_token")
    return _TOKEN_CACHE

def _call_mcp(tool_slug: str, params: dict = None, entity_id: str = "default") -> dict:
    """Direct Composio MCP call. Returns full response dict."""
    access_token = _get_token()
    if not access_token:
        return {"successful": False, "data": {"message": "No access token"}}

    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {
            "name": "COMPOSIO_MULTI_EXECUTE_TOOL",
            "arguments": {
                "tools": [{"tool_slug": tool_slug, "arguments": params or {}, "entityId": entity_id}],
                "thought": f"archive {tool_slug}",
                "sync_response_to_workbench": False
            }
        }
    }).encode()

    try:
        conn = http.client.HTTPSConnection("connect.composio.dev", timeout=300)
        conn.request("POST", "/mcp", body=payload, headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"
        })
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", errors="replace")
        conn.close()

        for line in text.split("\n"):
            if line.startswith("data: "):
                data = json.loads(line[6:])
                result_obj = data.get("result", {})
                for content in result_obj.get("content", []):
                    if content.get("type") == "text":
                        inner = json.loads(content["text"])
                        results_arr = inner.get("data", {}).get("results", [])
                        if results_arr:
                            return results_arr[0].get("response", {})
                        # Some tools nest differently
                        outer_data = inner.get("data", {})
                        if outer_data.get("results"):
                            return outer_data["results"][0].get("response", {})
                        return {"successful": False, "data": {"message": "no results in response"}, "raw": inner}
                return {"successful": False, "data": {"message": "no text content in SSE"}}
        return {"successful": False, "data": {"message": "no SSE data line"}}
    except Exception as e:
        logger.error(f"MCP call failed ({tool_slug}): {e}")
        return {"successful": False, "data": {"message": str(e)}}

def _extract_items(rd: dict, *keys: str) -> list:
    """Extract items from response using multiple possible key paths."""
    if not isinstance(rd, dict):
        return []
    # data_preview.key
    dp = rd.get("data_preview")
    if isinstance(dp, dict):
        for k in keys:
            val = dp.get(k)
            if isinstance(val, list):
                return val
    # data.key
    d = rd.get("data")
    if isinstance(d, dict):
        for k in keys:
            val = d.get(k)
            if isinstance(val, list):
                return val
        # data.response_data[]
        rd_val = d.get("response_data")
        if isinstance(rd_val, list):
            return rd_val
    # top-level keys
    for k in keys:
        val = rd.get(k)
        if isinstance(val, list):
            return val
    return []

def _is_ok(rd: dict) -> bool:
    return isinstance(rd, dict) and rd.get("successful") is True

def _strip_html(text):
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def _sanitize(s):
    """Remove characters that are problematic in filenames."""
    return re.sub(r'[\\/*?:"<>|]', '_', str(s))[:100]

def _write_export(filepath: str, blocks: list):
    """Write blocks to export file, overwriting each run (full snapshot)."""
    if not blocks:
        return
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        for b in blocks:
            f.write(b + "\n\n" + ("=" * 60) + "\n\n")

# ── Canvas: EVERYTHING ────────────────────────────────────────────────────

def export_canvas_full() -> dict:
    """Export ALL courses → ALL assignments, pages (full content), announcements,
       discussions, modules+items, groups, planner items."""
    logger.info("Canvas: fetching ALL courses...")
    rd = _call_mcp("CANVAS_LIST_COURSES", {"per_page": 100}, entity_id=ENTITY_CANVAS)
    if not _is_ok(rd):
        return {"error": "No courses", "counts": {}}
    courses = _extract_items(rd, "courses", "response_data")
    active = [c for c in courses
              if isinstance(c, dict) and c.get("name")
              and not c.get("access_restricted_by_date")]

    counts = {"courses": len(active), "assignments": 0, "pages": 0, "announcements": 0,
              "discussions": 0, "modules": 0, "module_items": 0, "groups": 0, "planner": 0}
    all_blocks = []
    seen = set()

    for course in active:
        cid = str(course["id"])
        cname = course["name"]
        logger.info(f"  Canvas course: {cname}")

        # ── Assignments ──
        rd_a = _call_mcp("CANVAS_GET_ALL_ASSIGNMENTS", {"course_id": cid}, entity_id=ENTITY_CANVAS)
        if _is_ok(rd_a):
            items = _extract_items(rd_a, "assignments", "response_data")
            for a in (items if isinstance(items, list) else []):
                if not isinstance(a, dict): continue
                key = f"canvas_assign_{cid}_{a.get('id','')}"
                if key in seen: continue
                seen.add(key)
                counts["assignments"] += 1
                desc = _strip_html(a.get("description", ""))[:2000]
                due = a.get("due_at", a.get("dueDate", ""))
                all_blocks.append(
                    f"COURSE: {cname}\nTYPE: Assignment\n"
                    f"Title: {a.get('name') or a.get('title') or 'Untitled'}\n"
                    f"Due: {due}\nPoints: {a.get('points_possible', a.get('maxPoints',''))}\n"
                    f"Status: {a.get('state','')}\n"
                    f"Description: {desc}"
                )

        # ── Pages (FULL content) ──
        rd_p = _call_mcp("CANVAS_LIST_PAGES_FOR_COURSE", {"course_id": cid}, entity_id=ENTITY_CANVAS)
        if _is_ok(rd_p):
            items = _extract_items(rd_p, "pages", "response_data")
            for p in (items if isinstance(items, list) else []):
                if not isinstance(p, dict): continue
                p_url = p.get("url", str(p.get("id", "")))
                key = f"canvas_page_{cid}_{p_url}"
                if key in seen: continue
                seen.add(key)
                counts["pages"] += 1
                # Try to get full page body
                rd_body = _call_mcp("CANVAS_GET_PAGE", {"course_id": cid, "url_or_id": p_url}, entity_id=ENTITY_CANVAS)
                body_content = ""
                if _is_ok(rd_body):
                    body_items = _extract_items(rd_body, "body", "response_data")
                    if body_items and isinstance(body_items, list):
                        body_content = _strip_html(str(body_items[0]))[:3000]
                all_blocks.append(
                    f"COURSE: {cname}\nTYPE: Page\n"
                    f"Title: {p.get('title','Untitled')}\n"
                    f"URL: https://forsyth.instructure.com/courses/{cid}/pages/{p_url}\n"
                    f"Content: {body_content}"
                )

        # ── Announcements ──
        rd_an = _call_mcp("CANVAS_LIST_ANNOUNCEMENTS",
                          {"context_codes": [f"course_{cid}"], "per_page": 100},
                          entity_id=ENTITY_CANVAS)
        if _is_ok(rd_an):
            items = _extract_items(rd_an, "announcements", "response_data")
            for a in (items if isinstance(items, list) else []):
                if not isinstance(a, dict): continue
                key = f"canvas_ann_{cid}_{a.get('id','')}"
                if key in seen: continue
                seen.add(key)
                counts["announcements"] += 1
                msg = _strip_html(a.get("message", ""))[:2000]
                all_blocks.append(
                    f"COURSE: {cname}\nTYPE: Announcement\n"
                    f"Title: {a.get('title','Untitled')}\n"
                    f"Posted: {a.get('posted_at', a.get('created_at',''))}\n"
                    f"Message: {msg}"
                )

        # ── Discussions ──
        rd_d = _call_mcp("CANVAS_LIST_DISCUSSIONS", {"course_id": cid}, entity_id=ENTITY_CANVAS)
        if _is_ok(rd_d):
            items = _extract_items(rd_d, "discussions", "response_data")
            for d in (items if isinstance(items, list) else []):
                if not isinstance(d, dict): continue
                key = f"canvas_disc_{cid}_{d.get('id','')}"
                if key in seen: continue
                seen.add(key)
                counts["discussions"] += 1
                msg = _strip_html(d.get("message", ""))[:1000]
                all_blocks.append(
                    f"COURSE: {cname}\nTYPE: Discussion\n"
                    f"Title: {d.get('title', d.get('name','Untitled'))}\n"
                    f"Message: {msg}"
                )

        # ── Modules + Items ──
        rd_m = _call_mcp("CANVAS_LIST_MODULES", {"course_id": cid}, entity_id=ENTITY_CANVAS)
        if _is_ok(rd_m):
            items = _extract_items(rd_m, "modules", "response_data")
            for mod in (items if isinstance(items, list) else []):
                if not isinstance(mod, dict): continue
                counts["modules"] += 1
                mod_name = mod.get("name", "Untitled Module")
                mod_id = mod.get("id", "")
                all_blocks.append(
                    f"COURSE: {cname}\nTYPE: Module\n"
                    f"Name: {mod_name}\n"
                    f"Position: {mod.get('position','')}"
                )
                # Items within this module
                rd_mi = _call_mcp("CANVAS_LIST_MODULE_ITEMS",
                                  {"course_id": cid, "module_id": str(mod_id)},
                                  entity_id=ENTITY_CANVAS)
                if _is_ok(rd_mi):
                    mitems = _extract_items(rd_mi, "items", "response_data")
                    for mi in (mitems if isinstance(mitems, list) else []):
                        if not isinstance(mi, dict): continue
                        counts["module_items"] += 1
                        all_blocks.append(
                            f"COURSE: {cname} > Module: {mod_name}\nTYPE: Module Item\n"
                            f"Title: {mi.get('title','Untitled')}\n"
                            f"Type: {mi.get('type','')}\n"
                            f"URL: {mi.get('html_url', mi.get('url',''))}"
                        )

        # ── Groups ──
        rd_g = _call_mcp("CANVAS_LIST_GROUPS", {"course_id": cid}, entity_id=ENTITY_CANVAS)
        if _is_ok(rd_g):
            items = _extract_items(rd_g, "groups", "response_data")
            for g in (items if isinstance(items, list) else []):
                if not isinstance(g, dict): continue
                key = f"canvas_group_{cid}_{g.get('id','')}"
                if key in seen: continue
                seen.add(key)
                counts["groups"] += 1
                all_blocks.append(
                    f"COURSE: {cname}\nTYPE: Group\n"
                    f"Name: {g.get('name','')}\n"
                    f"Members: {g.get('members_count', g.get('members_count',''))}\n"
                    f"Description: {g.get('description','')[:500]}"
                )

    # ── Planner items (user-level, not per-course) ──
    rd_pl = _call_mcp("CANVAS_LIST_PLANNER_ITEMS", {}, entity_id=ENTITY_CANVAS)
    if _is_ok(rd_pl):
        items = _extract_items(rd_pl, "planner", "response_data")
        for pl in (items if isinstance(items, list) else []):
            if not isinstance(pl, dict): continue
            counts["planner"] += 1
            all_blocks.append(
                f"TYPE: Planner Item\n"
                f"Title: {pl.get('plannable_title','')}\n"
                f"Date: {pl.get('plannable_date','')}\n"
                f"Type: {pl.get('plannable_type','')}"
            )

    _write_export(CANVAS_FILE, all_blocks)
    logger.info(f"Canvas done: {json.dumps(counts)}")
    return counts

# ── Google Classroom: EVERYTHING ──────────────────────────────────────────

def export_classroom_full() -> dict:
    """Export ALL Classroom courses → announcements, coursework, materials."""
    logger.info("Classroom: fetching ALL courses...")
    rd = _call_mcp("GOOGLE_CLASSROOM_COURSES_LIST", {})
    if not _is_ok(rd):
        return {"error": "No courses", "counts": {}}
    courses = _extract_items(rd, "courses")

    counts = {"courses": len(courses), "announcements": 0, "coursework": 0, "materials": 0}
    all_blocks = []
    seen = set()

    for course in courses:
        if not isinstance(course, dict): continue
        cid = course.get("id", "")
        cname = course.get("name", "Unknown")
        if not cid: continue
        logger.info(f"  Classroom course: {cname}")

        # ── Announcements ──
        rd_a = _call_mcp("GOOGLE_CLASSROOM_ANNOUNCEMENTS_LIST", {"course_id": cid})
        if _is_ok(rd_a):
            items = _extract_items(rd_a, "announcements")
            for a in (items if isinstance(items, list) else []):
                if not isinstance(a, dict): continue
                key = f"classroom_ann_{a.get('id','')}"
                if key in seen: continue
                seen.add(key)
                counts["announcements"] += 1
                all_blocks.append(
                    f"COURSE: {cname}\nTYPE: Announcement\n"
                    f"Text: {a.get('text','')[:2000]}\n"
                    f"Time: {a.get('creationTime','')}"
                )

        # ── Course Work (assignments) ──
        rd_w = _call_mcp("GOOGLE_CLASSROOM_COURSE_WORK_LIST", {"course_id": cid})
        if _is_ok(rd_w):
            items = _extract_items(rd_w, "courseWork")
            for w in (items if isinstance(items, list) else []):
                if not isinstance(w, dict): continue
                key = f"classroom_cw_{w.get('id','')}"
                if key in seen: continue
                seen.add(key)
                counts["coursework"] += 1
                due = w.get("dueDate", {})
                due_str = f"{due.get('year','')}-{due.get('month','')}-{due.get('day','')}" if isinstance(due, dict) else str(due)
                all_blocks.append(
                    f"COURSE: {cname}\nTYPE: CourseWork\n"
                    f"Title: {w.get('title','Untitled')}\n"
                    f"Due: {due_str}\n"
                    f"Max Points: {w.get('maxPoints','')}\n"
                    f"State: {w.get('state','')}\n"
                    f"Description: {w.get('description','')[:2000]}"
                )
                # Materials in coursework
                for mat in (w.get("materials") or []):
                    if not isinstance(mat, dict): continue
                    drive_file = mat.get("driveFile", {})
                    if isinstance(drive_file, dict):
                        drive_file = drive_file.get("driveFile", {})
                    else:
                        drive_file = {}
                    youtube = mat.get("youtubeVideo", {})
                    if not isinstance(youtube, dict): youtube = {}
                    link = mat.get("link", {})
                    if not isinstance(link, dict): link = {}
                    counts["materials"] += 1
                    mat_name = drive_file.get("title", youtube.get("title", link.get("title", "Material")))
                    mat_url = drive_file.get("alternateLink", youtube.get("alternateLink", link.get("url", "")))
                    all_blocks.append(
                        f"COURSE: {cname} > {w.get('title','')}\nTYPE: Material\n"
                        f"Name: {mat_name}\nURL: {mat_url}"
                    )

    _write_export(CLASSROOM_FILE, all_blocks)
    logger.info(f"Classroom done: {json.dumps(counts)}")
    return counts

# ── Gmail: ALL emails ─────────────────────────────────────────────────────

def export_gmail_full() -> dict:
    """Export Gmail messages (single batch of 50)."""
    logger.info("Gmail: fetching messages...")
    counts = {"emails": 0}
    all_blocks = []
    seen = set()

    rd = _call_mcp("GMAIL_FETCH_EMAILS", {"max_results": 50})
    if _is_ok(rd):
        items = _extract_items(rd, "messages")
        for msg in (items if isinstance(items, list) else []):
            if not isinstance(msg, dict): continue
            msg_id = msg.get("messageId", msg.get("id", ""))
            if msg_id and msg_id in seen: continue
            if msg_id: seen.add(msg_id)
            counts["emails"] += 1
            snippet = (msg.get("messageText", "") or "")[:2000]
            all_blocks.append(
                f"TYPE: Email\n"
                f"From: {msg.get('sender', msg.get('from',''))}\n"
                f"To: {msg.get('to', msg.get('To',''))}\n"
                f"Date: {msg.get('messageTimestamp', msg.get('date',''))}\n"
                f"Subject: {msg.get('subject', msg.get('Subject','(no subject)'))}\n"
                f"Labels: {', '.join(msg.get('labelIds', [])) if isinstance(msg.get('labelIds'), list) else msg.get('labelIds','')}\n"
                f"Body:\n{snippet}"
            )

    _write_export(GMAIL_FILE, all_blocks)
    logger.info(f"Gmail done: {json.dumps(counts)}")
    return counts

# ── Google Drive: ALL files ───────────────────────────────────────────────

def export_drive_full() -> dict:
    """Export ALL Google Drive files listing (name, type, size, modified)."""
    logger.info("Drive: fetching ALL files...")
    counts = {"files": 0, "folders": 0}
    all_blocks = []
    seen = set()
    page_token = None

    for page in range(20):  # max 20 pages of 100 = 2000 files
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        rd = _call_mcp("GOOGLEDRIVE_LIST_FILES", params)
        if not _is_ok(rd):
            break
        items = _extract_items(rd, "files", "response_data")
        if not items:
            break
        for f in (items if isinstance(items, list) else []):
            if not isinstance(f, dict): continue
            fid = f.get("id", "")
            if fid in seen: continue
            seen.add(fid)
            mime = f.get("mimeType", "")
            if mime == "application/vnd.google-apps.folder":
                counts["folders"] += 1
            else:
                counts["files"] += 1
            parents = f.get("parents", [])
            parent_path = str(parents) if parents else ""
            all_blocks.append(
                f"TYPE: {'Folder' if mime == 'application/vnd.google-apps.folder' else 'File'}\n"
                f"Name: {f.get('name','')}\n"
                f"ID: {fid}\n"
                f"MIME: {mime}\n"
                f"Size: {f.get('size','N/A')}\n"
                f"Modified: {f.get('modifiedTime','')}\n"
                f"Created: {f.get('createdTime','')}\n"
                f"Parents: {parent_path}\n"
                f"WebLink: {f.get('webViewLink', f.get('alternateLink',''))}"
            )
        # Drive pagination via raw response
        d = rd.get("data_preview") or rd.get("data") or {}
        if isinstance(d, dict) and d.get("nextPageToken"):
            page_token = d["nextPageToken"]
        else:
            break

    _write_export(DRIVE_FILE, all_blocks)
    logger.info(f"Drive done: {json.dumps(counts)}")
    return counts

# ── Google Docs: ALL docs with full content ───────────────────────────────

def export_docs_full() -> dict:
    """Export ALL Google Docs with full plaintext content."""
    logger.info("Docs: fetching ALL documents...")
    counts = {"docs": 0}
    all_blocks = []
    seen = set()

    rd = _call_mcp("GOOGLEDRIVE_LIST_FILES", {
        "page_size": 200,
        "q": "mimeType='application/vnd.google-apps.document' and trashed=false"
    })
    if not _is_ok(rd):
        return {"error": "No Drive query", "counts": {}}
    items = _extract_items(rd, "files", "response_data")
    if not items:
        return {"counts": counts}

    for item in (items if isinstance(items, list) else []):
        if not isinstance(item, dict): continue
        doc_id = item.get("id", "")
        doc_name = item.get("name", "Untitled")
        if not doc_id or doc_id in seen: continue
        seen.add(doc_id)
        counts["docs"] += 1
        logger.info(f"  Doc: {doc_name}")

        # Fetch full plaintext
        rd_doc = _call_mcp("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", {"document_id": doc_id})
        text = ""
        if _is_ok(rd_doc):
            result = _extract_items(rd_doc, "plainText", "content", "response_data")
            if isinstance(result, list):
                text = " ".join(str(x) for x in result if isinstance(x, str))
            elif isinstance(result, str):
                text = result
        preview = text[:5000] if text else "(empty or inaccessible)"
        all_blocks.append(
            f"TYPE: Google Doc\n"
            f"Title: {doc_name}\n"
            f"ID: {doc_id}\n"
            f"Modified: {item.get('modifiedTime','')}\n"
            f"Content ({len(text)} chars):\n{preview}"
        )

    _write_export(DOCS_FILE, all_blocks)
    logger.info(f"Docs done: {json.dumps(counts)}")
    return counts

# ── Calendar: ALL events ──────────────────────────────────────────────────

def export_calendar_full() -> dict:
    """Export ALL Google Calendar events."""
    logger.info("Calendar: fetching ALL events...")
    counts = {"events": 0}
    all_blocks = []
    seen = set()

    # Get events from last 2 years to cover everything
    rd = _call_mcp("GOOGLECALENDAR_EVENTS_LIST", {
        "max_results": 250,
        "time_min": "2024-01-01T00:00:00Z",
        "time_max": "2027-01-01T00:00:00Z"
    })
    if not _is_ok(rd):
        return {"error": "No events", "counts": {}}
    items = _extract_items(rd, "items")
    for ev in (items if isinstance(items, list) else []):
        if not isinstance(ev, dict): continue
        eid = ev.get("id", "")
        if eid in seen: continue
        seen.add(eid)
        counts["events"] += 1
        when = ev.get("start", {}).get("dateTime", ev.get("start", {}).get("date", ""))
        desc = _strip_html(ev.get("description", ""))[:1000]
        all_blocks.append(
            f"TYPE: Calendar Event\n"
            f"Summary: {ev.get('summary','(no title)')}\n"
            f"When: {when}\n"
            f"End: {ev.get('end', {}).get('dateTime', ev.get('end', {}).get('date', ''))}\n"
            f"Location: {ev.get('location','')}\n"
            f"Creator: {ev.get('creator',{}).get('email','')}\n"
            f"Description: {desc}"
        )

    _write_export(CALENDAR_FILE, all_blocks)
    logger.info(f"Calendar done: {json.dumps(counts)}")
    return counts

# ── Bot relevance & Drive file downloads ─────────────────────────────────
# Keywords/phrases that indicate a file is relevant to the bot's known subjects.
# Derived from Canvas course names and the bot's topic areas.
_RELEVANT_KEYWORDS = re.compile(
    r"(?i)(?:^|[\s\-_])("
    r"HOSA|SAT[^A-Z]|ACT(?![A-Z])|AP(?=\s|\d|$|_)|Robotics|Robot(?![A-Za-z])|"
    r"Physics(?![A-Za-z])|TSA(?![A-Za-z])|SkillsUSA|Investment|CyberPatriots|Cyberpatriots|"
    r"Geometry|Algebra|Calculus|Statistics|Polynomial|Science.?Fair|"
    r"Health|Syllabus|Pacing.?Guide|College.?Application|"
    r"Competition|Competitions|Intern|Research|STEM|"
    r"Study.?Guide|Flashcard|Quizlet|"
    r"STAPH.?ARM|N.?Gram|Orientation|"
    r"Odyssey|Greek|Summer.*Health|"
    r"BBC(?=[_\- ])|Poster|Flyer|"
    r"Gamma_bot|Text_Generation|"
    r"Supply\s*List|Score\s*Card|Field\s*Day|"
    r"Wax\s*Museum|Reflexive\s*Verbs|Black\s*Ships|"
    r"Characteristic.?of\s*Life|Biotic|"
    r"Sicilian\s*Defence|Manual.?of.?Chess"
    r")(?:$|[\s\-_])"
)

# Runtime set populated by _collect_contextual_file_ids() from Classroom
# coursework titles. Used as additional keywords in _is_relevant_file().
_CLASSROOM_COURSEWORK_KEYWORDS: set = set()

# Patterns that reliably indicate a file is NOT relevant (entertainment, VMs, etc.)
_BOT_ANTI_KEYWORDS = re.compile(
    r"(?i)\b("
    r"The Apothecary Diaries|Kimetsu|"
    r"msf24_|Nevermore|"
    r"Beyond Journey|Aperture Science|"
    r"Narnia Server|Alphabet Soup|"
    r"S\d{2}E\d{2}"  # TV episode patterns
    r")\b"
)

_MIME_EXPORT = {
    "application/vnd.google-apps.document": ("text/markdown", ".md"),
    "application/vnd.google-apps.spreadsheet": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
}

_MIME_EXT = {
    "application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg",
    "image/gif": ".gif", "text/plain": ".txt", "text/csv": ".csv",
    "application/zip": ".zip", "application/x-zip-compressed": ".zip",
    "application/x-7z-compressed": ".7z", "application/x-compressed": ".gz",
    "video/x-matroska": ".mkv", "video/mp4": ".mp4", "video/mpeg": ".mpeg",
    "video/webm": ".webm", "application/octet-stream": ".bin",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}

def _load_download_state() -> dict:
    if os.path.exists(DOWNLOAD_STATE_FILE):
        try:
            with open(DOWNLOAD_STATE_FILE) as f:
                return json.load(f)
        except: pass
    return {"drive": {}, "classroom": {}}

def _save_download_state(state: dict):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    with open(DOWNLOAD_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def _mime_ext(mime: str) -> str:
    return _MIME_EXT.get(mime, "." + mime.split("/")[-1] if "/" in mime else ".bin")

def _download_s3(s3url: str, out_path: str) -> int:
    """Download from S3 presigned URL, return bytes. Returns -1 on error."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", out_path, "-w", "%{http_code}", s3url],
            capture_output=True, text=True, timeout=300
        )
        if result.stdout.strip() == "200" and os.path.exists(out_path):
            return os.path.getsize(out_path)
        return -1
    except Exception as e:
        logger.warning(f"  Download failed: {e}")
        return -1


def _is_relevant_file(item: dict) -> bool:
    """Determine if a Drive file is relevant to the bot's subjects.

    Checks:
      1. Source linkage — file ID appears in Classroom/Gmail contextual set
      2. Filename keyword match against known bot subjects (_BOT_KEYWORDS)
      3. Anti-pattern filter to skip obvious entertainment/VM files

    Returns True if the file should be downloaded.
    """
    fname = item.get("name", "")
    fid = item.get("id", "")
    mime = item.get("mimeType", "")

    # Never download folders
    if mime == "application/vnd.google-apps.folder":
        return False

    # Skip obvious non-relevant files (anime, VM images, TV episodes)
    if _BOT_ANTI_KEYWORDS.search(fname):
        logger.debug(f"  SKIP (anti-keyword): {fname}")
        return False

    # Check filename against known bot subject keywords
    if _RELEVANT_KEYWORDS.search(fname):
        logger.debug(f"  RELEVANT (keyword match): {fname}")
        return True

    # Check against Classroom coursework titles (runtime-populated)
    if _CLASSROOM_COURSEWORK_KEYWORDS:
        fname_lower = fname.lower()
        for cw_keyword in _CLASSROOM_COURSEWORK_KEYWORDS:
            if cw_keyword in fname_lower or fname_lower in cw_keyword:
                logger.debug(f"  RELEVANT (coursework match): {fname}")
                return True

    # Google-native docs/slides/sheets that aren't obviously entertainment
    # are often educational — include them if they pass the anti-keyword filter
    if mime.startswith("application/vnd.google-apps."):
        logger.debug(f"  RELEVANT (Google-native): {fname}")
        return True

    # PDFs are almost always educational documents
    if mime == "application/pdf":
        logger.debug(f"  RELEVANT (PDF): {fname}")
        return True

    return False


def _collect_contextual_file_ids() -> set:
    """Collect Drive file IDs referenced in bot-relevant sources.

    Sources:
      1. Classroom coursework materials — checks driveFile attachments
         (Note: Composition MCP truncates nested driveFile objects to '{object}'
         server-side, so we also do title-to-filename matching as fallback)
      2. Gmail messages — looks for drive.google.com URLs in message text
    """
    ids = set()
    coursework_titles = set()
    creds = None

    # ── 1. Classroom coursework materials (driveFile attachments) ──
    # Use direct Google API because MCP truncates driveFile objects
    try:
        import sys
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from scrapers.google_scraper import get_google_credentials
        from googleapiclient.discovery import build

        creds = get_google_credentials()
        if creds:
            service = build('classroom', 'v1', credentials=creds)
            courses_result = service.courses().list(courseStates=['ACTIVE']).execute()
            courses = courses_result.get('courses', [])

            for course in courses:
                if not isinstance(course, dict):
                    continue
                cid = course.get('id')
                if not cid:
                    continue

                try:
                    cw_result = service.courses().courseWork().list(
                        courseId=cid, courseWorkStates=['PUBLISHED'],
                        orderBy='updateTime desc', pageSize=50
                    ).execute()
                except Exception:
                    continue

                for w in (cw_result.get('courseWork', []) or []):
                    if not isinstance(w, dict):
                        continue
                    cw_title = w.get('title', '')
                    if cw_title:
                        coursework_titles.add(cw_title)

                    for mat in (w.get('materials', []) or []):
                        if not isinstance(mat, dict):
                            continue
                        drive_file_wrapper = mat.get('driveFile')
                        if not isinstance(drive_file_wrapper, dict):
                            continue
                        df = drive_file_wrapper.get('driveFile', {})
                        if isinstance(df, dict) and df.get('id'):
                            ids.add(df['id'])
    except ImportError as e:
        logger.info(f"  Cannot use direct Google API: {e}")
    except Exception as e:
        logger.info(f"  Classroom API error: {e}")

    # ── 1b. Fallback: coursework titles as keywords ──
    # MCP truncates driveFile objects to '{object}' server-side, so we also
    # add coursework title words to the keyword registry so the relevance
    # filter in download_drive_files() catches them by name.
    for t in coursework_titles:
        if t:
            _CLASSROOM_COURSEWORK_KEYWORDS.add(t.lower())

    # ── 2. Gmail: look for Drive links in message text ──
    rd_g = _call_mcp("GMAIL_FETCH_EMAILS", {"max_results": 50})
    if _is_ok(rd_g):
        for msg in (_extract_items(rd_g, "messages") or []):
            if not isinstance(msg, dict):
                continue
            text = msg.get("messageText", "") or ""
            for m in re.finditer(
                r'drive\.google\.com/(?:file/d/|open\?id=)([a-zA-Z0-9_-]+)',
                text
            ):
                ids.add(m.group(1))

    logger.info(f"Contextual file IDs collected: {len(ids)}"
                f"{' (' + ', '.join(sorted(ids))[:200] + ')' if ids else ''}")
    return ids


def download_drive_files(contextual_ids: set = None) -> dict:
    """Download Drive files relevant to the bot's subject areas.

    Relevance is determined by:
      - Source linkage: IDs from Classroom/Gmail contexts (exact match)
      - Keyword match: filenames matching known bot subjects
      - MIME type: educational file types (PDFs, Docs, Slides, etc.)

    Files that fail relevance checks (anime MKVs, VM images, etc.) are skipped.
    """
    logger.info("Drive: downloading relevant files...")
    state = _load_download_state()
    drive_state = state.get("drive", {})
    os.makedirs(DRIVE_DL_DIR, exist_ok=True)

    # Collect all files via pagination
    all_files = []
    page_token = None
    for page in range(20):
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        rd = _call_mcp("GOOGLEDRIVE_LIST_FILES", params)
        if not _is_ok(rd):
            break
        items = _extract_items(rd, "files", "response_data") or []
        if not items:
            break
        all_files.extend(items)
        d = rd.get("data_preview") or rd.get("data") or {}
        if isinstance(d, dict) and d.get("nextPageToken"):
            page_token = d["nextPageToken"]
        else:
            break

    # Apply relevance filter
    contextual_ids = contextual_ids or set()
    dl_files = []
    skip_reasons = {"anti_keyword": 0, "not_educational": 0, "contextual": 0}
    for f in all_files:
        if not isinstance(f, dict) or not f.get("id"):
            continue
        if f["id"] in contextual_ids:
            dl_files.append(f)
            skip_reasons["contextual"] += 1
            continue
        if _is_relevant_file(f):
            dl_files.append(f)

    logger.info(f"  Drive listing: {len(all_files)} total, "
                f"{len(dl_files)} relevant for download "
                f"({len(all_files) - len(dl_files) - 1} filtered out)")

    if len(dl_files) == 0:
        return {"total": 0, "downloaded": 0, "skipped": 0, "errors": 0,
                "bytes": 0, "elapsed_seconds": 0}

    counts = {"total": len(dl_files), "downloaded": 0, "skipped": 0, "errors": 0,
              "bytes": 0, "elapsed_seconds": 0}

    for idx, item in enumerate(dl_files):
        fid = item["id"]
        fname = item.get("name", "unnamed")
        mime = item.get("mimeType", "")

        cached = drive_state.get(fid)
        if cached and cached.get("name") == fname:
            counts["skipped"] += 1
            continue

        export_info = _MIME_EXPORT.get(mime)
        params = {"file_id": fid}
        if export_info:
            params["mime_type"] = export_info[0]
            ext = export_info[1]
        else:
            ext = _mime_ext(mime)

        logger.info(f"  [{idx+1}/{len(dl_files)}] {fname}{ext}")
        t0 = time.time()
        rd = _call_mcp("GOOGLEDRIVE_DOWNLOAD_FILE", params)
        t1 = time.time()

        if not _is_ok(rd):
            rd = _call_mcp("GOOGLEDRIVE_DOWNLOAD_FILE", {"file_id": fid})
            if not _is_ok(rd):
                logger.warning(f"    FAILED ({t1-t0:.1f}s)")
                counts["errors"] += 1
                continue

        dl_info = rd.get("data", {}).get("downloaded_file_content", {})
        s3url = dl_info.get("s3url", "")
        if not s3url:
            logger.warning(f"    NO URL")
            counts["errors"] += 1
            continue

        actual_mime = dl_info.get("mimetype", mime)
        if actual_mime == "text/markdown":
            actual_ext = ".md"
        elif actual_mime == "application/pdf":
            actual_ext = ".pdf"
        else:
            actual_ext = _mime_ext(actual_mime)

        safe_name = fname.replace("/", "_").replace(" ", "_")[:100] + actual_ext
        out_path = os.path.join(DRIVE_DL_DIR, safe_name)

        t2 = time.time()
        size = _download_s3(s3url, out_path)
        t3 = time.time()

        if size > 0:
            counts["downloaded"] += 1
            counts["bytes"] += size
            drive_state[fid] = {"name": fname, "size": size,
                                "downloaded_at": datetime.datetime.now().isoformat()}
            logger.info(f"    OK {size:,} bytes ({t1-t0:.1f}s+{t3-t2:.1f}s)")
        else:
            logger.warning(f"    CURL FAILED")
            counts["errors"] += 1

        if counts["downloaded"] % 10 == 0 and counts["downloaded"] > 0:
            state["drive"] = drive_state
            _save_download_state(state)

    state["drive"] = drive_state
    _save_download_state(state)
    counts["elapsed_seconds"] = round(counts["bytes"] / 1024 / 1024 * 0)

    logger.info(f"Drive downloads done: {json.dumps(counts)}")
    return counts


def download_classroom_files() -> dict:
    """Download files attached to Google Classroom coursework.

    Uses direct Google API (not MCP) because the Composio MCP truncates
    nested driveFile objects to '{object}' server-side. This method gets
    the full file IDs, names, and mime types.
    """
    logger.info("Classroom: downloading attachments (direct API)...")
    state = _load_download_state()
    cls_state = state.get("classroom", {})
    os.makedirs(CLASSROOM_DL_DIR, exist_ok=True)

    try:
        import sys
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        # import the working functions from the self-made scraper
        from scrapers.google_scraper import get_google_credentials, download_drive_file
    except ImportError:
        logger.error("  Cannot import google_scraper — skipping Classroom downloads")
        return {"downloaded": 0, "skipped": 0, "errors": 0, "bytes": 0, "error": "Import failed"}

    creds = get_google_credentials()
    if not creds:
        logger.error("  No Google credentials — skipping Classroom downloads")
        return {"downloaded": 0, "skipped": 0, "errors": 0, "bytes": 0, "error": "No credentials"}

    counts = {"downloaded": 0, "skipped": 0, "errors": 0, "bytes": 0}

    try:
        from googleapiclient.discovery import build
        service = build('classroom', 'v1', credentials=creds)
        courses_result = service.courses().list(courseStates=['ACTIVE']).execute()
        courses = courses_result.get('courses', [])
    except Exception as e:
        logger.error(f"  Failed to build Classroom service: {e}")
        return {"downloaded": 0, "skipped": 0, "errors": 0, "bytes": 0, "error": str(e)}

    for course in courses:
        if not isinstance(course, dict):
            continue
        cid = course.get('id', '')
        cname = course.get('name', 'Unknown')
        if not cid:
            continue

        safe_cname = cname.replace("/", "_").replace(" ", "_")[:50]
        course_dir = os.path.join(CLASSROOM_DL_DIR, safe_cname)
        os.makedirs(course_dir, exist_ok=True)

        try:
            cw_result = service.courses().courseWork().list(
                courseId=cid, courseWorkStates=['PUBLISHED'],
                orderBy='updateTime desc', pageSize=50
            ).execute()
        except Exception as e:
            logger.warning(f"  {cname}: coursework list failed: {e}")
            continue

        for w in (cw_result.get('courseWork', []) or []):
            if not isinstance(w, dict):
                continue
            title = w.get('title', 'untitled')
            safe_cw = title.replace("/", "_").replace(" ", "_")[:50]
            cw_dir = os.path.join(course_dir, safe_cw)
            os.makedirs(cw_dir, exist_ok=True)

            for mat in (w.get('materials', []) or []):
                if not isinstance(mat, dict):
                    continue

                # Extract driveFile — Google API returns full objects, NOT truncated
                drive_file_wrapper = mat.get('driveFile')
                if not isinstance(drive_file_wrapper, dict):
                    continue
                df = drive_file_wrapper.get('driveFile', {})
                if not isinstance(df, dict):
                    continue
                fid = df.get('id')
                if not fid:
                    continue
                fname = df.get('title', 'unnamed')
                fmime = df.get('mimeType', '')

                # Skip if already downloaded
                cached = cls_state.get(fid)
                if cached and cached.get('name') == fname:
                    counts['skipped'] += 1
                    continue

                # Determine output path
                ext = '.pdf'
                if fmime == 'application/pdf':
                    ext = '.pdf'
                elif fmime.startswith('application/vnd.google-apps.'):
                    ext = '.pdf'
                elif fmime.startswith('image/'):
                    ext = '.' + fmime.split('/')[-1]
                elif fmime.startswith('video/'):
                    ext = '.' + fmime.split('/')[-1].replace('x-', '')
                elif fmime == 'text/csv':
                    ext = '.csv'

                safe_fname = fname.replace("/", "_").replace(" ", "_")[:100] + ext
                out_path = os.path.join(cw_dir, safe_fname)

                if os.path.exists(out_path):
                    counts['skipped'] += 1
                    continue

                logger.info(f"  Classroom DL: {cname} / {title} / {fname}")
                success = download_drive_file(fid, out_path)
                if success:
                    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
                    counts['downloaded'] += 1
                    counts['bytes'] += size
                    cls_state[fid] = {
                        'name': fname, 'mimeType': fmime, 'size': size,
                        'downloaded_at': datetime.datetime.now().isoformat()
                    }
                else:
                    logger.warning(f"    FAILED: {fname}")
                    counts['errors'] += 1

                # Save state periodically
                if counts['downloaded'] % 5 == 0 and counts['downloaded'] > 0:
                    state['classroom'] = cls_state
                    _save_download_state(state)

    state['classroom'] = cls_state
    _save_download_state(state)

    logger.info(f"Classroom downloads done: {json.dumps(counts)}")
    return counts


# ── Main orchestrator ─────────────────────────────────────────────────────

def run_full_archive() -> dict:
    """Run ALL exporters and return summary."""
    start = time.time()
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    results = {}
    results["canvas"] = export_canvas_full()
    results["classroom"] = export_classroom_full()
    results["gmail"] = export_gmail_full()
    results["drive"] = export_drive_full()
    results["docs"] = export_docs_full()
    results["calendar"] = export_calendar_full()

    # File downloads — ONLY contextually-linked files.
    # _collect_contextual_file_ids() scans Classroom coursework materials,
    # Canvas module items, and Gmail messages for Drive file references.
    contextual_ids = _collect_contextual_file_ids()
    results["drive_downloads"] = download_drive_files(contextual_ids)
    results["classroom_downloads"] = download_classroom_files()

    elapsed = time.time() - start

    # Build summary
    summary = {
        "timestamp": datetime.datetime.now().isoformat(),
        "duration_seconds": round(elapsed, 1),
        "sources": {k: v for k, v in results.items() if not k.endswith("_downloads")},
        "downloads": {k: v for k, v in results.items() if k.endswith("_downloads")}
    }
    with open(SUMMARY_FILE, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'='*60}")
    print(f"ARCHIVE COMPLETE ({round(elapsed, 1)}s)")
    print(f"{'='*60}")
    for source, data in results.items():
        if isinstance(data, dict):
            parts = [f"{k}={v}" for k, v in data.items() if k != "error" and not isinstance(v, dict)]
            print(f"  {source}: {', '.join(parts)}")
            if data.get("error"):
                print(f"    ERROR: {data['error']}")
        else:
            print(f"  {source}: {data}")
    print(f"\nFiles on HDD ({ARCHIVE_DIR}):")
    for fname in sorted(os.listdir(ARCHIVE_DIR)):
        if fname.endswith(".txt") or fname.endswith(".json"):
            fpath = os.path.join(ARCHIVE_DIR, fname)
            print(f"  {fname}: {os.path.getsize(fpath)} bytes")
    # Show download directory sizes
    dl_drive = os.path.join(ARCHIVE_DIR, "downloads", "drive")
    dl_classroom = os.path.join(ARCHIVE_DIR, "downloads", "classroom")
    if os.path.exists(dl_drive):
        total = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fn in os.walk(dl_drive) for f in fn) if os.path.exists(dl_drive) else 0
        count = len([f for f in os.listdir(dl_drive) if os.path.isfile(os.path.join(dl_drive, f))]) if os.path.exists(dl_drive) else 0
        print(f"  downloads/drive/: {count} files, {total:,} bytes")
    if os.path.exists(dl_classroom):
        total_c = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fn in os.walk(dl_classroom) for f in fn) if os.path.exists(dl_classroom) else 0
        print(f"  downloads/classroom/: {total_c:,} bytes")
    print(f"{'='*60}")

    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
    run_full_archive()
