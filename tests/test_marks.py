from app.marks import icon, marked


def test_category_marks_are_stable():
    assert icon("job_alert") == "📬"
    assert marked("interview_invite").startswith("📅")
    assert "Interview" in marked("interview_invite")


def test_nav_and_status_marks():
    assert icon("followups", "nav") == "👋"
    assert "Saved" in marked("saved", "job")
    assert icon("error", "severity") == "⛔"
