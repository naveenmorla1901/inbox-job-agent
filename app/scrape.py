from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html import unescape as html_unescape

from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .email_parse import clean_text, html_to_text
from .extract_jobs import JobCandidate, unwrap_url
from .job_fields import PostingFields, enrich_fields, fields_from_jsonld, normalize_visa

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
GOOGLEBOT_HEADERS = {
    **HEADERS,
    "User-Agent": (
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    ),
}

LINKEDIN_GUEST = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?content=true"
LEVER_API = "https://api.lever.co/v0/postings/{company}/{job_id}?mode=json"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"
GREENHOUSE_BOARD = re.compile(
    r"(?:[?&]for=([a-z0-9_-]+))|(?:greenhouse\.io/(?:embed/)?([a-z0-9_-]+)/jobs)", re.I
)

# Boilerplate that means we grabbed an application form instead of the posting.
FORM_NOISE = re.compile(
    r"(indicates a required field|attach.{0,20}dropbox|accepted file types|"
    r"enter manually|autofill my application)",
    re.I,
)
BLOCKED_NOISE = re.compile(
    r"(captcha|unusual traffic|are you a human|access denied|request blocked|"
    r"enable javascript to|cloudflare|please verify you are|sign in to continue|"
    r"401 unauthorized|403 forbidden|sign in with (apple|a passkey|google)|"
    r"new to linkedin|join now|get hired without the hassle|"
    r"your activity and behavior on this site made us think that you are a bot|"
    r"radware captcha)",
    re.I,
)
INTERSTITIAL_NOISE = re.compile(
    r"(you are now being redirected|if you are not redirected within|"
    r"this website uses (only essential )?cookies|"
    r"by continuing to browse this website|"
    r'"widget"\s*:\s*"redirect")',
    re.I,
)
NAV_CHROME = re.compile(
    r"skip to (?:main )?content.{0,400}(language|čeština|deutsch \(|select language)",
    re.I | re.S,
)
SHELL_TITLE = re.compile(
    r"(get hired without the hassle|^haystack\b|sign in to (continue|linkedin)|"
    r"page not found|access denied|adzuna jobs search|^adzuna$|"
    r"radware captcha|cookie (policy|consent|settings))",
    re.I,
)

LLM_EXTRACT_SYSTEM = "You extract structured job data from raw page text. JSON only, no prose."
LLM_EXTRACT_PROMPT = """Extract the job posting from this page text.

Return JSON:
{{"title": "", "company": "", "location": "", "description": "the responsibilities, requirements
and skills, plain text, up to 4000 characters", "is_job_posting": true|false,
 "posted_at": "YYYY-MM-DD or empty", "employment_type": "", "salary": "",
 "visa_sponsorship": "exactly one of: '' | No sponsorship | US citizen / GC only | H-1B sponsorship | OPT/CPT mentioned | Clearance required",
 "experience_required": "", "required_skills": "comma list",
 "state": "two-letter US state or Remote"}}

If the page is a login wall, search page, or anything other than a single job posting,
set is_job_posting to false.

PAGE TEXT:
{text}
"""


@dataclass
class ScrapedJob:
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    posted_at: str = ""
    employment_type: str = ""
    salary: str = ""
    visa_sponsorship: str = ""
    experience_required: str = ""
    required_skills: str = ""
    state: str = ""
    posting_id: str = ""
    source_type: str = ""
    ok: bool = False
    final_url: str = ""
    status: str = "empty"  # ok | blocked | empty | error | skipped
    extraction: str = ""  # jsonld | greenhouse | lever | ashby | html | llm | email

    def is_thin(self) -> bool:
        return len(self.description) < 250

    def apply_fields(self, fields: PostingFields) -> None:
        self.posted_at = self.posted_at or fields.posted_at
        self.employment_type = self.employment_type or fields.employment_type
        self.salary = self.salary or fields.salary
        self.visa_sponsorship = normalize_visa(
            self.visa_sponsorship or fields.visa_sponsorship, self.description
        )
        self.experience_required = self.experience_required or fields.experience_required
        self.required_skills = self.required_skills or fields.required_skills
        self.state = self.state or fields.state
        self.posting_id = self.posting_id or fields.posting_id
        self.source_type = self.source_type or fields.source_type


def _jsonld_blocks(soup: BeautifulSoup):
    for tag in soup.find_all("script", type=lambda v: v and "ld+json" in v):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", raw))
            except json.JSONDecodeError:
                continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                yield from ([item] + item.get("@graph", []) if "@graph" in item else [item])


