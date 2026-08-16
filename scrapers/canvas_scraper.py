"""Read Canvas data through an authenticated Firefox/ClassLink session.

Canvas personal access tokens are disabled for this account.  This module keeps
the existing scraper API, but makes read-only Canvas API requests from Firefox
after Selenium has completed the normal ClassLink sign-in flow.  The Firefox
profile holds the session; cookies are never copied into this process or saved
to a separate token file.
"""

from __future__ import annotations

from collections import deque
import json
import logging
import os
import re
import stat
import time
import tempfile
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

import requests
from config import get_setting

logger = logging.getLogger(__name__)

CANVAS_API_URL = get_setting("CANVAS_API_URL", "https://canvas.instructure.com").rstrip("/")
CLASSLINK_URL = get_setting("CANVAS_SSO_ENTRY_URL", "https://launchpad.classlink.com/forsyth")
DEFAULT_PROFILE_DIR = Path.home() / ".local" / "share" / "personal-assistant-bot" / "canvas-firefox-profile"
DEFAULT_DAEMON_URL = "http://127.0.0.1:8976"


class CanvasSessionError(RuntimeError):
    """Base error for a browser-backed Canvas request."""


class CanvasSignInRequired(CanvasSessionError):
    """The stored Firefox session is absent, expired, or needs user action."""


def _env_bool(name: str, default: bool) -> bool:
    value = get_setting(name, "")
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _short_date(value: Any, fallback: str) -> str:
    return str(value)[:10] if value else fallback


