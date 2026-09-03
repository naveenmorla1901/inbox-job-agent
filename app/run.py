"""Command line entrypoint: python -m app.run <command>"""

from __future__ import annotations

import argparse
import json
import logging
import time

from .config import get_profile, get_settings
from .db import init_db, session_scope, set_state
from .pipeline import STATE_CURSOR, run_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs full request URLs at INFO, which would print API keys carried in query strings.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("app.run")


def cmd_poll(args: argparse.Namespace) -> None:
    stats = run_once(
        max_messages=args.max,
        since_days=getattr(args, "days", None),
        query=getattr(args, "query", "") or "",
        reclassify=getattr(args, "refresh", False),
        reextract=getattr(args, "refresh_jobs", False),
    )
    print(json.dumps(stats.as_dict(), indent=2))
    if getattr(args, "days", None) is not None:
        cmd_report(argparse.Namespace(days=args.days))


def cmd_doctor(args: argparse.Namespace) -> None:
    from .doctor import run_doctor

    report = run_doctor(skip_network=args.offline)
    print(report.render())
    failures = report.failed
    print()
    if failures:
        print(f"{len(failures)} blocking problem(s). Fix those and run doctor again.")
        raise SystemExit(1)
    print("All required integrations are working. Next: python -m app.run poll --days 1")


def cmd_report(args: argparse.Namespace) -> None:
    from .reporting import build_breakdown

    init_db()
    with session_scope() as session:
        report = build_breakdown(
            session,
            days=args.days,
            since=getattr(args, "since", "") or None,
            until=getattr(args, "until", "") or None,
        )

    width = max((len(label) for _, label, _ in report.categories), default=10)
    print(f"\n{report.window_label}: {report.messages} email(s) processed")
    print("-" * (width + 10))
    for _, label, count in report.categories:
        if count:
            print(f"{label:<{width}}  {count}")
    print("-" * (width + 10))
    print(f"job links found : {report.jobs_found}")
    print(f"postings saved  : {report.jobs_stored}")
    print(f"matching you    : {report.jobs_matched}")
    print(f"kept as ignored : {report.jobs_ignored}")
    print(f"repeats hidden  : {report.jobs_duplicates}")
    print(f"follow-ups open : {report.outreach_open}")
    print(f"acknowledgements: {report.acknowledgements}")
    print(f"rejections      : {report.rejections}")
    print(f"unclassified    : {report.unclassified}")
    if report.applications:
        pipeline = ", ".join(f"{status}={count}" for status, count in report.applications)
        print(f"applications    : {pipeline}")
    if report.scrape_status:
        print("link fetches    : " + ", ".join(f"{s}={c}" for s, c in report.scrape_status))
    if report.top_jobs:
        print("\ntop matches:")
        for job in report.top_jobs:
            print(f"  {job.score:.2f}  {job.title[:48]:<48} {job.company[:24]:<24} {job.url[:60]}")


def cmd_loop(args: argparse.Namespace) -> None:
    while True:
        try:
            stats = run_once(max_messages=args.max)
            log.info("poll done: %s", stats.as_dict())
        except Exception:
            log.exception("poll failed, will retry")
        time.sleep(args.interval)


def cmd_backfill(args: argparse.Namespace) -> None:
    init_db()
    with session_scope() as session:
        cutoff = int(time.time()) - args.days * 86400
        set_state(session, STATE_CURSOR, str(cutoff))
        session.commit()
    log.info("cursor moved back %d day(s)", args.days)
    cmd_poll(args)


def cmd_demo(_: argparse.Namespace) -> None:
    """Fill the DB from a bundled sample alert so the dashboard has something to show."""
    from pathlib import Path

    from .email_parse import ParsedEmail, extract_links, html_to_text
    from .llm import LLM
    from .pipeline import process_email

    fixture = Path(__file__).parent.parent / "tests" / "fixtures" / "linkedin_alert.html"
    html = fixture.read_text(encoding="utf-8")
    email = ParsedEmail(
        id="demo-message",
        sender_name="LinkedIn Job Alerts",
        sender_email="jobalerts-noreply@linkedin.com",
        subject="8 new jobs match your preferences",
        html=html,
        text=html_to_text(html),
        links=extract_links(html),
    )
    samples = [
        (
            "demo-applied",
            "careers@northwind.example",
            "Northwind Talent",
            "Thank you for applying to Machine Learning Engineer at Northwind Labs",
            "We have received your application and the team is reviewing it.",
        ),
        (
            "demo-interview",
            "maya.chen@northwind.example",
            "Maya Chen",
            "Interview invitation - Machine Learning Engineer",
            "Your application for Machine Learning Engineer at Northwind Labs is moving forward. "
            "Please share your availability this week to schedule an interview.",
        ),
        (
            "demo-recruiter",
            "priya@talentbridge.example",
            "Priya Raman",
            "Data Scientist opening - are you interested?",
            "I came across your profile and we have an urgent opening. "
            "Would you be interested? Please share your updated resume.",
        ),
    ]

    from .models import Message

    init_db()
    with session_scope() as session:
        if session.get(Message, email.id):
            print("demo data already present, nothing to do")
            return

        outcome = process_email(session, email, LLM())
        session.commit()
        print(f"stored {len(outcome.jobs)} of {outcome.jobs_found} sample jobs")

        for message_id, sender, name, subject, body in samples:
            sample = ParsedEmail(
                id=message_id,
                sender_name=name,
                sender_email=sender,
                subject=subject,
                text=body,
                snippet=body[:150],
            )
            result = process_email(session, sample, LLM())
            session.commit()
            print(f"  {subject[:52]:<52} -> {result.classification.category}")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=args.port, reload=args.reload)


