"""Second-stage, LLM-backed digest extraction.

The regex extractor in :mod:`app.extract_jobs` is fast, free, and grounded, but
every job board invents a new link shape and card layout, so it always misses
something on formats it has never seen. This module adds a recovery pass:

1. Parse — collect every clickable link in the email (already done upstream).
2. Rules — run the regex extractor first (fast, no tokens, and it grounds URLs).
3. Recover — only when the rules look incomplete on a digest, hand the whole
   email plus a numbered table of its real links to an LLM (Gemini's context
   window swallows the entire message) and ask which links are job postings.
4. Merge — union rules + LLM by canonical key; the LLM may only *reference* a
   link number from the table, so a posting can never point at a made-up URL.

`extract_postings` is the single entry point the pipeline uses.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .classify import ALERT_SUBJECT, JOB_ALERT, _looks_like_job_board, classify_rules
from .config import get_profile, get_settings
from .email_parse import ParsedEmail, clean_text, html_to_text
from .extract_jobs import (
    APPLY_CTA,
    FOOTER_TEXT,
    JUNK_TITLE,
    NOISE_LINE,
    TITLE_ROLE,
    JobCandidate,
    canonical_key,
    company_from_sender,
    extract_from_email,
    is_click_tracker,
    is_footer_link,
    is_job_url,
    source_of,
    unwrap_url,
)
from .llm import EXTRACT, LLM

log = logging.getLogger(__name__)

LLM_DIGEST_SYSTEM = (
    "You read a job-alert email and list the job postings it advertises. "
    "You never invent links: you only reference link numbers from the table given to you. "
    "Reply with JSON only, no prose."
)

LLM_DIGEST_PROMPT = """This email may advertise several job postings. Below is a numbered
list of the real clickable LINKS in the email (with their visible text), then the email TEXT
for context (company names, locations, salaries live there).

A rough scan of this email suggests it advertises about {expected} job title(s). That number
is only a hint, but if you return far fewer rows than that you have almost certainly skipped
some: read the ENTIRE email, top to bottom, including long tables and repeated blocks.

List every DISTINCT job posting the email advertises. For each posting choose the ONE link
number that opens that job.

Rules:
- Use only link numbers from the LINKS list. Never invent a URL or a number.
- If a job title is named in the email but no link in the list opens it, still return the row
  with "link": null. Do not guess a number that belongs to a different job.
- One row per job. If a job has both a title link and a separate "Apply"/"1-Click Apply"
  link, use the title link and list the job once.
- Two different jobs can share the same link; keep both rows if the titles differ.
- Skip links that are not a single job posting: unsubscribe, manage/preferences, account,
  login, "see all"/"view more"/"more jobs", search pages, company logos, social, app-store,
  articles, and salary-tool links.
- title: the job title, cleaned (required, non-empty).
- company: the hiring company if shown anywhere for that job, else "".
- location: city / state / "Remote" if shown, else "".

Return JSON exactly:
{{"postings": [{{"link": <number or null>, "title": "...", "company": "...", "location": "..."}}]}}

LINKS:
{links}

EMAIL TEXT:
{text}
"""

LLM_DIGEST_RETRY_PROMPT = """You already listed some postings from this email, but the scan
suggests it advertises about {expected} job titles and only {found} were returned. Find the
ones that are MISSING.

Already found (do not repeat these):
{found_titles}

Same rules as before: only link numbers from the LINKS list, "link": null when no link opens
that job, one row per distinct job title, skip navigation and unsubscribe links.

Return JSON exactly:
{{"postings": [{{"link": <number or null>, "title": "...", "company": "...", "location": "..."}}]}}

LINKS:
{links}

