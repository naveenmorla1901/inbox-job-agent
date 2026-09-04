from __future__ import annotations

import base64
import json
import logging
from typing import Any, Iterable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .config import Settings, get_settings

log = logging.getLogger(__name__)

READONLY_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
MODIFY_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def scopes_for(settings: Settings) -> list[str]:
    return MODIFY_SCOPES if settings.gmail_apply_label else READONLY_SCOPES


def load_credentials(settings: Settings | None = None) -> Credentials:
    """Load a cached OAuth token from disk or from the GMAIL_TOKEN_JSON env var."""
    settings = settings or get_settings()
    info: dict[str, Any] | None = None

    if settings.gmail_token_json.strip():
        info = json.loads(settings.gmail_token_json)
    else:
        token_path = settings.path(settings.gmail_token_file)
        if token_path.exists():
            info = json.loads(token_path.read_text(encoding="utf-8"))

    if not info:
        raise RuntimeError(
            "No Gmail token found. Run `python -m app.auth_setup` once on your laptop, "
            "then copy secrets/token.json (or its contents into GMAIL_TOKEN_JSON)."
        )

    creds = Credentials.from_authorized_user_info(info, scopes_for(settings))
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        if not settings.gmail_token_json.strip():
            settings.path(settings.gmail_token_file).write_text(creds.to_json(), encoding="utf-8")
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
