"""
Gmail API client — the single place OAuth happens.

This replaces the temporary `test/myemail.py` import that `backend.main` used
to lean on. `test/` is gitignored, so once the project is on GitHub that hack
would leave a clone with no auth code at all; everything funnels through here
instead.

Scope is read-only (`gmail.readonly`): the agent parses mail and drafts replies
as *text* the user copies into Gmail themselves. It never sends. Widening the
scope is a deliberate future decision, not an accident of this file.

Files (both resolved against the repo root — the parent of backend/, which is
also the CWD when you run `python -m backend.main` from there):

    credentials.json   OAuth *client* secret downloaded from Google Cloud.
                        You provide this; it is gitignored and never committed.
    token.json          The *user* token, written after the first consent and
                        refreshed automatically. Also gitignored.

Both paths can be overridden with GMAIL_CREDENTIALS_FILE / GMAIL_TOKEN_FILE.

First run opens a browser for Google consent. After that the token is reused
and silently refreshed. Note: while the Google Cloud app is in "Testing" mode
the refresh token expires after 7 days — when that happens this just runs the
consent flow again (delete token.json to force it sooner).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

load_dotenv()

# Read-only on purpose — see module docstring. Changing this list invalidates
# any existing token.json (the user must re-consent).
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Repo root = parent of backend/. The OAuth files live there, next to CLAUDE.md.
_REPO_ROOT = Path(__file__).resolve().parent.parent

_CREDENTIALS_PATH = Path(
    os.getenv("GMAIL_CREDENTIALS_FILE", _REPO_ROOT / "credentials.json")
)
_TOKEN_PATH = Path(os.getenv("GMAIL_TOKEN_FILE", _REPO_ROOT / "token.json"))


def _load_saved_credentials() -> Credentials | None:
    """Return the stored user credentials, or None if there is no token file."""
    if not _TOKEN_PATH.exists():
        return None
    return Credentials.from_authorized_user_file(str(_TOKEN_PATH), SCOPES)


def _ensure_valid(creds: Credentials | None) -> Credentials:
    """Return usable credentials, doing the least work needed:

      - valid as-is                 -> return them
      - expired but refreshable     -> refresh silently
      - missing / unrefreshable     -> run the browser consent flow

    The (re)obtained token is written back to token.json so the next run is
    non-interactive.
    """
    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:  # noqa: BLE001 — refresh token revoked/expired (7-day testing cap)
            creds = None

    if not creds or not creds.valid:
        if not _CREDENTIALS_PATH.exists():
            raise FileNotFoundError(
                f"Gmail OAuth client secret not found at {_CREDENTIALS_PATH}. "
                "Download it from Google Cloud Console (OAuth client ID, type "
                '"Desktop app") and save it there, or set GMAIL_CREDENTIALS_FILE. '
                "See Oauthsetup.md."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(_CREDENTIALS_PATH), SCOPES
        )
        creds = flow.run_local_server(port=0)

    _TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


def get_service() -> Resource:
    """Return an authenticated Gmail API service (read-only).

    On first call this may open a browser for Google consent; afterwards the
    saved token is reused and auto-refreshed. This is the only function the
    rest of the backend should import for Gmail access.
    """
    creds = _ensure_valid(_load_saved_credentials())
    return build("gmail", "v1", credentials=creds)


if __name__ == "__main__":
    # Smoke test: authenticate and print the mailbox address + total threads.
    service = get_service()
    profile = service.users().getProfile(userId="me").execute()
    print(f"Authenticated as {profile['emailAddress']} "
          f"({profile.get('messagesTotal', '?')} messages).")
