from bs4 import BeautifulSoup

from app.scrape import (
    _from_ashby,
    _from_html,
    _from_jsonld,
    _from_linkedin,
    _from_workday,
    greenhouse_board_from_html,
    interstitial_destination,
    page_is_interstitial,
    workday_cxs_url,
)

JSONLD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org/","@type":"JobPosting",
 "title":"Senior Data Scientist",
 "datePosted":"2026-08-01",
 "employmentType":"FULL_TIME",
 "baseSalary":{"currency":"USD","value":{"minValue":130000,"maxValue":160000,"unitText":"YEAR"}},
 "description":"<p>Build <b>ML models</b> in Python and SQL.</p><ul><li>PyTorch</li></ul>",
 "hiringOrganization":{"@type":"Organization","name":"Northwind Labs"},
 "jobLocationType":"TELECOMMUTE",
 "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
   "addressLocality":"Austin","addressRegion":"TX","addressCountry":"US"}}}
</script></head><body>ignored</body></html>
"""

PLAIN_PAGE = """
<html><head><title>NLP Engineer at Globex</title>
<meta property="og:title" content="NLP Engineer"><meta property="og:site_name" content="Globex">
</head><body><main>We need spaCy, transformers and strong Python skills.</main></body></html>
"""


def test_jsonld_posting_is_preferred():
    job = _from_jsonld(BeautifulSoup(JSONLD_PAGE, "lxml"))
    assert job is not None
    assert job.title == "Senior Data Scientist"
    assert job.company == "Northwind Labs"
    assert "Austin" in job.location and "Remote" in job.location
    assert "ML models" in job.description and "<p>" not in job.description
    assert job.posted_at == "2026-08-01"
    assert job.employment_type == "Full-Time"
    assert "130000" in job.salary


def test_html_fallback_uses_meta_and_main():
    job = _from_html(BeautifulSoup(PLAIN_PAGE, "lxml"))
    assert job.title == "NLP Engineer"
    assert job.company == "Globex"
    assert "transformers" in job.description


def test_pages_without_jsonld_return_none():
    assert _from_jsonld(BeautifulSoup(PLAIN_PAGE, "lxml")) is None


LINKEDIN_GUEST_FRAGMENT = """
<section class="top-card-layout">
  <h2 class="top-card-layout__title">Machine Learning Engineer</h2>
  <a class="topcard__org-name-link" href="#">Northwind Labs</a>
  <span class="topcard__flavor topcard__flavor--bullet">Austin, TX</span>
</section>
<div class="show-more-less-html__markup">
  <p>Own model training pipelines in Python, PyTorch and Airflow.</p>
</div>
<ul>
  <li class="description__job-criteria-item">Seniority level Mid-Senior level</li>
  <li class="description__job-criteria-item">Employment type Full-time</li>
