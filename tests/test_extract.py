from pathlib import Path

import base64

from app.email_parse import ParsedEmail, extract_links, html_to_text
from app.extract_jobs import canonical_key, extract_from_email, is_job_url, source_of, unwrap_url

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
    assert not is_job_url(
        "https://t1.em.linkedin.com/r/?id=h3f7691e6-f0e8-4a05-a0b7-0d2f79f8be70,a5e21a,b866ed"
    )


def test_linkedin_premium_gift_email_is_not_jobs():
    html = """
    <a href="https://t1.em.linkedin.com/r/?id=aaa">Gift 2 free months</a>
    <a href="https://t1.em.linkedin.com/r/?id=bbb">Share now</a>
    <a href="https://t1.em.linkedin.com/r/?id=ccc">Learn why we included this.</a>
    """
    email = ParsedEmail(
        id="li-gift",
        sender_email="linkedin@em.linkedin.com",
        subject="Gift your friends 2 free months of Premium",
        html=html,
    )
    assert extract_from_email(email) == []


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


def test_adzuna_digest_urls_are_postings():
    url = "https://www.adzuna.com/land/ad/5123456789"
    assert source_of(url) == "adzuna"
    assert is_job_url(url)
    assert canonical_key(url) == "adzuna:5123456789"


def test_base64_encoded_redirects_are_unwrapped():
    target = "https://jobs.lever.co/acme/7fca4a70-174c-41a2-b44b-7ff1cb9422e7"
    blob = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    assert unwrap_url(f"https://email.acme.com/c/{blob}") == target


def test_glassdoor_partner_listings_are_jobs():
    url = (
        "https://www.glassdoor.com/partner/jobListing.htm?pos=101&ao=1"
        "&guid=000001a06800314da091717e3683952a&src=GD_JOB_AD"
    )
    assert is_job_url(url)
    assert canonical_key(url).startswith("glassdoor:000001a06800314da091717e3683952a")


def test_haystack_go_links_stay_haystack_and_unsubscribe_is_ignored():
    apply_url = (
        "https://haystack.cv/go?j=af2a1266-137b-4e28-b725-a56b86fc134d&s=searches-email"
        "&u=https%3A%2F%2Fclick.appcast.io%2Ft%2Fabc"
    )
    assert unwrap_url(apply_url).startswith("https://haystack.cv/go?j=af2a1266")
    assert is_job_url(apply_url)
    assert canonical_key(apply_url) == "haystack:af2a1266-137b-4e28-b725-a56b86fc134d"
    assert not is_job_url(
        "https://haystack.cv/unsubscribe?token=eyJpZCI6ImMwMjcyIn0"
    )


def test_aws_ses_click_wrapper_unwraps_builtin():
    wrapped = (
        "https://cb4sdw3d.r.us-west-2.awstrack.me/L0/"
        "https:%2F%2Fbuiltin.com%2Fjob%2Fassociate-ai-ml-engineer%2F10961329%3Fi=abc/1/010101"
    )
    dest = unwrap_url(wrapped)
    assert "builtin.com/job/associate-ai-ml-engineer/10961329" in dest
    assert is_job_url(wrapped)
    assert canonical_key(wrapped) == "builtin:10961329"


def test_appcast_sendgrid_cards_explode_into_jobs():
    html = """
    <a href="https://u14935226.ct.sendgrid.net/ls/click?upn=AAA111">
      New
      Olympia, WA
      Research Specialist III
      Would you like to fill an important role
      Apply Now
    </a>
    <a href="https://u14935226.ct.sendgrid.net/ls/click?upn=BBB222">
      New
      Pleasanton, CA
      Research Specialist II, Research Support
      Job Summary: Assists with research
      Apply Now
    </a>
    <a href="https://u14935226.ct.sendgrid.net/ls/click?upn=CCC333">View All Jobs</a>
    <a href="https://u14935226.ct.sendgrid.net/ls/click?upn=DDD444">unsubscribe</a>
    """
    email = ParsedEmail(
        id="kaiser",
        sender_email="kaiser-permanente@hiring.appcast.io",
        subject="New job matches for you at Kaiser Permanente",
        html=html,
    )
    jobs = extract_from_email(email)
    titles = {job.title for job in jobs}
    assert "Research Specialist III" in titles
    assert "Research Specialist II, Research Support" in titles
    assert len(jobs) == 2
    assert all(job.source == "appcast" for job in jobs)
    assert all(job.company == "Kaiser Permanente" for job in jobs)
    assert {job.location for job in jobs} == {"Olympia, WA", "Pleasanton, CA"}


