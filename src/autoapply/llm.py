"""LLM-backed answering of free-text / unexpected application questions.

The model is given the resume text and profile as the *only* source of truth
and must refuse (``can_answer=false``) whenever an honest answer is not
supported by that material. Low-confidence answers are rejected by the caller.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("autoapply.llm")

CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "can_answer": {"type": "boolean"},
        "answer": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "reason": {"type": "string"},
    },
    "required": ["can_answer", "answer", "confidence", "reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You fill in internship application forms on behalf of a candidate.

You are given the candidate's resume text and profile. These are the ONLY facts you may use.

Rules:
1. Never invent, embellish, or infer experience, skills, employers, dates, numbers, locations, legal status, or personal facts that are not explicitly present in the resume or profile.
2. If a truthful answer cannot be written entirely from the provided material, set can_answer to false and explain why in reason. Leave answer empty in that case.
3. When options are provided, answer must be EXACTLY one of the option strings, character for character. If none of the options is clearly correct from the material, set can_answer to false.
4. Prefer short, plain, professional answers written in first person. For "why this company/role" style questions, only reference the candidate's real projects, coursework, and experience from the resume, and the job title/company given. Do not claim to have used the company's products or to know company details you were not given.
5. Questions about compensation, availability of a security clearance, legal matters, references, or anything about the candidate's private life that is not in the profile: can_answer = false.
6. confidence is "high" only when every fact in the answer is directly supported by the material.
Respond with JSON only."""


@dataclass
class LLMAnswer:
    can_answer: bool
    answer: str
    confidence: str
    reason: str = ""

    def acceptable(self, min_confidence: str) -> bool:
        return (
            self.can_answer
            and bool(self.answer.strip())
            and CONFIDENCE_ORDER.get(self.confidence, 0) >= CONFIDENCE_ORDER.get(min_confidence, 2)
        )


class QuestionAnswerer:
    """Thin wrapper around the Anthropic Messages API."""

    def __init__(
        self,
        api_key: str,
        model: str,
        resume_text: str,
        profile_summary: str,
        min_confidence: str = "high",
        max_resume_chars: int = 12000,
    ):
        self.api_key = api_key
        self.model = model
        self.resume_text = resume_text[:max_resume_chars]
        self.profile_summary = profile_summary
        self.min_confidence = min_confidence
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
            "<profile>\n" + self.profile_summary + "\n</profile>"
        )

    def answer(
        self,
        question: str,
        options: list[str] | None = None,
        company: str = "",
        role: str = "",
        max_length: int | None = None,
    ) -> LLMAnswer:
        """Ask the model. Returns an LLMAnswer; never raises on refusal."""
        if not self.enabled:
            return LLMAnswer(False, "", "low", "No ANTHROPIC_API_KEY configured")

        user_parts = [self._context_block(), ""]
        if company or role:
            user_parts.append(f"Application: {role} at {company}")
        user_parts.append(f"Question: {question.strip()}")
        if options:
            user_parts.append("Options (answer must match one exactly):")
            user_parts.extend(f"- {o}" for o in options)
        if max_length:
            user_parts.append(f"Maximum answer length: {max_length} characters.")
        user_msg = "\n".join(user_parts)

        try:
            data = self._call(user_msg)
        except Exception as e:  # noqa: BLE001 - any API failure means "don't guess"
            log.warning("LLM call failed: %s", e)
            return LLMAnswer(False, "", "low", f"LLM error: {e}")

        ans = LLMAnswer(
            can_answer=bool(data.get("can_answer")),
            answer=str(data.get("answer", "") or "").strip(),
            confidence=str(data.get("confidence", "low")),
            reason=str(data.get("reason", "") or ""),
        )
        if options and ans.can_answer and ans.answer not in options:
            # Tolerate case/whitespace differences, otherwise reject.
            norm = {o.strip().lower(): o for o in options}
            if ans.answer.strip().lower() in norm:
                ans.answer = norm[ans.answer.strip().lower()]
            else:
                ans.can_answer = False
                ans.reason = f"Answer {ans.answer!r} is not one of the options"
        log.info("LLM answer for %r: can_answer=%s confidence=%s", question[:80], ans.can_answer, ans.confidence)
        return ans

    def _call(self, user_msg: str) -> dict[str, Any]:
        import anthropic

        client = self._get_client()
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            output_config={"format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
        )
        try:
            # Server-side refusal fallback: if the safety classifier declines the
            # request, the API re-runs it on Anthropic's recommended fallback model.
            response = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        except anthropic.BadRequestError:
            # Older SDK / platform without fallback support: plain call.
            response = client.messages.create(**kwargs)

        if response.stop_reason == "refusal":
            return {"can_answer": False, "answer": "", "confidence": "low", "reason": "model refused"}
        text = next((b.text for b in response.content if b.type == "text"), "")
        return json.loads(text)
