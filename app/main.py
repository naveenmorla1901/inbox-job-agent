from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, col, func, select

from .applications import (
    CLOSED_STATUSES,
    STATUS_RANK,
    create_from_job,
    fill_blank_roles,
    stale_applications,
)
from .classify import FOLLOW_UP_KINDS, JOB_ALERT, NOREPLY_RE
from .config import ROOT, get_profile, get_settings
from .db import get_engine, init_db
from .gmail_client import gmail_token_status, host_setup, parse_gmail_push
from .issues import issue_counts, latest_by_source, recent_issues
from .extract_miss import list_misses, miss_count, upsert_extract_miss
from .models import Application, ApplicationEvent, ExtractMiss, Job, Message, Outreach
from .pipeline import (
    clear_inbox,
    count_purge_window,
    demote_noise_followups,
    email_for_reextract,
    ensure_poll_origin,
    load_last_run,
    load_poll_origin,
    load_poll_progress,
    load_poll_runs,
    maybe_renew_watch,
    oldest_stored_at,
    parse_extract_payload,
    poll_since_cursor,
    purge_window,
    resolve_cache_window,
    reextract_email,
    rescrape_job,
    run_once,
    start_gmail_watch,
)
from .probe import probe_apis
from .reporting import (
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    application_funnel,
    build_breakdown,
    daily_brief,
    window_bounds,
)
from .schedule import next_tick_epoch
from .timefmt import et_datetime_value, et_day_label, fmt_et, group_by_et_day

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs full request URLs at INFO, which would print API keys carried in query strings.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger(__name__)
_poller_started = False


def _gmail_configured() -> bool:
    settings = get_settings()
    return bool(settings.gmail_token_json.strip()) or Path(settings.gmail_token_file).exists()


