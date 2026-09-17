"""Decision engine: decide how each form question gets its answer.

For every question the planner produces a ``Decision`` with one of these statuses:

  filled      - a value was produced (source: profile | inferred)
  draft       - the LLM wrote a personalized draft that the user must approve
  needs_user  - the user has to answer (subjective, unknown, risky, or left
                blank by policy such as "How did you hear about us?")
  n/a         - not a candidate question (e.g. password, cover-letter upload)

Nothing is ever silently skipped: every question ends up in exactly one bucket,
with a reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .classify import Category, Policy, classify, policy_for
from .config import Profile
from .fields import (
    choose_consent,
    choose_graduation,
    choose_month,
    choose_numeric_range,
    choose_option,
    clean_label,
    match_rule,
    profile_value,
    should_skip,
)
from .llm import BatchQuestion, JobContext, QuestionAnswerer

log = logging.getLogger("autoapply.planner")

GENDER_ALIASES = {"male": ["man", "male", "men"], "female": ["woman", "female", "women"]}


@dataclass
class Question:
    id: str
    label: str
    kind: str  # text | textarea | select | radio | checkbox_group | combobox | checkbox
    options: list[str] | None = None
    required: bool = False
    maxlength: int | None = None
    name: str = ""
    multi: bool = False
    option_ids: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    question: Question
    status: str  # filled | draft | needs_user | n/a
    value: str = ""
    source: str = ""  # profile | inferred | draft | n/a
    category: str = ""
    reason: str = ""


class Planner:
    def __init__(self, profile: Profile, llm: QuestionAnswerer, job: JobContext | None = None, platform: str = "", platforms=None):
        self.profile = profile
        self.llm = llm
        self.job = job
        self.platform = platform
        self.platforms = platforms  # PlatformProfiles | None

    # ------------------------------------------------------------------ #

    def plan(self, questions: list[Question]) -> list[Decision]:
        decisions: dict[str, Decision] = {}
        to_llm: list[tuple[Question, BatchQuestion]] = []

        for q in questions:
            label = clean_label(q.label) or q.name
            if should_skip(label):
                decisions[q.id] = Decision(q, "n/a", source="n/a", category="n/a", reason="not a candidate question")
                continue
            cat = classify(label, q.options, q.kind)
            policy = policy_for(cat)

            # 0. Platform answer profile: an answer configured for this exact recurring question.
            if self.platforms and self.platform:
                configured = self.platforms.answer_for(self.platform, label)
                if configured:
                    chosen = self._resolve_with_options("select", "platform", configured, q) if q.options else configured
                    if chosen is not None:
                        decisions[q.id] = Decision(q, "filled", chosen, "profile", cat.value, f"{self.platform} answer profile")
                        continue

            # 1. Profile rules (explicit facts), with platform field overrides winning over profile.yaml.
            rule = match_rule(label)
            rule_value = profile_value(self.profile, rule) if rule else ""
            if rule and self.platforms and self.platform:
                override = self.platforms.field_override(self.platform, rule.path)
                if override:
                    rule_value = override
            if rule and rule_value:
                chosen = self._resolve_with_options(rule.kind, rule.path, rule_value, q)
                if chosen is not None:
                    decisions[q.id] = Decision(q, "filled", chosen, "profile", cat.value, f"profile: {rule.path}")
                    continue
                # Known field, but the options didn't line up with the profile value:
                # let the LLM map it (it sees the options and the fact), unless policy forbids.
                if policy in (Policy.ASK, Policy.BLANK):
                    decisions[q.id] = Decision(q, "needs_user", "", "", cat.value, self._ask_reason(cat, policy))
                    continue
                to_llm.append((q, self._batch_q(q, label, cat, "strict" if policy in (Policy.PROFILE, Policy.STRICT) else "infer")))
                continue
            if rule and not rule_value:
                # The profile deliberately has no value ([FILL IN] / empty).
                if cat in (Category.DEMOGRAPHICS, Category.WORK_AUTH, Category.SPONSORSHIP, Category.JOB_SOURCE, Category.SALARY) or policy in (Policy.PROFILE, Policy.ASK, Policy.BLANK):
                    reason = self._ask_reason(cat, policy) if policy in (Policy.ASK, Policy.BLANK) else f"profile.yaml has no value for {rule.path}"
                    decisions[q.id] = Decision(q, "needs_user", "", "", cat.value, reason)
                    continue

            # 2. Category policy.
            if policy in (Policy.ASK, Policy.BLANK):
                decisions[q.id] = Decision(q, "needs_user", "", "", cat.value, self._ask_reason(cat, policy))
                continue
            if policy == Policy.PROFILE:
                # Personal / contact / demographic question with no matching rule: never guess.
                decisions[q.id] = Decision(q, "needs_user", "", "", cat.value, "personal detail not in profile.yaml")
                continue
            llm_policy = {Policy.DRAFT: "draft", Policy.STRICT: "strict"}.get(policy, "infer")
            to_llm.append((q, self._batch_q(q, label, cat, llm_policy)))

        # 3. One batched LLM call for everything left.
        if to_llm:
            if not self.llm.enabled:
                for q, bq in to_llm:
                    decisions[q.id] = Decision(q, "needs_user", "", "", bq.category, "no ANTHROPIC_API_KEY; cannot infer")
            else:
                results = self.llm.resolve_batch([bq for _, bq in to_llm], self.job)
                for q, bq in to_llm:
                    d = results.get(q.id)
                    if d is None or d.decision == "ask_user":
                        decisions[q.id] = Decision(q, "needs_user", "", "", bq.category, (d.reason if d else "no decision"))
                    elif d.decision == "draft":
                        decisions[q.id] = Decision(q, "draft", d.answer, "draft", bq.category, d.reason or "personalized draft; needs your approval")
                    else:
                        decisions[q.id] = Decision(q, "filled", d.answer, "inferred", bq.category, f"{d.confidence}: {d.reason}")

        return [decisions[q.id] for q in questions]

    # ------------------------------------------------------------------ #

    @staticmethod
    def _ask_reason(cat: Category, policy: Policy) -> str:
        if cat == Category.JOB_SOURCE:
            return "left blank by policy: only you know how you found this job"
        if cat == Category.SALARY:
            return "compensation questions are always yours to answer"
        return "personal preference / opinion: needs your answer"

    def _batch_q(self, q: Question, label: str, cat: Category, policy: str) -> BatchQuestion:
        return BatchQuestion(
            id=q.id,
            label=label,
            options=q.options,
            kind=q.kind,
            required=q.required,
            maxlength=q.maxlength,
            policy=policy,
            category=cat.value,
            multi=q.multi,
        )

    def _resolve_with_options(self, kind: str, path: str, value: str, q: Question) -> str | None:
        """Map a profile value onto the field. Returns None if the options don't fit."""
        options = q.options
        if not options:
            return value
        if kind == "date_month":
            return choose_month(options, value)
        if kind == "date_year":
            return next((o for o in options if value in o), None)
        if kind == "graduation":
            return choose_graduation(options, str(self.profile.get("education.graduation_month", "")), str(self.profile.get("education.graduation_year", "")))
        if kind == "consent":
            return choose_consent(options)
        chosen = choose_option(options, value, kind)
        if not chosen and path == "education.gpa":
            chosen = choose_numeric_range(options, value)
        if not chosen and path == "eeo.gender":
            for alias in GENDER_ALIASES.get(value.strip().lower(), []):
                chosen = choose_option(options, alias, "select")
                if chosen:
                    break
        if not chosen and path.startswith("eeo.") and value.lower().startswith(("decline", "prefer not")):
            chosen = choose_option(options, "decline", "select") or choose_option(options, "prefer not", "select")
        return chosen