def test_appcast_apply_button_next_to_the_card_still_counts():
    html = """
    <td>
      New
      Leesburg, VA
      Senior AI Engineer
      Logistics at full potential. At GXO, we are looking for talent.
      <a href="https://u14935226.ct.sendgrid.net/ls/click?upn=GXO111">Apply Now</a>
    </td>
    <td>
      New
      Bristow, VA
      Yard & Dock Coordinator - 3rd Shift
      Logistics at full potential. At GXO, we are looking for talent.
      <a href="https://u14935226.ct.sendgrid.net/ls/click?upn=GXO222">Apply Now</a>
    </td>
    """
    email = ParsedEmail(
        id="gxo",
        sender_name="Careers at GXO",
        sender_email="gxo@hiring.appcast.io",
        subject="New job matches for you at GXO",
        html=html,
    )
    jobs = extract_from_email(email)
    assert {job.title for job in jobs} == {
        "Senior AI Engineer",
        "Yard & Dock Coordinator - 3rd Shift",
    }
    assert all(job.company == "GXO" for job in jobs)


def test_chewy_career_alert_uses_company_not_opportunities():
    html = """
    <td>
      Research Scientist II
      Bellevue, WA
      <a href="https://u14935226.ct.sendgrid.net/ls/click?upn=CHEWY1">Apply Now</a>
    </td>
    <td>
      Machine Learning Engineer III
      USA - WA - Bellevue - SEA1
      <a href="https://u14935226.ct.sendgrid.net/ls/click?upn=CHEWY2">Apply Now</a>
    </td>
    """
    email = ParsedEmail(
        id="chewy",
        sender_name="Chewy Inc",
        sender_email="opportunities@careeralerts.chewy.com",
        subject="Naveen: Job Alerts for you from Chewy",
        html=html,
    )
    jobs = extract_from_email(email)
    assert {job.title for job in jobs} == {
        "Research Scientist II",
        "Machine Learning Engineer III",
    }
    assert all(job.company == "Chewy" for job in jobs)


def test_glassdoor_empty_card_links_keep_title_and_company():
    html = """
    <table>
      <tr><td>Enterprise Mobility</td><td>3.5 ★</td></tr>
      <tr><td>Data Scientist</td></tr>
      <tr><td>Saint Louis, MO</td></tr>
      <tr><td>$105K – $151K</td></tr>
      <tr><td><a href="https://www.glassdoor.com/partner/jobListing.htm?pos=101&guid=aaa111&jobListingId=1010227893844"></a></td></tr>
    </table>
    <table>
      <tr><td>Skechers USA, Inc.</td></tr>
      <tr><td>Data Scientist</td></tr>
      <tr><td>Hermosa Beach, CA</td></tr>
      <tr><td><a href="https://www.glassdoor.com/partner/jobListing.htm?pos=101&guid=bbb222&jobListingId=1010229851847"></a></td></tr>
    </table>
    """
    email = ParsedEmail(
        id="gd",
        sender_email="noreply@glassdoor.com",
        subject="Your job search",
        html=html,
    )
    jobs = {job.company: job for job in extract_from_email(email)}
    assert jobs["Enterprise Mobility"].title == "Data Scientist"
    assert "Saint Louis" in jobs["Enterprise Mobility"].location
    assert jobs["Skechers USA, Inc."].title == "Data Scientist"
    assert "Hermosa Beach" in jobs["Skechers USA, Inc."].location


