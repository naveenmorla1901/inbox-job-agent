"""Pull structured posting / outreach fields out of JSON-LD and plain text.

The scraper already has the page. This module is the extra pass that turns a
blob of description into salary, visa, employment type, state, and similar
columns — without another HTTP request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

BOARD_SOURCES = {
    "linkedin",
    "indeed",
    "glassdoor",
    "ziprecruiter",
    "dice",
    "monster",
    "simplyhired",
    "builtin",
    "wellfound",
    "handshake",
    "hired",
    "otta",
    "remoteok",
    "weworkremotely",
    "adzuna",
    "experteer",
    "haystack",
    "appcast",
    "ycombinator",
}
ATS_SOURCES = {
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "smartrecruiters",
    "workable",
    "icims",
    "taleo",
    "jobvite",
    "bamboohr",
    "breezy",
    "rippling",
    "eightfold",
    "successfactors",
    "jobs2web",
    "njoyn",
}

EMPLOYMENT_MAP = {
    "FULL_TIME": "Full-Time",
    "PART_TIME": "Part-Time",
    "CONTRACTOR": "Contract",
    "TEMPORARY": "Temporary",
    "INTERN": "Internship",
    "VOLUNTEER": "Volunteer",
    "OTHER": "Other",
}

EMPLOYMENT_RE = re.compile(
    r"\b(full[- ]?time|part[- ]?time|contract(?:or)?|w2(?:\s+only)?|c2c|"
    r"corp[- ]to[- ]corp|1099|internship|temporary|seasonal)\b",
    re.I,
)
SALARY_RE = re.compile(
    r"(\$\s?\d{2,3}(?:,\d{3})+(?:\s*[-–—to]+\s*\$?\s?\d{2,3}(?:,\d{3})+)?(?:\s*(?:k|K))?"
    r"|\b\d{2,3}(?:\.\d+)?\s*[-–—to]+\s*\d{2,3}(?:\.\d+)?\s*k(?:\s*(?:usd|usd/yr|/year|per year))?"
    r"|\$\s?\d{2,3}(?:\.\d+)?\s*(?:-|to|–|—)\s*\$?\s?\d{2,3}(?:\.\d+)?\s*k"
    r"|\$\s?\d{2,4}(?:\.\d{2})?\s*(?:/|per)\s*(?:hour|hr|year|yr|annum))",
    re.I,
)
VISA_NO_RE = re.compile(
    r"(will not sponsor|no sponsorship|unable to sponsor|cannot sponsor|"
    r"does not sponsor|do not sponsor|not sponsor|"
    r"not eligible for (?:any )?(?:visa |work authorization |immigration )?(?:sponsorship|sponsor)|"
    r"sponsorship is not (?:available|offered|provided)|"
    r"no visa sponsorship|without sponsorship|"
    r"must (?:already )?be (?:currently )?(?:legally )?authorized to work[^.]{0,80}without (?:visa )?sponsorship)",
    re.I,
)
VISA_GC_RE = re.compile(
    r"(green card (?:holders? )?only|green card required|permanent resident(?:s)? only|"
    r"must be a (?:us |u\.s\. )?permanent resident|uscis (?:permanent )?residen|"
    r"us citizen(?:s|ship)? (?:only|required)|must be (a )?(?:us|u\.s\.) citizen|"
    r"(?:u\.s\.|us) citizenship (?:is )?required)",
    re.I,
)
VISA_H1B_RE = re.compile(
    r"(h[- ]?1b (?:visa )?(?:sponsorship )?(?:is )?(?:available|offered|ok|okay|welcome|provided)?"
    r"|visa sponsorship (?:is )?(?:available|offered|provided|welcome)"
    r"|will sponsor (?:h[- ]?1b|visas?|work authorization)"
    r"|sponsorship (?:available|considered|welcome|offered))",
    re.I,
)
VISA_OPT_RE = re.compile(
    r"\b((?:stem[- ]?)?opt|cpt|f[- ]?1(?:\s+visa)?|ead)\b",
    re.I,
)
CLEARANCE_RE = re.compile(
    r"(security clearance|ts/?sci|polygraph|clearance required|"
    r"active (secret|top secret|ts)|must (be|have) (?:a )?(secret|top secret) clearance)",
    re.I,
)
VISA_LABELS = (
    "No sponsorship",
    "US citizen / GC only",
    "H-1B sponsorship",
    "OPT/CPT mentioned",
    "Clearance required",
)
VISA_REJECT_LABELS = ("No sponsorship", "US citizen / GC only", "Clearance required")
YEARS_PHRASE = re.compile(
    r"((?:at least |minimum (?:of )?)?\d{1,2}\s*\+?\s*(?:to|-|–)\s*\d{1,2}\s*\+?\s*"
    r"(?:years?|yrs?)(?:\s+of)?(?:\s+[a-z ]{0,20})?experience"
    r"|\d{1,2}\s*\+\s*(?:years?|yrs?)(?:\s+of)?(?:\s+[a-z ]{0,20})?experience"
    r"|(?:entry[- ]level|junior|mid[- ]level|senior)(?:\s+level)?)",
    re.I,
)
STATE_RE = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|"
    r"MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|"
    r"WV|WI|WY|DC)\b"
)
REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|telecommute)\b", re.I)
PHONE_RE = re.compile(
    r"(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}"
)
SCHEDULING_HOSTS = (
    "calendly.com",
    "chilipiper.com",
    "timetrade.com",
    "appointlet.com",
    "cal.com",
    "savvycal.com",
    "outlook.office.com",
    "bookingsms",
)
DATE_ISO = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
WEBINAR_RE = re.compile(r"\b(webinar|workshop|masterclass|office hours|info session)\b", re.I)
NEWSLETTER_RE = re.compile(r"\b(newsletter|weekly digest|roundup|unsubscribe from all)\b", re.I)
SECURITY_RE = re.compile(
    r"(security alert|verify your (email|account)|password (reset|changed)|"
    r"unusual sign[- ]in|new (login|sign[- ]in)|two[- ]factor)",
    re.I,
)

COMMON_SKILLS = (
    "python",
    "sql",
    "r",
    "java",
    "scala",
    "pytorch",
    "tensorflow",
    "keras",
    "scikit-learn",
    "sklearn",
    "pandas",
    "numpy",
    "spark",
    "airflow",
    "aws",
    "gcp",
    "azure",
    "docker",
    "kubernetes",
    "langchain",
    "transformers",
    "huggingface",
    "llm",
    "nlp",
    "rag",
    "mlflow",
    "dbt",
    "snowflake",
    "databricks",
    "tableau",
    "power bi",
    "fastapi",
    "flask",
    "django",
    "kafka",
    "redis",
    "postgres",
    "mongodb",
)


@dataclass
class PostingFields:
    posted_at: str = ""
    employment_type: str = ""
    salary: str = ""
    visa_sponsorship: str = ""
    experience_required: str = ""
    required_skills: str = ""
    state: str = ""
    posting_id: str = ""
    source_type: str = ""


def source_type_of(source: str, url: str = "") -> str:
    name = (source or "").lower()
    if name in BOARD_SOURCES:
        return "job_board"
    if name in ATS_SOURCES:
        return "official_career_site"
    host = (urlparse(url).hostname or "").lower()
    if any(token in host for token in ("careers", "jobs", "workday")):
        return "official_career_site"
    return "job_board" if name else ""


def posting_id_of(url_key: str) -> str:
    if not url_key or ":" not in url_key:
        return url_key
    return url_key.split(":", 1)[1][:120]


def state_of(location: str, text: str = "") -> str:
    blob = f"{location} {text[:1500]}"
    if REMOTE_RE.search(location or "") and not STATE_RE.search(location or ""):
        return "Remote"
    match = STATE_RE.search(location or "")
    if match:
        return match.group(1)
    if REMOTE_RE.search(blob) and not STATE_RE.search(location or ""):
        return "Remote"
    match = STATE_RE.search(text[:2000] if text else "")
    return match.group(1) if match else ""


def salary_from_jsonld(item: dict | None) -> str:
    if not item:
        return ""
    base = item.get("baseSalary") or item.get("estimatedSalary")
    if isinstance(base, list) and base:
        base = base[0]
    if not isinstance(base, dict):
        return ""
    currency = str(base.get("currency") or "USD")
    value = base.get("value") or base.get("minValue") or {}
    if not isinstance(value, dict):
        amount = str(value).strip()
        return f"{currency} {amount}".strip() if amount else ""
    low = value.get("minValue") or value.get("value") or base.get("minValue")
    high = value.get("maxValue") or base.get("maxValue")
    unit = str(value.get("unitText") or base.get("unitText") or "").replace("HOUR", "/hour").replace(
        "YEAR", "/year"
    )
    if low and high and str(low) != str(high):
        return f"{currency} {low}–{high} {unit}".strip()
    if low:
        return f"{currency} {low} {unit}".strip()
    return ""


def posted_at_from_jsonld(item: dict | None) -> str:
    if not item:
        return ""
    raw = str(item.get("datePosted") or item.get("dateCreated") or "")
    match = DATE_ISO.search(raw)
    if match:
        return match.group(1)
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            return raw[:32]
    return ""


def employment_from_jsonld(item: dict | None) -> str:
    if not item:
        return ""
    raw = item.get("employmentType") or ""
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    token = str(raw).upper().replace(" ", "_")
    if token in EMPLOYMENT_MAP:
        return EMPLOYMENT_MAP[token]
    return str(raw).replace("_", " ").title()[:40] if raw else ""


def employment_from_text(text: str) -> str:
    match = EMPLOYMENT_RE.search(text or "")
    if not match:
        return ""
    token = re.sub(r"\s+", " ", match.group(1)).lower()
    mapping = {
        "full time": "Full-Time",
        "full-time": "Full-Time",
        "part time": "Part-Time",
        "part-time": "Part-Time",
        "contractor": "Contract",
        "contract": "Contract",
        "w2": "W2 Contract",
        "w2 only": "W2 Contract",
        "c2c": "C2C",
        "corp-to-corp": "C2C",
        "corp to corp": "C2C",
        "1099": "1099",
        "internship": "Internship",
        "temporary": "Temporary",
        "seasonal": "Seasonal",
    }
    return mapping.get(token, match.group(1).title())


def visa_from_text(text: str) -> str:
    blob = text or ""
    if CLEARANCE_RE.search(blob):
        return "Clearance required"
    if VISA_NO_RE.search(blob):
        return "No sponsorship"
    if VISA_GC_RE.search(blob):
        return "US citizen / GC only"
    if VISA_H1B_RE.search(blob):
        return "H-1B sponsorship"
    if VISA_OPT_RE.search(blob):
        return "OPT/CPT mentioned"
    return ""


def normalize_visa(raw: str, text: str = "") -> str:
    """Map free-text / LLM visa notes onto the five labels we actually store."""
    classified = visa_from_text(f"{raw or ''}\n{text or ''}")
    if classified:
        return classified
    cleaned = re.sub(r"\s+", " ", (raw or "")).strip()
    return cleaned if cleaned in VISA_LABELS else ""


def experience_from_text(text: str) -> str:
    match = YEARS_PHRASE.search(text or "")
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()[:80]


def salary_from_text(text: str) -> str:
    match = SALARY_RE.search(text or "")
    return re.sub(r"\s+", " ", match.group(0)).strip()[:80] if match else ""


def skills_from_text(text: str, extra: list[str] | None = None) -> str:
    haystack = (text or "").lower()
    found: list[str] = []
    seen: set[str] = set()
    for skill in list(COMMON_SKILLS) + [s.lower() for s in (extra or [])]:
        if skill in seen:
            continue
        if len(skill) <= 2:
            if re.search(rf"(?<![a-z0-9]){re.escape(skill)}(?![a-z0-9])", haystack):
                found.append(skill)
                seen.add(skill)
        elif skill in haystack:
            found.append(skill)
            seen.add(skill)
    return ", ".join(found[:20])


def fields_from_jsonld(item: dict | None) -> PostingFields:
    return PostingFields(
        posted_at=posted_at_from_jsonld(item),
        employment_type=employment_from_jsonld(item),
        salary=salary_from_jsonld(item),
    )


def enrich_fields(
    *,
    source: str = "",
    url: str = "",
    url_key: str = "",
    location: str = "",
    description: str = "",
    extra_skills: list[str] | None = None,
    seed: PostingFields | None = None,
) -> PostingFields:
    """Fill any empty posting field from text after JSON-LD / API values are applied."""
    out = seed or PostingFields()
    blob = f"{location}\n{description}"
    out.source_type = out.source_type or source_type_of(source, url)
    out.posting_id = out.posting_id or posting_id_of(url_key)
    out.state = out.state or state_of(location, description)
    out.salary = out.salary or salary_from_text(blob)
    out.employment_type = out.employment_type or employment_from_text(blob)
    out.visa_sponsorship = normalize_visa(out.visa_sponsorship, blob)
    out.experience_required = out.experience_required or experience_from_text(blob)
    out.required_skills = out.required_skills or skills_from_text(blob, extra_skills)
    if not out.posted_at:
        match = DATE_ISO.search(description or "")
        if match:
            out.posted_at = match.group(1)
    return out


def email_type_of(category: str, subject: str, body: str) -> str:
    if category != "other":
        return ""
    blob = f"{subject}\n{body}"
    if SECURITY_RE.search(blob):
        return "security"
    if WEBINAR_RE.search(blob):
        return "webinar"
    if NEWSLETTER_RE.search(blob):
        return "newsletter"
    return "unclassified"


def phone_from_text(text: str) -> str:
    match = PHONE_RE.search(text or "")
    return match.group(0).strip()[:40] if match else ""


def scheduling_url_from_links(urls: list[str]) -> str:
    for url in urls:
        host = (urlparse(url).hostname or "").lower()
        if any(token in host for token in SCHEDULING_HOSTS):
            return url[:400]
        lowered = url.lower()
        if "calendly.com" in lowered or "/book" in lowered:
            return url[:400]
    return ""
