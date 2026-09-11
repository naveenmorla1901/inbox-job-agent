"""Offline tests for the LLM digest-recovery pass. No network: a fake LLM stands
in for the model so we test the anchor grounding, merge, and gating logic."""
from __future__ import annotations

import pytest

from app import config
from app.email_parse import ParsedEmail, extract_links
from app.llm_extract import (
    _anchor_table,
    _rules_look_incomplete,
    expected_posting_count,
    extract_postings,
    looks_like_digest,
)


class FakeLLM:
    """Returns canned JSON payloads in order and records the prompts it saw."""

    enabled = True

    def __init__(self, payload, *more):
        self.payloads = [payload, *more]
        self.prompts: list[str] = []
        self.calls = 0

    def json(self, prompt, system="", task="classify", timeout=45):
        self.prompts.append(prompt)
        self.calls += 1
        idx = min(self.calls - 1, len(self.payloads) - 1)
        return self.payloads[idx]


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
    expected = expected_posting_count(email, anchors)
    assert rules and not _rules_look_incomplete(rules, anchors, expected)


# The three digest shapes that under-extracted in production. Each names its roles
# in the body or anchor text while using link shapes the regex rules do not know.

APPLE_TEXT = """Hi Naveen,

Great news - the following role(s) posted on the Apple jobs site match the ml
criteria you selected.

Machine Learning Research Engineer, ASE Search
Senior Machine Learning Engineer, NLP, Input Experience
Sr. Machine Learning Research Engineer, Siri Speech
Sr. Machine Learning Engineer, Speech LLM Evaluation
"""

UKG_HTML = """
<table>
<tr><td><a href="https://email.ukgjobalerts.com/c/eJxEj7uO2zAQAL">Sr. Automation QA Engineer</a></td></tr>
<tr><td><a href="https://email.ukgjobalerts.com/c/bBxEj7uO2zAQBL">AML Compliance Analyst</a></td></tr>
<tr><td><a href="https://email.ukgjobalerts.com/c/cCxEj7uO2zAQCL">Senior Immigration Paralegal</a></td></tr>
<tr><td><a href="https://email.ukgjobalerts.com/c/dDxEj7uO2zAQDL">Staff Data Engineer</a></td></tr>
<tr><td><a href="https://email.ukgjobalerts.com/unsubscribe">Unsubscribe</a></td></tr>
</table>
"""


def test_career_site_link_shapes_still_trigger_the_recovery_pass(fresh_config):
    """UKG/BAL alerts: real titles, but no link the regex rules recognise."""
    email = _digest(UKG_HTML)
    from app.extract_jobs import extract_from_email

    rules = extract_from_email(email, limit=40)
    anchors = _anchor_table(email, cap=160)
    expected = expected_posting_count(email, anchors)
    assert expected >= 4, "every anchor label reads as a job title"
    assert _rules_look_incomplete(rules, anchors, expected)

    llm = FakeLLM(
        {"postings": [
            {"link": 0, "title": "Sr. Automation QA Engineer", "company": "BAL"},
            {"link": 1, "title": "AML Compliance Analyst", "company": "BAL"},
            {"link": 2, "title": "Senior Immigration Paralegal", "company": "BAL"},
            {"link": 3, "title": "Staff Data Engineer", "company": "BAL"},
        ]}
    )
    jobs = extract_postings(email, llm, limit=40)
    assert len(jobs) >= 4
    assert "Sr. Automation QA Engineer" in {job.title for job in jobs}


def test_expected_count_is_passed_to_the_model(fresh_config):
    email = _digest(UKG_HTML)
    llm = FakeLLM({"postings": [{"link": 0, "title": "Sr. Automation QA Engineer"}]})
    extract_postings(email, llm, limit=40)
    assert "about 4 job title(s)" in llm.prompts[0]


def test_roles_named_without_a_link_still_become_rows(fresh_config):
    """Apple alerts list the roles as plain text; a posting may have no own link."""
    email = ParsedEmail(
        id="m3",
        sender_name="Apple Jobs",
        sender_email="no_reply@apple.com",
        subject="New roles matching your ml job alert",
        text=APPLE_TEXT,
        html="",
    )
    assert expected_posting_count(email, []) == 4

    llm = FakeLLM(
        {"postings": [
            {"link": None, "title": "Machine Learning Research Engineer, ASE Search",
             "company": "Apple"},
            {"link": None, "title": "Senior Machine Learning Engineer, NLP, Input Experience",
             "company": "Apple"},
            {"link": None, "title": "Sr. Machine Learning Research Engineer, Siri Speech",
             "company": "Apple"},
            {"link": None, "title": "Sr. Machine Learning Engineer, Speech LLM Evaluation",
             "company": "Apple"},
        ]}
    )
    jobs = extract_postings(email, llm, limit=40)
    assert len(jobs) == 4
    assert all(job.url == "" and job.url_key for job in jobs)
    assert len({job.url_key for job in jobs}) == 4


def test_a_short_answer_triggers_one_top_up_call(fresh_config):
    email = _digest(UKG_HTML)
    llm = FakeLLM(
        {"postings": [{"link": 0, "title": "Sr. Automation QA Engineer", "company": "BAL"}]},
        {"postings": [
            {"link": 1, "title": "AML Compliance Analyst", "company": "BAL"},
            {"link": 2, "title": "Senior Immigration Paralegal", "company": "BAL"},
            {"link": 3, "title": "Staff Data Engineer", "company": "BAL"},
        ]},
    )
    jobs = extract_postings(email, llm, limit=40)
    assert llm.calls == 2, "one recovery call, then one top-up for the missing rows"
    assert len(jobs) == 4
    assert "Already found" in llm.prompts[1]


def test_two_roles_behind_one_link_both_survive(fresh_config):
    html = '<a href="https://careers.acme-labs.com/openings">See our openings</a>'
    email = _digest(html)
    llm = FakeLLM(
        {"postings": [
            {"link": 0, "title": "Data Scientist", "company": "Acme Labs"},
            {"link": 0, "title": "ML Platform Engineer", "company": "Acme Labs"},
        ]}
    )
    jobs = extract_postings(email, llm, limit=40)
    assert {job.title for job in jobs} == {"Data Scientist", "ML Platform Engineer"}
    assert len({job.url_key for job in jobs}) == 2
