"""Match an email to one of the user's applications using several signals."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from ..tracker.dedupe import external_job_id, similarity

AUTO_LINK = 0.8
ASK = 0.5

_GENERIC_DOMAINS = {"gmail.com", "greenhouse.io", "greenhouse-mail.io", "lever.co", "hire.lever.co", "ashbyhq.com", "myworkday.com",
                    "myworkdayjobs.com", "smartrecruiters.com", "icims.com", "hackerrank.com", "codesignal.com", "calendly.com", "workday.com"}


@dataclass
class MatchCandidate:
    application_id: int
    company: str
    title: str
    score: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"application_id": self.application_id, "company": self.company, "title": self.title, "score": round(self.score, 2), "reasons": self.reasons}


@dataclass
class MatchResult:
    status: str  # auto | needs_confirmation | unmatched
    application_id: int | None
    confidence: float
    candidates: list[MatchCandidate]


def _norm_company(s: str) -> str:
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|technology|labs|group|holdings)\b\.?", " ", s.lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _company_tokens(s: str) -> set[str]:
    return {t for t in _norm_company(s).split() if len(t) > 1}


def _domain(sender: str) -> str:
    m = re.search(r"@([\w.-]+)", sender)
    return m.group(1).lower() if m else ""


def match_email(cls_company: str, cls_role: str, sender: str, subject: str, body: str, urls: list[str],
                applications: list[dict[str, Any]], received_at: datetime | None = None) -> MatchResult:
    """Score every application; decide auto / ask / unmatched.

    ``applications`` rows need: id, company, title, job_url, application_url, external_job_id, date_applied/created_at, status.
    """
    text = f"{subject}\n{body}".lower()
    sender_domain = _domain(sender)
    sender_root = ".".join(sender_domain.split(".")[-2:]) if sender_domain else ""
    email_ids = {external_job_id(u) for u in urls if u} - {""}
    cands: list[MatchCandidate] = []
    for app in applications:
        score = 0.0
        reasons: list[str] = []
        app_company = app.get("company", "")
        ctoks = _company_tokens(app_company)
        # company name in classification or text
        if cls_company and _norm_company(cls_company) and _norm_company(cls_company) == _norm_company(app_company):
            score += 0.45
            reasons.append("company matches")
        elif ctoks and all(t in text for t in ctoks):
            score += 0.35
            reasons.append("company mentioned")
        elif cls_company and similarity(cls_company, app_company) > 0.85:
            score += 0.3
            reasons.append("company similar")
        # sender domain
        if sender_root and sender_root not in _GENERIC_DOMAINS and ctoks and any(t in sender_root for t in ctoks if len(t) > 3):
            score += 0.25
            reasons.append("sender domain")
        # job id / url
        app_ext = app.get("external_job_id") or external_job_id(app.get("job_url", "") or "")
        if app_ext and (app_ext in email_ids or app_ext in text):
            score += 0.4
            reasons.append("job id")
        for u in urls:
            if u and app.get("job_url") and urlparse(u).netloc == urlparse(app["job_url"]).netloc and urlparse(u).path.rstrip("/") == urlparse(app["job_url"]).path.rstrip("/"):
                score += 0.4
                reasons.append("job url")
                break
        # role
        if cls_role and app.get("title"):
            sim = similarity(cls_role, app["title"])
            if sim >= 0.9:
                score += 0.25
                reasons.append("role matches")
            elif sim >= 0.7:
                score += 0.12
                reasons.append("role similar")
            elif score > 0 and sim < 0.4:
                score -= 0.15
                reasons.append("role differs")
        elif app.get("title") and similarity(app["title"], subject) >= 0.8:
            score += 0.15
            reasons.append("title in subject")
        # recency: emails normally arrive after the application
        applied = app.get("date_applied") or app.get("created_at")
        if received_at and applied:
            try:
                ad = datetime.fromisoformat(applied)
                if received_at < ad:
                    score -= 0.3
                    reasons.append("email before application")
            except ValueError:
                pass
        if score > 0.15:
            cands.append(MatchCandidate(app["id"], app_company, app.get("title", ""), min(score, 1.0), reasons))
    cands.sort(key=lambda c: c.score, reverse=True)
    if not cands:
        return MatchResult("unmatched", None, 0.0, [])
    top = cands[0]
    # Several applications at the same company with the same score: ambiguous -> ask.
    ambiguous = len(cands) > 1 and cands[1].score >= top.score - 0.1
    if top.score >= AUTO_LINK and not ambiguous:
        return MatchResult("auto", top.application_id, top.score, cands[:5])
    if top.score >= ASK or ambiguous and top.score >= 0.35:
        return MatchResult("needs_confirmation", None, top.score, cands[:5])
    return MatchResult("unmatched", None, top.score, cands[:5])
