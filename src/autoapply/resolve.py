"""Conversational resolution of pending questions (voice or typed).

The resolver walks every ``PendingQuestion`` the planner could not answer,
reads it aloud (when voice is available), records and transcribes the user's
answer, shows the transcription, and only writes it into the form after the
user accepts it. The answer is applied through ``apply_fn(field_id, value)``,
i.e. always by the field's id, so an answer can never land in another field.

The resolver never submits anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Protocol

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from .ats.base import PendingQuestion
from .fields import choose_option

log = logging.getLogger("autoapply.resolve")

ApplyFn = Callable[[str, str], tuple[bool, str]]


class Interaction(Protocol):
    """Terminal prompts, injectable for tests."""

    def choose(self, prompt: str, choices: list[str], default: str) -> str: ...
    def text(self, prompt: str, default: str = "") -> str: ...


class RichInteraction:
    def choose(self, prompt: str, choices: list[str], default: str) -> str:
        return Prompt.ask(prompt, choices=choices, default=default)

    def text(self, prompt: str, default: str = "") -> str:
        return Prompt.ask(prompt, default=default)


@dataclass
class Resolution:
    field_id: str
    label: str
    action: str  # accepted | skipped | failed
    value: str = ""
    source: str = ""  # voice | typed | draft


class QuestionResolver:
    def __init__(self, voice=None, interaction: Interaction | None = None, console: Console | None = None, max_retries: int = 3):
        self.voice = voice
        self.io: Interaction = interaction or RichInteraction()
        self.console = console or Console()
        self.max_retries = max_retries

    # ------------------------------------------------------------------ #

    @property
    def can_listen(self) -> bool:
        return bool(self.voice) and getattr(self.voice, "can_listen", False)

    def _say(self, text: str) -> None:
        if self.voice:
            try:
                self.voice.say(text)
            except Exception as e:  # noqa: BLE001
                log.warning("TTS failed: %s", e)

    def resolve(self, pending: list[PendingQuestion], apply_fn: ApplyFn) -> list[Resolution]:
        """Walk every pending question. Returns one Resolution per question."""
        if not pending:
            return []
        # Required questions first, then optional ones.
        ordered = sorted(pending, key=lambda p: (not p.required))  # stable: keeps form order within each group
        results: list[Resolution] = []
        total = len(ordered)
        self.console.print(f"[bold]{total} question(s) need you.[/] Say or type your answer; nothing is submitted until you confirm later.")
        for i, p in enumerate(ordered, start=1):
            results.append(self._resolve_one(p, i, total, apply_fn))
        done = sum(1 for r in results if r.action == "accepted")
        self.console.print(f"[bold]Resolved {done}/{total}.[/] " + ("" if done == total else "Skipped questions stay blank."))
        return results

    # ------------------------------------------------------------------ #

    def _resolve_one(self, p: PendingQuestion, i: int, total: int, apply_fn: ApplyFn) -> Resolution:
        tag = "required" if p.required else "optional"
        header = f"[{i}/{total}] {p.label}"
        body = f"[dim]{p.category} · {tag} · {p.reason}[/]"
        if p.options:
            body += "\n" + "\n".join(f"  {n}. {o}" for n, o in enumerate(p.options, start=1))
        self.console.print(Panel(body, title=header, border_style="yellow"))

        if p.draft:
            return self._resolve_draft(p, apply_fn)

        spoken = f"Question {i} of {total}. {p.label}"
        if p.options:
            spoken += ". The options are: " + "; ".join(p.options[:12])
        self._say(spoken)

        if self.can_listen:
            return self._voice_loop(p, apply_fn)
        return self._typed_loop(p, apply_fn)

    # -- drafts ----------------------------------------------------------- #

    def _resolve_draft(self, p: PendingQuestion, apply_fn: ApplyFn) -> Resolution:
        self.console.print(Panel(p.draft, title="Draft (needs your approval)", border_style="magenta"))
        self._say(f"I drafted an answer for: {p.label}. Please review it on screen.")
        choices = ["a", "e", "s"] + (["v"] if self.can_listen else []) + ["t"]
        while True:
            choice = self.io.choose("[a]ccept draft / [e]dit / [t]ype your own" + (" / [v]oice your own" if self.can_listen else "") + " / [s]kip", choices, "a")
            if choice == "a":
                return self._apply(p, p.draft, "draft", apply_fn)
            if choice == "e":
                edited = self.io.text("Edit the answer", default=p.draft)
                if edited.strip():
                    return self._apply(p, edited.strip(), "typed", apply_fn)
            if choice == "t":
                return self._typed_loop(p, apply_fn)
            if choice == "v":
                return self._voice_loop(p, apply_fn)
            if choice == "s":
                self._clear(p, apply_fn)
                return Resolution(p.field_id, p.label, "skipped")

    # -- voice ------------------------------------------------------------ #

    def _voice_loop(self, p: PendingQuestion, apply_fn: ApplyFn) -> Resolution:
        attempts = 0
        while True:
            attempts += 1
            self.console.print("[bold red]● Listening…[/] speak now (stops after a pause)")
            try:
                transcript = self.voice.listen()
            except Exception as e:  # noqa: BLE001
                self.console.print(f"[red]Recording failed: {e}[/]")
                transcript = None
            if not transcript:
                self.console.print("[yellow]I didn't catch anything.[/]")
                self._say("I didn't catch that.")
                choice = self.io.choose("[r]etry / [t]ype / [s]kip", ["r", "t", "s"], "r" if attempts < self.max_retries else "t")
                if choice == "r":
                    continue
                if choice == "t":
                    return self._typed_loop(p, apply_fn)
                return Resolution(p.field_id, p.label, "skipped")

            value = transcript.strip()
            if p.options:
                matched = self._match_option(p.options, value)
                self.console.print(f"Heard: [bold]{value}[/]" + (f"  → option: [bold]{matched}[/]" if matched else "  (no matching option)"))
                if not matched:
                    choice = self.io.choose("[n]umber of option / [r]etry / [t]ype / [s]kip", ["n", "r", "t", "s"], "r")
                    if choice == "n":
                        picked = self._pick_by_number(p.options)
                        if picked:
                            return self._apply(p, picked, "typed", apply_fn)
                        continue
                    if choice == "r":
                        continue
                    if choice == "t":
                        return self._typed_loop(p, apply_fn)
                    return Resolution(p.field_id, p.label, "skipped")
                value = matched
            else:
                self.console.print(Panel(value, title="Transcription", border_style="cyan"))

            choice = self.io.choose("[a]ccept / [r]etry / [e]dit / [t]ype / [s]kip", ["a", "r", "e", "t", "s"], "a")
            if choice == "a":
                return self._apply(p, value, "voice", apply_fn)
            if choice == "r":
                continue
            if choice == "e":
                edited = self.io.text("Edit the answer", default=value)
                if edited.strip():
                    return self._apply(p, edited.strip(), "typed", apply_fn)
                continue
            if choice == "t":
                return self._typed_loop(p, apply_fn)
            return Resolution(p.field_id, p.label, "skipped")

    # -- typed ------------------------------------------------------------ #

    def _typed_loop(self, p: PendingQuestion, apply_fn: ApplyFn) -> Resolution:
        while True:
            if p.options:
                raw = self.io.text("Type the option number or text (empty = skip)")
                if not raw.strip():
                    return Resolution(p.field_id, p.label, "skipped")
                value = self._pick_from_raw(p.options, raw)
                if not value:
                    self.console.print("[yellow]That doesn't match an option.[/]")
                    continue
            else:
                value = self.io.text("Type your answer (empty = skip)").strip()
                if not value:
                    return Resolution(p.field_id, p.label, "skipped")
            return self._apply(p, value, "typed", apply_fn)

    # -- helpers ----------------------------------------------------------- #

    def _apply(self, p: PendingQuestion, value: str, source: str, apply_fn: ApplyFn) -> Resolution:
        ok, msg = apply_fn(p.field_id, value)
        if ok:
            self.console.print(f"[green]✓ {p.label[:60]} ← {value[:80]}[/]")
            return Resolution(p.field_id, p.label, "accepted", value, source)
        self.console.print(f"[red]Could not put the answer into the form: {msg}[/]")
        choice = self.io.choose("[t]ry typing a different value / [s]kip (fix it in the browser)", ["t", "s"], "s")
        if choice == "t":
            return self._typed_loop(p, apply_fn)
        return Resolution(p.field_id, p.label, "failed", value, source)

    def _clear(self, p: PendingQuestion, apply_fn: ApplyFn) -> None:
        if p.kind in ("text", "textarea"):
            try:
                apply_fn(p.field_id, "")
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _match_option(options: list[str], spoken: str) -> str | None:
        s = spoken.strip().rstrip(".").lower()
        # "option three" / "number 2" / "3"
        import re

        words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
        m = re.search(r"\b(?:option|number)?\s*(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\b", s)
        if m and (s.startswith(("option", "number")) or s.isdigit() or s in words):
            n = int(m.group(1)) if m.group(1).isdigit() else words[m.group(1)]
            if 1 <= n <= len(options):
                return options[n - 1]
        return choose_option(options, s, "yesno") or choose_option(options, s, "select")

    def _pick_by_number(self, options: list[str]) -> str | None:
        raw = self.io.text("Option number")
        return self._pick_from_raw(options, raw)

    def _pick_from_raw(self, options: list[str], raw: str) -> str | None:
        raw = raw.strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        return self._match_option(options, raw)
