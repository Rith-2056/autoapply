"""LLM-backed inference for application questions.

Two jobs:

1. ``resolve_batch`` - given a batch of questions the profile rules could not
   answer, decide for each one whether to ANSWER (explicit fact or reasonable,
   direct inference from the resume/profile), DRAFT (a personalized answer to a
   subjective question that the user must approve) or ASK_USER (unknown,
   subjective, or risky to guess).
2. ``research_company`` - build a short brief about the company/role (web search
   when enabled, plus the job page text) used to ground DRAFT answers.

The model is given the resume text, the profile facts and the job context as
the *only* source of truth and is told never to invent employers, titles,
dates, degrees, GPA, projects, technologies, awards, demographics, motivations,
or how the candidate discovered the job.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("autoapply.llm")

CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}

BATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "decision": {"type": "string", "enum": ["answer", "draft", "ask_user"]},
                    "answer": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "decision", "answer", "confidence", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You help a candidate fill internship application forms. You receive the candidate's resume, a set of authoritative profile facts, context about the job, and a batch of form questions. For EACH question return exactly one decision:

- "answer": use this when the answer is an explicit fact in the resume/profile, OR when it can be reasonably and directly inferred from them. Examples of acceptable inference: "Have you completed at least one internship?" -> Yes (resume lists internships); "Years of relevant experience?" -> count from the resume's dated experience; "Do you have experience with Python?" -> Yes (resume skills); "Expected graduation" -> from the profile; "Are you currently enrolled?" -> Yes (expected graduation is in the future); "Which technical domains interest you most?" -> choose the option(s) that best match the technologies and projects that recur in the resume, and say so in reason.
- "draft": use this for subjective / motivational questions (why this company, why this role, what excites you, proudest accomplishment, tell us about a time...). Write a specific, first-person draft grounded ONLY in the resume's real projects/experience and the job/company context provided. It must be specific enough that swapping the company name for another would not work. Reflect genuine technical overlap between the candidate's work and the company's products/engineering problems. You may state personality-level observations that the resume's pattern of work supports (e.g. a tendency to care about scalability and measurement), but never invent events, employers, products used, or feelings about the company you have no basis for. The user will review every draft before it is used.
- "ask_user": use this when the answer is unknown, is a personal preference/opinion/story with no grounding, requires private facts (accounts, usernames, conferences attended, references, salary), or when guessing could produce a false or harmful legal/authorization statement.

Hard rules:
1. Never invent employers, job titles, dates, degrees, GPA, projects, technologies, certifications, awards, leadership roles, demographic information, personal motivations, or how the candidate discovered the job.
2. "How did you hear about us / this job?" and anything about job discovery or referrals -> ask_user, always.
3. Work authorization, visa, sponsorship, OPT/CPT, clearance, citizenship: answer only when the profile facts state it explicitly for that exact question; otherwise ask_user. Never infer authorization for a country other than the one the profile states.
4. Demographics: answer only from the profile facts; never infer from the name or anything else.
5. When options are given, "answer" must be EXACTLY one option string (character for character), or for multi-select questions several option strings joined with " || ". If no option is clearly right, ask_user.
6. Keep answers short and plain unless the question asks for a paragraph. Respect any max length given.
7. confidence: "high" = explicit fact; "medium" = reasonable direct inference; "low" = weak inference (the caller may reject it).
8. reason: one short sentence citing the resume/profile evidence or explaining why the user is needed.
Return JSON only."""

RESEARCH_PROMPT = """Research the company and role below and write a concise brief (250-400 words) for a software engineering intern applicant. Go beyond surface facts. Cover: the product(s) and who uses them; how the company approaches its core problem; engineering culture and practices (experimentation, data, AI/ML, scale, open source, developer tooling) as far as public sources show; the technical challenges of building the product at their scale; what makes the product technically interesting; and what the specific role appears to involve (from the job text). Cite only what sources support; if something is unknown, say so. Plain text, no marketing language."""


@dataclass
class LLMDecision:
    id: str
    decision: str  # answer | draft | ask_user
    answer: str = ""
    confidence: str = "low"
    reason: str = ""

    def accepted(self, min_confidence: str) -> bool:
        return (
            self.decision == "answer"
            and bool(self.answer.strip())
            and CONFIDENCE_ORDER.get(self.confidence, 0) >= CONFIDENCE_ORDER.get(min_confidence, 1)
        )


