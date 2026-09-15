"""Greenhouse job boards (job-boards.greenhouse.io, boards.greenhouse.io, embedded ?gh_jid=)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from ..listings import Listing
from .base import BaseHandler, FillResult, HandlerError


class GreenhouseHandler(BaseHandler):
    name = "greenhouse"

    def open(self, listing: Listing) -> None:
        self.listing = listing
        url = listing.url
        parsed = urlparse(url)
        # Embedded boards: company.com/careers?gh_jid=123 -> we still navigate to the
        # company page; the Greenhouse form is usually rendered inline or in an iframe.
        self.goto(url)
        if "greenhouse.io" in parsed.netloc:
            # New-style boards render the form on the job page. Old boards have a
            # separate #app form reachable with the "Apply" button.
            if not self._form_visible():
                self.click_if_present("a:has-text('Apply'), button:has-text('Apply'), #apply_button, a.apply-button")
        else:
            # Embedded: try an "Apply" link that leads to greenhouse
            if not self._form_visible():
                self.click_if_present("a[href*='greenhouse'], a:has-text('Apply'), button:has-text('Apply')")
        if not self._form_visible():
            gh_jid = parse_qs(parsed.query).get("gh_jid", [None])[0]
            if gh_jid:
                raise HandlerError("Greenhouse form not found on embedded page (gh_jid present)")
            raise HandlerError("Greenhouse application form not found")
        self._switch_to_form_frame()

    def _form_visible(self) -> bool:
        try:
            return (
                self.page.locator("#application-form, form#application_form, form[action*='greenhouse'], #application_form, #grnhse_iframe").first.is_visible(timeout=5000)
            )
        except Exception:  # noqa: BLE001
            return False

    def _switch_to_form_frame(self) -> None:
        """Embedded boards render the form in an iframe; operate on that frame."""
        try:
            frame_el = self.page.locator("#grnhse_iframe, iframe[src*='greenhouse']").first
            if frame_el.is_visible(timeout=2000):
                frame = frame_el.content_frame()
                if frame is not None:
                    self.page = _FramePage(self.ctx.page, frame)
        except Exception:  # noqa: BLE001
            pass

    def fill(self) -> FillResult:
        result = self.fill_generic("greenhouse")
        return result

    def submit_selector(self) -> str:
        return "button:has-text('Submit application'), button:has-text('Submit Application'), #submit_app, button[type=submit]"

    def confirmation_patterns(self) -> list[str]:
        return super().confirmation_patterns() + [r"application submitted", r"thanks for applying"]


class _FramePage:
    """Proxy that routes locator/evaluate calls to a frame but keeps page-level APIs."""

    def __init__(self, page, frame):
        self._page = page
        self._frame = frame

    def locator(self, *a, **k):
        return self._frame.locator(*a, **k)

    def evaluate(self, *a, **k):
        return self._frame.evaluate(*a, **k)

    @property
    def keyboard(self):
        return self._page.keyboard

    @property
    def url(self):
        return self._frame.url

    @property
    def frames(self):
        return self._page.frames

    @property
    def main_frame(self):
        return self._page.main_frame

    def __getattr__(self, name):
        return getattr(self._page, name)


_ = re  # keep import for potential subclass use
