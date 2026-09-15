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


def print_answers(answers: list[dict] | list, unanswered: list[str]) -> None:
    table = Table(title="Form answers (review before submitting)", expand=True)
    table.add_column("Field")
    table.add_column("Answer")
    table.add_column("Source", no_wrap=True)
    table.add_column("Req", no_wrap=True)
    for a in answers:
        label = a.label if hasattr(a, "label") else a["label"]
        value = a.value if hasattr(a, "value") else a["value"]
        source = a.source if hasattr(a, "source") else a["source"]
        required = a.required if hasattr(a, "required") else a.get("required", False)
        style = {"llm": "magenta", "profile": "green", "resume": "green", "skipped": "yellow", "prefilled": "dim"}.get(source, "")
        table.add_row(_short(label, 50), _short(str(value), 90), f"[{style}]{source}[/]", "*" if required else "")
    console.print(table)
    if unanswered:
        console.print("[bold yellow]Required questions left unanswered:[/]")
        for q in unanswered:
            console.print(f"  • {q}")