def start_auto_poller() -> bool:
    """Background thread: poll windows counted from this process start.

    Cloud Run sleeps between requests, so production uses Cloud Scheduler → POST /api/run
    instead of this thread. Pytest and missing Gmail config leave this off.
    """
    global _poller_started
    settings = get_settings()
    if _poller_started or not settings.auto_poll:
        return False
    if os.environ.get("PYTEST_CURRENT_TEST") or not _gmail_configured():
        return False
    if os.environ.get("K_SERVICE"):
        log.info("Cloud Run: in-process poller off; Cloud Scheduler hits /api/run")
        return False
    _poller_started = True

    def loop() -> None:
        from .pipeline import interval_poll_loop

        interval_poll_loop()

    threading.Thread(target=loop, name="job-auto-poller", daemon=True).start()
    interval = max(60, settings.poll_interval_seconds)
    log.info("auto-sync started: every %d min from boot", interval // 60)
    return True


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    try:
        with Session(get_engine()) as session:
            ensure_poll_origin(session)
            session.commit()
    except Exception:
        log.exception("could not plant poll origin")
    start_auto_poller()
    yield


def _template_host_setup(_request: Request) -> dict:
    return {"host_setup": host_setup()}


def _template_nav(_request: Request) -> dict:
    """Header status shown on every page: pending follow-ups + last/next sync."""
    settings = get_settings()
    last_run: dict = {}
    progress: dict = {}
    pending = 0
    origin = 0
    open_errors = 0
    open_flags = 0
    open_misses = 0
    interval = max(60, settings.poll_interval_seconds)
    now = datetime.now(timezone.utc)
    try:
        with Session(get_engine()) as session:
            last_run = load_last_run(session)
            progress = load_poll_progress(session)
            pending = pending_outreach_count(session)
            origin = load_poll_origin(session)
            open_errors = issue_counts(session, hours=24).get("error", 0)
            open_flags = session.exec(
                select(func.count())
                .select_from(Message)
                .where(Message.flagged_category != "")
            ).one()
            open_misses = miss_count(session)
    except Exception:
        pass
    if origin:
        next_end = next_tick_epoch(origin, interval, now)
    else:
        next_end = int(now.timestamp()) + interval
    next_at = datetime.fromtimestamp(next_end, tz=timezone.utc)
    extracting = progress.get("status") == "running"
    return {
        "nav_pending": pending,
        "nav_errors": open_errors,
        "nav_flags": int(open_flags or 0),
        "nav_misses": int(open_misses or 0),
        "nav_sync": {
            "auto": settings.auto_poll,
            "interval_min": max(1, interval // 60),
            "last_run": last_run,
            "next_at": next_at,
            "origin": origin,
            "progress": progress,
            "extracting": extracting,
            "cloud": bool(os.environ.get("K_SERVICE")),
        },
    }


app = FastAPI(title="Inbox Job Agent", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
templates = Jinja2Templates(
    directory=str(ROOT / "app" / "templates"),
    context_processors=[_template_host_setup, _template_nav],
)
templates.env.filters["et"] = fmt_et
templates.env.filters["ago"] = lambda dt: _humanize_ago(dt)
templates.env.filters["epoch_et"] = lambda ts, fmt="%I:%M %p ET": (
    fmt_et(datetime.fromtimestamp(int(ts), tz=timezone.utc), fmt)
    if ts not in (None, "", 0, "0")
    else ""
)


def _humanize_ago(value) -> str:
    """'3m ago' / 'just now' from an ISO string or datetime."""
    if not value:
        return "never"
    try:
        when = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return "recently"
    secs = (datetime.now(timezone.utc) - when).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"

PUBLIC_PATHS = {"/healthz", "/login", "/favicon.ico"}
COOKIE = "ija_key"


def db_session():
    with Session(get_engine()) as session:
        yield session


def auth_enabled() -> bool:
    token = get_settings().api_token
    return bool(token) and token != "change-me"


@app.middleware("http")
async def gate(request: Request, call_next):
    if auth_enabled() and request.url.path not in PUBLIC_PATHS:
        token = get_settings().api_token
        provided = (
            request.cookies.get(COOKIE)
            or request.headers.get("x-api-token")
            or request.query_params.get("key")
        )
        if provided != token:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return RedirectResponse("/login", status_code=302)
    return await call_next(request)


def pending_outreach_count(session: Session) -> int:
    return session.exec(
        select(func.count())
        .select_from(Outreach)
        .where(Outreach.handled == False, col(Outreach.kind).in_(FOLLOW_UP_KINDS))  # noqa: E712
    ).one()


def safe_next(value: str, fallback: str = "/") -> str:
    if value.startswith("/") and not value.startswith("//"):
        return value
    return fallback


def mail_bundle(
    session: Session,
    days: int,
    category: str = "",
    q: str = "",
    has: str = "",
) -> tuple[list[Message], dict[str, list[Job]], dict[str, Outreach]]:
    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    stmt = select(Message).where(Message.received_at >= since)
    if category:
        stmt = stmt.where(Message.category == category)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Message.subject).like(like)
            | func.lower(Message.sender).like(like)
            | func.lower(Message.sender_email).like(like)
            | func.lower(Message.summary).like(like)
        )
    if has == "jobs":
        stmt = stmt.where(Message.jobs_found > 0)
    elif has == "norows":
        # A job alert the extractor read as empty: the digest shape was missed.
        stmt = stmt.where(
            col(Message.category).in_([JOB_ALERT]), Message.jobs_found == 0
        )
    elif has == "unstored":
        # Parser found postings (raw extract) but they never became Job rows.
        stored = select(Job.message_id)
        stmt = stmt.where(Message.jobs_found > 0, col(Message.id).not_in(stored))
    elif has == "followups":
        stmt = stmt.where(col(Message.category).in_(FOLLOW_UP_KINDS))
    elif has == "other":
        stmt = stmt.where(Message.category == "other")
    messages = session.exec(stmt.order_by(col(Message.received_at).desc()).limit(150)).all()
    ids = [message.id for message in messages]
    jobs_by_mail: dict[str, list[Job]] = {}
    outreach_by_mail: dict[str, Outreach] = {}
    if ids:
        jobs = session.exec(select(Job).where(col(Job.message_id).in_(ids))).all()
        for job in sorted(jobs, key=lambda row: (-(row.score or 0.0), row.id or 0)):
            jobs_by_mail.setdefault(job.message_id, []).append(job)
        for item in session.exec(select(Outreach).where(col(Outreach.message_id).in_(ids))).all():
            outreach_by_mail[item.message_id] = item
    return messages, jobs_by_mail, outreach_by_mail


def require_token(request: Request) -> None:
    if not auth_enabled():
        return
    token = get_settings().api_token
    provided = request.cookies.get(COOKIE) or request.headers.get("x-api-token")
    if provided != token:
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
def login(key: str = Form(...)):
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(COOKIE, key, httponly=True, max_age=60 * 60 * 24 * 90, samesite="lax")
    return response


@app.get("/", response_class=HTMLResponse)
def mail_page(
    request: Request,
    session: Session = Depends(db_session),
    days: int = 1,
    category: str = "",
    q: str = "",
    has: str = "",
    m: str = "",
    view: str = "analysis",
    checked: str = "",
    flash: str = "",
):
    messages, jobs_by_mail, outreach_by_mail = mail_bundle(
        session, days=days, category=category, q=q, has=has
    )
    matched_by_mail = {
        message_id: sum(1 for job in rows if job.matched)
        for message_id, rows in jobs_by_mail.items()
    }
    selected = next((row for row in messages if row.id == m), None)
    if selected is None and messages:
        selected = messages[0]
    flash = flash or ""
    if checked and not flash:
        last = load_last_run(session)
        flash = (
            f"Checked {last.get('fetched', 0)} email(s). "
            f"Analyzed {last.get('processed', 0)} new."
        )
    category_counts: dict[str, int] = {}
    for row in messages:
        category_counts[row.category] = category_counts.get(row.category, 0) + 1
    # Job alerts the extractor read as empty. Worth a Re-extract, not a silent skip.
    empty_alerts = sum(
        1 for row in messages if row.category == JOB_ALERT and not row.jobs_found
    )
    unstored_extracts = sum(
        1 for row in messages if row.jobs_found and not jobs_by_mail.get(row.id)
    )

    def mail_qs(**overrides) -> str:
        params = {
            "days": days,
            "category": category,
            "q": q,
            "has": has,
            "m": selected.id if selected else m,
            "view": view if view in ("analysis", "raw") else "analysis",
        }
        params.update(overrides)
        clean = {key: value for key, value in params.items() if value not in (None, "")}
        return urlencode(clean)

    selected_jobs = jobs_by_mail.get(selected.id, []) if selected else []
    raw_extract = parse_extract_payload(selected.extract_json) if selected else []
    if selected and not raw_extract:
        raw_extract = [
            {
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "url": job.url,
                "url_key": job.url_key,
                "source": job.source,
                "context": (job.description or "")[:600],
            }
            for job in selected_jobs
        ]

    return templates.TemplateResponse(
        request,
        "mail.html",
        {
            "messages": messages,
            "day_groups": group_by_et_day(messages),
            "jobs_by_mail": jobs_by_mail,
            "matched_by_mail": matched_by_mail,
            "outreach_by_mail": outreach_by_mail,
            "selected": selected,
            "selected_jobs": selected_jobs,
            "selected_outreach": outreach_by_mail.get(selected.id) if selected else None,
            "raw_extract": raw_extract,
            "days": days,
            "category": category,
            "q": q,
            "has": has,
            "view": view if view in ("analysis", "raw") else "analysis",
            "flash": flash,
            "pending_outreach": pending_outreach_count(session),
            "category_labels": CATEGORY_LABELS,
            "category_order": CATEGORY_ORDER,
            "category_counts": category_counts,
            "empty_alerts": empty_alerts,
            "unstored_extracts": unstored_extracts,
            "mail_jobs": sum(len(rows) for rows in jobs_by_mail.values()),
            "mail_matches": sum(matched_by_mail.values()),
            "mail_qs": mail_qs,
            "extract_miss": session.get(ExtractMiss, selected.id) if selected else None,
        },
    )


@app.get("/matches", response_class=HTMLResponse)
def matches_page(
    request: Request,
    session: Session = Depends(db_session),
    status: str = "new",
    q: str = "",
    days: int = 7,
    min_score: float | None = None,
    show: str = "matched",
    duplicates: str = "hide",
    sort: str = "time",
):
    settings = get_settings()
    threshold = 0.0 if show == "all" else (settings.min_job_score if min_score is None else min_score)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    if sort not in ("time", "score", "score_asc"):
        sort = "time"

    stmt = select(Job).where(Job.score >= threshold, Job.received_at >= since)
    if duplicates == "hide":
        stmt = stmt.where(Job.duplicate_of == None)  # noqa: E711
    if show == "matched":
        stmt = stmt.where(Job.matched == True)  # noqa: E712
    if status and status != "all" and show != "all":
        stmt = stmt.where(Job.status == status)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Job.title).like(like)
            | func.lower(Job.company).like(like)
            | func.lower(Job.description).like(like)
        )
    if sort == "score":
        order = (col(Job.score).desc(), col(Job.received_at).desc())
    elif sort == "score_asc":
        order = (col(Job.score).asc(), col(Job.received_at).desc())
    else:
        order = (col(Job.received_at).desc(), col(Job.score).desc())
    jobs = session.exec(stmt.order_by(*order).limit(300)).all()
    mail_ids = {job.message_id for job in jobs}
    messages_by_id = {}
    if mail_ids:
        for message in session.exec(select(Message).where(col(Message.id).in_(list(mail_ids)))).all():
            messages_by_id[message.id] = message
    if sort == "time":
        day_groups = group_by_et_day(jobs)
    else:
        day_groups = [("", jobs)]

    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "day_groups": day_groups,
            "messages_by_id": messages_by_id,
            "status": status,
            "q": q,
            "days": days,
            "show": show,
            "sort": sort,
            "duplicates": duplicates,
            "duplicate_count": session.exec(
                select(func.count())
                .select_from(Job)
                .where(Job.duplicate_of != None, Job.received_at >= since)  # noqa: E711
            ).one(),
            "min_score": threshold,
            "pending_outreach": pending_outreach_count(session),
            "profile": get_profile(),
        },
    )