def cmd_inspect(args: argparse.Namespace) -> None:
    from .inspect_run import run_inspect

    path = run_inspect(
        days=args.days,
        max_messages=args.max,
        message_id=args.message_id,
        scrape=not args.no_scrape,
        fixture_only=args.fixture_only,
    )
    print(f"wrote {path}")


def cmd_match(args: argparse.Namespace) -> None:
    """Score arbitrary text against the profile, to tune weights without touching Gmail."""
    from .matcher import match_job

    text = args.text or (open(args.file, encoding="utf-8").read() if args.file else "")
    result = match_job(get_profile(), args.title, text, args.location, args.company)
    print(json.dumps(result.__dict__, indent=2))


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="app.run")
    sub = parser.add_subparsers(dest="command", required=True)

    poll = sub.add_parser("poll", help="one pass over new mail")
    poll.add_argument("--max", type=int, default=None)
    poll.add_argument("--days", type=int, default=None, help="ignore the cursor, look back N days")
    poll.add_argument("--query", default="", help="override GMAIL_QUERY for this run")
    poll.add_argument(
        "--refresh",
        action="store_true",
        help="re-triage mail already stored (picks up next-step / video rounds)",
    )
    poll.add_argument(
        "--refresh-jobs",
        action="store_true",
        help="re-extract and re-scrape postings from mail already stored",
    )
    poll.set_defaults(func=cmd_poll)

    doctor = sub.add_parser("doctor", help="check Gmail, LLM, database, Telegram and scraping")
    doctor.add_argument("--offline", action="store_true", help="skip checks that need the network")
    doctor.set_defaults(func=cmd_doctor)

    report = sub.add_parser("report", help="category breakdown of what was processed")
    report.add_argument("--days", type=int, default=1)
    report.add_argument("--since", default="", help="start date YYYY-MM-DD (overrides --days)")
    report.add_argument("--until", default="", help="end date YYYY-MM-DD inclusive")
    report.set_defaults(func=cmd_report)

    loop = sub.add_parser("loop", help="poll forever (for a local machine or a container)")
    loop.add_argument("--interval", type=int, default=900)
    loop.add_argument("--max", type=int, default=None)
    loop.set_defaults(func=cmd_loop)

    backfill = sub.add_parser("backfill", help="rewind the cursor N days and poll")
    backfill.add_argument("--days", type=int, default=7)
    backfill.add_argument("--max", type=int, default=200)
    backfill.set_defaults(func=cmd_backfill)

    demo = sub.add_parser("demo", help="seed the DB from a sample alert email (no Gmail needed)")
    demo.set_defaults(func=cmd_demo)

    serve = sub.add_parser("serve", help="run the dashboard")
    serve.add_argument("--port", type=int, default=settings.port)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    match = sub.add_parser("match", help="score a job description against your profile")
    match.add_argument("--title", default="")
    match.add_argument("--company", default="")
    match.add_argument("--location", default="")
    match.add_argument("--text", default="")
    match.add_argument("--file", default="")
    match.set_defaults(func=cmd_match)

    inspect_cmd = sub.add_parser(
        "inspect",
        help="temporary: walk extraction / classify / scrape and write data/inspect-trace.md",
    )
    inspect_cmd.add_argument("--days", type=int, default=1)
    inspect_cmd.add_argument("--max", type=int, default=6)
    inspect_cmd.add_argument("--message-id", default="", help="inspect one Gmail message id")
    inspect_cmd.add_argument("--no-scrape", action="store_true")
    inspect_cmd.add_argument("--fixture-only", action="store_true", help="skip Gmail, use the sample alert")
    inspect_cmd.set_defaults(func=cmd_inspect)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
