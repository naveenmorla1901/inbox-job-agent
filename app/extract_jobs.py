from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from .email_parse import ParsedEmail, clean_text


def _urlparse(url: str):
    """urlparse that never raises on malformed input.

    Real emails carry broken hrefs (unbalanced IPv6 brackets, stray characters)
    that make urlparse raise ValueError; one bad link must not abort extraction
    for the whole message."""
    try:
        return urlparse(url or "")
    except ValueError:
        return urlparse("")

# Hosts that host actual postings. Anything else in an email is ignored.
JOB_HOSTS = {
    "linkedin.com": "linkedin",
    "indeed.com": "indeed",
    "glassdoor.com": "glassdoor",
    "ziprecruiter.com": "ziprecruiter",
    "dice.com": "dice",
    "monster.com": "monster",
    "simplyhired.com": "simplyhired",
    "builtin.com": "builtin",
    "wellfound.com": "wellfound",
    "angel.co": "wellfound",
    "joinhandshake.com": "handshake",
    "lever.co": "lever",
    "greenhouse.io": "greenhouse",
    "ashbyhq.com": "ashby",
    "myworkdayjobs.com": "workday",
    "smartrecruiters.com": "smartrecruiters",
    "workable.com": "workable",
    "icims.com": "icims",
    "taleo.net": "taleo",
    "jobvite.com": "jobvite",
    "bamboohr.com": "bamboohr",
    "breezy.hr": "breezy",
    "rippling.com": "rippling",
    "eightfold.ai": "eightfold",
    "successfactors.com": "successfactors",
    "hired.com": "hired",
    "otta.com": "otta",
    "remoteok.com": "remoteok",
    "weworkremotely.com": "weworkremotely",
    "y-combinator.com": "ycombinator",
    "adzuna.com": "adzuna",
    "adzuna.co.uk": "adzuna",
    "experteer.com": "experteer",
    "joinhaystack.com": "haystack",
    "haystack.cv": "haystack",
    "haystackapp.io": "haystack",
    "appcast.io": "appcast",
    "click.appcast.io": "appcast",
    "jobs2web.com": "jobs2web",
    "njoyn.com": "njoyn",
    "myworkday.com": "workday",
    "myworkdaysite.com": "workday",
    "wd1.myworkdayjobs.com": "workday",
    "wd5.myworkdayjobs.com": "workday",
    "jobright.ai": "jobright",
    "jobright.com": "jobright",
    "avature.net": "avature",
    "simplify.jobs": "simplify",
    "ihire.com": "ihire",
    "twine.net": "twine",
    "zoom.us": "zoom",
}

# URL paths that look like a single posting rather than a search page.
POSTING_PATH = re.compile(
    r"/(jobs?/view|viewjob|job-listing|job-detail|jobs?/\d+|rc/clk|pagead/clk|"
    r"jobs?/[a-z0-9-]+/[0-9a-f-]{8,}|careers?/job|job/|opportunit|job_app|"
    r"partner/joblisting|remote-jobs?/[a-z0-9][a-z0-9-]{3,}|go$)",
    re.I,
)
# Same idea, but strict enough to trust on a company site we have never seen before.
STRONG_POSTING_PATH = re.compile(
    r"/(jobs?/view/|viewjob|job-listing|job-detail|job_app|"
    r"(jobs?|careers?|positions?|openings?|vacanc(y|ies))/[^/]*\d{3,}|"
    r"(jobs?|careers?|positions?|openings?)/[a-z0-9-]+/[0-9a-f-]{8,}|"
    r"job/[^/]+/\d[\w.-]*)",
    re.I,
)
# SuccessFactors / jobs2web vanity URLs: /job/Title-Slug/36799-en_US
JOBS2WEB_PATH = re.compile(r"/job/[^/]+/[^/]+", re.I)
# Lever and Ashby address postings by bare UUID: /<company>/<uuid>.
UUID_PATH = re.compile(
    r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)