@app.get("/overview", response_class=HTMLResponse)
def overview_page(
    request: Request,
    session: Session = Depends(db_session),
    days: int = 1,
    since: str = "",
    until: str = "",
    flash: str = "",
    app_q: str = "",
):
    report = build_breakdown(session, days=days, since=since or None, until=until or None)
    start, end, _, _ = window_bounds(days=days, since=since or None, until=until or None)
    funnel = application_funnel(session, start, end, q=app_q)
    fill_blank_roles(session, funnel.rows)
    brief = daily_brief(session, start, end)
    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "report": report,
            "brief": brief,
            "days": days,
            "since": since,
            "until": until,
            "app_q": app_q,
            "funnel": funnel,
            "flash": flash,
            "pending_outreach": pending_outreach_count(session),
        },
    )


def _cache_hours(raw: str | int | None) -> int:
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _cache_query(
    scope: str,
    start_at: str,
    end_at: str,
    hours: int,
    **extra: str,
) -> str:
    q = {"scope": scope, "start_at": start_at, "end_at": end_at}
    if hours:
        q["hours"] = str(hours)
    q.update({key: value for key, value in extra.items() if value})
    return urlencode(q)


@app.get("/cache", response_class=HTMLResponse)
def cache_page(
    request: Request,
    session: Session = Depends(db_session),
    scope: str = "range",
    start_at: str = "",
    end_at: str = "",
    hours: str = "",
    flash: str = "",
):
    hour_count = _cache_hours(hours)
    start, end, label = resolve_cache_window(
        session, scope=scope, start_at=start_at, end_at=end_at, hours=hour_count
    )
    counts = count_purge_window(session, start, end)
    oldest = oldest_stored_at(session)
    return templates.TemplateResponse(
        request,
        "cache.html",
        {
            "scope": "old" if scope == "old" else "range",
            "start_at": start_at or et_datetime_value(start),
            "end_at": end_at or et_datetime_value(end),
            "hours": hour_count or "",
            "label": label,
            "start": start,
            "end": end,
            "oldest": oldest,
            "counts": counts,
            "total": sum(counts.values()),
            "flash": flash,
        },
    )


