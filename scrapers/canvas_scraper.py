"""Read Canvas data through an authenticated Firefox/ClassLink session.

Canvas personal access tokens are disabled for this account.  This module keeps
the existing scraper API, but makes read-only Canvas API requests from Firefox
after Selenium has completed the normal ClassLink sign-in flow.  The Firefox
profile holds the session; cookies are never copied into this process or saved
to a separate token file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

CANVAS_API_URL = os.getenv("CANVAS_API_URL", "https://canvas.instructure.com").rstrip("/")
CLASSLINK_URL = os.getenv("CANVAS_SSO_ENTRY_URL", "https://launchpad.classlink.com/forsyth")
DEFAULT_PROFILE_DIR = Path.home() / ".local" / "share" / "personal-assistant-bot" / "canvas-firefox-profile"
DEFAULT_DAEMON_URL = "http://127.0.0.1:8976"


class CanvasSessionError(RuntimeError):
    """Base error for a browser-backed Canvas request."""


class CanvasSignInRequired(CanvasSessionError):
    """The stored Firefox session is absent, expired, or needs user action."""


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _short_date(value: Any, fallback: str) -> str:
    return str(value)[:10] if value else fallback


class CanvasBrowserClient:
    """Read Canvas through the persistent local Firefox daemon.

    The daemon owns the Firefox process and its session cookie.  Keeping that
    process alive is necessary because Canvas uses a browser session rather
    than a personal API token.  ``use_daemon=False`` is reserved for the
    daemon process itself, which drives Selenium directly.
    """

    def __init__(self, *, headless: bool | None = None, use_daemon: bool | None = None) -> None:
        profile_value = os.getenv("CANVAS_FIREFOX_PROFILE_DIR", str(DEFAULT_PROFILE_DIR))
        self.profile_dir = Path(profile_value).expanduser().resolve()
        self.headless = _env_bool("CANVAS_BROWSER_HEADLESS", True) if headless is None else headless
        self.login_timeout = int(os.getenv("CANVAS_LOGIN_TIMEOUT_SECONDS", "90"))
        self.use_daemon = _env_bool("CANVAS_USE_BROWSER_DAEMON", True) if use_daemon is None else use_daemon
        self.daemon_url = os.getenv("CANVAS_BROWSER_DAEMON_URL", DEFAULT_DAEMON_URL).rstrip("/")
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
        username = os.getenv("CLASSLINK_USERNAME")
        password = os.getenv("CLASSLINK_PASSWORD")
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
        username_selector = os.getenv(
            "CLASSLINK_USERNAME_SELECTOR",
            "input[name='username'], input[name='user'], input#username, input#user, "
            "input#login, input#loginId, input[name='login'], input[name='loginId'], "
            "input#userNameInput, input[name='UserName'], input[name='userName'], "
            "input[name='identifier'], input[type='email'], input[autocomplete='username'], "
            "input[placeholder*='Username']",
        )
        password_selector = os.getenv(
            "CLASSLINK_PASSWORD_SELECTOR",
            "input[name='password'], input#password, input[type='password'], "
            "input#passwordInput, input[autocomplete='current-password']",
        )
        submit_selector = os.getenv(
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
        custom_selector = os.getenv("CLASSLINK_START_SELECTOR")
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

        selector = os.getenv("CLASSLINK_CANVAS_APP_SELECTOR")
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
    courses = courses if courses is not None else canvas.get_favorite_courses()
    assignments_text: list[str] = []

    for course in courses:
        course_id = course.get("id")
        course_name = course.get("name", "Unnamed course")
        if not course_id:
            continue
        try:
            assignments = canvas.get_paginated(
                f"/api/v1/courses/{course_id}/assignments?order_by=updated_at&order=desc&per_page=10",
                max_pages=1,
            )
            for assignment in assignments[:10]:
                assignments_text.append(
                    f"[{course_name}] {assignment.get('name', 'Untitled')} - "
                    f"Due: {_short_date(assignment.get('due_at'), 'No due date')} "
                    f"(Updated: {_short_date(assignment.get('updated_at'), 'Unknown')})"
                )
        except CanvasSessionError as exc:
            logger.warning("Could not fetch assignments for %s: %s", course_name, exc)

    if not assignments_text:
        return "No recent assignments found in your favorite courses!"
    return "📚 **Recent Canvas Assignments:**\n" + "\n".join(assignments_text)


def get_upcoming_assignments() -> str:
    """Fetch recent assignments from the normal ClassLink-authenticated Canvas session."""
    return _with_client(_get_upcoming_assignments)


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

    lines = ["📢 **Canvas Announcements:**"]
    for announcement in announcements[:5]:
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
            for page in pages[:3]:
                title = page.get("title", "Untitled")
                updated = _short_date(page.get("updated_at"), "")
                lines.append(f"- [{course_name}] {title}" + (f" (updated {updated})" if updated else ""))
                found += 1
        except CanvasSessionError as exc:
            logger.info("Could not fetch pages for %s: %s", course_name, exc)

    return "\n".join(lines) if found else ""


def get_canvas_pages() -> str:
    """Fetch recently updated pages from the normal ClassLink-authenticated Canvas session."""
    return _with_client(_get_canvas_pages, unavailable="")


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


if __name__ == "__main__":
    print(get_all_canvas_data())
