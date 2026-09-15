"""Ashby (jobs.ashbyhq.com)."""

from __future__ import annotations

from ..listings import Listing
from .base import BaseHandler, FillResult, HandlerError


class AshbyHandler(BaseHandler):
    name = "ashby"

    def open(self, listing: Listing) -> None:
        self.listing = listing
        url = listing.url.split("?")[0].rstrip("/")
        if not url.endswith("/application"):
            url = url + "/application"
        self.goto(url)
        if not self._form_visible():
            # Some postings land on the overview tab; click "Apply".
            self.click_if_present("a:has-text('Apply for this Job'), a:has-text('Apply'), button:has-text('Apply')")
        if not self._form_visible():
            raise HandlerError("Ashby application form not found")

    def _form_visible(self) -> bool:
        try:
            return self.page.locator("form, [class*='ashby-application-form' i]").first.is_visible(timeout=8000)
        except Exception:  # noqa: BLE001
            return False

    def fill(self) -> FillResult:
        return self.fill_generic("ashby")

    def submit_selector(self) -> str:
        return "button:has-text('Submit Application'), button:has-text('Submit application'), button[type=submit]"