</ul>
"""


def test_linkedin_guest_fragment_yields_all_fields():
    job = _from_linkedin(BeautifulSoup(LINKEDIN_GUEST_FRAGMENT, "lxml"))
    assert job is not None
    assert job.title == "Machine Learning Engineer"
    assert job.company == "Northwind Labs"
    assert job.location == "Austin, TX"
    assert "PyTorch" in job.description
    assert "Full-time" in job.description
    assert job.extraction == "linkedin"


def test_linkedin_parser_defers_when_the_fragment_is_missing():
    assert _from_linkedin(BeautifulSoup(PLAIN_PAGE, "lxml")) is None


def test_bot_walls_are_reported_as_blocked():
    wall = "<html><body><main>Please verify you are a human. Enable JavaScript to continue.</main></body></html>"
    job = _from_html(BeautifulSoup(wall, "lxml"))
    assert job.status == "blocked" and not job.ok


def test_linkedin_login_shell_is_blocked():
    wall = (
        "<html><head><title>LinkedIn Login, Sign in | LinkedIn</title></head>"
        "<body><main>Sign in with Apple. Sign in with a passkey. New to LinkedIn? Join now</main></body></html>"
    )
    job = _from_html(BeautifulSoup(wall, "lxml"))
    assert job.status == "blocked" and not job.ok


def test_haystack_marketing_shell_is_not_a_posting():
    page = (
        "<html><head><title>Haystack – Get hired without the hassle</title></head>"
        "<body><main>Create an account and let Haystack apply for you. "
        "Get hired without the hassle.</main></body></html>"
    )
    job = _from_html(BeautifulSoup(page, "lxml"))
    assert not job.ok
    assert job.status == "empty"


def test_application_forms_are_not_mistaken_for_descriptions():
    form_page = (
        "<html><body><main>Apply for this job * indicates a required field "
        "First Name Last Name Attach Dropbox Accepted file types: pdf, doc</main></body></html>"
    )
    job = _from_html(BeautifulSoup(form_page, "lxml"))
    assert not job.ok


def test_adzuna_redirect_stub_is_not_a_description():
    page = (
        "<html><head><title>Adzuna Jobs Search</title></head>"
        "<body><main>Adzuna. Every job. Everywhere. You are now being redirected to CoreWeave. "
        "If you are not redirected within 5 seconds, "
        '<a href="https://boards.greenhouse.io/coreweave/jobs/1">click here</a>.</main></body></html>'
    )
    job = _from_html(BeautifulSoup(page, "lxml"))
    assert not job.ok
    assert job.status == "empty"
    assert "redirected" not in (job.description or "").lower()
    assert job.company == "CoreWeave"
    dest = interstitial_destination(page, "https://www.adzuna.com/land/ad/1")
    assert "greenhouse.io/coreweave" in dest


def test_workday_widget_json_is_an_interstitial():
    blob = '{"widget":"redirect","url":"/MyNelnet/job/Lincoln-NE/IT-Manager_R22979-1","externalSpa":true}'
    assert page_is_interstitial("", blob)
    dest = interstitial_destination(blob, "https://nelnet.wd1.myworkdayjobs.com/MyNelnet")
    assert dest.endswith("/MyNelnet/job/Lincoln-NE/IT-Manager_R22979-1")


def test_cookie_banner_is_not_a_job_description():
    page = (
        "<html><head><title>ASSA ABLOY Careers</title></head>"
        "<body><main>This website uses only essential cookies. By continuing to browse this website "
        "without changing your browser cookie settings, you agree to let us store cookies. "
        "Accept Close</main></body></html>"
    )
    job = _from_html(BeautifulSoup(page, "lxml"))
    assert not job.ok
    assert "essential cookies" not in (job.description or "").lower()


def test_language_picker_chrome_is_not_a_job_description():
    page = (
        "<html><head><title>Doosan</title></head>"
        "<body><main>Skip to main content. Language. Čeština (Česká republika). "
        "Deutsch (Deutschland). Français (France).</main></body></html>"
    )
    job = _from_html(BeautifulSoup(page, "lxml"))
    assert not job.ok


def test_workday_cxs_url_uses_tenant_site_and_job_path():
    url = (
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/"
        "job/Santa-Clara-CA/Software-Engineer_JR123"
    )
    api = workday_cxs_url(url)
    assert api.endswith(
        "/wday/cxs/nvidia/NVIDIAExternalCareerSite/job/Santa-Clara-CA/Software-Engineer_JR123"
    )


def test_greenhouse_board_is_discovered_in_html():
    html = '<script src="https://boards-api.greenhouse.io/v1/boards/stripe/embed/job_board"></script>'
    assert greenhouse_board_from_html(html) == "stripe"


def test_workday_and_ashby_json_parsers():
    workday = _from_workday(
        {
            "jobPostingInfo": {
                "title": "ML Engineer",
                "jobDescription": "<p>Build models in Python and PyTorch.</p>",
                "location": "Remote",
                "timeType": "Full time",
            }
        }
    )
    assert workday.ok
    assert workday.title == "ML Engineer"
    assert "PyTorch" in workday.description
    ashby = _from_ashby(
        {
            "jobs": [
                {
                    "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "title": "Data Scientist",
                    "descriptionHtml": "<p>SQL and Python.</p>",
                    "locationName": "Austin, TX",
                }
            ]
        },
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    )
    assert ashby.ok
    assert ashby.title == "Data Scientist"


def test_blocked_adzuna_page_follows_the_click_here_link(monkeypatch):
    from app.extract_jobs import JobCandidate
    from app.scrape import UA, fetch_job
    from app import scrape as scrape_mod

    wall = (
        "<html><head><title>Adzuna Jobs Search</title></head><body>"
        "If you are not redirected within 5 seconds, "
        '<a href="https://jobs.example.com/ai-engineer">click here</a>.'
        "</body></html>"
    )
    dest = (
        "<html><head><title>AI Engineer</title>"
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","title":"AI Engineer",'
        '"description":"<p>' + ("Python SQL machine learning. " * 20) + '</p>","hiringOrganization":{"name":"Acme"}}'
        "</script></head><body><main>Build models in Python and SQL. "
        + ("machine learning " * 40)
        + "</main></body></html>"
    )

    class Fake:
        def __init__(self, status, text, url):
            self.status_code = status
            self.text = text
            self.url = url
            self.request = type("Req", (), {"headers": {"user-agent": UA}})()

    def fake_get(_client, url, _headers):
        if "adzuna.com" in url:
            return Fake(403, wall, url)
        return Fake(200, dest, url)

    monkeypatch.setattr(scrape_mod, "_get", fake_get)
    job = fetch_job(
        JobCandidate(
            url="https://www.adzuna.com/land/ad/5123456789",
            url_key="adzuna:5123456789",
            title="Artificial Intelligence Engineer",
            source="adzuna",
        )
    )
    assert job.ok
    assert "AI Engineer" in job.title


def test_login_wall_host_detects_linkedin_and_indeed():
    from app.scrape import login_wall_host, notable_scrape_failures, ScrapedJob

    assert login_wall_host("https://www.linkedin.com/jobs/view/123")
    assert login_wall_host("https://www.indeed.com/viewjob?jk=abc")
    assert login_wall_host("https://www.ihire.com/job/1")
    assert login_wall_host("https://jobs.jobs2web.com/acme")
    assert not login_wall_host("https://jobs.example.com/ai-engineer")
    walls = {
        "a": ScrapedJob(status="blocked", final_url="https://www.linkedin.com/jobs/view/123"),
        "b": ScrapedJob(status="blocked", final_url="https://www.indeed.com/viewjob?jk=x"),
        "c": ScrapedJob(status="error", final_url="https://careers.example.com/job/9"),
        "d": ScrapedJob(status="blocked", final_url="https://www.ihire.com/job/1"),
        "e": ScrapedJob(status="blocked", final_url=""),
    }
    notable = notable_scrape_failures(walls)
    assert len(notable) == 1
    assert notable[0].final_url.endswith("/job/9")


def test_reader_retries_login_wall_boards_through_the_guest_fragment(monkeypatch):
    """LinkedIn 403s a browser UA; the reader plus the guest fragment still reads it."""
    from app.extract_jobs import JobCandidate
    from app.scrape import UA, fetch_job
    from app import scrape as scrape_mod

    guest = "Machine Learning Engineer at Northwind. " + ("pytorch airflow " * 40)

    class Fake:
        def __init__(self, status, text, url):
            self.status_code = status
            self.text = text
            self.url = url
            self.request = type("Req", (), {"headers": {"user-agent": UA}})()

    def fake_get(_client, url, _headers):
        if url.startswith("https://r.jina.ai/") and "jobs-guest" in url:
            return Fake(200, guest, url)
        return Fake(403, "Sign in to view this job", url)

    monkeypatch.setattr(scrape_mod, "_get", fake_get)
    job = fetch_job(
        JobCandidate(
            url="https://www.linkedin.com/jobs/view/3901234567",
            url_key="linkedin:3901234567",
            title="Machine Learning Engineer",
            company="Northwind",
            source="linkedin",
        )
    )
    assert job.ok
    assert job.extraction == "reader"
    assert "pytorch" in job.description


def test_a_posting_with_no_link_is_never_fetched():
    from app.extract_jobs import JobCandidate
    from app.scrape import fetch_all

    scraped = fetch_all(
        [JobCandidate(url="", url_key="card:apple:senior ml engineer", title="Senior ML Engineer")]
    )
    page = scraped["card:apple:senior ml engineer"]
    assert page.status == "skipped"
    assert page.extraction == "email"


def test_reader_fallback_recovers_a_blocked_company_site(monkeypatch):
    from app.extract_jobs import JobCandidate
    from app.scrape import UA, fetch_job
    from app import scrape as scrape_mod

    posting = (
        "AI Engineer at Acme. Build models in Python and SQL. "
        + ("machine learning " * 40)
    )

    class Fake:
        def __init__(self, status, text, url):
            self.status_code = status
            self.text = text
            self.url = url
            self.request = type("Req", (), {"headers": {"user-agent": UA}})()

    def fake_get(_client, url, _headers):
        if url.startswith("https://r.jina.ai/"):
            return Fake(200, posting, url)
        return Fake(403, "Access denied", url)

    monkeypatch.setattr(scrape_mod, "_get", fake_get)
    job = fetch_job(
        JobCandidate(
            url="https://jobs.example.com/ai-engineer",
            url_key="html:jobs.example.com/ai-engineer",
            title="AI Engineer",
            company="Acme",
            source="example",
        )
    )
    assert job.ok
    assert job.extraction == "reader"
    assert "Python" in job.description