SEARCH_PATH = re.compile(
    r"/(jobs/search|q-|browse|alerts?|my-alerts|unsubscribe|settings|help|feed|privacy|"
    r"interview/index|community/bowl|profile/job-preferences)",
    re.I,
)
# Query params that identify a specific posting. "id"/"token" alone are too generic.
ID_PARAMS = (
    "jk",
    "gh_jid",
    "jobid",
    "jobId",
    "jobListingId",
    "currentJobId",
    "requisitionid",
    "vjk",
    "guid",
    "j",
)
WEAK_ID_PARAMS = ("id",)

FOOTER_TEXT = re.compile(
    r"^(unsubscribe|privacy policy|view all jobs|see more jobs|update your preferences|"
    r"update your profile|browse all|post a job|get more recommendations|we want to know|"
    r"adjust your job alert notifications?|this is a bad match)$",
    re.I,
)
CLICK_HOST_HINT = (
    "ct.sendgrid.net",
    "sendgrid.email",
    "click.appcast.io",
    "awstrack.me",
    "click.mailer.io",
    "mandrillapp.com",
    "avature.net",
    "simplify.jobs",
    "cts.indeed.com",
    "click.monster.com",
    "track.ihire.com",
    "pageuppeople.com",
    "redirect.twinehq.com",
    "api.clinchtalent.com",
)

LOCATION_HINT = re.compile(
    r"(remote|hybrid|on-?site|,\s*[A-Z]{2}\b|United States|USA|India|Canada|Europe|"
    r"New York|San Francisco|Seattle|Austin|Boston|Chicago|Dallas|Atlanta|Denver)",
    re.I,
)
NOISE_LINE = re.compile(
    r"^(view job|apply now(?:\s*→)?|see all|actively hiring|easy apply|be an early applicant|"
    r"\d+\s+(school|connection|alum)|promoted|new|technology|full[- ]time|part[- ]time|"
    r"(?:just now|\d+\s+(?:hour|day|week|minute)s? ago)\b.*|"
    r"posted|save|unsubscribe|view all jobs|browse all jobs|"
    r"top match|more details.*|this job is available in multiple locations)$",
    re.I,
)
TITLE_HINT = re.compile(
    r"\b(engineer|scientist|developer|analyst|specialist|architect|researcher|"
    r"consultant|intern|co-?op|coordinator|associate|director|manager|lead|"
    r"principal|staff|accountant|strategist|machine learning|software|research|"
    r"data science|ai/?ml|nlp|llm)\b",
    re.I,
)
# Role words only — "software" in a company tagline is not a job title.
TITLE_ROLE = re.compile(
    r"\b(engineer|scientist|developer|analyst|specialist|architect|researcher|"
    r"consultant|intern|co-?op|coordinator|associate|director|manager|lead|"
    r"principal|staff|accountant|strategist)\b",
    re.I,
)
COMPANY_HINT = re.compile(
    r"\b(inc\.?|llc|ltd\.?|corp\.?|co\.|company|group|university|college|"
    r"laboratories|hospital|partners|holdings|technologies)\b",
    re.I,
)
COMP_LINE = re.compile(
    r"(\$\s?\d|\bUSD\b|\bEUR\b|\bGBP\b|\bCAD\b|"
    r"\d[\d,]+\s*[-–]\s*\d|"
    r"\d+\s*[-–]\s*\d+\s*(k|/yr|/hr|an hour|per year)|"
    r"\d[\d,]+\s+a year)",
    re.I,
)
JUNK_TITLE = re.compile(
    r"(search for more|see the latest|see more jobs?|show me more|view all|browse all|update your|"
    r"unsubscribe|manage settings|privacy policy|^jobs for |^create$|"
    r"^job alert:|.+ jobs$|has not been updated|minimum base pay|job listings|"
    r"improve your alerts|telling us what you.re looking for|"
    r"email preferences|^preferences$|^\d{4}\s+\S+,?\s+inc|"
    r"gift \d+ free|^share now$|^learn why|^help$|^terms and|^conditions:)",
    re.I,
)
APPLY_CTA = re.compile(
    r"^(apply now|apply\b|view job|see job|learn more|read more|"
    r"(?:1|one)[\s-]?click apply|quick apply|easy apply)\b",
    re.I,
)
# A street address, as opposed to a job title that merely ends in a "City, ST[, ZIP]".
# Keyed on a leading street number or a street-type word - NOT a bare ZIP, because
# real jobs2web titles end in "Brentwood, TN, US, 37027".
ADDRESS_LINE = re.compile(
    r"^\d+\s+\w|"
    r"\b(blvd|boulevard|street|st\.|ave|avenue|road|rd\.|suite|ste\.|floor|fl\.|"
    r"drive|dr\.|lane|ln\.|way|parkway|pkwy|plaza|hwy|highway)\b",
    re.I,
)


