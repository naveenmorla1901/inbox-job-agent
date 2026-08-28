from app.config import get_profile
from app.matcher import match_job, max_years_required, score_title

PROFILE = get_profile()

DS_JD = """
We are hiring a Data Scientist to build machine learning models in Python and SQL.
You will work with pandas, scikit-learn and PyTorch, run A/B tests, and deploy models
on AWS with Docker. Experience with NLP, LLMs and RAG pipelines is a plus.
2+ years of experience required. Remote (United States).
"""

SALES_JD = """
Account Executive selling SaaS subscriptions. Cold calling, quota, CRM hygiene.
Commission only. 5 years of sales experience.
"""

CLEARED_JD = """
Senior Data Scientist supporting a federal program. Active security clearance (TS/SCI)
with polygraph required. Python, SQL, machine learning.
"""


def test_relevant_role_scores_high():
    result = match_job(PROFILE, "Data Scientist", DS_JD, "Remote, United States", "Acme")
    assert not result.rejected
    assert result.score >= 0.7
    assert "python" in result.matched_skills


def test_unrelated_role_is_rejected_on_title():
    result = match_job(PROFILE, "Account Executive", SALES_JD, "Chicago, IL", "Globex")
    assert result.rejected


def test_hard_blocker_keyword_rejects():
    result = match_job(PROFILE, "Senior Data Scientist", CLEARED_JD, "Virginia", "GovCo")
    assert result.rejected
    assert "clearance" in result.verdict


def test_seniority_penalty_applies():
    junior = match_job(PROFILE, "Machine Learning Engineer", DS_JD)
    senior_jd = DS_JD.replace("2+ years of experience", "12+ years of experience")
    senior = match_job(PROFILE, "Machine Learning Engineer", senior_jd)
    assert senior.score < junior.score


def test_years_parser_ignores_unrelated_numbers():
    assert max_years_required("founded 15 years ago; 3 years of experience needed") == 3
    assert max_years_required("no numbers here") == 0


def test_title_scoring_tiers():
    assert score_title(PROFILE, "AI Engineer")[0] == 1.0
    assert score_title(PROFILE, "Analytics Consultant")[0] >= 0.5
    assert score_title(PROFILE, "Warehouse Associate")[0] < 0.5
