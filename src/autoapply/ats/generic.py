"""Fallback for unknown ATS: try to reach a form and fill it generically.

If no fillable form with a resume upload can be found, the listing is marked
``needs_manual`` so nothing is guessed.
"""

from __future__ import annotations

from ..listings import Listing
from .base import BaseHandler, FillResult, NeedsManual

APPLY_BUTTONS = (
    "a:has-text('Apply Now'), button:has-text('Apply Now'), a:has-text('Apply now'), button:has-text('Apply now'), "
    "a:has-text('Apply for this job'), a:has-text('Apply'), button:has-text('Apply'), "
    "a:has-text(\"I'm interested\"), a[href*='apply']"
)


class GenericHandler(BaseHandler):
    name = "generic"

    def open(self, listing: Listing) -> None:
        self.listing = listing
        self.goto(listing.url)
        if not self._has_form():
            self.click_if_present(APPLY_BUTTONS, timeout=5000)
        if not self._has_form():
            raise NeedsManual("No application form found on page (unknown ATS)")

    def _has_form(self) -> bool:
        try:
            has_file = self.page.locator("input[type=file]").count() > 0
            has_inputs = self.page.locator("form input:visible, form textarea:visible").count() >= 3
            return has_file or has_inputs
        except Exception:  # noqa: BLE001
            return False

    def fill(self) -> FillResult:
        result = self.fill_generic("generic")
        if not result.resume_uploaded:
            raise NeedsManual("No resume upload field found; not safe to submit automatically", result.unanswered)
        return result
