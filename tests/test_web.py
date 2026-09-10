import base64
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, func, select

from app import db
from app.config import get_settings
from app.gmail_client import parse_gmail_push
from app.llm import LLM
from app.main import app
from app.models import Issue, Job, Message
from app.pipeline import STATE_CURSOR, process_email
from app.timefmt import et_day_label, group_by_et_day
from tests.test_extract import alert_email


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.enforce_sqlite_foreign_keys(engine)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(get_settings(), "api_token", "change-me", raising=False)
    monkeypatch.setattr(get_settings(), "scrape_job_pages", False, raising=False)
    monkeypatch.setattr(get_settings(), "llm_provider", "none", raising=False)
    with TestClient(app) as test_client:
        yield test_client, engine


def test_overview_renders(client):
    test_client, _engine = client
    response = test_client.get("/overview")
    assert response.status_code == 200
    assert b"email" in response.content.lower()


def test_api_docs_is_gone(client):
    test_client, _engine = client
    assert test_client.get("/api/docs").status_code == 404
    assert test_client.get("/openapi.json").status_code == 404


def test_status_page_shows_auto_sync_and_no_manual_buttons(client):
    test_client, _engine = client
    response = test_client.get("/activity")
    assert response.status_code == 200
    body = response.content
    assert b"Auto-sync" in body or b"Poll interval" in body
    assert b"15 minutes" in body or b"min" in body
    assert b"Origin" in body
    # The manual controls were removed in favour of automatic syncing.
    assert b"Check now" not in body
    assert b"Start fresh" not in body
    assert b"extra GitHub copy" not in body


def test_run_page_explains_github_copy_service(client, monkeypatch):
    test_client, _engine = client
    monkeypatch.setenv("K_SERVICE", "inbox-job-agent-git")
    response = test_client.get("/activity")
    assert response.status_code == 200
    assert b"extra GitHub copy" in response.content
    assert b"inbox-job-agent-git" in response.content
    assert b"us-east1" in response.content


def test_mail_page_bundles_jobs_under_the_email(client):
    test_client, engine = client
    with Session(engine) as session:
        process_email(session, alert_email(), LLM())
        session.commit()

    response = test_client.get("/?days=30")
    assert response.status_code == 200
    body = response.content
    assert b"8 new jobs match your preferences" in body or b"jobs match" in body.lower()
    assert b"Data Scientist" in body or b"Machine Learning" in body
    assert b"Mail" in body
    assert b"Raw extract" in body
    assert b"Analysis" in body
    assert b"/api/docs" not in body

    raw = test_client.get("/?days=30&view=raw")
    assert raw.status_code == 200
    assert b"Email text" in raw.content
    assert b"Extracted postings" in raw.content


def test_issues_page_lists_recorded_failures(client):
    from app.issues import record_issue

    test_client, _engine = client
    record_issue("gmail", "Gmail list failed", "429 quota", severity="error")
    record_issue("llm", "No LLM answered extract", "tried groq, gemini", severity="error")
    response = test_client.get("/issues")
    assert response.status_code == 200
    body = response.content
    assert b"Gmail list failed" in body
    assert b"429 quota" in body
    assert b"gmail" in body
    assert b"Live APIs" in body
    assert b"No LLM answered extract" in body
    assert b"LLM" in body
    assert b"LLM_PROVIDER=none" in body


