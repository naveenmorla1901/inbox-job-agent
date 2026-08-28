from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from .email_parse import ParsedEmail, clean_text

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
}

# URL paths that look like a single posting rather than a search page.
POSTING_PATH = re.compile(
    r"/(jobs?/view|viewjob|job-listing|job-detail|jobs?/\d+|rc/clk|pagead/clk|"
    r"jobs?/[a-z0-9-]+/[0-9a-f-]{8,}|careers?/job|job/|opportunit|job_app|"
    r"remote-jobs?/[a-z0-9][a-z0-9-]{3,})",
    re.I,
)
# Same idea, but strict enough to trust on a company site we have never seen before.
STRONG_POSTING_PATH = re.compile(
    r"/(jobs?/view/|viewjob|job-listing|job-detail|job_app|"
    r"(jobs?|careers?|positions?|openings?|vacanc(y|ies))/[^/]*\d{3,}|"
    r"(jobs?|careers?|positions?|openings?)/[a-z0-9-]+/[0-9a-f-]{8,})",
    re.I,
)
# Lever and Ashby address postings by bare UUID: /<company>/<uuid>.
UUID_PATH = re.compile(
    r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)
SEARCH_PATH = re.compile(r"/(jobs/search|q-|browse|alerts?|unsubscribe|settings|help|feed)", re.I)
# Query params that identify a specific posting. "id" alone is too generic off known boards.
ID_PARAMS = ("jk", "gh_jid", "jobid", "jobId", "currentJobId", "requisitionid", "vjk", "token")
WEAK_ID_PARAMS = ("id",)

LOCATION_HINT = re.compile(
    r"(remote|hybrid|on-?site|,\s*[A-Z]{2}\b|United States|USA|India|Canada|Europe|"
    r"New York|San Francisco|Seattle|Austin|Boston|Chicago|Dallas|Atlanta|Denver)",
    re.I,
)
NOISE_LINE = re.compile(
    r"^(view job|apply now|see all|actively hiring|easy apply|be an early applicant|"
    r"\d+\s+(school|connection|alum)|promoted|new|\d+ (hour|day|week|minute)s? ago|"
    r"posted|save|unsubscribe|view all jobs)",
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
        parsed = urlparse(url)
        params = parse_qs(parsed.query or "")
        nested = ""

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


def host_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def source_of(url: str) -> str:
    host = host_of(url)
    for domain, name in JOB_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return ""


def is_job_url(url: str) -> bool:
    parsed = urlparse(unwrap_url(url))
    path = parsed.path or ""
    params = parse_qs(parsed.query or "")

    # An ATS id in the query is a posting no matter whose domain hosts the page.
    if any(p in params for p in ID_PARAMS):
        return True
    if UUID_PATH.search(path):
        return True
    if not source_of(url):
        return bool(STRONG_POSTING_PATH.search(path))
    if SEARCH_PATH.search(path) and not POSTING_PATH.search(path):
        return False
    if POSTING_PATH.search(path):
        return True
    return any(p in params for p in WEAK_ID_PARAMS)


def canonical_key(url: str) -> str:
    """Stable identity for a posting so the same job never lands in the DB twice."""
    url = unwrap_url(url)
    parsed = urlparse(url)
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

    for key in (*ID_PARAMS, *WEAK_ID_PARAMS):
        if key in params:
            return f"{source}:{host}{path}:{params[key][0]}"
    return f"{source}:{host}{path}"


def _container_lines(anchor) -> list[str]:
    node = anchor
    for _ in range(5):
        parent = node.parent
        if parent is None:
            break
        node = parent
        text = clean_text(node.get_text("\n"))
        lines = [ln for ln in text.split("\n") if ln.strip()]
        if len(lines) >= 2:
            return lines[:12]
    return []


def _guess_fields(anchor_text: str, lines: list[str]) -> tuple[str, str, str]:
    useful = [ln for ln in lines if not NOISE_LINE.match(ln) and len(ln) > 1]
    title = anchor_text.strip()
    if not title or NOISE_LINE.match(title):
        title = useful[0] if useful else ""

    rest = [ln for ln in useful if ln.strip().lower() != title.strip().lower()]
    company = location = ""
    for line in rest:
        if "·" in line or "•" in line:
            parts = [p.strip() for p in re.split(r"[·•]", line) if p.strip()]
            if parts:
                company = company or parts[0]
                if len(parts) > 1:
                    location = location or parts[1]
                continue
        if not company:
            company = line
        elif not location and LOCATION_HINT.search(line):
            location = line
    return title[:200], company[:150], location[:150]


def extract_from_email(email: ParsedEmail, limit: int = 25) -> list[JobCandidate]:
    """Pull distinct job postings out of an alert digest (HTML preferred, text fallback)."""
    found: dict[str, JobCandidate] = {}

    if email.html:
        soup = BeautifulSoup(email.html, "lxml")
        for anchor in soup.find_all("a", href=True):
            url = unwrap_url(anchor["href"].strip())
            if not is_job_url(url):
                continue
            key = canonical_key(url)
            anchor_text = clean_text(anchor.get_text(" "))
            lines = _container_lines(anchor)
            title, company, location = _guess_fields(anchor_text, lines)
            existing = found.get(key)
            if existing and len(existing.title) >= len(title):
                continue
            found[key] = JobCandidate(
                url=url,
                url_key=key,
                title=title,
                company=company,
                location=location,
                source=source_of(url),
                context=" | ".join(lines)[:600],
            )
            if len(found) >= limit:
                break

    if not found:
        for raw in re.findall(r"https?://[^\s<>\"')]+", email.text or ""):
            url = unwrap_url(raw.rstrip(".,);"))
            if not is_job_url(url):
                continue
            key = canonical_key(url)
            found.setdefault(
                key, JobCandidate(url=url, url_key=key, source=source_of(url))
            )
            if len(found) >= limit:
                break

    return list(found.values())
