"""Eval harness: explode one Gmail digest at a time, no page scrape.

`python -m app.run eval-extract --since 2026-09-04`
`python -m app.run eval-extract --since 2026-09-04 --one MESSAGE_ID`

Lists all mail in the window (not `GMAIL_QUERY`) and writes gitignored
`data/eval/{id}.json`. Screenshots stay in the Cursor browser session.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .classify import ALERT_SUBJECT, JOB_ALERT, classify_rules, _looks_like_job_board
from .config import ROOT, get_profile, get_settings
from .email_parse import ParsedEmail, parse_message
from .extract_jobs import extract_from_email
from .timefmt import EASTERN

log = logging.getLogger(__name__)

EVAL_DIR = ROOT / "data" / "eval"
IDS_FILE = "ids.json"


def since_epoch(since: str) -> int:
    """Unix seconds for `YYYY-MM-DD` at 00:00 America/New_York."""
    raw = (since or "").strip()
    try:
        local = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=EASTERN)
    except ValueError as exc:
        raise SystemExit(f"--since must be YYYY-MM-DD, got {since!r}") from exc
    return int(local.timestamp())


def window_query(since: str) -> str:
    """All mail after the Eastern midnight, including Promotions.

    Production poll uses `GMAIL_QUERY` (`in:inbox -category:promotions`). Eval
    must not, because job alerts often sit in Promotions.
    """
    return f"after:{since_epoch(since)}"


def eval_record(email: ParsedEmail, me_email: str = "") -> dict:
    jobs = extract_from_email(email, limit=None)
    screenshot, skip_reason = should_screenshot(email, len(jobs), me_email)
    return {
        "id": email.id,
        "thread_id": email.thread_id,
        "sender": email.sender_name,
        "sender_email": email.sender_email,
        "subject": email.subject,
        "job_count": len(jobs),
        "titles": [job.title for job in jobs],
        "gmail_link": email.gmail_link,
        "gmail_search_link": email.gmail_search_link,
        "received_at": email.received_at.isoformat(),
        "screenshot": screenshot,
        "skip_reason": skip_reason,
        "category_guess": classify_rules(email, get_profile(), job_count=len(jobs)).category,
    }


def write_eval_json(record: dict, html: str = "") -> Path:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    message_id = record["id"]
    path = EVAL_DIR / f"{message_id}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    if html:
        (EVAL_DIR / f"{message_id}.html").write_text(html, encoding="utf-8")
    return path


def should_screenshot(email: ParsedEmail, job_count: int, me_email: str) -> tuple[bool, str]:
    """Cheap skip: self-sent, or clearly not a digest and no posting URLs."""
    me = (me_email or "").strip().lower()
    sender = (email.sender_email or "").strip().lower()
    if me and sender == me:
        return False, "self"
    if _looks_like_digest(email, job_count) or job_count > 0:
        return True, ""
    return False, "not_digest_no_jobs"


def _looks_like_digest(email: ParsedEmail, job_count: int) -> bool:
    profile = get_profile()
    rules = classify_rules(email, profile, job_count=job_count)
    if rules.category == JOB_ALERT:
        return True
    if ALERT_SUBJECT.search(email.subject or ""):
        return True
    if _looks_like_job_board(email, profile):
        return True
    return False


def run_eval_extract(since: str = "", message_id: str = "") -> None:
    if not since and not message_id:
        raise SystemExit("eval-extract needs --since YYYY-MM-DD and/or --one MESSAGE_ID")

    from .gmail_client import GmailClient

    settings = get_settings()
    try:
        gmail = GmailClient(settings)
    except Exception as exc:
        raise SystemExit(f"Gmail is not authorised: {exc}") from exc

    me = ""
    try:
        me = gmail.me_email()
    except Exception as exc:
        log.warning("could not read Gmail profile email: %s", exc)

    if message_id:
        email = parse_message(gmail.get_message(message_id))
        record = eval_record(email, me)
        path = write_eval_json(record, html=email.html)
        print(f"id={email.id}")
        print(f"gmail={email.gmail_link}")
        if email.gmail_search_link:
            print(f"search={email.gmail_search_link}")
        print(f"jobs={record['job_count']}")
        print(f"screenshot={str(record['screenshot']).lower()}")
        if record["skip_reason"]:
            print(f"skip={record['skip_reason']}")
        print(f"wrote={path}")
        return

    query = window_query(since)
    ids = gmail.list_message_ids(query, max_results=None)
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    index_path = EVAL_DIR / IDS_FILE
    index_path.write_text(
        json.dumps({"since": since, "query": query, "ids": ids}, indent=2),
        encoding="utf-8",
    )
    print(f"query={query}")
    print(f"count={len(ids)}")
    print(f"wrote={index_path}")
    for mid in ids:
        print(mid)
