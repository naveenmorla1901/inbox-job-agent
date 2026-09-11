"""User-flagged extraction misses: one JSON blob per email, upserted on repeat reports.

Cloud Run cannot keep files, so Postgres is the source of truth. The dashboard
downloads JSON, and `python -m app.run pull-misses` writes `extract-misses/`
locally for Cursor / later extractor work.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, col, func, select

from .config import ROOT
from .email_parse import ParsedEmail
from .models import ExtractMiss, Job, Message, utcnow
from .pipeline import email_for_reextract, parse_extract_payload
from .timefmt import EASTERN

log = logging.getLogger(__name__)

SCHEMA = 1
BODY_CHARS = 20000
HTML_CHARS = 80000
DESC_EXCERPT = 800
CANDIDATE_CONTEXT = 400
MAX_LINKS = 80
NOTE_CHARS = 2000
SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")

HOW_TO_READ = (
    "User-flagged extraction failure. Read problem.tags and problem.user_note "
    "first — that is what the human saw. Then compare extracted.candidates "
    "(parser dump) with stored_jobs (Job rows after _should_store_jobs / scrape) "
    "and email.body_text / email.html. Typical fixes: extract_jobs.py (rules), "
    "llm_extract.py (digest recovery / expected count), pipeline._should_store_jobs "
    "(parsed but not stored), scrape.py (empty/blocked descriptions). Add a "
    "regression test from this email rather than guessing from a screenshot."
)

PROBLEM_HINTS = {
    "empty_body": "Stored email body is nearly empty; parser had little text to work with.",
    "zero_candidates": "Extractor returned no titled postings.",
    "parsed_not_stored": "Raw extract found titles, but no Job rows were stored.",
    "title_count_gap": "Body/anchors look like more postings than the extractor claimed.",
    "thin_scrape": "Stored jobs exist but descriptions are empty, blocked, or very short.",
    "missing_job_content": "User flagged a specific posting whose page content looks wrong.",
    "wrong_category": "User says the classifier picked the wrong category.",
}


def miss_dir() -> Path:
    preferred = ROOT / "extract-misses"
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "extract-misses"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def safe_message_id(message_id: str) -> str:
    return SAFE_ID.sub("_", (message_id or "").strip())[:80] or "unknown"


def miss_count(session: Session) -> int:
    return int(session.exec(select(func.count()).select_from(ExtractMiss)).one() or 0)


def list_misses(session: Session, limit: int = 200) -> list[ExtractMiss]:
    return list(
        session.exec(
            select(ExtractMiss).order_by(col(ExtractMiss.reported_at).desc()).limit(limit)
        ).all()
    )


def upsert_extract_miss(
    session: Session,
    message: Message,
    *,
    note: str = "",
    source: str = "mail",
    extra: dict | None = None,
) -> ExtractMiss:
    """Create or refresh the miss for this Gmail id. Repeat clicks increment report_count."""
    note = (note or "").strip()[:NOTE_CHARS]
    now = utcnow()
    row = session.get(ExtractMiss, message.id)
    old_payload: dict = {}
    if row and row.payload:
        try:
            loaded = json.loads(row.payload)
            if isinstance(loaded, dict):
                old_payload = loaded
        except json.JSONDecodeError:
            old_payload = {}

    if row is None:
        row = ExtractMiss(
            message_id=message.id,
            first_reported_at=now,
            report_count=1,
        )
    else:
        row.report_count = int(row.report_count or 1) + 1

    email = _live_email(message)
    payload = build_payload(
        session,
        message,
        note=_merge_notes(row.note if row.note else old_payload.get("problem", {}).get("user_note", ""), note, now),
        source=source,
        extra=extra,
        email=email,
        old_payload=old_payload,
        report_count=row.report_count,
        first_reported_at=row.first_reported_at or now,
        reported_at=now,
    )
    tags = payload.get("problem", {}).get("tags") or []
    row.reported_at = now
    row.subject = (message.subject or "")[:240]
    row.sender = (message.sender or "")[:240]
    row.category = message.category or ""
    row.note = payload["problem"].get("user_note") or ""
    row.problems = ",".join(tags)
    row.payload = json.dumps(payload, indent=2, ensure_ascii=False)
    session.add(row)
    session.flush()
    write_miss_file(payload)
    return row


def build_payload(
    session: Session,
    message: Message,
    *,
    note: str = "",
    source: str = "mail",
    extra: dict | None = None,
    email: ParsedEmail | None = None,
    old_payload: dict | None = None,
    report_count: int = 1,
    first_reported_at: datetime | None = None,
    reported_at: datetime | None = None,
) -> dict:
    old_payload = old_payload or {}
    reported_at = reported_at or utcnow()
    first_reported_at = first_reported_at or reported_at
    candidates = parse_extract_payload(message.extract_json)
    jobs = list(session.exec(select(Job).where(Job.message_id == message.id)).all())
    body_text = (email.body(BODY_CHARS) if email else "") or (message.body_text or "")
    html = ((email.html or "") if email else "")[:HTML_CHARS]
    links = _links(email, candidates)
    expected = _expected_count(email) if email else None
    stored = [_job_blob(job) for job in jobs]
    extracted = [_candidate_blob(item) for item in candidates]
    titled = [item for item in extracted if item.get("title")]
    tags = _problem_tags(
        message,
        titled_count=len(titled),
        stored=stored,
        expected=expected,
        body_text=body_text,
        source=source,
        extra=extra,
        flagged=bool(message.flagged_category),
    )
    sources = list(
        dict.fromkeys([*(old_payload.get("sources") or []), source])
    )
    summary = _summary(tags, note, titled_count=len(titled), stored_count=len(stored), expected=expected)
    return {
        "schema": SCHEMA,
        "how_to_read": HOW_TO_READ,
        "message_id": message.id,
        "reported_at": _iso(reported_at),
        "first_reported_at": _iso(first_reported_at),
        "report_count": report_count,
        "source": source,
        "sources": sources,
        "problem": {
            "tags": tags,
            "summary": summary,
            "user_note": note,
            "hints": {tag: PROBLEM_HINTS[tag] for tag in tags if tag in PROBLEM_HINTS},
        },
        "gap": {
            "jobs_found_field": message.jobs_found,
            "candidate_count": len(extracted),
            "titled_candidate_count": len(titled),
            "stored_job_count": len(stored),
            "expected_posting_count": expected,
            "candidates_minus_stored": len(titled) - len(stored),
        },
        "email": {
            "id": message.id,
            "thread_id": message.thread_id or (email.thread_id if email else ""),
            "gmail_link": email.gmail_link if email else f"https://mail.google.com/mail/u/0/#all/{message.thread_id or message.id}",
            "gmail_search_link": (email.gmail_search_link if email else ""),
            "sender": message.sender,
            "sender_email": message.sender_email,
            "subject": message.subject,
            "snippet": message.snippet,
            "received_at": _iso(message.received_at),
            "category": message.category,
            "confidence": message.confidence,
            "reason": message.reason,
            "summary": message.summary,
            "email_type": message.email_type,
            "flagged_category": message.flagged_category,
            "jobs_found": message.jobs_found,
            "jobs_matched": message.jobs_matched,
            "body_chars": len(body_text or ""),
            "html_chars": len(html or ""),
            "html_truncated": bool(email and email.html and len(email.html) > HTML_CHARS),
            "body_text": (body_text or "")[:BODY_CHARS],
            "html": html,
            "link_count": len(links),
            "links": links[:MAX_LINKS],
        },
        "extracted": {
            "candidate_count": len(extracted),
            "titles": [item.get("title") or "" for item in extracted],
            "candidates": extracted,
        },
        "stored_jobs": stored,
        "extra": extra or {},
    }


def write_miss_file(payload: dict) -> Path | None:
    message_id = safe_message_id(str(payload.get("message_id") or ""))
    try:
        directory = miss_dir()
        path = directory / f"{message_id}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        write_index(directory)
        return path
    except OSError as exc:
        log.warning("could not write miss file for %s: %s", message_id, exc)
        return None


def dump_misses(session: Session, dest: Path | None = None) -> list[Path]:
    """Write every stored miss to extract-misses/{id}.json for local AI work."""
    dest = dest or miss_dir()
    dest.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for row in list_misses(session, limit=2000):
        try:
            payload = json.loads(row.payload) if row.payload else {}
        except json.JSONDecodeError:
            payload = {"message_id": row.message_id, "payload": row.payload}
        if not isinstance(payload, dict):
            payload = {"message_id": row.message_id, "payload": payload}
        payload.setdefault("message_id", row.message_id)
        path = dest / f"{safe_message_id(row.message_id)}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        paths.append(path)
    write_index(dest)
    return paths


def write_index(directory: Path) -> Path | None:
    rows = []
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        problem = data.get("problem") or {}
        rows.append(
            {
                "message_id": data.get("message_id") or path.stem,
                "subject": (data.get("email") or {}).get("subject") or "",
                "tags": problem.get("tags") or [],
                "summary": problem.get("summary") or "",
                "user_note": problem.get("user_note") or "",
                "report_count": data.get("report_count") or 1,
                "reported_at": data.get("reported_at") or "",
                "file": path.name,
            }
        )
    index = {
        "how_to_read": HOW_TO_READ,
        "count": len(rows),
        "misses": rows,
    }
    path = directory / "_index.json"
    try:
        path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path
    except OSError as exc:
        log.warning("could not write miss index: %s", exc)
        return None


def _live_email(message: Message) -> ParsedEmail | None:
    """Gmail copy when available so the miss file has HTML, not only the stored stub."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    try:
        from .gmail_client import gmail_token_present

        if not gmail_token_present():
            return None
        return email_for_reextract(message)
    except Exception as exc:
        log.warning("miss capture skipped live Gmail for %s: %s", message.id, exc)
        return None