@app.post("/cache/clear")
def cache_clear(
    request: Request,
    session: Session = Depends(db_session),
    scope: str = Form("range"),
    start_at: str = Form(""),
    end_at: str = Form(""),
    hours: str = Form(""),
):
    require_token(request)
    hour_count = _cache_hours(hours)
    start, end, label = resolve_cache_window(
        session, scope=scope, start_at=start_at, end_at=end_at, hours=hour_count
    )
    counts = purge_window(session, start, end)
    bits = [f"{n} {name}" for name, n in counts.items() if n]
    flash = f"Cleared {', '.join(bits) or 'nothing'} for {label}. Gmail itself was not touched."
    return RedirectResponse(
        f"/cache?{_cache_query(scope, start_at, end_at, hour_count, flash=flash)}",
        status_code=303,
    )


@app.post("/overview/purge")
def overview_purge(
    request: Request,
    session: Session = Depends(db_session),
    days: int = Form(1),
    since: str = Form(""),
    until: str = Form(""),
):
    """Old Overview delete URL — same clear, then send you to Clear cache."""
    require_token(request)
    start, end, label, _ = window_bounds(days=days, since=since or None, until=until or None)
    counts = purge_window(session, start, end)
    bits = [f"{n} {name}" for name, n in counts.items() if n]
    flash = f"Cleared {', '.join(bits) or 'nothing'} for {label}. Gmail itself was not touched."
    return RedirectResponse(
        f"/cache?{urlencode({'scope': 'range', 'flash': flash})}",
        status_code=303,
    )


def _activity_context(session: Session, **extra) -> dict:
    settings = get_settings()
    origin = load_poll_origin(session)
    interval = max(60, settings.poll_interval_seconds)
    counts = issue_counts(session, hours=24)
    llm = None
    try:
        from .llm import LLM

        llm = LLM(settings)
    except Exception:
        pass
    return {
        "pending_outreach": pending_outreach_count(session),
        "last_run": extra.pop("last_run", None) or load_last_run(session),
        "poll_runs": extra.pop("poll_runs", None) or load_poll_runs(session, 40),
        "origin": origin,
        "interval": interval,
        "issue_counts": counts,
        "llm_enabled": bool(llm and llm.enabled),
        "llm_chain": llm.describe("extract") if llm else "none",
        "flash": extra.pop("flash", ""),
        "error": extra.pop("error", ""),
        **extra,
    }


@app.get("/activity", response_class=HTMLResponse)
def activity_page(
    request: Request,
    session: Session = Depends(db_session),
    fresh: str = "",
    checked: str = "",
):
    flash = ""
    if fresh:
        flash = "Saved jobs and mail history were cleared. Only new emails will be analyzed."
    elif checked:
        last = load_last_run(session)
        flash = (
            f"Checked {last.get('fetched', 0)} email(s). "
            f"Analyzed {last.get('processed', 0)} new. "
            f"Skipped {last.get('skipped', 0)} already seen."
        )
    return templates.TemplateResponse(
        request, "activity.html", _activity_context(session, flash=flash)
    )


