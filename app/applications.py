from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, col, select

from .classify import (
    APPLICATION_UPDATE,
    ASSESSMENT,
    INTERVIEW,
    NEXT_STEP,
    OFFER,
    RECRUITER,
    REJECTION,
    SUBMITTED_RE,
    Classification,
)
from .config import get_profile
from .email_parse import ParsedEmail
from .models import Application, ApplicationEvent, Job, as_utc, utcnow

# Later stages win; an application never walks backwards on its own.
STATUS_RANK = {
    "applied": 1,
    "in_review": 2,
    "next_step": 3,
    "assessment": 4,
    "interview": 5,
    "offer": 6,
    "rejected": 7,
    "withdrawn": 7,
}
CATEGORY_STATUS = {
    APPLICATION_UPDATE: "in_review",
    NEXT_STEP: "next_step",
    ASSESSMENT: "assessment",
    INTERVIEW: "interview",
    OFFER: "offer",
    REJECTION: "rejected",
    RECRUITER: "applied",
}
CLOSED_STATUSES = {"rejected", "offer", "withdrawn"}

# "Thank you for applying to Data Scientist at Acme", "Your application for Data Analyst - Acme"
SUBJECT_PATTERNS = (
    re.compile(r"thank you for (?:applying|your application)(?: to| for)?\s+(?P<role>[^-–—|]+?)\s+(?:at|@|with)\s+(?P<company>[^-–—|]+)$", re.I),
    re.compile(r"your application (?:to|for)\s+(?P<role>[^-–—|]+?)\s+(?:at|@|with)\s+(?P<company>[^-–—|]+)$", re.I),
    re.compile(r"application (?:received|submitted|update)(?: for| to)?\s*[:\-–]?\s*(?P<role>[^-–—|]+?)\s+(?:at|@|with)\s+(?P<company>[^-–—|]+)$", re.I),
    re.compile(r"(?P<company>[^-–—|]+?)\s*[-–—|]\s*(?:application|update|interview|assessment)(?:\s+for)?\s*[:\-–]?\s*(?P<role>[^-–—|]+)$", re.I),
    # "Your R2026-2851 Data Scientist application at DLA Piper LLP (US)"
    re.compile(r"(?P<role>[^|]{3,60}?)\s+application\s+(?:at|with)\s+(?P<company>[^-–—|]+)$", re.I),
)
BODY_PATTERNS = (
    re.compile(r"your application (?:to|for) (?:the )?(?P<role>[^.\n]{3,60}?) (?:position |role )?(?:at|with) (?P<company>[^.\n,]{2,60})", re.I),
    re.compile(r"applied (?:to|for) (?:the )?(?P<role>[^.\n]{3,60}?) (?:position |role )?(?:at|with) (?P<company>[^.\n,]{2,60})", re.I),
    re.compile(r"interest in (?:the )?(?P<role>[^.\n]{3,60}?) (?:position|role|opening) (?:at|with) (?P<company>[^.\n,]{2,60})", re.I),
)
COMPANY_ONLY_PATTERNS = (
    re.compile(r"(?:update on|regarding|status of) your application (?:at|with|to|for)\s+(?P<company>[^-–—|]+)$", re.I),
    re.compile(r"your application (?:at|with)\s+(?P<company>[^-–—|]+)$", re.I),
    # "Thanks for applying to Adobe" - what follows is the employer, not the role.
    re.compile(r"thank(?:s| you)?(?: very much)? for (?:applying|your application|your interest)(?:\s+(?:to|at|with|in))\s+(?P<company>[^-–—|!?.]+)", re.I),
    re.compile(r"^(?:applying|application) (?:to|at|with)\s+(?P<company>[^-–—|!?.]+)", re.I),
)
# A captured company often runs into the rest of the sentence ("Acme is moving forward").
COMPANY_TAIL = re.compile(
    r"\s+\b(is|are|was|were|has|have|had|and|we|you|they|to|for|which|that|the|it|"
    r"team|careers?|recruiting|talent|regarding|about|after|since|will|would)\b.*$",
    re.I,
)

ROLE_ONLY = (
    re.compile(r"(?:application|applying|interview|assessment)(?: for| to)(?: the)? (?P<role>[^.\n,]{3,60}?)(?: position| role)?[.\n,]", re.I),
)

