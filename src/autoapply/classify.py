"""Question classification and per-category answering policy.

Every form field is classified into a category; each category has a policy
that says how the answer may be produced:

  PROFILE  - filled from profile.yaml rules; if the profile has no value the
             question is handed to the user (never inferred).
  INFER    - the LLM may answer from explicit facts or reasonable, direct
             inference from the resume/profile.
  STRICT   - the LLM may answer only from explicit facts; anything else is
             asked of the user (legal / authorization questions).
  DRAFT    - the LLM writes a personalized draft grounded in the resume and
             company/role research; the user must approve it.
  ASK      - always requires the user (personal preference, story, opinion).
  BLANK    - leave blank and ask the user (e.g. "How did you hear about us?").
"""

from __future__ import annotations

import re
from enum import Enum


class Category(str, Enum):
    PERSONAL = "personal_info"
    CONTACT = "contact"
    EDUCATION = "education"
    EMPLOYMENT = "employment"
    SKILLS = "technical_skills"
    WORK_AUTH = "work_authorization"
    SPONSORSHIP = "sponsorship"
    DEMOGRAPHICS = "demographics"
    AVAILABILITY = "availability"
    SALARY = "salary"
    JOB_SOURCE = "job_source"
    COMPANY_MOTIVATION = "company_motivation"
    ROLE_MOTIVATION = "role_motivation"
    BEHAVIORAL = "behavioral"
    SHORT_ANSWER = "short_answer"
    LONG_FORM = "long_form"
    CONSENT = "consent"
    OTHER = "other"


class Policy(str, Enum):
    PROFILE = "profile"
    INFER = "infer"
    STRICT = "strict"
    DRAFT = "draft"
    ASK = "ask"
    BLANK = "blank"


POLICY: dict[Category, Policy] = {
    Category.PERSONAL: Policy.PROFILE,
    Category.CONTACT: Policy.PROFILE,
    Category.EDUCATION: Policy.INFER,
    Category.EMPLOYMENT: Policy.INFER,
    Category.SKILLS: Policy.INFER,
    Category.WORK_AUTH: Policy.STRICT,
    Category.SPONSORSHIP: Policy.STRICT,
    Category.DEMOGRAPHICS: Policy.PROFILE,
    Category.AVAILABILITY: Policy.INFER,
    Category.SALARY: Policy.ASK,
    Category.JOB_SOURCE: Policy.BLANK,
    Category.COMPANY_MOTIVATION: Policy.DRAFT,
    Category.ROLE_MOTIVATION: Policy.DRAFT,
    Category.BEHAVIORAL: Policy.DRAFT,
    Category.SHORT_ANSWER: Policy.INFER,
    Category.LONG_FORM: Policy.INFER,
    Category.CONSENT: Policy.PROFILE,
    Category.OTHER: Policy.INFER,
}


def _r(p: str) -> re.Pattern[str]:
    return re.compile(p, re.I)