def _issues_api_rows(session: Session) -> tuple[list[dict], bool, str]:
    """Live Gmail + LLM status for the Issues page, plus last recorded event."""
    latest = latest_by_source(session)
    gmail = gmail_token_status()
    apis: list[dict] = [
        {
            "name": "Gmail",
            "source": "gmail",
            "status": gmail["status"],
            "detail": gmail["detail"],
            "last": latest.get("gmail"),
        }
    ]
    llm = None
    llm_last = latest.get("llm")
    llm_last_attached = False
    try:
        from .llm import LLM

        llm = LLM(get_settings())
        for row in llm.health():
            if not row["configured"] and not row["in_chain"]:
                continue
            apis.append(
                {
                    "name": f"LLM · {row['name']}",
                    "source": "llm",
                    "status": row["status"],
                    "detail": row["detail"],
                    "last": None if llm_last_attached else llm_last,
                }
            )
            llm_last_attached = True
        listed_llm = any(row["name"].startswith("LLM") for row in apis)
        if llm.enabled and not listed_llm:
            apis.append(
                {
                    "name": "LLM",
                    "source": "llm",
                    "status": "error",
                    "detail": "LLM is on but no provider has an API key",
                    "last": llm_last,
                }
            )
        elif not llm.enabled and not listed_llm:
            apis.append(
                {
                    "name": "LLM",
                    "source": "llm",
                    "status": "idle",
                    "detail": "LLM_PROVIDER=none — classify/extract use rules only",
                    "last": llm_last,
                }
            )
    except Exception as exc:
        apis.append(
            {
                "name": "LLM",
                "source": "llm",
                "status": "error",
                "detail": str(exc)[:240],
                "last": latest.get("llm"),
            }
        )
    for source in ("scrape", "poll", "config"):
        last = latest.get(source)
        if last:
            apis.append(
                {
                    "name": source.title(),
                    "source": source,
                    "status": "error" if last.severity == "error" else "warn",
                    "detail": last.title,
                    "last": last,
                }
            )
    return apis, bool(llm and llm.enabled), (llm.describe("extract") if llm else "none")


@app.get("/issues", response_class=HTMLResponse)
def issues_page(
    request: Request,
    session: Session = Depends(db_session),
    source: str = "",
    hours: int = 168,
    flash: str = "",
):
    hours = max(1, min(int(hours or 168), 720))
    rows = recent_issues(session, hours=hours, source=source)
    counts = issue_counts(session, hours=24)
    week = issue_counts(session, hours=hours)
    apis, llm_enabled, llm_chain = _issues_api_rows(session)
    return templates.TemplateResponse(
        request,
        "issues.html",
        {
            "issues": rows,
            "source": source,
            "hours": hours,
            "counts": counts,
            "week": week,
            "apis": apis,
            "llm_enabled": llm_enabled,
            "llm_chain": llm_chain,
            "flash": flash,
            "sources": ["gmail", "llm", "scrape", "poll", "config"],
        },
    )


@app.post("/issues/probe")
def issues_probe(request: Request):
    require_token(request)
    results = probe_apis()
    bits = [f"{row['name']} {'ok' if row['ok'] else 'failed'}" for row in results]
    flash = "Probe: " + "; ".join(bits)
    return RedirectResponse(f"/issues?{urlencode({'flash': flash})}", status_code=303)


@app.post("/activity/check")
def activity_check(
    request: Request,
    session: Session = Depends(db_session),
    next: str = Form("/"),
):
    require_token(request)
    dest = safe_next(next, "/")
    try:
        run_once()
    except Exception as exc:
        logging.getLogger(__name__).exception("manual inbox check failed")
        return templates.TemplateResponse(
            request,
            "activity.html",
            _activity_context(session, error=str(exc)),
            status_code=500,
        )
    sep = "&" if "?" in dest else "?"
    return RedirectResponse(f"{dest}{sep}checked=1", status_code=303)


@app.post("/activity/reset")
def activity_reset(request: Request, session: Session = Depends(db_session)):
    require_token(request)
    clear_inbox(session, from_now=True)
    return RedirectResponse("/activity?fresh=1", status_code=303)


@app.post("/activity/watch")
def activity_watch(request: Request, session: Session = Depends(db_session)):
    require_token(request)
    try:
        start_gmail_watch()
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "activity.html",
            _activity_context(session, error=str(exc)),
            status_code=400,
        )
    return RedirectResponse("/activity?on=1", status_code=303)


@app.get("/api/breakdown")
def api_breakdown(
    session: Session = Depends(db_session),
    days: int = 1,
    since: str = "",
    until: str = "",
) -> dict:
    return build_breakdown(session, days=days, since=since or None, until=until or None).as_dict()


@app.get("/job/{job_id}", response_class=HTMLResponse)
def job_detail(
    job_id: int,
    request: Request,
    session: Session = Depends(db_session),
    flash: str = "",
):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    message = session.get(Message, job.message_id)
    return templates.TemplateResponse(
        request, "job_detail.html", {"job": job, "message": message, "flash": flash}
    )


@app.post("/job/{job_id}/status")
def set_job_status(
    job_id: int,
    request: Request,
    status: str = Form(...),
    redirect: str = Form("/matches"),
    session: Session = Depends(db_session),
):
    require_token(request)
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if status not in ("new", "saved", "applied", "ignored"):
        raise HTTPException(400, "bad status")
    job.status = status
    session.add(job)
    if status == "applied":
        create_from_job(session, job)
    session.commit()
    return RedirectResponse(redirect, status_code=303)