def test_haystack_digest_keeps_every_apply_card():
    html = """
    <a href="https://haystack.cv/go?j=11111111-1111-1111-1111-111111111111&u=https%3A%2F%2Fclick.appcast.io%2Ft%2Fa">
      Data Scientist, Mid
      BOOZ, ALLEN & HAMILTON, INC.
      Annandale, United States
      USD 77,600 - 176,000/yr
      Apply Now
    </a>
    <a href="https://haystack.cv/go?j=22222222-2222-2222-2222-222222222222&u=https%3A%2F%2Fclick.appcast.io%2Ft%2Fb">
      AI Developer
      Brooksource
      United States
      USD 45 - 60/hr
      Apply Now
    </a>
    <a href="https://haystack.cv/unsubscribe?token=abc">Unsubscribe from these emails</a>
    """
    email = ParsedEmail(
        id="hay",
        sender_email="alerts@alerts.haystack.cv",
        subject="5 New Jobs Matching Your Search",
        html=html,
    )
    jobs = extract_from_email(email)
    assert len(jobs) == 2
    assert {job.title for job in jobs} == {"Data Scientist, Mid", "AI Developer"}
    assert all(job.source == "haystack" for job in jobs)
    booz = next(job for job in jobs if job.title == "Data Scientist, Mid")
    assert booz.company == "BOOZ, ALLEN & HAMILTON, INC."
    assert "Annandale" in booz.location
    assert "USD" not in booz.location


def test_inline_salary_does_not_drop_the_title():
    html = """
    <a href="https://haystack.cv/go?j=33333333-3333-3333-3333-333333333333">
      Data Scientist, Product Analytics $88 W2 *** Direct end client ***
      Projas Technologies, LLC
      San Diego, United States
      Apply Now
    </a>
    """
    email = ParsedEmail(
        id="hay2",
        sender_email="alerts@alerts.haystack.cv",
        subject="Projas is hiring",
        html=html,
    )
    jobs = extract_from_email(email)
    assert len(jobs) == 1
    assert jobs[0].title.startswith("Data Scientist, Product Analytics")
    assert "Projas" in jobs[0].company


def test_jobs2web_vanity_dot_jobs_urls_are_postings():
    url = "http://komatsu.jobs/job/Human-Resources-Manager/36799-en_US/?from=email"
    assert source_of(url) == "jobs2web"
    assert is_job_url(url)
    assert canonical_key(url).startswith("jobs2web:komatsu.jobs/job/Human-Resources-Manager")
    assert not is_job_url("http://komatsu.jobs/?from=email")
    assert not is_job_url("https://komatsu.jobs/unsubscribe/?from=email")


def test_adzuna_alert_settings_are_not_jobs():
    url = "https://www.adzuna.com/my-alerts?id=127403108&vhash=abc"
    assert not is_job_url(url)


def test_jobs2web_digest_keeps_each_titled_link():
    html = """
    <p>Your Job Alert matched the following jobs at komatsu.jobs.</p>
    <b>Jobs</b>
    <div>
      <a href="http://komatsu.jobs/job/Human-Resources-Manager/36799-en_US/">Human Resources Manager</a>
      <a href="http://komatsu.jobs/job/IT-Cybersecurity-Compliance-Analyst/36849-en_US/">IT Cybersecurity Compliance Analyst</a>
      <a href="http://komatsu.jobs/job/Sr_-Accountant/35250-en_US/">Sr. Accountant</a>
    </div>
    <a href="https://komatsu.jobs/unsubscribe/">Unsubscribe</a>
    """
    email = ParsedEmail(
        id="komatsu",
        sender_email="komatsuam-jobnotification@noreply.jobs2web.com",
        subject="New jobs posted from komatsu.jobs",
        html=html,
    )
    jobs = extract_from_email(email)
    assert {job.title for job in jobs} == {
        "Human Resources Manager",
        "IT Cybersecurity Compliance Analyst",
        "Sr. Accountant",
    }
    assert all(job.company == "Komatsu" for job in jobs)


