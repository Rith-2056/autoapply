"""Applicant-tracking-system detection and handler registry."""

from __future__ import annotations

import re
from urllib.parse import urlparse

ATS_TYPES = ("greenhouse", "lever", "ashby", "workday", "smartrecruiters", "generic")

# (ats name, hostname regex, optional path/query regex)
_RULES: list[tuple[str, re.Pattern[str], re.Pattern[str] | None]] = [
    ("greenhouse", re.compile(r"(^|\.)greenhouse\.io$"), None),
    ("greenhouse", re.compile(r"^grnh\.se$"), None),
    ("lever", re.compile(r"(^|\.)lever\.co$"), None),
    ("ashby", re.compile(r"(^|\.)ashbyhq\.com$"), None),
    ("workday", re.compile(r"(^|\.)myworkdayjobs\.com$"), None),
    ("workday", re.compile(r"(^|\.)myworkday\.com$"), None),
    ("workday", re.compile(r"(^|\.)myworkdaysite\.com$"), None),
    ("smartrecruiters", re.compile(r"(^|\.)smartrecruiters\.com$"), None),
    # Embedded Greenhouse boards on company sites use ?gh_jid=... or /gh_jid/...
    ("greenhouse", re.compile(r".*"), re.compile(r"gh_jid=|/gh_jid/|greenhouse\.io", re.I)),
]


def detect_ats(url: str) -> str:
    """Return the ATS name for a job URL, or ``"generic"`` if unknown."""
    if not url:
        return "generic"
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return "generic"
    host = (parsed.netloc or "").lower().split("@")[-1].split(":")[0]
    rest = f"{parsed.path}?{parsed.query}"
    for name, host_re, rest_re in _RULES:
        if host_re.search(host) and (rest_re is None or rest_re.search(rest)):
            return name
    return "generic"


def get_handler_class(ats: str):
    """Import lazily so tests can use detect_ats without Playwright installed."""
    from . import ashby, generic, greenhouse, lever, smartrecruiters, workday

    return {
        "greenhouse": greenhouse.GreenhouseHandler,
        "lever": lever.LeverHandler,
        "ashby": ashby.AshbyHandler,
        "workday": workday.WorkdayHandler,
        "smartrecruiters": smartrecruiters.SmartRecruitersHandler,
        "generic": generic.GenericHandler,
    }[ats]