COMPANY_NOISE = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|co|gmbh|plc|pvt|private|limited|technologies|"
    r"technology|solutions|systems|labs|group|team|careers|talent|recruiting|hr)\b",
    re.I,
)
ROLE_NOISE = re.compile(
    r"\b(senior|sr|junior|jr|lead|principal|staff|i{1,3}|iv|v|1|2|3|entry|level|"
    r"remote|hybrid|onsite|full|part|time|contract|intern|internship)\b",
    re.I,
)
GENERIC_COMPANY = {
    "gmail", "google", "linkedin", "indeed", "greenhouse", "lever", "workday", "myworkday",
    "ashby", "smartrecruiters", "icims", "taleo", "jobvite", "notification", "notifications",
    "no-reply", "noreply", "mail", "email", "careers", "jobs", "talent", "hire", "hiring",
    # Applicant tracking vendors. Their domain is on mail from hundreds of employers, so
    # treating it as the company merges unrelated applications into one.
    "myworkdayjobs", "myworkdaysite", "workdayjobs", "ashbyhq", "appcast", "acquiretm",
    "talentacquisition", "mailsuite", "ziprecruiter", "oraclecloud", "successfactors",
    "brassring", "avature", "phenompeople", "eightfold", "paylocity", "bamboohr", "dayforce",
    "ceridian", "ultipro", "jazzhr", "breezy", "teamtailor", "recruitee", "workable", "adp",
    "candidate", "candidates", "recruiting", "recruitment", "applicant", "applications",
    "sendgrid", "mailgun", "mandrill", "salesforce", "hrsmart", "silkroad", "clearcompany",
}
# Display names arrive as "ONEOK Careers" or "Adobe Talent Acquisition".
SENDER_NAME_NOISE = re.compile(
    r"\b(careers?|talent|acquisition|recruit(?:ing|ment|er)?|hr|human resources|team|"
    r"hiring|jobs?|no[- ]?reply|do[- ]?not[- ]?reply|notifications?|via|support|admin|"
    r"applications?|candidate|people|staffing)\b",
    re.I,
)
TRAILING_PUNCT = " -–—|!?.,:;\"'“”‘’()"
# A capture that starts with a preposition took the phrase, not the name: "at MANTECH".
COMPANY_LEAD = re.compile(r"^(?:at|with|to|for|from|in|the)\b[\s:,-]*", re.I)
# Phrases that survive the tail trimming but name nothing.
COMPANY_STOPWORDS = {
    "moment", "this time", "that time", "present", "now", "today", "future", "time",
    "our company", "company", "us", "them", "position", "role", "opportunity",
}
ROLE_STOPWORDS = {
    "thank", "thanks", "job", "jobs", "position", "role", "application", "applications",
    "opportunity", "opening", "career", "careers", "update", "your application", "it",
}
# "Your R2026-2851 Data Scientist" -> "Data Scientist"
ROLE_LEAD = re.compile(r"^(?:your|the|our|a|an|re|fw|fwd)\b[\s:,-]*", re.I)
REQ_ID = re.compile(r"\b(?:req(?:uisition)?[\s#:-]*)?[A-Z]{0,3}[-_]?\d{3,}[-_]?\d*\b")


def strip_salutation(value: str) -> str:
    """"Update on your application to Notion, Naveen" ends with your own name, not the company."""
    first_name = (get_profile().name or "").split()[:1]
    if not first_name or not value:
        return value
    return re.sub(rf",?\s+{re.escape(first_name[0])}\b.*$", "", value, flags=re.I)


def trim_fragment(value: str) -> str:
    """Subject lines truncate mid-bracket: 'DLA Piper LLP (US' keeps a dangling group."""
    value = (value or "").strip(TRAILING_PUNCT)
    if value.count("(") > value.count(")"):
        value = value[: value.rindex("(")]
    return value.strip(TRAILING_PUNCT)


def normalise_company(value: str) -> str:
    value = re.sub(r"[^a-z0-9 ]", " ", (value or "").lower())
    value = COMPANY_NOISE.sub(" ", value)
    return " ".join(value.split())


def normalise_role(value: str) -> str:
    value = re.sub(r"[^a-z0-9 ]", " ", (value or "").lower())
    value = ROLE_NOISE.sub(" ", value)
    return " ".join(value.split())


def match_key(company: str, role: str) -> str:
    return f"{normalise_company(company)}|{normalise_role(role)}"


def dupe_key(company: str, title: str) -> str:
    company_key, role_key = normalise_company(company), normalise_role(title)
    return f"{company_key}|{role_key}" if company_key and role_key else ""