def _parse_canvas_date(value: Any) -> datetime | None:
    """Parse Canvas's ISO timestamps into an aware UTC datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _assignment_is_actionable(assignment: dict[str, Any], now: datetime | None = None) -> bool:
    """Return whether an assignment belongs in the active task feed.

    Canvas favorites often include old club and prior-year courses.  Canvas also
    returns those old assignments when sorting by ``updated_at``.  A due date
    is the best indicator of an actionable task; undated work is retained only
    when it was edited recently.  Both windows are configurable so an unusual
    teacher workflow can be accommodated without changing code.
    """
    if assignment.get("workflow_state") == "deleted":
        return False

    now = now or datetime.now(timezone.utc)
    overdue_grace_days = max(0, int(get_setting("CANVAS_ASSIGNMENT_OVERDUE_GRACE_DAYS", "7")))
    undated_update_days = max(0, int(get_setting("CANVAS_NO_DUE_UPDATE_DAYS", "21")))
    due_at = _parse_canvas_date(assignment.get("due_at"))

    if due_at is not None:
        return due_at >= now - timedelta(days=overdue_grace_days)

    updated_at = _parse_canvas_date(assignment.get("updated_at"))
    return updated_at is not None and updated_at >= now - timedelta(days=undated_update_days)


def _assignment_sort_key(assignment: dict[str, Any]) -> tuple[int, datetime]:
    """Sort dated work by urgency, then recent undated work by last update."""
    due_at = _parse_canvas_date(assignment.get("due_at"))
    if due_at is not None:
        return (0, due_at)
    # Reverse the undated time so more recently edited work appears first.
    updated_at = _parse_canvas_date(assignment.get("updated_at")) or datetime.min.replace(tzinfo=timezone.utc)
    return (1, datetime.max.replace(tzinfo=timezone.utc) - (updated_at - datetime.min.replace(tzinfo=timezone.utc)))


def _is_recent_canvas_update(item: dict[str, Any], *fields: str, now: datetime | None = None) -> bool:
    """Keep informational Canvas content out of the digest after it goes stale."""
    now = now or datetime.now(timezone.utc)
    update_days = max(0, int(get_setting("CANVAS_CONTENT_UPDATE_DAYS", "21")))
    for field in fields:
        value = _parse_canvas_date(item.get(field))
        if value is not None:
            return value >= now - timedelta(days=update_days)
    return False


class _CanvasPageHTMLParser(HTMLParser):
    """Extract readable text and anchors without following arbitrary URLs."""

    _BLOCK_TAGS = {"br", "div", "li", "p", "section", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self._active_link: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self.text_parts.append("\n")
        if tag.lower() != "a" or self._active_link is not None:
            return
        values = {key.lower(): value or "" for key, value in attrs}
        href = values.get("href", "").strip()
        if href:
            self._active_link = {"href": href, "label": values.get("title", "").strip()}

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._active_link is not None:
            self.links.append(self._active_link)
            self._active_link = None
        if tag.lower() in self._BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not data:
            return
        self.text_parts.append(data)
        if self._active_link is not None and not self._active_link["label"]:
            self._active_link["label"] = data.strip()


def _parse_canvas_page_html(html_body: str) -> tuple[str, list[dict[str, str]]]:
    """Return plain page text plus the links declared in its HTML."""
    parser = _CanvasPageHTMLParser()
    try:
        parser.feed(html_body or "")
        parser.close()
    except Exception:
        logger.debug("Canvas page contained malformed HTML", exc_info=True)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", "".join(parser.text_parts))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, parser.links


def _canvas_link_target(href: str, course_id: str) -> tuple[str, str] | None:
    """Map a same-course Canvas UI URL to its safe, read-only API endpoint."""
    if not href or href.startswith(("#", "javascript:", "mailto:")):
        return None
    target = urlsplit(urljoin(f"{CANVAS_API_URL}/", href))
    canvas_origin = urlsplit(CANVAS_API_URL)
    if target.netloc and target.netloc != canvas_origin.netloc:
        return None
    parts = [unquote(part) for part in target.path.split("/") if part]
    if len(parts) < 4 or parts[0] != "courses" or parts[1] != str(course_id):
        return None
    collection, object_id = parts[2], parts[3]
    if collection == "pages" and object_id:
        return "page", f"/api/v1/courses/{course_id}/pages/{quote(object_id, safe='')}"
    if not object_id.isdigit():
        return None
    kinds = {
        "assignments": "assignment",
        "discussion_topics": "discussion",
        "files": "file",
        "modules": "module",
    }
    kind = kinds.get(collection)
    if kind is None:
        return None
    return kind, f"/api/v1/courses/{course_id}/{collection}/{object_id}"


def _describe_linked_canvas_item(kind: str, payload: Any) -> str:
    """Make API payloads useful to the digest without reproducing an entire page."""
    if not isinstance(payload, dict):
        return ""
    if kind == "page":
        body, _links = _parse_canvas_page_html(str(payload.get("body") or ""))
        title = str(payload.get("title") or "Untitled page")
        return f"Page: {title}" + (f" — {body}" if body else "")
    if kind == "assignment":
        title = str(payload.get("name") or "Untitled assignment")
        due = _short_date(payload.get("due_at"), "")
        description, _links = _parse_canvas_page_html(str(payload.get("description") or ""))
        return f"Assignment: {title}" + (f" (due {due})" if due else "") + (f" — {description}" if description else "")
    if kind == "discussion":
        title = str(payload.get("title") or "Untitled discussion")
        message, _links = _parse_canvas_page_html(str(payload.get("message") or ""))
        return f"Discussion: {title}" + (f" — {message}" if message else "")
    if kind == "file":
        title = str(payload.get("display_name") or payload.get("filename") or "Untitled file")
        size = payload.get("size")
        return f"File: {title}" + (f" ({size} bytes)" if isinstance(size, int) else "")
    if kind == "module":
        return f"Module: {str(payload.get('name') or 'Untitled module')}"
    return ""


def _linked_canvas_item_html(kind: str, payload: Any) -> str:
    """Return the only HTML field that may contain more contextual links."""
    if not isinstance(payload, dict):
        return ""
    fields = {
        "page": "body",
        "assignment": "description",
        "discussion": "message",
    }
    return str(payload.get(fields.get(kind, "")) or "")


_CONTEXT_STOP_WORDS = {
    "about", "after", "also", "and", "are", "assignment", "canvas", "class", "course", "from",
    "have", "here", "information", "instructions", "into", "just", "learn", "link", "more", "need",
    "only", "page", "please", "read", "resource", "site", "that", "the", "their", "there", "these",
    "this", "through", "visit", "website", "with", "your",
}


def _context_terms(*values: str) -> set[str]:
    """Derive a small local topic vocabulary from the Canvas source context."""
    terms: set[str] = set()
    for value in values:
        for word in re.findall(r"[a-zA-Z][a-zA-Z0-9'-]{3,}", value.lower()):
            if word not in _CONTEXT_STOP_WORDS:
                terms.add(word)
    return terms


def _is_contextual_external_text(text: str, terms: set[str]) -> bool:
    """Reject public pages that do not substantively match the Canvas context."""
    # Two distinct terms keeps a generic word from a course page (for example,
    # "read" or "resource") from being enough to take the crawl off-topic.
    minimum = max(1, int(get_setting("CANVAS_EXTERNAL_LINK_MIN_CONTEXT_MATCHES", "2")))
    words = set(re.findall(r"[a-zA-Z][a-zA-Z0-9'-]{3,}", text.lower()))
    return len(words.intersection(terms)) >= minimum


def _fetch_contextual_external_page(href: str, terms: set[str]) -> tuple[str, str, list[str]]:
    """Fetch a public page only when its text remains on the Canvas topic."""
    from scrapers.web_precacher import fetch_public_page

    source_url, text, links = fetch_public_page(
        href,
        max_chars=max(200, int(get_setting("CANVAS_EXTERNAL_LINK_TEXT_LIMIT", "1200"))),
    )
    if not source_url or not text or not _is_contextual_external_text(text, terms):
        return "", "", []
    host = urlsplit(source_url).netloc
    return source_url, f"Public source ({host}): {text}", links


def _crawl_canvas_page_links(
    canvas: "CanvasBrowserClient",
    course_id: str,
    html_body: str,
    *,
    initial_seen: set[str] | None = None,
    context_terms: set[str] | None = None,
) -> list[tuple[int, str]]:
    """Traverse contextual Canvas links deeply while bounding work and output.

    A Canvas course can contain arbitrary cycles (for example, page A → page B
    → page A).  Canonical API paths are marked as seen before they are queued,
    so every Canvas resource is fetched at most once per root page. Each public
    HTTPS page must independently match the original Canvas context before its
    text or child links are allowed into the crawl.
    """
    max_depth = min(12, max(0, int(get_setting("CANVAS_PAGE_LINK_MAX_DEPTH", "8"))))
    max_items = min(120, max(0, int(get_setting("CANVAS_PAGE_LINK_MAX_ITEMS", "60"))))
    text_limit = max(200, int(get_setting("CANVAS_PAGE_LINK_TEXT_LIMIT", "1200")))
    total_limit = max(text_limit, int(get_setting("CANVAS_PAGE_LINK_TOTAL_TEXT_LIMIT", "10000")))
    external_limit = min(24, max(0, int(get_setting("CANVAS_EXTERNAL_LINK_LIMIT", "8"))))
    # Public pages receive the same depth allowance as Canvas pages. They must
    # still independently pass the original source-context check above.
    external_depth = min(max_depth, max(0, int(get_setting("CANVAS_EXTERNAL_LINK_MAX_DEPTH", "8"))))
    if max_depth == 0 or max_items == 0:
        return []

    queue: deque[tuple[int, str, str]] = deque()
    seen = set(initial_seen or ())
    requested_external: set[str] = set()
    terms = context_terms or set()
    descriptions: list[tuple[int, str]] = []
    total_chars = 0

    def append_description(depth: int, description: str) -> bool:
        nonlocal total_chars
        if not description or len(descriptions) >= max_items or total_chars >= total_limit:
            return False
        remaining = total_limit - total_chars
        clipped = description[:min(text_limit, remaining)]
        descriptions.append((depth, clipped))
        total_chars += len(clipped)
        return True

    def queue_public_link(href: str, depth: int) -> None:
        if depth > external_depth or len(requested_external) >= external_limit:
            return
        # A cross-course Canvas URL remains out of scope even though it shares
        # the hostname. Public URLs are revalidated before every fetch.
        absolute = urljoin(f"{CANVAS_API_URL}/", href)
        if urlsplit(absolute).netloc == urlsplit(CANVAS_API_URL).netloc:
            return
        if absolute in requested_external:
            return
        requested_external.add(absolute)
        queue.append((depth, "public", absolute))

    def queue_links(source_html: str, depth: int) -> None:
        _text, links = _parse_canvas_page_html(source_html)
        for link in links:
            href = link.get("href", "")
            target = _canvas_link_target(href, course_id)
            if target is not None:
                if depth > max_depth:
                    continue
                kind, api_path = target
                if api_path in seen:
                    continue
                seen.add(api_path)
                queue.append((depth, kind, api_path))
                continue

            # Do not treat a cross-course Canvas URL as a public-web URL: it
            # escapes the current course context and requires a separate scan.
            if terms:
                queue_public_link(href, depth)

    queue_links(html_body, 1)
    while queue and len(descriptions) < max_items and total_chars < total_limit:
        depth, kind, api_path = queue.popleft()
        if kind == "public":
            source_url, preview, public_links = _fetch_contextual_external_page(api_path, terms)
            if not source_url:
                continue
            append_description(depth, preview)
            if depth < external_depth:
                for public_link in public_links:
                    queue_public_link(public_link, depth + 1)
            continue
        try:
            payload = canvas.get_json(api_path)
        except CanvasSessionError as exc:
            logger.info("Could not read linked Canvas %s: %s", kind, exc)
            continue
        description = _describe_linked_canvas_item(kind, payload)
        append_description(depth, description)
        if depth < max_depth:
            queue_links(_linked_canvas_item_html(kind, payload), depth + 1)
    return descriptions


def _follow_canvas_page_links(
    canvas: "CanvasBrowserClient", course_id: str, html_body: str
) -> list[str]:
    """Compatibility wrapper that returns the text from the recursive crawl."""
    return [description for _depth, description in _crawl_canvas_page_links(canvas, course_id, html_body)]


def _canvas_coursework_window() -> tuple[datetime, datetime]:
    """Return the past/future window used for current Canvas work."""
    now = datetime.now(timezone.utc)
    overdue_days = max(0, int(get_setting("CANVAS_OVERDUE_LOOKBACK_DAYS", "30")))
    due_soon_days = max(1, int(get_setting("CANVAS_DUE_SOON_DAYS", "14")))
    return now - timedelta(days=overdue_days), now + timedelta(days=due_soon_days)


def _submission_is_complete(submission: Any) -> bool:
    if not isinstance(submission, dict):
        return False
    if submission.get("excused"):
        return True
    return bool(
        submission.get("submitted_at")
        or submission.get("graded_at")
        or submission.get("workflow_state") in {"submitted", "graded"}
    )


def _submission_state(assignment: dict[str, Any], now: datetime) -> str | None:
    """Classify work by completion only; Canvas scores are deliberately ignored."""
    submission = assignment.get("submission") or {}
    due_at = _parse_canvas_date(assignment.get("due_at"))
    complete = _submission_is_complete(submission)
    if isinstance(submission, dict) and submission.get("excused"):
        return None
    if isinstance(submission, dict) and submission.get("missing"):
        return "Missing"
    if due_at and due_at < now and not complete:
        return "Overdue"
    if isinstance(submission, dict) and submission.get("late") and complete:
        return "Submitted late"
    if complete:
        return "Completed"
    return None


def _supported_canvas_study_file(file_data: dict[str, Any]) -> bool:
    """Keep the nightly study pipeline to formats it can extract safely."""
    content_type = str(file_data.get("content-type") or file_data.get("content_type") or "").lower()
    filename = str(file_data.get("display_name") or file_data.get("filename") or "").lower()
    allowed_extensions = (".pdf", ".docx", ".txt")
    return (
        content_type in {
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "text/plain",
        }
        or filename.endswith(allowed_extensions)
    )


class CanvasBrowserClient:
    """Read Canvas through the persistent local Firefox daemon.

    The daemon owns the Firefox process and its session cookie.  Keeping that
    process alive is necessary because Canvas uses a browser session rather
    than a personal API token.  ``use_daemon=False`` is reserved for the
    daemon process itself, which drives Selenium directly.
    """

    def __init__(self, *, headless: bool | None = None, use_daemon: bool | None = None) -> None:
        profile_value = get_setting("CANVAS_FIREFOX_PROFILE_DIR", str(DEFAULT_PROFILE_DIR))
        self.profile_dir = Path(profile_value).expanduser().resolve()
        self.headless = _env_bool("CANVAS_BROWSER_HEADLESS", True) if headless is None else headless
        self.login_timeout = int(get_setting("CANVAS_LOGIN_TIMEOUT_SECONDS", "90"))
        self.use_daemon = _env_bool("CANVAS_USE_BROWSER_DAEMON", True) if use_daemon is None else use_daemon
        self.daemon_url = get_setting("CANVAS_BROWSER_DAEMON_URL", DEFAULT_DAEMON_URL).rstrip("/")
        self.driver: Any | None = None
        self.classlink_post_login_location = "not reached"
        self.canvas_app_opened = False
        self.canvas_app_target = "not detected"

    def __enter__(self) -> "CanvasBrowserClient":
        self.connect()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def connect(self) -> None:
        """Verify the persistent Firefox daemon has an authenticated Canvas session."""
        if self.use_daemon:
            self._connect_to_daemon()
            return

        """Start Firefox and make sure its profile has an authenticated Canvas session."""
        if self.driver is None:
            self._start_browser()

        if self._is_canvas_authenticated():
            return

        self._sign_in_via_classlink()
        if not self._is_canvas_authenticated():
            raise CanvasSignInRequired(
                "ClassLink sign-in completed, but the Canvas app did not establish a session. "
                f"LaunchPad after ADFS: {self.classlink_post_login_location}; "
                f"Canvas app clicked: {'yes' if self.canvas_app_opened else 'no'} ({self.canvas_app_target}); "
                f"current browser page: {self._safe_browser_location()}."
            )

    def close(self) -> None:
        if self.use_daemon:
            return
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                logger.debug("Firefox did not close cleanly", exc_info=True)
            finally:
                self.driver = None

    def get_current_user(self) -> dict[str, Any]:
        data = self.get_json("/api/v1/users/self")
        if not isinstance(data, dict):
            raise CanvasSessionError("Canvas returned an unexpected current-user response.")
        return data

    def get_favorite_courses(self) -> list[dict[str, Any]]:
        return self.get_paginated("/api/v1/users/self/favorites/courses?per_page=100")

    def get_paginated(self, path_or_url: str, max_pages: int = 20) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_url: str | None = path_or_url

        for _ in range(max_pages):
            if not next_url:
                break
            data, link_header = self._request_json(next_url)
            if not isinstance(data, list):
                raise CanvasSessionError("Canvas returned an unexpected list response.")
            items.extend(item for item in data if isinstance(item, dict))
            next_url = self._next_link(link_header)

        return items

    def get_json(self, path_or_url: str) -> Any:
        data, _ = self._request_json(path_or_url)
        return data

    def download_file(self, file_id: str | int, destination: str | Path) -> bool:
        """Download one Canvas file through Firefox's authenticated session.

        File bytes are streamed only from the localhost browser daemon.  The
        bot never stores or reuses Canvas session cookies outside Firefox.
        """
        if not str(file_id).isdigit():
            raise CanvasSessionError("Canvas file IDs must be numeric.")
        if not self.use_daemon:
            raise CanvasSessionError("Canvas file downloads require the persistent browser daemon.")
        try:
            response = requests.get(
                f"{self.daemon_url}/download",
                params={"file_id": str(file_id)},
                timeout=180,
            )
        except requests.RequestException as exc:
            raise CanvasSessionError("Canvas browser daemon did not return the requested file.") from exc

        if response.status_code in {401, 403}:
            raise CanvasSignInRequired("The saved Canvas session has expired.")
        if response.status_code != 200:
            try:
                detail = response.json().get("error", "Canvas file download failed.")
            except ValueError:
                detail = "Canvas file download failed."
            raise CanvasSessionError(detail)

        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(dir=destination_path.parent, delete=False) as temporary:
                temporary.write(response.content)
                temporary_path = Path(temporary.name)
            temporary_path.replace(destination_path)
            return True
        except OSError as exc:
            raise CanvasSessionError("Could not save the Canvas study file.") from exc

    def _connect_to_daemon(self) -> None:
        try:
            response = requests.get(f"{self.daemon_url}/health", timeout=8)
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise CanvasSignInRequired(
                "Canvas browser daemon is not running. Start scripts/canvas_browser_daemon.py "
                "with DISPLAY=:1, then sign in through the VNC Firefox window."
            ) from exc

        if response.status_code != 200 or not payload.get("authenticated"):
            location = payload.get("location", "the browser") if isinstance(payload, dict) else "the browser"
            raise CanvasSignInRequired(
                f"Canvas sign-in is required in the persistent Firefox session (currently at {location})."
            )

    def _start_browser(self) -> None:
        try:
            from selenium import webdriver
            from selenium.webdriver.firefox.options import Options
        except ImportError as exc:
            raise CanvasSessionError(
                "Selenium is not installed. Install project requirements before using Canvas browser auth."
            ) from exc

        self.profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.profile_dir.chmod(stat.S_IRWXU)
        except OSError:
            logger.warning("Could not restrict permissions on Canvas Firefox profile directory.")

        options = Options()
        options.add_argument("-profile")
        options.add_argument(str(self.profile_dir))
        if self.headless:
            options.add_argument("-headless")

        try:
            self.driver = webdriver.Firefox(options=options)
            self.driver.set_page_load_timeout(45)
            self.driver.set_script_timeout(45)
        except Exception as exc:
            self.driver = None
            raise CanvasSessionError(
                "Could not start Firefox for Canvas. Ensure Firefox and geckodriver/Selenium Manager are available."
            ) from exc

    def _is_canvas_authenticated(self, *, navigate_to_canvas: bool = True) -> bool:
        assert self.driver is not None
        try:
            if navigate_to_canvas:
                self.driver.switch_to.default_content()
                self.driver.get(CANVAS_API_URL)
            elif urlsplit(self.driver.current_url).netloc != urlsplit(CANVAS_API_URL).netloc:
                return False
            user = self.get_current_user()
            return bool(user.get("id"))
        except CanvasSessionError:
            return False
        except Exception:
            logger.debug("Canvas browser session is not authenticated", exc_info=True)
            return False

    def _sign_in_via_classlink(self) -> None:
        assert self.driver is not None
        username = get_setting("CLASSLINK_USERNAME")
        password = get_setting("CLASSLINK_PASSWORD")
        if not username or not password:
            raise CanvasSignInRequired(
                "Set CLASSLINK_USERNAME and CLASSLINK_PASSWORD in .env; do not put them in chat."
            )

        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:  # Defensive: _start_browser already imports Selenium.
            raise CanvasSessionError("Selenium is not installed.") from exc

        self.driver.get(CLASSLINK_URL)
        wait = WebDriverWait(self.driver, self.login_timeout)
        username_selector = get_setting(
            "CLASSLINK_USERNAME_SELECTOR",
            "input[name='username'], input[name='user'], input#username, input#user, "
            "input#login, input#loginId, input[name='login'], input[name='loginId'], "
            "input#userNameInput, input[name='UserName'], input[name='userName'], "
            "input[name='identifier'], input[type='email'], input[autocomplete='username'], "
            "input[placeholder*='Username']",
        )
        password_selector = get_setting(
            "CLASSLINK_PASSWORD_SELECTOR",
            "input[name='password'], input#password, input[type='password'], "
            "input#passwordInput, input[autocomplete='current-password']",
        )
        submit_selector = get_setting(
            "CLASSLINK_SUBMIT_SELECTOR",
            "button[type='submit'], input[type='submit'], #submitButton, #loginButton",
        )

        try:
            # Some LaunchPad tenants place the sign-in controls behind a landing-page
            # button or inside a web component's shadow root.
            username_input = WebDriverWait(self.driver, 15).until(
                lambda _d: self._find_deep_css(username_selector)
            )
        except Exception:
            self._click_classlink_sign_in_entry()
            try:
                username_input = wait.until(lambda _d: self._find_deep_css(username_selector))
            except Exception as exc:
                raise CanvasSignInRequired(self._classlink_form_diagnostic()) from exc

        try:
            password_input = wait.until(lambda _d: self._find_deep_css(password_selector))
            username_input.clear()
            username_input.send_keys(username)
            password_input.clear()
            password_input.send_keys(password)
            wait.until(lambda _d: self._find_deep_css(submit_selector)).click()
        except Exception as exc:
            raise CanvasSignInRequired(
                self._classlink_form_diagnostic()
            ) from exc

        # Forsyth's ADFS endpoint briefly shows an intermediate "Working..." page.
        # Do not look for the Canvas tile until its redirect has returned to LaunchPad.
        try:
            wait.until(lambda d: "fcss-adfs.forsyth.k12.ga.us" not in urlsplit(d.current_url).netloc)
        except Exception as exc:
            raise CanvasSignInRequired(
                "ADFS did not finish returning to ClassLink. Current browser page: "
                f"{self._safe_browser_location()}."
            ) from exc

        # A ClassLink session may need its Canvas app tile to mint the Canvas SSO assertion.
        # Try it first, then use the Canvas URL as the final check/SSO return location.
        self.driver.switch_to.default_content()
        self.classlink_post_login_location = self._safe_browser_location()
        self.canvas_app_opened = self._open_canvas_app_if_visible()
        deadline = time.monotonic() + self.login_timeout
        while time.monotonic() < deadline:
            # Do not navigate this new tab while ClassLink is still completing its SSO
            # redirect. Navigating it to the Canvas root here cancels the handoff and
            # leaves the browser on Canvas's ordinary username/password screen.
            if self._is_canvas_authenticated(navigate_to_canvas=False):
                return
            time.sleep(1)

    def _find_deep_css(self, selector: str) -> Any | None:
        """Find a visible element in the document, an open Shadow DOM root, or an iframe."""
        assert self.driver is not None
        try:
            from selenium.webdriver.common.by import By
        except ImportError:
            return None

        self.driver.switch_to.default_content()
        found = self._find_deep_css_in_current_document(selector)
        if found:
            return found

        def search_frames() -> Any | None:
            for frame in self.driver.find_elements(By.CSS_SELECTOR, "iframe, frame"):
                self.driver.switch_to.frame(frame)
                found_in_frame = self._find_deep_css_in_current_document(selector)
                if found_in_frame:
                    return found_in_frame
                nested = search_frames()
                if nested:
                    return nested
                self.driver.switch_to.parent_frame()
            return None

        return search_frames()

    def _find_deep_css_in_current_document(self, selector: str) -> Any | None:
        """Find a visible element in the currently selected frame or its Shadow DOM."""
        assert self.driver is not None
        return self.driver.execute_script(
            """
            const selector = arguments[0];
            function find(root) {
                const direct = root.querySelector(selector);
                if (direct && direct.offsetParent !== null) return direct;
                for (const element of root.querySelectorAll('*')) {
                    if (!element.shadowRoot) continue;
                    const nested = find(element.shadowRoot);
                    if (nested) return nested;
                }
                return null;
            }
            return find(document);
            """,
            selector,
        )

    def _click_classlink_sign_in_entry(self) -> None:
        """Advance a LaunchPad landing page to its username/password form when present."""
        assert self.driver is not None
        custom_selector = get_setting("CLASSLINK_START_SELECTOR")
        try:
            if custom_selector:
                entry = self._find_deep_css(custom_selector)
            else:
                entry = self.driver.execute_script(
                    """
                    function find(root) {
                        for (const element of root.querySelectorAll('a, button, input[type="button"], input[type="submit"]')) {
                            const label = (element.innerText || element.value || element.getAttribute('aria-label') || '')
                                .trim().toLowerCase();
                            if ((label.startsWith('sign in') || ['log in', 'login'].includes(label))
                                && element.offsetParent !== null) return element;
                        }
                        for (const element of root.querySelectorAll('*')) {
                            if (element.shadowRoot) {
                                const nested = find(element.shadowRoot);
                                if (nested) return nested;
                            }
                        }
                        return null;
                    }
                    return find(document);
                    """
                )
            if entry:
                self.driver.execute_script("arguments[0].click();", entry)
        except Exception:
            logger.debug("ClassLink sign-in entry button was not available", exc_info=True)

    def _classlink_form_diagnostic(self) -> str:
        """Describe the page safely: no query string, input values, cookies, or credentials."""
        assert self.driver is not None
        try:
            parts = urlsplit(self.driver.current_url)
            safe_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            title = self.driver.title or "untitled page"
        except Exception:
            safe_url = "unknown URL"
            title = "unknown page"
        controls = self._visible_form_controls()
        controls_text = "; ".join(controls) if controls else "none detected"
        return (
            f"Could not find the expected ClassLink sign-in form on {title!r} at {safe_url}. "
            f"Visible controls (values excluded): {controls_text}. "
            "Set CLASSLINK_USERNAME_SELECTOR, CLASSLINK_PASSWORD_SELECTOR, and "
            "CLASSLINK_SUBMIT_SELECTOR in .env for this page."
        )

    def _safe_browser_location(self) -> str:
        """Return the current title and origin/path without any sign-in query parameters."""
        assert self.driver is not None
        try:
            parts = urlsplit(self.driver.current_url)
            safe_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            return f"{self.driver.title or 'untitled page'!r} at {safe_url}"
        except Exception:
            return "unknown"

    def _safe_element_description(self, element: Any) -> str:
        """Describe a launch tile without retaining an SSO URL query or page data."""
        assert self.driver is not None
        try:
            data = self.driver.execute_script(
                """
                const e = arguments[0];
                const target = e.closest('a, button, [role="button"]') || e;
                return {
                    tag: target.tagName.toLowerCase(),
                    label: (target.innerText || target.getAttribute('aria-label') || target.getAttribute('title') || target.getAttribute('alt') || '')
                        .trim().replace(/\\s+/g, ' ').slice(0, 80),
                    href: target.href || ''
                };
                """,
                element,
            )
            href = data.get("href", "")
            if href:
                parts = urlsplit(href)
                href = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            return f"{data.get('tag', 'element')} label={data.get('label', '')!r} href={href or 'none'}"
        except Exception:
            return "unavailable"

    def _visible_form_controls(self) -> list[str]:
        """Return safe control metadata from the document, Shadow DOM roots, and frames."""
        assert self.driver is not None
        try:
            from selenium.webdriver.common.by import By
        except ImportError:
            return []

        controls: list[str] = []

        def collect_current_frame() -> None:
            entries = self.driver.execute_script(
                """
                function collect(root, results) {
                    for (const element of root.querySelectorAll('input, button, select, textarea')) {
                        if (element.offsetParent === null) continue;
                        const label = (element.innerText || element.getAttribute('aria-label') || element.getAttribute('placeholder') || '')
                            .trim().replace(/\\s+/g, ' ').slice(0, 50);
                        results.push({
                            tag: element.tagName.toLowerCase(), type: element.getAttribute('type') || '',
                            id: element.id || '', name: element.getAttribute('name') || '', label
                        });
                    }
                    for (const element of root.querySelectorAll('*')) {
                        if (element.shadowRoot) collect(element.shadowRoot, results);
                    }
                }
                const results = [];
                collect(document, results);
                return results;
                """
            )
            for entry in entries or []:
                if len(controls) >= 12:
                    return
                controls.append(
                    "{tag}[type={type}, id={id}, name={name}, label={label}]".format(
                        tag=entry.get("tag", ""),
                        type=entry.get("type", ""),
                        id=entry.get("id", ""),
                        name=entry.get("name", ""),
                        label=entry.get("label", ""),
                    )
                )

        def collect_frames() -> None:
            collect_current_frame()
            if len(controls) >= 12:
                return
            for frame in self.driver.find_elements(By.CSS_SELECTOR, "iframe, frame"):
                self.driver.switch_to.frame(frame)
                collect_frames()
                self.driver.switch_to.parent_frame()
                if len(controls) >= 12:
                    return

        try:
            self.driver.switch_to.default_content()
            collect_frames()
        except Exception:
            logger.debug("Could not inspect ClassLink login controls", exc_info=True)
        finally:
            self.driver.switch_to.default_content()
        return controls

    def _open_canvas_app_if_visible(self) -> bool:
        assert self.driver is not None
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError:
            return False

        selector = get_setting("CLASSLINK_CANVAS_APP_SELECTOR")
        handles_before = set(self.driver.window_handles)
        try:
            if selector:
                app = WebDriverWait(self.driver, 30).until(lambda _d: self._find_deep_css(selector))
            else:
                app = WebDriverWait(self.driver, 30).until(
                    lambda _d: self.driver.execute_script(
                        """
                        function findCanvas(root) {
                            for (const element of root.querySelectorAll('a, button, [role="button"], img, [aria-label], [title]')) {
                                const label = [
                                    element.innerText, element.getAttribute('aria-label'),
                                    element.getAttribute('title'), element.getAttribute('alt')
                                ].filter(Boolean).join(' ').toLowerCase();
                                if (label.includes('canvas')) {
                                    return element.closest('a, button, [role="button"]') || element;
                                }
                            }
                            for (const element of root.querySelectorAll('*')) {
                                if (element.shadowRoot) {
                                    const nested = findCanvas(element.shadowRoot);
                                    if (nested) return nested;
                                }
                            }
                            return null;
                        }
                        return findCanvas(document);
                        """
                    )
                )
            self.canvas_app_target = self._safe_element_description(app)
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", app)
            # LaunchPad's app tile uses pointer events.  An Actions click reproduces the
            # normal user gesture more faithfully than a DOM ``element.click()`` call.
            ActionChains(self.driver).move_to_element(app).pause(0.15).click().perform()
            WebDriverWait(self.driver, 30).until(
                lambda d: len(d.window_handles) > len(handles_before)
                or urlsplit(d.current_url).netloc.endswith("instructure.com")
            )
            new_handles = [h for h in self.driver.window_handles if h not in handles_before]
            if new_handles:
                self.driver.switch_to.window(new_handles[-1])
            return True
        except Exception:
            # Some tenants start Canvas SSO when the Canvas URL is visited, so a missing tile
            # is not an error. _is_canvas_authenticated performs that fallback.
            logger.debug("Canvas ClassLink app tile was not available", exc_info=True)
            return False

    def _request_json(self, path_or_url: str) -> tuple[Any, str]:
        if self.use_daemon:
            return self._request_json_from_daemon(path_or_url)
        if self.driver is None:
            raise CanvasSessionError("Firefox is not running.")

        target = urljoin(f"{CANVAS_API_URL}/", path_or_url)
        result = self.driver.execute_async_script(
            """
            const target = arguments[0];
            const done = arguments[arguments.length - 1];
            fetch(target, {
                credentials: 'include',
                headers: {Accept: 'application/json'},
                redirect: 'follow'
            }).then(async response => {
                const body = await response.text();
                done({
                    status: response.status,
                    contentType: response.headers.get('content-type') || '',
                    link: response.headers.get('link') || '',
                    body
                });
            }).catch(error => done({error: String(error)}));
            """,
            target,
        )
        if not isinstance(result, dict) or result.get("error"):
            raise CanvasSessionError("Canvas request failed inside Firefox.")

        status_code = result.get("status")
        if status_code in {401, 403}:
            raise CanvasSignInRequired("The saved Canvas session has expired.")
        if not isinstance(status_code, int) or not 200 <= status_code < 300:
            raise CanvasSessionError(f"Canvas returned HTTP {status_code}.")

        try:
            return json.loads(result.get("body", "")), result.get("link", "")
        except json.JSONDecodeError as exc:
            # Canvas can return a 200 HTML ClassLink page after an expired session.
            raise CanvasSignInRequired("Canvas redirected Firefox to sign in again.") from exc

    def _request_json_from_daemon(self, path_or_url: str) -> tuple[Any, str]:
        try:
            response = requests.get(
                f"{self.daemon_url}/request",
                params={"path": path_or_url},
                timeout=50,
            )
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise CanvasSessionError("Canvas browser daemon did not return a valid response.") from exc

        if response.status_code in {401, 403}:
            raise CanvasSignInRequired(payload.get("error", "The saved Canvas session has expired."))
        if response.status_code != 200:
            raise CanvasSessionError(payload.get("error", f"Canvas daemon returned HTTP {response.status_code}."))
        return payload.get("data"), payload.get("link", "")

    @staticmethod
    def _next_link(link_header: str) -> str | None:
        for url, relation in re.findall(r"<([^>]+)>;\s*rel=\"?([^;,\"]+)\"?", link_header or ""):
            if relation == "next":
                return url
        return None


def _with_client(operation: Any, unavailable: str = "") -> str:
    try:
        with CanvasBrowserClient() as canvas:
            return operation(canvas)
    except CanvasSignInRequired as exc:
        logger.info("Canvas sign-in required: %s", exc)
        return unavailable or f"Canvas sign-in required: {exc}"
    except CanvasSessionError as exc:
        logger.error("Canvas browser error: %s", exc)
        return unavailable or f"Canvas unavailable: {exc}"


def _get_upcoming_assignments(canvas: CanvasBrowserClient, courses: list[dict[str, Any]] | None = None) -> str:
    """Return an action-oriented Canvas view with submission completion state."""
    courses = courses if courses is not None else canvas.get_favorite_courses()
    overdue: list[tuple[datetime, str]] = []
    due_soon: list[tuple[datetime, str]] = []
    completed: list[tuple[datetime, str]] = []
    lookback, upcoming_cutoff = _canvas_coursework_window()
    now = datetime.now(timezone.utc)
    per_section_limit = max(1, int(get_setting("CANVAS_ACTION_ITEMS_LIMIT", "8")))

    for course in courses:
        course_id = course.get("id")
        course_name = course.get("name", "Unnamed course")
        if not course_id:
            continue
        try:
            assignments = canvas.get_paginated(
                # ``submission`` supplies completion/missing/late state for
                # the signed-in student. No score is requested or displayed.
                f"/api/v1/courses/{course_id}/assignments?include[]=submission&order_by=due_at&order=asc&per_page=100",
                max_pages=1,
            )
            for assignment in assignments:
                due_at = _parse_canvas_date(assignment.get("due_at"))
                state = _submission_state(assignment, now)
                title = assignment.get("name", "Untitled")
                due_text = _short_date(assignment.get("due_at"), "No due date")
                line = f"[{course_name}] {title} — Due: {due_text}"
                if assignment.get("html_url"):
                    line += f" [Link: {assignment['html_url']}]"

                # A missing/overdue item stays visible for a manageable
                # window; ancient prior-year work never floods the plan.
                if state in {"Missing", "Overdue"} and (due_at is None or due_at >= lookback):
                    overdue.append((due_at or now, f"{line} · **{state}**"))
                elif (
                    due_at is not None
                    and now <= due_at <= upcoming_cutoff
                    and state != "Completed"
                    and state != "Submitted late"
                ):
                    due_soon.append((due_at, line))
                elif state == "Completed":
                    submitted_at = _parse_canvas_date((assignment.get("submission") or {}).get("submitted_at"))
                    if submitted_at and submitted_at >= now - timedelta(days=7):
                        completed.append((submitted_at, f"[{course_name}] {title} · Completed"))
        except CanvasSessionError as exc:
            logger.warning("Could not fetch assignments for %s: %s", course_name, exc)

    overdue.sort(key=lambda item: item[0])
    due_soon.sort(key=lambda item: item[0])
    completed.sort(key=lambda item: item[0], reverse=True)
    if not overdue and not due_soon and not completed:
        return "✅ **Canvas: What to do next**\nNo current, missing, or recently completed coursework."

    lines = ["🎯 **Canvas: What to do next**"]
    if overdue:
        lines.append("\n🚨 **Missing / overdue**")
        lines.extend(f"- {line}" for _date, line in overdue[:per_section_limit])
    if due_soon:
        lines.append("\n📅 **Due soon**")
        lines.extend(f"- {line}" for _date, line in due_soon[:per_section_limit])
    if completed:
        lines.append("\n✅ **Recently completed**")
        lines.extend(f"- {line}" for _date, line in completed[:per_section_limit])
    return "\n".join(lines)


def get_upcoming_assignments() -> str:
    """Fetch the current Canvas action plan and completion state."""
    return _with_client(_get_upcoming_assignments)


def _calendar_task_type(title: str) -> str:
    normalized = title.lower()
    if any(word in normalized for word in ("test", "quiz", "exam")):
        return "Test"
    if "project" in normalized:
        return "Project"
    if any(word in normalized for word in ("reading", "read ")):
        return "Reading"
    return "Assignment"


def _get_calendar_assignments(
    canvas: CanvasBrowserClient,
    courses: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Return structured Canvas deadlines for the calendar sync service."""
    courses = courses if courses is not None else canvas.get_favorite_courses()
    now = datetime.now(timezone.utc)
    result: list[dict[str, str]] = []

    # Start a fresh per-pass budget for AI page extraction so a slow/cold
    # inference pass cannot stall the whole calendar collection.
    try:
        from scrapers.canvas_page_extractor import reset_extraction_budget
        reset_extraction_budget()
    except Exception:
        pass

    for course in courses:
        course_id = course.get("id")
        course_name = str(course.get("name") or "Unnamed course")
        if not course_id:
            continue
        try:
            assignments = canvas.get_paginated(
                f"/api/v1/courses/{course_id}/assignments?include[]=submission&order_by=due_at&order=asc&per_page=100",
                max_pages=1,
            )
        except CanvasSessionError as exc:
            logger.info("Could not fetch Canvas calendar assignments for %s: %s", course_name, exc)
            continue

        for assignment in assignments:
            due_at = assignment.get("due_at")
            assignment_id = assignment.get("id")
            if not due_at or not assignment_id or not _assignment_is_actionable(assignment, now):
                continue
            submission = assignment.get("submission") or {}
            if isinstance(submission, dict) and submission.get("excused"):
                continue
            state = _submission_state(assignment, now)
            title = str(assignment.get("name") or "Untitled")
            result.append({
                "id": f"{course_id}:{assignment_id}",
                "title": title,
                "course": course_name,
                "due_at": str(due_at),
                "url": str(assignment.get("html_url") or "") or None,
                "task_type": _calendar_task_type(title),
                "status": "Completed" if state in {"Completed", "Submitted late"} else "Not started",
                "official": True,
            })

        # AI Extraction for course front page, module pages, and announcements
        try:
            pages_to_extract = []
            seen_urls = set()

            # 1. Always include Course Front Page
            try:
                front_page, _ = canvas._request_json(f"/api/v1/courses/{course_id}/front_page")
                if isinstance(front_page, dict) and front_page.get("url"):
                    purl = front_page.get("url")
                    seen_urls.add(purl)
                    pages_to_extract.append({
                        "url": purl,
                        "title": front_page.get("title", "Course Home Page"),
                    })
            except Exception as exc:
                logger.debug("Could not fetch front page for %s: %s", course_name, exc)

            # 2. Module pages and external tools
            try:
                modules = canvas.get_paginated(
                    f"/api/v1/courses/{course_id}/modules?include[]=items&per_page=30",
                    max_pages=2,
                )
                for mod in modules:
                    mname = mod.get("name", "Module")
                    for item in mod.get("items", []):
                        itype = item.get("type")
                        ititle = item.get("title", "")
                        purl = item.get("page_url")
                        ext_url = item.get("external_url") or item.get("url")

                        if itype == "Page" and purl and purl not in seen_urls:
                            seen_urls.add(purl)
                            pages_to_extract.append({
                                "url": purl,
                                "title": f"[{mname}] {ititle}",
                            })
                        elif itype in ["ExternalTool", "ExternalUrl"] and ext_url and ext_url not in seen_urls:
                            seen_urls.add(ext_url)
                            pages_to_extract.append({
                                "url": ext_url,
                                "title": f"[{mname}] {ititle}",
                                "body": f"<p>External Resource: <a href=\"{ext_url}\">{ititle}</a></p>",
                            })
            except Exception as exc:
                logger.debug("Could not fetch modules for %s: %s", course_name, exc)

            # 3. Course Announcements
            try:
                announcements = canvas.get_paginated(
                    f"/api/v1/courses/{course_id}/discussion_topics?only_announcements=true&per_page=5",
                    max_pages=1,
                )
                for ann in announcements:
                    ann_id = str(ann.get("id") or "")
                    if ann_id and ann_id not in seen_urls:
                        seen_urls.add(ann_id)
                        ann_body = ann.get("message") or ""
                        ann_title = ann.get("title") or "Announcement"
                        if ann_body:
                            pages_to_extract.append({
                                "url": f"announcement-{ann_id}",
                                "title": f"[Announcement] {ann_title}",
                                "body": ann_body,
                            })
            except Exception as exc:
                logger.debug("Could not fetch announcements for %s: %s", course_name, exc)

            # Extract assignments via AI RPC
            from scrapers.canvas_page_extractor import extract_assignments_from_html
            for p in pages_to_extract:
                p_url = p.get("url")
                p_title = p.get("title", "Untitled")
                p_body = p.get("body")
                if not p_body and p_url:
                    try:
                        pdetail = canvas.get_json(f"/api/v1/courses/{course_id}/pages/{quote(p_url, safe='')}")
                        if isinstance(pdetail, dict):
                            p_body = pdetail.get("body")
                    except Exception as exc:
                        logger.debug("Could not fetch page detail %s: %s", p_url, exc)

                if p_body:
                    try:
                        extracted = extract_assignments_from_html(
                            str(course_id), course_name, p_title, p_url, p_body
                        )
                        result.extend(extracted)
                    except Exception as e:
                        logger.error("Error extracting from page %s: %s", p_url, e)
        except Exception as exc:
            logger.info("Could not fetch Canvas pages for AI extraction %s: %s", course_name, exc)

    return result


