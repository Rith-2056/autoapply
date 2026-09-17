"""Job deduplication across sources.

A job is the same job when any strong signal matches (normalised URL, external
job id) or when company + title + location are effectively identical.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import parse_qs, urlparse

_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "source", "src", "gh_src", "lever-source", "referrer"}
_ID_PATTERNS = [
    re.compile(r"/jobs?/(\d{5,})"),                 # greenhouse /jobs/8170944
    re.compile(r"gh_jid=(\d+)"),
    re.compile(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I),  # lever/ashby uuid
    re.compile(r"_(R-?\d+|RQ\d+|JR\d+|REQ\d+)\b", re.I),  # workday requisition
    re.compile(r"/(\d{6,})(?:[/?#-]|$)"),
]


def normalize_url(url: str) -> str:
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url.strip().lower()
    q = parse_qs(p.query, keep_blank_values=False)
    kept = sorted((k, v[0]) for k, v in q.items() if k.lower() not in _TRACKING)
    host = p.netloc.lower().removeprefix("www.")
    path = re.sub(r"/+$", "", p.path)
    query = "&".join(f"{k}={v}" for k, v in kept)
    return f"{host}{path}" + (f"?{query}" if query else "")


def external_job_id(url: str) -> str:
    for pat in _ID_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1).lower()
    return ""


def _norm_text(s: str) -> str:
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s.lower())
    s = re.sub(r"\b(summer|fall|spring|winter)\s*20\d\d\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(intern|internship|co op|coop|the|a|an|of|and|or)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    a, b = _norm_text(a), _norm_text(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class DuplicateMatch:
    job_id: int
    reason: str
    confidence: float


def find_duplicate(candidate: dict, existing: list[dict]) -> DuplicateMatch | None:
    """``candidate``/``existing`` items are dicts with id, company, title, location, url, external_job_id."""
    c_url = normalize_url(candidate.get("url", ""))
    c_ext = candidate.get("external_job_id") or external_job_id(candidate.get("url", ""))
    c_company = _norm_text(candidate.get("company", ""))
    best: DuplicateMatch | None = None
    for e in existing:
        if c_url and normalize_url(e.get("url", "")) == c_url:
            return DuplicateMatch(e["id"], "same URL", 1.0)
        e_ext = e.get("external_job_id") or external_job_id(e.get("url", ""))
        e_company = _norm_text(e.get("company", ""))
        if c_ext and e_ext and c_ext == e_ext and (c_company == e_company or not c_company or not e_company):
            return DuplicateMatch(e["id"], "same job id", 0.98)
        if c_company != e_company or not c_company:
            continue
        t = similarity(candidate.get("title", ""), e.get("title", ""))
        loc = similarity(candidate.get("location", "") or "", e.get("location", "") or "")
        if t >= 0.92 and (loc >= 0.6 or not candidate.get("location") or not e.get("location")):
            conf = 0.85 + 0.1 * loc
            if best is None or conf > best.confidence:
                best = DuplicateMatch(e["id"], "same company, title and location", min(conf, 0.95))
    return best


_QUESTION_STOP = set("why do you want to work at the a an in is are of and for this our with about what how would be your it that us here".split())


def question_similarity(a: str, b: str) -> float:
    """Similarity between two application questions (for suggesting reusable answers)."""
    ta = {t for t in re.findall(r"[a-z0-9]+", a.lower()) if t not in _QUESTION_STOP}
    tb = {t for t in re.findall(r"[a-z0-9]+", b.lower()) if t not in _QUESTION_STOP}
    if not ta or not tb:
        return 0.0
    containment = len(ta & tb) / min(len(ta), len(tb))
    return 0.5 * containment + 0.5 * similarity(a, b)
