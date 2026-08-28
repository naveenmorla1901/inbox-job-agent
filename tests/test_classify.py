from app.classify import (
    ASSESSMENT,
    INTERVIEW,
    JOB_ALERT,
    OTHER,
    RECRUITER,
    REJECTION,
    classify_rules,
)
from app.config import get_profile
from app.email_parse import ParsedEmail

PROFILE = get_profile()


def email(sender_email: str, subject: str, text: str, name: str = "Someone") -> ParsedEmail:
    return ParsedEmail(
        id="x",
        sender_name=name,
        sender_email=sender_email,
        subject=subject,
        text=text,
        snippet=text[:120],
    )


def test_job_board_digest_is_an_alert():
    result = classify_rules(
        email("jobalerts-noreply@linkedin.com", "8 new jobs for you", "jobs jobs jobs"),
        PROFILE,
        job_count=8,
    )
    assert result.category == JOB_ALERT


def test_recruiter_outreach_detected():
    result = classify_rules(
        email(
            "priya@talentbridge.com",
            "Data Scientist role - immediate need",
            "Hi, I came across your profile on LinkedIn and we have an urgent opening. "
            "Would you be interested? Please share your updated resume.",
        ),
        PROFILE,
    )
    assert result.category == RECRUITER
    assert result.confidence >= 0.8


def test_interview_invite_beats_generic_outreach():
    result = classify_rules(
        email(
            "recruiting@northwind.com",
            "Interview invitation - ML Engineer",
            "We would like to schedule an interview. Please share your availability this week.",
        ),
        PROFILE,
    )
    assert result.category == INTERVIEW


def test_assessment_detected():
    result = classify_rules(
        email("no-reply@hackerrank.com", "Your coding assessment", "Complete the online assessment"),
        PROFILE,
    )
    assert result.category == ASSESSMENT


def test_rejection_detected():
    result = classify_rules(
        email(
            "no-reply@greenhouse.io",
            "Update on your application",
            "Unfortunately we have decided to move forward with other candidates.",
        ),
        PROFILE,
    )
    assert result.category == REJECTION


def test_marketing_is_ignored():
    result = classify_rules(
        email("news@medium.com", "Your weekly newsletter", "Top stories this week. Unsubscribe from all."),
        PROFILE,
    )
    assert result.category == OTHER