def test_overview_purge_deletes_only_the_selected_window(client):
    from datetime import datetime, timezone

    test_client, engine = client
    inside = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)
    outside = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(Message(id="keep", subject="old", received_at=outside))
        session.add(Message(id="drop", subject="new", received_at=inside))
        session.commit()
        session.add(Job(message_id="keep", url_key="keep-1", title="Old", received_at=outside))
        session.add(Job(message_id="drop", url_key="drop-1", title="New", received_at=inside))
        session.add(Issue(source="gmail", title="old fail", occurred_at=outside))
        session.add(Issue(source="llm", title="new fail", occurred_at=inside))
        session.commit()

    page = test_client.get("/overview?since=2026-09-10&until=2026-09-10")
    assert page.status_code == 200
    assert b"Delete this window" in page.content
    assert b"1 emails" in page.content

    response = test_client.post(
        "/overview/purge",
        data={"days": 1, "since": "2026-09-10", "until": "2026-09-10"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash=" in response.headers["location"]

    with Session(engine) as session:
        assert session.get(Message, "keep") is not None
        assert session.get(Message, "drop") is None
        assert session.exec(select(Job).where(Job.url_key == "keep-1")).first() is not None
        assert session.exec(select(Job).where(Job.url_key == "drop-1")).first() is None
        assert session.exec(select(Issue).where(Issue.title == "old fail")).first() is not None
        assert session.exec(select(Issue).where(Issue.title == "new fail")).first() is None


def test_matches_page_groups_by_day_and_shows_source_mail(client):
    test_client, engine = client
    with Session(engine) as session:
        process_email(session, alert_email(), LLM())
        session.commit()

    response = test_client.get("/matches?days=30&show=all&status=all")
    assert response.status_code == 200
    assert b"from" in response.content
    assert test_client.get("/messages").status_code in (200, 302)


def test_start_fresh_clears_rows_and_sets_cursor(client):
    test_client, engine = client
    with Session(engine) as session:
        process_email(session, alert_email(), LLM())
        session.commit()
        assert session.exec(select(Job)).first() is not None

    response = test_client.post("/activity/reset", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/activity?fresh=1"

    with Session(engine) as session:
        from app.models import State

        assert session.exec(select(Job)).first() is None
        assert session.exec(select(Message)).first() is None
        row = session.get(State, STATE_CURSOR)
        assert row is not None
        assert int(row.value) > 0


def test_parse_gmail_push_reads_history():
    payload = {"emailAddress": "n@example.com", "historyId": "99"}
    body = {
        "message": {
            "data": base64.b64encode(json.dumps(payload).encode()).decode(),
            "messageId": "1",
        }
    }
    assert parse_gmail_push(body) == payload
    assert parse_gmail_push({}) == {}
    assert parse_gmail_push({"message": {}}) == {}


def test_group_by_et_day_keeps_order():
    class Row:
        def __init__(self, when):
            self.received_at = when

    from datetime import datetime, timezone

    a = Row(datetime(2026, 9, 3, 22, 0, tzinfo=timezone.utc))
    b = Row(datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc))
    c = Row(datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc))
    groups = group_by_et_day([a, b, c])
    assert [label for label, _ in groups][0] == et_day_label(a.received_at)
    assert len(groups[0][1]) == 2
    assert len(groups[1][1]) == 1


def test_flag_and_flags_page(client):
    test_client, engine = client
    with Session(engine) as session:
        process_email(session, alert_email(), LLM())
        session.commit()

    flagged = test_client.post(
        "/mail/m1/flag",
        data={"category": "recruiter_outreach", "redirect": "/?m=m1&days=30"},
        follow_redirects=False,
    )
    assert flagged.status_code == 303
    page = test_client.get("/flags")
    assert page.status_code == 200
    assert b"recruiter" in page.content.lower()
    mail = test_client.get("/?m=m1&days=30")
    assert b"Re-extract this email" in mail.content
    assert b"Wrong category" in mail.content


def test_charts_and_overview_funnel_pages(client):
    test_client, _engine = client
    charts = test_client.get("/charts")
    assert charts.status_code == 200
    assert b"Fetched per extract window" in charts.content
    overview = test_client.get("/overview?days=30")
    assert overview.status_code == 200
    assert b"Application funnel" in overview.content
    assert b"Search applications" in overview.content
    issues = test_client.get("/issues")
    assert b"Probe APIs" in issues.content
    apps = test_client.get("/applications")
    assert apps.status_code == 200
    flags = test_client.get("/flags")
    assert flags.status_code == 200
    assert b"Wrong-category flags" in flags.content


def test_reextract_this_email_rewrites_jobs(client):
    test_client, engine = client
    with Session(engine) as session:
        process_email(session, alert_email(), LLM())
        session.commit()
        before = session.exec(select(func.count()).select_from(Job)).one()

    response = test_client.post(
        "/mail/m1/reextract",
        data={"redirect": "/?m=m1&days=30"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(engine) as session:
        after = session.exec(select(func.count()).select_from(Job)).one()
    assert after == before
