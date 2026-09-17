"""Dictation-style voice input: fast transcript clean-up with a no-invention guard.

Pipeline (see docs/ARCHITECTURE.md §7):

  raw transcript
    -> deterministic vocabulary correction (resume/profile/job terms, tech lexicon)
    -> optional fast LLM pass (punctuation, filler words, obvious homophones)
    -> guard: the result may only *edit* the transcript; if it adds content the
       raw text is kept and the answer is flagged low-confidence
    -> uncertain spans with suggestions for the UI

The LLM pass is optional and uses a fast model; nothing here ever generates an
answer, only cleans what the user said.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("autoapply.dictation")

# Common technical vocabulary that speech recognisers mangle.
TECH_LEXICON = [
    "Lambda", "DynamoDB", "PostgreSQL", "Postgres", "MySQL", "SQLite", "Redis", "Kafka", "Kubernetes", "Docker", "Terraform",
    "GraphQL", "gRPC", "REST", "WebSocket", "OAuth", "JWT", "PyTorch", "TensorFlow", "JAX", "NumPy", "pandas", "scikit-learn",
    "HuggingFace", "LangChain", "FastAPI", "Django", "Flask", "React", "Next.js", "Node.js", "TypeScript", "JavaScript", "Python",
    "C++", "Rust", "Golang", "Go", "Java", "Kotlin", "Swift", "Redux", "Tailwind", "Firebase", "Firestore", "Supabase", "AWS",
    "EC2", "S3", "GCP", "Azure", "Lambda", "SageMaker", "BigQuery", "Snowflake", "Spark", "Hadoop", "Airflow", "dbt",
    "data lake", "data warehouse", "data pipeline", "ETL", "CI/CD", "GitHub", "GitLab", "Linux", "Bash", "microservices",
    "Pydantic", "HDF5", "Drake", "reinforcement learning", "inverse kinematics", "embeddings", "LLM", "RAG", "transformer",
    "API", "SDK", "CLI", "SQL", "NoSQL", "MongoDB", "Elasticsearch", "OpenAI", "Gemini", "Claude", "Anthropic", "Playwright",
    "Selenium", "Streamlit", "Jupyter", "Matplotlib", "OpenCV", "ROS", "CUDA", "GPU", "Vercel", "Netlify", "Heroku",
]

# Frequent mishearings that fuzzy matching alone does not catch.
HOMOPHONES = {
    "data lack": "data lake", "data lakes": "data lakes", "lamba": "Lambda", "lambda": "Lambda", "dynamo db": "DynamoDB",
    "dynamodb": "DynamoDB", "dinah mo db": "DynamoDB", "post grass": "Postgres", "post gres": "Postgres", "my sequel": "MySQL",
    "sequel": "SQL", "no sequel": "NoSQL", "pie torch": "PyTorch", "pi torch": "PyTorch", "tensor flow": "TensorFlow",
    "react js": "React", "type script": "TypeScript", "java script": "JavaScript", "see plus plus": "C++", "c plus plus": "C++",
    "kuber netties": "Kubernetes", "cooper netties": "Kubernetes", "docker": "Docker", "get hub": "GitHub", "git hub": "GitHub",
    "fast api": "FastAPI", "graph ql": "GraphQL", "g rpc": "gRPC", "rest api": "REST API", "web socket": "WebSocket",
    "hugging face": "HuggingFace", "numb pie": "NumPy", "num pie": "NumPy", "pandas": "pandas", "air flow": "Airflow",
    "a w s": "AWS", "s three": "S3", "e c two": "EC2", "big query": "BigQuery", "snow flake": "Snowflake", "ci cd": "CI/CD",
    "l l m": "LLM", "rag": "RAG", "pi dantic": "Pydantic", "pydantic": "Pydantic", "hd f five": "HDF5", "h d f five": "HDF5",
    "fish eye": "FishEye", "u mass": "UMass", "you mass": "UMass",
}

FILLERS = re.compile(r"\b(um+|uh+|erm+|hmm+|you know,?|like,|sort of,|kind of,|basically,)\s*", re.I)


@dataclass
class Span:
    start: int
    end: int
    text: str
    suggestion: str = ""
    reason: str = ""


@dataclass
class CleanResult:
    raw: str
    cleaned: str
    confidence: str  # high | medium | low
    spans: list[Span] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    ai_modified: bool = False
    guard_rejected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "cleaned": self.cleaned,
            "confidence": self.confidence,
            "spans": [s.__dict__ for s in self.spans],
            "changes": self.changes,
            "ai_modified": self.ai_modified,
            "guard_rejected": self.guard_rejected,
        }


@dataclass
class DictationContext:
    company: str = ""
    role: str = ""
    question: str = ""
    job_text: str = ""
    resume_text: str = ""
    profile_summary: str = ""
    previous_answers: list[str] = field(default_factory=list)
    extra_vocabulary: list[str] = field(default_factory=list)


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]*")


def _tech_like(w: str) -> bool:
    """Words whose casing carries meaning (PyTorch, HDF5, AWS, C++), unlike sentence-case English."""
    return any(c.isupper() for c in w[1:]) or any(c.isdigit() for c in w) or "+" in w or "#" in w or "." in w or (w.isupper() and len(w) >= 2)


def vocabulary_from_text(*texts: str) -> list[str]:
    """Capitalised / technical tokens from the resume, job posting and profile.

    A capitalised word that also appears in lowercase somewhere in the same
    material ("Data" / "data") is sentence-case English, not vocabulary.
    """
    vocab: set[str] = set()
    lowercase_seen: set[str] = set()
    for t in texts:
        for w in _WORD.findall(t or ""):
            if w[0].islower():
                lowercase_seen.add(w.lower())
    for t in texts:
        words = _WORD.findall(t or "")
        for w in words:
            if len(w) < 3:
                continue
            w = w.strip(".,")
            if _tech_like(w):
                vocab.add(w)
            elif w[0].isupper() and w.lower() not in lowercase_seen and w.lower() not in _COMMON:
                vocab.add(w)
        # Proper-noun phrases: runs of consecutive capitalised words ("FishEye Software", "Necessary Behavior").
        run: list[str] = []
        for w in words + [""]:
            ww = w.strip(".,")
            if ww and ww[0].isupper() and len(ww) >= 2:
                run.append(ww)
            else:
                for n in (2, 3):
                    for i in range(0, max(0, len(run) - n + 1)):
                        phrase = run[i:i + n]
                        if not all(r.lower() in _COMMON for r in phrase) and phrase[-1].lower() not in ("inc", "llc", "ltd"):
                            vocab.add(" ".join(phrase))
                run = []
    return sorted(vocab)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


# --------------------------------------------------------------------------- #
# Deterministic pass
# --------------------------------------------------------------------------- #


def correct_vocabulary(raw: str, vocabulary: list[str]) -> tuple[str, list[Span], list[str]]:
    """Fix known phrases and fuzzy-match unknown words to the vocabulary."""
    text = raw
    changes: list[str] = []
    spans: list[Span] = []

    # 1. Multi-word homophones / known mishearings (longest first).
    for wrong in sorted(HOMOPHONES, key=len, reverse=True):
        pat = re.compile(r"\b" + re.escape(wrong) + r"\b", re.I)
        if pat.search(text):
            right = HOMOPHONES[wrong]
            if right.lower() != wrong.lower():
                text = pat.sub(right, text)
                changes.append(f"{wrong} -> {right}")

    # 2. Vocabulary: lowercase multi-word entries ("data lake") and single tokens.
    vocab = list(dict.fromkeys(vocabulary + TECH_LEXICON))
    lower_map = {v.lower(): v for v in vocab}
    for v in sorted(vocab, key=len, reverse=True):
        if " " in v:
            pat = re.compile(r"\b" + re.escape(v) + r"\b", re.I)
            if pat.search(text) and not re.search(r"\b" + re.escape(v) + r"\b", text):
                text = pat.sub(v, text)
                changes.append(f"casing -> {v}")

    # 3. Fuzzy single-word repair (only for words not already valid English-looking words in the vocab).
    out_words: list[str] = []
    pos = 0
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9+#'-]*", text):
        w = m.group(0)
        lw = w.lower()
        if lw in lower_map:
            fixed = lower_map[lw]
            if fixed != w and fixed.lower() == lw and (_tech_like(fixed) or fixed in TECH_LEXICON):
                text = text[: m.start()] + fixed + text[m.end():]
                changes.append(f"casing -> {fixed}")
            continue
        if len(w) >= 4:
            cands = difflib.get_close_matches(lw, [k for k in lower_map if " " not in k and len(k) >= 5], n=1, cutoff=0.86)
            if cands and cands[0] != lw:
                fixed = lower_map[cands[0]]
                # Only auto-correct when the original is not a common word (heuristic: not in a small stoplist).
                if lw not in _COMMON:
                    spans.append(Span(m.start(), m.start() + len(fixed), fixed, "", f"corrected from '{w}'"))
                    text = text[: m.start()] + fixed + text[m.end():]
                    changes.append(f"{w} -> {fixed}")
        out_words.append(w)
        pos = m.end()
    return text, spans, changes


_COMMON = set("""the and for with that this from have were been they their there where what when which while about after before
because between during under over into onto than then them these those through would could should might must also just
very really where here more most some such only other same each both many much like make made makes making work worked
working build built building team teams project projects system systems data model models learn learned learning
using used use used code coding design designed designing test tests testing lead led leads year years time first
last next want wanted interested interest excited role company product products people user users problem problems
experience skills language languages""".split())


def find_uncertain(raw: str, cleaned: str, vocabulary: list[str]) -> list[Span]:
    """Words that look like near-misses of vocabulary terms but were not corrected."""
    spans: list[Span] = []
    vocab_lower = {v.lower(): v for v in dict.fromkeys(vocabulary + TECH_LEXICON) if " " not in v and len(v) >= 4}
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9+#'-]*", cleaned):
        w = m.group(0)
        lw = w.lower()
        if lw in vocab_lower or lw in _COMMON or len(w) < 4:
            continue
        cands = difflib.get_close_matches(lw, list(vocab_lower), n=1, cutoff=0.72)
        if cands and cands[0] != lw and difflib.SequenceMatcher(None, lw, cands[0]).ratio() < 0.86:
            spans.append(Span(m.start(), m.end(), w, vocab_lower[cands[0]], f"did you mean {vocab_lower[cands[0]]}?"))
    return spans


def tidy(text: str) -> tuple[str, list[str]]:
    changes: list[str] = []
    t = FILLERS.sub("", text)
    if t != text:
        changes.append("removed filler words")
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    if t and t[-1] not in ".!?":
        t += "."
        changes.append("punctuation")
    return t, changes


# --------------------------------------------------------------------------- #
# Guard
# --------------------------------------------------------------------------- #


def guard_no_invention(raw: str, cleaned: str, max_new_ratio: float = 0.15, max_new_words: int = 8) -> tuple[bool, list[str]]:
    """True if ``cleaned`` only edits ``raw``. Rejects results that add substantive content."""
    rt, ct = _tokens(raw), _tokens(cleaned)
    if not rt:
        return not ct, []
    raw_set = set(rt)
    # A new token is fine if it is a close spelling of a raw token (a correction), otherwise it's an addition.
    added = []
    for w in ct:
        if w in raw_set or w in _COMMON_GLUE:
            continue
        if difflib.get_close_matches(w, list(raw_set), n=1, cutoff=0.7):
            continue
        # multi-token expansions like "c++" -> "c", "++" are fine
        if len(w) <= 2:
            continue
        added.append(w)
    if len(ct) > len(rt) * 1.35 + 3:
        return False, added or ["length"]
    if len(added) > max_new_words or len(added) > max_new_ratio * max(len(rt), 1):
        return False, added
    return True, added


_COMMON_GLUE = {"a", "an", "the", "and", "to", "of", "in", "i", "i'm", "is", "am", "at", "on", "it", "that", "which", "with", "for"}


# --------------------------------------------------------------------------- #
# Optional LLM pass
# --------------------------------------------------------------------------- #

LLM_SYSTEM = """You clean up a dictated answer to a job-application question. You are an editor, not an author.
Allowed: fix punctuation and capitalisation, remove filler words (um, uh, like), fix obvious speech-recognition errors and homophones using the vocabulary and context provided, fix grammar only when the intent is unambiguous.
Forbidden: adding any information, examples, skills, technologies, employers, numbers, or claims the speaker did not say; changing the meaning; making it more impressive; rephrasing beyond what is needed. Keep first person and the speaker's wording.
If a word is unclear, keep the speaker's word and list it in uncertain with your best guess.
Return JSON: {"cleaned": string, "uncertain": [{"text": string, "suggestion": string}], "confidence": "high"|"medium"|"low"}"""

LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "cleaned": {"type": "string"},
        "uncertain": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}, "suggestion": {"type": "string"}}, "required": ["text", "suggestion"], "additionalProperties": False}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["cleaned", "uncertain", "confidence"],
    "additionalProperties": False,
}


class TranscriptCleaner:
    def __init__(self, api_key: str = "", model: str = "claude-haiku-4-5", use_llm: bool = True):
        self.api_key = api_key
        self.model = model
        self.use_llm = use_llm and bool(api_key)
        self._client = None

    def _llm(self, raw: str, ctx: DictationContext, vocabulary: list[str]) -> dict[str, Any] | None:
        try:
            import anthropic

            if self._client is None:
                self._client = anthropic.Anthropic(api_key=self.api_key)
            user = (
                f"Company: {ctx.company}\nRole: {ctx.role}\nQuestion: {ctx.question}\n"
                f"Vocabulary (names/terms the speaker is likely to use): {', '.join(vocabulary[:200])}\n\n"
                f"Dictated answer:\n{raw}"
            )
            resp = self._client.messages.create(
                model=self.model, max_tokens=1500, system=LLM_SYSTEM,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": LLM_SCHEMA}},
            )
            if resp.stop_reason == "refusal":
                return None
            text = next((b.text for b in resp.content if b.type == "text"), "")
            return json.loads(text)
        except Exception as e:  # noqa: BLE001
            log.warning("transcript LLM clean-up failed: %s", e)
            return None

    def clean(self, raw: str, ctx: DictationContext | None = None, llm_result: dict[str, Any] | None = None) -> CleanResult:
        """Clean a transcript. ``llm_result`` may be injected (tests) to skip the API."""
        ctx = ctx or DictationContext()
        raw = (raw or "").strip()
        if not raw:
            return CleanResult(raw, "", "low")
        vocabulary = vocabulary_from_text(ctx.resume_text, ctx.job_text, ctx.profile_summary, ctx.company, ctx.role) + list(ctx.extra_vocabulary)
        if ctx.company:
            vocabulary.append(ctx.company)

        text, spans, changes = correct_vocabulary(raw, vocabulary)
        text, tidy_changes = tidy(text)
        changes += tidy_changes
        ai_modified = False
        confidence = "high"

        result = llm_result
        if result is None and self.use_llm:
            result = self._llm(text, ctx, vocabulary)
        if result and result.get("cleaned"):
            candidate = str(result["cleaned"]).strip()
            ok, added = guard_no_invention(raw, candidate)
            if ok:
                if candidate != text:
                    ai_modified = True
                    changes.append("llm clean-up")
                text = candidate
                confidence = str(result.get("confidence", "high"))
                for u in result.get("uncertain", []) or []:
                    t = str(u.get("text", "")).strip()
                    if not t:
                        continue
                    idx = text.lower().find(t.lower())
                    if idx >= 0:
                        spans.append(Span(idx, idx + len(t), text[idx: idx + len(t)], str(u.get("suggestion", "")), "unclear"))
            else:
                log.warning("LLM clean-up rejected by guard (added: %s)", added)
                changes.append("llm result rejected: added content")
                return CleanResult(raw, text, "low", spans + find_uncertain(raw, text, vocabulary), changes, False, True)

        uncertain = find_uncertain(raw, text, vocabulary)
        seen = {(s.start, s.end) for s in spans}
        spans += [u for u in uncertain if (u.start, u.end) not in seen]
        if any(s.suggestion for s in spans):
            confidence = "medium" if confidence == "high" else confidence
        if len(_tokens(raw)) < 3:
            confidence = "medium" if confidence == "high" else confidence
        return CleanResult(raw, text, confidence, spans, changes, ai_modified)