def get_calendar_assignments() -> list[dict[str, str]]:
    """Fetch structured Canvas assignments without formatting a Telegram digest."""
    try:
        with CanvasBrowserClient() as canvas:
            return _get_calendar_assignments(canvas)
    except CanvasSessionError as exc:
        logger.warning("Canvas calendar collection skipped: %s", exc)
        return []


def _get_canvas_study_files(
    canvas: CanvasBrowserClient,
    courses: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Find recently updated PDF/DOCX/text materials suitable for study guides."""
    courses = courses if courses is not None else canvas.get_favorite_courses()
    update_days = max(1, int(get_setting("CANVAS_STUDY_FILE_UPDATE_DAYS", "60")))
    per_course_limit = max(1, int(get_setting("CANVAS_STUDY_FILES_PER_COURSE", "3")))
    max_file_bytes = max(1, int(get_setting("CANVAS_STUDY_FILE_MAX_MB", "15"))) * 1024 * 1024
    cutoff = datetime.now(timezone.utc) - timedelta(days=update_days)
    candidates: list[dict[str, str]] = []

    for course in courses:
        course_id = course.get("id")
        course_name = course.get("name", "Unnamed course")
        if not course_id:
            continue
        try:
            files = canvas.get_paginated(
                f"/api/v1/courses/{course_id}/files?sort=updated_at&order=desc&per_page=50",
                max_pages=1,
            )
        except CanvasSessionError as exc:
            logger.info("Could not fetch study files for %s: %s", course_name, exc)
            continue

        kept = 0
        for file_data in files:
            updated_at = _parse_canvas_date(file_data.get("updated_at"))
            if updated_at is None or updated_at < cutoff or not _supported_canvas_study_file(file_data):
                continue
            try:
                size = int(file_data.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            if size > max_file_bytes:
                continue
            file_id = file_data.get("id")
            filename = file_data.get("display_name") or file_data.get("filename") or "Canvas material"
            if not file_id:
                continue
            candidates.append({
                "source": "canvas",
                "file_id": str(file_id),
                "title": f"[{course_name}] {filename}",
                "filename": str(filename),
                "course": str(course_name),
                "updated_at": updated_at.isoformat(),
            })
            kept += 1
            if kept >= per_course_limit:
                break

    return candidates


def queue_recent_canvas_study_files() -> int:
    """Queue new Canvas study materials for the existing nightly extractor."""
    try:
        import config
        with CanvasBrowserClient() as canvas:
            candidates = _get_canvas_study_files(canvas)
    except CanvasSessionError as exc:
        logger.warning("Canvas study-file queue skipped: %s", exc)
        return 0

    queue_path = Path(config.NIGHTLY_QUEUE_FILE)
    try:
        existing = json.loads(queue_path.read_text(encoding="utf-8")) if queue_path.exists() else []
    except (OSError, json.JSONDecodeError):
        existing = []
    if not isinstance(existing, list):
        existing = []

    known = {
        (str(item.get("source", "google_drive")), str(item.get("file_id", "")))
        for item in existing
        if isinstance(item, dict)
    }
    new_items = [
        item for item in candidates
        if (item["source"], item["file_id"]) not in known
    ]
    if not new_items:
        return 0

    existing.extend(new_items)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=queue_path.parent, delete=False, encoding="utf-8") as temporary:
        json.dump(existing, temporary, indent=2)
        temporary_path = Path(temporary.name)
    temporary_path.replace(queue_path)
    logger.info("Queued %d new Canvas study file(s) for nightly extraction.", len(new_items))
    return len(new_items)


def _get_canvas_announcements(canvas: CanvasBrowserClient, courses: list[dict[str, Any]] | None = None) -> str:
    courses = courses if courses is not None else canvas.get_favorite_courses()
    course_codes = [("context_codes[]", f"course_{course['id']}") for course in courses if course.get("id")]
    if not course_codes:
        return ""

    try:
        announcements = canvas.get_paginated(
            "/api/v1/announcements?" + urlencode(course_codes) + "&per_page=100",
            max_pages=1,
        )
    except CanvasSessionError as exc:
        logger.warning("Could not fetch Canvas announcements: %s", exc)
        return ""

    if not announcements:
        return "No recent Canvas announcements."

    recent_announcements = [
        announcement
        for announcement in announcements
        if _is_recent_canvas_update(announcement, "posted_at", "updated_at")
    ]
    if not recent_announcements:
        return "No recent Canvas announcements."

    lines = ["📢 **Canvas Announcements:**"]
    for announcement in recent_announcements[:5]:
        title = announcement.get("title", "No title")
        posted = _short_date(announcement.get("posted_at"), "")
        lines.append(f"- {title}" + (f" (posted {posted})" if posted else ""))
    return "\n".join(lines)


def get_canvas_announcements() -> str:
    """Fetch recent announcements from the normal ClassLink-authenticated Canvas session."""
    return _with_client(_get_canvas_announcements, unavailable="")


def _get_canvas_pages(canvas: CanvasBrowserClient, courses: list[dict[str, Any]] | None = None) -> str:
    courses = courses if courses is not None else canvas.get_favorite_courses()
    lines = ["📄 **Recently Updated Canvas Pages:**"]
    found = 0
    page_text_limit = max(200, int(get_setting("CANVAS_PAGE_TEXT_LIMIT", "1800")))

    for course in courses:
        course_id = course.get("id")
        course_name = course.get("name", "Unnamed course")
        if not course_id:
            continue
        try:
            pages = canvas.get_paginated(
                f"/api/v1/courses/{course_id}/pages?sort=updated_at&order=desc&per_page=3",
                max_pages=1,
            )
            recent_pages = [page for page in pages if _is_recent_canvas_update(page, "updated_at", "created_at")]
            for page in recent_pages[:3]:
                title = page.get("title", "Untitled")
                updated = _short_date(page.get("updated_at"), "")
                lines.append(f"- [{course_name}] {title}" + (f" (updated {updated})" if updated else ""))
                found += 1
                page_url = str(page.get("url") or "")
                if not page_url:
                    continue
                try:
                    page_detail = canvas.get_json(
                        f"/api/v1/courses/{course_id}/pages/{quote(page_url, safe='')}"
                    )
                    if not isinstance(page_detail, dict):
                        continue
                    page_text, _links = _parse_canvas_page_html(str(page_detail.get("body") or ""))
                    if page_text:
                        lines.append(f"  {page_text[:page_text_limit]}")
                    root_api_path = f"/api/v1/courses/{course_id}/pages/{quote(page_url, safe='')}"
                    for depth, linked in _crawl_canvas_page_links(
                        canvas,
                        str(course_id),
                        str(page_detail.get("body") or ""),
                        initial_seen={root_api_path},
                        context_terms=_context_terms(str(course_name), str(title), page_text),
                    ):
                        lines.append(f"{'  ' * depth}↳ {linked}")
                except CanvasSessionError as exc:
                    logger.info("Could not read Canvas page %s: %s", title, exc)
        except CanvasSessionError as exc:
            logger.info("Could not fetch pages for %s: %s", course_name, exc)

    return "\n".join(lines) if found else ""


def get_canvas_pages() -> str:
    """Fetch recently updated pages from the normal ClassLink-authenticated Canvas session."""
    return _with_client(_get_canvas_pages, unavailable="")


def download_canvas_file(file_id: str | int, output_path: str | Path) -> bool:
    """Download a queued study file through the live Canvas browser session."""
    try:
        with CanvasBrowserClient() as canvas:
            return canvas.download_file(file_id, output_path)
    except CanvasSessionError as exc:
        logger.warning("Could not download Canvas file %s: %s", file_id, exc)
        return False


def get_all_canvas_data() -> str:
    """Fetch assignments, announcements, and pages in one authenticated Firefox session."""
    try:
        with CanvasBrowserClient() as canvas:
            courses = canvas.get_favorite_courses()
            parts = [
                _get_upcoming_assignments(canvas, courses),
                _get_canvas_announcements(canvas, courses),
                _get_canvas_pages(canvas, courses),
            ]
            return "\n\n".join(part for part in parts if part) or "No Canvas data available."
    except CanvasSignInRequired as exc:
        logger.info("Canvas sign-in required: %s", exc)
        return f"Canvas sign-in required: {exc}"
    except CanvasSessionError as exc:
        logger.error("Canvas browser error: %s", exc)
        return f"Canvas unavailable: {exc}"


# ═════════════════════════════════════════════════════════════════════════════
# Playwright-style deep link navigator
# -----------------------------------------------------------------------------
# The production path reads Canvas through the authenticated Firefox/ClassLink
# daemon (CanvasBrowserClient, above). This section adds a browser-driven deep
# crawler for pages whose assignment data only exists in rendered DOM / clicked
# sub-pages (module trees, embedded viewers) rather than the JSON API.
#
# It is written against a minimal duck-typed "page" protocol so it can drive a
# real Playwright page OR a stub page in tests without importing Playwright (an
# optional dependency). The only place Playwright is imported is the lazy
# ``make_playwright_navigator`` factory. Every network/browser action funnels
# through the injected page object, so the unit suite stays fully offline.
# ═════════════════════════════════════════════════════════════════════════════

# Link text / heading keywords that mark a high-value academic page. Ordered by
# rough signal strength; scoring sums matched weights.
_RELEVANCE_KEYWORDS: dict[str, int] = {
    "syllabus": 5,
    "schedule": 5,
    "agenda": 4,
    "calendar": 4,
    "unit": 3,
    "homework": 3,
    "assignment": 3,
    "assessment": 3,
    "rubric": 3,
    "quiz": 3,
    "test": 3,
    "exam": 3,
    "project": 3,
    "reading": 2,
    "module": 2,
    "lesson": 2,
    "due": 2,
}

# Administrative / help anchors that should never be traversed.
_LOW_VALUE_MARKERS: tuple[str, ...] = (
    "help", "support", "logout", "log out", "sign out", "privacy", "terms",
    "accessibility", "profile", "settings", "notification", "inbox", "calendar/ical",
    "account", "commons", "conferences", "collaborations", "cookie", "feedback",
)

# External resource hosts we still consider relevant to click/resolve.
_RELEVANT_EXTERNAL_HOSTS: tuple[str, ...] = (
    "docs.google.com", "drive.google.com", "forms.gle", "canva.com",
    "gateway.cengage.com", "onenote", "sharepoint.com", "1drv.ms",
)


def _link_relevance_score(link_text: str, heading_context: str = "", href: str = "") -> int:
    """Score a link's academic relevance from its text, nearby heading, and href.

    Returns an integer score. ``0`` (or negative) means "skip" — the link is a
    generic administrative/help anchor. Higher means more likely to hold
    schedules, syllabi, or assignment data. This is the pre-filter that keeps the
    deep crawl focused and bounded.
    """
    haystack = f"{link_text} {heading_context}".lower().strip()
    href_low = (href or "").lower()

    # Hard skip obvious admin/help chrome (unless it also names a high-value term).
    if any(marker in haystack or marker in href_low for marker in _LOW_VALUE_MARKERS):
        if not any(kw in haystack for kw in ("syllabus", "schedule", "agenda", "unit")):
            return 0

    score = 0
    for keyword, weight in _RELEVANCE_KEYWORDS.items():
        if keyword in haystack:
            score += weight
    # A relevant external resource (Google Doc agenda, etc.) is worth a look even
    # when its anchor text is terse.
    if any(host in href_low for host in _RELEVANT_EXTERNAL_HOSTS):
        score += 2
    return score


def _normalize_url(url: str) -> str:
    """Canonicalize a URL for the visited-set: drop fragment, trailing slash."""
    if not url:
        return ""
    split = urlsplit(url)
    path = split.path.rstrip("/") or "/"
    return urlunsplit((split.scheme, split.netloc, path, split.query, ""))


class BrowserNavigator:
    """Recursive, relevance-filtered deep crawler over a duck-typed browser page.

    The ``page`` object must provide (sync) methods compatible with a Playwright
    page: ``goto(url)``, ``content() -> str`` (rendered HTML), and
    ``query_selector_all(selector) -> list`` where each element exposes
    ``get_attribute(name)`` and ``inner_text()``. ``screenshot()`` is optional and
    only used by the hybrid vision path.

    Loop safety: a normalized visited-URL set plus a hard ``max_depth`` (default
    2) and ``max_pages`` cap guarantee termination even with cyclic navigation.
    """

    def __init__(
        self,
        page: Any,
        *,
        base_url: str | None = None,
        max_depth: int = 2,
        max_pages: int = 40,
        min_relevance: int = 1,
    ) -> None:
        self.page = page
        self.base_url = (base_url or CANVAS_API_URL).rstrip("/")
        self.max_depth = max(0, int(max_depth))
        self.max_pages = max(1, int(max_pages))
        self.min_relevance = int(min_relevance)
        self.visited: set[str] = set()
        self.results: list[dict[str, Any]] = []

    # ── DOM link discovery ────────────────────────────────────────────────────
    def _discover_links(self) -> list[dict[str, Any]]:
        """Return relevance-scored candidate links on the current page.

        Uses the page's anchor elements; each candidate carries its resolved
        absolute href, anchor text, and a relevance score computed with nearby
        heading context when the page exposes it.
        """
        candidates: list[dict[str, Any]] = []
        try:
            anchors = self.page.query_selector_all("a")
        except Exception as exc:
            logger.debug("BrowserNavigator: link discovery failed: %s", type(exc).__name__)
            return []

        for anchor in anchors or []:
            try:
                href = anchor.get_attribute("href") or ""
                text = (anchor.inner_text() or "").strip()
            except Exception:
                continue
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            heading = ""
            # Optional: some fake/real pages expose a data-heading attribute for
            # the section a link sits under; use it when present.
            try:
                heading = anchor.get_attribute("data-heading") or ""
            except Exception:
                heading = ""
            absolute = urljoin(self.base_url + "/", href)
            score = _link_relevance_score(text, heading, absolute)
            candidates.append(
                {"url": absolute, "text": text, "score": score}
            )
        # Highest-value links first so the page/scale cap keeps the best pages.
        candidates.sort(key=lambda c: c["score"], reverse=True)
        return candidates

    def _capture_current(self, url: str, depth: int) -> dict[str, Any]:
        """Snapshot the current page's rendered HTML (and title if available)."""
        html_body = ""
        title = ""
        try:
            html_body = self.page.content() or ""
        except Exception as exc:
            logger.debug("BrowserNavigator: content() failed for %s: %s", url, type(exc).__name__)
        # Title is best-effort; a Playwright page exposes .title(), stubs may not.
        title_getter = getattr(self.page, "title", None)
        if callable(title_getter):
            try:
                title = title_getter() or ""
            except Exception:
                title = ""
        return {"url": url, "title": title, "html": html_body, "depth": depth}

    # ── Recursive crawl ────────────────────────────────────────────────────────
    def crawl(self, start_url: str, depth: int = 0) -> list[dict[str, Any]]:
        """Depth-first traverse from ``start_url`` collecting relevant pages.

        Returns the accumulated ``results`` list; each entry is
        ``{"url", "title", "html", "depth"}``. Safe against cycles and bounded by
        ``max_depth`` / ``max_pages``.
        """
        canonical = _normalize_url(start_url)
        if not canonical or canonical in self.visited:
            return self.results
        if len(self.results) >= self.max_pages:
            return self.results

        self.visited.add(canonical)
        try:
            self.page.goto(start_url)
        except Exception as exc:
            logger.debug("BrowserNavigator: goto(%s) failed: %s", start_url, type(exc).__name__)
            return self.results

        captured = self._capture_current(start_url, depth)
        self.results.append(captured)

        if depth >= self.max_depth or len(self.results) >= self.max_pages:
            return self.results

        for candidate in self._discover_links():
            if len(self.results) >= self.max_pages:
                break
            if candidate["score"] < self.min_relevance:
                continue  # relevance pre-filter: skip generic/admin links
            child = _normalize_url(candidate["url"])
            if not child or child in self.visited:
                continue  # loop detection
            self.crawl(candidate["url"], depth + 1)
        return self.results


def crawl_course_pages(
    page: Any,
    start_urls: list[str] | str,
    *,
    base_url: str | None = None,
    max_depth: int = 2,
    max_pages: int = 40,
    min_relevance: int = 1,
) -> list[dict[str, Any]]:
    """Deep-crawl one or more Canvas entry points with a browser page.

    Thin orchestration wrapper around :class:`BrowserNavigator` that shares a
    single visited-set across all ``start_urls`` (so the same page reached from
    the dashboard and the syllabus is fetched once). Returns collected page
    records; pass each ``html`` to the hybrid extractor.
    """
    if isinstance(start_urls, str):
        start_urls = [start_urls]
    navigator = BrowserNavigator(
        page,
        base_url=base_url,
        max_depth=max_depth,
        max_pages=max_pages,
        min_relevance=min_relevance,
    )
    for start_url in start_urls:
        navigator.crawl(start_url, depth=0)
    return navigator.results


def make_playwright_navigator(
    *,
    headless: bool = True,
    storage_state: str | None = None,
    **navigator_kwargs: Any,
):
    """Create a :class:`BrowserNavigator` backed by a real Playwright page.

    Lazily imports Playwright (an optional dependency; the production daemon uses
    Selenium/ClassLink). Returns ``(navigator, browser, playwright)`` so the
    caller can close the browser and stop Playwright when finished. Cookie
    persistence is provided via ``storage_state`` (a Playwright storage-state
    JSON path) so the authenticated ClassLink session is reused.

    Raises ``RuntimeError`` with an actionable message if Playwright is missing.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "Playwright is not installed. Install with "
            "`pip install playwright && playwright install firefox`, "
            "or use the Selenium-based CanvasBrowserClient daemon instead."
        ) from exc

    playwright = sync_playwright().start()
    browser = playwright.firefox.launch(headless=headless)
    context = browser.new_context(
        storage_state=storage_state if storage_state and os.path.exists(storage_state) else None
    )
    page = context.new_page()
    navigator = BrowserNavigator(page, **navigator_kwargs)
    return navigator, browser, playwright


if __name__ == "__main__":
    print(get_all_canvas_data())
