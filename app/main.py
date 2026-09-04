from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, and_, col, func, select

from .applications import CLOSED_STATUSES, STATUS_RANK, create_from_job, stale_applications
from .classify import FOLLOW_UP_KINDS, NOREPLY_RE
from .config import ROOT, get_profile, get_settings
from .db import get_engine, init_db
from .gmail_client import parse_gmail_push
from .models import Application, ApplicationEvent, Job, Message, Outreach
from .pipeline import (
    clear_inbox,
    load_last_run,
    maybe_renew_watch,
    run_once,
    start_gmail_watch,
    watch_expiration_ms,
)
from .reporting import build_breakdown
from .timefmt import fmt_et, parse_et_datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs full request URLs at INFO, which would print API keys carried in query strings.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Inbox Job Agent", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
templates.env.filters["et"] = fmt_et

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
def jobs_page(
    request: Request,
    session: Session = Depends(db_session),
    status: str = "new",
    q: str = "",
    days: int = 30,
    min_score: float | None = None,
    sort: str = "score",
    show: str = "matched",
    duplicates: str = "hide",
):
    settings = get_settings()
    threshold = 0.0 if show == "all" else (settings.min_job_score if min_score is None else min_score)
    since = datetime.now(timezone.utc) - timedelta(days=days)

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
    order = col(Job.score).desc() if sort == "score" else col(Job.received_at).desc()
    jobs = session.exec(stmt.order_by(order).limit(300)).all()

    pending_outreach = pending_outreach_count(session)

    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": jobs,
            "status": status,
            "q": q,
            "days": days,
            "sort": sort,
            "show": show,
            "duplicates": duplicates,
            "duplicate_count": session.exec(
                select(func.count())
                .select_from(Job)
                .where(Job.duplicate_of != None, Job.received_at >= since)  # noqa: E711
            ).one(),
            "min_score": threshold,
            "pending_outreach": pending_outreach,
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
):
    report = build_breakdown(session, days=days, since=since or None, until=until or None)
    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "report": report,
            "days": days,
            "since": since,
            "until": until,
            "pending_outreach": pending_outreach_count(session),
        },
    )


def _activity_context(session: Session, **extra) -> dict:
    settings = get_settings()
    exp_ms = watch_expiration_ms(session)
    expires_at = (
        datetime.fromtimestamp(exp_ms / 1000, tz=timezone.utc) if exp_ms else None
    )
    watching = bool(exp_ms and exp_ms > time.time() * 1000)
    return {
        "pending_outreach": pending_outreach_count(session),
        "last_run": extra.pop("last_run", None) or load_last_run(session),
        "topic_ready": bool(settings.gmail_pubsub_topic.strip()),
        "watching": watching,
        "watch_until": fmt_et(expires_at, "%b %d, %I:%M %p ET") if expires_at else "",
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
    on: str = "",
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
    elif on:
        flash = "New-mail trigger is on. The next inbox message will be analyzed automatically."
    return templates.TemplateResponse(
        request, "activity.html", _activity_context(session, flash=flash)
    )


@app.post("/activity/check")
def activity_check(request: Request, session: Session = Depends(db_session)):
    require_token(request)
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
    return RedirectResponse("/activity?checked=1", status_code=303)


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
def job_detail(job_id: int, request: Request, session: Session = Depends(db_session)):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    message = session.get(Message, job.message_id)
    return templates.TemplateResponse(
        request, "job_detail.html", {"job": job, "message": message}
    )


@app.post("/job/{job_id}/status")
def set_job_status(
    job_id: int,
    request: Request,
    status: str = Form(...),
    redirect: str = Form("/"),
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


@app.get("/applications", response_class=HTMLResponse)
def applications_page(
    request: Request, session: Session = Depends(db_session), show: str = "open"
):
    stmt = select(Application)
    if show == "open":
        stmt = stmt.where(Application.closed == False)  # noqa: E712
    applications = session.exec(stmt.order_by(col(Application.last_event_at).desc())).all()

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


@app.get("/preview", response_class=HTMLResponse)
def preview_page(
    request: Request,
    session: Session = Depends(db_session),
    hours: int = 1,
    start: str = "",
    end: str = "",
):
    """Temporary extract review. Remove once you trust the pipeline."""
    hours = max(1, min(hours, 48))
    start_at = parse_et_datetime(start)
    end_at = parse_et_datetime(end)
    if start_at and end_at and end_at > start_at:
        since, until = start_at, end_at
        mail_filter = and_(Message.received_at >= since, Message.received_at < until)
        job_filter = and_(Job.received_at >= since, Job.received_at < until)
        outreach_filter = and_(Outreach.received_at >= since, Outreach.received_at < until)
    else:
        until = None
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        mail_filter = Message.received_at >= since
        job_filter = Job.received_at >= since
        outreach_filter = Outreach.received_at >= since

    messages = session.exec(
        select(Message).where(mail_filter).order_by(col(Message.received_at).desc())
    ).all()
    jobs = session.exec(select(Job).where(job_filter).order_by(col(Job.score).desc())).all()
    outreach = session.exec(
        select(Outreach).where(outreach_filter).order_by(col(Outreach.received_at).desc())
    ).all()
    jobs_by_mail: dict[str, list[Job]] = {}
    for job in jobs:
        jobs_by_mail.setdefault(job.message_id, []).append(job)
    outreach_by_mail = {item.message_id: item for item in outreach}
    categories: dict[str, int] = {}
    for message in messages:
        categories[message.category] = categories.get(message.category, 0) + 1
    scrape: dict[str, int] = {}
    for job in jobs:
        scrape[job.scrape_status or "unknown"] = scrape.get(job.scrape_status or "unknown", 0) + 1
    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            "hours": hours,
            "since": since,
            "until": until,
            "start": start,
            "end": end,
            "messages": messages,
            "jobs": jobs,
            "outreach": outreach,
            "jobs_by_mail": jobs_by_mail,
            "outreach_by_mail": outreach_by_mail,
            "categories": categories,
            "scrape": scrape,
            "matched": sum(1 for job in jobs if job.matched),
            "ignored": sum(1 for job in jobs if not job.matched),
        },
    )


@app.get("/messages", response_class=HTMLResponse)
def messages_page(
    request: Request,
    session: Session = Depends(db_session),
    limit: int = 100,
    category: str = "",
    email_type: str = "",
    days: int = 30,
):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = select(Message).where(Message.received_at >= since)
    if category:
        stmt = stmt.where(Message.category == category)
    if email_type:
        stmt = stmt.where(Message.email_type == email_type)
    messages = session.exec(stmt.order_by(col(Message.received_at).desc()).limit(limit)).all()
    return templates.TemplateResponse(
        request,
        "messages.html",
        {
            "messages": messages,
            "category": category,
            "email_type": email_type,
            "days": days,
        },
    )


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
    return run_once(max_messages).as_dict()


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