def test_jobs2web_accountant_and_strategist_titles_are_kept():
    html = """
    <a href="http://careers.hexion.com/job/Sourcing-Manager/1/">Sourcing Manager - Columbus, OH, US</a>
    <a href="http://careers.hexion.com/job/Accountant-Financial/2/">Accountant, Financial Shared Services - Columbus, OH, US</a>
    <a href="http://careers.hexion.com/job/Senior-Content-Strategist/3/">Senior Content Strategist - Columbus, OH, US</a>
    """
    email = ParsedEmail(
        id="hexion",
        sender_email="hexioncareers-jobnotification@noreply.jobs2web.com",
        subject="New jobs posted from careers.hexion.com",
        html=html,
    )
    titles = {job.title.split(" - ")[0] for job in extract_from_email(email)}
    assert titles == {
        "Sourcing Manager",
        "Accountant, Financial Shared Services",
        "Senior Content Strategist",
    }


def test_workday_link_text_is_kept_as_the_title():
    html = """
    <p>Please review the jobs below.</p>
    <a href="https://td.wd3.myworkdayjobs.com/TD_Bank_Careers/job/Metuchen-New-Jersey/Premier-Banker--Metuchen_R_1488093?shared_id=abc">Premier Banker- Metuchen</a> (Metuchen, New Jersey)<br>
    <a href="https://td.wd3.myworkdayjobs.com/TD_Bank_Careers/job/North-Hampton-New-Hampshire/Store-Manager-II---North-Hampton_R_1492308?shared_id=abc">Store Manager II - North Hampton</a> (North Hampton, New Hampshire)
    """
    email = ParsedEmail(
        id="td",
        sender_name="TD",
        sender_email="TD@myworkday.com",
        subject="TD Career Alerts: Potential roles for you",
        html=html,
    )
    jobs = extract_from_email(email)
    titles = {job.title for job in jobs}
    assert "Premier Banker- Metuchen" in titles
    assert "Store Manager II - North Hampton" in titles
    assert "Premier" not in {job.company for job in jobs}


def test_workday_title_spans_are_joined():
    html = """
    <a href="https://nelnet.wd1.myworkdayjobs.com/MyNelnet/job/Lincoln-NE/IT-Manager---Infrastructure-HelpDesk--Tier-II_R22979-1?shared_id=abc"><span>IT</span><span>Manager - Infrastructure HelpDesk- Tier II</span></a>
    <a href="https://nelnet.wd1.myworkdayjobs.com/MyNelnet/job/Lincoln-NE/Nelnet-Customer-Service-Representative---Lincoln---NE---October--2026-start-date_R23077-1?shared_id=abc">Nelnet Customer Service Representative - Lincoln, NE - October 2026 start date</a>
    """
    email = ParsedEmail(
        id="nelnet",
        sender_name="Nelnet Talent Acquisition",
        sender_email="nelnet@myworkday.com",
        subject="Good News: A Nelnet Job Match Has Arrived",
        html=html,
    )
    jobs = extract_from_email(email)
    titles = {job.title for job in jobs}
    assert "IT Manager - Infrastructure HelpDesk- Tier II" in titles
def test_workday_wrapped_title_newlines_are_collapsed():
    html = """
    <a href="https://td.wd3.myworkdayjobs.com/TD_Bank_Careers/job/Metuchen-New-Jersey/Premier-Banker--Metuchen_R_1?shared_id=abc">Premier
Banker- Metuchen</a>
    """
    email = ParsedEmail(
        id="td2",
        sender_email="TD@myworkday.com",
        subject="TD Career Alerts: Potential roles for you",
        html=html,
    )
    jobs = extract_from_email(email)
    assert jobs[0].title == "Premier Banker- Metuchen"


