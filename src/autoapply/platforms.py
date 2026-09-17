"""Platform-specific answer profiles (Workday, Greenhouse, Lever, Ashby, SmartRecruiters, generic).

``config/platforms.yaml`` holds, per platform:

  fields:   overrides keyed by profile path (e.g. ``education.major: Computer Science``)
            that win over ``profile.yaml`` on that platform only
  answers:  recurring question text -> answer, applied when the same question
            (normalised) appears again on that platform

The set of fields is not hard-coded: a canonical catalogue seeds the UI, and
every question actually encountered on a platform is recorded in the tracker
(``platform_questions``) so the user can set an answer for it.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import yaml

from .config import CONFIG_DIR, Profile

PLATFORMS = ["workday", "greenhouse", "lever", "ashby", "smartrecruiters", "generic"]

# (profile path, label). Seeds the per-platform settings page; the profile value is the default.
CANONICAL_FIELDS: list[tuple[str, str]] = [
    ("name.first", "First name"), ("name.last", "Last name"), ("name.preferred", "Preferred name"),
    ("contact.email", "Email"), ("contact.phone", "Phone"), ("contact.linkedin", "LinkedIn"), ("contact.github", "GitHub"),
    ("contact.website", "Portfolio / website"),
    ("address.street", "Address"), ("address.city", "City"), ("address.state", "State"), ("address.zip", "ZIP code"),
    ("address.country", "Country"), ("address.location", "Location (single field)"),
    ("education.school", "School"), ("education.degree", "Degree"), ("education.major", "Major"), ("education.gpa", "GPA"),
    ("education.start", "Education start"), ("education.graduation", "Graduation date"), ("education.level", "Current level"),
    ("work_authorization.authorized_us", "Work authorization"), ("work_authorization.requires_sponsorship", "Requires sponsorship"),
    ("work_authorization.willing_to_relocate", "Willing to relocate"), ("work_authorization.citizenship", "Citizenship"),
    ("preferences.available_start", "Available start date"), ("preferences.how_did_you_hear", "How did you hear about us"),
    ("preferences.previously_employed_here", "Previously employed here"), ("preferences.salary_expectation", "Salary expectation"),
    ("eeo.gender", "Gender"), ("eeo.race", "Race / ethnicity"), ("eeo.hispanic", "Hispanic / Latino"),
    ("eeo.veteran", "Veteran status"), ("eeo.disability", "Disability status"),
]


def normalize_question(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    t = re.sub(r"[*✱]|\(required\)|\(optional\)", "", t)
    return re.sub(r"[^a-z0-9 ?]", "", t).strip(" ?")


class PlatformProfiles:
    def __init__(self, path: Path | None = None):
        self.path = path or CONFIG_DIR / "platforms.yaml"
        self._lock = threading.Lock()
        self.data: dict[str, dict[str, Any]] = {}
        self.load()

    # -- persistence --------------------------------------------------- #

    def load(self) -> None:
        if self.path.exists():
            with self.path.open() as f:
                raw = yaml.safe_load(f) or {}
        else:
            raw = {}
        self.data = {p: {"fields": dict((raw.get(p) or {}).get("fields") or {}), "answers": dict((raw.get(p) or {}).get("answers") or {})} for p in PLATFORMS}
        for p, v in raw.items():
            if p not in self.data and isinstance(v, dict):
                self.data[p] = {"fields": dict(v.get("fields") or {}), "answers": dict(v.get("answers") or {})}

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            out = {p: {"fields": {k: v for k, v in d["fields"].items() if v not in (None, "")},
                       "answers": {k: v for k, v in d["answers"].items() if v not in (None, "")}} for p, d in self.data.items()}
            self.path.write_text("# Platform-specific answers. fields: override profile.yaml paths on that platform.\n"
                                 "# answers: exact recurring question -> answer. Edited from Settings → Application platforms.\n"
                                 + yaml.safe_dump(out, sort_keys=False, allow_unicode=True), encoding="utf-8")

    # -- lookups ---------------------------------------------------------- #

    def field_override(self, platform: str, path: str) -> str:
        v = (self.data.get(platform) or {}).get("fields", {}).get(path)
        return str(v) if v not in (None, "") else ""

    def answer_for(self, platform: str, question: str) -> str:
        answers = (self.data.get(platform) or {}).get("answers", {})
        key = normalize_question(question)
        if not key:
            return ""
        for q, a in answers.items():
            if normalize_question(q) == key and a not in (None, ""):
                return str(a)
        return ""

    def set_fields(self, platform: str, fields: dict[str, Any]) -> None:
        self.data.setdefault(platform, {"fields": {}, "answers": {}})["fields"].update(fields)
        self.save()

    def set_answer(self, platform: str, question: str, answer: str) -> None:
        d = self.data.setdefault(platform, {"fields": {}, "answers": {}})
        if answer in (None, ""):
            d["answers"].pop(question, None)
        else:
            d["answers"][question] = answer
        self.save()

    def effective_fields(self, platform: str, profile: Profile) -> list[dict[str, Any]]:
        """Canonical fields with the profile default and the platform override."""
        out = []
        for path, label in CANONICAL_FIELDS:
            out.append({"path": path, "label": label, "default": str(profile.get(path, "") or ""), "override": self.field_override(platform, path)})
        return out
