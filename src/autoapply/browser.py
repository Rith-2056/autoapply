"""Playwright helpers: browser lifecycle, captcha detection, screenshots."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger("autoapply.browser")

CAPTCHA_IFRAME_RE = re.compile(r"recaptcha|hcaptcha|turnstile|arkose|funcaptcha|captcha", re.I)
CAPTCHA_TEXT_RE = re.compile(
    r"verify (that )?you are (a )?human|are you a robot|i'm not a robot|complete the (security )?check|"
    r"checking your browser|access denied|unusual traffic|press and hold",
    re.I,
)


def launch(playwright, headless: bool, profile_dir: Path, timeout_ms: int, executable_path: str | None = None):
    """Launch a persistent Chromium context (cookies survive between runs).

    ``executable_path`` (settings ``run.chromium_executable`` or env
    ``AUTOAPPLY_CHROMIUM``) lets you point at an existing Chrome/Chromium
    instead of the one ``playwright install chromium`` downloads.
    """
    import os

    profile_dir.mkdir(parents=True, exist_ok=True)
    exe = executable_path or os.environ.get("AUTOAPPLY_CHROMIUM") or None
    # Only for environments behind a TLS-intercepting corporate proxy. Never
    # needed on a normal machine; leave unset otherwise.
    ignore_tls = os.environ.get("AUTOAPPLY_IGNORE_TLS_ERRORS", "").lower() in ("1", "true", "yes")
    context = playwright.chromium.launch_persistent_context(
        str(profile_dir),
        headless=headless,
        viewport={"width": 1280, "height": 900},
        accept_downloads=False,
        args=["--disable-blink-features=AutomationControlled"],
        executable_path=exe,
        ignore_https_errors=ignore_tls,
    )
    context.set_default_timeout(timeout_ms)
    context.set_default_navigation_timeout(timeout_ms)
    return context


def captcha_present(page) -> bool:
    """Detect a visible captcha / bot challenge. Never attempts to solve it."""
    try:
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            if CAPTCHA_IFRAME_RE.search(frame.url or ""):
                # Widgets are often present but invisible until submit; only
                # count them when the iframe is actually rendered. The invisible
                # reCAPTCHA badge (bottom-right) needs no user action and is ignored.
                try:
                    el = frame.frame_element()
                    if el.evaluate("e => !!e.closest('.grecaptcha-badge')"):
                        continue
                    if el.is_visible():
                        box = el.bounding_box()
                        if box and box["width"] > 60 and box["height"] > 40:
                            return True
                except Exception:  # noqa: BLE001
                    continue
        for sel in ("div.g-recaptcha:visible", "div.h-captcha:visible",
                    "[data-sitekey]:visible", ".cf-turnstile:visible", "#challenge-form"):
            try:
                if page.locator(sel).first.is_visible(timeout=300):
                    return True
            except Exception:  # noqa: BLE001
                pass
        title = (page.title() or "")
        if CAPTCHA_TEXT_RE.search(title):
            return True
        body = page.locator("body").inner_text(timeout=2000)[:4000]
        if re.search(r"i'm not a robot|verify you are human|are you a robot", body, re.I):
            return True
    except Exception as e:  # noqa: BLE001
        log.debug("captcha check error: %s", e)
    return False


def safe_filename(s: str, max_len: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:max_len].strip("_") or "x"


def screenshot(page, directory: Path, company: str, tag: str) -> str:
    """Full-page screenshot; returns the path as a string ('' on failure)."""
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now():%Y%m%d_%H%M%S}_{safe_filename(company)}_{safe_filename(tag)}.png"
    path = directory / name
    try:
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception as e:  # noqa: BLE001
        log.warning("screenshot failed: %s", e)
        try:
            page.screenshot(path=str(path))
            return str(path)
        except Exception:  # noqa: BLE001
            return ""