def _from_jsonld(soup: BeautifulSoup) -> ScrapedJob | None:
    for item in _jsonld_blocks(soup):
        types = item.get("@type", "")
        types = types if isinstance(types, list) else [types]
        if "JobPosting" not in types:
            continue
        org = item.get("hiringOrganization") or {}
        company = org.get("name", "") if isinstance(org, dict) else str(org)
        loc = item.get("jobLocation") or {}
        loc = loc[0] if isinstance(loc, list) and loc else loc
        address = loc.get("address", {}) if isinstance(loc, dict) else {}
        location = ", ".join(
            str(address.get(k, "")).strip()
            for k in ("addressLocality", "addressRegion", "addressCountry")
            if address.get(k)
        )
        if item.get("jobLocationType") == "TELECOMMUTE":
            location = (location + " (Remote)").strip()
        extras = fields_from_jsonld(item)
        return ScrapedJob(
            title=clean_text(str(item.get("title", "")))[:200],
            company=clean_text(str(company))[:150],
            location=clean_text(location)[:150],
            description=html_to_text(html_unescape(str(item.get("description", ""))))[:20000],
            posted_at=extras.posted_at,
            employment_type=extras.employment_type,
            salary=extras.salary,
            ok=True,
            status="ok",
            extraction="jsonld",
        )
    return None


def page_is_interstitial(title: str, body: str) -> bool:
    """True when the fetched page is a cookie wall, redirect stub, or site chrome."""
    blob = f"{title}\n{body[:4000]}"
    if INTERSTITIAL_NOISE.search(blob):
        return True
    if NAV_CHROME.search(body[:1200]) and not re.search(
        r"\b(responsibilities|requirements|qualifications|what you.ll do)\b",
        body,
        re.I,
    ):
        return True
    stripped = (body or "").strip()
    if stripped.startswith("{") and '"widget"' in stripped[:120] and "redirect" in stripped[:200]:
        return True
    return False


def _redirected_company(body: str) -> str:
    match = re.search(r"you are now being redirected to\s+([^\n.<]+)", body or "", re.I)
    if not match:
        return ""
    return clean_text(match.group(1))[:150]


