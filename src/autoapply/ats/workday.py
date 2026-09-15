"""Workday (*.myworkdayjobs.com).

Workday requires a per-company candidate account. Without credentials for the
listing's host in ``WORKDAY_ACCOUNTS`` (.env) the listing is marked
``needs_manual``. With credentials we sign in, click "Apply Manually", and fill
each page of the multi-step form generically, marking ``needs_manual`` as soon
as a required question cannot be answered.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from ..listings import Listing
from .base import ApplyContext, BaseHandler, FillResult, HandlerError, NeedsManual

log = logging.getLogger("autoapply.ats.workday")


class WorkdayHandler(BaseHandler):
    name = "workday"

    def __init__(self, ctx: ApplyContext, accounts: dict[str, dict[str, str]] | None = None):
        super().__init__(ctx)
        self.accounts = accounts or {}
        self._creds: dict[str, str] | None = None

    def open(self, listing: Listing) -> None:
        self.listing = listing
        host = urlparse(listing.url).netloc.lower()
        self._creds = self.accounts.get(host)
        if not self._creds:
            raise NeedsManual(f"Workday requires an account for {host}; no credentials in WORKDAY_ACCOUNTS")
        self.goto(listing.url)
        if not self.click_if_present("a[data-automation-id='adventureButton'], a:has-text('Apply'), button:has-text('Apply')", timeout=8000):
            raise HandlerError("Workday Apply button not found")
        self.click_if_present("a[data-automation-id='applyManually'], a:has-text('Apply Manually'), button:has-text('Apply Manually')", timeout=6000)
        self._sign_in()

    def _sign_in(self) -> None:
        assert self._creds
        page = self.page
        try:
            if page.locator("input[data-automation-id='email']").first.is_visible(timeout=6000):
                # Prefer sign-in if an account exists; the same form serves create-account.
                self.click_if_present("button[data-automation-id='signInLink'], a:has-text('Sign In'), button:has-text('Sign In')", timeout=2000)
                page.locator("input[data-automation-id='email']").first.fill(self._creds["email"])
                page.locator("input[data-automation-id='password']").first.fill(self._creds["password"])
                self.click_if_present("button[data-automation-id='signInSubmitButton'], button[type=submit]", timeout=3000)
                self.settle(2500)
                if page.locator("[data-automation-id='errorMessage']").first.is_visible(timeout=1500):
                    raise NeedsManual("Workday sign-in failed; check WORKDAY_ACCOUNTS credentials")
        except NeedsManual:
            raise
        except Exception as e:  # noqa: BLE001
            log.info("Workday sign-in step skipped: %s", e)

    def fill(self) -> FillResult:
        """Fill page by page until the review page. Each 'Next' advances a step."""
        total = FillResult()
        for step in range(8):
            part = self.fill_generic(f"workday step {step + 1}")
            total.answers.extend(part.answers)
            total.unanswered.extend(part.unanswered)
            total.resume_uploaded = total.resume_uploaded or part.resume_uploaded
            if part.unanswered:
                raise NeedsManual("Required Workday questions could not be answered", part.unanswered)
            if self.page.locator("button:has-text('Submit'), button[data-automation-id='bottom-navigation-next-button']:has-text('Submit')").first.is_visible(timeout=1000):
                break
            if not self.click_if_present("button[data-automation-id='bottom-navigation-next-button'], button:has-text('Save and Continue'), button:has-text('Next')", timeout=4000):
                break
            errors = self.visible_errors()
            if errors:
                raise NeedsManual("Workday validation errors: " + "; ".join(errors[:5]))
        return total

    def submit_selector(self) -> str:
        return "button[data-automation-id='bottom-navigation-next-button']:has-text('Submit'), button:has-text('Submit')"