def test_sibling_job_titles_are_not_stolen_from_the_parent():
    html = """
    <td>
      <a href="http://jobs.doosan.com/bobcat/job/Minneapolis-Software-Engineer-MN-55402/1406069400/">Software Engineer - Minneapolis, MN, US, 55402</a><br>
      <a href="http://jobs.doosan.com/bobcat/job/Buford-Data-Science-Student-Experience-Spring-2027-ND-30518/1426195100/">Data Science Student Experience - Spring 2027 - Buford, US, 30518</a>
    </td>
    """
    email = ParsedEmail(
        id="doosan",
        sender_email="doosan-jobnotification@noreply.jobs2web.com",
        subject="New jobs posted from Doosan",
        html=html,
    )
    jobs = {job.url_key: job for job in extract_from_email(email)}
    assert len(jobs) == 2
    data = next(job for job in jobs.values() if "Data-Science" in job.url)
    assert data.title.startswith("Data Science Student Experience")
    assert data.title != "Software Engineer - Minneapolis, MN, US, 55402"


def test_unlimited_limit_keeps_every_digest_card():
    cards = []
    for i in range(45):
        cards.append(
            f'<a href="https://haystack.cv/go?j=aaaaaaaa-bbbb-cccc-dddd-{i:012d}">'
            f"Role {i} Engineer</a>"
        )
    email = ParsedEmail(
        id="many",
        sender_email="alerts@alerts.haystack.cv",
        subject="45 New Jobs Matching Your Search",
        html="\n".join(cards),
    )
    assert len(extract_from_email(email)) == 25
    assert len(extract_from_email(email, limit=None)) == 45


def test_jobright_info_urls_are_postings():
    url = "https://jobright.ai/jobs/info/6aa0543e3b5aa83237b08694?utm_source=email"
    assert source_of(url) == "jobright"
    assert is_job_url(url)
    assert canonical_key(url) == "jobright:6aa0543e3b5aa83237b08694"
    assert not is_job_url("https://jobright.ai/?utm_source=email")


def test_jobright_digest_keeps_each_apply_card():
    html = """
    <a href="https://jobright.ai/jobs/info/aaa111111111111111111111">
      Apple
      Apps · Public Company
      97%
      Simulation Engineer
      Cupertino, CA
      APPLY NOW
    </a>
    <a href="https://jobright.ai/jobs/info/bbb222222222222222222222">Data Scientist</a>
    <a href="https://jobright.ai/jobs/info/bbb222222222222222222222">APPLY NOW</a>
    <a href="https://jobright.ai/?utm_source=email">View More Opportunities</a>
    """
    email = ParsedEmail(
        id="jobright",
        sender_name="Jobright Job Alert",
        sender_email="noreply@jobright.ai",
        subject="Your Top Job matches from Jobright",
        html=html,
    )
    jobs = extract_from_email(email)
    assert {job.title for job in jobs} == {"Simulation Engineer", "Data Scientist"}
    assert all(job.source == "jobright" for job in jobs)
    apple = next(job for job in jobs if job.title == "Simulation Engineer")
    assert apple.company == "Apple"
    assert "Cupertino" in apple.location


def test_avature_job_title_links_are_kept():
    html = """
    <p>We've identified jobs that match your alert preferences!</p>
    <p>Job Matches:
      <a href="http://ea.avature.net/ltrk/aaa111">Live Service Design Director</a>
      <a href="http://ea.avature.net/ltrk/bbb222">Gameplay Data Science Engineer (Apex Legends)</a>
      <a href="http://ea.avature.net/ltrk/ccc333">Technical Director</a>
      <a href="http://ea.avature.net/ltrk/ddd444">Technical Program Manager - Enterprise Intelligence</a>
    </p>
    <a href="http://ea.avature.net/ltrk/eee555">here</a>
    <a href="http://ea.avature.net/ltrk/fff666">update your profile</a>
    <a href="https://ea.avature.net/unsubscribe.php?uid=x">Unsubscribe</a>
    """
    email = ParsedEmail(
        id="ea",
        sender_email="jobnotification@ea.avature.net",
        subject="New Job Openings at Electronic Arts!",
        html=html,
    )
    jobs = extract_from_email(email)
    assert {job.title for job in jobs} == {
        "Live Service Design Director",
        "Gameplay Data Science Engineer (Apex Legends)",
        "Technical Director",
        "Technical Program Manager - Enterprise Intelligence",
    }
    assert all(job.source == "avature" for job in jobs)
    assert all(job.company == "Electronic Arts" for job in jobs)


