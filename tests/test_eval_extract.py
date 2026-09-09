from datetime import datetime

from app.classify import JOB_ALERT, OTHER
from app.email_parse import ParsedEmail
from app.eval_extract import (
    eval_record,
    should_screenshot,
    since_epoch,
    window_query,
)
from app.timefmt import EASTERN
from tests.test_extract import alert_email
from tests.test_classify import email as plain_email


def test_since_epoch_is_friday_midnight_eastern():
    expected = int(datetime(2026, 9, 4, tzinfo=EASTERN).timestamp())
    assert since_epoch("2026-09-04") == expected


def test_eval_query_is_all_mail_not_gmail_query():
    query = window_query("2026-09-04")
    assert query == f"after:{since_epoch('2026-09-04')}"
    assert "in:inbox" not in query
    assert "promotions" not in query
    assert "category:" not in query


def test_eval_record_ignores_job_cap_and_keeps_titles():
    record = eval_record(alert_email(), me_email="me@example.com")
    assert record["job_count"] == 3
    assert record["gmail_link"].startswith("https://mail.google.com/mail/u/0/#all/")
    assert "Data Scientist" in record["titles"]
    assert record["screenshot"] is True


def test_skip_self_sent_even_if_it_looks_like_an_alert():
    mail = alert_email()
    mail.sender_email = "me@example.com"
    shot, reason = should_screenshot(mail, job_count=3, me_email="me@example.com")
    assert shot is False
    assert reason == "self"


def test_skip_non_digest_with_no_job_urls():
    mail = plain_email("friend@example.com", "Lunch tomorrow?", "Want to grab lunch?")
    shot, reason = should_screenshot(mail, job_count=0, me_email="me@example.com")
    assert shot is False
    assert reason == "not_digest_no_jobs"
    assert classify_guess(mail, 0) != JOB_ALERT


def test_screenshot_when_extract_finds_job_urls():
    mail = plain_email("random@example.com", "Checking in", "see attached")
    shot, reason = should_screenshot(mail, job_count=2, me_email="me@example.com")
    assert shot is True
    assert reason == ""


def classify_guess(mail: ParsedEmail, job_count: int) -> str:
    from app.classify import classify_rules
    from app.config import get_profile

    return classify_rules(mail, get_profile(), job_count=job_count).category


def test_screenshot_job_board_digest():
    mail = alert_email()
    shot, _ = should_screenshot(mail, job_count=3, me_email="me@example.com")
    assert shot is True
    assert classify_guess(mail, 3) == JOB_ALERT
    assert classify_guess(plain_email("a@b.com", "hi", "hey"), 0) == OTHER
