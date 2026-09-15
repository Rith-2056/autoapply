"""Orchestrates one run: sync listings -> filter -> apply -> record -> report."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.prompt import Prompt

from .ats import detect_ats, get_handler_class
from .ats.base import ApplyContext, FillResult, HandlerError, NeedsManual
from .browser import captcha_present, launch, screenshot
from .config import Profile, Secrets, Settings
from .db import ApplicationRecord, Database
from .listings import Listing, ListingFilters, filter_listings, load_listings, sync_repo
from .llm import QuestionAnswerer
from .report import console, print_answers, print_records

log = logging.getLogger("autoapply.runner")


@dataclass
class RunOptions:
    mode: str = "review"  # review | auto
    dry_run: bool = False
    limit: int | None = None
    headless: bool | None = None
    retry: bool = False  # re-attempt failed / needs_manual / skipped listings
    no_sync: bool = False
    ats_only: list[str] = field(default_factory=list)
    company: str | None = None  # only listings whose company matches
    run_id: str = ""


class Runner:
    def __init__(self, settings: Settings, profile: Profile, secrets: Secrets, db: Database, opts: RunOptions):
        self.settings = settings
        self.profile = profile
        self.secrets = secrets
        self.db = db
        self.opts = opts
        self.results: list[ApplicationRecord] = []
        self.llm = QuestionAnswerer(
            api_key=secrets.anthropic_api_key,
            model=str(settings.get("llm.model", "claude-opus-5")),
            resume_text=profile.resume_text(),
            profile_summary=profile.summary_for_llm(),
            min_confidence=str(settings.get("llm.min_confidence", "high")),
            max_resume_chars=int(settings.get("llm.max_resume_chars", 12000)),
        )

    # ------------------------------------------------------------------ #
    # Listings
    # ------------------------------------------------------------------ #

    def collect_candidates(self) -> tuple[list[Listing], dict[str, Any]]:
        s = self.settings
        repo_path = s.listings_repo_path
        if not self.opts.no_sync:
            sync_repo(repo_path, str(s.get("listings.repo_url")), str(s.get("listings.branch", "dev")))
        listings, source = load_listings(repo_path, str(s.get("listings.json_path", ".github/scripts/listings.json")))
        filters = ListingFilters.from_settings(s.data)
        matched = filter_listings(listings, filters)

        allow = set(self.opts.ats_only or s.get("filters.ats_allowlist", []) or [])
        retry_statuses = ("failed", "needs_manual", "skipped") if self.opts.retry else ()
        candidates: list[Listing] = []
        skipped_db = skipped_ats = 0
        for l in matched:
            if self.opts.company and self.opts.company.lower() not in l.company.lower():
                continue
            if self.db.already_handled(l.id, l.url, retry_statuses):
                skipped_db += 1
                continue
            if allow and detect_ats(l.url) not in allow:
                skipped_ats += 1
                continue
            candidates.append(l)
        stats = {
            "source": source,
            "total": len(listings),
            "matched_filters": len(matched),
            "already_in_db": skipped_db,
            "ats_not_allowed": skipped_ats,
            "candidates": len(candidates),
        }
        return candidates, stats

    # ------------------------------------------------------------------ #
    # Run
    # ------------------------------------------------------------------ #

    def run(self) -> list[ApplicationRecord]:
        from playwright.sync_api import sync_playwright

        candidates, stats = self.collect_candidates()
        limit = self.opts.limit or int(self.settings.get("filters.max_applications_per_run", 10))
        console.print(
            f"Listings source: [bold]{stats['source']}[/] | total {stats['total']} | after filters {stats['matched_filters']} | "
            f"already in db {stats['already_in_db']} | ats not allowed {stats['ats_not_allowed']} | "
            f"candidates {stats['candidates']} | limit {limit}"
        )
        self.db.start_run(self.opts.run_id, self.opts.mode, self.opts.dry_run, len(candidates))
        if not candidates:
            self.db.finish_run(self.opts.run_id, 0)
            return []
        if self.opts.mode == "auto" and self.profile.placeholders():
            console.print(
                "[yellow]profile.yaml still has [FILL IN] placeholders: "
                + ", ".join(self.profile.placeholders())
                + ". Those fields will be left blank (and the job marked needs_manual if they are required).[/]"
            )

        headless = self.opts.headless if self.opts.headless is not None else bool(self.settings.get("run.headless", False))
        timeout_ms = int(self.settings.get("run.page_timeout_ms", 45000))
        dmin = float(self.settings.get("run.delay_min_seconds", 20))
        dmax = float(self.settings.get("run.delay_max_seconds", 60))

        attempted = 0
        with sync_playwright() as pw:
            context = launch(pw, headless, self.settings.browser_profile_dir, timeout_ms, self.settings.get("run.chromium_executable") or None)
            try:
                for listing in candidates:
                    if attempted >= limit:
                        break
                    if attempted > 0:
                        delay = random.uniform(dmin, dmax)
                        log.info("Sleeping %.0fs before next application", delay)
                        time.sleep(delay)
                    page = context.new_page()
                    try:
                        rec = self.apply_one(page, listing)
                    finally:
                        try:
                            page.close()
                        except Exception:  # noqa: BLE001
                            pass
                    self.db.insert(rec)
                    self.results.append(rec)
                    attempted += 1
                    if rec.status == "aborted":  # pragma: no cover - defensive
                        break
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
        self.db.finish_run(self.opts.run_id, attempted)
        return self.results

    # ------------------------------------------------------------------ #
    # One application
    # ------------------------------------------------------------------ #

    def apply_one(self, page, listing: Listing) -> ApplicationRecord:
        ats = detect_ats(listing.url)
        console.rule(f"[bold]{listing.company}[/] — {listing.title} [{ats}]")
        log.info("Applying: %s | %s | %s | %s", listing.company, listing.title, ats, listing.url)
        rec = ApplicationRecord(
            listing_id=listing.id,
            company=listing.company,
            role=listing.title,
            location=listing.location,
            url=listing.url,
            ats=ats,
            status="skipped",
            run_id=self.opts.run_id,
            mode=("dry_run" if self.opts.dry_run else self.opts.mode),
        )
        shots = self.settings.screenshots_dir
        ctx = ApplyContext(page=page, profile=self.profile, llm=self.llm, resume_pdf=self.profile.resume_pdf, timeout_ms=int(self.settings.get("run.page_timeout_ms", 45000)))
        cls = get_handler_class(ats)
        handler = cls(ctx, self.secrets.workday_accounts) if ats == "workday" else cls(ctx)

        try:
            handler.open(listing)
            # A Simplify / short link may redirect to a different ATS: re-detect.
            final_ats = detect_ats(page.url)
            if final_ats != ats and final_ats != "generic":
                log.info("Redirected to %s (%s); switching handler", final_ats, page.url)
                rec.ats = final_ats
                listing.url = page.url
                cls = get_handler_class(final_ats)
                handler = cls(ctx, self.secrets.workday_accounts) if final_ats == "workday" else cls(ctx)
                handler.open(listing)

            if self._captcha_gate(page, rec, shots, listing, stage="before filling"):
                return rec

            fill: FillResult = handler.fill()
            rec.answers = [a.__dict__ for a in fill.answers]
            rec.unanswered_questions = list(fill.unanswered)

            if self._captcha_gate(page, rec, shots, listing, stage="after filling"):
                return rec

            if fill.unanswered and self.opts.mode == "auto":
                rec.status = "needs_manual"
                rec.error = "Required questions could not be answered truthfully from profile/resume"
                rec.screenshot_path = screenshot(page, shots, listing.company, "needs_manual")
                return rec

            if self.opts.dry_run:
                print_answers(fill.answers, fill.unanswered)
                rec.status = "dry_run"
                rec.error = "" if not fill.unanswered else "dry run; required questions unanswered"
                rec.screenshot_path = screenshot(page, shots, listing.company, "dry_run")
                console.print("[cyan]Dry run: form filled, NOT submitted.[/]")
                if self.opts.mode == "review":
                    Prompt.ask("Press Enter to continue to the next listing", default="")
                return rec

            if self.opts.mode == "review":
                decision = self._review(page, fill)
                if decision == "skip":
                    rec.status = "skipped"
                    rec.error = "skipped by user in review"
                    return rec
                if decision == "manual":
                    rec.status = "needs_manual"
                    rec.error = "left for manual completion by user"
                    rec.screenshot_path = screenshot(page, shots, listing.company, "needs_manual")
                    return rec

            # Submit
            ok = handler.submit()
            if not ok and captcha_present(page):
                if self._captcha_gate(page, rec, shots, listing, stage="on submit"):
                    return rec
                ok = handler.wait_for_confirmation(timeout_s=20)
            if ok:
                rec.status = "applied"
                rec.screenshot_path = screenshot(page, shots, listing.company, "confirmation")
                console.print("[bold green]Submitted.[/]")
            else:
                errors = handler.visible_errors()
                rec.status = "failed"
                rec.error = "No confirmation after submit" + (": " + "; ".join(errors[:5]) if errors else "")
                rec.screenshot_path = screenshot(page, shots, listing.company, "failed")
                console.print(f"[red]{rec.error}[/]")
        except NeedsManual as e:
            rec.status = "needs_manual"
            rec.error = e.reason
            rec.unanswered_questions = list(dict.fromkeys(rec.unanswered_questions + e.questions))
            rec.screenshot_path = screenshot(page, shots, listing.company, "needs_manual")
            console.print(f"[yellow]needs_manual: {e.reason}[/]")
        except HandlerError as e:
            rec.status = "failed"
            rec.error = str(e)
            rec.screenshot_path = screenshot(page, shots, listing.company, "failed")
            console.print(f"[red]failed: {e}[/]")
        except Exception as e:  # noqa: BLE001
            log.exception("Unexpected error")
            rec.status = "failed"
            rec.error = f"{type(e).__name__}: {e}"[:500]
            rec.screenshot_path = screenshot(page, shots, listing.company, "failed")
            console.print(f"[red]failed: {rec.error}[/]")
        return rec

    # ------------------------------------------------------------------ #
    # Interaction
    # ------------------------------------------------------------------ #

    def _captcha_gate(self, page, rec: ApplicationRecord, shots: Path, listing: Listing, stage: str) -> bool:
        """Returns True if the run of this listing should stop here."""
        if not captcha_present(page):
            return False
        log.info("Captcha detected %s", stage)
        if self.opts.mode == "review" and not (self.opts.headless or self.settings.get("run.headless")):
            console.print(f"[bold yellow]A CAPTCHA / bot check appeared {stage}. Solve it in the browser, then press Enter.[/]")
            Prompt.ask("Press Enter when solved (or type s to skip)", default="")
            if not captcha_present(page):
                return False
            ans = Prompt.ask("Captcha still present. [s]kip and mark needs_manual, or [r]e-check", choices=["s", "r"], default="r")
            if ans == "r" and not captcha_present(page):
                return False
        rec.status = "needs_manual"
        rec.error = f"CAPTCHA / bot check {stage}; not bypassed"
        rec.screenshot_path = screenshot(page, shots, listing.company, "captcha")
        return True

    def _review(self, page, fill: FillResult) -> str:
        """Returns 'submit', 'skip' or 'manual'."""
        while True:
            print_answers(fill.answers, fill.unanswered)
            if fill.unanswered:
                console.print("[yellow]Some required questions are blank. Choose [e] to fill them in the browser, or [n] to leave this job as needs_manual.[/]")
            choice = Prompt.ask("Submit this application? [y]es / [n]o (skip) / [e]dit in browser first", choices=["y", "n", "e"], default="n")
            if choice == "y":
                if fill.unanswered:
                    console.print("[yellow]Refusing to submit with required questions blank. Use [e] to complete them.[/]")
                    continue
                return "submit"
            if choice == "n":
                return "manual" if fill.unanswered else "skip"
            # edit
            Prompt.ask("Edit the form in the browser. Press Enter here when done", default="")
            fill.unanswered = []  # user takes responsibility for the edited form
            fill.answers.append(type(fill.answers[0])("(edited in browser)", "see browser", "manual") if fill.answers else None)  # type: ignore[arg-type]
            fill.answers = [a for a in fill.answers if a is not None]
            confirm = Prompt.ask("Submit now? [y]es / [n]o (mark needs_manual)", choices=["y", "n"], default="n")
            return "submit" if confirm == "y" else "manual"


def print_run_summary(records: list[ApplicationRecord], run_id: str) -> None:
    print_records(records, title=f"Run {run_id}: {len(records)} attempt(s)", show_url=False)
    console.print("[dim]Full details incl. URLs: autoapply status --run " + run_id + "[/]")
    counts: dict[str, int] = {}
    for r in records:
        counts[r.status] = counts.get(r.status, 0) + 1
    if counts:
        console.print("  ".join(f"[bold]{k}[/]={v}" for k, v in counts.items()))