@dataclass
class BatchQuestion:
    id: str
    label: str
    options: list[str] | None = None
    kind: str = "text"
    required: bool = False
    maxlength: int | None = None
    policy: str = "infer"  # infer | strict | draft
    category: str = "other"
    multi: bool = False


@dataclass
class LLMAnswer:  # kept for backwards compatibility with single-question callers
    can_answer: bool
    answer: str
    confidence: str
    reason: str = ""

    def acceptable(self, min_confidence: str) -> bool:
        return self.can_answer and bool(self.answer.strip()) and CONFIDENCE_ORDER.get(self.confidence, 0) >= CONFIDENCE_ORDER.get(min_confidence, 1)


@dataclass
class JobContext:
    company: str = ""
    role: str = ""
    url: str = ""
    job_text: str = ""
    research: str = ""

    def as_text(self, max_job_chars: int = 8000) -> str:
        parts = [f"Company: {self.company}", f"Role: {self.role}"]
        if self.url:
            parts.append(f"Job URL: {self.url}")
        if self.research:
            parts.append("<company_research>\n" + self.research.strip() + "\n</company_research>")
        if self.job_text:
            parts.append("<job_posting>\n" + self.job_text[:max_job_chars].strip() + "\n</job_posting>")
        return "\n".join(parts)