@app.post("/mail/{message_id}/flag")
def flag_mail(
    message_id: str,
    request: Request,
    session: Session = Depends(db_session),
    category: str = Form(""),
    apply: str = Form(""),
    redirect: str = Form("/"),
):
    require_token(request)
    row = session.get(Message, message_id)
    if not row:
        raise HTTPException(404, "message not found")
    category = (category or "").strip()
    if category and category not in CATEGORY_LABELS:
        raise HTTPException(400, "bad category")
    if not category:
        row.flagged_category = ""
        row.flagged_at = None
        flash = "Flag cleared."
    else:
        was = row.category
        row.flagged_category = category
        row.flagged_at = datetime.now(timezone.utc)
        flash = f"Flagged as {CATEGORY_LABELS.get(category, category)}."
        if apply:
            row.category = category
            flash = f"Set category to {CATEGORY_LABELS.get(category, category)}."
        try:
            upsert_extract_miss(
                session,
                row,
                note=f"flagged_category={category} (was {was})",
                source="flag",
            )
        except Exception:
            log.exception("could not store extract miss for flag %s", message_id)
    session.add(row)
    session.commit()
    dest = safe_next(redirect, "/")
    sep = "&" if "?" in dest else "?"
    return RedirectResponse(f"{dest}{sep}{urlencode({'flash': flash})}", status_code=303)


@app.post("/mail/{message_id}/reextract")
def reextract_mail(
    message_id: str,
    request: Request,
    session: Session = Depends(db_session),
    redirect: str = Form("/"),
):
    require_token(request)
    row = session.get(Message, message_id)
    if not row:
        raise HTTPException(404, "message not found")
    from .llm import LLM

    outcome = reextract_email(session, email_for_reextract(row), LLM())
    session.commit()
    stored = len(outcome.jobs)
    flash = (
        f"Re-extracted this email: {outcome.jobs_found} posting(s) parsed, "
        f"{stored} stored, {len(outcome.matched_jobs)} match."
    )
    dest = safe_next(redirect, f"/?m={message_id}&days=30")
    sep = "&" if "?" in dest else "?"
    return RedirectResponse(f"{dest}{sep}{urlencode({'flash': flash})}", status_code=303)


@app.post("/mail/{message_id}/miss")
def report_extract_miss(
    message_id: str,
    request: Request,
    session: Session = Depends(db_session),
    redirect: str = Form("/"),
    note: str = Form(""),
):
    """Save a structured miss file for this email. Repeat clicks update the same record."""
    require_token(request)
    row = session.get(Message, message_id)
    if not row:
        raise HTTPException(404, "message not found")
    miss = upsert_extract_miss(session, row, note=note, source="mail")
    session.commit()
    from .issues import record_issue

    payload = parse_extract_payload(row.extract_json)
    titles = ", ".join((item.get("title") or "?")[:40] for item in payload[:12]) or "(none)"
    record_issue(
        "extract",
        f"Extraction miss: {(row.subject or '(no subject)')[:80]}",
        (
            f"category={row.category} jobs_found={row.jobs_found} "
            f"raw_rows={len(payload)} body_chars={len(row.body_text or '')}\n"
            f"titles: {titles}\nnote={note}\n{(row.body_text or '')[:1500]}"
        ),
        severity="warn",
        message_id=row.id,
    )
    times = miss.report_count
    flash = (
        f"Saved extract miss ({times} report{'s' if times != 1 else ''} on this email). "
        "Same message stays one file. Open Misses to download it."
    )
    dest = safe_next(redirect, f"/?m={message_id}")
    sep = "&" if "?" in dest else "?"
    return RedirectResponse(f"{dest}{sep}{urlencode({'flash': flash})}", status_code=303)


@app.post("/job/{job_id}/miss")
def report_job_miss(
    job_id: int,
    request: Request,
    session: Session = Depends(db_session),
    redirect: str = Form(""),
    note: str = Form(""),
):
    """Flag a posting whose scraped content looks wrong. Upserts the parent email's miss."""
    require_token(request)
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    row = session.get(Message, job.message_id)
    if not row:
        raise HTTPException(404, "message not found")
    detail = (
        f"job_id={job.id} title={job.title or '?'} scrape={job.scrape_status or '?'} "
        f"extraction={job.extraction or '?'} desc_chars={len(job.description or '')}"
    )
    user_note = (note or "").strip()
    combined = f"{detail}\n{user_note}".strip() if user_note else detail
    miss = upsert_extract_miss(
        session,
        row,
        note=combined,
        source="job",
        extra={"job_id": job.id, "job_url": job.url, "scrape_status": job.scrape_status},
    )
    session.commit()
    flash = (
        f"Saved extract miss for this posting ({miss.report_count} report"
        f"{'s' if miss.report_count != 1 else ''} on the source email)."
    )
    dest = safe_next(redirect or f"/job/{job.id}", f"/job/{job.id}")
    sep = "&" if "?" in dest else "?"
    return RedirectResponse(f"{dest}{sep}{urlencode({'flash': flash})}", status_code=303)


