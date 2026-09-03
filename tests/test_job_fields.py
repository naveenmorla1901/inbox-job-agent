from app.job_fields import (
    email_type_of,
    enrich_fields,
    fields_from_jsonld,
    normalize_visa,
    salary_from_jsonld,
    source_type_of,
    state_of,
    visa_from_text,
)


def test_jsonld_salary_and_date():
    item = {
        "datePosted": "2026-08-20T12:00:00Z",
        "employmentType": "FULL_TIME",
        "baseSalary": {
            "currency": "USD",
            "value": {"minValue": 120000, "maxValue": 150000, "unitText": "YEAR"},
        },
    }
    fields = fields_from_jsonld(item)
    assert fields.posted_at == "2026-08-20"
    assert fields.employment_type == "Full-Time"
    assert "120000" in salary_from_jsonld(item)
    assert "150000" in fields.salary


def test_text_enrichment_fills_gaps():
    fields = enrich_fields(
        source="linkedin",
        url="https://www.linkedin.com/jobs/view/3901234567",
        url_key="linkedin:3901234567",
        location="Austin, TX (Remote)",
        description=(
            "Full-time Machine Learning Engineer. 3-5 years of experience. "
            "Python, PyTorch, SQL. $140,000-$170,000. We will not sponsor visas."
        ),
    )
    assert fields.source_type == "job_board"
    assert fields.posting_id == "3901234567"
    assert fields.state == "TX"
    assert fields.employment_type == "Full-Time"
    assert fields.visa_sponsorship == "No sponsorship"
    assert "3-5" in fields.experience_required
    assert "python" in fields.required_skills
    assert fields.salary


def test_career_site_is_official():
    assert source_type_of("greenhouse", "https://boards.greenhouse.io/acme/jobs/1") == "official_career_site"
    assert source_type_of("linkedin") == "job_board"


def test_remote_state():
    assert state_of("Remote - United States") == "Remote"
    assert state_of("Seattle, WA") == "WA"


def test_visa_yes_and_no():
    assert visa_from_text("Must be a US citizen. No visa sponsorship.") == "No sponsorship"
    assert (
        visa_from_text("This position is not eligible for work authorization sponsorship.")
        == "No sponsorship"
    )
    assert visa_from_text("H-1B sponsorship available for this role.") == "H-1B sponsorship"
    assert visa_from_text("STEM OPT candidates are welcome.") == "OPT/CPT mentioned"
    assert visa_from_text("Green card holders only.") == "US citizen / GC only"
    assert visa_from_text("Active TS/SCI security clearance required.") == "Clearance required"
    assert normalize_visa("Sponsorship mentioned", "Python, SQL, remote US.") == ""
    assert (
        normalize_visa("Sponsorship mentioned", "We will not sponsor visas for this role.")
        == "No sponsorship"
    )


def test_unclassified_subtypes():
    assert email_type_of("other", "Join our webinar", "office hours tomorrow") == "webinar"
    assert email_type_of("other", "Your weekly newsletter", "unsubscribe from all") == "newsletter"
    assert email_type_of("other", "Security alert", "unusual sign-in on your account") == "security"
    assert email_type_of("job_alert", "8 new jobs", "jobs") == ""
