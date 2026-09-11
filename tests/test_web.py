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
from app.models import Application, ExtractMiss, Issue, Job, Message, Outreach
from app.pipeline import STATE_CURSOR, process_email
from app.timefmt import et_day_label, group_by_et_day
from tests.test_extract import alert_email


@pytest.fixture()
def client(monkeypatch, tmp_path):
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
    monkeypatch.setattr("app.extract_miss.miss_dir", lambda: tmp_path / "extract-misses")
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
    assert b"Poll interval" in body
    assert b"min" in body
    assert b"Poll start" in body
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


def test_cache_tab_clears_hour_window_not_overview(client):
    from datetime import datetime, timezone

    test_client, engine = client
    inside = datetime(2026, 9, 10, 18, 30, tzinfo=timezone.utc)  # 2:30pm ET
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

    overview = test_client.get("/overview?since=2026-09-10&until=2026-09-10")
    assert overview.status_code == 200
    assert b"Delete this window" not in overview.content
    assert b"Clear cache" in overview.content

    page = test_client.get("/cache?scope=range&start_at=2026-09-10T14:00&end_at=2026-09-10T15:00")
    assert page.status_code == 200
    assert b"Clear cache" in page.content
    assert b"1 emails" in page.content
    assert b"All remaining old data" in page.content

    response = test_client.post(
        "/cache/clear",
        data={
            "scope": "range",
            "start_at": "2026-09-10T14:00",
            "end_at": "2026-09-10T15:00",
            "hours": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/cache?")
    assert "flash=" in response.headers["location"]

    with Session(engine) as session:
        assert session.get(Message, "keep") is not None
        assert session.get(Message, "drop") is None
        assert session.exec(select(Job).where(Job.url_key == "keep-1")).first() is not None
        assert session.exec(select(Job).where(Job.url_key == "drop-1")).first() is None
        assert session.exec(select(Issue).where(Issue.title == "old fail")).first() is not None
        assert session.exec(select(Issue).where(Issue.title == "new fail")).first() is None


def test_cache_all_remaining_old_preview(client):
    from datetime import datetime, timezone

    test_client, engine = client
    old = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(Message(id="ancient", subject="leftover", received_at=old))
        session.commit()

    page = test_client.get("/cache?scope=old&end_at=2026-09-10T12:00")
    assert page.status_code == 200
    assert b"all remaining old" in page.content.lower()
    assert b"1 emails" in page.content


def test_matches_page_groups_by_day_and_shows_source_mail(client):
    test_client, engine = client
    with Session(engine) as session:
        process_email(session, alert_email(), LLM())
        session.commit()

    response = test_client.get("/matches?days=30&show=all&status=all")
    assert response.status_code == 200
    assert b"from" in response.content or b"Details" in response.content
    assert b"Newest first" in response.content
    scored = test_client.get("/matches?days=30&show=all&status=all&sort=score")
    assert scored.status_code == 200
    assert b"Score high" in scored.content
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
    assert b"Needs attention" in overview.content
    issues = test_client.get("/issues")
    assert b"Probe APIs" in issues.content
    apps = test_client.get("/applications")
    assert apps.status_code == 200
    flags = test_client.get("/flags")
    assert flags.status_code == 200
    assert b"Wrong-category flags" in flags.content
    cache = test_client.get("/cache")
    assert cache.status_code == 200
    assert b"Start day and time" in cache.content


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


def test_mail_defaults_to_one_day(client):
    test_client, _engine = client
    response = test_client.get("/")
    assert response.status_code == 200
    body = response.text
    assert 'value="1" selected' in body or "value=\"1\" selected" in body
    assert "<h1>Mail</h1>" not in body


def test_applications_page_is_a_sheet(client):
    test_client, _engine = client
    response = test_client.get("/applications")
    assert response.status_code == 200
    body = response.text
    assert "<h1>Applications</h1>" not in body
    assert "In review" in body or "in_review" in body.lower() or "In Review" in body


def test_alerts_with_no_postings_are_callable_out(client):
    test_client, engine = client
    with Session(engine) as session:
        session.add(
            Message(id="empty1", subject="Candidate New Job Alerts", category="job_alert")
        )
        session.commit()
    page = test_client.get("/?days=30")
    assert b"Alerts with no postings" in page.content
    assert b"j / k / o" in page.content
    filtered = test_client.get("/?days=30&has=norows")
    assert b"Candidate New Job Alerts" in filtered.content


def test_parsed_not_stored_shows_on_analysis(client):
    test_client, engine = client
    with Session(engine) as session:
        session.add(
            Message(
                id="gap1",
                subject="Deloitte is interested in you",
                category="other",
                jobs_found=2,
                extract_json='{"candidates":[{"title":"ML Engineer","company":"Deloitte","url":"","url_key":"card:deloitte:ml"}]}',
            )
        )
        session.commit()
    page = test_client.get("/?days=30&m=gap1")
    assert b"Parsed, not stored" in page.content
    assert b"ML Engineer" in page.content
    assert b"never stored as job rows" in page.content
    filtered = test_client.get("/?days=30&has=unstored")
    assert b"Deloitte is interested in you" in filtered.content


def test_report_miss_upserts_one_file(client):
    test_client, engine = client
    with Session(engine) as session:
        session.add(
            Message(
                id="miss1",
                subject="Apple jobs",
                category="other",
                jobs_found=4,
                extract_json='{"candidates":[{"title":"SWE","company":"Apple","url":""}]}',
            )
        )
        session.commit()
    first = test_client.post(
        "/mail/miss1/miss",
        data={"redirect": "/?m=miss1&days=30", "note": "expected 12 jobs, only 4"},
        follow_redirects=False,
    )
    assert first.status_code == 303
    second = test_client.post(
        "/mail/miss1/miss",
        data={"redirect": "/?m=miss1&days=30", "note": "still missing internships"},
        follow_redirects=False,
    )
    assert second.status_code == 303
    with Session(engine) as session:
        rows = session.exec(select(ExtractMiss)).all()
        assert len(rows) == 1
        assert rows[0].report_count == 2
        assert "expected 12 jobs" in rows[0].note
        assert "still missing internships" in rows[0].note
        issue = session.exec(select(Issue)).first()
        assert issue is not None
        assert "Extraction miss" in issue.title
        assert issue.message_id == "miss1"
    page = test_client.get("/misses")
    assert page.status_code == 200
    assert b"Apple jobs" in page.content
    assert b"parsed_not_stored" in page.content
    download = test_client.get("/misses/miss1.json")
    assert download.status_code == 200
    payload = download.json()
    assert payload["message_id"] == "miss1"
    assert payload["extracted"]["titles"] == ["SWE"]
    export = test_client.get("/misses/export.jsonl")
    assert export.status_code == 200
    lines = [line for line in export.text.splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["message_id"] == "miss1"


def test_job_miss_updates_the_parent_email_file(client):
    test_client, engine = client
    with Session(engine) as session:
        session.add(Message(id="jm1", subject="Digest", category="job_alert", jobs_found=1))
        session.flush()
        session.add(
            Job(
                message_id="jm1",
                url_key="jm1-job",
                title="Staff Engineer",
                company="Acme",
                scrape_status="empty",
                description="",
            )
        )
        session.commit()
    response = test_client.post(
        "/job/1/miss",
        data={"note": "description empty", "redirect": "/job/1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(engine) as session:
        miss = session.get(ExtractMiss, "jm1")
        assert miss is not None
        assert "missing_job_content" in miss.problems
        assert "description empty" in miss.note
        payload = json.loads(miss.payload)
        assert payload["extra"]["job_id"] == 1


def test_flag_also_writes_an_extract_miss(client):
    test_client, engine = client
    with Session(engine) as session:
        session.add(Message(id="flag1", subject="Recruiter ping", category="other"))
        session.commit()
    response = test_client.post(
        "/mail/flag1/flag",
        data={"category": "recruiter_outreach", "redirect": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(engine) as session:
        miss = session.get(ExtractMiss, "flag1")
        assert miss is not None
        assert miss.report_count == 1
        assert "wrong_category" in miss.problems



def test_applications_label_blank_roles_as_unknown(client):
    test_client, engine = client
    with Session(engine) as session:
        session.add(Application(company="Cisco", role="", match_key="cisco|"))
        session.commit()
    page = test_client.get("/applications")
    assert b"Unknown role" in page.content


def test_outreach_hides_first_steps_acknowledgement(client):
    test_client, engine = client
    with Session(engine) as session:
        session.add(Message(id="ack1", subject="Thank you for taking the first steps towards a career at Acme"))
        session.add(
            Outreach(
                message_id="ack1",
                kind="next_step",
                subject="Thank you for taking the first steps towards a career at Acme",
                company="Acme",
            )
        )
        session.commit()
    page = test_client.get("/outreach")
    assert page.status_code == 200
    assert b"first steps" not in page.content


def test_not_followup_drops_item_from_the_list(client):
    test_client, engine = client
    with Session(engine) as session:
        session.add(Message(id="o1", subject="Recruiter note"))
        session.add(
            Outreach(
                message_id="o1",
                kind="recruiter_outreach",
                subject="Quick chat?",
                company="Acme",
            )
        )
        session.commit()
    page = test_client.get("/outreach")
    assert b"Not a follow-up" in page.content
    assert b"Quick chat?" in page.content
    response = test_client.post(
        "/outreach/1/not-followup",
        data={"redirect": "/outreach"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = test_client.get("/outreach")
    assert b"Quick chat?" not in page.content


def test_overview_puts_category_table_in_a_grid(client):
    test_client, _engine = client
    response = test_client.get("/overview")
    assert response.status_code == 200
    assert b"dash-grid" in response.content
    assert b"<h1>Overview</h1>" not in response.content
