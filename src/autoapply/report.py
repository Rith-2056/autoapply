"""Terminal tables (rich)."""

from __future__ import annotations

from typing import Iterable

from rich.console import Console
from rich.table import Table

from .db import ApplicationRecord

console = Console()

STATUS_STYLE = {
    "applied": "bold green",
    "failed": "bold red",
    "needs_manual": "bold yellow",
    "skipped": "dim",
    "dry_run": "cyan",
}


def _short(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def print_records(records: Iterable[ApplicationRecord], title: str = "Applications", show_url: bool = True) -> None:
    table = Table(title=title, show_lines=show_url, expand=True)
    table.add_column("When", style="dim", no_wrap=True, min_width=16)
    table.add_column("Company", min_width=12)
    table.add_column("Role", min_width=16)
    table.add_column("Location", min_width=10)
    table.add_column("ATS", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Notes", min_width=20)
    if show_url:
        table.add_column("URL", overflow="fold", min_width=24)
    n = 0
    for r in records:
        n += 1
        notes = r.error or ""
        if r.unanswered_questions:
            notes = (notes + " " if notes else "") + "Unanswered: " + "; ".join(r.unanswered_questions)
        row = [
            r.timestamp.replace("T", " ")[:16],
            _short(r.company, 24),
            _short(r.role, 40),
            _short(r.location, 24),
            r.ats,
            f"[{STATUS_STYLE.get(r.status, '')}]{r.status}[/]",
            _short(notes, 120),
        ]
        if show_url:
            row.append(r.url)
        table.add_row(*row)
    if n == 0:
        console.print(f"[dim]{title}: nothing to show.[/]")
    else:
        console.print(table)


def print_counts(counts: dict[str, int]) -> None:
    table = Table(title="Totals", show_header=True)
    table.add_column("Status")
    table.add_column("Count", justify="right")
    for k, v in counts.items():
        table.add_row(f"[{STATUS_STYLE.get(k, '')}]{k}[/]", str(v))
    table.add_row("[bold]total[/]", f"[bold]{sum(counts.values())}[/]")
    console.print(table)


def print_answers(answers, pending=None) -> None:
    """Table of every field with its status. Nothing is hidden: fields that need
    the user are listed both in the table and in a separate 'Needs you' block."""
    table = Table(title="Form answers (review before submitting)", expand=True)
    table.add_column("Field", min_width=24)
    table.add_column("Answer", min_width=24)
    table.add_column("Source", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Req", no_wrap=True)
    style_for = {"profile": "green", "resume": "green", "inferred": "cyan", "llm": "cyan", "draft": "magenta",
                 "user": "bold green", "prefilled": "dim", "needs_user": "bold red", "n/a": "dim", "skipped": "yellow"}
    status_for = {"filled": "[green]filled[/]", "draft": "[magenta]needs approval[/]", "needs_user": "[bold red]NEEDS YOU[/]", "n/a": "[dim]n/a[/]"}
    for a in answers:
        g = (lambda k, d="": getattr(a, k, None) if hasattr(a, k) else a.get(k, d))
        source, status = g("source"), g("status", "filled")
        table.add_row(
            _short(g("label"), 60), _short(str(g("value")), 100),
            f"[{style_for.get(source, '')}]{source}[/]", status_for.get(status, status), "*" if g("required", False) else "",
        )
    console.print(table)
    pending = pending or []
    if pending:
        console.print(f"[bold yellow]{len(pending)} question(s) need you:[/]")
        for p in pending:
            g = (lambda k, d="": getattr(p, k, None) if hasattr(p, k) else p.get(k, d))
            tag = "required" if g("required", False) else "optional"
            extra = " [magenta](draft ready for approval)[/]" if g("draft", "") else ""
            console.print(f"  • {g('label')}  [dim]({g('category')}, {tag}) — {g('reason')}[/]{extra}")