@dataclass
class JobCandidate:
    url: str
    url_key: str
    title: str = ""
    company: str = ""
    location: str = ""
    source: str = ""
    context: str = ""


# Click-tracker wrappers put the real destination in one of these query params.
REDIRECT_PARAMS = ("url", "u", "redirect", "redirect_url", "target", "destination", "link", "r", "q")
BASE64_URL = re.compile(r"(aHR0c[0-9A-Za-z+/=_-]{10,})")


def unwrap_url(url: str, depth: int = 4) -> str:
    """Peel click-tracking wrappers until the real posting URL is exposed.

    Marketing mail rarely links straight to the job: LinkedIn, SendGrid, Mailchimp and most ATS
    vendors wrap it, sometimes base64-encoded. Without this the scraper fetches a redirect stub
    and de-duplication sees ten different URLs for one posting.
    """
    for _ in range(depth):
        haystack = _haystack_go(url)
        if haystack:
            return haystack

        aws = _encoded_redirect_dest(url)
        if aws and aws != url:
            url = aws
            continue

        parsed = _urlparse(url)
        params = parse_qs(parsed.query or "")
        nested = ""

        # Haystack's `u=` is another click tracker. Keep /go?j= as the identity.
        if host_of(url).endswith("haystack.cv") and params.get("j"):
            return url

        for key in REDIRECT_PARAMS:
            for value in params.get(key, []):
                candidate = unquote(value)
                if candidate.lower().startswith("http"):
                    nested = candidate
                    break
            if nested:
                break

        if not nested:
            match = BASE64_URL.search(url)
            if match:
                blob = match.group(1)
                padded = blob + "=" * (-len(blob) % 4)
                try:
                    decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
                except Exception:
                    decoded = ""
                if decoded.lower().startswith("http"):
                    nested = decoded.split("\x00")[0].strip()

        if not nested or nested == url:
            break
        url = nested
    return url


def _haystack_go(url: str) -> str:
    parsed = _urlparse(url)
    host = host_of(url)
    params = parse_qs(parsed.query or "")
    if host.endswith("haystack.cv") and (parsed.path or "").rstrip("/") == "/go" and params.get("j"):
        job_id = params["j"][0]
        return f"https://haystack.cv/go?j={job_id}"
    return ""


def _encoded_redirect_dest(url: str) -> str:
    """SES L0 and Twine CL0 wraps: .../L0/https:%2F%2Fhost%2Fpath/1/<id>."""
    lower = url.lower()
    for marker in ("/cl0/", "/l0/"):
        idx = lower.find(marker)
        if idx < 0:
            continue
        rest = url[idx + len(marker) :]
        dest = unquote(rest.split("/1/")[0])
        if dest.lower().startswith("http"):
            return dest
    return ""


