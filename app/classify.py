from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Profile
from .email_parse import ParsedEmail
from .extract_jobs import JOB_HOSTS
from .llm import LLM

JOB_ALERT = "job_alert"
RECRUITER = "recruiter_outreach"
INTERVIEW = "interview_invite"
ASSESSMENT = "assessment"
OFFER = "offer"
REJECTION = "rejection"
APPLICATION_UPDATE = "application_update"
OTHER = "other"

# Categories worth a push notification, most urgent first.
NOTIFY_KINDS = (OFFER, INTERVIEW, ASSESSMENT, RECRUITER)
OUTREACH_KINDS = (OFFER, INTERVIEW, ASSESSMENT, RECRUITER, REJECTION, APPLICATION_UPDATE)

ALERT_SUBJECT = re.compile(
    r"(job alert|jobs? for you|new jobs?|\d+\s+new\s+(job|opportunit)|"
    r"jobs? matching|recommended for you|your job alert|hiring now|new opportunities)",
    re.I,
)
INTERVIEW_RE = re.compile(
    r"(schedule (an? )?(interview|call|chat)|interview (invitation|request|scheduled|confirm)|"
    r"phone screen|screening call|availability (for|to)|book a time|calendly|"
    r"next (round|steps?) (of|in) (the )?interview|meet with the team|invite you to interview)",
    re.I,
)
ASSESSMENT_RE = re.compile(
    r"(coding (challenge|assessment|test)|online assessment|take[- ]home|hackerrank|codesignal|"
    r"codility|karat|hirevue|technical assessment|skills? test)",
    re.I,
)
OFFER_RE = re.compile(r"(offer letter|pleased to offer|job offer|we are excited to offer)", re.I)
REJECTION_RE = re.compile(
    r"(not (moving|move) forward|unfortunately[^.]{0,80}(not|other candidates)|"
    r"decided to (proceed|move forward) with other|no longer under consideration|"
    r"we will not be|position has been filled|pursuing other candidates)",
    re.I,
)
APPLIED_RE = re.compile(
    r"(thank you for (applying|your (interest|application))|application (was )?(received|submitted)|"
    r"we (have )?received your application|your application (to|for|is)|application status)",
    re.I,
)
RECRUITER_RE = re.compile(
    r"(reaching out|came across your (profile|resume)|i(’|')?m a (technical )?recruiter|"
    r"we have an (urgent )?(opening|requirement|opportunity)|is this (role|position) of interest|"
    r"are you (open|available|interested) (to|for|in)|hotlist|c2c|w2 (only|position)|"
    r"corp to corp|would you be interested|share your (updated )?resume|your candidacy)",
    re.I,
)
URGENT_RE = re.compile(
    r"(today|tomorrow|asap|urgent|by (eod|end of day)|within 24 hours|expires|deadline|"
    r"respond by|last (date|day))",
    re.I,
)
NOREPLY_RE = re.compile(r"(no-?reply|do-?not-?reply|notification|alerts?@|mailer|bounce)", re.I)
MARKETING_RE = re.compile(
    r"(newsletter|webinar|unsubscribe from all|sale ends|% off|course|masterclass|"
    r"upgrade to premium|invoice|receipt|statement|verify your (email|account)|"
    r"security alert|password)",
    re.I,
)

CLASSIFIER_SYSTEM = (
    "You triage a job seeker's inbox. Reply with JSON only, no prose. "
    "Be conservative: only pick recruiter_outreach/interview_invite when a real person or "
    "ATS is addressing this candidate about a specific role."
)
CLASSIFIER_PROMPT = """Classify this email.

Allowed categories:
- job_alert: automated digest listing multiple job postings
- recruiter_outreach: a recruiter/hiring manager contacting the candidate about a role
- interview_invite: interview scheduling, availability request, or confirmed interview
- assessment: coding test / online assessment / take-home
- offer: job offer
- rejection: application declined
- application_update: application received/under review status mail
- other: anything else (newsletters, bills, social, marketing)

Return JSON:
{{"category": "...", "confidence": 0.0-1.0, "company": "", "role": "",
 "person": "", "summary": "one sentence", "action_required": "what the candidate must do, or ''",
 "urgency": "high|normal|low"}}

From: {sender} <{sender_email}>
Subject: {subject}
Body:
{body}
"""


