from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from .applications import dupe_key, record_email
from .classify import JOB_ALERT, Classification, classify_email
from .config import get_profile, get_settings
from .db import exists, get_state, init_db, session_scope, set_state
from .email_parse import ParsedEmail, parse_message
from .extract_jobs import JobCandidate, extract_from_email
from .gmail_client import GmailClient
from .llm import LLM
from .matcher import match_job
from .models import Application, Job, Message, Outreach
from .notify import Notifier
from .scrape import ScrapedJob, fetch_all, llm_extract

log = logging.getLogger(__name__)

STATE_CURSOR = "last_poll_epoch"
OVERLAP_SECONDS = 300


@dataclass
class RunStats:
    fetched: int = 0
    processed: int = 0
    skipped: int = 0
    job_alerts: int = 0
    jobs_found: int = 0
    jobs_stored: int = 0
    jobs_matched: int = 0
    outreach: int = 0
    applications_touched: int = 0
    application_status_changes: int = 0
    notified: int = 0
    categories: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    duration_s: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


def build_query(session: Session, since_days: int | None = None, override: str = "") -> str:
    settings = get_settings()
    if override:
        return override
    if since_days is not None:
        after = int((datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp())
    else:
        cursor = get_state(session, STATE_CURSOR)
        if cursor:
            after = max(0, int(cursor) - OVERLAP_SECONDS)
        else:
            lookback = timedelta(days=settings.gmail_initial_lookback_days)
            after = int((datetime.now(timezone.utc) - lookback).timestamp())
    return f"{settings.gmail_query} after:{after}".strip()


def _store_jobs(
    session: Session,
    email: ParsedEmail,
    candidates: list[JobCandidate],
    scraped: dict[str, ScrapedJob],
    llm: LLM | None = None,
) -> list[Job]:
    """Persist every posting found in the email; `matched` marks the ones worth your time."""
    profile = get_profile()
    settings = get_settings()
    stored: list[Job] = []

    for candidate in candidates:
        if exists(session, Job, url_key=candidate.url_key):
            continue

        page = scraped.get(candidate.url_key, ScrapedJob(status="skipped"))

        if not page.ok:
            # The page was blocked, empty or never fetched: try to salvage the fields with an
            # LLM over whatever text we do have (the alert email's own summary block).
            fallback_text = "\n".join(
                filter(None, [candidate.title, candidate.company, candidate.location, candidate.context, page.description])
            )
            rescued = llm_extract(fallback_text, llm, candidate)
            if rescued is not None:
                page = rescued

        title = page.title or candidate.title
        company = page.company or candidate.company
        location = page.location or candidate.location
        description = page.description or candidate.context
        extraction = page.extraction or ("email" if candidate.context else "")

        result = match_job(profile, title, description, location, company)
        is_match = not result.rejected and result.score >= settings.min_job_score

        # Same role from a second board or a repeat alert: keep the row, point it at the original.
        key = dupe_key(company, title)
        original = None
        if key:
            original = session.exec(
                select(Job).where(Job.dupe_key == key, Job.duplicate_of == None)  # noqa: E711
            ).first()

        job = Job(
            message_id=email.id,
            url=candidate.url,
            url_key=candidate.url_key,
            title=title[:200],
            company=company[:150],
            location=location[:150],
            source=candidate.source,
            description=description[:20000],
            score=result.score,
            title_score=result.title_score,
            skill_score=result.skill_score,
            resume_score=result.resume_score,
            matched_skills=", ".join(result.matched_skills)[:500],
            missing_skills=", ".join(result.missing_skills)[:500],
            verdict=result.verdict[:300],
            scraped=page.ok,
            scrape_status=page.status,
            extraction=extraction,
            matched=is_match,
            status="new" if is_match else "ignored",
            dupe_key=key,
            duplicate_of=original.id if original else None,
            received_at=email.received_at,
        )
        session.add(job)
        stored.append(job)

    return stored


def _store_outreach(session: Session, email: ParsedEmail, result: Classification) -> Outreach:
    item = Outreach(
        message_id=email.id,
        kind=result.category,
        person=result.person or email.sender_name,
        person_email=email.sender_email,
        company=result.company,
        role=result.role,
        subject=email.subject[:300],
        summary=result.summary or email.snippet[:300],
        action_required=result.action_required,
        urgency=result.urgency if result.urgency in ("high", "normal", "low") else "normal",
        gmail_link=email.gmail_link,
        received_at=email.received_at,
    )
    session.add(item)
    return item


@dataclass
class EmailResult:
    classification: Classification
    jobs: list[Job] = field(default_factory=list)
    outreach: Outreach | None = None
    jobs_found: int = 0
    application: Application | None = None
    status_changed: bool = False

    @property
    def matched_jobs(self) -> list[Job]:
        return [job for job in self.jobs if job.matched]


def process_email(session: Session, email: ParsedEmail, llm: LLM) -> EmailResult:
    settings = get_settings()
    candidates = extract_from_email(email, limit=settings.max_jobs_per_email)
    result = classify_email(email, get_profile(), llm, job_count=len(candidates))

    jobs: list[Job] = []
    outreach: Outreach | None = None
    application: Application | None = None
    status_changed = False

    if result.category == JOB_ALERT and candidates:
        scraped = (
            fetch_all(candidates, timeout=settings.scrape_timeout)
            if settings.scrape_job_pages
            else {}
        )
        jobs = _store_jobs(session, email, candidates, scraped, llm)
    elif result.is_outreach:
        outreach = _store_outreach(session, email, result)
        tracked = record_email(session, email, result)
        if tracked is not None:
            application, status_changed = tracked

    session.add(
        Message(
            id=email.id,
            thread_id=email.thread_id,
            sender=email.sender_name[:200],
            sender_email=email.sender_email[:200],
            subject=email.subject[:300],
            snippet=email.snippet[:500],
            received_at=email.received_at,
            category=result.category,
            confidence=result.confidence,
            reason=result.reason[:200],
            summary=result.summary[:1000],
            jobs_found=len(candidates),
            jobs_matched=sum(1 for job in jobs if job.matched),
        )
    )
    return EmailResult(
        classification=result,
        jobs=jobs,
        outreach=outreach,
        jobs_found=len(candidates),
        application=application,
        status_changed=status_changed,
    )


def run_once(
    max_messages: int | None = None, since_days: int | None = None, query: str = ""
) -> RunStats:
    started = time.time()
    stats = RunStats(started_at=datetime.now(timezone.utc).isoformat())
    settings = get_settings()
    init_db()

    llm = LLM(settings)
    notifier = Notifier(settings)
    gmail = GmailClient(settings)
    new_jobs: list[Job] = []
    pending_notifications: list[Outreach] = []
    latest_epoch = 0

    with session_scope() as session:
        search = build_query(session, since_days=since_days, override=query)
        limit = max_messages or settings.gmail_max_results
        message_ids = gmail.list_message_ids(search, limit)
        stats.fetched = len(message_ids)
        log.info("query=%r -> %d message(s)", search, len(message_ids))

        for message_id in message_ids:
            if session.get(Message, message_id):
                stats.skipped += 1
                continue
            try:
                email = parse_message(gmail.get_message(message_id))
                outcome = process_email(session, email, llm)
                session.commit()

                category = outcome.classification.category
                stats.processed += 1
                stats.jobs_found += outcome.jobs_found
                stats.jobs_stored += len(outcome.jobs)
                stats.jobs_matched += len(outcome.matched_jobs)
                stats.categories[category] = stats.categories.get(category, 0) + 1
                new_jobs.extend(outcome.matched_jobs)
                if category == JOB_ALERT:
                    stats.job_alerts += 1
                if outcome.application is not None:
                    stats.applications_touched += 1
                    stats.application_status_changes += int(outcome.status_changed)
                if outcome.outreach is not None:
                    stats.outreach += 1
                    if outcome.classification.should_notify and settings.notify_on_outreach:
                        pending_notifications.append(outcome.outreach)
                if (outcome.matched_jobs or outcome.outreach) and settings.gmail_apply_label:
                    gmail.add_label(message_id, settings.gmail_label_name)
                latest_epoch = max(latest_epoch, int(email.received_at.timestamp()))
            except Exception as exc:
                session.rollback()
                log.exception("failed on message %s", message_id)
                stats.errors.append(f"{message_id}: {exc}")

        for item in pending_notifications:
            if notifier.outreach(item):
                item.notified = True
                session.add(item)
                stats.notified += 1

        if new_jobs and settings.notify_on_jobs and notifier.jobs(new_jobs):
            stats.notified += 1

        if latest_epoch:
            set_state(session, STATE_CURSOR, str(latest_epoch))
        session.commit()

    stats.duration_s = round(time.time() - started, 2)
    log.info("run complete: %s", stats.as_dict())
    return stats