def host_of(url: str) -> str:
    host = (_urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def source_of(url: str) -> str:
    host = host_of(url)
    for domain, name in JOB_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return name
    if host.endswith(".jobs"):
        return "jobs2web"
    return ""


def is_click_tracker(url: str) -> bool:
    host = host_of(url)
    return any(host == hint or host.endswith("." + hint) or hint in host for hint in CLICK_HOST_HINT)


def is_footer_link(text: str, url: str) -> bool:
    blob = f"{text} {url}".lower()
    if FOOTER_TEXT.match((text or "").strip()):
        return True
    return any(
        token in blob
        for token in (
            "unsubscribe",
            "privacy.htm",
            "update your preferences",
            "view all jobs",
            "/interview/index",
            "community/bowl",
            "job-preferences",
        )
    )


def looks_like_job_card(text: str) -> bool:
    blob = (text or "").strip()
    if len(blob) < 8 or FOOTER_TEXT.match(blob.split("\n", 1)[0].strip()):
        return False
    lines = [ln for ln in blob.split("\n") if ln.strip() and not NOISE_LINE.match(ln.strip())]
    if LOCATION_HINT.search(blob) and len(lines) >= 1:
        return True
    if COMP_LINE.search(blob):
        return True
    if TITLE_HINT.search(blob) and len(lines) >= 1:
        return True
    return False


def is_job_url(url: str) -> bool:
    raw = url
    url = unwrap_url(url)
    parsed = _urlparse(url)
    path = parsed.path or ""
    params = parse_qs(parsed.query or "")
    host = host_of(url)

    if re.search(
        r"/(unsubscribe|settings|privacy|help|interview/index|community/bowl|editagent)",
        path,
        re.I,
    ):
        return False
    # An ATS id in the query is a posting no matter whose domain hosts the page.
    if any(p in params for p in ID_PARAMS):
        return True
    if UUID_PATH.search(path):
        return True
    if host.endswith("haystack.cv") and (path.rstrip("/") == "/go" or "j" in params):
        return True
    if host.endswith("glassdoor.com") and "joblisting.htm" in path.lower():
        return True
    if source_of(url) == "jobright":
        return bool(re.search(r"/jobs/info/[0-9a-f]+", path, re.I))
    if source_of(url) == "ihire" and re.search(r"/jobs/view/\d+", path, re.I):
        return True
    if source_of(url) == "twine" and re.search(r"/projects/[a-z0-9-]+", path, re.I):
        return True
    if source_of(url) == "zoom" and re.search(r"/jobs/[a-z0-9-]+", path, re.I):
        return True
    # ZipRecruiter wraps each posting in a per-job redirect: /ekm/<token> (job view)
    # or /km/<token> (1-Click Apply). The real title rides in the anchor text, so
    # these are postings even though the path carries no numeric id.
    if source_of(url) == "ziprecruiter" and re.search(r"^/e?km/[A-Za-z0-9_-]{10,}", path):
        return True
    if SEARCH_PATH.search(path) and not POSTING_PATH.search(path):
        return False
    if JOBS2WEB_PATH.search(path) and re.search(r"/\d[\w.-]*/?$", path):
        return True
    if not source_of(url):
        return bool(STRONG_POSTING_PATH.search(path))
    if POSTING_PATH.search(path):
        return True
    if source_of(url) == "adzuna" and re.search(r"/\d{5,}", path):
        return True
    if source_of(url) == "builtin" and re.search(r"/job/", path):
        return True
    if host.endswith("em.linkedin.com"):
        return False
    return any(p in params for p in WEAK_ID_PARAMS)


def canonical_key(url: str) -> str:
    """Stable identity for a posting so the same job never lands in the DB twice."""
    url = unwrap_url(url)
    parsed = _urlparse(url)
    host = host_of(url)
    path = (parsed.path or "").rstrip("/")
    params = parse_qs(parsed.query or "")
    source = source_of(url) or host

    if source == "linkedin":
        m = re.search(r"/jobs/view/(?:[^/]*-)?(\d{6,})", path) or re.search(r"(\d{8,})", path)
        if m:
            return f"linkedin:{m.group(1)}"
        if "currentJobId" in params:
            return f"linkedin:{params['currentJobId'][0]}"
    if source == "indeed":
        for key in ("jk", "vjk"):
            if key in params:
                return f"indeed:{params[key][0]}"
    if "gh_jid" in params:
        return f"greenhouse:{params['gh_jid'][0]}"
    if source == "greenhouse":
        if "token" in params:
            return f"greenhouse:{params['token'][0]}"
        m = re.search(r"/jobs/(\d+)", path)
        if m:
            return f"greenhouse:{m.group(1)}"
    if source == "lever":
        m = re.search(r"/([^/]+)/([0-9a-f-]{20,})", path)
        if m:
            return f"lever:{m.group(1)}:{m.group(2)}"
    if source == "adzuna":
        m = re.search(r"/(\d{5,})", path)
        if m:
            return f"adzuna:{m.group(1)}"
    if source == "monster":
        m = re.search(r"/(\d{6,})", path)
        if m:
            return f"monster:{m.group(1)}"
    if source == "haystack":
        if "j" in params:
            return f"haystack:{params['j'][0]}"
    if source == "glassdoor":
        listing = (params.get("jobListingId") or params.get("joblistingid") or [""])[0]
        if listing:
            return f"glassdoor:{listing}"
        guid = (params.get("guid") or [""])[0]
        pos = (params.get("pos") or [""])[0]
        if guid:
            return f"glassdoor:{guid}:{pos}" if pos else f"glassdoor:{guid}"
    if source == "builtin":
        m = re.search(r"/job/[^/]+/(\d+)", path)
        if m:
            return f"builtin:{m.group(1)}"
    if source == "jobright":
        m = re.search(r"/jobs/info/([0-9a-f]+)", path, re.I)
        if m:
            return f"jobright:{m.group(1)}"
    if source == "twine":
        m = re.search(r"/projects/([a-z0-9-]+)", path, re.I)
        if m:
            return f"twine:{m.group(1)}"
    if "sendgrid.net" in host or "sendgrid.email" in host or host.endswith("click.appcast.io"):
        upn = (params.get("upn") or [url])[0]
        return f"appcast:{upn[:80]}"
    if params.get("upn"):
        return f"click:{host}:{params['upn'][0][:80]}"

    for key in (*ID_PARAMS, *WEAK_ID_PARAMS):
        if key in params:
            return f"{source}:{host}{path}:{params[key][0]}"
    return f"{source}:{host}{path}"


def _job_hrefs(node) -> set[str]:
    found: set[str] = set()
    for anchor in node.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href.lower().startswith("http"):
            continue
        text = clean_text(anchor.get_text("\n"))
        if is_footer_link(text, href):
            continue
        if is_job_url(href):
            found.add(canonical_key(href))
        elif is_click_tracker(href):
            found.add(href)
    return found


def _card_node(anchor):
    """Smallest ancestor that still contains only this posting's link."""
    node = anchor
    last = anchor
    for _ in range(10):
        parent = getattr(node, "parent", None)
        if parent is None or getattr(parent, "name", None) in {"body", "html", "[document]"}:
            break
        hrefs = _job_hrefs(parent)
        if len(hrefs) > 1:
            break
        last = parent
        node = parent
    return last


def _container_lines(anchor) -> list[str]:
    node = _card_node(anchor)
    text = clean_text(node.get_text("\n"))
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(lines) >= 2:
        return lines[:16]
    parent = getattr(anchor, "parent", None)
    # A wrapping table cell often lists every posting. Don't steal sibling titles.
    if parent is not None and len(_job_hrefs(parent)) <= 1:
        extra = [ln for ln in clean_text(parent.get_text("\n")).split("\n") if ln.strip()]
        if extra:
            return extra[:16]
    return lines[:16]


def _previous_sibling_card_lines(anchor) -> list[str]:
    """Simplify-style cards: title lives in a table, the apply URL is an empty overlay <a>."""
    prev = getattr(anchor, "previous_sibling", None)
    for _ in range(12):
        while prev is not None and getattr(prev, "name", None) is None:
            prev = getattr(prev, "previous_sibling", None)
        if prev is None:
            break
        text = clean_text(prev.get_text("\n") if hasattr(prev, "get_text") else "")
        if looks_like_job_card(text):
            return [ln for ln in text.split("\n") if ln.strip()][:16]
        prev = getattr(prev, "previous_sibling", None)
    return []


def _add_unlinked_card_tables(
    soup, found: dict[str, JobCandidate], fallback_company: str, limit: int | None
) -> None:
    """Leaf tables that look like job cards but have no apply <a> (last Simplify card)."""
    seen = {(j.title.lower(), (j.company or "").lower()) for j in found.values()}
    for table in soup.find_all("table"):
        if limit is not None and len(found) >= limit:
            return
        if table.find("a", href=True):
            continue
        if table.find("table"):
            inner_cards = 0
            for inner in table.find_all("table"):
                blob = clean_text(inner.get_text("\n"))
                if looks_like_job_card(blob) and TITLE_ROLE.search(blob):
                    inner_cards += 1
            if inner_cards >= 2:
                continue
        lines = [ln for ln in clean_text(table.get_text("\n")).split("\n") if ln.strip()][:16]
        if not looks_like_job_card("\n".join(lines)):
            continue
        title, company, location = _guess_fields("", lines)
        if not title or not TITLE_ROLE.search(title) or JUNK_TITLE.search(title):
            continue
        company = company or fallback_company or ""
        if JUNK_TITLE.search(company or "") or GENERIC_COMPANY.search(company or ""):
            company = fallback_company or ""
        if any(t == title.lower() for t, _ in seen):
            continue
        if not company:
            continue
        key = f"card:{(company or 'job').lower()}:{title.lower()}"[:180]
        source = next((j.source for j in found.values() if j.source), "")
        found[key] = JobCandidate(
            url="",
            url_key=key,
            title=title,
            company=company,
            location=location,
            source=source,
            context=" | ".join(lines)[:600],
        )
        seen.add((title.lower(), company.lower()))


def _strip_prefix(line: str) -> str:
    return re.sub(
        r"^[\W🔥🏢📍💰➜→🏠🇺🇸⭐★·•]+",
        "",
        line,
    ).strip(" 🇺🇸★·•")


def _looks_like_snippet(line: str) -> bool:
    if len(line) > 70 or line.endswith("...") or line.endswith("…"):
        return True
    if "." in line and len(line) > 40:
        return True
    if re.match(r"^(job summary|we are|we.re|would you like|.+ seeks |logistics at)", line, re.I):
        return True
    return line.count(" ") >= 8


def is_location_line(line: str) -> bool:
    if TITLE_HINT.search(line) or COMPANY_HINT.search(line):
        return False
    if re.search(r",\s*[A-Z]{2}\b", line):
        return True
    if re.search(r"^(remote|hybrid|on-?site)\b", line, re.I):
        return True
    if re.search(
        r"\b(united states|canada|india|europe|new york|san francisco|seattle|"
        r"austin|boston|chicago|dallas|atlanta|denver)\b",
        line,
        re.I,
    ) and len(line) < 80:
        return True
    return bool(LOCATION_HINT.search(line) and len(line) < 40)


GENERIC_COMPANY = re.compile(
    r"^(opportunities|careers|jobs|hiring|talent acquisition|job alerts?|"
    r"just posted!?|jobseekers?|posted\b|open to offers|twine team|"
    r"what you can expect)\b",
    re.I,
)
_BOARD_SENDER_NAMES = {
    "linkedin",
    "indeed",
    "glassdoor",
    "haystack",
    "adzuna",
    "monster",
    "workday",
}


def _is_board_sender_name(name: str) -> bool:
    token = (name or "").lower().strip()
    return any(token == board or token.startswith(board + " ") for board in _BOARD_SENDER_NAMES)


def company_from_sender(email: ParsedEmail) -> str:
    name = email.sender_name or ""
    match = re.search(r"(?:careers|jobs|hiring|opportunities)(?:\s+at)?\s+(.+)", name, re.I)
    if match:
        return match.group(1).strip(" .!")[:150]
    match = re.search(r"^(.+?)\s+(?:careers|jobs|hiring)\b", name, re.I)
    if match:
        token = match.group(1).strip(" .!")
        if not _is_board_sender_name(token):
            return token[:150]
    match = re.search(r"new jobs posted from\s+(.+)", email.subject or "", re.I)
    if match:
        token = match.group(1).strip(" .!")
        token = re.sub(r"\.(jobs2web\.com|jobs)$", "", token, flags=re.I)
        return token.replace(".", " ").title()[:150]
    match = re.search(r"(?:job alerts? for you from|alerts? for you from)\s+(.+)", email.subject or "", re.I)
    if match:
        token = match.group(1).strip(" .!")
        if token and not _is_board_sender_name(token):
            return token[:150]
    match = re.search(r"\bat\s+(.+?)(?:\.|$)", email.subject or "", re.I)
    if match:
        token = match.group(1).strip(" .!")
        if not _is_board_sender_name(token) and not re.search(r"\d+\s+more jobs", token, re.I):
            return token[:150]
    match = re.search(r"\s@\s+(.+)$", email.subject or "", re.I)
    if match:
        token = match.group(1).strip(" .!")
        if token and not _is_board_sender_name(token) and not re.search(r"\d+\s+more jobs", token, re.I):
            return token[:150]
    local = (email.sender_email or "").split("@")[0]
    local = re.split(r"[-_.]?(?:am|a)?[-_.]?jobnotification", local, maxsplit=1, flags=re.I)[0]
    if local and not re.search(
        r"noreply|no-reply|alerts?|mailer|jobs?alerts|opportunit|careers?",
        local,
        re.I,
    ):
        return re.sub(r"[._-]+", " ", local).title()[:150]
    token = name.strip(" .")
    if token and not _is_board_sender_name(token) and len(token) < 60:
        return token[:150]
    return ""


def _guess_fields(anchor_text: str, lines: list[str]) -> tuple[str, str, str]:
    blob_lines = [ln.strip() for ln in (anchor_text or "").split("\n") if ln.strip()]
    merged = blob_lines + [ln for ln in lines if ln not in blob_lines]
    useful: list[str] = []
    for ln in merged:
        cleaned = _strip_prefix(ln)
        if not cleaned or NOISE_LINE.match(cleaned):
            continue
        if re.match(r"^\d+\.?\d*\s*★", cleaned):
            continue
        if "·" in cleaned or "•" in cleaned:
            useful.extend(p.strip() for p in re.split(r"[·•]", cleaned) if p.strip())
            continue
        useful.append(cleaned)

    locations, titles, companies = [], [], []
    for ln in useful:
        if _looks_like_snippet(ln) and not TITLE_HINT.search(ln):
            continue
        if APPLY_CTA.match(ln) or FOOTER_TEXT.match(ln):
            continue
        if COMPANY_HINT.search(ln) and not TITLE_HINT.search(ln):
            companies.append(ln)
            continue
        if TITLE_ROLE.search(ln) and len(ln) < 180:
            titles.append(re.split(r"\s*[\$*]", ln, 1)[0].strip()[:140] or ln[:140])
            continue
        if COMP_LINE.search(ln):
            continue
        if is_location_line(ln) and len(ln) < 80:
            locations.append(ln)
            continue
        if len(ln) < 80:
            companies.append(ln)

    title = titles[0] if titles else ""
    company = companies[0] if companies else ""
    location = locations[0] if locations else ""
    if not title and companies:
        title = companies.pop(0)
        company = companies[0] if companies else ""
    if not title:
        for ln in useful:
            if ln not in locations and ln != company and not COMP_LINE.search(ln) and not _looks_like_snippet(ln):
                title = ln
                break
    if title and company and TITLE_HINT.search(company) and not TITLE_HINT.search(title):
        title, company = company, title
    if title and JUNK_TITLE.search(title):
        title = titles[1] if len(titles) > 1 else ""
    return title[:200], company[:150], location[:150]


def _usable_job_label(text: str) -> bool:
    blob = (text or "").strip()
    if len(blob) < 6 or len(blob) > 180:
        return False
    if APPLY_CTA.match(blob) or FOOTER_TEXT.match(blob) or JUNK_TITLE.search(blob):
        return False
    if NOISE_LINE.match(blob):
        return False
    # Appcast cards dump location + title + description into one <a>.
    if LOCATION_HINT.search(blob) and re.search(r"\b(the|about|would you)\b", blob, re.I):
        return False
    return True


def extract_from_email(email: ParsedEmail, limit: int | None = 25) -> list[JobCandidate]:
    """Pull distinct job postings out of an alert digest (HTML preferred, text fallback).

    `limit=None` keeps every distinct posting (eval). Production poll still caps.
    """
    found: dict[str, JobCandidate] = {}
    fallback_company = company_from_sender(email)

    if email.html:
        soup = BeautifulSoup(email.html, "lxml")
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href.lower().startswith("http"):
                continue
            url = unwrap_url(href)
            anchor_text = clean_text(anchor.get_text("\n"))
            if is_footer_link(anchor_text, href) or is_footer_link(anchor_text, url):
                continue
            lines = _container_lines(anchor)
            if not lines and is_click_tracker(href):
                lines = _previous_sibling_card_lines(anchor)
            card_text = "\n".join(lines) or anchor_text
            keep = is_job_url(href) or (
                is_click_tracker(href)
                and (looks_like_job_card(anchor_text) or looks_like_job_card(card_text) or APPLY_CTA.match(anchor_text))
            )
            if not keep:
                continue
            if is_click_tracker(href) and not is_job_url(href):
                url = href
            key = canonical_key(url)
            title, company, location = _guess_fields(anchor_text, lines)
            label = " ".join(clean_text(anchor.get_text(" ")).split())
            nlines = [ln for ln in (anchor_text or "").split("\n") if ln.strip()]
            # Workday/jobs2web titles are often split across <span>s or wrapped
            # lines. Multi-line digest cards stay with _guess_fields.
            if _usable_job_label(label) and len(nlines) <= 3:
                title = label
                if company and (
                    company.lower() == title.lower()
                    or title.lower().startswith(company.lower().rstrip(" -–—") + " ")
                    or title.lower().startswith(company.lower() + "-")
                ):
                    company = ""
            if (
                not company
                or _looks_like_snippet(company)
                or JUNK_TITLE.search(company or "")
                or GENERIC_COMPANY.search(company or "")
                or APPLY_CTA.match(company or "")
                or re.search(r"\d+\s+more jobs", company or "", re.I)
            ):
                company = fallback_company or ""
            if JUNK_TITLE.search(title or "") or APPLY_CTA.match(title or ""):
                continue
            if not (title or "").strip():
                continue
            if title.lower().startswith("http"):
                continue
            # Only discard a "title" that is really just a location string. A bare
            # location is short ("Houston, TX"); a genuine title that merely ends in
            # a location ("Surveyor-Southeast District - Mobile, AL", "Sales Executive
            # Merchant Regional (Houston, TX)") is longer and must be kept even when
            # its role noun (Executive, Surveyor, Pilot) is not in TITLE_ROLE.
            if (
                is_location_line(title)
                and not TITLE_ROLE.search(title or "")
                and (len(title) <= 24 or ADDRESS_LINE.search(title or ""))
            ):
                continue
            if COMP_LINE.search(title or "") and not TITLE_ROLE.search(title or ""):
                continue
            if not company and title == title.lower():
                continue
            if not company and any(
                j.title.lower() == title.lower() and j.company for j in found.values()
            ):
                continue
            existing = found.get(key)
            if existing and len(existing.title) >= len(title):
                continue
            source = source_of(url) or ("appcast" if is_click_tracker(url) else "")
            found[key] = JobCandidate(
                url=url,
                url_key=key,
                title=title,
                company=company,
                location=location,
                source=source,
                context=" | ".join(lines)[:600],
            )
            if limit is not None and len(found) >= limit:
                break
        _add_unlinked_card_tables(soup, found, fallback_company, limit)

    if not found:
        for raw in re.findall(r"https?://[^\s<>\"')]+", email.text or ""):
            url = unwrap_url(raw.rstrip(".,);"))
            if not is_job_url(url):
                continue
            key = canonical_key(url)
            found.setdefault(
                key,
                JobCandidate(
                    url=url,
                    url_key=key,
                    company=fallback_company,
                    source=source_of(url),
                ),
            )
            if limit is not None and len(found) >= limit:
                break

    return list(found.values())
