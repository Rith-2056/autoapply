"""Gmail access: read-only OAuth (desktop loopback flow) and message fetching."""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .classify import EmailMessage
from .crypto import decrypt, encrypt

log = logging.getLogger("autoapply.mail.gmail")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
DEFAULT_QUERY = ('newer_than:30d -category:promotions -category:social '
                 '(application OR applied OR interview OR assessment OR "coding challenge" OR hackerrank OR codesignal OR '
                 'recruiter OR internship OR offer OR candidate OR position)')


def _html_to_text(html: str) -> str:
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#39;|&apos;", "'", text)
    text = re.sub(r"&quot;", '"', text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def _walk_parts(payload: dict[str, Any]) -> tuple[str, str, list[str]]:
    plain, html, urls = "", "", []
    stack = [payload]
    while stack:
        part = stack.pop()
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data:
            try:
                decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                decoded = ""
            if mime == "text/plain":
                plain += decoded + "\n"
            elif mime == "text/html":
                html += decoded + "\n"
                urls += re.findall(r'href="(https?://[^"]+)"', decoded)
        stack.extend(part.get("parts", []) or [])
    text = plain.strip() or _html_to_text(html)
    urls += re.findall(r"https?://[^\s<>\")]+", plain)
    return text, html, list(dict.fromkeys(urls))


class GmailClient:
    def __init__(self, project_root: Path, credentials_file: Path, token_enc: bytes | None = None):
        self.project_root = project_root
        self.credentials_file = credentials_file
        self.token_enc = token_enc
        self._service = None
        self._creds = None

    # -- auth -------------------------------------------------------------- #

    def connect_interactive(self, port: int = 0) -> tuple[bytes, str]:
        """Run the desktop OAuth flow (opens the browser). Returns (encrypted token, email)."""
        from google_auth_oauthlib.flow import InstalledAppFlow

        if not self.credentials_file.exists():
            raise FileNotFoundError(
                f"Google OAuth client file not found at {self.credentials_file}. Create a Desktop OAuth client in Google Cloud "
                "Console (Gmail API enabled) and save it there, or set GOOGLE_OAUTH_CLIENT_FILE in .env.")
        flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_file), SCOPES)
        creds = flow.run_local_server(port=port, open_browser=True, prompt="consent")
        self._creds = creds
        self.token_enc = encrypt(self.project_root, creds.to_json().encode())
        email = self.profile_email()
        return self.token_enc, email

    def _load_creds(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        if self._creds is None:
            if not self.token_enc:
                raise RuntimeError("Gmail is not connected")
            info = json.loads(decrypt(self.project_root, self.token_enc).decode())
            self._creds = Credentials.from_authorized_user_info(info, SCOPES)
        if self._creds.expired and self._creds.refresh_token:
            self._creds.refresh(Request())
            self.token_enc = encrypt(self.project_root, self._creds.to_json().encode())
        return self._creds

    def service(self):
        if self._service is None:
            from googleapiclient.discovery import build

            self._service = build("gmail", "v1", credentials=self._load_creds(), cache_discovery=False)
        return self._service

    def profile_email(self) -> str:
        try:
            return self.service().users().getProfile(userId="me").execute().get("emailAddress", "")
        except Exception as e:  # noqa: BLE001
            log.warning("could not read Gmail profile: %s", e)
            return ""

    # -- fetch ------------------------------------------------------------- #

    def list_message_ids(self, query: str = DEFAULT_QUERY, max_results: int = 100) -> list[str]:
        svc = self.service()
        ids: list[str] = []
        page_token = None
        while len(ids) < max_results:
            resp = svc.users().messages().list(userId="me", q=query, maxResults=min(100, max_results - len(ids)), pageToken=page_token).execute()
            ids += [m["id"] for m in resp.get("messages", [])]
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids

    def fetch(self, message_id: str) -> EmailMessage:
        msg = self.service().users().messages().get(userId="me", id=message_id, format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        text, _html, urls = _walk_parts(msg.get("payload", {}))
        ts = int(msg.get("internalDate", "0")) / 1000
        received = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().replace(tzinfo=None) if ts else datetime.now()
        return EmailMessage(id=message_id, subject=headers.get("subject", ""), sender=headers.get("from", ""), body=text[:20000],
                            received_at=received, snippet=msg.get("snippet", ""), urls=urls, to=headers.get("to", ""))