@app.post("/job/{job_id}/rescrape")
def rescrape_job_page(
    job_id: int,
    request: Request,
    session: Session = Depends(db_session),
    redirect: str = Form(""),
):
    require_token(request)
    from .llm import LLM

    job = rescrape_job(session, job_id, LLM())
    if job is None:
        raise HTTPException(404, "job not found")
    session.commit()
    flash = f"Refetched {job.title or 'posting'}: fetch {job.scrape_status or '?'}, score {job.score:.2f}."
    dest = safe_next(redirect or f"/job/{job.id}", f"/job/{job.id}")
    sep = "&" if "?" in dest else "?"
    return RedirectResponse(f"{dest}{sep}{urlencode({'flash': flash})}", status_code=303)


@app.get("/misses", response_class=HTMLResponse)
def misses_page(request: Request, session: Session = Depends(db_session), flash: str = ""):
    return templates.TemplateResponse(
        request,
        "misses.html",
        {"misses": list_misses(session), "flash": flash},
    )


@app.get("/misses/export.jsonl")
def export_misses(request: Request, session: Session = Depends(db_session)):
    require_token(request)
    lines = []
    for row in list_misses(session, limit=2000):
        try:
            obj = json.loads(row.payload) if row.payload else {"message_id": row.message_id}
        except json.JSONDecodeError:
            obj = {"message_id": row.message_id, "payload": row.payload}
        if not isinstance(obj, dict):
            obj = {"message_id": row.message_id, "payload": obj}
        lines.append(json.dumps(obj, ensure_ascii=False))
    body = "\n".join(lines) + ("\n" if lines else "")
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="extract-misses.jsonl"'},
    )


