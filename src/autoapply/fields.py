"""Map form-field labels to profile values.

Every rule is (regex on the normalised label, dotted profile path, kind).
``kind`` tells the filler how to treat the value:
  text   - type it in
  yesno  - the profile value is a Yes/No style answer; pick the matching option
  select - pick the option that best matches the profile value
  date_month / date_year - graduation month/year selects
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Profile


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern[str]
    path: str
    kind: str = "text"


def _r(p: str) -> re.Pattern[str]:
    return re.compile(p, re.I)


# Order matters: first match wins. More specific patterns go first.
RULES: list[Rule] = [
    Rule(_r(r"\bpreferred (first )?name\b"), "name.preferred"),
    Rule(_r(r"\b(first|given) name\b"), "name.first"),
    Rule(_r(r"\b(last|family) name\b|\bsurname\b"), "name.last"),
    Rule(_r(r"^(full )?name$|\byour (full )?name\b|\blegal name\b"), "name.full"),
    Rule(_r(r"\be-?mail\b"), "contact.email"),
    Rule(_r(r"\bphone\b|\bmobile\b|\btelephone\b"), "contact.phone"),
    Rule(_r(r"\blinked ?in\b"), "contact.linkedin"),
    Rule(_r(r"\bgit ?hub\b"), "contact.github"),
    Rule(_r(r"\b(portfolio|website|personal site|personal url|other url|web site)\b"), "contact.website"),
    Rule(_r(r"\b(school|university|college|institution|alma mater)\b"), "education.school", "select"),
    Rule(_r(r"\b(major|field of study|discipline|concentration|area of study)\b"), "education.major", "select"),
    Rule(_r(r"\bgpa\b|\bgrade point\b"), "education.gpa"),
    Rule(_r(r"\b(end|completion) date\b.*\bmonth\b|\bmonth\b.*\b(end|completion) date\b"), "education.graduation_month", "date_month"),
    Rule(_r(r"\b(end|completion) date\b.*\byear\b|\byear\b.*\b(end|completion) date\b"), "education.graduation_year", "date_year"),
    Rule(_r(r"\bstart date\b.*\bmonth\b|\bmonth\b.*\bstart date\b|\b(school|education|program) start\b.*\bmonth\b"), "education.start_month", "date_month"),
    Rule(_r(r"\bstart date\b.*\byear\b|\byear\b.*\bstart date\b|\b(school|education|program) start\b.*\byear\b"), "education.start_year", "date_year"),
    Rule(_r(r"\bgraduat\w*\b.*\bmonth\b|\bmonth\b.*\bgraduat"), "education.graduation_month", "date_month"),
    Rule(_r(r"\bgraduat\w*\b.*\byear\b|\byear\b.*\bgraduat|\bclass of\b"), "education.graduation_year", "date_year"),
    Rule(_r(r"\bgraduat"), "education.graduation", "graduation"),
    Rule(_r(r"\b(degree|education level|level of education|highest level)\b"), "education.degree", "select"),
    Rule(_r(r"\b(current (year|level)|year in school|academic (year|level|standing)|class year|student status)\b"), "education.level", "select"),
    Rule(_r(r"\b(street|address line|address 1|home address|mailing address)\b"), "address.street"),
    Rule(_r(r"\b(zip|postal)\b"), "address.zip"),
    Rule(_r(r"\b(state|province|region)\b"), "address.state", "select"),
    Rule(_r(r"\bcountry\b"), "address.country", "select"),
    Rule(_r(r"\b(city|current location|location|where (are you|do you) (based|located|live))\b"), "address.location"),
    # Only actual sponsorship questions. OPT/CPT/H-1B status questions are not the
    # same fact and go to the strict LLM policy (usually: ask the user).
    Rule(_r(r"\b(sponsor\w*|employment visa|work visa|visa (status|sponsorship))\b"), "work_authorization.requires_sponsorship", "yesno"),
    Rule(_r(r"\b(authori[sz]ed|eligible|legally|permitted|right) to work\b|\bwork authori[sz]ation\b|\bwork permit\b"), "work_authorization.authorized_us", "yesno"),
    Rule(_r(r"\bcitizen"), "work_authorization.citizenship", "select"),
    Rule(_r(r"\brelocat|\bwilling to work (from|in|at) (the |our )?(office|on-?site|in-?person)|\bwork (on-?site|in-?person|from the office)\b"), "work_authorization.willing_to_relocate", "yesno"),
    Rule(_r(r"\btransgender\b"), "eeo.transgender", "select"),
    Rule(_r(r"\bsexual orientation\b"), "eeo.sexual_orientation", "select"),
    Rule(_r(r"\b(gender|sex)\b"), "eeo.gender", "select"),
    Rule(_r(r"\bpronoun"), "eeo.pronouns", "select"),
    Rule(_r(r"\b(hispanic|latin[oax])\b"), "eeo.hispanic", "select"),
    Rule(_r(r"\b(race|ethnic)"), "eeo.race", "select"),
    Rule(_r(r"\bveteran\b|\bmilitary\b"), "eeo.veteran", "select"),
    Rule(_r(r"\bdisabilit"), "eeo.disability", "select"),
    Rule(_r(r"\bhow did you (hear|learn|find)\b|\breferr?al source\b|\bsource of (application|referral)\b|\bwhere did you (hear|find)\b"), "preferences.how_did_you_hear", "select"),
    Rule(_r(r"\b(18|eighteen)\b.*\b(years|age|older)\b|\bat least 18\b|\bover 18\b|\bminimum age\b"), "preferences.over_18", "yesno"),
    Rule(_r(r"\b(previously|formerly|ever) (worked|employed|been employed|interned)\b|\bformer employee\b|\bcurrent(ly)? (an )?employee\b|\bhave you (ever |previously )?(worked|interned|been employed) (at|for|with)\b"), "preferences.previously_employed_here", "yesno"),
    Rule(_r(r"\b(sms|text message|whatsapp)\b"), "preferences.sms_opt_in", "yesno"),
    Rule(_r(r"\b(acknowledg|consent|agree|privacy (notice|policy|statement)|terms (and|&) conditions|certif(y|ication))"), "preferences.consent", "consent"),
    Rule(_r(r"\b(salary|compensation|pay expectation|hourly rate|desired pay)\b"), "preferences.salary_expectation"),
    Rule(_r(r"\b(start date|available to start|availability|earliest start|when (can|could) you start)\b"), "preferences.available_start"),
    Rule(_r(r"\b(term|season|semester|which (summer|internship))\b.*\b(apply|interest|prefer|available)\b|\binternship term\b"), "preferences.term", "select"),
]

# Fields that are not questions for the candidate at all (never asked, shown as n/a).
SKIP_PATTERNS = [
    _r(r"\bcover letter\b"),
    _r(r"\bpassword\b"),
    _r(r"\bpromo|\bcoupon"),
    _r(r"\bsearch\b"),
]

YES_WORDS = re.compile(r"^(yes|y|true)\b", re.I)
NO_WORDS = re.compile(r"^(no|n|false)\b", re.I)

_LABEL_NOISE = re.compile(r"[*✱]|\(required\)|\brequired\b|\(optional\)|\boptional\b|:\s*$", re.I)


def clean_label(label: str) -> str:
    label = re.sub(r"\s+", " ", label or "").strip()
    # Some layouts (Lever) put the select's option text inside the label container.
    label = re.sub(r"\s*\b(Select|Choose)( one)?\s*(\.\.\.|…).*$", "", label, flags=re.I)
    label = _LABEL_NOISE.sub("", label).strip()
    return label


def should_skip(label: str) -> bool:
    return any(p.search(label) for p in SKIP_PATTERNS)


# Identity fields are matched only on short labels ("Email", "Phone Number").
# A long question that merely mentions "email" (e.g. "...we will contact you via
# email...") must not be filled with the email address.
_IDENTITY_PATHS = {"name.first", "name.last", "name.full", "name.preferred", "contact.email",
                   "contact.phone", "contact.linkedin", "contact.github", "contact.website",
                   "education.gpa", "address.street", "address.zip", "address.state", "address.country",
                   "address.location"}
_NON_US_RE = re.compile(r"\b(canada|canadian|uk|u\.k\.|united kingdom|britain|europe|eu|germany|france|india|"
                        r"australia|singapore|japan|china|ireland|netherlands|switzerland|mexico|brazil|israel)\b", re.I)
_US_RE = re.compile(r"\b(us|u\.s\.|usa|united states|america)\b", re.I)


def match_rule(label: str) -> Rule | None:
    lbl = clean_label(label)
    if not lbl:
        return None
    for rule in RULES:
        if rule.pattern.search(lbl):
            if rule.path in _IDENTITY_PATHS and (len(lbl) > 45 or "?" in lbl):
                continue
            if rule.path == "contact.email" and re.search(r"\b(alternate|alternative|secondary|other|additional)\b", lbl, re.I):
                return None
            if rule.path == "work_authorization.authorized_us" and _NON_US_RE.search(lbl) and not _US_RE.search(lbl):
                # Authorization for another country: profile can't answer it.
                return None
            return rule
    return None


def profile_value(profile: Profile, rule: Rule) -> str:
    val = profile.get(rule.path, "")
    return str(val) if val not in (None, "") else ""


def yes_no(value: str) -> str | None:
    """Reduce a profile value like 'Yes, I am authorized...' to 'Yes' / 'No'."""
    v = (value or "").strip()
    if YES_WORDS.match(v):
        return "Yes"
    if NO_WORDS.match(v):
        return "No"
    return None


def choose_option(options: list[str], value: str, kind: str = "select") -> str | None:
    """Pick the option that best matches ``value``. None if no sensible match."""
    if not options or not value:
        return None
    v = value.strip().lower()
    normalized = [(o, o.strip().lower()) for o in options]

    # 1. exact
    for o, n in normalized:
        if n == v:
            return o
    # 2. yes / no semantics
    yn = yes_no(value) if kind == "yesno" else None
    if yn:
        target = yn.lower()
        for o, n in normalized:
            if n == target or n.startswith(target + ",") or n.startswith(target + " ") or n.startswith(target + "."):
                return o
        # Sponsorship questions sometimes phrase options as "I will (not) require..."
        for o, n in normalized:
            if target == "no" and re.search(r"\b(do not|don't|will not|won't|not)\b", n):
                return o
            if target == "yes" and re.search(r"\b(will|do) (require|need)\b", n) and "not" not in n:
                return o
    # 3. substring either way (avoid "male" matching "female")
    for o, n in normalized:
        if v in n and not (v == "male" and "female" in n):
            return o
    for o, n in normalized:
        if n and n in v and len(n) > 2:
            return o
    # 4. significant-token overlap (possessives and filler words ignored)
    vt = _tokens(v)
    best, best_score = None, 0.0
    for o, n in normalized:
        ot = _tokens(n)
        if not ot or not vt:
            continue
        score = len(vt & ot) / min(len(ot), len(vt))
        if score > best_score:
            best, best_score = o, score
    return best if best_score >= 0.6 else None


_STOPWORDS = {"of", "the", "a", "an", "in", "and", "or", "degree", "to", "i", "am", "is", "my"}


def _tokens(text: str) -> set[str]:
    toks = re.findall(r"[a-z0-9]+(?:'s)?", text.lower())
    return {t[:-2] if t.endswith("'s") else t for t in toks} - _STOPWORDS


MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]


def choose_month(options: list[str], month_name: str) -> str | None:
    m = (month_name or "").strip().lower()
    if not m:
        return None
    idx = next((i for i, name in enumerate(MONTHS) if name.startswith(m[:3])), None)
    for o in options:
        ol = o.strip().lower()
        if ol == m or ol.startswith(m[:3]) and ol[:3] == m[:3]:
            return o
        if idx is not None and ol in (str(idx + 1), f"{idx + 1:02d}"):
            return o
    return None


_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def choose_numeric_range(options: list[str], value: str) -> str | None:
    """Pick the option whose numeric range contains ``value`` (e.g. GPA '3.5 - 4.0')."""
    try:
        v = float(_NUM_RE.search(value).group())  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        return None
    for o in options:
        nums = [float(x) for x in _NUM_RE.findall(o)]
        ol = o.lower()
        if len(nums) >= 2 and min(nums) <= v <= max(nums):
            return o
        if len(nums) == 1:
            n = nums[0]
            if ("+" in o or "above" in ol or "or higher" in ol or "and above" in ol or ">" in o) and v >= n:
                return o
            if ("below" in ol or "under" in ol or "less than" in ol or "<" in o) and v < n:
                return o
    return None


CONSENT_VALUES = ["Yes", "I acknowledge", "I agree", "Acknowledge", "Agree", "Accept", "I accept", "I consent", "I certify", "Confirm", "I have read"]


def choose_consent(options: list[str]) -> str | None:
    for v in CONSENT_VALUES:
        pick = choose_option(options, v, "select")
        if pick and not re.search(r"\b(no|not|decline|disagree)\b", pick, re.I):
            return pick
    return None


_SEASON_FOR_MONTH = {"january": "winter", "february": "winter", "march": "spring", "april": "spring", "may": "spring",
                     "june": "summer", "july": "summer", "august": "summer", "september": "fall", "october": "fall",
                     "november": "fall", "december": "winter"}


def choose_graduation(options: list[str], month: str, year: str | int) -> str | None:
    """Pick a graduation option like 'Spring 2028', 'May 2028', '2028', '2027-2028'."""
    y = str(year).strip()
    m = (month or "").strip().lower()
    if not y:
        return None
    with_year = [o for o in options if y in o]
    if not with_year:
        return None
    if len(with_year) == 1:
        return with_year[0]
    if m:
        season = _SEASON_FOR_MONTH.get(m, "")
        for o in with_year:
            ol = o.lower()
            if m in ol or m[:3] in ol.split() or (season and season in ol):
                return o
    return None
