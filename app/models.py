from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Column, Field, SQLModel, Text


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; make them comparable with fresh aware ones."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Message(SQLModel, table=True):
    """One processed Gmail message."""

    id: str = Field(primary_key=True)  # Gmail message id
    thread_id: str = ""
    sender: str = ""
    sender_email: str = Field(default="", index=True)
    subject: str = ""
    snippet: str = ""
    received_at: datetime = Field(default_factory=utcnow, index=True)
    category: str = Field(default="other", index=True)
    confidence: float = 0.0
    reason: str = ""
    summary: str = Field(default="", sa_column=Column(Text))
    jobs_found: int = 0
    jobs_matched: int = 0
    processed_at: datetime = Field(default_factory=utcnow)


class Job(SQLModel, table=True):
    """A single posting pulled out of a job-alert email."""

    id: int | None = Field(default=None, primary_key=True)
    message_id: str = Field(index=True, foreign_key="message.id")
    url: str = Field(default="", sa_column=Column(Text))
    url_key: str = Field(default="", index=True, unique=True)
    title: str = ""
    company: str = Field(default="", index=True)
    location: str = ""
    source: str = ""
    description: str = Field(default="", sa_column=Column(Text))
    score: float = Field(default=0.0, index=True)
    title_score: float = 0.0
    skill_score: float = 0.0
    resume_score: float = 0.0
    matched_skills: str = ""
    missing_skills: str = ""
    verdict: str = ""
    scraped: bool = False
    scrape_status: str = ""  # ok | blocked | empty | error | skipped
    extraction: str = ""  # jsonld | greenhouse | lever | html | llm | email
    matched: bool = Field(default=True, index=True)  # cleared the score threshold
    status: str = Field(default="new", index=True)  # new | saved | applied | ignored
    # Same role re-advertised (another board, another alert): points at the first row we kept.
    dupe_key: str = Field(default="", index=True)  # normalised company|title
    duplicate_of: int | None = Field(default=None, index=True)
    received_at: datetime = Field(default_factory=utcnow, index=True)
    created_at: datetime = Field(default_factory=utcnow)


class Outreach(SQLModel, table=True):
    """A human actually reaching out: recruiter, interview, assessment, offer, rejection."""

    id: int | None = Field(default=None, primary_key=True)
    message_id: str = Field(index=True, foreign_key="message.id")
    kind: str = Field(default="recruiter_outreach", index=True)
    person: str = ""
    person_email: str = ""
    company: str = ""
    role: str = ""
    subject: str = ""
    summary: str = Field(default="", sa_column=Column(Text))
    action_required: str = ""
    urgency: str = Field(default="normal", index=True)
    gmail_link: str = ""
    notified: bool = False
    handled: bool = Field(default=False, index=True)
    received_at: datetime = Field(default_factory=utcnow, index=True)
    created_at: datetime = Field(default_factory=utcnow)


class Application(SQLModel, table=True):
    """One role you applied to, tracked from confirmation through to offer or rejection."""

    id: int | None = Field(default=None, primary_key=True)
    company: str = Field(default="", index=True)
    role: str = ""
    match_key: str = Field(default="", index=True)  # normalised company|role
    source: str = ""  # linkedin | greenhouse | manual | ...
    status: str = Field(default="applied", index=True)
    job_id: int | None = Field(default=None, foreign_key="job.id", index=True)
    job_url: str = Field(default="", sa_column=Column(Text))
    applied_at: datetime = Field(default_factory=utcnow, index=True)
    last_event_at: datetime = Field(default_factory=utcnow, index=True)
    last_event: str = ""
    next_action: str = ""
    closed: bool = Field(default=False, index=True)


class ApplicationEvent(SQLModel, table=True):
    """Timeline entry for an application: the mail that moved it, or a manual action."""

    id: int | None = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id", index=True)
    kind: str = Field(default="update", index=True)
    subject: str = ""
    summary: str = Field(default="", sa_column=Column(Text))
    message_id: str = ""
    gmail_link: str = ""
    occurred_at: datetime = Field(default_factory=utcnow, index=True)


class State(SQLModel, table=True):
    """Tiny key/value table for poll cursors."""

    key: str = Field(primary_key=True)
    value: str = ""
    updated_at: datetime = Field(default_factory=utcnow)