@app.get("/misses/{message_id}.json")
def download_miss(message_id: str, request: Request, session: Session = Depends(db_session)):
    require_token(request)
    row = session.get(ExtractMiss, message_id)
    if not row:
        raise HTTPException(404, "miss not found")
    filename = f"{message_id}.json"
    return Response(
        content=row.payload or "{}",
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/flags", response_class=HTMLResponse)
def flags_page(request: Request, session: Session = Depends(db_session)):
    rows = session.exec(
        select(Message)
        .where(Message.flagged_category != "")
        .order_by(col(Message.flagged_at).desc())
        .limit(200)
    ).all()
    return templates.TemplateResponse(
        request,
        "flags.html",
        {
            "flags": rows,
            "category_labels": CATEGORY_LABELS,
            "category_order": CATEGORY_ORDER,
        },
    )


@app.get("/charts", response_class=HTMLResponse)
def charts_page(request: Request, session: Session = Depends(db_session)):
    runs = list(reversed(load_poll_runs(session, 96)))
    max_fetched = max((run.fetched for run in runs), default=1) or 1
    max_jobs = max((run.jobs_found for run in runs), default=1) or 1
    day_map: dict[str, dict] = {}
    for run in runs:
        label = et_day_label(run.started_at) if run.started_at else "unknown"
        bucket = day_map.setdefault(
            label,
            {"fetched": 0, "processed": 0, "jobs_found": 0, "jobs_matched": 0, "runs": 0, "errors": 0},
        )
        bucket["fetched"] += run.fetched or 0
        bucket["processed"] += run.processed or 0
        bucket["jobs_found"] += run.jobs_found or 0
        bucket["jobs_matched"] += run.jobs_matched or 0
        bucket["runs"] += 1
        bucket["errors"] += 1 if run.status == "error" else 0
    days = list(day_map.items())
    day_max = max((row["fetched"] for _, row in days), default=1) or 1
    return templates.TemplateResponse(
        request,
        "charts.html",
        {
            "runs": runs,
            "max_fetched": max_fetched,
            "max_jobs": max_jobs,
            "days": days,
            "day_max": day_max,
        },
    )


@app.get("/applications", response_class=HTMLResponse)
def applications_page(
    request: Request, session: Session = Depends(db_session), show: str = "open"
):
    stmt = select(Application)
    if show == "open":
        stmt = stmt.where(Application.closed == False)  # noqa: E712
    applications = session.exec(stmt.order_by(col(Application.last_event_at).desc())).all()
    fill_blank_roles(session, applications)

    events: dict[int, list[ApplicationEvent]] = {}
    if applications:
        ids = [a.id for a in applications]
        rows = session.exec(
            select(ApplicationEvent)
            .where(col(ApplicationEvent.application_id).in_(ids))
            .order_by(col(ApplicationEvent.occurred_at).desc())
        ).all()
        for event in rows:
            events.setdefault(event.application_id, []).append(event)

    counts = dict(
        session.exec(
            select(Application.status, func.count()).group_by(Application.status)
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "applications.html",
        {
            "applications": applications,
            "events": events,
            "counts": counts,
            "show": show,
            "stale": {a.id for a in stale_applications(session)},
        },
    )


@app.post("/application/{application_id}/status")
def set_application_status(
    application_id: int,
    request: Request,
    status: str = Form(...),
    redirect: str = Form("/applications"),
    session: Session = Depends(db_session),
):
    require_token(request)
    application = session.get(Application, application_id)
    if not application:
        raise HTTPException(404, "application not found")
    if status not in STATUS_RANK:
        raise HTTPException(400, "bad status")
    application.status = status
    application.closed = status in CLOSED_STATUSES
    application.last_event_at = datetime.now(timezone.utc)
    application.last_event = status
    session.add(application)
    session.add(
        ApplicationEvent(
            application_id=application.id,
            kind=status,
            subject=f"Marked {status}",
            occurred_at=datetime.now(timezone.utc),
        )
    )
    session.commit()
    return RedirectResponse(redirect, status_code=303)


@app.get("/api/applications")
def api_applications(session: Session = Depends(db_session), limit: int = 200) -> list[dict]:
    rows = session.exec(
        select(Application).order_by(col(Application.last_event_at).desc()).limit(limit)
    ).all()
    return [row.model_dump() for row in rows]


@app.get("/outreach", response_class=HTMLResponse)
def outreach_page(
    request: Request,
    session: Session = Depends(db_session),
    show: str = "open",
    days: int = 60,
    who: str = "all",
):
    demote_noise_followups(session)
    session.commit()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = select(Outreach).where(Outreach.received_at >= since, col(Outreach.kind).in_(FOLLOW_UP_KINDS))
    if show == "open":
        stmt = stmt.where(Outreach.handled == False)  # noqa: E712
    items = session.exec(stmt.order_by(col(Outreach.received_at).desc()).limit(300)).all()

    # "People" means a human wrote to you: drop the no-reply robots, keep everything else.
    if who == "people":
        items = [item for item in items if not NOREPLY_RE.search(item.person_email or "")]

    return templates.TemplateResponse(
        request, "outreach.html", {"items": items, "show": show, "days": days, "who": who}
    )


@app.post("/outreach/{item_id}/handled")
def mark_handled(
    item_id: int,
    request: Request,
    redirect: str = Form("/outreach"),
    session: Session = Depends(db_session),
):
    require_token(request)
    item = session.get(Outreach, item_id)
    if not item:
        raise HTTPException(404, "not found")
    item.handled = not item.handled
    session.add(item)
    session.commit()
    return RedirectResponse(redirect, status_code=303)


@app.post("/outreach/{item_id}/not-followup")
def mark_not_followup(
    item_id: int,
    request: Request,
    redirect: str = Form("/outreach"),
    session: Session = Depends(db_session),
):
    """This mail should not sit on Follow-ups. Keep the row, drop it from that list."""
    require_token(request)
    item = session.get(Outreach, item_id)
    if not item:
        raise HTTPException(404, "not found")
    item.handled = True
    item.kind = "other"
    session.add(item)
    session.commit()
    return RedirectResponse(redirect, status_code=303)


@app.get("/preview", response_class=HTMLResponse)
def preview_page(days: int = 1, hours: int = 0):
    lookback = days if hours <= 0 else max(1, (hours + 23) // 24)
    return RedirectResponse(f"/?days={lookback}", status_code=302)


@app.get("/messages", response_class=HTMLResponse)
def messages_page(days: int = 1, category: str = ""):
    qs = f"days={days}"
    if category:
        qs += f"&category={category}"
    return RedirectResponse(f"/?{qs}", status_code=302)


@app.get("/api/jobs")
def api_jobs(
    session: Session = Depends(db_session), status: str = "new", limit: int = 100
) -> list[dict]:
    stmt = select(Job)
    if status != "all":
        stmt = stmt.where(Job.status == status)
    jobs = session.exec(stmt.order_by(col(Job.score).desc()).limit(limit)).all()
    return [j.model_dump(exclude={"description"}) for j in jobs]


@app.get("/api/outreach")
def api_outreach(session: Session = Depends(db_session), limit: int = 100) -> list[dict]:
    items = session.exec(
        select(Outreach).order_by(col(Outreach.received_at).desc()).limit(limit)
    ).all()
    return [i.model_dump() for i in items]


@app.get("/api/stats")
def api_stats(session: Session = Depends(db_session)) -> dict:
    def count(model, *where):
        stmt = select(func.count()).select_from(model)
        for clause in where:
            stmt = stmt.where(clause)
        return session.exec(stmt).one()

    return {
        "messages": count(Message),
        "jobs": count(Job),
        "jobs_new": count(Job, Job.status == "new"),
        "outreach_open": count(
            Outreach, Outreach.handled == False, col(Outreach.kind).in_(FOLLOW_UP_KINDS)  # noqa: E712
        ),
    }


@app.post("/api/run")
def api_run(request: Request, max_messages: int | None = None) -> dict:
    require_token(request)
    try:
        return poll_since_cursor(max_messages, trigger="api").as_dict()
    except Exception as exc:
        logging.getLogger(__name__).exception("scheduled poll failed")
        from .issues import record_issue

        record_issue("poll", "Scheduled poll failed", str(exc))
        raise


@app.post("/api/gmail-push")
async def api_gmail_push(request: Request) -> dict:
    """Pub/Sub calls this when Gmail says the inbox changed."""
    require_token(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    parse_gmail_push(body)
    try:
        maybe_renew_watch()
    except Exception:
        logging.getLogger(__name__).exception("gmail watch renew failed")
    return run_once().as_dict()