def interstitial_destination(text: str, base_url: str) -> str:
    """Follow Workday widget JSON and Adzuna 'click here if not redirected' targets once."""
    raw = (text or "").strip()
    if raw.startswith("{") and "widget" in raw[:80]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and str(data.get("widget", "")).lower() == "redirect":
            dest = str(data.get("url") or "").strip()
            if dest:
                return urljoin(base_url, dest)

    match = re.search(
        r"if you are not redirected[^<]{0,120}<a[^>]+href=[\"'](https?://[^\"']+|/[^\s\"']+)[\"']",
        text or "",
        re.I | re.S,
    )
    if match:
        return urljoin(base_url, match.group(1))
    match = re.search(
        r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*url=([^"\']+)',
        text or "",
        re.I,
    )
    if match:
        return urljoin(base_url, match.group(1).strip())
    return ""


def _from_html(soup: BeautifulSoup) -> ScrapedJob:
    def meta(prop: str) -> str:
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        return clean_text(tag.get("content", "")) if tag else ""

    for tag in soup(["script", "style", "nav", "footer", "header", "form", "aside", "noscript"]):
        tag.decompose()

    candidates = soup.find_all(["main", "article"]) + soup.find_all(
        attrs={"class": re.compile(r"(job|description|posting|details|content)", re.I)}
    )
    best = max((html_to_text(str(c)) for c in candidates), key=len, default="")
    body = best if len(best) > 200 else html_to_text(str(soup))
    title = meta("og:title") or clean_text(soup.title.get_text() if soup.title else "")

    blocked = bool(BLOCKED_NOISE.search(body[:2000])) and len(body) < 1500
    if SHELL_TITLE.search(title) or page_is_interstitial(title, body):
        company = _redirected_company(body)
        return ScrapedJob(
            status="empty",
            extraction="html",
            title="",
            company=company[:150],
        )
    usable = bool(body.strip()) and not FORM_NOISE.search(body[:1500]) and not blocked
    return ScrapedJob(
        title=title[:200],
        company=meta("og:site_name")[:150],
        location="",
        description=body[:20000],
        ok=usable,
        status="blocked" if blocked else ("ok" if usable else "empty"),
        extraction="html",
    )


def _from_linkedin(soup: BeautifulSoup) -> ScrapedJob | None:
    """Parse LinkedIn's public guest fragment: title/company/location live in the topcard."""

    def pick(*selectors: str) -> str:
        for selector in selectors:
            node = soup.select_one(selector)
            if node:
                text = clean_text(node.get_text(" "))
                if text:
                    return text
        return ""

    body_node = soup.select_one(".show-more-less-html__markup, .description__text")
    if body_node is None:
        return None

    description = html_to_text(str(body_node))
    criteria = [
        clean_text(node.get_text(" "))
        for node in soup.select(".description__job-criteria-item")
    ]
    if criteria:
        description += "\n\n" + "\n".join(criteria)

    employment = ""
    for line in criteria:
        if "employment" in line.lower():
            employment = line.split(":", 1)[-1].replace("Employment type", "").strip()[:40]

    return ScrapedJob(
        title=pick("h1.top-card-layout__title", "h2.top-card-layout__title", ".topcard__title")[:200],
        company=pick("a.topcard__org-name-link", ".topcard__org-name-link")[:150],
        location=pick(".topcard__flavor--bullet", ".top-card-layout__second-subline span")[:150],
        description=description[:20000],
        employment_type=employment,
        ok=bool(description.strip()),
        status="ok" if description.strip() else "empty",
        extraction="linkedin",
    )


def _from_greenhouse(data: dict) -> ScrapedJob:
    location = (data.get("location") or {}).get("name", "")
    content = html_unescape(str(data.get("content", "")))
    posted = str(data.get("updated_at") or data.get("first_published") or "")[:10]
    return ScrapedJob(
        title=clean_text(str(data.get("title", "")))[:200],
        company=clean_text(str(data.get("company_name", "")))[:150],
        location=clean_text(location)[:150],
        description=html_to_text(content)[:20000],
        posted_at=posted if re.match(r"20\d{2}-\d{2}-\d{2}", posted) else "",
        ok=bool(content.strip()),
        status="ok" if content.strip() else "empty",
        extraction="greenhouse",
    )


def _from_lever(data: dict) -> ScrapedJob:
    categories = data.get("categories") or {}
    body = data.get("descriptionPlain") or html_to_text(str(data.get("description", "")))
    for section in data.get("lists") or []:
        body += "\n\n" + clean_text(str(section.get("text", ""))) + "\n"
        body += html_to_text(str(section.get("content", "")))
    return ScrapedJob(
        title=clean_text(str(data.get("text", "")))[:200],
        company=clean_text(str(categories.get("team", "")))[:150],
        location=clean_text(str(categories.get("location", "")))[:150],
        description=clean_text(body)[:20000],
        employment_type=clean_text(str(categories.get("commitment", "")))[:40],
        posted_at=str(data.get("createdAt") or "")[:10]
        if str(data.get("createdAt") or "").startswith("20")
        else "",
        ok=bool(body.strip()),
        status="ok" if body.strip() else "empty",
        extraction="lever",
    )


def _api_target(candidate: JobCandidate) -> tuple[str, str]:
    """Prefer a board's public JSON API over its JavaScript-heavy HTML page."""
    key, url = candidate.url_key, unwrap_url(candidate.url)

    if key.startswith("linkedin:"):
        return LINKEDIN_GUEST.format(job_id=key.split(":", 1)[1]), "linkedin"
    if key.startswith("greenhouse:"):
        match = GREENHOUSE_BOARD.search(url)
        board = (match.group(1) or match.group(2)) if match else ""
        if board:
            return GREENHOUSE_API.format(board=board, job_id=key.split(":", 1)[1]), "greenhouse"
    if key.startswith("lever:"):
        _, company, job_id = key.split(":", 2)
        return LEVER_API.format(company=company, job_id=job_id), "lever"
    return url, "html"


def _get(client: httpx.Client, url: str, headers: dict) -> httpx.Response | None:
    try:
        return client.get(url, headers=headers)
    except Exception as exc:
        log.info("request failed for %s: %s", url, exc)
        return None


def fetch_job(candidate: JobCandidate, timeout: int = 15) -> ScrapedJob:
    """Follow a posting link and pull structured content out of whatever comes back.

    Order of attack: board JSON API -> schema.org JSON-LD -> readable HTML. If the site blocks
    a normal browser UA (LinkedIn and Indeed often do), retry once as Googlebot, which most job
    boards deliberately allow so their postings get indexed.
    """
    url, kind = _api_target(candidate)
    original = unwrap_url(candidate.url)

    with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as client:
        resp = _get(client, url, HEADERS)

        # A dead API shortcut should not cost us the posting: fall back to the original page.
        if kind != "html" and (resp is None or resp.status_code >= 400):
            url, kind = original, "html"
            resp = _get(client, url, HEADERS)

        if resp is None:
            return ScrapedJob(status="error", extraction="", final_url=url)

        if resp.status_code in (401, 403, 429, 451, 999) or not resp.text.strip():
            retry = _get(client, original, GOOGLEBOT_HEADERS)
            if retry is not None and retry.status_code < 400 and retry.text.strip():
                resp, kind = retry, "html"
            else:
                status = "blocked" if resp.status_code in (401, 403, 429, 451, 999) else "empty"
                return ScrapedJob(status=status, final_url=str(resp.url))

        hop = interstitial_destination(resp.text, str(resp.url))
        if hop and hop.split("?", 1)[0] != str(resp.url).split("?", 1)[0]:
            nxt = _get(client, hop, HEADERS)
            if nxt is not None and nxt.status_code < 400 and nxt.text.strip():
                resp, kind = nxt, "html"

        try:
            if kind == "greenhouse":
                job = _from_greenhouse(resp.json())
            elif kind == "lever":
                job = _from_lever(resp.json())
            else:
                soup = BeautifulSoup(resp.text, "lxml")
                job = None
                if kind == "linkedin":
                    job = _from_linkedin(soup)
                job = job or _from_jsonld(soup) or _from_html(soup)
        except Exception as exc:
            log.info("parse failed for %s: %s", url, exc)
            return ScrapedJob(status="error", final_url=str(resp.url))

        job.final_url = str(resp.url)

        # Some boards render an empty shell for bots; one Googlebot retry often fixes it.
        if job.is_thin() and kind == "html" and resp.request.headers.get("user-agent") == UA:
            retry = _get(client, original, GOOGLEBOT_HEADERS)
            if retry is not None and retry.status_code < 400 and len(retry.text) > len(resp.text):
                soup = BeautifulSoup(retry.text, "lxml")
                better = _from_jsonld(soup) or _from_html(soup)
                if len(better.description) > len(job.description):
                    better.final_url = str(retry.url)
                    job = better

        if job.ok and job.is_thin():
            job.ok = len(job.description) > 40
            job.status = "ok" if job.ok else "empty"
        _fill_listing_fields(job, candidate)
        return job


def llm_extract(page_text: str, llm, candidate: JobCandidate) -> ScrapedJob | None:
    """Last resort: let a free-tier model read the page text and pull the fields out."""
    if not llm or not llm.enabled or len(page_text) < 200:
        return None
    data = llm.json(
        LLM_EXTRACT_PROMPT.format(text=page_text[:12000]),
        LLM_EXTRACT_SYSTEM,
        task="extract",
    )
    if not data or data.get("is_job_posting") is False:
        return None
    description = str(data.get("description", ""))
    if len(description) < 80:
        return None
    job = ScrapedJob(
        title=str(data.get("title", "") or candidate.title)[:200],
        company=str(data.get("company", "") or candidate.company)[:150],
        location=str(data.get("location", "") or candidate.location)[:150],
        description=description[:20000],
        posted_at=str(data.get("posted_at", ""))[:10],
        employment_type=str(data.get("employment_type", ""))[:40],
        salary=str(data.get("salary", ""))[:80],
        visa_sponsorship=normalize_visa(str(data.get("visa_sponsorship", "")), description)[:80],
        experience_required=str(data.get("experience_required", ""))[:80],
        required_skills=str(data.get("required_skills", ""))[:400],
        state=str(data.get("state", ""))[:20],
        ok=True,
        status="ok",
        extraction="llm",
    )
    _fill_listing_fields(job, candidate)
    return job


def _fill_listing_fields(job: ScrapedJob, candidate: JobCandidate) -> None:
    """Backfill salary / visa / state / etc. from the description we already have."""
    extra = [s.strip() for s in (job.required_skills or "").split(",") if s.strip()]
    fields = enrich_fields(
        source=candidate.source,
        url=candidate.url,
        url_key=candidate.url_key,
        location=job.location or candidate.location,
        description=job.description,
        extra_skills=extra,
        seed=PostingFields(
            posted_at=job.posted_at,
            employment_type=job.employment_type,
            salary=job.salary,
            visa_sponsorship=job.visa_sponsorship,
            experience_required=job.experience_required,
            required_skills=job.required_skills,
            state=job.state,
            posting_id=job.posting_id,
            source_type=job.source_type,
        ),
    )
    job.apply_fields(fields)


def fetch_all(
    candidates: list[JobCandidate], timeout: int = 15, workers: int = 5
) -> dict[str, ScrapedJob]:
    if not candidates:
        return {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda c: (c.url_key, fetch_job(c, timeout)), candidates)
        return dict(results)
