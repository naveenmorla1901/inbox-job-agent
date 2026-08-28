from __future__ import annotations

import logging

import httpx

from .config import Settings, get_settings
from .models import Job, Outreach

log = logging.getLogger(__name__)

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
URGENCY_MARK = {"high": "[URGENT] ", "normal": "", "low": ""}


def _escape(text: str) -> str:
    return (text or "").replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;")


class Notifier:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            log.info("notification (telegram disabled):\n%s", text)
            return False
        try:
            resp = httpx.post(
                TELEGRAM_URL.format(token=self.settings.telegram_bot_token),
                json={
                    "chat_id": self.settings.telegram_chat_id,
                    "text": text[:4000],
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            log.warning("telegram send failed: %s", exc)
            return False

    def outreach(self, item: Outreach) -> bool:
        mark = URGENCY_MARK.get(item.urgency, "")
        lines = [
            f"<b>{mark}{_escape(item.kind.replace('_', ' ').title())}</b>",
            f"From: {_escape(item.person or item.person_email)}"
            + (f" ({_escape(item.company)})" if item.company else ""),
            f"Subject: {_escape(item.subject)}",
        ]
        if item.role:
            lines.append(f"Role: {_escape(item.role)}")
        if item.summary:
            lines.append("")
            lines.append(_escape(item.summary))
        if item.action_required:
            lines.append(f"\nAction: {_escape(item.action_required)}")
        if item.gmail_link:
            lines.append(f'\n<a href="{item.gmail_link}">Open in Gmail</a>')
        return self.send("\n".join(lines))

    def jobs(self, jobs: list[Job]) -> bool:
        if not jobs:
            return False
        top = sorted(jobs, key=lambda j: j.score, reverse=True)[:10]
        lines = [f"<b>{len(jobs)} new matching job(s)</b>", ""]
        for job in top:
            title = _escape(job.title or "Untitled role")
            company = _escape(job.company or job.source)
            where = f" — {_escape(job.location)}" if job.location else ""
            lines.append(f'{job.score:.2f} <a href="{job.url}">{title}</a> @ {company}{where}')
        if len(jobs) > len(top):
            lines.append(f"\n+{len(jobs) - len(top)} more in the dashboard")
        return self.send("\n".join(lines))