def _expected_count(email: ParsedEmail) -> int | None:
    try:
        from .llm_extract import _anchor_table, expected_posting_count

        return expected_posting_count(email, _anchor_table(email, cap=80))
    except Exception:
        return None


def _problem_tags(
    message: Message,
    *,
    titled_count: int,
    stored: list[dict],
    expected: int | None,
    body_text: str,
    source: str,
    extra: dict | None,
    flagged: bool,
) -> list[str]:
    tags: list[str] = []
    if len((body_text or "").strip()) < 40:
        tags.append("empty_body")
    if titled_count == 0:
        tags.append("zero_candidates")
    if titled_count > 0 and not stored:
        tags.append("parsed_not_stored")
    if expected is not None and expected >= titled_count + 2:
        tags.append("title_count_gap")
    thin = [
        job
        for job in stored
        if job.get("scrape_status") in ("empty", "blocked", "error")
        or int(job.get("description_chars") or 0) < 80
    ]
    if thin and stored:
        tags.append("thin_scrape")
    if source == "job" or (extra or {}).get("job_id"):
        tags.append("missing_job_content")
    if flagged or source == "flag":
        tags.append("wrong_category")
    return tags


def _summary(
    tags: list[str],
    note: str,
    *,
    titled_count: int,
    stored_count: int,
    expected: int | None,
) -> str:
    bits = []
    if note:
        bits.append(note.splitlines()[0][:180])
    if "parsed_not_stored" in tags:
        bits.append(f"parsed {titled_count} titled posting(s), stored 0")
    elif "title_count_gap" in tags and expected is not None:
        bits.append(f"expected about {expected} postings, extractor claimed {titled_count}")
    elif "thin_scrape" in tags:
        bits.append("job rows exist but descriptions look empty or blocked")
    elif "empty_body" in tags:
        bits.append("email body is nearly empty")
    elif "zero_candidates" in tags:
        bits.append("extractor found no postings")
    elif "wrong_category" in tags:
        bits.append("wrong category")
    if not bits:
        bits.append(f"parsed {titled_count}, stored {stored_count}")
    return "; ".join(dict.fromkeys(bits))


