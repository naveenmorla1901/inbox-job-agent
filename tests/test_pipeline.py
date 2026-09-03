import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app import db, pipeline
from app.config import get_settings
from app.email_parse import ParsedEmail, extract_links
from app.llm import LLM
from app.models import Job, Message, Outreach
from app.reporting import build_breakdown
from tests.test_extract import alert_email
from tests.test_classify import email as plain_email


@pytest.fixture()
def session(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    db.enforce_sqlite_foreign_keys(engine)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    settings = get_settings()
    monkeypatch.setattr(settings, "scrape_job_pages", False, raising=False)
    monkeypatch.setattr(settings, "llm_provider", "none", raising=False)
    with Session(engine) as s:
        yield s


def test_job_alert_stores_every_posting_and_flags_the_matches(session):
    outcome = pipeline.process_email(session, alert_email(), LLM())
    session.commit()

    assert outcome.classification.category == "job_alert"
    assert outcome.jobs_found == 3

    stored = session.exec(select(Job)).all()
    assert len(stored) == 3, "every posting in the digest is kept, matched or not"
    assert {job.title for job in stored} >= {"Data Scientist", "Machine Learning Engineer"}

    matched = [job for job in stored if job.matched]
    assert matched and all(job.score >= get_settings().min_job_score for job in matched)
    assert all(job.status == "new" for job in matched)
    assert all(job.status == "ignored" for job in stored if not job.matched)
    assert session.get(Message, "m1").jobs_matched == len(matched)
    linkedin = next(job for job in stored if job.source == "linkedin")
    assert linkedin.source_type == "job_board"
    assert linkedin.posting_id
    assert linkedin.state or linkedin.location


def test_skipped_scraping_is_recorded_on_the_row(session):
    pipeline.process_email(session, alert_email(), LLM())
    session.commit()
    assert {job.scrape_status for job in session.exec(select(Job)).all()} == {"skipped"}


def test_same_posting_reattaches_to_a_later_email(session):
    pipeline.process_email(session, alert_email(), LLM())
    session.commit()
    originals = session.exec(select(Job)).all()
    assert originals

    repeat = alert_email()
    repeat.id = "m2"
    outcome = pipeline.process_email(session, repeat, LLM())
    session.commit()

    stored = session.exec(select(Job)).all()
    assert len(stored) == len(originals) * 2
    pointers = [job for job in stored if job.message_id == "m2"]
    assert len(pointers) == len(originals)
    assert all(job.duplicate_of for job in pointers)
    assert {job.url_key for job in originals}.isdisjoint({job.url_key for job in pointers})
    assert len(outcome.jobs) == len(originals)


def test_unrelated_email_titles_are_not_fetched(session, monkeypatch):
    monkeypatch.setattr(get_settings(), "scrape_job_pages", True, raising=False)
    called: list[str] = []

    def fake_fetch_all(candidates, timeout=15, workers=5):
        called.extend(c.title for c in candidates)
        return {}

    monkeypatch.setattr(pipeline, "fetch_all", fake_fetch_all)
    html = """
    <a href="http://komatsu.jobs/job/Human-Resources-Manager/36799-en_US/">Human Resources Manager</a>
    <a href="http://komatsu.jobs/job/AI-Engineer/36800-en_US/">AI Engineer</a>
    """
    mail = ParsedEmail(
        id="skip-scrape",
        sender_email="komatsuam-jobnotification@noreply.jobs2web.com",
        subject="New jobs posted from komatsu.jobs",
        html=html,
        text="Your Job Alert matched the following jobs at komatsu.jobs.",
    )
    outcome = pipeline.process_email(session, mail, LLM())
    session.commit()
    assert "Human Resources Manager" not in called
    assert "AI Engineer" in called
    hr = next(job for job in outcome.jobs if "Human Resources" in job.title)
    assert hr.scrape_status == "skipped"
    assert not hr.matched


def test_breakdown_counts_each_category(session):
    pipeline.process_email(session, alert_email(), LLM())
    pipeline.process_email(
        session,
        plain_email(
            "recruiting@northwind.com",
            "Interview invitation",
            "Please share your availability to schedule an interview this week.",
        ),
        LLM(),
    )
    session.commit()

    report = build_breakdown(session, days=1)
    counts = {key: count for key, _, count in report.categories}
    assert report.messages == 2
    assert counts["job_alert"] == 1
    assert counts["interview_invite"] == 1
    assert report.jobs_found == 3
    assert report.jobs_stored == 3
    assert report.jobs_matched >= 1


def test_recruiter_mail_becomes_an_outreach_row(session):
    mail = plain_email(
        "priya@talentbridge.com",
        "Data Scientist opening",
        "Hi, I came across your profile and would you be interested in this role? "
        "Please share your updated resume today.",
        name="Priya R",
    )
    outcome = pipeline.process_email(session, mail, LLM())
    session.commit()

    assert outcome.outreach is not None
    assert outcome.classification.should_notify
    item = session.exec(select(Outreach)).one()
    assert item.kind == "recruiter_outreach"
    assert item.person == "Priya R"
    assert item.urgency == "high"  # "today" in the body


def test_confirmation_with_similar_jobs_stores_both(session):
    html = """
    <p>Your application was sent to Acme.</p>
    <a href="https://www.linkedin.com/jobs/view/3901234567">Data Scientist</a>
    <div>Acme Analytics · Remote</div>
    <a href="https://www.linkedin.com/jobs/view/3907654321">Machine Learning Engineer</a>
    <div>Northwind Labs · Austin, TX</div>
    """
    mail = ParsedEmail(
        id="confirm-similar",
        sender_name="LinkedIn",
        sender_email="jobs-noreply@linkedin.com",
        subject="Naveen, your application was sent to Acme",
        html=html,
        text="Your application was sent to Acme.",
        links=extract_links(html),
    )
    outcome = pipeline.process_email(session, mail, LLM())
    session.commit()
    assert outcome.classification.category == "application_update"
    assert outcome.application is not None
    assert outcome.outreach is None
    assert len(outcome.jobs) == 2


def test_application_receipt_is_tracked_but_not_a_follow_up(session):
    mail = plain_email(
        "no-reply@greenhouse.io",
        "Thank you for applying to Data Scientist at Northwind Labs",
        "We have received your application and will review it shortly.",
    )
    mail.id = "receipt-1"
    outcome = pipeline.process_email(session, mail, LLM())
    session.commit()

    assert outcome.application is not None
    assert outcome.outreach is None
    assert session.exec(select(Outreach)).all() == []


def test_refresh_promotes_a_video_round_onto_follow_ups(session):
    receipt = plain_email(
        "no-reply@hirevue.com",
        "Thanks for applying",
        "We have received your application.",
        name="Acme Talent",
    )
    receipt.id = "video-1"
    pipeline.process_email(session, receipt, LLM())
    session.commit()
    assert session.exec(select(Outreach)).all() == []

    video = plain_email(
        "no-reply@hirevue.com",
        "Please record a short video",
        "Your next step is a one-way video interview. Record a video answering three questions.",
        name="Acme Talent",
    )
    video.id = "video-1"
    outcome = pipeline.reclassify_email(session, video, LLM())
    session.commit()
    assert outcome.classification.category == "next_step"
    assert outcome.outreach is not None
    assert session.exec(select(Outreach)).one().kind == "next_step"


def test_refresh_demotes_a_receipt_off_follow_ups(session):
    video = plain_email(
        "careers@sharkninja.com",
        "Please record a short video",
        "Record a video for the next step in the interview process.",
    )
    video.id = "demote-1"
    pipeline.process_email(session, video, LLM())
    session.commit()
    assert session.exec(select(Outreach)).one().kind == "next_step"

    receipt = plain_email(
        "careers@sharkninja.com",
        "Thank you for applying to SharkNinja",
        "We've received your application. What's next? We will review it and get back to you.",
    )
    receipt.id = "demote-1"
    outcome = pipeline.reclassify_email(session, receipt, LLM())
    session.commit()
    assert outcome.outreach is None
    assert session.exec(select(Outreach)).one().kind == "application_update"
