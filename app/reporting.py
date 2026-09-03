from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlmodel import Session, col, func, select

from .classify import (
    APPLICATION_UPDATE,
    ASSESSMENT,
    FOLLOW_UP_KINDS,
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


def parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def window_bounds(
    days: int = 1,
    since: str | date | None = None,
    until: str | date | None = None,
) -> tuple[datetime, datetime, str, int]:
    """Inclusive calendar window in UTC. `since`/`until` beat a rolling `days` lookback."""
    now = datetime.now(timezone.utc)
    start_day = parse_day(since) if isinstance(since, str) else since
    end_day = parse_day(until) if isinstance(until, str) else until

    if start_day or end_day:
        start_day = start_day or (end_day - timedelta(days=max(days, 1) - 1))
        end_day = end_day or now.date()
        if end_day < start_day:
            start_day, end_day = end_day, start_day
        start = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)
        end = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        span = (end_day - start_day).days + 1
        label = (
            start_day.isoformat()
            if start_day == end_day
            else f"{start_day.isoformat()} to {end_day.isoformat()}"
        )
        return start, end, label, span

    start = now - timedelta(days=days)
    label = f"last {days} day" + ("" if days == 1 else "s")
    return start, now, label, days


@dataclass
class Breakdown:
    days: int
    window_label: str = ""
    since: str = ""
    until: str = ""
    messages: int = 0
    categories: list[tuple[str, str, int]] = field(default_factory=list)  # key, label, count
    jobs_found: int = 0
    jobs_stored: int = 0
    jobs_matched: int = 0
    jobs_ignored: int = 0
    jobs_duplicates: int = 0
    outreach_open: int = 0
    acknowledgements: int = 0
    rejections: int = 0
    unclassified: int = 0
    not_job_mail: int = 0
    applications: list[tuple[str, int]] = field(default_factory=list)
    applications_open: int = 0
    scrape_status: list[tuple[str, int]] = field(default_factory=list)
    sources: list[tuple[str, int]] = field(default_factory=list)
    email_types: list[tuple[str, int]] = field(default_factory=list)
    top_jobs: list[Job] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "days": self.days,
            "window": self.window_label,
            "since": self.since,
            "until": self.until,
            "messages": self.messages,
            "categories": {key: count for key, _, count in self.categories},
            "jobs_found": self.jobs_found,
            "jobs_stored": self.jobs_stored,
            "jobs_matched": self.jobs_matched,
            "jobs_ignored": self.jobs_ignored,
            "jobs_duplicates": self.jobs_duplicates,
            "outreach_open": self.outreach_open,
            "acknowledgements": self.acknowledgements,
            "rejections": self.rejections,
            "unclassified": self.unclassified,
            "not_job_mail": self.not_job_mail,
            "applications": dict(self.applications),
            "applications_open": self.applications_open,
            "scrape_status": dict(self.scrape_status),
            "sources": dict(self.sources),
            "email_types": dict(self.email_types),
        }


def _count(session: Session, stmt) -> int:
    return int(session.exec(stmt).one() or 0)


def build_breakdown(
    session: Session,
    days: int = 1,
    since: str | date | None = None,
    until: str | date | None = None,
) -> Breakdown:
    start, end, label, span = window_bounds(days=days, since=since, until=until)
    in_window = (Message.received_at >= start, Message.received_at < end)
    job_window = (Job.received_at >= start, Job.received_at < end)
    report = Breakdown(
        days=span,
        window_label=label,
        since=start.date().isoformat(),
        until=(end - timedelta(seconds=1)).date().isoformat(),
    )

    rows = session.exec(
        select(Message.category, func.count()).where(*in_window).group_by(Message.category)
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
    report.acknowledgements = counts.get(APPLICATION_UPDATE, 0)
    report.rejections = counts.get(REJECTION, 0)
    report.unclassified = counts.get(OTHER, 0)
    report.not_job_mail = report.messages - counts.get(JOB_ALERT, 0)

    report.jobs_found = _count(
        session, select(func.coalesce(func.sum(Message.jobs_found), 0)).where(*in_window)
    )
    report.jobs_stored = _count(session, select(func.count()).select_from(Job).where(*job_window))
    report.jobs_matched = _count(
        session,
        select(func.count())
        .select_from(Job)
        .where(*job_window, Job.matched == True),  # noqa: E712
    )
    report.jobs_ignored = _count(
        session,
        select(func.count())
        .select_from(Job)
        .where(*job_window, Job.matched == False),  # noqa: E712
    )
    report.outreach_open = _count(
        session,
        select(func.count())
        .select_from(Outreach)
        .where(
            Outreach.received_at >= start,
            Outreach.received_at < end,
            Outreach.handled == False,  # noqa: E712
            col(Outreach.kind).in_(FOLLOW_UP_KINDS),
        ),
    )
    report.jobs_duplicates = _count(
        session,
        select(func.count())
        .select_from(Job)
        .where(*job_window, Job.duplicate_of != None),  # noqa: E711
    )

    # The application pipeline is a running total, not a window: a role you applied to last
    # month is still live today.
    report.applications = [
        (status, count)
        for status, count in session.exec(
            select(Application.status, func.count()).group_by(Application.status)
        ).all()
    ]
    report.applications_open = _count(
        session,
        select(func.count())
        .select_from(Application)
        .where(Application.closed == False),  # noqa: E712
    )

    report.scrape_status = [
        (status or "unknown", count)
        for status, count in session.exec(
            select(Job.scrape_status, func.count()).where(*job_window).group_by(Job.scrape_status)
        ).all()
    ]
    report.sources = [
        (source or "unknown", count)
        for source, count in session.exec(
            select(Job.source, func.count())
            .where(*job_window)
            .group_by(Job.source)
            .order_by(func.count().desc())
        ).all()
    ]
    report.email_types = [
        (kind or "unclassified", count)
        for kind, count in session.exec(
            select(Message.email_type, func.count())
            .where(*in_window, Message.category == OTHER)
            .group_by(Message.email_type)
        ).all()
    ]
    report.top_jobs = session.exec(
        select(Job)
        .where(*job_window, Job.matched == True)  # noqa: E712
        .order_by(col(Job.score).desc())
        .limit(10)
    ).all()
    return report