def test_simplify_click_cards_are_kept():
    html = """
    <a href="https://email.matches.simplify.jobs/c/eJxAAA111">
      Intercontinental Exchange
      Full-Time
      Operates global exchanges, data, and connectivity
      Data Scientist
      Atlanta, GA, USA
    </a>
    <a href="https://email.matches.simplify.jobs/c/eJxBBB222">
      Capgemini
      Full-Time
      Global consulting, technology services, outsourcing
      Associate Data Scientist
      $60k – $80k
      Toronto, ON, Canada
    </a>
    <a href="https://email.matches.simplify.jobs/c/eJxCCC333">View Matches</a>
    <a href="https://email.matches.simplify.jobs/u/unsub">Unsubscribe</a>
    """
    email = ParsedEmail(
        id="simplify",
        sender_name="Simplify",
        sender_email="matches@simplify.jobs",
        subject="New matches from Intercontinental Exchange & more",
        html=html,
    )
    jobs = extract_from_email(email)
    assert {job.title for job in jobs} == {"Data Scientist", "Associate Data Scientist"}
    assert all(job.source == "simplify" for job in jobs)
    ice = next(job for job in jobs if job.title == "Data Scientist")
    assert "Intercontinental" in ice.company
    assert "Atlanta" in ice.location


def test_simplify_overlay_links_read_the_previous_card():
    html = """
    <td>
      <table>
        <tr><td>
          <p style="font-weight:700">Intercontinental Exchange</p>
          <p>Full-Time</p>
          <p>Operates global exchanges, data, and connectivity</p>
          <p style="font-weight:700">Data Scientist</p>
          <p>Atlanta, GA, USA</p>
        </td></tr>
      </table>
      <div>&nbsp;</div>
      <a href="https://email.matches.simplify.jobs/c/eJxAAA111"></a>
      <table>
        <tr><td>
          <p style="font-weight:700">Capgemini</p>
          <p>Full-Time</p>
          <p>Global consulting, technology services, outsourcing</p>
          <p style="font-weight:700">Associate Data Scientist</p>
          <p>$60k – $80k</p>
          <p>Toronto, ON, Canada</p>
        </td></tr>
      </table>
      <div>&nbsp;</div>
      <a href="https://email.matches.simplify.jobs/c/eJxBBB222"></a>
      <table>
        <tr><td>
          <p style="font-weight:700">The Walt Disney Company</p>
          <p>Full-Time</p>
          <p>Diversified entertainment conglomerate: media, parks, streaming</p>
          <p style="font-weight:700">Software Engineer 2</p>
          <p>Glendale, CA, USA</p>
        </td></tr>
      </table>
    </td>
    """
    email = ParsedEmail(
        id="simplify-overlay",
        sender_email="matches@simplify.jobs",
        subject="New matches from Intercontinental Exchange & more",
        html=html,
    )
    jobs = extract_from_email(email)
    assert {job.title for job in jobs} == {
        "Data Scientist",
        "Associate Data Scientist",
        "Software Engineer 2",
    }
    ice = next(job for job in jobs if job.title == "Data Scientist")
    assert "Intercontinental" in ice.company
    assert "Atlanta" in ice.location


def test_section_heading_jobs_is_not_a_posting():
    html = """
    <a href="https://u14935226.ct.sendgrid.net/ls/click?upn=WC1">A.I. Solutions Architect</a>
    <a href="https://u14935226.ct.sendgrid.net/ls/click?upn=WC2">Data Scientist Jobs</a>
    """
    email = ParsedEmail(
        id="wc",
        sender_name="Waste Connections",
        sender_email="opportunities@careeralerts.wasteconnections.com",
        subject="Naveen: We've got a new job for you!",
        html=html,
    )
    jobs = extract_from_email(email)
    assert {job.title for job in jobs} == {"A.I. Solutions Architect"}