@dataclass
class Classification:
    category: str = OTHER
    confidence: float = 0.0
    reason: str = ""
    summary: str = ""
    company: str = ""
    role: str = ""
    person: str = ""
    action_required: str = ""
    urgency: str = "normal"

    @property
    def is_outreach(self) -> bool:
        return self.category in OUTREACH_KINDS

    @property
    def should_notify(self) -> bool:
        return self.category in NOTIFY_KINDS


def _looks_like_job_board(email: ParsedEmail, profile: Profile) -> bool:
    sender = email.sender_email.lower()
    if sender in {s.lower() for s in profile.job_alert_senders}:
        return True
    domain = email.sender_domain
    return any(domain == d or domain.endswith("." + d) for d in JOB_HOSTS)


def classify_rules(email: ParsedEmail, profile: Profile, job_count: int = 0) -> Classification:
    subject = email.subject or ""
    body = email.body(6000)
    blob = f"{subject}\n{body}"
    automated = bool(NOREPLY_RE.search(email.sender_email))

    if _looks_like_job_board(email, profile) and (ALERT_SUBJECT.search(subject) or job_count >= 2):
        return Classification(JOB_ALERT, 0.95, "job board sender + alert subject")
    if job_count >= 3 and automated:
        return Classification(JOB_ALERT, 0.8, f"{job_count} posting links in automated mail")

    if OFFER_RE.search(blob):
        return Classification(OFFER, 0.9, "offer language")
    if REJECTION_RE.search(blob):
        return Classification(REJECTION, 0.85, "rejection language")
    if ASSESSMENT_RE.search(blob):
        return Classification(ASSESSMENT, 0.85, "assessment language")
    if INTERVIEW_RE.search(blob):
        return Classification(INTERVIEW, 0.85, "interview scheduling language")
    if RECRUITER_RE.search(blob):
        confidence = 0.6 if automated else 0.85
        return Classification(RECRUITER, confidence, "recruiter outreach language")
    if APPLIED_RE.search(blob):
        return Classification(APPLICATION_UPDATE, 0.8, "application acknowledgement")
    if MARKETING_RE.search(blob):
        return Classification(OTHER, 0.7, "marketing/transactional")
    if job_count >= 1 and ALERT_SUBJECT.search(subject):
        return Classification(JOB_ALERT, 0.6, "alert subject with posting link")
    return Classification(OTHER, 0.3, "no rule matched")


def _fill_from_email(result: Classification, email: ParsedEmail) -> Classification:
    if not result.person:
        result.person = email.sender_name
    if not result.summary:
        result.summary = email.snippet[:300]
    if not result.company and email.sender_domain:
        result.company = email.sender_domain.split(".")[0].title()
    if URGENT_RE.search(f"{email.subject} {email.body(2000)}"):
        result.urgency = "high"
    return result


def classify_email(
    email: ParsedEmail, profile: Profile, llm: LLM | None = None, job_count: int = 0
) -> Classification:
    result = classify_rules(email, profile, job_count)

    ambiguous = result.confidence < 0.75 or result.category in (RECRUITER, OTHER)
    if llm and llm.enabled and ambiguous and result.category != JOB_ALERT:
        data = llm.json(
            CLASSIFIER_PROMPT.format(
                sender=email.sender_name,
                sender_email=email.sender_email,
                subject=email.subject,
                body=email.body(6000),
            ),
            CLASSIFIER_SYSTEM,
        )
        category = str(data.get("category", "")).strip().lower()
        if category in (JOB_ALERT, *OUTREACH_KINDS, OTHER):
            result = Classification(
                category=category,
                confidence=float(data.get("confidence") or 0.7),
                reason="llm",
                summary=str(data.get("summary", ""))[:500],
                company=str(data.get("company", ""))[:150],
                role=str(data.get("role", ""))[:150],
                person=str(data.get("person", ""))[:150],
                action_required=str(data.get("action_required", ""))[:300],
                urgency=str(data.get("urgency", "normal")).lower(),
            )
    return _fill_from_email(result, email)
