"""Orchestrates one run: sync listings -> filter -> apply -> record -> report.

The runner is UI-agnostic: it records everything in the tracker and asks an
``Interaction`` whenever a person is needed (terminal or web).
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .ats import detect_ats, get_handler_class
from .ats.base import ApplyContext, FillResult, HandlerError, NeedsManual
from .browser import captcha_present, launch, screenshot
from .config import Profile, Secrets, Settings, resolve_path
from .eligibility import evaluate
from .interaction import Interaction, SessionCancelled, TerminalInteraction
from .platforms import PlatformProfiles, normalize_question
from .listings import Listing, ListingFilters, filter_listings, load_listings, sync_repo
from .llm import QuestionAnswerer
from .report import console, print_answers
from .tracker import ApplicationStatus, EventType, Tracker

log = logging.getLogger("autoapply.runner")

RETRYABLE = {ApplicationStatus.FAILED, ApplicationStatus.NEEDS_INPUT, ApplicationStatus.WITHDRAWN}
FRESH = {ApplicationStatus.DISCOVERED, ApplicationStatus.READY_TO_APPLY}


@dataclass
class RunOptions:
    mode: str = "review"  # review | auto
    dry_run: bool = False
    limit: int | None = None
    headless: bool | None = None
    retry: bool = False
    no_sync: bool = False
    ats_only: list[str] = field(default_factory=list)
    company: str | None = None
    voice: bool = False
    run_id: str = ""
    session_id: int | None = None
    # Overrides coming from the web AutoApply panel.
    title_keywords: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    exclude_companies: list[str] = field(default_factory=list)
    exclude_locations: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    listing_ids: list[str] = field(default_factory=list)  # apply to exactly these
    us_only: bool | None = None  # None = settings default (true)


@dataclass
class RunResult:
    app_id: int
    company: str
    role: str
    status: str
    error: str = ""


class Runner:
    def __init__(self, settings: Settings, profile: Profile, secrets: Secrets, tracker: Tracker, opts: RunOptions,
                 interaction: Interaction | None = None, on_event: Callable[[dict[str, Any]], None] | None = None):
        self.settings = settings
        self.profile = profile
        self.secrets = secrets
        self.tracker = tracker
        self.opts = opts
        self.results: list[RunResult] = []
        self.on_event = on_event or (lambda e: None)
        self.llm = QuestionAnswerer(
            api_key=secrets.anthropic_api_key,
            model=str(settings.get("llm.model", "claude-opus-5")),
            resume_text=profile.resume_text(),
            profile_summary=profile.summary_for_llm(),
            min_confidence=str(settings.get("llm.min_confidence", "medium")),
            max_resume_chars=int(settings.get("llm.max_resume_chars", 12000)),
            web_research=bool(settings.get("llm.web_research", True)),
            research_cache_dir=resolve_path(str(settings.get("llm.research_cache_dir", "./data/research"))),
            max_job_text_chars=int(settings.get("llm.max_job_text_chars", 8000)),
        )
        self.interaction: Interaction = interaction or TerminalInteraction(self._terminal_voice(), self._is_headless())
        self.platforms = PlatformProfiles()
        self.excluded: list[dict[str, Any]] = []  # listings rejected by the eligibility gate (with reasons)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _terminal_voice(self):
        if not self.opts.voice:
            return None
        from .voice import build_voice

        v = build_voice(self.settings.get("voice", {}) or {}, console)
        console.print("[green]Voice mode on.[/]" if v.can_listen else "[yellow]Voice backend unavailable; typed answers.[/]")
        return v

    def _is_headless(self) -> bool:
        return bool(self.opts.headless if self.opts.headless is not None else self.settings.get("run.headless", False))

    def emit(self, app_id: int, event: EventType, message: str = "", metadata: dict[str, Any] | None = None) -> None:
        try:
            ev = self.tracker.record_event(app_id, event, message, metadata, enforce=False)
        except Exception as e:  # noqa: BLE001
            log.warning("could not record event %s: %s", event, e)
            return
        log.info("[app %s] %s %s", app_id, event.value, message)
        self.on_event({"kind": "application_event", **ev})

    # ------------------------------------------------------------------ #
    # Listings
    # ------------------------------------------------------------------ #

    def _filters(self) -> ListingFilters:
        f = ListingFilters.from_settings(self.settings.data)
        o = self.opts
        if o.title_keywords:
            f.title_keywords = list(o.title_keywords)
        if o.locations:
            f.locations = list(o.locations)
        if o.exclude_companies:
            f.exclude_companies = list(f.exclude_companies) + list(o.exclude_companies)
        if o.categories:
            from .listings import normalize_category

            f.categories = [normalize_category(c) for c in o.categories]
        return f

    def collect_candidates(self) -> tuple[list[Listing], dict[str, Any]]:
        s = self.settings
        repo_path = s.listings_repo_path
        if not self.opts.no_sync:
            try:
                sync_repo(repo_path, str(s.get("listings.repo_url")), str(s.get("listings.branch", "dev")))
            except Exception as e:  # noqa: BLE001
                log.warning("listings sync failed: %s", e)
        listings, source = load_listings(repo_path, str(s.get("listings.json_path", ".github/scripts/listings.json")))
        if self.opts.listing_ids:
            wanted = set(self.opts.listing_ids)
            matched = [l for l in listings if l.id in wanted or l.url in wanted]
        else:
            matched = filter_listings(listings, self._filters())
        allow = set(self.opts.ats_only or s.get("filters.ats_allowlist", []) or [])
        us_only = self.opts.us_only if self.opts.us_only is not None else bool(s.get("filters.us_only", True))
        filters = self._filters()
        candidates: list[Listing] = []
        self.excluded = []
        by_check: dict[str, int] = {}
        for l in matched:
            if self.opts.company and self.opts.company.lower() not in l.company.lower():
                continue
            gate = evaluate(l, filters, self.tracker, us_only=us_only, retry=self.opts.retry,
                            ats_allow=allow if not self.opts.listing_ids else None, exclude_locations=self.opts.exclude_locations)
            if gate.eligible:
                candidates.append(l)
            else:
                by_check[gate.failed] = by_check.get(gate.failed, 0) + 1
                self.excluded.append({**l.to_dict(), "gate": gate.to_dict()})
        stats = {"source": source, "total": len(listings), "matched_filters": len(matched), "excluded_by_check": by_check,
                 "already_in_db": by_check.get("already_applied", 0), "ats_not_allowed": by_check.get("ats_supported", 0),
                 "not_us": by_check.get("us_location", 0), "candidates": len(candidates), "us_only": us_only}
        return candidates, stats

    # ------------------------------------------------------------------ #
    # Run
    # ------------------------------------------------------------------ #

    def run(self) -> list[RunResult]:
        from playwright.sync_api import sync_playwright

        candidates, stats = self.collect_candidates()
        limit = self.opts.limit or int(self.settings.get("filters.max_applications_per_run", 10))
        console.print(
            f"Listings source: [bold]{stats['source']}[/] | total {stats['total']} | after filters {stats['matched_filters']} | "
            f"already tracked {stats['already_in_db']} | ats not allowed {stats['ats_not_allowed']} | candidates {stats['candidates']} | limit {limit}"
        )
        self.on_event({"kind": "session", "message": f"{stats['candidates']} candidate listings; limit {limit}", "stats": stats})
        if not candidates:
            return []
        if self.opts.mode == "auto" and self.profile.placeholders():
            console.print("[yellow]profile.yaml still has [FILL IN] placeholders: " + ", ".join(self.profile.placeholders()) + "[/]")

        headless = self._is_headless()
        timeout_ms = int(self.settings.get("run.page_timeout_ms", 45000))
        dmin = float(self.settings.get("run.delay_min_seconds", 20))
        dmax = float(self.settings.get("run.delay_max_seconds", 60))
        attempted = 0
        with sync_playwright() as pw:
            context = launch(pw, headless, self.settings.browser_profile_dir, timeout_ms, self.settings.get("run.chromium_executable") or None)
            try:
                for listing in candidates[:limit]:
                    self.interaction.checkpoint()
                    if attempted > 0:
                        delay = random.uniform(dmin, dmax)
                        log.info("Sleeping %.0fs before next application", delay)
                        self.on_event({"kind": "session", "message": f"Waiting {delay:.0f}s before the next application"})
                        time.sleep(delay)
                        self.interaction.checkpoint()
                    page = context.new_page()
                    try:
                        res = self.apply_one(page, listing)
                    finally:
                        try:
                            page.close()
                        except Exception:  # noqa: BLE001
                            pass
                    self.results.append(res)
                    attempted += 1
            except SessionCancelled:
                log.info("Session cancelled by user")
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
        return self.results

    # ------------------------------------------------------------------ #
    # One application
    # ------------------------------------------------------------------ #

    def apply_one(self, page, listing: Listing) -> RunResult:
        ats = detect_ats(listing.url)
        console.rule(f"[bold]{listing.company}[/] — {listing.title} [{ats}]")
        job = self.tracker.upsert_job(listing.company, listing.title, listing.url, listing.location, source=listing.source or "simplify",
                                      listing_id=listing.id, category=listing.category)
        existing = self.tracker.application_for_job(job["id"])
        if existing and ApplicationStatus(existing["status"]) in FRESH | RETRYABLE:
            app_id = existing["id"]
            self.tracker.update_application(app_id, ats=ats, session_id=self.opts.session_id, error="")
            self.emit(app_id, EventType.APPLICATION_RESUMED, "Retrying application")
        else:
            app = self.tracker.create_application(job["id"], ats=ats, resume_path=str(self.profile.resume_pdf), session_id=self.opts.session_id)
            app_id = app["id"]
        res = RunResult(app_id, listing.company, listing.title, "APPLYING")
        shots = self.settings.screenshots_dir
        ctx = ApplyContext(page=page, profile=self.profile, llm=self.llm, resume_pdf=self.profile.resume_pdf,
                           timeout_ms=int(self.settings.get("run.page_timeout_ms", 45000)), platforms=self.platforms)
        cls = get_handler_class(ats)
        handler = cls(ctx, self.secrets.workday_accounts) if ats == "workday" else cls(ctx)

        def fail(status_event: EventType, message: str, tag: str) -> RunResult:
            shot = screenshot(page, shots, listing.company, tag)
            self.tracker.update_application(app_id, error=message, screenshot_path=shot, step=tag)
            self.emit(app_id, status_event, message, {"screenshot": shot})
            res.status = self.tracker.get_application(app_id)["status"]  # type: ignore[index]
            res.error = message
            return res

        try:
            self.emit(app_id, EventType.APPLICATION_STARTED, f"Starting application at {ats}", {"url": listing.url})
            self.tracker.update_application(app_id, step="opening")
            handler.open(listing)
            final_ats = detect_ats(page.url)
            if final_ats != ats and final_ats != "generic":
                log.info("Redirected to %s (%s); switching handler", final_ats, page.url)
                self.tracker.update_application(app_id, ats=final_ats)
                listing.url = page.url
                cls = get_handler_class(final_ats)
                handler = cls(ctx, self.secrets.workday_accounts) if final_ats == "workday" else cls(ctx)
                handler.open(listing)
            self.tracker.update_application(app_id, application_url=page.url)
            self.emit(app_id, EventType.APPLICATION_OPENED, "Application form opened", {"url": page.url})

            if not self._captcha_ok(page, app_id, "before filling"):
                return fail(EventType.CAPTCHA_ENCOUNTERED, "CAPTCHA / bot check before filling; not bypassed", "captcha")

            self.tracker.update_application(app_id, step="filling")
            handler.prepare_context(research=bool(self.settings.get("llm.web_research", True)))
            if handler._job_text:
                self.tracker._exec("UPDATE jobs SET description=? WHERE id=? AND (description IS NULL OR description='')", (handler._job_text[:20000], job["id"]))
            fill: FillResult = handler.fill()
            self._record_fill(app_id, fill)

            if not self._captcha_ok(page, app_id, "after filling"):
                return fail(EventType.CAPTCHA_ENCOUNTERED, "CAPTCHA / bot check after filling; not bypassed", "captcha")

            # Web sessions are always interactive (the UI answers); the CLI needs a visible browser.
            interactive = bool(self.opts.session_id) or ((self.opts.mode == "review" or self.opts.voice) and not self._is_headless())
            if fill.pending and interactive:
                self.tracker.update_application(app_id, step="questions")
                self.emit(app_id, EventType.INPUT_REQUESTED, f"{len(fill.pending)} question(s) need you", {"count": len(fill.pending)})
                self.interaction.resolve_pending(handler, fill, app_id)
                self._record_fill(app_id, fill, update_only=True)
                self.emit(app_id, EventType.ANSWER_PROVIDED, "Questions resolved", {"remaining": len(fill.pending)})

            blocking = [p for p in fill.pending if p.required or p.draft]
            if blocking and not interactive:
                return fail(EventType.INPUT_REQUESTED, "Questions need your input", "needs_input")

            if self.opts.dry_run:
                print_answers(fill.answers, fill.pending)
                shot = screenshot(page, shots, listing.company, "dry_run")
                self.tracker.update_application(app_id, screenshot_path=shot, step="dry_run")
                self.emit(app_id, EventType.NOTE, "Dry run: form filled, not submitted", {"screenshot": shot})
                self.tracker.set_status(app_id, ApplicationStatus.READY_TO_APPLY, "dry run")
                console.print("[cyan]Dry run: form filled, NOT submitted.[/]")
                res.status = "READY_TO_APPLY"
                return res

            if interactive:
                self.tracker.update_application(app_id, step="review")
                self.emit(app_id, EventType.REVIEW_REQUESTED, "Waiting for your review before submitting")
                decision = self.interaction.review(fill, app_id)
                if decision == "skip":
                    return fail(EventType.APPLICATION_SKIPPED, "Skipped by you during review", "skipped")
                if decision == "manual":
                    return fail(EventType.INPUT_REQUESTED, "Left for you to finish in the browser", "needs_input")
                self._record_fill(app_id, fill, update_only=True)

            self.tracker.update_application(app_id, step="submitting")
            ok = handler.submit()
            if not ok and captcha_present(page):
                if not self._captcha_ok(page, app_id, "on submit"):
                    return fail(EventType.CAPTCHA_ENCOUNTERED, "CAPTCHA on submit; not bypassed", "captcha")
                ok = handler.wait_for_confirmation(timeout_s=20)
            if ok:
                shot = screenshot(page, shots, listing.company, "confirmation")
                self.tracker.update_application(app_id, screenshot_path=shot, step="done", error="")
                self.emit(app_id, EventType.APPLICATION_SUBMITTED, "Application submitted", {"screenshot": shot, "url": page.url})
                self._save_approved_answers(app_id, fill, listing.company)
                res.status = "SUBMITTED"
                console.print("[bold green]Submitted.[/]")
                return res
            errors = handler.visible_errors()
            return fail(EventType.APPLICATION_FAILED, "No confirmation after submit" + (": " + "; ".join(errors[:5]) if errors else ""), "failed")
        except SessionCancelled:
            self.emit(app_id, EventType.APPLICATION_FAILED, "Session cancelled while applying", {"cancelled": True})
            raise
        except NeedsManual as e:
            for q in e.questions:
                self.tracker.add_question(app_id, q, status="pending", reason=e.reason)
            return fail(EventType.INPUT_REQUESTED, e.reason, "needs_input")
        except HandlerError as e:
            return fail(EventType.APPLICATION_FAILED, str(e), "failed")
        except Exception as e:  # noqa: BLE001
            log.exception("Unexpected error")
            return fail(EventType.APPLICATION_FAILED, f"{type(e).__name__}: {e}"[:500], "failed")

    # ------------------------------------------------------------------ #

    def _captcha_ok(self, page, app_id: int, stage: str) -> bool:
        if not captcha_present(page):
            return True
        self.emit(app_id, EventType.CAPTCHA_ENCOUNTERED, f"CAPTCHA detected {stage}", {"stage": stage})
        if self.opts.mode == "review" or self.opts.voice or self.opts.session_id:
            if self.interaction.captcha(page, stage, app_id):
                self.emit(app_id, EventType.ANSWER_PROVIDED, "CAPTCHA solved by you")
                return True
        return False

    def _record_fill(self, app_id: int, fill: FillResult, update_only: bool = False) -> None:
        """Persist every field decision as question + answer rows."""
        existing = {q["field_id"]: q for q in self.tracker.questions(app_id) if q.get("field_id")}
        filled = sum(1 for a in fill.answers if a.status == "filled")
        app_row = self.tracker.get_application(app_id) or {}
        platform = app_row.get("ats") or ""
        for a in fill.answers:
            if not a.label:
                continue
            if platform and a.kind != "file":
                self.tracker.record_platform_question(platform, normalize_question(a.label), a.label, a.category, a.kind, None,
                                                      a.value if a.status == "filled" else "", a.source)
            status = {"filled": "auto", "draft": "pending", "needs_user": "pending", "n/a": "skipped"}.get(a.status, "pending")
            q = existing.get(a.field_id) if a.field_id else None
            if q is None:
                if update_only and not a.field_id:
                    continue
                q = self.tracker.add_question(app_id, a.label, a.field_id, a.kind, a.category, None, a.required, status, a.reason)
                existing[a.field_id] = q
            else:
                self.tracker.set_question_status(q["id"], status)
            last = (q.get("answers") or [{}])[-1] if q.get("answers") else {}
            if a.value and last.get("final_answer") != a.value:
                self.tracker.add_answer(q["id"], a.value, a.source, cleaned=a.value if a.source in ("inferred", "draft") else "",
                                        confidence=a.reason.split(":")[0] if a.source == "inferred" else "",
                                        ai_modified=a.source in ("inferred", "draft"), user_approved=a.source in ("user", "profile"), status=status)
        if not update_only:
            self.emit(app_id, EventType.FIELDS_FILLED, f"Filled {filled} field(s)", {"filled": filled, "pending": len(fill.pending)})
            if fill.resume_uploaded:
                self.emit(app_id, EventType.RESUME_UPLOADED, f"Uploaded {self.profile.resume_pdf.name}")
            for p in fill.pending:
                self.emit(app_id, EventType.QUESTION_DETECTED, p.label[:200], {"category": p.category, "required": p.required, "draft": bool(p.draft)})

    def _save_approved_answers(self, app_id: int, fill: FillResult, company: str) -> None:
        for a in fill.answers:
            if a.source in ("user", "draft") and a.status == "filled" and a.kind in ("textarea", "text") and len(a.value) > 40:
                self.tracker.save_approved_answer(a.label, a.value, company)


def print_run_summary(results: list[RunResult], run_id: str) -> None:
    from rich.table import Table

    table = Table(title=f"Run {run_id}: {len(results)} attempt(s)", expand=True)
    for col in ("Company", "Role", "Status", "Notes"):
        table.add_column(col)
    for r in results:
        table.add_row(r.company, r.role, r.status, r.error[:100])
    console.print(table)
    console.print("[dim]Open the web app (autoapply web) or run: autoapply status[/]")
