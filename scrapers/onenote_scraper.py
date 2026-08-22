"""Microsoft OneNote scraper — Graph API client and asset fetcher.

Responsibilities
----------------
* OAuth2: exchange a stored refresh token for a short-lived access token and
  persist the rotated refresh token (Microsoft rotates it on every refresh).
* List notebooks/sections/pages via ``/v1.0/me/onenote/pages``.
* Fetch a page's raw HTML via ``/pages/{id}/content`` (optionally with the
  ``includeInkML`` preview so digital-ink pages can be detected downstream).
* Download embedded resources (images, file attachments) referenced by a page's
  HTML ``<img data-fullres-src>`` / ``<object data>`` links.

Design notes
------------
All network access funnels through two seams — :meth:`OneNoteClient._request`
(Graph calls) and :meth:`OneNoteClient._refresh_access_token` (token endpoint) —
so the unit suite can mock exactly one boundary and stay fully offline. Nothing
here parses page layout; that is ``onenote_page_extractor``'s job.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

GRAPH_ROOT = os.getenv("ONENOTE_GRAPH_ROOT", "https://graph.microsoft.com/v1.0")
TOKEN_ENDPOINT = os.getenv(
    "ONENOTE_TOKEN_ENDPOINT",
    "https://login.microsoftonline.com/common/oauth2/v2.0/token",
)
# Device-code endpoint for the one-time interactive bootstrap (public client,
# no client secret required — works for personal/student Microsoft accounts).
DEVICE_CODE_ENDPOINT = os.getenv(
    "ONENOTE_DEVICE_CODE_ENDPOINT",
    "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode",
)
DEFAULT_SCOPE = os.getenv(
    "ONENOTE_SCOPE", "offline_access Notes.Read Notes.Read.All"
)

# Token store: reuse the app's private CONFIG_DIR when available.
try:
    from config import CONFIG_DIR

    _DEFAULT_TOKEN_PATH = CONFIG_DIR / "onenote_token.json"
except Exception:  # pragma: no cover
    _DEFAULT_TOKEN_PATH = Path.home() / ".config" / "personal-assistant-bot" / "onenote_token.json"


class OneNoteAuthError(RuntimeError):
    """Raised when no valid token can be obtained (missing/expired refresh)."""


class OneNoteRequestError(RuntimeError):
    """Raised for a non-retryable Graph API response."""


class OneNoteClient:
    """Authenticated Microsoft Graph client scoped to OneNote reads."""

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        token_path: str | Path | None = None,
        graph_root: str | None = None,
        request_timeout: float = 30.0,
    ) -> None:
        self.client_id = client_id or os.getenv("ONENOTE_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("ONENOTE_CLIENT_SECRET", "")
        self.graph_root = (graph_root or GRAPH_ROOT).rstrip("/")
        self.request_timeout = request_timeout
        self.token_path = Path(token_path) if token_path else _DEFAULT_TOKEN_PATH

        stored = self._load_token_store()
        self._refresh_token = refresh_token or stored.get("refresh_token", "")
        self._access_token = stored.get("access_token", "")
        # Absolute epoch second at which the cached access token expires.
        self._access_expiry = float(stored.get("expires_at", 0) or 0)

    # ── Token persistence ────────────────────────────────────────────────────
    def _load_token_store(self) -> dict[str, Any]:
        try:
            return json.loads(self.token_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save_token_store(self) -> None:
        try:
            self.token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.token_path.write_text(
                json.dumps(
                    {
                        "refresh_token": self._refresh_token,
                        "access_token": self._access_token,
                        "expires_at": self._access_expiry,
                    }
                )
            )
            try:
                self.token_path.chmod(0o600)
            except OSError:
                pass
        except OSError as exc:
            logger.debug("Could not persist OneNote token store: %s", exc)

    # ── OAuth2 ────────────────────────────────────────────────────────────────
    def _refresh_access_token(self) -> None:
        """Exchange the refresh token for a fresh access token.

        Network seam: unit tests patch this method (or ``requests.post``).
        Persists the rotated refresh token that Microsoft returns.
        """
        if not self._refresh_token:
            raise OneNoteAuthError("No OneNote refresh token configured")

        import requests  # lazy import keeps module import cheap/offline

        data = {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "scope": DEFAULT_SCOPE,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret

        try:
            resp = requests.post(TOKEN_ENDPOINT, data=data, timeout=self.request_timeout)
        except Exception as exc:
            raise OneNoteAuthError(f"Token endpoint unreachable: {type(exc).__name__}") from exc

        if resp.status_code != 200:
            raise OneNoteAuthError(
                f"Token refresh failed with HTTP {resp.status_code}: {resp.text[:200]}"
            )

        payload = resp.json()
        access = payload.get("access_token")
        if not access:
            raise OneNoteAuthError("Token endpoint returned no access_token")

        self._access_token = access
        # Microsoft rotates the refresh token on each use; keep the newest.
        if payload.get("refresh_token"):
            self._refresh_token = payload["refresh_token"]
        # Refresh 60s early to avoid a mid-flight expiry.
        expires_in = float(payload.get("expires_in", 3600) or 3600)
        self._access_expiry = time.time() + max(0.0, expires_in - 60.0)
        self._save_token_store()

    def _ensure_token(self) -> str:
        if not self._access_token or time.time() >= self._access_expiry:
            self._refresh_access_token()
        return self._access_token

    # ── One-time interactive bootstrap ────────────────────────────────────────
    def device_code_login(self, *, on_prompt=None, poll_interval: float = 5.0) -> None:
        """Acquire the first refresh token via the OAuth2 device-code flow.

        This is the one-time interactive step. It requests a device code, shows
        the user a URL + short code to enter on any browser, polls the token
        endpoint until the user completes sign-in, then persists the resulting
        access + refresh tokens to ``token_path``.

        Uses the public-client flow (no client secret), so it works with a
        personal/student Microsoft account that cannot create a confidential app.

        Args:
            on_prompt: optional callable ``(verification_uri, user_code, message)``
                for custom display. Defaults to printing the instructions.
            poll_interval: seconds between token polls (Graph dictates a minimum;
                the server-provided interval is honored when larger).
        """
        if not self.client_id:
            raise OneNoteAuthError(
                "ONENOTE_CLIENT_ID is required for device-code login. Register a "
                "public client app in Azure and set its Application (client) ID."
            )
        import requests

        # 1. Request a device code.
        try:
            resp = requests.post(
                DEVICE_CODE_ENDPOINT,
                data={"client_id": self.client_id, "scope": DEFAULT_SCOPE},
                timeout=self.request_timeout,
            )
        except Exception as exc:
            raise OneNoteAuthError(f"Device-code endpoint unreachable: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise OneNoteAuthError(
                f"Device-code request failed HTTP {resp.status_code}: {resp.text[:200]}"
            )
        flow = resp.json()
        device_code = flow.get("device_code")
        user_code = flow.get("user_code", "")
        verification_uri = flow.get("verification_uri", "https://microsoft.com/devicelogin")
        message = flow.get("message", "")
        interval = max(poll_interval, float(flow.get("interval", poll_interval) or poll_interval))
        expires_in = float(flow.get("expires_in", 900) or 900)
        if not device_code:
            raise OneNoteAuthError("Device-code response missing device_code")

        if on_prompt is not None:
            on_prompt(verification_uri, user_code, message)
        else:  # pragma: no cover - interactive path
            print("\n=== OneNote sign-in required ===")
            print(message or f"Open {verification_uri} and enter code: {user_code}")
            print("Waiting for you to complete sign-in in your browser...\n")

        # 2. Poll for completion.
        deadline = time.time() + expires_in
        while time.time() < deadline:
            time.sleep(interval)
            try:
                token_resp = requests.post(
                    TOKEN_ENDPOINT,
                    data={
                        "client_id": self.client_id,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device_code,
                    },
                    timeout=self.request_timeout,
                )
            except Exception as exc:
                logger.debug("Device-code poll transient error: %s", type(exc).__name__)
                continue

            payload = token_resp.json() if token_resp.content else {}
            if token_resp.status_code == 200 and payload.get("access_token"):
                self._access_token = payload["access_token"]
                if payload.get("refresh_token"):
                    self._refresh_token = payload["refresh_token"]
                expires = float(payload.get("expires_in", 3600) or 3600)
                self._access_expiry = time.time() + max(0.0, expires - 60.0)
                self._save_token_store()
                logger.info("OneNote device-code login complete; tokens persisted")
                return

            # authorization_pending / slow_down are expected while waiting.
            error = payload.get("error", "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            raise OneNoteAuthError(
                f"Device-code login failed: {error or token_resp.status_code}: "
                f"{payload.get('error_description', '')[:200]}"
            )
        raise OneNoteAuthError("Device-code login timed out before user completed sign-in")

    # ── HTTP seam ─────────────────────────────────────────────────────────────
    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        stream: bool = False,
        allow_reauth: bool = True,
    ):
        """Perform an authenticated Graph request and return the response.

        Accepts either an absolute URL (e.g. an ``@odata.nextLink``) or a path
        relative to the Graph root. Transparently refreshes the access token on a
        401 exactly once. This is the single Graph network boundary.
        """
        import requests

        token = self._ensure_token()
        url = (
            path_or_url
            if path_or_url.startswith("http")
            else urljoin(self.graph_root + "/", path_or_url.lstrip("/"))
        )
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.request(
            method, url, headers=headers, stream=stream, timeout=self.request_timeout
        )

        if resp.status_code == 401 and allow_reauth:
            # Access token may have been revoked/expired early; force one refresh.
            self._access_expiry = 0.0
            self._refresh_access_token()
            return self._request(method, path_or_url, stream=stream, allow_reauth=False)

        if resp.status_code >= 400:
            raise OneNoteRequestError(
                f"Graph {method} {url} -> HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return resp

    def _get_json(self, path_or_url: str) -> dict[str, Any]:
        resp = self._request("GET", path_or_url)
        try:
            data = resp.json()
        except ValueError as exc:
            raise OneNoteRequestError(f"Graph returned non-JSON for {path_or_url}") from exc
        return data if isinstance(data, dict) else {}


    # ── Page listing ──────────────────────────────────────────────────────────
    def list_pages(
        self,
        *,
        top: int = 50,
        max_pages: int = 20,
        order_by: str = "lastModifiedDateTime desc",
        select: str = "id,title,links,lastModifiedDateTime,contentUrl",
    ) -> list[dict[str, Any]]:
        """Return OneNote page metadata, following ``@odata.nextLink`` pagination."""
        params = f"?$top={int(top)}&$orderby={order_by.replace(' ', '%20')}&$select={select}"
        next_url: str | None = f"/me/onenote/pages{params}"
        pages: list[dict[str, Any]] = []
        fetched = 0
        while next_url and fetched < max_pages:
            data = self._get_json(next_url)
            batch = data.get("value", [])
            if isinstance(batch, list):
                pages.extend(item for item in batch if isinstance(item, dict))
            next_url = data.get("@odata.nextLink")
            fetched += 1
        return pages

    # ── Page content ──────────────────────────────────────────────────────────
    def get_page_html(self, page_id: str, *, include_ink: bool = False) -> str:
        """Fetch a page's raw HTML via ``/pages/{id}/content``.

        With ``include_ink=True`` the Graph ``includeInkML`` preview flag is set
        so digital-ink strokes are represented in the returned markup, letting the
        extractor detect handwriting-only pages.
        """
        if not page_id:
            return ""
        suffix = "?includeInkML=true" if include_ink else ""
        resp = self._request("GET", f"/me/onenote/pages/{page_id}/content{suffix}")
        return resp.text or ""

    # ── Asset download ──────────────────────────────────────────────────────────
    def download_resource(self, resource_url: str) -> bytes:
        """Download an embedded image/attachment referenced by a page.

        ``resource_url`` is typically a ``data-fullres-src`` or ``src`` value
        pointing at ``/me/onenote/resources/{id}/$value`` (relative or absolute).
        """
        if not resource_url:
            return b""
        resp = self._request("GET", resource_url, stream=True)
        return resp.content

    def download_page_assets(self, html_body: str, *, max_assets: int = 25) -> list[dict[str, Any]]:
        """Resolve and download every embedded asset URL found in page HTML.

        Returns a list of ``{"url", "kind", "content_type", "data"}`` dicts.
        Individual failures degrade to an entry with empty ``data`` rather than
        aborting the whole page.
        """
        assets: list[dict[str, Any]] = []
        for kind, url in _iter_asset_urls(html_body):
            if len(assets) >= max_assets:
                break
            try:
                resp = self._request("GET", url, stream=True)
                assets.append(
                    {
                        "url": url,
                        "kind": kind,
                        "content_type": resp.headers.get("Content-Type", ""),
                        "data": resp.content,
                    }
                )
            except (OneNoteRequestError, OneNoteAuthError) as exc:
                logger.debug("Could not download OneNote asset %s: %s", url, exc)
                assets.append({"url": url, "kind": kind, "content_type": "", "data": b""})
        return assets


# ── HTML asset discovery (no auth, pure parsing) ─────────────────────────────
_IMG_FULLRES_RE = re.compile(r'<img[^>]*\bdata-fullres-src="([^"]+)"', re.IGNORECASE)
_IMG_SRC_RE = re.compile(r'<img[^>]*\bsrc="([^"]+)"', re.IGNORECASE)
_OBJECT_DATA_RE = re.compile(r'<object[^>]*\bdata="([^"]+)"', re.IGNORECASE)


def _iter_asset_urls(html_body: str):
    """Yield ``(kind, url)`` pairs for embedded images and file attachments.

    Prefers full-resolution image URLs when present, de-duplicates, and skips
    inline ``data:`` URIs (already embedded, nothing to fetch).
    """
    if not html_body or not isinstance(html_body, str):
        return
    seen: set[str] = set()
    for url in _IMG_FULLRES_RE.findall(html_body):
        if url and not url.startswith("data:") and url not in seen:
            seen.add(url)
            yield "image", url
    for url in _IMG_SRC_RE.findall(html_body):
        if url and not url.startswith("data:") and url not in seen:
            seen.add(url)
            yield "image", url
    for url in _OBJECT_DATA_RE.findall(html_body):
        if url and not url.startswith("data:") and url not in seen:
            seen.add(url)
            yield "attachment", url