class QuestionAnswerer:
    """Thin wrapper around the Anthropic Messages API."""

    def __init__(
        self,
        api_key: str,
        model: str,
        resume_text: str,
        profile_summary: str,
        min_confidence: str = "medium",
        max_resume_chars: int = 12000,
        web_research: bool = True,
        research_cache_dir: Path | None = None,
        max_job_text_chars: int = 8000,
    ):
        self.api_key = api_key
        self.model = model
        self.resume_text = resume_text[:max_resume_chars]
        self.profile_summary = profile_summary
        self.min_confidence = min_confidence
        self.web_research = web_research
        self.research_cache_dir = research_cache_dir
        self.max_job_text_chars = max_job_text_chars
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def _context_block(self) -> str:
        return (
            "<resume>\n" + self.resume_text + "\n</resume>\n\n"
            "<profile_facts>\n" + self.profile_summary + "\n</profile_facts>"
        )

    # ------------------------------------------------------------------ #
    # Batch resolution
    # ------------------------------------------------------------------ #

    def resolve_batch(self, questions: list[BatchQuestion], job: JobContext | None = None) -> dict[str, LLMDecision]:
        """Decide every question in one request. Never raises; failures -> ask_user."""
        if not questions:
            return {}
        if not self.enabled:
            return {q.id: LLMDecision(q.id, "ask_user", reason="No ANTHROPIC_API_KEY configured") for q in questions}

        parts = [self._context_block(), ""]
        if job:
            parts.append(job.as_text(self.max_job_text_chars))
            parts.append("")
        parts.append("Questions (answer every id):")
        for q in questions:
            line = f"- id={q.id} | policy={q.policy} | category={q.category} | required={'yes' if q.required else 'no'} | type={q.kind}"
            if q.maxlength:
                line += f" | max_length={q.maxlength}"
            if q.multi:
                line += " | multi-select (join options with ' || ')"
            parts.append(line)
            parts.append(f"  question: {q.label.strip()}")
            if q.options:
                parts.append("  options: " + " | ".join(o.replace("|", "/") for o in q.options))
        parts.append("")
        parts.append("policy meanings: infer = explicit facts or reasonable direct inference allowed; "
                     "strict = explicit facts only, else ask_user; draft = write a personalized draft for user approval.")
        user_msg = "\n".join(parts)

        try:
            data = self._call_json(user_msg, BATCH_SCHEMA, max_tokens=8000)
        except Exception as e:  # noqa: BLE001 - any API failure means "ask the user"
            log.warning("LLM batch call failed: %s", e)
            return {q.id: LLMDecision(q.id, "ask_user", reason=f"LLM error: {e}") for q in questions}

        by_id = {q.id: q for q in questions}
        out: dict[str, LLMDecision] = {}
        for r in data.get("results", []):
            qid = str(r.get("id", ""))
            if qid not in by_id:
                continue
            d = LLMDecision(
                id=qid,
                decision=str(r.get("decision", "ask_user")),
                answer=str(r.get("answer", "") or "").strip(),
                confidence=str(r.get("confidence", "low")),
                reason=str(r.get("reason", "") or ""),
            )
            q = by_id[qid]
            if d.decision == "answer" and q.options:
                d = self._validate_options(d, q)
            if d.decision == "answer" and not d.accepted(self.min_confidence):
                d.decision = "ask_user"
                d.reason = f"low confidence: {d.reason}"
            if d.decision == "draft" and not d.answer:
                d.decision = "ask_user"
            out[qid] = d
        for q in questions:
            out.setdefault(q.id, LLMDecision(q.id, "ask_user", reason="no decision returned"))
        log.info("LLM batch: %s", {k: v.decision for k, v in out.items()})
        return out

    @staticmethod
    def _validate_options(d: LLMDecision, q: BatchQuestion) -> LLMDecision:
        assert q.options
        norm = {o.strip().lower(): o for o in q.options}
        parts = [p.strip() for p in d.answer.split("||")] if q.multi else [d.answer.strip()]
        fixed: list[str] = []
        for p in parts:
            if p in q.options:
                fixed.append(p)
            elif p.lower() in norm:
                fixed.append(norm[p.lower()])
            else:
                d.decision = "ask_user"
                d.reason = f"answer {p!r} is not one of the options"
                d.answer = ""
                return d
        d.answer = " || ".join(fixed)
        return d

    # ------------------------------------------------------------------ #
    # Company research
    # ------------------------------------------------------------------ #

    def research_company(self, company: str, role: str, job_text: str = "", url: str = "") -> str:
        """Return a research brief for the company/role (cached per company+role)."""
        if not self.enabled or not company:
            return ""
        cache = None
        if self.research_cache_dir:
            slug = re.sub(r"[^a-z0-9]+", "-", f"{company}-{role}".lower()).strip("-")[:80]
            cache = Path(self.research_cache_dir) / f"{slug}.md"
            if cache.exists():
                return cache.read_text(encoding="utf-8")
        msg = f"Company: {company}\nRole: {role}\n" + (f"Job URL: {url}\n" if url else "")
        if job_text:
            msg += "\n<job_posting>\n" + job_text[: self.max_job_text_chars] + "\n</job_posting>\n"
        try:
            text = self._call_text(RESEARCH_PROMPT, msg, web_search=self.web_research)
        except Exception as e:  # noqa: BLE001
            log.warning("Company research failed: %s", e)
            return ""
        if cache and text:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(text, encoding="utf-8")
        return text

    # ------------------------------------------------------------------ #
    # Single question (compatibility)
    # ------------------------------------------------------------------ #

    def answer(self, question: str, options: list[str] | None = None, company: str = "", role: str = "", max_length: int | None = None) -> LLMAnswer:
        q = BatchQuestion(id="q", label=question, options=options, maxlength=max_length)
        d = self.resolve_batch([q], JobContext(company=company, role=role)).get("q")
        if d is None:
            return LLMAnswer(False, "", "low", "no result")
        return LLMAnswer(d.decision == "answer", d.answer, d.confidence, d.reason)

    # ------------------------------------------------------------------ #
    # API calls
    # ------------------------------------------------------------------ #

    def _call_json(self, user_msg: str, schema: dict[str, Any], max_tokens: int = 4096) -> dict[str, Any]:
        import anthropic

        client = self._get_client()
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        try:
            # Server-side refusal fallback: if a safety classifier declines the
            # request, the API re-runs it on Anthropic's recommended fallback model.
            response = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        except anthropic.BadRequestError:
            response = client.messages.create(**kwargs)
        if response.stop_reason == "refusal":
            return {"results": []}
        text = next((b.text for b in response.content if b.type == "text"), "")
        return json.loads(text)

    def _call_text(self, system: str, user_msg: str, web_search: bool = False) -> str:
        client = self._get_client()
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        if web_search:
            kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]
        response = client.messages.create(**kwargs)
        if response.stop_reason == "refusal":
            return ""
        return "\n".join(b.text for b in response.content if b.type == "text").strip()
