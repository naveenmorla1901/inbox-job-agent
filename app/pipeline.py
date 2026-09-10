from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, col, func, select

from .applications import dupe_key, record_email
from .classify import (
    APPLICATION_UPDATE,
    JOB_ALERT,
    OTHER,
    Classification,
    classify_email,
)
from .config import get_profile, get_settings
from .db import exists, get_state, init_db, session_scope, set_state
from .email_parse import Link, ParsedEmail, parse_message
from .extract_jobs import JobCandidate, extract_from_email
from .llm_extract import extract_postings
from .gmail_client import GmailClient
from .job_fields import enrich_fields, normalize_visa, phone_from_text, scheduling_url_from_links
from .llm import LLM
from .matcher import match_job, promote_empty_scrape_match, title_worth_scraping
from .issues import record_issue
from .models import Application, ApplicationEvent, Issue, Job, Message, Outreach, PollRun
from .notify import Notifier
from .schedule import next_tick_epoch, tick_window
from .scrape import SHELL_TITLE, ScrapedJob, fetch_all, fetch_job, llm_extract, notable_scrape_failures
from .timefmt import EASTERN, as_et, fmt_et, parse_et_datetime

log = logging.getLogger(__name__)

STATE_CURSOR = "last_poll_epoch"
STATE_ORIGIN = "poll_origin_epoch"
STATE_BOOT = "poll_boot_id"
STATE_WATCH = "gmail_watch_expiration"
STATE_LAST_RUN = "last_run_json"
STATE_POLL = "poll_progress"
OVERLAP_SECONDS = 300
BODY_STORE_CHARS = 40000


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
    emails: list[dict] = field(default_factory=list)
    started_at: str = ""
    duration_s: float = 0.0
    window_start: int = 0
    window_end: int = 0
    trigger: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def build_query(
    session: Session,
    since_days: int | None = None,
    override: str = "",
    after_epoch: int | None = None,
    before_epoch: int | None = None,
) -> str:
    settings = get_settings()
    if override:
        return override
    parts = [settings.gmail_query.strip()]
    if after_epoch is not None:
        # Gmail `after:` is exclusive of that second; step back so 6:00:00 is in 6:00–6:15.
        parts.append(f"after:{max(0, int(after_epoch) - 1)}")
        if before_epoch is not None:
            parts.append(f"before:{int(before_epoch)}")
        return " ".join(p for p in parts if p)
    if since_days is not None:
        after = int((datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp())
    else:
        cursor = get_state(session, STATE_CURSOR)
        if cursor:
            after = max(0, int(cursor) - OVERLAP_SECONDS)
        else:
            lookback = timedelta(days=settings.gmail_initial_lookback_days)
            after = int((datetime.now(timezone.utc) - lookback).timestamp())
    parts.append(f"after:{after}")
    return " ".join(p for p in parts if p)


def boot_id() -> str:
    """Cloud Run revision, or this local process. Cold starts of the same revision reuse it."""
    revision = (os.environ.get("K_REVISION") or "").strip()
    if revision:
        return f"cloud:{revision}"
    return f"local:{os.getpid()}"


def _int_state(session: Session, key: str, default: int = 0) -> int:
    raw = get_state(session, key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def ensure_poll_origin(
    session: Session,
    when: datetime | None = None,
    identity: str | None = None,
) -> dict:
    """On a new local process or Cloud Run revision, skip mail from before now.

    Same Cloud Run revision waking from sleep keeps the cursor, so idle time is not dropped.
    """
    identity = identity or boot_id()
    now_ts = int((when or datetime.now(timezone.utc)).timestamp())
    previous = get_state(session, STATE_BOOT)
    if previous == identity:
        origin = _int_state(session, STATE_ORIGIN, now_ts)
        cursor = _int_state(session, STATE_CURSOR, origin)
        return {"reset": False, "origin": origin, "cursor": cursor, "boot": identity}
    set_state(session, STATE_BOOT, identity)
    set_state(session, STATE_ORIGIN, str(now_ts))
    set_state(session, STATE_CURSOR, str(now_ts))
    session.add(
        PollRun(
            trigger="boot",
            status="ok",
            window_start=now_ts,
            window_end=now_ts,
            note="Deploy started polling. Mail from before this time is skipped on purpose.",
        )
    )
    log.info("poll origin set to now (%s); mail before this is skipped", identity)
    return {"reset": True, "origin": now_ts, "cursor": now_ts, "boot": identity}


def plant_poll_cursor(
    session: Session, when: datetime | None = None, interval_s: int | None = None
) -> int:
    """Back-compat wrapper: plant the cursor at `when` (not a clock slot)."""
    del interval_s
    return int(ensure_poll_origin(session, when=when)["cursor"])


def load_poll_origin(session: Session) -> int:
    return _int_state(session, STATE_ORIGIN, 0)


def set_poll_progress(session: Session, **fields) -> None:
    set_state(session, STATE_POLL, json.dumps(fields))


def load_poll_progress(session: Session) -> dict:
    raw = get_state(session, STATE_POLL)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_extract_payload(raw: str) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        rows = data.get("candidates") or []
        return [row for row in rows if isinstance(row, dict)]
    return []


def _extract_blob(email: ParsedEmail, candidates: list[JobCandidate]) -> tuple[str, str]:
    payload = {
        "candidates": [asdict(item) for item in candidates],
        "link_count": len(email.links),
    }
    return email.body(BODY_STORE_CHARS), json.dumps(payload, ensure_ascii=False)


def clear_inbox(session: Session, *, from_now: bool = True) -> dict[str, int]:
    """Delete everything this app stored. Gmail itself is not touched.

    `from_now` plants the poll cursor at this moment so the next run only
    picks up mail that arrives after the reset.
    """
    counts = {
        "events": 0,
        "applications": 0,
        "outreach": 0,
        "jobs": 0,
        "messages": 0,
    }
    for key, model in (
        ("events", ApplicationEvent),
        ("applications", Application),
        ("outreach", Outreach),
        ("jobs", Job),
        ("messages", Message),
    ):
        rows = session.exec(select(model)).all()
        counts[key] = len(rows)
        for row in rows:
            session.delete(row)
        session.flush()
    if from_now:
        set_state(session, STATE_CURSOR, str(int(datetime.now(timezone.utc).timestamp())))
    session.commit()
    return counts


def _uniq(rows) -> list:
    seen: set[int | str] = set()
    out = []
    for row in rows:
        key = getattr(row, "id", None)
        if key is None or key in seen:
            if key is None:
                out.append(row)
            continue
        seen.add(key)
        out.append(row)
    return out


def oldest_stored_at(session: Session) -> datetime | None:
    """Earliest timestamp among cached mail, jobs, scrapes, issues, and extract runs."""
    stamps: list[datetime] = []
    for value in (
        session.exec(select(func.min(Message.received_at))).one(),
        session.exec(select(func.min(Job.received_at))).one(),
        session.exec(select(func.min(Outreach.received_at))).one(),
        session.exec(select(func.min(Issue.occurred_at))).one(),
        session.exec(select(func.min(PollRun.started_at))).one(),
        session.exec(select(func.min(ApplicationEvent.occurred_at))).one(),
    ):
        if not value:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        stamps.append(value)
    return min(stamps) if stamps else None


def resolve_cache_window(
    session: Session,
    *,
    scope: str = "range",
    start_at: str = "",
    end_at: str = "",
    hours: int = 0,
) -> tuple[datetime, datetime, str]:
    """Build a clear-cache window in UTC from Eastern date/time fields.

    `scope=old` starts at the oldest stored row (all remaining old data).
    `hours` from start overrides the end time when > 0.
    A typed end time is inclusive through that minute.
    """
    now = datetime.now(timezone.utc)
    scope = (scope or "range").strip().lower()
    hours = max(0, int(hours or 0))
    typed_end = parse_et_datetime(end_at)

    if scope == "old":
        start = oldest_stored_at(session) or now
        if hours:
            end = start + timedelta(hours=hours)
        elif typed_end:
            end = typed_end + timedelta(minutes=1)
        else:
            end = now
        if end <= start:
            end = start + timedelta(minutes=1)
        return start, end, f"all remaining old through {fmt_et(end)}"

    start = parse_et_datetime(start_at)
    if start is None:
        local = as_et(now) or now.astimezone(EASTERN)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    if hours:
        end = start + timedelta(hours=hours)
    elif typed_end:
        end = typed_end + timedelta(minutes=1)
    else:
        end = now
    if end <= start:
        start, end = end, start
        if end <= start:
            end = start + timedelta(minutes=1)
    return start, end, f"{fmt_et(start)} → {fmt_et(end)}"


def count_purge_window(session: Session, start: datetime, end: datetime) -> dict[str, int]:
    """How many stored rows a window delete would remove. Gmail is not touched."""
    return _purge_window(session, start, end, apply=False)


def purge_window(session: Session, start: datetime, end: datetime) -> dict[str, int]:
    """Delete stored mail, matches, scrapes, issues, and extract logs in [start, end).

    Gmail itself is not touched. Open applications stay; their job link is cleared
    if that posting is removed.
    """
    return _purge_window(session, start, end, apply=True)


def _purge_window(session: Session, start: datetime, end: datetime, *, apply: bool) -> dict[str, int]:
    messages = list(
        session.exec(select(Message).where(Message.received_at >= start, Message.received_at < end)).all()
    )
    mail_ids = [row.id for row in messages]
    jobs = list(session.exec(select(Job).where(Job.received_at >= start, Job.received_at < end)).all())
    if mail_ids:
        jobs.extend(session.exec(select(Job).where(col(Job.message_id).in_(mail_ids))).all())
    jobs = _uniq(jobs)
    job_ids = [row.id for row in jobs if row.id is not None]

    outreach = list(
        session.exec(select(Outreach).where(Outreach.received_at >= start, Outreach.received_at < end)).all()
    )
    if mail_ids:
        outreach.extend(session.exec(select(Outreach).where(col(Outreach.message_id).in_(mail_ids))).all())
    outreach = _uniq(outreach)

    events = list(
        session.exec(
            select(ApplicationEvent).where(
                ApplicationEvent.occurred_at >= start, ApplicationEvent.occurred_at < end
            )
        ).all()
    )
    if mail_ids:
        events.extend(
            session.exec(select(ApplicationEvent).where(col(ApplicationEvent.message_id).in_(mail_ids))).all()
        )
    events = _uniq(events)

    issues = list(
        session.exec(select(Issue).where(Issue.occurred_at >= start, Issue.occurred_at < end)).all()
    )
    runs = list(
        session.exec(select(PollRun).where(PollRun.started_at >= start, PollRun.started_at < end)).all()
    )

    counts = {
        "messages": len(messages),
        "jobs": len(jobs),
        "outreach": len(outreach),
        "events": len(events),
        "issues": len(issues),
        "runs": len(runs),
    }
    if not apply:
        return counts

    if job_ids:
        for app in session.exec(select(Application).where(col(Application.job_id).in_(job_ids))).all():
            app.job_id = None
            session.add(app)
        session.flush()

    for row in events + outreach + jobs + messages + issues + runs:
        session.delete(row)
    session.commit()
    return counts


def start_gmail_watch() -> dict:
    """Tell Gmail to ping our Pub/Sub topic when the inbox changes."""
    settings = get_settings()
    topic = settings.gmail_pubsub_topic.strip()
    if not topic:
        raise RuntimeError("GMAIL_PUBSUB_TOPIC is not set")
    result = GmailClient(settings).watch_inbox(topic)
    with session_scope() as session:
        if result.get("expiration"):
            set_state(session, STATE_WATCH, str(result["expiration"]))
        if result.get("historyId"):
            set_state(session, "gmail_history_id", str(result["historyId"]))
        session.commit()
    return result


def maybe_renew_watch() -> None:
    settings = get_settings()
    if not settings.gmail_pubsub_topic.strip():
        return
    with session_scope() as session:
        raw = get_state(session, STATE_WATCH)
    if not raw:
        return
    try:
        expires_ms = int(raw)
    except ValueError:
        expires_ms = 0
    # Watch dies after ~7 days. Renew when fewer than 2 days remain.
    if expires_ms < (time.time() * 1000) + 2 * 86400 * 1000:
        start_gmail_watch()


def watch_expiration_ms(session: Session) -> int | None:
    raw = get_state(session, STATE_WATCH)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def remember_run(session: Session, stats: RunStats) -> None:
    payload = {
        "started_at": stats.started_at,
        "duration_s": stats.duration_s,
        "fetched": stats.fetched,
        "processed": stats.processed,
        "skipped": stats.skipped,
        "jobs_matched": stats.jobs_matched,
        "errors": stats.errors[:20],
        "emails": stats.emails[:80],
        "window_start": stats.window_start,
        "window_end": stats.window_end,
        "trigger": stats.trigger,
    }
    set_state(session, STATE_LAST_RUN, json.dumps(payload))
    started = datetime.now(timezone.utc)
    try:
        started = datetime.fromisoformat(stats.started_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    session.add(
        PollRun(
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            trigger=stats.trigger or "poll",
            status="error" if stats.errors else "ok",
            window_start=stats.window_start,
            window_end=stats.window_end,
            fetched=stats.fetched,
            processed=stats.processed,
            skipped=stats.skipped,
            jobs_found=stats.jobs_found,
            jobs_matched=stats.jobs_matched,
            error_count=len(stats.errors),
            note="\n".join(stats.errors[:8]),
        )
    )


def load_last_run(session: Session) -> dict:
    raw = get_state(session, STATE_LAST_RUN)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def load_poll_runs(session: Session, limit: int = 40) -> list[PollRun]:
    return list(session.exec(select(PollRun).order_by(col(PollRun.started_at).desc()).limit(limit)).all())


def _pointer_job(email: ParsedEmail, candidate: JobCandidate, original: Job) -> Job:
    """Same posting already stored from another email: keep a row so this digest still lists it."""
    def clip(value: str | None, n: int) -> str:
        return (value or "")[:n]

    return Job(
        message_id=email.id,
        url=original.url or candidate.url,
        url_key=f"{candidate.url_key}@{email.id}",
        title=clip(original.title or candidate.title, 200),
        company=clip(original.company or candidate.company, 150),
        location=clip(original.location or candidate.location, 150),
        source=original.source or candidate.source,
        source_type=original.source_type or "",
        posting_id=clip(original.posting_id, 120),
        posted_at=clip(original.posted_at, 32),
        state=clip(original.state, 20),
        employment_type=clip(original.employment_type, 40),
        salary=clip(original.salary, 80),
        visa_sponsorship=clip(original.visa_sponsorship, 80),
        experience_required=clip(original.experience_required, 80),
        required_skills=clip(original.required_skills, 400),
        description=clip(candidate.context or original.description, 20000),
        score=original.score or 0.0,
        title_score=original.title_score or 0.0,
        skill_score=original.skill_score or 0.0,
        resume_score=original.resume_score or 0.0,
        matched_skills=clip(original.matched_skills, 500),
        missing_skills=clip(original.missing_skills, 500),
        verdict=clip(original.verdict, 300),
        scraped=bool(original.scraped),
        scrape_status=original.scrape_status or "",
        extraction=original.extraction or "email",
        matched=bool(original.matched),
        status=original.status if original.status in ("new", "ignored") else "ignored",
        dupe_key=original.dupe_key or "",
        duplicate_of=original.duplicate_of or original.id,
        received_at=email.received_at,
    )


def _jobs_to_fetch(session: Session, candidates: list[JobCandidate], profile) -> list[JobCandidate]:
    wanted: list[JobCandidate] = []
    for candidate in candidates:
        if exists(session, Job, url_key=candidate.url_key):
            continue
        if not title_worth_scraping(profile, candidate.title):
            continue
        wanted.append(candidate)
    return wanted


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
        original_row = session.exec(select(Job).where(Job.url_key == candidate.url_key)).first()
        if original_row is not None:
            if original_row.message_id == email.id:
                continue
            pointer_key = f"{candidate.url_key}@{email.id}"
            if exists(session, Job, url_key=pointer_key):
                continue
            job = _pointer_job(email, candidate, original_row)
            session.add(job)
            stored.append(job)
            continue

        page = scraped.get(candidate.url_key, ScrapedJob(status="skipped"))
        worth = title_worth_scraping(profile, candidate.title or page.title)

        if not worth:
            page = ScrapedJob(status="skipped", extraction="email")
        elif not page.ok:
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
        if not page.ok:
            title = candidate.title or page.title
            company = candidate.company or page.company
            location = candidate.location or page.location
            description = candidate.context
        if SHELL_TITLE.search(title or "") and candidate.title:
            title = candidate.title
            description = candidate.context or description
            page.ok = False
            page.status = "empty"
        if candidate.company and re.match(r"^haystack$", company or "", re.I):
            company = candidate.company
        extraction = page.extraction or ("email" if candidate.context else "")
        extra_skills = [s.name for s in profile.skills]
        fields = enrich_fields(
            source=candidate.source,
            url=candidate.url,
            url_key=candidate.url_key,
            location=location,
            description=description,
            extra_skills=extra_skills,
        )

        result = match_job(profile, title, description, location, company)
        result = promote_empty_scrape_match(result, settings.min_job_score, page.ok)
        if not worth and not result.rejected:
            result.verdict = "not related — skipped job page"
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
            source_type=page.source_type or fields.source_type,
            posting_id=(page.posting_id or fields.posting_id)[:120],
            posted_at=(page.posted_at or fields.posted_at)[:32],
            state=(page.state or fields.state)[:20],
            employment_type=(page.employment_type or fields.employment_type)[:40],
            salary=(page.salary or fields.salary)[:80],
            visa_sponsorship=normalize_visa(
                page.visa_sponsorship or fields.visa_sponsorship, description
            )[:80],
            experience_required=(page.experience_required or fields.experience_required)[:80],
            required_skills=(page.required_skills or fields.required_skills)[:400],
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
    blob = f"{email.subject}\n{email.body(4000)}"
    item = Outreach(
        message_id=email.id,
        kind=result.category,
        person=result.person or email.sender_name,
        person_email=email.sender_email,
        person_phone=result.phone or phone_from_text(blob),
        company=result.company,
        role=result.role,
        location=result.location,
        state=result.state,
        employment_type=result.employment_type,
        experience_required=result.experience_required,
        scheduling_url=result.scheduling_url
        or scheduling_url_from_links([link.url for link in email.links] + re.findall(r"https?://[^\s<>\"')]+", blob)),
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
    candidates = extract_postings(email, llm, limit=settings.max_jobs_per_email)
    result = classify_email(email, get_profile(), llm, job_count=len(candidates))

    # The message row has to land before anything referencing it: the dedupe lookups below
    # autoflush pending jobs mid-loop, and Postgres enforces the foreign key SQLite ignored.
    body_text, extract_json = _extract_blob(email, candidates)
    record = Message(
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
        email_type=result.email_type,
        body_text=body_text,
        extract_json=extract_json,
    )
    session.add(record)
    session.flush()

    jobs: list[Job] = []
    outreach: Outreach | None = None
    application: Application | None = None
    status_changed = False

    if _should_store_jobs(result, candidates):
        scraped = (
            fetch_all(
                _jobs_to_fetch(session, candidates, get_profile()),
                timeout=settings.scrape_timeout,
            )
            if settings.scrape_job_pages
            else {}
        )
        _note_scrape_problems(scraped, email.id)
        jobs = _store_jobs(session, email, candidates, scraped, llm)
    if result.is_tracked:
        # Acknowledgements and rejections belong to the application timeline only; the
        # Follow-ups page is for mail that still wants something from you.
        if result.is_follow_up:
            outreach = _store_outreach(session, email, result)
        tracked = record_email(session, email, result)
        if tracked is not None:
            application, status_changed = tracked

    record.jobs_matched = sum(1 for job in jobs if job.matched)
    session.add(record)
    return EmailResult(
        classification=result,
        jobs=jobs,
        outreach=outreach,
        jobs_found=len(candidates),
        application=application,
        status_changed=status_changed,
    )


def _should_store_jobs(result: Classification, candidates: list[JobCandidate]) -> bool:
    """Job-alert digests always explode into rows. Receipts and leftover mail that
    still carry posting links (Monster 'similar jobs', Adzuna leftovers) do too.
    """
    if not candidates:
        return False
    if result.category == JOB_ALERT:
        return True
    if result.category in (APPLICATION_UPDATE, OTHER) and len(candidates) >= 1:
        sources = {c.source for c in candidates if c.source}
        return bool(sources)
    return False


def _note_scrape_problems(scraped: dict, message_id: str) -> None:
    bad = notable_scrape_failures(scraped)
    if not bad:
        return
    statuses = sorted({page.status for page in bad})
    lines = []
    for page in bad[:8]:
        url = getattr(page, "final_url", "") or getattr(page, "url", "") or ""
        lines.append(f"{page.status}: {url}".strip())
    record_issue(
        "scrape",
        f"Job page fetch {', '.join(statuses)} ({len(bad)} link(s))",
        "\n".join(lines),
        severity="warn" if "error" not in statuses else "error",
        message_id=message_id,
    )


def reclassify_email(session: Session, email: ParsedEmail, llm: LLM) -> EmailResult:
    """Re-triage mail we already stored. Does not scrape job links again."""
    result = classify_email(email, get_profile(), llm, job_count=0)
    record = session.get(Message, email.id)
    if record is not None:
        record.category = result.category
        record.confidence = result.confidence
        record.reason = result.reason[:200]
        record.summary = result.summary[:1000]
        record.email_type = result.email_type
        session.add(record)

    outreach = session.exec(select(Outreach).where(Outreach.message_id == email.id)).first()
    if result.is_follow_up:
        if outreach is None:
            outreach = _store_outreach(session, email, result)
        else:
            outreach.kind = result.category
            outreach.action_required = result.action_required or outreach.action_required
            outreach.summary = (result.summary or outreach.summary)[:300]
            outreach.urgency = result.urgency if result.urgency in ("high", "normal", "low") else outreach.urgency
            session.add(outreach)
    elif outreach is not None:
        # Receipts used to be stored as follow-ups. Demote so the page hides them.
        outreach.kind = result.category
        session.add(outreach)
        outreach = None

    application = None
    status_changed = False
    if result.is_tracked:
        tracked = record_email(session, email, result)
        if tracked is not None:
            application, status_changed = tracked

    return EmailResult(
        classification=result,
        outreach=outreach,
        application=application,
        status_changed=status_changed,
    )


def reextract_email(session: Session, email: ParsedEmail, llm: LLM) -> EmailResult:
    """Re-run link extract + scrape for mail we already stored."""
    settings = get_settings()
    candidates = extract_postings(email, llm, limit=settings.max_jobs_per_email)
    result = classify_email(email, get_profile(), llm, job_count=len(candidates))
    record = session.get(Message, email.id)
    if record is not None:
        record.category = result.category
        record.confidence = result.confidence
        record.reason = result.reason[:200]
        record.summary = result.summary[:1000]
        record.email_type = result.email_type
        record.jobs_found = len(candidates)
        record.body_text, record.extract_json = _extract_blob(email, candidates)
        session.add(record)

    for job in session.exec(select(Job).where(Job.message_id == email.id)).all():
        for application in session.exec(select(Application).where(Application.job_id == job.id)).all():
            application.job_id = None
            session.add(application)
        session.delete(job)
    session.flush()

    jobs: list[Job] = []
    if _should_store_jobs(result, candidates):
        scraped = (
            fetch_all(
                _jobs_to_fetch(session, candidates, get_profile()),
                timeout=settings.scrape_timeout,
            )
            if settings.scrape_job_pages
            else {}
        )
        _note_scrape_problems(scraped, email.id)
        jobs = _store_jobs(session, email, candidates, scraped, llm)
    if record is not None:
        record.jobs_matched = sum(1 for job in jobs if job.matched)
        session.add(record)
    return EmailResult(
        classification=result,
        jobs=jobs,
        jobs_found=len(candidates),
    )


def email_from_message(record: Message) -> ParsedEmail:
    """Rebuild enough of the original email to re-extract without calling Gmail."""
    payload = parse_extract_payload(record.extract_json)
    links: list[Link] = []
    html_bits: list[str] = []
    for item in payload:
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "job")
        if not url:
            continue
        links.append(Link(url=url, text=title))
        extra = " ".join(str(item.get(k) or "") for k in ("company", "location") if item.get(k))
        html_bits.append(f'<a href="{url}">{title}</a> {extra}'.strip())
    return ParsedEmail(
        id=record.id,
        thread_id=record.thread_id or "",
        sender_name=record.sender or "",
        sender_email=record.sender_email or "",
        subject=record.subject or "",
        snippet=record.snippet or "",
        received_at=record.received_at,
        text=record.body_text or record.snippet or "",
        html="\n".join(html_bits),
        links=links,
    )


def apply_page_to_job(
    session: Session,
    job: Job,
    page: ScrapedJob,
    candidate: JobCandidate,
    llm: LLM | None = None,
) -> Job:
    settings = get_settings()
    profile = get_profile()
    if not page.ok:
        fallback_text = "\n".join(
            filter(
                None,
                [candidate.title, candidate.company, candidate.location, candidate.context, page.description, job.description],
            )
        )
        rescued = llm_extract(fallback_text, llm, candidate)
        if rescued is not None:
            page = rescued
    title = page.title or candidate.title or job.title
    company = page.company or candidate.company or job.company
    location = page.location or candidate.location or job.location
    description = page.description or candidate.context or job.description
    if not page.ok:
        title = candidate.title or job.title or page.title
        company = candidate.company or job.company or page.company
        location = candidate.location or job.location or page.location
        description = candidate.context or job.description
    extra_skills = [s.name for s in profile.skills]
    fields = enrich_fields(
        source=candidate.source or job.source,
        url=candidate.url or job.url,
        url_key=candidate.url_key or job.url_key,
        location=location,
        description=description,
        extra_skills=extra_skills,
    )
    result = match_job(profile, title, description, location, company)
    result = promote_empty_scrape_match(result, settings.min_job_score, page.ok)
    is_match = not result.rejected and result.score >= settings.min_job_score
    job.title = (title or "")[:200]
    job.company = (company or "")[:150]
    job.location = (location or "")[:150]
    job.description = (description or "")[:20000]
    job.source_type = page.source_type or fields.source_type or job.source_type
    job.posting_id = (page.posting_id or fields.posting_id or job.posting_id)[:120]
    job.posted_at = (page.posted_at or fields.posted_at or job.posted_at)[:32]
    job.state = (page.state or fields.state or job.state)[:20]
    job.employment_type = (page.employment_type or fields.employment_type or job.employment_type)[:40]
    job.salary = (page.salary or fields.salary or job.salary)[:80]
    job.visa_sponsorship = normalize_visa(
        page.visa_sponsorship or fields.visa_sponsorship, description
    )[:80]
    job.experience_required = (page.experience_required or fields.experience_required or job.experience_required)[:80]
    job.required_skills = (page.required_skills or fields.required_skills or job.required_skills)[:400]
    job.score = result.score
    job.title_score = result.title_score
    job.skill_score = result.skill_score
    job.resume_score = result.resume_score
    job.matched_skills = ", ".join(result.matched_skills)[:500]
    job.missing_skills = ", ".join(result.missing_skills)[:500]
    job.verdict = result.verdict[:300]
    job.scraped = page.ok
    job.scrape_status = page.status
    job.extraction = page.extraction or job.extraction
    job.matched = is_match
    if is_match and job.status == "ignored":
        job.status = "new"
    elif not is_match and job.status == "new":
        job.status = "ignored"
    session.add(job)
    return job


def rescrape_job(session: Session, job_id: int, llm: LLM | None = None) -> Job | None:
    """Fetch one stored posting again and rematch it against the profile."""
    job = session.get(Job, job_id)
    if job is None:
        return None
    if job.duplicate_of:
        original = session.get(Job, job.duplicate_of)
        if original is not None:
            job = original
    if not (job.url or "").strip():
        return job
    settings = get_settings()
    llm = llm or LLM(settings)
    key = (job.url_key or "").split("@", 1)[0]
    candidate = JobCandidate(
        url=job.url,
        url_key=key,
        title=job.title,
        company=job.company,
        location=job.location,
        source=job.source,
        context=(job.description or "")[:600],
    )
    page = fetch_job(candidate, timeout=settings.scrape_timeout)
    return apply_page_to_job(session, job, page, candidate, llm)


def run_once(
    max_messages: int | None = None,
    since_days: int | None = None,
    query: str = "",
    reclassify: bool = False,
    reextract: bool = False,
    after_epoch: int | None = None,
    before_epoch: int | None = None,
    trigger: str = "poll",
) -> RunStats:
    started = time.time()
    stats = RunStats(
        started_at=datetime.now(timezone.utc).isoformat(),
        window_start=int(after_epoch or 0),
        window_end=int(before_epoch or 0),
        trigger=trigger,
    )
    settings = get_settings()
    init_db()

    llm = LLM(settings)
    notifier = Notifier(settings)
    try:
        gmail = GmailClient(settings)
    except Exception as exc:
        record_issue("gmail", "Gmail client failed to start", str(exc))
        stats.errors.append(str(exc))
        stats.duration_s = round(time.time() - started, 2)
        with session_scope() as session:
            remember_run(session, stats)
            session.commit()
        return stats
    new_jobs: list[Job] = []
    pending_notifications: list[Outreach] = []
    latest_epoch = 0

    with session_scope() as session:
        search = build_query(
            session,
            since_days=since_days,
            override=query,
            after_epoch=after_epoch,
            before_epoch=before_epoch,
        )
        limit = max_messages or settings.gmail_max_results
        try:
            message_ids = gmail.list_message_ids(search, limit)
        except Exception as exc:
            log.exception("gmail list failed")
            record_issue("gmail", "Gmail list failed", str(exc))
            stats.errors.append(str(exc))
            set_poll_progress(
                session,
                status="error",
                index=0,
                total=0,
                subject="",
                window_start=after_epoch or 0,
                window_end=before_epoch or 0,
            )
            stats.duration_s = round(time.time() - started, 2)
            remember_run(session, stats)
            session.commit()
            return stats
        stats.fetched = len(message_ids)
        log.info("query=%r -> %d message(s)", search, len(message_ids))
        set_poll_progress(
            session,
            status="running",
            index=0,
            total=len(message_ids),
            subject="",
            window_start=after_epoch or 0,
            window_end=before_epoch or 0,
        )
        session.commit()

        for step, message_id in enumerate(message_ids, start=1):
            already = session.get(Message, message_id)
            if already and not reclassify and not reextract:
                stats.skipped += 1
                continue
            try:
                email = parse_message(gmail.get_message(message_id))
                set_poll_progress(
                    session,
                    status="running",
                    index=step,
                    total=len(message_ids),
                    subject=(email.subject or "")[:120],
                    window_start=after_epoch or 0,
                    window_end=before_epoch or 0,
                )
                log.info("extract %d/%d %s", step, len(message_ids), message_id)
                if already and reextract:
                    outcome = reextract_email(session, email, llm)
                elif already:
                    outcome = reclassify_email(session, email, llm)
                else:
                    outcome = process_email(session, email, llm)
                session.commit()

                category = outcome.classification.category
                stats.processed += 1
                stats.jobs_found += outcome.jobs_found
                stats.jobs_stored += len(outcome.jobs)
                stats.jobs_matched += len(outcome.matched_jobs)
                stats.categories[category] = stats.categories.get(category, 0) + 1
                new_jobs.extend(outcome.matched_jobs)
                stats.emails.append(
                    {
                        "id": message_id,
                        "subject": (email.subject or "")[:140],
                        "sender": (email.sender_name or email.sender_email or "")[:80],
                        "category": category,
                        "jobs_found": outcome.jobs_found,
                        "jobs_matched": len(outcome.matched_jobs),
                        "error": "",
                    }
                )
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
                record_issue("gmail", f"Failed on message {message_id}", str(exc), message_id=message_id)
                stats.emails.append(
                    {"id": message_id, "subject": "", "sender": "", "category": "", "jobs_found": 0, "jobs_matched": 0, "error": str(exc)}
                )

        for item in pending_notifications:
            if notifier.outreach(item):
                item.notified = True
                session.add(item)
                stats.notified += 1

        if new_jobs and settings.notify_on_jobs and notifier.jobs(new_jobs):
            stats.notified += 1

        if before_epoch:
            set_state(session, STATE_CURSOR, str(int(before_epoch)))
        elif latest_epoch:
            set_state(session, STATE_CURSOR, str(latest_epoch))
        set_poll_progress(
            session,
            status="idle",
            index=len(message_ids),
            total=len(message_ids),
            subject="",
            window_start=after_epoch or 0,
            window_end=before_epoch or 0,
        )
        stats.duration_s = round(time.time() - started, 2)
        remember_run(session, stats)
        session.commit()

    try:
        maybe_renew_watch()
    except Exception as exc:
        log.exception("gmail watch renew failed")
        record_issue("gmail", "Gmail watch renew failed", str(exc), severity="warn")
    log.info("run complete: %s", {k: v for k, v in stats.as_dict().items() if k != "emails"})
    return stats


def poll_since_cursor(max_messages: int | None = None, trigger: str = "api") -> RunStats:
    """Extract mail from the stored cursor up to now. Used by Cloud Scheduler."""
    init_db()
    with session_scope() as session:
        info = ensure_poll_origin(session)
        session.commit()
        cursor = int(info["cursor"])
    end = int(time.time())
    if end <= cursor:
        stats = RunStats(
            started_at=datetime.now(timezone.utc).isoformat(),
            window_start=cursor,
            window_end=end,
            trigger=trigger,
        )
        with session_scope() as session:
            remember_run(session, stats)
            session.commit()
        return stats
    return run_once(
        max_messages=max_messages,
        after_epoch=cursor,
        before_epoch=end,
        trigger=trigger,
    )


def interval_poll_loop(max_messages: int | None = None, interval_s: int | None = None) -> None:
    """Wait one interval from boot, extract that window, repeat.

    Boot at 6:03 → skip older mail → first run at 6:08 covering 6:03–6:08 when the interval is 5 minutes.
    """
    interval = max(60, interval_s or get_settings().poll_interval_seconds)
    init_db()
    with session_scope() as session:
        info = ensure_poll_origin(session)
        session.commit()
        origin = int(info["origin"])
    tick = 1
    log.info(
        "auto-sync from %s; first window ends %s (older mail skipped)",
        fmt_et(datetime.fromtimestamp(origin, tz=timezone.utc), "%I:%M %p ET"),
        fmt_et(datetime.fromtimestamp(origin + interval, tz=timezone.utc), "%I:%M %p ET"),
    )
    while True:
        end = origin + tick * interval
        delay = end - time.time()
        if delay > 0:
            log.info(
                "auto-sync sleeping %.0fs until %s",
                delay,
                fmt_et(datetime.fromtimestamp(end, tz=timezone.utc), "%I:%M %p ET"),
            )
            time.sleep(delay)
        start, stop = tick_window(end, origin, interval)
        try:
            stats = run_once(
                max_messages=max_messages,
                after_epoch=start,
                before_epoch=stop,
                trigger="loop",
            )
            log.info("auto-sync: %s", {k: v for k, v in stats.as_dict().items() if k != "emails"})
        except Exception as exc:
            log.exception("auto-sync failed; will retry next window")
            record_issue("poll", "Auto-sync window failed", str(exc))
        tick += 1


def aligned_poll_loop(max_messages: int | None = None, interval_s: int | None = None) -> None:
    """Old name — interval is now counted from boot, not :00/:15/:30/:45."""
    interval_poll_loop(max_messages=max_messages, interval_s=interval_s)