# Ordered: first match wins. Specific intents before generic ones.
_PATTERNS: list[tuple[Category, re.Pattern[str]]] = [
    (Category.JOB_SOURCE, _r(r"\bhow did you (hear|learn|find out|discover)\b|\bwhere did you (hear|find|see|learn)\b|"
                             r"\b(referral|application) source\b|\bsource of (application|referral|hire)\b|\bwho referred you\b|"
                             r"\breferred by\b|\bhow (were you|did you get) referred\b")),
    (Category.SALARY, _r(r"\b(salary|compensation|pay (expectation|rate|range)|hourly rate|desired pay|wage|stipend)\b")),
    (Category.SPONSORSHIP, _r(r"\b(sponsor|visa|immigration|h-?1b|(?-i:OPT|CPT)\b|stem extension|work permit status)")),
    (Category.WORK_AUTH, _r(r"\b(authori[sz]ed|eligible|legally|permitted|right) to work\b|\bwork authori[sz]ation\b|"
                            r"\bcitizen|\bsecurity clearance\b|\bclearance\b|\bbackground check\b|\bexport control\b|\bus person\b")),
    (Category.CONSENT, _r(r"\b(acknowledg\w*|consent|agree|privacy (notice|policy|statement|acknowledg\w*)|terms (and|&) conditions|certif\w*|"
                          r"i (have )?read)\b")),
    (Category.DEMOGRAPHICS, _r(r"\b(gender|sex\b|hispanic|latin[oax]|race|ethnic|veteran|military|disabilit|transgender|"
                               r"sexual orientation|pronouns?\b|lgbtq|self[- ]identif|date of birth|birth ?date|\bage\b|"
                               r"first[- ]generation|neurodiver)")),
    (Category.COMPANY_MOTIVATION, _r(r"\bwhy (do you want to|would you like to|are you interested in) (work|join|be)\b.*|"
                                     r"\bwhy (this company|us|here|our company|our team)\b|\bwhy do you want to work (at|for|with)\b|"
                                     r"\bwhy (?-i:[A-Z])[\w&.-]*\??$|\bwhat (motivated|made|inspired|drew|attracted) you to apply\b|"
                                     r"\binterested in (joining|working (at|for|with)) \b|\bwhy are you interested in (?!this |the |a |an |our )")),
    (Category.ROLE_MOTIVATION, _r(r"\bwhy (are you interested in|this role|this position|this internship|do you want this)\b|"
                                  r"\bwhat (excites|interests) you about (this|the) (role|position|opportunity|internship|team)\b|"
                                  r"\bgood fit\b|\bwhy (should we|would you be)\b|\bwhat (do you hope|would you like) to (learn|gain|get)\b|"
                                  r"\bwhat (are you looking for|matters to you) in\b")),
    (Category.BEHAVIORAL, _r(r"\btell (us|me) about a time\b|\bdescribe a (time|situation|challenge|project)\b|"
                             r"\bproudest\b|\bmost proud\b|\bbiggest (challenge|failure|accomplishment)\b|"
                             r"\bhow (would|do) you (handle|approach|deal with)\b|\bexample of (a time|when)\b|"
                             r"\bhow will your personal experiences\b|\bwhat makes you, you\b|\bpersonal (story|experiences)\b")),
    (Category.EMPLOYMENT, _r(r"\b(current (company|employer|job|title)|employer|previous(ly)? (worked|employed|interned)|"
                             r"years of (relevant |work |professional )?experience|internship experience|work experience|"
                             r"job title|have you (worked|interned|been employed)|completed (at least|an) internship|"
                             r"relevant (full[- ]time )?experience|employment)\b")),
    (Category.AVAILABILITY, _r(r"\b(start date|available to start|availability|earliest start|when (can|could|are you able to) (you )?start|"
                               r"available (for|to)|internship (term|dates|period|duration)|which (term|season|semester|summer)|"
                               r"weeks? (available|duration)|full[- ]time|part[- ]time|hours per week|relocat\w*|in[- ]person|on[- ]?site|office location|remote)\b")),
    (Category.EDUCATION, _r(r"\b(school|university|college|institution|degree|major|minor|gpa|grade point|graduat|"
                            r"field of study|discipline|concentration|education|enrolled|class of|academic (year|level|standing)|graduat\w*|"
                            r"year in school|student status|transcript|coursework|end date|start date month|start date year|"
                            r"scholarship|dean'?s list|academic honor|fellowship)\b")),
    (Category.SKILLS, _r(r"\b(programming languages?|technolog(y|ies)|frameworks?|tools?|proficien|skills?|experience (with|in|using) "
                         r"[A-Za-z+#.]+|familiar with|technical (domains?|interests?|areas?)|languages? you know|stack|"
                         r"(python|java|c\+\+|javascript|typescript|react|sql|pytorch|tensorflow|aws|docker|kubernetes)\b)")),
    (Category.CONTACT, _r(r"\b(e-?mail|phone|mobile|telephone|linked ?in|git ?hub|portfolio|website|personal site|url|"
                          r"address|street|city|state|province|zip|postal|country|location)\b")),
    (Category.PERSONAL, _r(r"\b(first name|last name|surname|full name|preferred name|middle name|legal name|"
                           r"\bname\b|pronounc\w*|nickname|over 18|at least 18|minimum age|18 years)\b")),
]

_QUESTION_WORDS = _r(r"\b(why|what|how|describe|tell us|tell me|share|explain)\b")


def classify(label: str, options: list[str] | None = None, field_type: str = "text") -> Category:
    """Classify a form field from its label (and, if useful, its options / type)."""
    lbl = (label or "").strip()
    if not lbl:
        return Category.OTHER
    for cat, pat in _PATTERNS:
        if pat.search(lbl):
            return cat
    if field_type == "textarea":
        return Category.LONG_FORM
    if _QUESTION_WORDS.search(lbl) or lbl.endswith("?"):
        return Category.SHORT_ANSWER
    return Category.OTHER


def policy_for(category: Category) -> Policy:
    return POLICY[category]
