import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app import db, pipeline
from app.applications import create_from_job, extract_company_role, normalise_company
from app.classify import Classification, classify_rules
from app.config import get_profile, get_settings
from app.llm import LLM
from app.models import Application, ApplicationEvent, Job, Message
from tests.test_classify import email as plain_email

PROFILE = get_profile()


@pytest.fixture()
def session(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    db.enforce_sqlite_foreign_keys(engine)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(get_settings(), "llm_provider", "none", raising=False)
    with Session(engine) as s:
        yield s


_counter = iter(range(1, 10_000))


def feed(session, sender, subject, body, name="Talent Team"):
    mail = plain_email(sender, subject, body, name=name)
    mail.id = f"msg-{next(_counter)}"
    outcome = pipeline.process_email(session, mail, LLM())
    session.commit()
    return outcome


def saved_job(session, **fields):
    """A job always arrives via a message, and the foreign key is enforced here as in Postgres."""
    session.add(Message(id="m", subject="Job alert"))
    session.flush()
    job = Job(
        message_id="m",
        url="https://example.com/job/1",
        url_key="x:1",
        title="AI Engineer",
        company="Globex",
        dupe_key="globex|ai engineer",
        **fields,
    )
    session.add(job)
    session.commit()
    return job


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
    job = saved_job(session)

    application = create_from_job(session, job)
    session.commit()
    assert application.status == "applied"
    assert application.job_id == job.id
    assert session.exec(select(ApplicationEvent)).one().kind == "applied"


def test_later_email_links_back_to_the_saved_posting(session):
    job = saved_job(session)

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


def test_applicant_tracking_domain_never_becomes_the_company(session):
    """Workday-hosted mail from two employers used to collapse into one application."""
    feed(
        session,
        "oneok@myworkday.com",
        "Thank you for applying to ONEOK!",
        "We received your application and will be in touch.",
        name="ONEOK Careers",
    )
    feed(
        session,
        "adobe@myworkday.com",
        "Thanks for Applying to Adobe",
        "Your application has been received.",
        name="Adobe Talent Acquisition",
    )
    companies = {a.company for a in session.exec(select(Application)).all()}
    assert companies == {"ONEOK", "Adobe"}


def test_company_is_not_mistaken_for_the_role():
    mail = plain_email(
        "oneok@myworkday.com",
        "Thank you for applying to ONEOK!",
        "We received your application.",
        name="ONEOK Careers",
    )
    company, role = extract_company_role(mail, Classification())
    assert company == "ONEOK"
    assert role == ""


def test_company_falls_back_to_the_address_when_there_is_no_display_name():
    mail = plain_email(
        "qualcomm@myworkday.com",
        "Thank You for Your Application!",
        "We have received your application.",
        name="qualcomm@myworkday.com",
    )
    company, _ = extract_company_role(mail, Classification())
    assert company == "Qualcomm"


def test_your_own_name_is_not_part_of_the_company():
    mail = plain_email(
        "no-reply@notion.so",
        f"Update on your application to Notion, {get_profile().name.split()[0]}",
        "Thanks for your interest in Notion.",
        name="Notion Recruiting",
    )
    company, _ = extract_company_role(mail, Classification())
    assert company == "Notion"


def test_stray_phrases_never_become_a_company():
    mail = plain_email(
        "noreply@brightpath.com",
        "Application update",
        "We are not moving forward with your application at the moment.",
        name="Recruiting Team",
    )
    company, role = extract_company_role(mail, Classification())
    assert normalise_company(company) not in {"the moment", "moment"}
    assert role == ""


def test_longer_company_name_joins_the_existing_application(session):
    feed(
        session,
        "careers@iibhs.org",
        "Thank you for applying to Data Engineer at Insurance Institute",
        "We received your application.",
    )
    feed(
        session,
        "careers@iibhs.org",
        "Update on your application at Insurance Institute for Business & Home Safety",
        "Your application is under review.",
    )
    assert len(session.exec(select(Application)).all()) == 1


def test_role_and_company_split_on_ats_reference_subjects():
    mail = plain_email(
        "dlapiper@myworkday.com",
        "Your R2026-2851 Data Scientist application at DLA Piper LLP (US)",
        "Thank you for your interest.",
        name="DLA Piper Careers",
    )
    company, role = extract_company_role(mail, Classification())
    assert company.startswith("DLA Piper")
    assert "Data Scientist" in role


def test_classification_still_drives_the_status(session):
    mail = plain_email("no-reply@x.com", "Your coding assessment", "Complete the online assessment")
    assert classify_rules(mail, PROFILE).category == "assessment"
