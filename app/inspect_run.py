"""Temporary pipeline tracer.

Walks parse → link extract → classify → scrape → match for a handful of emails
and writes a readable markdown file. Nothing is stored in the database.

Remove this module and the `inspect` CLI command once you trust the pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .classify import classify_email, classify_rules
from .config import ROOT, get_profile, get_settings
from .email_parse import ParsedEmail, extract_links, html_to_text, parse_message
from .extract_jobs import JobCandidate, extract_from_email
from .job_fields import enrich_fields
from .llm import EXTRACT, CLASSIFY, LLM
from .matcher import match_job, title_worth_scraping
from .pipeline import _should_store_jobs, build_query
from .scrape import fetch_all, fetch_job, llm_extract

TRACE_PATH = ROOT / "data" / "inspect-trace.md"


def _clip(text: str, limit: int = 400) -> str:
    text = (text or "").replace("\r\n", "\n").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… ({len(text)} chars total)"


def _md_block(text: str) -> str:
    return f"```\n{_clip(text, 800)}\n```"


def _fixture_email() -> ParsedEmail:
    html = (ROOT / "tests" / "fixtures" / "linkedin_alert.html").read_text(encoding="utf-8")
    return ParsedEmail(
        id="fixture-linkedin-alert",
        sender_name="LinkedIn Job Alerts",
        sender_email="jobalerts-noreply@linkedin.com",
        subject="8 new jobs match your preferences",
        html=html,
        text=html_to_text(html),
        links=extract_links(html),
        received_at=datetime.now(timezone.utc),
        snippet="LinkedIn found roles matching your alert.",
    )


def _load_live_emails(days: int, max_messages: int, message_id: str) -> list[ParsedEmail] | str:
    try:
        from .gmail_client import GmailClient
    except Exception as exc:
        return f"Gmail client import failed: {exc}"

    settings = get_settings()
    try:
        gmail = GmailClient(settings)
    except Exception as exc:
        return f"Gmail is not authorised ({exc}). Run `python -m app.auth_setup` or use --fixture-only."

    try:
        if message_id:
            return [parse_message(gmail.get_message(message_id))]
        from .db import init_db, session_scope

        init_db()
        with session_scope() as session:
            query = build_query(session, since_days=days)
        ids = gmail.list_message_ids(query, max_messages)
        return [parse_message(gmail.get_message(mid)) for mid in ids]
    except Exception as exc:
        return f"Gmail fetch failed: {exc}"


def _trace_email(email: ParsedEmail, llm: LLM, scrape: bool, label: str) -> list[str]:
    settings = get_settings()
    profile = get_profile()
    lines = [
        f"## {label}",
        "",
        f"- **Gmail id:** `{email.id}`",
        f"- **From:** {email.sender_name} `<{email.sender_email}>`",
        f"- **Subject:** {email.subject or '(none)'}",
        f"- **Received:** {email.received_at.isoformat()}",
        f"- **Has plain text:** {'yes' if email.text else 'no'} ({len(email.text)} chars)",
        f"- **Has HTML:** {'yes' if email.html else 'no'} ({len(email.html)} chars)",
        f"- **`<a href>` links in HTML:** {len(email.links)}",
        "",
        "### Step 1 — parse the MIME message",
        "",
        "Both `text/plain` and `text/html` parts are kept. Classification reads text first,",
        "then HTML converted to text. Job extraction prefers HTML anchors so HTML-only",
        "career-site alerts are not missed.",
        "",
        "Snippet / first body lines:",
        _md_block(email.snippet or email.body(500)),
        "",
        "### Step 2 — extract posting links (before classification)",
        "",
    ]

    candidates = extract_from_email(email, limit=settings.max_jobs_per_email)
    lines.append(
        f"Found **{len(candidates)}** posting candidate(s). "
        "Click-trackers are unwrapped; search/unsubscribe URLs are dropped."
    )
    lines.append("")
    if not candidates:
        lines.append("_No job URLs survived the host / path filters._")
        lines.append("")
    for i, cand in enumerate(candidates, 1):
        lines.extend(
            [
                f"**Candidate {i}**",
                f"- title from email: {cand.title or '(none yet)'}",
                f"- company from email: {cand.company or '(none yet)'}",
                f"- location from email: {cand.location or '(none yet)'}",
                f"- source / url_key: `{cand.source}` / `{cand.url_key}`",
                f"- url: {cand.url}",
                "",
            ]
        )

    lines.extend(["### Step 3 — classify the email", ""])
    rules = classify_rules(email, profile, job_count=len(candidates))
    lines.append(
        f"Rules said **`{rules.category}`** (confidence {rules.confidence:.2f}, reason: {rules.reason})."
    )
    lines.append("")

    used_llm = False
    before = llm.last_used
    result = classify_email(email, profile, llm, job_count=len(candidates))
    if result.reason == "llm":
        used_llm = True
        lines.append(
            f"LLM refined it to **`{result.category}`** "
            f"(confidence {result.confidence:.2f}) using `{llm.last_used or before or 'unknown'}`."
        )
    else:
        lines.append(
            f"LLM was **not** called "
            f"({'disabled' if not llm.enabled else 'rules were confident enough'}). "
            f"Final category stays **`{result.category}`**."
        )
    lines.extend(
        [
            "",
            f"- company / role / person: {result.company or '—'} / {result.role or '—'} / {result.person or '—'}",
            f"- action required: {result.action_required or 'none'}",
            f"- urgency: {result.urgency}",
            f"- unclassified subtype: {result.email_type or '—'}",
            f"- follow-up (wants a reply): {'yes' if result.is_follow_up else 'no'}",
            f"- tracked as an application event: {'yes' if result.is_tracked else 'no'}",
            f"- will scrape posting pages: {'yes' if _should_store_jobs(result, candidates) else 'no'}",
            "",
        ]
    )

    lines.extend(["### Step 4 — follow each posting link (scrape)", ""])
    scraped = {}
    if not _should_store_jobs(result, candidates):
        lines.append("Skipped. This category does not explode into Job rows.")
        lines.append("")
    elif not scrape:
        lines.append("Skipped (`--no-scrape`).")
        lines.append("")
    elif not candidates:
        lines.append("Nothing to fetch.")
        lines.append("")
    else:
        lines.append(
            "Order of attack per URL: board JSON API → LinkedIn guest fragment → "
            "schema.org JobPosting JSON-LD → readable HTML → optional LLM salvage."
        )
        lines.append("")
        scraped = fetch_all(
            [c for c in candidates if title_worth_scraping(profile, c.title)],
            timeout=settings.scrape_timeout,
        )
        for cand in candidates:
            page = scraped.get(cand.url_key)
            if page is None:
                if not title_worth_scraping(profile, cand.title):
                    lines.extend(
                        [
                            f"**{cand.title or cand.url_key}** — skipped page scrape (title not related)",
                            "",
                        ]
                    )
                continue
            if not page.ok:
                rescued = llm_extract(
                    "\n".join(
                        filter(
                            None,
                            [cand.title, cand.company, cand.location, cand.context, page.description],
                        )
                    ),
                    llm,
                    cand,
                )
                if rescued is not None:
                    page = rescued
            extra = [s.name for s in profile.skills]
            fields = enrich_fields(
                source=cand.source,
                url=cand.url,
                url_key=cand.url_key,
                location=page.location or cand.location,
                description=page.description or cand.context,
                extra_skills=extra,
            )
            title = page.title or cand.title
            company = page.company or cand.company
            location = page.location or cand.location
            description = page.description or cand.context
            match = match_job(profile, title, description, location, company)
            keep = not match.rejected and match.score >= settings.min_job_score
            lines.extend(
                [
                    f"#### {title or cand.url_key}",
                    f"- fetch: **{page.status}** via `{page.extraction or 'none'}`",
                    f"- company / location / state: {company or '—'} / {location or '—'} / {page.state or fields.state or '—'}",
                    f"- posted: {page.posted_at or fields.posted_at or '—'} · type: {page.employment_type or fields.employment_type or '—'}",
                    f"- salary: {page.salary or fields.salary or '—'} · visa: {page.visa_sponsorship or fields.visa_sponsorship or '—'}",
                    f"- experience: {page.experience_required or fields.experience_required or '—'}",
                    f"- skills on the posting: {page.required_skills or fields.required_skills or '—'}",
                    f"- source type / posting id: {page.source_type or fields.source_type or '—'} / `{page.posting_id or fields.posting_id}`",
                    f"- match score: **{match.score:.2f}** → {'KEEP as new' if keep else 'KEEP as ignored'}"
                    + (f" ({match.verdict})" if match.verdict else ""),
                    f"- description length: {len(description)} chars",
                    "",
                    _md_block(description),
                    "",
                ]
            )

    lines.extend(
        [
            "### Step 5 — what would be stored (not written by inspect)",
            "",
            f"- `Message.category` = `{result.category}`"
            + (f" / `email_type` = `{result.email_type}`" if result.email_type else ""),
            f"- Job rows: {len(candidates) if _should_store_jobs(result, candidates) else 0}",
            f"- Outreach row: {'yes' if result.is_follow_up else 'no'}",
            f"- Application event: {'yes' if result.is_tracked else 'no'}",
            f"- LLM used for classify: {'yes' if used_llm else 'no'}"
            + (f" (`{llm.last_used}`)" if used_llm and llm.last_used else ""),
            "",
            "---",
            "",
        ]
    )
    return lines


def run_inspect(
    days: int = 1,
    max_messages: int = 6,
    message_id: str = "",
    scrape: bool = True,
    fixture_only: bool = False,
) -> Path:
    settings = get_settings()
    profile = get_profile()
    llm = LLM(settings)
    TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)

    header = [
        "# Inbox Job Agent — extraction inspect",
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.",
        "This file is temporary. Delete it (and `app/inspect_run.py`) once the pipeline looks right.",
        "",
        "## How the pipeline is wired",
        "",
        "1. **Parse** — Gmail MIME → plain text + HTML + links.",
        "2. **Extract links** — unwrap trackers, keep posting URLs, guess title/company from the digest.",
        "3. **Classify** — regex rules first. An LLM only runs when the guess is weak, or the mail looks like recruiter / other. Job alerts never spend LLM tokens.",
        "4. **Scrape** — each kept URL is fetched (API / JSON-LD / HTML). Extra columns (salary, posted date, visa, employment type, state) are mined from that page text.",
        "5. **Match** — score vs `config/profile.yaml`. Matches stay `new`; the rest stay `ignored` so you can still see that scraping worked.",
        "",
        "## Which AI is used",
        "",
        f"- Classify chain: `{llm.describe(CLASSIFY)}`",
        f"- Extract (scrape salvage) chain: `{llm.describe(EXTRACT)}`",
        "",
        "Recommendation for this project (cheap, fast, good enough for short JSON):",
        "",
        "- **Best default:** Gemini 2.0 Flash — free tier, JSON mode, already the default in `.env.example`.",
        "- **Best high-volume classify backup:** Groq `llama-3.3-70b-versatile` — faster than Gemini when the quota is open.",
        "- **Leave extract on Gemini or NVIDIA** — those calls are rare and the prompt is long.",
        "- **Set `LLM_PROVIDER=none`** if you want rules-only while you watch the inspect output. You can turn a key on later.",
        "",
        f"Profile titles: {', '.join(profile.target_titles) or '(none)'}",
        f"Excluded titles: {', '.join(profile.exclude_titles) or '(none)'}",
        f"`MIN_JOB_SCORE` = {settings.min_job_score}",
        "",
        "---",
        "",
    ]

    sections = _trace_email(_fixture_email(), llm, scrape=scrape, label="Worked example (bundled LinkedIn fixture)")
    if scrape:
        sections.extend(_trace_public_posting())

    live_note = ""
    if fixture_only:
        live_note = "Live Gmail skipped (`--fixture-only`)."
    else:
        live = _load_live_emails(days, max_messages, message_id)
        if isinstance(live, str):
            live_note = live
        elif not live:
            live_note = "Gmail returned no messages in this window."
        else:
            live_note = f"Tracing {len(live)} live message(s)."
            for i, email in enumerate(live, 1):
                sections.extend(
                    _trace_email(email, llm, scrape=scrape, label=f"Live email {i}/{len(live)}")
                )

    header.extend([f"## Live inbox", "", live_note, "", "---", ""])
    TRACE_PATH.write_text("\n".join(header + sections), encoding="utf-8")
    return TRACE_PATH


def _trace_public_posting() -> list[str]:
    """A known-good public Greenhouse job so you can see a real page fetch, not a fake fixture URL."""
    candidate = JobCandidate(
        url="https://boards.greenhouse.io/embed/job_app?for=stripe&token=7532733",
        url_key="greenhouse:7532733",
        source="greenhouse",
        title="(from page)",
    )
    job = fetch_job(candidate)
    extra = enrich_fields(
        source=candidate.source,
        url=candidate.url,
        url_key=candidate.url_key,
        location=job.location,
        description=job.description,
    )
    job.apply_fields(extra)
    return [
        "## Worked example (live Greenhouse page — Stripe #7532733)",
        "",
        "This is not from your inbox. It proves the scrape chain against a real career-site posting.",
        "",
        f"- fetch: **{job.status}** via `{job.extraction or 'none'}`",
        f"- title: {job.title or '—'}",
        f"- company / location / state: {job.company or '—'} / {job.location or '—'} / {job.state or '—'}",
        f"- posted: {job.posted_at or '—'} · type: {job.employment_type or '—'}",
        f"- salary: {job.salary or '—'} · visa: {job.visa_sponsorship or '—'}",
        f"- experience: {job.experience_required or '—'}",
        f"- skills on the posting: {job.required_skills or '—'}",
        f"- source type / posting id: {job.source_type or '—'} / `{job.posting_id}`",
        f"- description length: {len(job.description)} chars",
        "",
        _md_block(job.description),
        "",
        "---",
        "",
    ]
