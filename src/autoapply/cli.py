"""autoapply command-line interface."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .config import PROJECT_ROOT, load_profile, load_secrets, load_settings
from .db import STATUSES, Database
from .report import console, print_counts, print_records

app = typer.Typer(
    name="autoapply",
    help="Apply to Summer 2027 internships from the SimplifyJobs list and track every attempt.",
    no_args_is_help=True,
    add_completion=False,
)


def _db() -> Database:
    return Database(load_settings().database_path)


@app.callback()
def _root(version: bool = typer.Option(False, "--version", help="Show version and exit")) -> None:
    if version:
        console.print(f"autoapply {__version__}")
        raise typer.Exit()


@app.command()
def run(
    review: bool = typer.Option(True, "--review/--auto", help="Review each filled form before submitting (default) or submit automatically."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fill forms but never submit."),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Max applications this run (overrides settings)."),
    headless: Optional[bool] = typer.Option(None, "--headless/--headed", help="Override run.headless from settings."),
    retry: bool = typer.Option(False, "--retry", help="Re-attempt listings previously marked failed / needs_manual / skipped."),
    no_sync: bool = typer.Option(False, "--no-sync", help="Do not git pull the listings repo first."),
    ats: Optional[str] = typer.Option(None, "--ats", help="Comma-separated ATS types to attempt (e.g. greenhouse,lever)."),
    company: Optional[str] = typer.Option(None, "--company", help="Only listings whose company name contains this text."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Apply to matching listings (review mode by default)."""
    from .logging_setup import new_run_id, setup_logging
    from .runner import RunOptions, Runner, print_run_summary

    settings = load_settings()
    profile = load_profile()
    secrets = load_secrets()
    run_id = new_run_id()
    log_path = setup_logging(settings.logs_dir, run_id, verbose)
    mode = "review" if review else "auto"
    console.print(f"[bold]autoapply run {run_id}[/] mode={mode} dry_run={dry_run} log={log_path}")

    if not profile.resume_pdf.exists():
        console.print(f"[red]Resume PDF not found at {profile.resume_pdf}. Put it there (see README) and re-run.[/]")
        raise typer.Exit(2)
    if not secrets.anthropic_api_key:
        console.print("[yellow]No ANTHROPIC_API_KEY in .env: unexpected questions will be left blank and the job marked needs_manual.[/]")
    if profile.placeholders():
        console.print("[yellow]profile.yaml placeholders still unfilled: " + ", ".join(profile.placeholders()) + "[/]")

    opts = RunOptions(
        mode=mode,
        dry_run=dry_run,
        limit=limit,
        headless=headless,
        retry=retry,
        no_sync=no_sync,
        ats_only=[a.strip() for a in ats.split(",")] if ats else [],
        company=company,
        run_id=run_id,
    )
    with _db() as db:
        runner = Runner(settings, profile, secrets, db, opts)
        try:
            records = runner.run()
        except KeyboardInterrupt:
            console.print("[yellow]Interrupted; recording what finished.[/]")
            records = runner.results
    print_run_summary(records, run_id)


@app.command()
def listings(
    limit: int = typer.Option(30, "--limit", "-n"),
    no_sync: bool = typer.Option(False, "--no-sync"),
    show_all: bool = typer.Option(False, "--all", help="Include listings already in the database / not-allowed ATS."),
) -> None:
    """Show which listings currently match your filters (no browser)."""
    from .ats import detect_ats
    from .listings import ListingFilters, filter_listings, load_listings, sync_repo
    from rich.table import Table

    settings = load_settings()
    if not no_sync:
        sync_repo(settings.listings_repo_path, str(settings.get("listings.repo_url")), str(settings.get("listings.branch", "dev")))
    all_listings, source = load_listings(settings.listings_repo_path, str(settings.get("listings.json_path")))
    matched = filter_listings(all_listings, ListingFilters.from_settings(settings.data))
    allow = set(settings.get("filters.ats_allowlist", []) or [])
    table = Table(title=f"Matching listings from {source} ({len(matched)} of {len(all_listings)})", expand=True)
    table.add_column("Posted", no_wrap=True, min_width=10)
    table.add_column("Company", min_width=14)
    table.add_column("Role", min_width=20)
    table.add_column("Location", min_width=12)
    table.add_column("Cat", no_wrap=True)
    table.add_column("ATS", no_wrap=True)
    table.add_column("In DB", no_wrap=True)
    table.add_column("URL", overflow="fold")
    shown = 0
    with _db() as db:
        for l in matched:
            a = detect_ats(l.url)
            status = db.latest_status(l.id, l.url) or ""
            if not show_all and (status or (allow and a not in allow)):
                continue
            table.add_row(l.posted.strftime("%Y-%m-%d"), l.company[:28], l.title[:45], l.location[:28], l.category, a, status, l.url)
            shown += 1
            if shown >= limit:
                break
    console.print(table)


