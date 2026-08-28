from pathlib import Path

import base64

from app.email_parse import ParsedEmail, extract_links, html_to_text
from app.extract_jobs import canonical_key, extract_from_email, is_job_url, unwrap_url

FIXTURE = Path(__file__).parent / "fixtures" / "linkedin_alert.html"


def alert_email() -> ParsedEmail:
    html = FIXTURE.read_text(encoding="utf-8")
    return ParsedEmail(
        id="m1",
        sender_name="LinkedIn Job Alerts",
        sender_email="jobalerts-noreply@linkedin.com",
        subject="8 new jobs match your preferences",
        html=html,
        text=html_to_text(html),
        links=extract_links(html),
    )


def test_only_real_postings_are_extracted():
    jobs = extract_from_email(alert_email())
    keys = {job.url_key for job in jobs}
    assert keys == {"linkedin:3901234567", "linkedin:3907654321", "indeed:9f8e7d6c5b4a3210"}


def test_titles_and_companies_are_recovered():
    jobs = {job.url_key: job for job in extract_from_email(alert_email())}
    first = jobs["linkedin:3901234567"]
    assert first.title == "Data Scientist"
    assert first.company == "Acme Analytics"
    assert "Remote" in first.location
    assert jobs["linkedin:3907654321"].title == "Machine Learning Engineer"
    assert jobs["indeed:9f8e7d6c5b4a3210"].source == "indeed"


def test_search_and_unsubscribe_links_are_not_jobs():
    assert not is_job_url("https://www.linkedin.com/jobs/search/?keywords=data")
    assert not is_job_url("https://www.linkedin.com/comm/psettings/email-unsubscribe?lipi=1")
    assert not is_job_url("https://example.com/newsletter")


def test_company_career_pages_count_when_they_carry_an_ats_id():
    assert is_job_url("https://stripe.com/jobs/search?gh_jid=7532733")
    assert is_job_url("https://jobs.lever.co/matchgroup/7fca4a70-174c-41a2-b44b-7ff1cb9422e7")
    assert is_job_url("https://careers.acme.com/careers/job/12345")
    assert not is_job_url("https://acme.com/about-us")
    assert not is_job_url("https://acme.com/careers")


def test_greenhouse_urls_share_one_key():
    embed = canonical_key("https://boards.greenhouse.io/embed/job_app?for=stripe&token=7532733")
    company = canonical_key("https://stripe.com/jobs/search?gh_jid=7532733")
    board = canonical_key("https://job-boards.greenhouse.io/stripe/jobs/7532733")
    assert embed == company == board == "greenhouse:7532733"


def test_tracking_params_collapse_to_one_key():
    a = canonical_key("https://www.linkedin.com/comm/jobs/view/3901234567/?trackingId=aaa")
    b = canonical_key("https://www.linkedin.com/jobs/view/3901234567?refId=bbb&trk=ccc")
    assert a == b == "linkedin:3901234567"


def test_click_trackers_are_unwrapped():
    target = "https://boards.greenhouse.io/acme/jobs/4567890"
    wrapped = f"https://click.mailer.io/track?u=12345&url={target.replace(':', '%3A').replace('/', '%2F')}"
    assert unwrap_url(wrapped) == target
    assert is_job_url(wrapped)
    assert canonical_key(wrapped) == "greenhouse:4567890"


def test_base64_encoded_redirects_are_unwrapped():
    target = "https://jobs.lever.co/acme/7fca4a70-174c-41a2-b44b-7ff1cb9422e7"
    blob = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    assert unwrap_url(f"https://email.acme.com/c/{blob}") == target
