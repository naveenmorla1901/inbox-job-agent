from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, col, func, select

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
from .models import Application, Job, Message, Outreach

CATEGORY_LABELS = {
    JOB_ALERT: "Job alerts",
    RECRUITER: "Recruiter outreach",
    INTERVIEW: "Interview invites",
    ASSESSMENT: "Assessments",
    NEXT_STEP: "Next steps asked of you",
    OFFER: "Offers",
    REJECTION: "Rejections",
    APPLICATION_UPDATE: "Application updates",
    OTHER: "Everything else",
}
CATEGORY_ORDER = list(CATEGORY_LABELS)


@dataclass
class Breakdown:
    days: int
    messages: int = 0
    categories: list[tuple[str, str, int]] = field(default_factory=list)  # key, label, count
    jobs_found: int = 0
    jobs_stored: int = 0
    jobs_matched: int = 0
    jobs_duplicates: int = 0
    outreach_open: int = 0
    applications: list[tuple[str, int]] = field(default_factory=list)
    applications_open: int = 0
    scrape_status: list[tuple[str, int]] = field(default_factory=list)
    sources: list[tuple[str, int]] = field(default_factory=list)
    top_jobs: list[Job] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "days": self.days,
            "messages": self.messages,
            "categories": {key: count for key, _, count in self.categories},
            "jobs_found": self.jobs_found,
            "jobs_stored": self.jobs_stored,
            "jobs_matched": self.jobs_matched,
            "jobs_duplicates": self.jobs_duplicates,
            "outreach_open": self.outreach_open,
            "applications": dict(self.applications),
            "applications_open": self.applications_open,
            "scrape_status": dict(self.scrape_status),
            "sources": dict(self.sources),
        }


def build_breakdown(session: Session, days: int = 1) -> Breakdown:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    report = Breakdown(days=days)

    rows = session.exec(
        select(Message.category, func.count())
        .where(Message.received_at >= since)
        .group_by(Message.category)
    ).all()
    counts = {category: count for category, count in rows}
    report.messages = sum(counts.values())
    report.categories = [
        (key, CATEGORY_LABELS[key], counts.get(key, 0)) for key in CATEGORY_ORDER
    ] + [
        (key, key.replace("_", " ").title(), count)
        for key, count in counts.items()
        if key not in CATEGORY_LABELS
    ]

    totals = session.exec(
        select(func.coalesce(func.sum(Message.jobs_found), 0)).where(Message.received_at >= since)
    ).one()
    report.jobs_found = int(totals or 0)

    report.jobs_stored = int(
        session.exec(
            select(func.count()).select_from(Job).where(Job.received_at >= since)
        ).one()
    )
    report.jobs_matched = int(
        session.exec(
            select(func.count())
            .select_from(Job)
            .where(Job.received_at >= since, Job.matched == True)  # noqa: E712
        ).one()
    )
    report.outreach_open = int(
        session.exec(
            select(func.count())
            .select_from(Outreach)
            .where(Outreach.received_at >= since, Outreach.handled == False)  # noqa: E712
        ).one()
    )

    report.jobs_duplicates = int(
        session.exec(
            select(func.count())
            .select_from(Job)
            .where(Job.received_at >= since, Job.duplicate_of != None)  # noqa: E711
        ).one()
    )

    # The application pipeline is a running total, not a window: a role you applied to last
    # month is still live today.
    report.applications = [
        (status, count)
        for status, count in session.exec(
            select(Application.status, func.count()).group_by(Application.status)
        ).all()
    ]
    report.applications_open = int(
        session.exec(
            select(func.count())
            .select_from(Application)
            .where(Application.closed == False)  # noqa: E712
        ).one()
    )

    report.scrape_status = [
        (status or "unknown", count)
        for status, count in session.exec(
            select(Job.scrape_status, func.count())
            .where(Job.received_at >= since)
            .group_by(Job.scrape_status)
        ).all()
    ]
    report.sources = [
        (source or "unknown", count)
        for source, count in session.exec(
            select(Job.source, func.count())
            .where(Job.received_at >= since)
            .group_by(Job.source)
            .order_by(func.count().desc())
        ).all()
    ]
    report.top_jobs = session.exec(
        select(Job)
        .where(Job.received_at >= since, Job.matched == True)  # noqa: E712
        .order_by(col(Job.score).desc())
        .limit(10)
    ).all()
    return report
