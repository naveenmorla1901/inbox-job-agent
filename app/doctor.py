"""`python -m app.run doctor` - check every integration and say exactly what is missing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
from sqlmodel import func, select

from .config import ROOT, get_profile, get_settings
from .db import init_db, session_scope
from .models import Application, Job, Message, Outreach

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
MARK = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]", SKIP: "[skip]"}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", hint: str = "") -> None:
        self.checks.append(Check(name, status, detail, hint))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    def render(self) -> str:
        width = max(len(c.name) for c in self.checks)
        lines = []
        for check in self.checks:
            lines.append(f"{MARK[check.status]} {check.name:<{width}}  {check.detail}")
            if check.hint and check.status in (FAIL, WARN):
                lines.append(f"{'':>7}{'':<{width}}  -> {check.hint}")
        return "\n".join(lines)


def check_files(report: Report) -> None:
    settings = get_settings()

    env = ROOT / ".env"
    report.add(
        "config file (.env)",
        OK if env.exists() else WARN,
        str(env) if env.exists() else "not found, using defaults",
        "copy .env.example .env",
    )

    try:
        profile = get_profile()
        placeholder = "Replace this block" in profile.resume_text
        report.add(
            "profile (config/profile.yaml)",
            WARN if placeholder else OK,
            f"{len(profile.skills)} skills, {len(profile.target_titles)} target titles"
            + (" - still the example resume" if placeholder else ""),
            "put your real resume text into config/profile.yaml",
        )
    except Exception as exc:
        report.add("profile (config/profile.yaml)", FAIL, str(exc), "copy config/profile.example.yaml config/profile.yaml")


def check_database(report: Report) -> None:
    settings = get_settings()
    kind = settings.database_url.split(":", 1)[0]
    try:
        init_db()
        with session_scope() as session:
            counts = {
                "messages": session.exec(select(func.count()).select_from(Message)).one(),
                "jobs": session.exec(select(func.count()).select_from(Job)).one(),
                "follow-ups": session.exec(select(func.count()).select_from(Outreach)).one(),
                "applications": session.exec(select(func.count()).select_from(Application)).one(),
            }
        detail = f"{kind}: " + ", ".join(f"{v} {k}" for k, v in counts.items())
        report.add("database", OK, detail)
    except Exception as exc:
        report.add("database", FAIL, f"{kind}: {exc}", "check DATABASE_URL")


def check_gmail(report: Report) -> None:
    settings = get_settings()
    secrets_path = settings.path(settings.google_client_secrets)
    token_path = settings.path(settings.gmail_token_file)
    has_inline = bool(settings.gmail_token_json.strip())

    report.add(
        "Gmail OAuth client",
        OK if secrets_path.exists() else (SKIP if has_inline else FAIL),
        str(secrets_path) if secrets_path.exists() else "not found",
        "Google Cloud Console -> Credentials -> OAuth client ID -> Desktop app, "
        "save as secrets/client_secret.json",
    )

    if not (token_path.exists() or has_inline):
        report.add(
            "Gmail token",
            FAIL,
            "no token yet",
            "run: python -m app.auth_setup (opens a browser once)",
        )
        return

    try:
        from .gmail_client import GmailClient

        client = GmailClient(settings)
        me = client.service.users().getProfile(userId="me").execute()
        report.add(
            "Gmail token",
            OK,
            f"{me.get('emailAddress')} - {me.get('messagesTotal', 0):,} messages in the account",
        )

        after = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
        query = f"{settings.gmail_query} after:{after}".strip()
        ids = client.list_message_ids(query, 100)
        report.add("Gmail query", OK, f"{len(ids)} message(s) in the last 24h matching {query!r}")
    except Exception as exc:
        report.add("Gmail token", FAIL, str(exc)[:200], "re-run: python -m app.auth_setup")


def check_llm(report: Report) -> None:
    settings = get_settings()
    from .llm import CLASSIFY, EXTRACT, LLM

    llm = LLM(settings)
    classify = llm.describe(CLASSIFY)
    extract = llm.describe(EXTRACT)
    if not llm.enabled:
        report.add(
            "LLM",
            SKIP,
            "disabled - rules only (this is a valid setup)",
            "set LLM_PROVIDER to gemini (or groq) and add any of GEMINI_API_KEY, GROQ_API_KEY, "
            "NVIDIA_API_KEY, DEEPSEEK_API_KEY, OPENROUTER_API_KEY",
        )
        return

    answer = llm.json('Reply with {"ping": "pong"} and nothing else.', "You output JSON only.")
    if answer:
        report.add("LLM", OK, f"classify {classify}")
        if extract != classify:
            report.add("LLM extract", OK, extract)
    else:
        report.add(
            "LLM",
            WARN,
            f"chain ready ({classify}) but the test call returned nothing",
            "the first provider is likely rate-limited; the next poll will skip it for a while",
        )


def check_telegram(report: Report) -> None:
    settings = get_settings()
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        report.add("Telegram", SKIP, "not configured - alerts print to the log instead")
        return
    try:
        resp = httpx.get(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe", timeout=15
        )
        resp.raise_for_status()
        username = resp.json().get("result", {}).get("username", "?")
        report.add("Telegram", OK, f"bot @{username}, chat id {settings.telegram_chat_id}")
    except Exception as exc:
        report.add("Telegram", WARN, str(exc)[:150], "check TELEGRAM_BOT_TOKEN")


def check_scraping(report: Report) -> None:
    settings = get_settings()
    if not settings.scrape_job_pages:
        report.add("job page fetching", SKIP, "SCRAPE_JOB_PAGES=false")
        return
    try:
        from .extract_jobs import JobCandidate
        from .scrape import fetch_job

        probe = JobCandidate(
            url="https://boards.greenhouse.io/embed/job_app?for=stripe&token=7532733",
            url_key="greenhouse:7532733",
        )
        job = fetch_job(probe, timeout=settings.scrape_timeout)
        report.add(
            "job page fetching",
            OK if job.ok else WARN,
            f"probe returned status={job.status} via={job.extraction or '-'} "
            f"({len(job.description)} chars)",
            "outbound HTTPS may be blocked on this network",
        )
    except Exception as exc:
        report.add("job page fetching", WARN, str(exc)[:150])


def check_dashboard(report: Report) -> None:
    settings = get_settings()
    if settings.api_token and settings.api_token != "change-me":
        report.add("dashboard auth", OK, "API_TOKEN set - login required")
    else:
        report.add(
            "dashboard auth",
            WARN,
            "API_TOKEN is the default, dashboard is open to anyone who reaches it",
            "fine for localhost; set a long random API_TOKEN before hosting it",
        )


def run_doctor(skip_network: bool = False) -> Report:
    report = Report()
    check_files(report)
    check_database(report)
    check_gmail(report)
    if not skip_network:
        check_llm(report)
        check_telegram(report)
        check_scraping(report)
    check_dashboard(report)
    return report
