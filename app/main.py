from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, col, func, select

from .applications import CLOSED_STATUSES, STATUS_RANK, create_from_job, stale_applications
from .classify import NOREPLY_RE
from .config import ROOT, get_profile, get_settings
from .db import get_engine, init_db
from .models import Application, ApplicationEvent, Job, Message, Outreach
from .pipeline import run_once
from .reporting import build_breakdown

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs full request URLs at INFO, which would print API keys carried in query strings.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Inbox Job Agent", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))

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

    pending_outreach = session.exec(
        select(func.count()).select_from(Outreach).where(Outreach.handled == False)  # noqa: E712
    ).one()

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
def overview_page(request: Request, session: Session = Depends(db_session), days: int = 1):
    report = build_breakdown(session, days=days)
    return templates.TemplateResponse(
        request, "overview.html", {"report": report, "days": days}
    )


@app.get("/api/breakdown")
def api_breakdown(session: Session = Depends(db_session), days: int = 1) -> dict:
    return build_breakdown(session, days=days).as_dict()


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
    who: str = "people",
):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = select(Outreach).where(Outreach.received_at >= since)
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


@app.get("/messages", response_class=HTMLResponse)
def messages_page(request: Request, session: Session = Depends(db_session), limit: int = 100):
    messages = session.exec(
        select(Message).order_by(col(Message.received_at).desc()).limit(limit)
    ).all()
    return templates.TemplateResponse(request, "messages.html", {"messages": messages})


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
        "outreach_open": count(Outreach, Outreach.handled == False),  # noqa: E712
    }


@app.post("/api/run")
def api_run(request: Request, max_messages: int | None = None) -> dict:
    require_token(request)
    return run_once(max_messages).as_dict()
