"""SmartRecruiters (jobs.smartrecruiters.com)."""

from __future__ import annotations

from ..listings import Listing
from .base import BaseHandler, FillResult, HandlerError


class SmartRecruitersHandler(BaseHandler):
    name = "smartrecruiters"

    def open(self, listing: Listing) -> None:
        self.listing = listing
        self.goto(listing.url)
        # Job page -> "I'm interested" opens the application form (same page or /apply).
        self.click_if_present("a:has-text(\"I'm interested\"), button:has-text(\"I'm interested\"), a[href*='/apply'], button:has-text('Apply')")
        try:
            self.page.locator("form").first.wait_for(timeout=10000)
        except Exception as e:  # noqa: BLE001
            raise HandlerError(f"SmartRecruiters form not found: {e}") from e
        # Dismiss "apply with LinkedIn/Indeed" chooser if shown.
        self.click_if_present("button:has-text('Apply manually'), a:has-text('Apply manually'), button:has-text('Continue')", timeout=2000)

    def fill(self) -> FillResult:
        return self.fill_generic("smartrecruiters")

    def submit_selector(self) -> str:
        return "button:has-text('Submit'), button[type=submit]"