def test_indeed_cts_tracker_keeps_the_job_title():
    html = """
    <a href="https://cts.indeed.com/v3/abc111">View job</a>
    <a href="https://cts.indeed.com/v3/abc111">Healthcare Data Scientist</a>
    <a href="https://cts.indeed.com/v3/abc111">Learn more</a>
    <a href="https://cts.indeed.com/v3/abc111">This is a bad match</a>
    <a href="https://cts.indeed.com/v3/copy">2026 Indeed, Inc.</a>
    """
    email = ParsedEmail(
        id="indeed-cts",
        sender_email="donotreply@match.indeed.com",
        subject="Healthcare Data Scientist @ EPIC Brokers",
        html=html,
    )
    jobs = extract_from_email(email)
    assert len(jobs) == 1
    assert jobs[0].title == "Healthcare Data Scientist"
    assert jobs[0].company == "EPIC Brokers"
    assert "2026 Indeed, Inc." not in {job.title for job in jobs}


def test_glassdoor_see_more_and_create_links_are_dropped():
    html = """
    <a href="https://www.glassdoor.com/partner/jobListing.htm?pos=101&guid=aaa&jobListingId=1">Data Scientist</a>
    <a href="https://www.glassdoor.com/partner/jobListing.htm?pos=102&guid=bbb&jobListingId=2">See more jobs</a>
    <a href="https://www.glassdoor.com/partner/jobListing.htm?pos=103&guid=ccc&jobListingId=3">Create</a>
    """
    email = ParsedEmail(
        id="gd-footer",
        sender_email="noreply@glassdoor.com",
        subject="Data Scientist at EXL Service and 8 more jobs in New York, NY for you.",
        html=html,
    )
    jobs = extract_from_email(email)
    assert {job.title for job in jobs} == {"Data Scientist"}


def test_monster_click_tracker_keeps_titled_cards():
    html = """
    <a href="http://click.monster.com/f/a/aaa~~/job1">Associate Data Scientist</a>
    <a href="http://click.monster.com/f/a/aaa~~/job1">VIEW JOB</a>
    <a href="http://click.monster.com/f/a/bbb~~/job2">Data Scientist II</a>
    <a href="http://click.monster.com/f/a/bbb~~/job2">VIEW JOB</a>
    <a href="http://click.monster.com/f/a/ccc~~/pref">here</a>
    """
    email = ParsedEmail(
        id="monster-click",
        sender_email="monster@notifications.monster.com",
        subject="Your job alert for: Data Scientist,",
        html=html,
    )
    jobs = extract_from_email(email)
    assert {job.title for job in jobs} == {"Associate Data Scientist", "Data Scientist II"}


def test_ihire_track_links_keep_the_job_title():
    html = """
    <a class="link" href="https://track.ihire.com/ss/c/u001.abc">Immediate need for the ASP .NET Developer</a>
    <a href="https://track.ihire.com/ss/c/u001.abc">View</a>
    <a class="link" href="https://track.ihire.com/ss/c/u001.def">IS Data Center Intern</a>
    <a href="https://track.ihire.com/ss/c/pref">Email Preferences</a>
    """
    email = ParsedEmail(
        id="ihire-track",
        sender_email="jobseekers@email.ihire.com",
        subject="[Work Your Way Job Alert] Immediate need for the ASP .NET Developer",
        html=html,
    )
    jobs = extract_from_email(email)
    assert {job.title for job in jobs} == {
        "Immediate need for the ASP .NET Developer",
        "IS Data Center Intern",
    }
    assert "Email Preferences" not in {job.title for job in jobs}


def test_pageup_sendgrid_click_keeps_titled_jobs():
    html = """
    <a href="http://sendgrid.email.pageuppeople.com/ls/click?upn=aaa">IT Manager</a>
    <a href="http://sendgrid.email.pageuppeople.com/ls/click?upn=bbb">Director of Research Technology Services</a>
    <a href="http://sendgrid.email.pageuppeople.com/ls/click?upn=ccc">Unsubscribe</a>
    """
    email = ParsedEmail(
        id="pageup-click",
        sender_email="jobalerts-967@mail.pageuppeople.com",
        subject="Job Alerts Email",
        html=html,
    )
    jobs = extract_from_email(email)
    assert {job.title for job in jobs} == {
        "IT Manager",
        "Director of Research Technology Services",
    }


