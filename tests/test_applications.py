import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app import db, pipeline
from app.applications import create_from_job, extract_company_role, normalise_company
from app.classify import Classification, classify_rules
from app.config import get_profile
from app.llm import LLM
from app.models import Application, ApplicationEvent, Job
from tests.test_classify import email as plain_email

PROFILE = get_profile()


@pytest.fixture()
def session(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    with Session(engine) as s:
        yield s


_counter = iter(range(1, 10_000))


def feed(session, sender, subject, body, name="Talent Team"):
    mail = plain_email(sender, subject, body, name=name)
    mail.id = f"msg-{next(_counter)}"
    outcome = pipeline.process_email(session, mail, LLM())
    session.commit()
    return outcome


def test_confirmation_email_starts_tracking(session):
    outcome = feed(
        session,
        "no-reply@greenhouse.io",
        "Thank you for applying to Data Scientist at Northwind Labs",
        "We have received your application and will review it shortly.",
    )
    application = session.exec(select(Application)).one()
    assert outcome.application is not None
    assert application.company == "Northwind Labs"
    assert application.role == "Data Scientist"
    assert application.status == "in_review"
    assert session.exec(select(ApplicationEvent)).one().kind == "application_update"


def test_pipeline_advances_through_stages_and_never_regresses(session):
    feed(
        session,
        "no-reply@greenhouse.io",
        "Thank you for applying to Data Scientist at Northwind Labs",
        "We received your application.",
    )
    feed(
        session,
        "recruiting@northwind.com",
        "Interview invitation",
        "Your application for Data Scientist at Northwind Labs is moving on. "
        "Please share your availability to schedule an interview.",
    )
    application = session.exec(select(Application)).one()
    assert application.status == "interview"

    # A late-arriving "we received your application" must not drag it back to in_review.
    feed(
        session,
        "no-reply@greenhouse.io",
        "Your application to Data Scientist at Northwind Labs",
        "Application received, thank you for your interest.",
    )
    application = session.exec(select(Application)).one()
    assert application.status == "interview"
    assert len(session.exec(select(ApplicationEvent)).all()) == 3


def test_rejection_closes_the_application(session):
    feed(
        session,
        "no-reply@greenhouse.io",
        "Thank you for applying to Data Scientist at Northwind Labs",
        "We received your application.",
    )
    feed(
        session,
        "no-reply@greenhouse.io",
        "Update on your application at Northwind Labs",
        "Unfortunately we have decided to move forward with other candidates.",
    )
    application = session.exec(select(Application)).one()
    assert application.status == "rejected"
    assert application.closed is True


def test_cold_recruiter_pitch_does_not_create_an_application(session):
    feed(
        session,
        "priya@talentbridge.com",
        "Data Scientist role",
        "I came across your profile and we have an urgent opening. Would you be interested?",
        name="Priya R",
    )
    assert session.exec(select(Application)).all() == []


def test_marking_a_job_applied_creates_a_tracked_application(session):
    job = Job(
        message_id="m",
        url="https://example.com/job/1",
        url_key="x:1",
        title="AI Engineer",
        company="Globex",
        dupe_key="globex|ai engineer",
    )
    session.add(job)
    session.commit()

    application = create_from_job(session, job)
    session.commit()
    assert application.status == "applied"
    assert application.job_id == job.id
    assert session.exec(select(ApplicationEvent)).one().kind == "applied"


def test_later_email_links_back_to_the_saved_posting(session):
    job = Job(
        message_id="m",
        url="https://example.com/job/1",
        url_key="x:1",
        title="AI Engineer",
        company="Globex",
        dupe_key="globex|ai engineer",
    )
    session.add(job)
    session.commit()

    feed(
        session,
        "careers@globex.com",
        "Thank you for applying to AI Engineer at Globex",
        "Your application has been received.",
    )
    application = session.exec(select(Application)).one()
    assert application.job_id == job.id
    assert session.get(Job, job.id).status == "applied"


def test_company_and_role_parsing_from_subjects():
    mail = plain_email(
        "careers@acme.com",
        "Your application for Machine Learning Engineer at Acme Analytics",
        "body",
    )
    company, role = extract_company_role(mail, Classification())
    assert company == "Acme Analytics"
    assert role == "Machine Learning Engineer"


def test_company_normalisation_ignores_legal_suffixes():
    assert normalise_company("Northwind Labs, Inc.") == normalise_company("northwind")


def test_classification_still_drives_the_status(session):
    mail = plain_email("no-reply@x.com", "Your coding assessment", "Complete the online assessment")
    assert classify_rules(mail, PROFILE).category == "assessment"