def extract_company_role(email: ParsedEmail, result: Classification) -> tuple[str, str]:
    """Best effort company + role for an application email.

    What the email *says* beats what the classifier guessed: a Greenhouse-hosted mail about a
    Northwind role has "greenhouse.io" in the sender and "Northwind Labs" in the subject.
    """
    company = role = ""

    subject = (email.subject or "").strip()
    for pattern in SUBJECT_PATTERNS:
        match = pattern.search(subject)
        if match:
            role = match.group("role").strip()
            company = match.group("company").strip()
            break

    if not company:
        for pattern in COMPANY_ONLY_PATTERNS:
            match = pattern.search(subject)
            if match:
                company = match.group("company").strip()
                break

    if not (company and role):
        body = email.body(4000)
        for pattern in BODY_PATTERNS:
            match = pattern.search(body)
            if match:
                role = role or match.group("role").strip()
                company = company or match.group("company").strip()
                break

    company = strip_salutation(COMPANY_TAIL.sub("", company)).strip(TRAILING_PUNCT)
    role = strip_salutation(COMPANY_TAIL.sub("", role)).strip(TRAILING_PUNCT)

    company = company or (result.company or "").strip(TRAILING_PUNCT)
    role = role or (result.role or "").strip(TRAILING_PUNCT)

    if not role:
        for pattern in ROLE_ONLY:
            match = pattern.search(f"{subject}\n{email.body(2000)}")
            if match:
                role = match.group("role").strip(TRAILING_PUNCT)
                break

    # "Thanks for applying to ONEOK" reads as a role to the role-only patterns.
    if role and normalise_role(role) == normalise_role(company):
        role = ""

    company = COMPANY_LEAD.sub("", company)
    if is_generic_company(company):
        company = sender_company(email)

    role = ROLE_LEAD.sub("", role)
    role = " ".join(REQ_ID.sub(" ", role).split())
    if normalise_role(role) in ROLE_STOPWORDS or role.lower() == (get_profile().name or "").lower():
        role = ""

    return trim_fragment(company[:150]), trim_fragment(role[:150])


def is_generic_company(value: str) -> bool:
    key = normalise_company(value)
    return (
        not key
        or key in GENERIC_COMPANY
        or key.replace(" ", "") in GENERIC_COMPANY
        or key in COMPANY_STOPWORDS
    )


def clean_name_part(value: str) -> str:
    value = SENDER_NAME_NOISE.sub(" ", value or "")
    return " ".join(re.sub(r"[^\w &/'-]", " ", value).split())


def sender_company(email: ParsedEmail) -> str:
    """Fall back to the From header: the display name names the employer far more often
    than the domain, which usually belongs to the tracking system."""
    name = email.sender_name or ""
    if "@" in name:  # no display name, just the address echoed back
        name = name.split("@", 1)[0]
    candidate = clean_name_part(name)
    if candidate and not is_generic_company(candidate):
        return candidate.title() if candidate.islower() else candidate

    # "qualcomm@myworkday.com": the local part names the employer, the domain names the vendor.
    local = clean_name_part((email.sender_email or "").split("@", 1)[0].replace(".", " "))
    if local and not is_generic_company(local):
        return local.title() if local.islower() else local

    domain = email.sender_domain.split(".")
    guess = domain[-2] if len(domain) >= 2 else (domain[0] if domain else "")
    return guess.replace("-", " ").title() if guess and not is_generic_company(guess) else ""


def same_company(left: str, right: str) -> bool:
    """True when one normalised name is a whole-word prefix of the other."""
    if not left or not right or left == right:
        return bool(left and left == right)
    short, long = sorted((left.split(), right.split()), key=len)
    return bool(short) and long[: len(short)] == short


