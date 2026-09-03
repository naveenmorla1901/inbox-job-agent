from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from .config import Profile
from .job_fields import VISA_REJECT_LABELS, visa_from_text

TOKEN = re.compile(r"[a-z0-9+#./-]{2,}")
# "5+ years of relevant experience", "3-5 yrs experience"
YEARS_BEFORE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:to|-|–)?\s*(\d{1,2})?\s*\+?\s*(?:years?|yrs?)"
    r"\s*(?:of\s+)?[a-z ]{0,25}?experience",
    re.I,
)
# "Experience: 5+ years"
YEARS_AFTER = re.compile(r"experience[a-z:, ]{0,20}?(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I)
STOPWORDS = {
    "the", "and", "for", "with", "you", "our", "are", "will", "who", "this", "that", "have",
    "from", "your", "all", "job", "work", "team", "role", "about", "not", "but", "can", "any",
    "their", "has", "was", "its", "into", "such", "more", "than", "other", "also", "may",
    "per", "out", "use", "using", "well", "new", "one", "two", "how", "help", "make", "including",
    "candidate", "candidates", "company", "position", "opportunity", "employment", "employer",
    "years", "year", "experience", "skills", "ability", "strong", "must", "should", "would",
}
REMOTE_HINT = re.compile(r"\b(remote|work from home|wfh|distributed|anywhere)\b", re.I)


@dataclass
class MatchResult:
    score: float = 0.0
    title_score: float = 0.0
    skill_score: float = 0.0
    resume_score: float = 0.0
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    verdict: str = ""
    rejected: bool = False


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN.findall((text or "").lower()) if t not in STOPWORDS and len(t) > 1]


def cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    num = sum(math.sqrt(a[t]) * math.sqrt(b[t]) for t in common)
    na = math.sqrt(sum(a[t] for t in a))
    nb = math.sqrt(sum(b[t] for t in b))
    return num / (na * nb) if na and nb else 0.0


def _contains_term(haystack: str, term: str) -> bool:
    if len(term) <= 3 or not term.isalnum():
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", haystack) is not None
    return term in haystack


def score_title(profile: Profile, title: str) -> tuple[float, bool]:
    t = (title or "").lower().strip()
    if not t:
        return 0.35, False
    for bad in profile.exclude_titles:
        term = bad.lower().strip()
        if not term:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", t):
            return 0.0, True
    for target in profile.target_titles:
        if target.lower() in t:
            return 1.0, False
    hits = sum(1 for kw in profile.title_keywords if _contains_term(t, kw.lower()))
    if hits >= 2:
        return 0.75, False
    if hits == 1:
        return 0.5, False
    return 0.1, False


STUDENT_TITLE = re.compile(r"\b(intern|internship|co-?op|student experience|apprentice)\b", re.I)


def title_worth_scraping(profile: Profile, title: str) -> bool:
    """Skip the job-page fetch when the email title is clearly not a target role."""
    score, rejected = score_title(profile, title)
    if rejected or score < 0.5:
        return False
    if STUDENT_TITLE.search(title or "") and score < 1.0:
        return False
    return True


def score_skills(profile: Profile, text: str) -> tuple[float, list[str], list[str]]:
    haystack = (text or "").lower()
    matched, missing, earned = [], [], 0.0
    for skill in profile.skills:
        if any(_contains_term(haystack, term) for term in skill.terms()):
            matched.append(skill.name)
            earned += skill.weight
        else:
            missing.append(skill.name)
    coverage = earned / profile.skill_weight_total()
    # Full credit well before 100% coverage: no posting asks for a whole resume.
    return min(1.0, coverage / 0.45), matched, missing


def max_years_required(text: str) -> int:
    best = 0
    for match in YEARS_BEFORE.finditer(text or ""):
        lo = int(match.group(1))
        hi = int(match.group(2)) if match.group(2) else lo
        best = max(best, min(hi, 25))
    for match in YEARS_AFTER.finditer(text or ""):
        best = max(best, min(int(match.group(1)), 25))
    return best


def location_ok(profile: Profile, location: str, text: str) -> bool:
    blob = f"{location} {text[:2000]}".lower()
    if profile.remote_ok and REMOTE_HINT.search(blob):
        return True
    if not profile.preferred_locations:
        return True
    return any(loc.lower() in blob for loc in profile.preferred_locations)


def match_job(
    profile: Profile, title: str, description: str, location: str = "", company: str = ""
) -> MatchResult:
    text = f"{title}\n{company}\n{location}\n{description}"
    lowered = text.lower()

    for bad in profile.exclude_keywords:
        if bad.lower() in lowered:
            return MatchResult(verdict=f"rejected: contains '{bad}'", rejected=True)

    visa = visa_from_text(text)
    if visa in VISA_REJECT_LABELS:
        return MatchResult(verdict=f"rejected: {visa.lower()}", rejected=True)

    title_score, title_rejected = score_title(profile, title)
    if title_rejected:
        return MatchResult(verdict="rejected: excluded title", rejected=True)

    skill_score, matched, missing = score_skills(profile, text)
    resume_score = cosine(Counter(tokenize(profile.resume_text)), Counter(tokenize(text)))
    resume_score = min(1.0, resume_score / 0.30)

    score = 0.40 * title_score + 0.35 * skill_score + 0.25 * resume_score

    years = max_years_required(description)
    if years > profile.max_years_experience + 2:
        score -= 0.15
    if not location_ok(profile, location, description):
        score -= 0.10

    score = round(max(0.0, min(1.0, score)), 3)
    verdict = (
        f"title {title_score:.2f} / skills {skill_score:.2f} / resume {resume_score:.2f}"
        + (f" / wants {years}y" if years else "")
    )
    return MatchResult(
        score=score,
        title_score=round(title_score, 3),
        skill_score=round(skill_score, 3),
        resume_score=round(resume_score, 3),
        matched_skills=matched,
        missing_skills=missing,
        verdict=verdict,
    )
