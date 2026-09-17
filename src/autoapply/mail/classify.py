"""Classify application emails and extract structured information.

Rules run first (fast, no cost, and enough for the common cases). The LLM is
used only for emails the rules consider job-related but cannot pin down, and
for structured extraction (company, role, platform, deadline text, action).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .deadlines import parse_deadline

log = logging.getLogger("autoapply.mail.classify")

CATEGORIES = ["APPLICATION_CONFIRMATION", "ASSESSMENT", "INTERVIEW", "INTERVIEW_SCHEDULING", "REJECTION",
              "RECRUITER_CONTACT", "ADDITIONAL_INFORMATION", "STATUS_UPDATE", "DEADLINE", "OFFER", "UNKNOWN", "IRRELEVANT"]

_R = lambda p: re.compile(p, re.I)  # noqa: E731

JOB_SIGNALS = _R(r"\b(application|applied|applying|position|role|internship|intern|candidate|recruit|hiring|interview|assessment|"
                 r"hackerrank|codesignal|codility|coderpad|karat|hirevue|offer|talent|careers?|job|req(uisition)?|opportunity)\b")
NOISE = _R(r"\b(unsubscribe from job alerts|job alert|jobs you may like|recommended jobs|new jobs? (for you|matching)|"
           r"newsletter|webinar|career fair|weekly digest|daily digest|% off|sale|coupon|invoice|receipt|your order)\b")
ATS_SENDERS = _R(r"(greenhouse|lever\.co|ashbyhq|myworkday|workday|smartrecruiters|icims|taleo|jobvite|workable|successfactors|"
                 r"oraclecloud|hackerrank|codesignal|codility|karat|hirevue|brassring|rippling|bamboohr|dover\.com|wellfound)")

RULES: list[tuple[str, re.Pattern[str]]] = [
    ("OFFER", _R(r"\b(offer letter|pleased to offer|extend (you )?an offer|offer of employment|congratulations.*(offer|selected))\b")),
    ("REJECTION", _R(r"\b(not (be )?moving forward|decided (not )?to (move|proceed)|other candidates|unfortunately|regret to inform|"
                     r"not selected|will not be (proceeding|moving)|no longer (under )?consideration|position has been filled|"
                     r"pursue other candidates|not able to offer|we have decided to)\b")),
    ("ASSESSMENT", _R(r"\b(online assessment|coding (challenge|assessment|test)|technical assessment|hackerrank|codesignal|codility|"
                      r"coderpad|karat|take[- ]home|complete (the|your|this) (assessment|challenge|test)|\bOA\b|hirevue|"
                      r"video (assessment|interview) invitation|assessment (invitation|link))\b")),
    ("INTERVIEW_SCHEDULING", _R(r"\b(schedule (your|an|the) (interview|call|conversation)|book (a|your) (time|slot|interview)|"
                                r"select (a|your) (time|slot)|availability for (an|a|the) (interview|call)|calendly|"
                                r"pick a time|choose a time|interview scheduling|scheduling link)\b")),
    ("INTERVIEW", _R(r"\b(interview (invitation|confirmed|confirmation|details|is scheduled|has been scheduled)|"
                     r"invite you to (an )?interview|next (round|step|stage).*interview|phone screen|technical (phone )?screen|"
                     r"onsite|on-site interview|final (round|interview)|panel interview|meet (the|our) team)\b")),
    ("ADDITIONAL_INFORMATION", _R(r"\b(additional information|missing information|please (provide|submit|upload|complete)|"
                                  r"complete your (application|profile)|action required|required documents|"
                                  r"background check|reference check|work authorization documents)\b")),
    ("APPLICATION_CONFIRMATION", _R(r"\b(thank you for (applying|your application|your interest)|application (received|has been received|"
                                    r"submitted|confirmation|was successfully)|we('ve| have) received your application|"
                                    r"successfully (applied|submitted)|your application to)\b")),
    ("RECRUITER_CONTACT", _R(r"\b(recruiter|talent (partner|acquisition)|reaching out|would love to (chat|connect|talk)|"
                             r"quick (chat|call)|connect with you|introduce myself)\b")),
    ("STATUS_UPDATE", _R(r"\b(application (status|update)|update on your application|status of your application|"
                         r"under review|being reviewed|reviewing your application|still (reviewing|considering))\b")),
]

PLATFORMS = _R(r"\b(hackerrank|codesignal|codility|coderpad|karat|hirevue|leetcode|glider|imocha|testgorilla|calendly|goodtime|"
               r"greenhouse|lever|ashby|workday|smartrecruiters|icims|taleo)\b")


@dataclass
class EmailMessage:
    id: str
    subject: str
    sender: str
    body: str
    received_at: datetime
    snippet: str = ""
    urls: list[str] = field(default_factory=list)
    to: str = ""


@dataclass
class Classification:
    category: str
    confidence: float
    company: str = ""
    role: str = ""
    platform: str = ""
    action_required: bool = False
    action_title: str = ""
    deadline_text: str = ""
    deadline: datetime | None = None
    priority: str = "medium"
    summary: str = ""
    job_related: bool = True
    source: str = "rules"  # rules | llm

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category, "confidence": self.confidence, "company": self.company, "role": self.role,
            "platform": self.platform, "action_required": self.action_required, "action_title": self.action_title,
            "deadline_text": self.deadline_text, "deadline": self.deadline.isoformat(timespec="minutes") if self.deadline else None,
            "priority": self.priority, "summary": self.summary, "job_related": self.job_related, "source": self.source,
        }


_ROLE_RE = _R(r"\b(?:for|to|as|regarding)\s+(?:the\s+|our\s+|a\s+)?((?:(?-i:[A-Z])[\w+/.-]*\s+){0,5}?(?:intern(?:ship)?|engineer|developer|analyst|scientist|"
              r"researcher|designer|manager|associate)(?:\s*[-–,]?\s*(?:summer|fall|spring)\s*20\d\d)?)(?=\s+position|\s+role|[.,!\n]|\s*$|\s+at\b|\s+with\b|\s+-)")
_COMPANY_FROM_SENDER = _R(r"^(.*?)\s*<")
_COMPANY_AT = _R(r"\b(?:applying to|application (?:to|with)|applied to|interest in|position at|role at|opportunity at|at|with|from|join(?:ing)?)\s+"
                 r"((?-i:[A-Z])[\w&.'-]*(?:\s+(?-i:[A-Z&])[\w&.'-]*){0,3})(?=[.,!:\n]|\s+(?:for|as|team|is|has|and|to|we|our)\b)")
_COMPANY_SUBJECT = _R(r"^\s*((?-i:[A-Z])[\w&.'-]*(?:\s+(?-i:[A-Z&])[\w&.'-]*){0,3})\s*[:|\-–]\s")


def is_job_related(msg: EmailMessage) -> bool:
    text = f"{msg.subject}\n{msg.body[:4000]}"
    if NOISE.search(text) and not ATS_SENDERS.search(msg.sender):
        return False
    return bool(JOB_SIGNALS.search(text) or ATS_SENDERS.search(msg.sender))


def rule_classify(msg: EmailMessage) -> Classification:
    text = f"{msg.subject}\n{msg.body[:6000]}"
    if not is_job_related(msg):
        return Classification("IRRELEVANT", 0.9, job_related=False)
    category, conf = "UNKNOWN", 0.3
    subject_hit = None
    for cat, pat in RULES:
        if pat.search(msg.subject):
            subject_hit = cat
            break
    body_hits = [cat for cat, pat in RULES if pat.search(text)]
    if subject_hit:
        category, conf = subject_hit, 0.85
    elif body_hits:
        category, conf = body_hits[0], 0.7 if len(body_hits) == 1 else 0.55
    # A rejection phrase beats an earlier confirmation phrase in the same mail.
    if "REJECTION" in body_hits and category in ("APPLICATION_CONFIRMATION", "STATUS_UPDATE", "UNKNOWN"):
        category, conf = "REJECTION", max(conf, 0.75)
    cls = Classification(category, conf)
    cls.platform = (PLATFORMS.search(text) or [None, ""])[1].lower() if PLATFORMS.search(text) else ""
    m = _ROLE_RE.search(msg.subject) or _ROLE_RE.search(msg.body[:2000])
    if m:
        cls.role = re.sub(r"\s+", " ", m.group(1)).strip(" -–,")
    m = _COMPANY_AT.search(msg.subject) or _COMPANY_SUBJECT.search(msg.subject) or _COMPANY_AT.search(msg.body[:1500])
    if m:
        cls.company = re.sub(r"\b(the|your|our|this)\b", "", m.group(1), flags=re.I).strip(" -:")
    if not cls.company:
        m = _COMPANY_FROM_SENDER.search(msg.sender)
        if m and m.group(1).strip() and "@" not in m.group(1):
            name = re.sub(r"\b(recruiting|careers|talent|team|hr|no-?reply|notifications?|jobs)\b", "", m.group(1), flags=re.I).strip(" -|")
            cls.company = name
    cls.deadline = parse_deadline(text, msg.received_at)
    if cls.deadline:
        dm = re.search(r"(within [^.\n]{0,40}|by [^.\n]{0,40}|before [^.\n]{0,40}|due [^.\n]{0,40}|expires? [^.\n]{0,40})", text, re.I)
        cls.deadline_text = dm.group(1).strip() if dm else ""
    cls.action_required = category in ("ASSESSMENT", "INTERVIEW_SCHEDULING", "ADDITIONAL_INFORMATION", "OFFER") or (category == "INTERVIEW" and bool(re.search(r"\b(confirm|schedule|select|choose|reply)\b", text, re.I)))
    cls.action_title = {
        "ASSESSMENT": "Complete online assessment" + (f" ({cls.platform})" if cls.platform else ""),
        "INTERVIEW_SCHEDULING": "Schedule interview",
        "INTERVIEW": "Prepare for interview",
        "ADDITIONAL_INFORMATION": "Provide requested information",
        "OFFER": "Review offer",
    }.get(category, "")
    cls.priority = "high" if category in ("ASSESSMENT", "OFFER", "INTERVIEW_SCHEDULING") or (cls.deadline and (cls.deadline - msg.received_at).days <= 3) else ("medium" if category in ("INTERVIEW", "ADDITIONAL_INFORMATION", "RECRUITER_CONTACT") else "low")
    cls.summary = msg.subject
    return cls


LLM_SYSTEM = """You classify emails about a job seeker's internship applications and extract structured facts. Categories:
APPLICATION_CONFIRMATION, ASSESSMENT, INTERVIEW, INTERVIEW_SCHEDULING, REJECTION, RECRUITER_CONTACT, ADDITIONAL_INFORMATION, STATUS_UPDATE, DEADLINE, OFFER, UNKNOWN, IRRELEVANT (not about one of the user's applications: newsletters, job alerts, marketing).
Extract only what the email states. company: the employer (not the ATS vendor). role: the job title as written. platform: assessment/scheduling tool if named. deadline_text: the exact phrase stating a deadline, or "". action_required: true only if the email asks the recipient to do something. action_title: a short imperative ("Complete HackerRank assessment"). priority: high for assessments/offers/near deadlines, medium for interviews/info requests, low otherwise. confidence 0-1. summary: one sentence. Return JSON only."""

LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "confidence": {"type": "number"},
        "company": {"type": "string"}, "role": {"type": "string"}, "platform": {"type": "string"},
        "action_required": {"type": "boolean"}, "action_title": {"type": "string"},
        "deadline_text": {"type": "string"}, "priority": {"type": "string", "enum": ["high", "medium", "low"]},
        "summary": {"type": "string"},
    },
    "required": ["category", "confidence", "company", "role", "platform", "action_required", "action_title", "deadline_text", "priority", "summary"],
    "additionalProperties": False,
}


class EmailClassifier:
    def __init__(self, api_key: str = "", model: str = "claude-opus-5", use_llm: bool = True, llm_threshold: float = 0.8):
        self.api_key = api_key
        self.model = model
        self.use_llm = use_llm and bool(api_key)
        self.llm_threshold = llm_threshold
        self._client = None

    def classify(self, msg: EmailMessage, llm_result: dict[str, Any] | None = None) -> Classification:
        rules = rule_classify(msg)
        if not rules.job_related and llm_result is None:
            return rules
        if rules.confidence >= self.llm_threshold and rules.company and llm_result is None:
            return rules
        data = llm_result if llm_result is not None else (self._llm(msg) if self.use_llm else None)
        if not data:
            return rules
        cls = Classification(
            category=str(data.get("category", rules.category)),
            confidence=float(data.get("confidence", 0.6)),
            company=str(data.get("company") or rules.company),
            role=str(data.get("role") or rules.role),
            platform=str(data.get("platform") or rules.platform).lower(),
            action_required=bool(data.get("action_required", rules.action_required)),
            action_title=str(data.get("action_title") or rules.action_title),
            deadline_text=str(data.get("deadline_text") or rules.deadline_text),
            priority=str(data.get("priority") or rules.priority),
            summary=str(data.get("summary") or rules.summary),
            job_related=str(data.get("category")) != "IRRELEVANT",
            source="llm",
        )
        cls.deadline = parse_deadline(cls.deadline_text, msg.received_at) or rules.deadline
        return cls

    def _llm(self, msg: EmailMessage) -> dict[str, Any] | None:
        try:
            import anthropic

            if self._client is None:
                self._client = anthropic.Anthropic(api_key=self.api_key)
            user = f"From: {msg.sender}\nSubject: {msg.subject}\nDate: {msg.received_at.isoformat()}\n\n{msg.body[:6000]}"
            resp = self._client.messages.create(model=self.model, max_tokens=800, system=LLM_SYSTEM, messages=[{"role": "user", "content": user}],
                                                output_config={"format": {"type": "json_schema", "schema": LLM_SCHEMA}})
            if resp.stop_reason == "refusal":
                return None
            return json.loads(next((b.text for b in resp.content if b.type == "text"), "{}"))
        except Exception as e:  # noqa: BLE001
            log.warning("email LLM classification failed: %s", e)
            return None
