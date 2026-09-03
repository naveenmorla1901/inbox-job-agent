from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr
from html import unescape

from bs4 import BeautifulSoup

from .gmail_client import decode_part, walk_parts

WHITESPACE = re.compile(r"[ \t\r\f\v]+")
BLANKLINES = re.compile(r"\n{3,}")


@dataclass
class Link:
    url: str
    text: str = ""


@dataclass
class ParsedEmail:
    id: str
    thread_id: str = ""
    sender_name: str = ""
    sender_email: str = ""
    to: str = ""
    subject: str = ""
    snippet: str = ""
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    text: str = ""
    html: str = ""
    links: list[Link] = field(default_factory=list)
    label_ids: list[str] = field(default_factory=list)

    @property
    def sender_domain(self) -> str:
        return self.sender_email.split("@")[-1].lower() if "@" in self.sender_email else ""

    @property
    def gmail_link(self) -> str:
        return f"https://mail.google.com/mail/u/0/#inbox/{self.id}"

    def body(self, limit: int = 20000) -> str:
        return (self.text or html_to_text(self.html))[:limit]


def html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    return clean_text(soup.get_text("\n"))


def clean_text(text: str) -> str:
    text = unescape(text or "")
    text = WHITESPACE.sub(" ", text.replace("\u200c", "").replace("\xa0", " "))
    text = "\n".join(line.strip() for line in text.split("\n"))
    return BLANKLINES.sub("\n\n", text).strip()


def extract_links(html: str) -> list[Link]:
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    links: list[Link] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.lower().startswith("http") or href in seen:
            continue
        seen.add(href)
        links.append(Link(url=href, text=clean_text(a.get_text(" "))[:200]))
    return links


def parse_message(msg: dict) -> ParsedEmail:
    payload = msg.get("payload", {}) or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", []) or []}
    sender_name, sender_email = parseaddr(headers.get("from", ""))

    text_parts: list[str] = []
    html_parts: list[str] = []
    for part in walk_parts(payload):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        try:
            content = decode_part(data)
        except Exception:
            continue
        if mime == "text/plain":
            text_parts.append(content)
        elif mime == "text/html":
            html_parts.append(content)

    html = "\n".join(html_parts)
    text = clean_text("\n".join(text_parts)) or html_to_text(html)

    ts = msg.get("internalDate")
    received = (
        datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
        if ts
        else datetime.now(timezone.utc)
    )

    return ParsedEmail(
        id=msg.get("id", ""),
        thread_id=msg.get("threadId", ""),
        sender_name=sender_name or sender_email,
        sender_email=sender_email.lower(),
        to=headers.get("to", ""),
        subject=headers.get("subject", ""),
        snippet=clean_text(unescape(msg.get("snippet", ""))),
        received_at=received,
        text=text,
        html=html,
        links=extract_links(html),
        label_ids=msg.get("labelIds", []) or [],
    )