EMAIL TEXT:
{text}
"""


@dataclass
class Anchor:
    idx: int
    text: str
    url: str  # unwrapped destination
    key: str  # canonical de-dupe key
    jobish: bool  # on a known board / click tracker / posting-shaped path


def _anchor_table(email: ParsedEmail, cap: int) -> list[Anchor]:
    """Every non-footer link in the email, de-duped by canonical key, numbered.

    Keeps job-board / click-tracker links plus any link that carries visible text
    (career-site blasts hide postings behind vanity domains we do not hard-code)."""
    anchors: list[Anchor] = []
    seen: set[str] = set()
    if not email.html:
        for link in email.links:
            url = unwrap_url(link.url)
            # De-dupe on the destination URL, not the canonical key: opaque click
            # trackers (/ls/click?upn=…, /t/…) share one path across every posting,
            # so keying on canonical_key would collapse all of them into one anchor.
            if url in seen:
                continue
            seen.add(url)
            anchors.append(
                Anchor(len(anchors), (link.text or "")[:160], url, canonical_key(url),
                       bool(source_of(url) or is_click_tracker(link.url) or is_job_url(link.url)))
            )
            if len(anchors) >= cap:
                break
        return anchors

    soup = BeautifulSoup(email.html, "lxml")
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href.lower().startswith("http"):
            continue
        text = clean_text(a.get_text(" "))[:160]
        if is_footer_link(text, href):
            continue
        url = unwrap_url(href)
        if url in seen:
            continue
        jobish = bool(source_of(url) or is_click_tracker(href) or is_job_url(href))
        # A bare link with no text and no board signal is navigation/tracking noise.
        if not jobish and len(text) < 3:
            continue
        seen.add(url)
        anchors.append(Anchor(len(anchors), text, url, canonical_key(url), jobish))
        if len(anchors) >= cap:
            break
    return anchors


def looks_like_digest(email: ParsedEmail, rules_count: int) -> bool:
    profile = get_profile()
    if classify_rules(email, profile, job_count=rules_count).category == JOB_ALERT:
        return True
    if ALERT_SUBJECT.search(email.subject or ""):
        return True
    return _looks_like_job_board(email, profile)


def _title_like(text: str) -> bool:
    blob = (text or "").strip()
    if len(blob) < 6:
        return False
    if APPLY_CTA.match(blob) or JUNK_TITLE.search(blob):
        return False
    return True


def _role_like(text: str) -> bool:
    """Reads as a job title: role noun, sane length, not a CTA or footer line."""
    blob = " ".join((text or "").split())
    if not (6 <= len(blob) <= 180):
        return False
    if APPLY_CTA.match(blob) or FOOTER_TEXT.match(blob) or NOISE_LINE.match(blob):
        return False
    if JUNK_TITLE.search(blob) or blob.lower().startswith("http"):
        return False
    return bool(TITLE_ROLE.search(blob))


def _title_fingerprint(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split())


def expected_posting_count(email: ParsedEmail, anchors: list[Anchor]) -> int:
    """How many job titles this email looks like it names, ignoring the rules.

    Career-site blasts (Apple, UKG/BAL, Zillow) name every role in the body and in
    the anchor labels even when their URLs match none of our posting patterns. The
    rules see nothing, so without this the recovery pass never runs. Counting
    distinct role-shaped strings costs nothing and tells the LLM how deep to read.
    """
    role_anchors = {
        _title_fingerprint(a.text) for a in anchors if _role_like(a.text)
    } - {""}
    from_anchors = role_anchors
    if len(role_anchors) >= 2:
        # Several role-shaped rows means this is a list of postings, so trust the
        # other title-like labels beside them too: role nouns we do not carry
        # ("Paralegal", "Actuary") sit in the same table as ones we do.
        from_anchors = {
            _title_fingerprint(a.text) for a in anchors if _title_like(a.text)
        } - {""}
    body = email.body()
    from_text = {
        _title_fingerprint(line)
        for line in (body or "").splitlines()
        if _role_like(line)
    } - {""}
    return max(len(from_anchors), len(from_text))


def _rules_look_incomplete(
    rules: list[JobCandidate], anchors: list[Anchor], expected: int
) -> bool:
    """Spend an LLM call only when the rules plausibly missed postings.

    Two signals, either is enough. A posting-shaped link with a title-like label
    that became no candidate is the strong one. The weak one is the role-title
    count above: when the email names many more roles than the rules produced, the
    link shapes are unfamiliar rather than absent. Nav/CTA links on a job-board
    domain ("Manage job alerts", "View more") count for neither, so digests we
    already handle spend no tokens."""
    if not rules:
        return True
    if expected >= len(rules) + 2:
        return True
    claimed = {c.url_key for c in rules}
    unclaimed = [
        a
        for a in anchors
        if a.key not in claimed and _title_like(a.text) and is_job_url(a.url)
    ]
    return len(unclaimed) >= 2


def _valid_title(title: str) -> bool:
    t = (title or "").strip()
    if len(t) < 3 or len(t) > 200:
        return False
    if JUNK_TITLE.search(t) or APPLY_CTA.match(t) or t.lower().startswith("http"):
        return False
    return True


def _rows_to_candidates(
    postings: object,
    email: ParsedEmail,
    anchors: list[Anchor],
    fallback_company: str,
    limit: int | None,
) -> list[JobCandidate]:
    if not isinstance(postings, list):
        return []
    out: dict[str, JobCandidate] = {}
    for row in postings:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title", "")).strip()
        if not _valid_title(title):
            continue
        company = str(row.get("company", "")).strip()[:150]
        if not company or JUNK_TITLE.search(company):
            company = fallback_company or ""

        raw_link = row.get("link")
        if raw_link is None or raw_link == "":
            anchor = None  # the email names this role but nothing links to it
        else:
            try:
                idx = int(raw_link)
            except (TypeError, ValueError):
                continue
            if not 0 <= idx < len(anchors):
                continue  # a number outside the table is a hallucination, not a role
            anchor = anchors[idx]

        if anchor is not None:
            # Two distinct roles can legitimately share one link (career-site
            # blasts often point every row at the same search page), so the key
            # carries the title once that link is already taken.
            key = anchor.key
            if key in out and _title_fingerprint(out[key].title) != _title_fingerprint(title):
                key = f"{anchor.key}#{_title_fingerprint(title)}"[:180]
            url, source, context = anchor.url, source_of(anchor.url), anchor.text
            if not source and is_click_tracker(anchor.url):
                source = "appcast"
        else:
            # Named in the email but nothing links to it. Keep the row so the
            # title still shows up; matching falls back to the email text.
            key = f"card:{(company or 'job').lower()}:{_title_fingerprint(title)}"[:180]
            url, source, context = "", "", ""

        if key in out:
            continue
        out[key] = JobCandidate(
            url=url,
            url_key=key,
            title=title[:200],
            company=company,
            location=str(row.get("location", "")).strip()[:150],
            source=source,
            context=(context or "")[:600],
        )
        if limit is not None and len(out) >= limit:
            break
    return list(out.values())


def llm_extract_digest(
    email: ParsedEmail,
    llm: LLM,
    anchors: list[Anchor],
    limit: int | None,
    expected: int = 0,
) -> list[JobCandidate]:
    settings = get_settings()
    fallback_company = company_from_sender(email)
    links_block = (
        "\n".join(f"[{a.idx}] text={a.text!r} url={a.url[:180]}" for a in anchors)
        or "(no links in this email)"
    )
    body = email.body(settings.llm_extract_body_chars)
    data = llm.json(
        LLM_DIGEST_PROMPT.format(
            links=links_block, text=body, expected=max(expected, 1)
        ),
        LLM_DIGEST_SYSTEM,
        task=EXTRACT,
        timeout=60,
    )
    found = _rows_to_candidates(
        data.get("postings") if isinstance(data, dict) else None,
        email,
        anchors,
        fallback_company,
        limit,
    )

    # One top-up call when the model clearly stopped early on a long digest.
    short_by = expected - len(found)
    room = limit is None or len(found) < limit
    if found and room and short_by >= 3:
        extra = llm.json(
            LLM_DIGEST_RETRY_PROMPT.format(
                links=links_block,
                text=body,
                expected=expected,
                found=len(found),
                found_titles="\n".join(f"- {c.title}" for c in found[:60]),
            ),
            LLM_DIGEST_SYSTEM,
            task=EXTRACT,
            timeout=60,
        )
        seen = {c.url_key for c in found}
        titles = {_title_fingerprint(c.title) for c in found}
        remaining = None if limit is None else max(0, limit - len(found))
        for candidate in _rows_to_candidates(
            extra.get("postings") if isinstance(extra, dict) else None,
            email,
            anchors,
            fallback_company,
            remaining,
        ):
            if candidate.url_key in seen or _title_fingerprint(candidate.title) in titles:
                continue
            found.append(candidate)
            seen.add(candidate.url_key)
            titles.add(_title_fingerprint(candidate.title))
        if len(found) > short_by:
            log.info("digest top-up pass brought the total to %d posting(s)", len(found))
    return found


def _merge(
    rules: list[JobCandidate], recovered: list[JobCandidate], limit: int | None
) -> list[JobCandidate]:
    merged: dict[str, JobCandidate] = {c.url_key: c for c in rules}
    added = 0
    for cand in recovered:
        existing = merged.get(cand.url_key)
        if existing is not None:
            # Rules ground the title; only backfill fields the rules left blank.
            if not existing.company and cand.company:
                existing.company = cand.company
            if not existing.location and cand.location:
                existing.location = cand.location
            continue
        if limit is not None and len(merged) >= limit:
            break
        merged[cand.url_key] = cand
        added += 1
    if added:
        log.info("llm recovered %d extra posting(s) the rules missed", added)
    return list(merged.values())


def extract_postings(
    email: ParsedEmail, llm: LLM | None = None, limit: int | None = 25
) -> list[JobCandidate]:
    """Rules first; an LLM recovery pass fills the gaps on digests it under-reads."""
    rules = extract_from_email(email, limit=limit)
    settings = get_settings()
    if (
        llm is None
        or not settings.llm_extract_digests
        or not llm.enabled
        or not looks_like_digest(email, len(rules))
    ):
        return rules

    anchors = _anchor_table(email, cap=settings.llm_extract_max_anchors)
    expected = expected_posting_count(email, anchors)
    if not _rules_look_incomplete(rules, anchors, expected):
        return rules

    try:
        recovered = llm_extract_digest(email, llm, anchors, limit, expected=expected)
    except Exception as exc:  # never let a recovery attempt break the poll
        log.warning("llm digest extraction failed: %s", exc)
        return rules
    if not recovered:
        return rules
    return _merge(rules, recovered, limit)