def test_jobs2web_title_with_zip_is_kept():
    html = """
    <a href="http://jobs.enersys.com/job/Warrensburg-Process-Engineer-MO-64093-9301/1411902633/">Process Engineer - Warrensburg, MO, US, 64093-9301</a>
    <a href="https://jobs.enersys.com/unsubscribe/">Unsubscribe</a>
    """
    email = ParsedEmail(
        id="enersys",
        sender_email="enersysdp2-jobnotification@noreply12.jobs2web.com",
        subject="New jobs posted from jobs.enersys.com",
        html=html,
    )
    jobs = extract_from_email(email)
    assert len(jobs) == 1
    assert jobs[0].title.startswith("Process Engineer")


def test_twine_cl0_redirect_keeps_project_cards():
    html = """
    <a href="https://redirect.twinehq.com/CL0/https:%2F%2Fwww.twine.net%2Fprojects%2Fb9qbr0-absolute-workforce/1/abc">Absolute Workforce LLC - Lead Data Scientist</a>
    <a href="https://redirect.twinehq.com/CL0/https:%2F%2Fwww.twine.net%2Fprojects%2Fb9qbr0-absolute-workforce/1/abc">Apply</a>
    <a href="https://redirect.twinehq.com/CL0/https:%2F%2Fwww.twine.net%2Fprojects%2Fb9q9n0-senior-full/1/abc">Senior Full Stack Engineer</a>
    """
    email = ParsedEmail(
        id="twine",
        sender_email="no-reply@twinehq.com",
        subject="5 new job opportunities posted in the past 24 hours",
        html=html,
    )
    jobs = extract_from_email(email)
    assert {job.title for job in jobs} == {
        "Absolute Workforce LLC - Lead Data Scientist",
        "Senior Full Stack Engineer",
    }


def test_appcast_location_then_title_without_role_word():
    html = """
    <a href="https://u14935226.ct.sendgrid.net/ls/click?upn=SAF1">
      Grove City, OH
      Customer Advocate
      The Customer Advocate is one of Safelite first impressions
    </a>
    <a href="https://u14935226.ct.sendgrid.net/ls/click?upn=SAF2">View All Jobs</a>
    """
    email = ParsedEmail(
        id="safelite",
        sender_email="safelite@hiring.appcast.io",
        subject="Naveen: New job matches for you at Safelite.",
        html=html,
    )
    jobs = extract_from_email(email)
    assert len(jobs) == 1
    assert jobs[0].title == "Customer Advocate"
    assert jobs[0].company == "Safelite"


def test_zoom_clinchtalent_unwraps_careers_links():
    html = """
    <td>
      <strong>Principal Agentic AI Engineer</strong>
      <p>Home Office, Downtown Seattle, Seattle, Washington, United States, 99999</p>
      <a href="https://api.clinchtalent.com/v1/public/messages/abc/events/clicked?url=https%3A%2F%2Fcareers.zoom.us%2Fjobs%2Fprincipal-agentic-ai-engineer-seattle-washington-united-states">Read More</a>
    </td>
    <a href="https://api.clinchtalent.com/v1/public/messages/abc/events/clicked?url=https%3A%2F%2Fwww.zoom.com">55 Almaden Blvd San Jose, CA 95113</a>
    """
    email = ParsedEmail(
        id="zoom-alert",
        sender_email="talent.acquisition@zoom.com",
        subject="Your Zoom Job Alerts",
        html=html,
    )
    jobs = extract_from_email(email)
    assert len(jobs) == 1
    assert jobs[0].title == "Principal Agentic AI Engineer"
    assert "careers.zoom.us" in jobs[0].url

