"""Small scan marks for the dashboard. Same symbol = same meaning on every page."""

from .classify import (
    APPLICATION_UPDATE,
    ASSESSMENT,
    INTERVIEW,
    JOB_ALERT,
    NEXT_STEP,
    OFFER,
    OTHER,
    RECRUITER,
    REJECTION,
)
from .reporting import CATEGORY_LABELS

# Mail / follow-up kinds. Keep these distinct at 12px so a list can be skimmed.
CATEGORY = {
    JOB_ALERT: "📬",
    RECRUITER: "👤",
    INTERVIEW: "📅",
    ASSESSMENT: "🧪",
    NEXT_STEP: "➡️",
    OFFER: "🎉",
    REJECTION: "❌",
    APPLICATION_UPDATE: "📩",
    OTHER: "📎",
}

NAV = {
    "mail": "📬",
    "matches": "🎯",
    "followups": "👋",
    "applications": "🗂️",
    "overview": "📊",
    "charts": "📈",
    "cache": "🧹",
    "status": "🔄",
    "issues": "⚠️",
    "flags": "🚩",
    "misses": "🧩",
}

JOB_STATUS = {
    "new": "·",
    "saved": "⭐",
    "applied": "✅",
    "ignored": "—",
}

APP_STATUS = {
    "applied": "📩",
    "in_review": "👀",
    "next_step": "➡️",
    "assessment": "🧪",
    "interview": "📅",
    "offer": "🎉",
    "rejected": "❌",
}

SCRAPE = {
    "ok": "✓",
    "blocked": "🚫",
    "empty": "○",
    "error": "⚠",
    "skipped": "…",
}

SEVERITY = {
    "error": "⛔",
    "warn": "⚠️",
    "info": "ℹ️",
}

TABLES = {
    "category": CATEGORY,
    "nav": NAV,
    "job": JOB_STATUS,
    "app": APP_STATUS,
    "scrape": SCRAPE,
    "severity": SEVERITY,
}


def icon(key: str, kind: str = "category") -> str:
    return TABLES.get(kind, CATEGORY).get(key or "", "")


def marked(key: str, kind: str = "category") -> str:
    """Icon plus the human label, for tags and selects."""
    mark = icon(key, kind)
    if kind == "category":
        label = CATEGORY_LABELS.get(key, (key or "").replace("_", " ").title())
    else:
        label = (key or "").replace("_", " ").title()
    return f"{mark} {label}".strip() if mark else label
