"""Persist Gmail / LLM / scrape problems so the Issues page has timestamps."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, col, func, select

from .db import init_db, session_scope
from .models import Issue, utcnow

log = logging.getLogger(__name__)

KEEP = 400


def record_issue(
    source: str,
    title: str,
    detail: str = "",
    severity: str = "error",
    message_id: str = "",
) -> None:
    try:
        init_db()
        with session_scope() as session:
            session.add(
                Issue(
                    occurred_at=utcnow(),
                    source=source,
                    severity=severity,
                    title=(title or "error")[:240],
                    detail=(detail or "")[:8000],
                    message_id=message_id or "",
                )
            )
            session.commit()
            _prune(session)
            session.commit()
    except Exception:
        log.exception("could not record issue")


def _prune(session: Session) -> None:
    total = session.exec(select(func.count()).select_from(Issue)).one()
    extra = int(total) - KEEP
    if extra <= 0:
        return
    oldest = session.exec(select(Issue).order_by(col(Issue.occurred_at).asc()).limit(extra)).all()
    for row in oldest:
        session.delete(row)


def recent_issues(session: Session, *, hours: int = 168, source: str = "", limit: int = 200) -> list[Issue]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, hours))
    stmt = select(Issue).where(Issue.occurred_at >= since)
    if source:
        stmt = stmt.where(Issue.source == source)
    return list(session.exec(stmt.order_by(col(Issue.occurred_at).desc()).limit(limit)).all())


def issue_counts(session: Session, *, hours: int = 24) -> dict[str, int]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, hours))
    rows = session.exec(
        select(Issue.source, Issue.severity, func.count())
        .where(Issue.occurred_at >= since)
        .group_by(Issue.source, Issue.severity)
    ).all()
    out: dict[str, int] = {"total": 0, "error": 0, "warn": 0}
    for source, severity, count in rows:
        out["total"] += count
        out[severity] = out.get(severity, 0) + count
        out[source] = out.get(source, 0) + count
    return out
