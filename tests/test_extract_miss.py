import json

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import db
from app.extract_miss import build_payload, dump_misses, upsert_extract_miss
from app.models import ExtractMiss, Job, Message


def _session(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.enforce_sqlite_foreign_keys(engine)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("app.extract_miss.miss_dir", lambda: tmp_path)
    return engine


def test_upsert_same_email_does_not_duplicate(tmp_path, monkeypatch):
    engine = _session(tmp_path, monkeypatch)
    with Session(engine) as session:
        session.add(
            Message(
                id="m1",
                subject="Apple jobs",
                sender="Apple",
                category="job_alert",
                jobs_found=2,
                body_text="Software Engineer\nML Engineer",
                extract_json=json.dumps(
                    {
                        "candidates": [
                            {"title": "Software Engineer", "company": "Apple", "url": ""},
                            {"title": "ML Engineer", "company": "Apple", "url": ""},
                        ]
                    }
                ),
            )
        )
        session.commit()
        message = session.get(Message, "m1")
        first = upsert_extract_miss(session, message, note="expected 6 jobs, only 2", source="mail")
        session.commit()
        second = upsert_extract_miss(session, message, note="still missing titles", source="mail")
        session.commit()
        rows = session.exec(select(ExtractMiss)).all()
        assert len(rows) == 1
        assert rows[0].message_id == "m1"
        assert rows[0].report_count == 2
        assert first.message_id == second.message_id
        assert "expected 6 jobs" in rows[0].note
        assert "still missing titles" in rows[0].note
        payload = json.loads(rows[0].payload)
        assert payload["schema"] == 1
        assert payload["how_to_read"]
        assert payload["extracted"]["titles"] == ["Software Engineer", "ML Engineer"]
        assert "parsed_not_stored" in payload["problem"]["tags"]
        assert (tmp_path / "m1.json").is_file()
        dumped = dump_misses(session, tmp_path)
        assert len(dumped) == 1
        index = json.loads((tmp_path / "_index.json").read_text(encoding="utf-8"))
        assert index["count"] == 1


def test_payload_includes_stored_jobs(tmp_path, monkeypatch):
    engine = _session(tmp_path, monkeypatch)
    with Session(engine) as session:
        session.add(Message(id="m2", subject="Zillow", category="job_alert", jobs_found=1))
        session.flush()
        session.add(
            Job(
                message_id="m2",
                url_key="zillow-1",
                title="Data Engineer",
                company="Zillow",
                scrape_status="empty",
                description="",
            )
        )
        session.commit()
        payload = build_payload(session, session.get(Message, "m2"), note="description empty")
        assert payload["stored_jobs"][0]["title"] == "Data Engineer"
        assert payload["stored_jobs"][0]["description_chars"] == 0
        assert "thin_scrape" in payload["problem"]["tags"]
        assert payload["problem"]["user_note"] == "description empty"