@app.command()
def status(
    status: Optional[str] = typer.Option(None, "--status", "-s", help=f"One of {', '.join(STATUSES)}"),
    company: Optional[str] = typer.Option(None, "--company", "-c", help="Company name contains"),
    since: Optional[str] = typer.Option(None, "--since", help="YYYY-MM-DD"),
    until: Optional[str] = typer.Option(None, "--until", help="YYYY-MM-DD"),
    run_id: Optional[str] = typer.Option(None, "--run", help="Only this run id"),
    search: Optional[str] = typer.Option(None, "--search", help="Text in company/role/location"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n"),
    show_url: bool = typer.Option(True, "--url/--no-url"),
) -> None:
    """List recorded applications with filters."""
    if status and status not in STATUSES:
        console.print(f"[red]Unknown status {status!r}. Choose from: {', '.join(STATUSES)}[/]")
        raise typer.Exit(2)
    with _db() as db:
        records = db.query(status=status, company=company, since=since, until=until, run_id=run_id, search=search, limit=limit)
        print_records(records, title=f"{len(records)} application(s)", show_url=show_url)
        print_counts(db.counts_by_status())


@app.command()
def export(
    out: Path = typer.Option(Path("./data/applications.csv"), "--out", "-o", help="CSV output path"),
    status: Optional[str] = typer.Option(None, "--status", "-s"),
    company: Optional[str] = typer.Option(None, "--company", "-c"),
    since: Optional[str] = typer.Option(None, "--since"),
    until: Optional[str] = typer.Option(None, "--until"),
) -> None:
    """Export applications to CSV."""
    with _db() as db:
        n = db.export_csv(out, status=status, company=company, since=since, until=until)
    console.print(f"Exported {n} record(s) to {out}")


@app.command()
def dashboard(port: int = typer.Option(8501, "--port"), headless: bool = typer.Option(False, "--no-browser", help="Don't open a browser tab")) -> None:
    """Launch the local Streamlit dashboard."""
    app_path = Path(__file__).with_name("dashboard.py")
    cmd = [sys.executable, "-m", "streamlit", "run", str(app_path), "--server.port", str(port)]
    if headless:
        cmd += ["--server.headless", "true"]
    console.print(f"Starting dashboard on http://localhost:{port} (Ctrl+C to stop)")
    subprocess.run(cmd, check=False, cwd=str(PROJECT_ROOT))


@app.command("check")
def check() -> None:
    """Validate configuration: profile placeholders, resume, API key, browser."""
    settings = load_settings()
    profile = load_profile()
    secrets = load_secrets()
    ok = True
    ph = profile.placeholders()
    console.print(("[yellow]" if ph else "[green]") + f"profile placeholders: {ph or 'none'}[/]")
    console.print(("[green]" if profile.resume_pdf.exists() else "[red]") + f"resume pdf: {profile.resume_pdf} ({'found' if profile.resume_pdf.exists() else 'MISSING'})[/]")
    ok &= profile.resume_pdf.exists()
    txt = profile.resume_text_path
    console.print(("[green]" if txt.exists() else "[yellow]") + f"resume text: {txt} ({'found' if txt.exists() else 'missing; run: autoapply extract-resume'})[/]")
    console.print(("[green]" if secrets.anthropic_api_key else "[yellow]") + f"ANTHROPIC_API_KEY: {'set' if secrets.anthropic_api_key else 'not set'}[/]")
    console.print(f"workday accounts: {list(secrets.workday_accounts) or 'none'}")
    console.print(f"database: {settings.database_path}")
    try:
        from playwright.sync_api import sync_playwright

        import os

        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True, executable_path=settings.get("run.chromium_executable") or os.environ.get("AUTOAPPLY_CHROMIUM") or None)
            b.close()
        console.print("[green]playwright chromium: ok[/]")
    except Exception as e:  # noqa: BLE001
        ok = False
        console.print(f"[red]playwright chromium: {e}\nRun: playwright install chromium[/]")
    raise typer.Exit(0 if ok else 1)


@app.command("extract-resume")
def extract_resume() -> None:
    """Extract resume PDF text to resume/resume.txt (used as LLM context)."""
    from pypdf import PdfReader

    profile = load_profile()
    pdf = profile.resume_pdf
    if not pdf.exists():
        console.print(f"[red]Resume PDF not found: {pdf}[/]")
        raise typer.Exit(2)
    text = "\n\n".join((p.extract_text() or "") for p in PdfReader(str(pdf)).pages)
    profile.resume_text_path.parent.mkdir(parents=True, exist_ok=True)
    profile.resume_text_path.write_text(text, encoding="utf-8")
    console.print(f"Wrote {len(text)} characters to {profile.resume_text_path}")


if __name__ == "__main__":
    app()
