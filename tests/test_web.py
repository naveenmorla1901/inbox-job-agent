import base64
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import db
from app.config import get_settings
from app.gmail_client import parse_gmail_push
from app.llm import LLM
from app.main import app
from app.models import Job, Message
from app.pipeline import STATE_CURSOR, process_email
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


def test_inbox_page_replaces_swagger(client):
    test_client, _engine = client
    response = test_client.get("/activity")
    assert response.status_code == 200
    assert b"Check new mail" in response.content
    assert b"Start fresh" in response.content
    assert b"/api/docs" not in response.content


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
