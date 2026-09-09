"""Offline tests for the LLM digest-recovery pass. No network: a fake LLM stands
in for the model so we test the anchor grounding, merge, and gating logic."""
from __future__ import annotations

import pytest

from app import config
from app.email_parse import ParsedEmail, extract_links
from app.llm_extract import _anchor_table, _rules_look_incomplete, extract_postings, looks_like_digest


class FakeLLM:
    """Returns a canned JSON payload and counts how often it was asked."""

    enabled = True

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def json(self, prompt, system="", task="classify", timeout=45):
        self.calls += 1
        return self.payload


@pytest.fixture()
def fresh_config():
    config.get_settings.cache_clear()
    config.get_profile.cache_clear()
    yield
    config.get_settings.cache_clear()
    config.get_profile.cache_clear()


def _digest(html: str) -> ParsedEmail:
    # A board sender so looks_like_digest() is true via the rules classifier.
    return ParsedEmail(
        id="m1",
        sender_name="LinkedIn Job Alerts",
        sender_email="jobalerts-noreply@linkedin.com",
        subject="8 new jobs match your preferences",
        html=html,
        text="",
        links=extract_links(html),
    )


def test_llm_recovers_a_posting_the_rules_missed(fresh_config):
    # Unknown career host with a non-numeric path: the regex rules reject it.
    html = """
    <a href="https://careers.acme-labs.com/roles/gooey">Senior Data Scientist</a>
    <a href="https://example.com/unsubscribe">Unsubscribe</a>
    """
    email = _digest(html)
    llm = FakeLLM(
        {"postings": [{"link": 0, "title": "Senior Data Scientist",
                       "company": "Acme Labs", "location": "Remote"}]}
    )
    jobs = extract_postings(email, llm, limit=40)
    assert llm.calls == 1
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Senior Data Scientist"
    assert job.company == "Acme Labs"
    # URL is grounded in the real anchor, never invented by the model.
    assert job.url == "https://careers.acme-labs.com/roles/gooey"


def test_llm_cannot_invent_a_url(fresh_config):
    """A link index the model returns must map to a real anchor; junk is dropped."""
    html = '<a href="https://careers.acme-labs.com/roles/gooey">Data Engineer</a>'
    email = _digest(html)
    llm = FakeLLM(
        {"postings": [
            {"link": 99, "title": "Ghost Job", "company": "Nowhere"},           # out of range
            {"link": 0, "title": "Apply now", "company": "Acme"},               # junk CTA title
            {"link": 0, "title": "Data Engineer", "company": "Acme Labs"},      # valid
        ]}
    )
    jobs = extract_postings(email, llm, limit=40)
    assert [j.title for j in jobs] == ["Data Engineer"]
    assert all(j.url.startswith("https://careers.acme-labs.com") for j in jobs)


def test_non_digest_never_calls_the_llm(fresh_config):
    email = ParsedEmail(
        id="m2",
        sender_name="A Friend",
        sender_email="friend@personal.com",
        subject="lunch tomorrow?",
        html='<a href="https://maps.example.com/place">the cafe</a>',
        text="see you at noon",
    )
    llm = FakeLLM({"postings": [{"link": 0, "title": "Chef"}]})
    jobs = extract_postings(email, llm, limit=40)
    assert llm.calls == 0
    assert jobs == []


def test_none_llm_falls_back_to_rules_only(fresh_config):
    html = '<a href="https://www.linkedin.com/jobs/view/3901234567">Data Scientist</a>'
    email = _digest(html)
    jobs = extract_postings(email, None, limit=40)
    assert len(jobs) == 1  # the rules alone handle a plain LinkedIn posting


def test_anchor_table_keeps_distinct_tracker_links(fresh_config):
    # Two postings behind the same click-tracker path, distinguished only by query.
    html = """
    <a href="https://track.acme.com/ls/click?upn=AAA">Data Scientist</a>
    <a href="https://track.acme.com/ls/click?upn=BBB">ML Engineer</a>
    """
    email = _digest(html)
    anchors = _anchor_table(email, cap=160)
    assert len(anchors) == 2  # not collapsed by canonical key


def test_gap_gate_skips_llm_when_rules_are_complete(fresh_config):
    html = '<a href="https://www.linkedin.com/jobs/view/3901234567">Data Scientist</a>'
    email = _digest(html)
    from app.extract_jobs import extract_from_email

    rules = extract_from_email(email, limit=40)
    anchors = _anchor_table(email, cap=160)
    assert rules and not _rules_look_incomplete(rules, anchors)
