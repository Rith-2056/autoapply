"""Lever (jobs.lever.co). Note: Lever forms commonly show an hCaptcha on submit."""

from __future__ import annotations

from ..listings import Listing
from .base import BaseHandler, FillResult, HandlerError


class LeverHandler(BaseHandler):
    name = "lever"

    def open(self, listing: Listing) -> None:
        self.listing = listing
        url = listing.url.split("?")[0].rstrip("/")
        if not url.endswith("/apply"):
            url = url + "/apply"
        self.goto(url)
        try:
            self.page.locator("form#application-form, form.application-form, .application-form").first.wait_for(timeout=10000)
        except Exception as e:  # noqa: BLE001
            raise HandlerError(f"Lever application form not found: {e}") from e

    def fill(self) -> FillResult:
        return self.fill_generic("lever")

    def submit_selector(self) -> str:
        return "#btn-submit, button:has-text('Submit application'), button[type=submit]"

    def confirmation_patterns(self) -> list[str]:
        return super().confirmation_patterns() + [r"application submitted", r"thanks!? for applying"]
