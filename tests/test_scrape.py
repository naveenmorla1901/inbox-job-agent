from bs4 import BeautifulSoup

from app.scrape import _from_html, _from_jsonld, _from_linkedin, interstitial_destination, page_is_interstitial

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
