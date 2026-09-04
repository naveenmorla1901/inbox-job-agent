from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .config import Settings, get_settings

log = logging.getLogger(__name__)

READONLY_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
MODIFY_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

REAL_DASHBOARD_URL = "https://inbox-job-agent-244210842384.us-east1.run.app"
REAL_SERVICE = "inbox-job-agent"
REAL_REGION = "us-east1"

_SECRET_MOUNT_PATHS = (
    Path("/secrets/gmail-token"),
    Path("/secrets/token.json"),
    Path("/var/secrets/gmail-token"),
    Path("/gmail-token"),
)


def scopes_for(settings: Settings) -> list[str]:
    return MODIFY_SCOPES if settings.gmail_apply_label else READONLY_SCOPES


def _read_if_file(path: Path) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8")
    if path.is_dir():
        for name in ("token.json", "gmail-token", "latest"):
            nested = path / name
            if nested.is_file():
                return nested.read_text(encoding="utf-8")
        files = [item for item in path.iterdir() if item.is_file()]
        if len(files) == 1:
            return files[0].read_text(encoding="utf-8")
    return ""


def read_gmail_token_text(settings: Settings | None = None) -> str:
    """Return token JSON from env, a file path in that env, or a Cloud Run mount."""
    settings = settings or get_settings()
    raw = (settings.gmail_token_json or "").strip()
    if raw:
        if raw.lstrip().startswith("{"):
            return raw
        mounted = _read_if_file(Path(raw))
        if mounted.strip():
            return mounted
    for path in (settings.path(settings.gmail_token_file), *_SECRET_MOUNT_PATHS):
        mounted = _read_if_file(path)
        if mounted.strip():
            return mounted
    return ""


def gmail_token_present(settings: Settings | None = None) -> bool:
    return bool(read_gmail_token_text(settings).strip())


def cloud_service_name() -> str:
    return (os.environ.get("K_SERVICE") or "").strip()


def is_github_copy_service(service: str | None = None) -> bool:
    name = service if service is not None else cloud_service_name()
    return name.endswith("-git") or name == f"{REAL_SERVICE}-git"


def token_missing_message(service: str | None = None) -> str:
    name = service if service is not None else cloud_service_name()
    if is_github_copy_service(name):
        return (
            f"This site is Cloud Run service `{name}` — a second copy created when "
            "GitHub was connected. It has no Gmail token, database, or dashboard password. "
            f"Open {REAL_DASHBOARD_URL} (service `{REAL_SERVICE}` in {REAL_REGION}). "
            f"Then delete `{name}`."
        )
    if name:
        return (
            f"Cloud Run service `{name}` has no GMAIL_TOKEN_JSON. "
            f"Open `{REAL_SERVICE}` in region {REAL_REGION} (not `{REAL_SERVICE}-git`). "
            "Edit & deploy new revision → Variables & secrets → Reference a secret → "
            "secret `gmail-token`, exposed as environment variable `GMAIL_TOKEN_JSON`, version latest."
        )
    return (
        "No Gmail token found. On your laptop run `python -m app.auth_setup`. "
        f"On Cloud Run, attach secret `gmail-token` as env `GMAIL_TOKEN_JSON` on "
        f"`{REAL_SERVICE}` in {REAL_REGION} — do not use `{REAL_SERVICE}-git`."
    )


def host_setup(settings: Settings | None = None) -> dict[str, Any]:
    """Dashboard flags for the current process (Cloud Run vs laptop)."""
    settings = settings or get_settings()
    service = cloud_service_name()
    gmail_ok = gmail_token_present(settings)
    postgres = (settings.database_url or "").startswith("postgresql")
    auth_ok = bool(settings.api_token) and settings.api_token != "change-me"
    wrong_service = is_github_copy_service(service)
    warning = ""
    if service and (wrong_service or not gmail_ok):
        warning = token_missing_message(service)
    return {
        "on_cloud": bool(service),
        "service": service,
        "wrong_service": wrong_service,
        "gmail_ok": gmail_ok,
        "postgres": postgres,
        "auth_ok": auth_ok,
        "real_url": REAL_DASHBOARD_URL,
        "real_service": REAL_SERVICE,
        "real_region": REAL_REGION,
        "warning": warning,
    }


def load_credentials(settings: Settings | None = None) -> Credentials:
    """Load a cached OAuth token from disk or from the GMAIL_TOKEN_JSON env var."""
    settings = settings or get_settings()
    text = read_gmail_token_text(settings)
    if not text.strip():
        raise RuntimeError(token_missing_message())

    info = json.loads(text)
    if not isinstance(info, dict):
        raise RuntimeError("Gmail token JSON must be an object with client_id and refresh_token.")

    creds = Credentials.from_authorized_user_info(info, scopes_for(settings))
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        inline = (settings.gmail_token_json or "").strip()
        if not inline or not inline.lstrip().startswith("{"):
            token_path = settings.path(settings.gmail_token_file)
            try:
                token_path.parent.mkdir(parents=True, exist_ok=True)
                token_path.write_text(creds.to_json(), encoding="utf-8")
            except OSError:
                log.warning("could not write refreshed Gmail token to %s", token_path)
    return creds


class GmailClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.service = build(
            "gmail", "v1", credentials=load_credentials(self.settings), cache_discovery=False
        )
        self._label_id: str | None = None

    def list_message_ids(self, query: str, max_results: int) -> list[str]:
        ids: list[str] = []
        page_token = None
        while len(ids) < max_results:
            resp = (
                self.service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    maxResults=min(100, max_results - len(ids)),
                    pageToken=page_token,
                )
                .execute()
            )
            ids.extend(m["id"] for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids

    def get_message(self, message_id: str) -> dict[str, Any]:
        return (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

    def ensure_label(self, name: str) -> str:
        if self._label_id:
            return self._label_id
        labels = self.service.users().labels().list(userId="me").execute().get("labels", [])
        for label in labels:
            if label["name"].lower() == name.lower():
                self._label_id = label["id"]
                return self._label_id
        created = (
            self.service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        self._label_id = created["id"]
        return self._label_id

    def watch_inbox(self, topic: str) -> dict[str, Any]:
        """Ask Gmail to publish a notification to `topic` when INBOX changes.

        The watch expires after about 7 days and must be renewed.
        """
        return (
            self.service.users()
            .watch(
                userId="me",
                body={
                    "topicName": topic,
                    "labelIds": ["INBOX"],
                    "labelFilterBehavior": "INCLUDE",
                },
            )
            .execute()
        )

    def add_label(self, message_id: str, name: str) -> None:
        try:
            label_id = self.ensure_label(name)
            self.service.users().messages().modify(
                userId="me", id=message_id, body={"addLabelIds": [label_id]}
            ).execute()
        except Exception as exc:  # labeling is a nicety, never fail the pipeline for it
            log.warning("could not label %s: %s", message_id, exc)


def parse_gmail_push(body: dict[str, Any]) -> dict[str, Any]:
    """Decode a Cloud Pub/Sub push envelope from Gmail's watch notification."""
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, dict):
        return {}
    data = message.get("data")
    if not data:
        return {}
    try:
        raw = base64.b64decode(data)
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def walk_parts(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    stack = [payload]
    while stack:
        part = stack.pop()
        yield part
        stack.extend(part.get("parts", []) or [])