def _candidate_blob(item: dict) -> dict:
    return {
        "title": str(item.get("title") or ""),
        "company": str(item.get("company") or ""),
        "location": str(item.get("location") or ""),
        "url": str(item.get("url") or ""),
        "url_key": str(item.get("url_key") or ""),
        "source": str(item.get("source") or ""),
        "context": str(item.get("context") or "")[:CANDIDATE_CONTEXT],
    }


def _job_blob(job: Job) -> dict:
    description = job.description or ""
    return {
        "id": job.id,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "url": job.url,
        "url_key": job.url_key,
        "source": job.source,
        "source_type": job.source_type,
        "scrape_status": job.scrape_status,
        "extraction": job.extraction,
        "score": job.score,
        "matched": job.matched,
        "description_chars": len(description),
        "description_excerpt": description[:DESC_EXCERPT],
    }


def _links(email: ParsedEmail | None, candidates: list[dict]) -> list[dict]:
    if email and email.links:
        return [{"url": link.url, "text": link.text} for link in email.links]
    out = []
    seen: set[str] = set()
    for item in candidates:
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "text": str(item.get("title") or "")})
    return out


def _merge_notes(old: str, new: str, when: datetime) -> str:
    old = (old or "").strip()
    new = (new or "").strip()
    if not new:
        return old
    if not old:
        return new
    if new in old:
        return old
    stamp = when.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M ET")
    return f"{old}\n---\n[{stamp}] {new}"


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()
