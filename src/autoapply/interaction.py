"""How the runner talks to a human.

The automation (``runner.py``) never prompts directly. It calls an
``Interaction`` whenever it needs a person: to answer pending questions, to
review before submitting, or to deal with a CAPTCHA. ``TerminalInteraction``
reproduces the CLI behaviour; ``web.orchestrator.WebInteraction`` blocks the
worker until the browser UI supplies the answer.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from rich.prompt import Prompt

from .ats.base import FieldAnswer, FillResult
from .report import console, print_answers
from .resolve import QuestionResolver

log = logging.getLogger("autoapply.interaction")


class SessionCancelled(Exception):
    """Raised inside the worker when the user cancels the session."""


class Interaction(Protocol):
    def resolve_pending(self, handler: Any, fill: FillResult, app_id: int) -> None: ...
    def review(self, fill: FillResult, app_id: int) -> str: ...  # submit | skip | manual
    def captcha(self, page: Any, stage: str, app_id: int) -> bool: ...  # True = solved, continue
    def checkpoint(self) -> None: ...  # pause / cancel point between steps
    def notice(self, message: str, app_id: int | None = None) -> None: ...


def apply_resolutions(fill: FillResult, results: list, source_override: str | None = None) -> None:
    """Fold resolver results back into the FillResult (shared by both interactions)."""
    by_id = {r.field_id: r for r in results}
    remaining = []
    for p in fill.pending:
        r = by_id.get(p.field_id)
        if r and r.action == "accepted":
            for a in fill.answers:
                if a.field_id == p.field_id:
                    a.value = r.value
                    a.source = source_override or ("draft" if r.source == "draft" else "user")
                    a.status = "filled"
                    a.reason = f"answered by you ({r.source})"
        else:
            remaining.append(p)
    fill.pending = remaining
    fill.unanswered = [p.label for p in remaining if p.required]


class TerminalInteraction:
    """The original CLI experience: typed answers (voice only with --voice)."""

    def __init__(self, voice_io=None, headless: bool = False):
        self.voice_io = voice_io
        self.headless = headless

    def notice(self, message: str, app_id: int | None = None) -> None:
        console.print(message)

    def checkpoint(self) -> None:
        return None

    def resolve_pending(self, handler: Any, fill: FillResult, app_id: int) -> None:
        resolver = QuestionResolver(voice=self.voice_io, console=console)
        results = resolver.resolve(fill.pending, handler.apply_answer)
        apply_resolutions(fill, results)

    def review(self, fill: FillResult, app_id: int) -> str:
        while True:
            print_answers(fill.answers, fill.pending)
            blocking = [p for p in fill.pending if p.required or p.draft]
            if blocking:
                console.print("[yellow]Some required questions or unapproved drafts remain. Choose [e] to finish them in the browser, or [n] to leave this job as needs input.[/]")
            choice = Prompt.ask("Submit this application? [y]es / [n]o (skip) / [e]dit in browser first", choices=["y", "n", "e"], default="n")
            if choice == "y":
                if blocking:
                    console.print("[yellow]Refusing to submit with required questions blank or drafts unapproved. Use [e] to complete them.[/]")
                    continue
                return "submit"
            if choice == "n":
                return "manual" if blocking else "skip"
            Prompt.ask("Edit the form in the browser. Press Enter here when done", default="")
            fill.pending = []
            fill.unanswered = []
            fill.answers.append(FieldAnswer("(edited in browser)", "see browser", "user", status="filled", reason="edited by you"))
            confirm = Prompt.ask("Submit now? [y]es / [n]o (mark needs input)", choices=["y", "n"], default="n")
            return "submit" if confirm == "y" else "manual"

    def captcha(self, page: Any, stage: str, app_id: int) -> bool:
        from .browser import captcha_present

        if self.headless:
            return False
        console.print(f"[bold yellow]A CAPTCHA / bot check appeared {stage}. Solve it in the browser, then press Enter.[/]")
        Prompt.ask("Press Enter when solved (or type s to skip)", default="")
        if not captcha_present(page):
            return True
        ans = Prompt.ask("Captcha still present. [s]kip and mark needs input, or [r]e-check", choices=["s", "r"], default="r")
        return ans == "r" and not captcha_present(page)