def find_application(session: Session, company: str, role: str) -> Application | None:
    """Match on company + role, then fall back to the newest open application at that company."""
    company_key = normalise_company(company)
    if not company_key:
        return None

    if role:
        exact = session.exec(
            select(Application).where(Application.match_key == match_key(company, role))
        ).first()
        if exact:
            return exact

    candidates = session.exec(
        select(Application)
        .where(Application.match_key.startswith(f"{company_key}|"))  # type: ignore[attr-defined]
        .order_by(col(Application.last_event_at).desc())
    ).all()
    if not candidates:
        # One mail says "Insurance Institute", the next says "Insurance Institute for Business
        # & Home Safety". Treat a whole-word prefix of the other as the same employer.
        candidates = [
            application
            for application in session.exec(
                select(Application).order_by(col(Application.last_event_at).desc()).limit(500)
            ).all()
            if same_company(company_key, application.match_key.split("|", 1)[0])
        ]
    if not candidates:
        return None

    role_key = normalise_role(role)
    if role_key:
        for candidate in candidates:
            candidate_role = candidate.match_key.split("|", 1)[1]
            if candidate_role and (candidate_role in role_key or role_key in candidate_role):
                return candidate
    open_ones = [c for c in candidates if not c.closed]
    return open_ones[0] if open_ones else candidates[0]


def advance(application: Application, status: str) -> bool:
    """Move an application forward. Returns True when the status actually changed."""
    current = STATUS_RANK.get(application.status, 0)
    incoming = STATUS_RANK.get(status, 0)
    if incoming <= current:
        return False
    application.status = status
    application.closed = status in CLOSED_STATUSES
    return True


def link_job(session: Session, application: Application, company: str, role: str) -> None:
    """Attach the saved posting this application came from, when we have it."""
    if application.job_id:
        return
    key = dupe_key(company, role)
    if not key:
        return
    job = session.exec(
        select(Job).where(Job.dupe_key == key).order_by(col(Job.received_at).desc())
    ).first()
    if job:
        application.job_id = job.id
        application.job_url = job.url
        application.source = application.source or job.source
        if job.status != "applied":
            job.status = "applied"
            session.add(job)


def record_email(
    session: Session, email: ParsedEmail, result: Classification
) -> tuple[Application, bool] | None:
    """Create or update the application this email belongs to, and log a timeline event."""
    status = CATEGORY_STATUS.get(result.category)
    if status is None:
        return None

    # "We received your application" is the application starting, not a review of it.
    kind = result.category
    if result.category == APPLICATION_UPDATE and SUBMITTED_RE.search(
        f"{email.subject}\n{email.body(2000)}"
    ):
        status, kind = "applied", "submitted"

    company, role = extract_company_role(email, result)
    if not normalise_company(company):
        return None

    # A cold recruiter pitch is not an application: only attach it to one that already exists.
    application = find_application(session, company, role)
    if application is None:
        if result.category == RECRUITER:
            return None
        application = Application(
            company=company,
            role=role,
            match_key=match_key(company, role),
            status="applied",
            applied_at=email.received_at,
            last_event_at=email.received_at,
        )
        session.add(application)
        session.flush()

    changed = advance(application, status)
    if not application.role and role:
        application.role = role
        application.match_key = match_key(application.company, role)
    if as_utc(email.received_at) >= as_utc(application.last_event_at):
        application.last_event_at = email.received_at
        application.last_event = kind
        application.next_action = result.action_required or application.next_action
    link_job(session, application, application.company, application.role or role)

    session.add(
        ApplicationEvent(
            application_id=application.id,
            kind=kind,
            subject=email.subject[:300],
            summary=(result.summary or email.snippet)[:800],
            message_id=email.id,
            gmail_link=email.gmail_link,
            occurred_at=email.received_at,
        )
    )
    session.add(application)
    return application, changed


def create_from_job(session: Session, job: Job) -> Application:
    """Manual path: you hit 'applied' on a posting in the dashboard."""
    existing = find_application(session, job.company, job.title)
    if existing is not None and not existing.closed:
        existing.job_id = existing.job_id or job.id
        existing.job_url = existing.job_url or job.url
        session.add(existing)
        return existing

    application = Application(
        company=job.company,
        role=job.title,
        match_key=match_key(job.company, job.title),
        source=job.source,
        status="applied",
        job_id=job.id,
        job_url=job.url,
        applied_at=utcnow(),
        last_event_at=utcnow(),
        last_event="applied",
    )
    session.add(application)
    session.flush()
    session.add(
        ApplicationEvent(
            application_id=application.id,
            kind="applied",
            subject=f"Marked applied: {job.title}",
            summary=f"{job.title} at {job.company}",
            occurred_at=utcnow(),
        )
    )
    return application


def stale_applications(session: Session, days: int = 14) -> list[Application]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return session.exec(
        select(Application)
        .where(Application.closed == False, Application.last_event_at < cutoff)  # noqa: E712
        .order_by(col(Application.last_event_at))
    ).all()
