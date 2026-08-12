#!/usr/bin/env python3
"""Explicit, local-only Google OAuth token generator.

Run this on the machine that has the browser and the Google client-secret file.
It writes a private token file locally; deployment of that token is an explicit
operator action and is deliberately not performed by this script.
"""
from __future__ import annotations

import argparse
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config


SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]
MIN_VALID_DAYS = 3
# The checked-in desktop OAuth client registers the loopback redirect as
# ``http://localhost``.  Keep the generated redirect URI on that hostname;
# using the numerically equivalent 127.0.0.1 changes the URI and Google can
# reject it with ``redirect_uri_mismatch`` after the user signs in.
OAUTH_LOOPBACK_HOST = "localhost"


def _write_private_token(destination: Path, token_json: str) -> None:
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        destination.parent.chmod(0o700)
    except OSError:
        pass
    if destination.is_symlink():
        raise OSError("refusing to write OAuth token through a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".token-", suffix=".tmp", dir=destination.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token_json)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        destination.chmod(0o600)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def token_is_fresh(token_path: Path) -> bool:
    """Return whether the existing token remains valid for the safety window."""
    if not token_path.is_file() or token_path.is_symlink():
        return False
    try:
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except Exception:
        return False
    if not creds or not creds.valid or not creds.expiry:
        return False
    expiry = creds.expiry if creds.expiry.tzinfo else creds.expiry.replace(tzinfo=timezone.utc)
    return expiry - datetime.now(timezone.utc) >= timedelta(days=MIN_VALID_DAYS)


def do_auth(credentials_path: Path, output_path: Path, *, open_browser: bool) -> None:
    """Run the loopback-only browser OAuth flow and store the resulting token."""
    if not credentials_path.is_file() or credentials_path.is_symlink():
        raise FileNotFoundError(f"Google OAuth client secret is unavailable: {credentials_path}")
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(
        host=OAUTH_LOOPBACK_HOST,
        port=0,
        prompt="consent",
        open_browser=open_browser,
        success_message="Authentication successful. You may close this tab.",
    )
    _write_private_token(output_path, creds.to_json())


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a local private Google OAuth token.")
    parser.add_argument("--force", action="store_true", help="Authenticate even if the current token is fresh.")
    parser.add_argument("--credentials", type=Path, default=config.CREDENTIALS_PATH, help="OAuth client-secret JSON path.")
    parser.add_argument("--output", type=Path, default=config.TOKEN_PATH, help="Private token output path.")
    parser.add_argument("--no-browser", action="store_true", help="Print the local authorization URL instead of opening a browser.")
    args = parser.parse_args()

    config.initialize_runtime()
    output_path = args.output.expanduser()
    if not args.force and token_is_fresh(output_path):
        print("Existing token is still fresh; use --force to re-authenticate.")
        return 0
    try:
        do_auth(args.credentials.expanduser(), output_path, open_browser=not args.no_browser)
    except Exception as exc:
        print(f"Google OAuth did not complete ({type(exc).__name__}).")
        return 1
    print(f"Token written locally with mode 0600: {output_path}")
    print("Install it through your approved secret-management/deployment workflow; this script does not copy files or restart services.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
