"""Base handler: generic form discovery + filling shared by every ATS.

The flow is split into ``open`` -> ``fill`` -> ``submit`` so the runner can
insert the review prompt / dry-run stop between filling and submitting.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Profile
from ..fields import (
    choose_consent,
    choose_month,
    choose_numeric_range,
    choose_option,
    clean_label,
    match_rule,
    profile_value,
    should_skip,
)
from ..listings import Listing
from ..llm import QuestionAnswerer

log = logging.getLogger("autoapply.ats")


class NeedsManual(Exception):
    """Raised when the handler cannot proceed automatically."""

    def __init__(self, reason: str, questions: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.questions = questions or []


class HandlerError(Exception):
    """Unrecoverable error while applying (recorded as ``failed``)."""


@dataclass
class FieldAnswer:
    label: str
    value: str
    source: str  # profile | llm | resume | skipped | manual
    kind: str = "text"
    required: bool = False


@dataclass
class FillResult:
    answers: list[FieldAnswer] = field(default_factory=list)
    unanswered: list[str] = field(default_factory=list)  # required questions left blank
    resume_uploaded: bool = False


@dataclass
class ApplyContext:
    page: Any
    profile: Profile
    llm: QuestionAnswerer
    resume_pdf: Path
    timeout_ms: int = 45000


# JavaScript that enumerates visible form controls and tags each with a
# data-aa-id attribute so Python can address them reliably afterwards.
_ENUMERATE_JS = r"""
() => {
  const visible = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    if (el.type === 'hidden') return false;
    return true;
  };
  const text = (el) => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const labelFor = (el) => {
    const al = el.getAttribute('aria-label'); if (al && al.trim()) return al.trim();
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(id => text(document.getElementById(id))).filter(Boolean).join(' '); if (t) return t; }
    if (el.id) { try { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l && text(l)) return text(l); } catch (e) {} }
    const cl = el.closest('label'); if (cl && text(cl)) return text(cl);
    let p = el.parentElement;
    for (let i = 0; i < 5 && p; i++) {
      const l = p.querySelector('label, legend, [class*="label" i], [class*="question" i] > *:first-child, h1, h2, h3, h4, h5, p');
      if (l && !l.contains(el) && text(l)) return text(l);
      p = p.parentElement;
    }
    return el.getAttribute('placeholder') || el.name || '';
  };
  const groupLabel = (el) => {
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg && text(lg)) return text(lg); const al = fs.getAttribute('aria-label'); if (al) return al; }
    const rg = el.closest('[role="radiogroup"], [role="group"]');
    if (rg) { const al = rg.getAttribute('aria-label'); if (al) return al;
      const lb = rg.getAttribute('aria-labelledby'); if (lb) { const t = lb.split(/\s+/).map(id => text(document.getElementById(id))).filter(Boolean).join(' '); if (t) return t; } }
    let p = el.parentElement;
    for (let i = 0; i < 6 && p; i++) {
      const cands = p.querySelectorAll('legend, label, [class*="label" i], h3, h4, h5, p, span');
      for (const c of cands) { if (!c.contains(el) && !c.querySelector('input') && text(c).length > 3) return text(c); }
      p = p.parentElement;
    }
    return '';
  };
  const requiredOf = (el, lbl) => el.required || el.getAttribute('aria-required') === 'true' || /\*/.test(lbl) || /required/i.test(el.closest('[class*="field" i], [class*="question" i], fieldset, div')?.className || '');

  const out = []; let n = 0; const seenGroups = {};
  const controls = document.querySelectorAll('input, textarea, select');
  controls.forEach((el) => {
    const type = (el.tagName === 'SELECT') ? 'select' : (el.tagName === 'TEXTAREA' ? 'textarea' : (el.type || 'text').toLowerCase());
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) return;
    if (type === 'file') { if (!el.dataset.aaId) el.dataset.aaId = 'aa' + (n++);
      out.push({id: el.dataset.aaId, type, name: el.name || '', label: labelFor(el), required: requiredOf(el, labelFor(el)), accept: el.accept || '', visible: visible(el)}); return; }
    if (!visible(el)) return;
    if (type === 'radio' || type === 'checkbox') {
      const key = el.name || (el.closest('fieldset, [role="radiogroup"], [role="group"]') ? groupLabel(el) : ('id:' + (el.id || Math.random())));
      const optLabel = labelFor(el);
      if (!el.dataset.aaId) el.dataset.aaId = 'aa' + (n++);
      if (type === 'radio' || (el.closest('fieldset, [role="group"]') && el.closest('fieldset, [role="group"]').querySelectorAll('input[type=checkbox]').length > 1)) {
        if (!seenGroups[key]) { seenGroups[key] = {id: el.dataset.aaId, type: type === 'radio' ? 'radio' : 'checkbox_group', name: el.name || '', label: groupLabel(el) || optLabel, required: requiredOf(el, groupLabel(el)), options: [], optionIds: []}; out.push(seenGroups[key]); }
        seenGroups[key].options.push(optLabel); seenGroups[key].optionIds.push(el.dataset.aaId);
        seenGroups[key].required = seenGroups[key].required || requiredOf(el, '');
      } else {
        out.push({id: el.dataset.aaId, type: 'checkbox', name: el.name || '', label: optLabel, required: requiredOf(el, optLabel), checked: el.checked});
      }
      return;
    }
    if (!el.dataset.aaId) el.dataset.aaId = 'aa' + (n++);
    const lbl = labelFor(el);
    const item = {id: el.dataset.aaId, type, name: el.name || '', label: lbl, required: requiredOf(el, lbl), value: el.value || '', maxlength: el.maxLength > 0 ? el.maxLength : null,
      combobox: el.getAttribute('role') === 'combobox' || !!el.getAttribute('aria-autocomplete') || /select__input|autocomplete/i.test(el.className) || !!el.closest('[class*="select__control" i], [class*="combobox" i]')};
    if (type === 'select') item.options = Array.from(el.options).map(o => o.text.trim()).filter(t => t && !/^(select|choose|please select|--)/i.test(t));
    out.push(item);
  });
  // Custom (non-native) comboboxes with no inner input, e.g. div[role=combobox] buttons
  document.querySelectorAll('[role="combobox"]:not(input):not(select), button[aria-haspopup="listbox"]').forEach((el) => {
    if (!visible(el) || el.querySelector('input')) return;
    if (!el.dataset.aaId) el.dataset.aaId = 'aa' + (n++);
    const lbl = labelFor(el);
    out.push({id: el.dataset.aaId, type: 'custom_select', name: '', label: lbl, required: requiredOf(el, lbl), value: text(el), combobox: true});
  });
  return out;
}
"""


class BaseHandler:
    name = "base"

    def __init__(self, ctx: ApplyContext):
        self.ctx = ctx
        self.page = ctx.page
        self.profile = ctx.profile
        self.llm = ctx.llm
        self.listing: Listing | None = None

    # ------------------------------------------------------------------ #
    # Steps overridden by concrete handlers
    # ------------------------------------------------------------------ #

    def open(self, listing: Listing) -> None:
        """Navigate to the application form."""
        self.listing = listing
        self.goto(listing.url)

    def fill(self) -> FillResult:
        return self.fill_generic()

    def submit_selector(self) -> str:
        return (
            "button[type=submit], input[type=submit], "
            "button:has-text('Submit application'), button:has-text('Submit Application'), "
            "button:has-text('Submit'), button:has-text('Apply')"
        )

    def confirmation_patterns(self) -> list[str]:
        return [
            r"thank you for (applying|your application|your interest|submitting)",
            r"application (has been )?(submitted|received|complete)",
            r"we('ve| have) received your application",
            r"successfully (submitted|applied)",
            r"your application was sent",
        ]

    def submit(self) -> bool:
        """Click submit and wait for a confirmation. True on confirmed success."""
        btn = self.page.locator(self.submit_selector()).first
        btn.scroll_into_view_if_needed()
        btn.click()
        return self.wait_for_confirmation()

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #

    def goto(self, url: str) -> None:
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001
            raise HandlerError(f"navigation failed: {e}") from e
        self.settle()

    def settle(self, ms: int = 1500) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        self.page.wait_for_timeout(ms)

    def click_if_present(self, selector: str, timeout: int = 4000) -> bool:
        try:
            loc = self.page.locator(selector).first
            if loc.is_visible(timeout=timeout):
                loc.click()
                self.settle()
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def wait_for_confirmation(self, timeout_s: int = 25) -> bool:
        pats = [re.compile(p, re.I) for p in self.confirmation_patterns()]
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                body = self.page.locator("body").inner_text(timeout=3000)
            except Exception:  # noqa: BLE001
                body = ""
            if any(p.search(body) for p in pats) or any(p.search(self.page.url) for p in [re.compile(r"confirmation|thank", re.I)]):
                return True
            self.page.wait_for_timeout(1000)
        return False

    def visible_errors(self) -> list[str]:
        """Collect visible validation error messages after a failed submit."""
        out: list[str] = []
        for sel in ("[class*='error' i]:visible", "[role=alert]:visible", "[aria-invalid=true]"):
            try:
                for el in self.page.locator(sel).all()[:10]:
                    t = el.inner_text(timeout=500).strip()
                    if t and len(t) < 200 and t not in out:
                        out.append(t)
            except Exception:  # noqa: BLE001
                pass
        return out

    def enumerate_fields(self) -> list[dict[str, Any]]:
        return self.page.evaluate(_ENUMERATE_JS)

    def loc(self, aa_id: str):
        return self.page.locator(f'[data-aa-id="{aa_id}"]').first

    # ------------------------------------------------------------------ #
    # Generic filling
    # ------------------------------------------------------------------ #

    def fill_generic(self, scope_note: str = "") -> FillResult:
        result = FillResult()
        fields = self.enumerate_fields()
        log.info("Found %d form controls%s", len(fields), f" ({scope_note})" if scope_note else "")
        for f in fields:
            try:
                self._fill_one(f, result)
            except NeedsManual:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Field %r: %s", f.get("label"), e)
                if f.get("required"):
                    result.unanswered.append(f"{clean_label(f.get('label', ''))} (error: {e})")
        return result

    # -- individual field ------------------------------------------------ #

    def _fill_one(self, f: dict[str, Any], result: FillResult) -> None:
        ftype = f["type"]
        label = clean_label(f.get("label", "")) or f.get("name", "")
        required = bool(f.get("required"))

        if ftype == "file":
            self._handle_file(f, label, required, result)
            return
        if ftype == "checkbox":
            self._handle_single_checkbox(f, label, required, result)
            return
        if should_skip(label):
            if required:
                result.unanswered.append(label)
            result.answers.append(FieldAnswer(label, "", "skipped", ftype, required))
            return

        options: list[str] | None = f.get("options")
        if ftype in ("radio", "checkbox_group"):
            self._handle_group(f, label, required, result)
            return
        if ftype == "select":
            self._handle_select(f, label, required, options or [], result)
            return
        if ftype == "custom_select" or f.get("combobox"):
            self._handle_combobox(f, label, required, result)
            return
        self._handle_text(f, label, required, result)

    def _handle_file(self, f: dict[str, Any], label: str, required: bool, result: FillResult) -> None:
        if re.search(r"cover", label, re.I) or re.search(r"cover", f.get("name", ""), re.I):
            result.answers.append(FieldAnswer(label or "Cover letter", "", "skipped", "file", required))
            if required:
                result.unanswered.append(f"{label or 'Cover letter'} (file upload)")
            return
        if result.resume_uploaded and not re.search(r"resume|cv", label + f.get("name", ""), re.I):
            return
        self.loc(f["id"]).set_input_files(str(self.ctx.resume_pdf))
        self.page.wait_for_timeout(1500)
        result.resume_uploaded = True
        result.answers.append(FieldAnswer(label or "Resume", self.ctx.resume_pdf.name, "resume", "file", required))

    def _handle_single_checkbox(self, f: dict[str, Any], label: str, required: bool, result: FillResult) -> None:
        # Consent / acknowledgement boxes are checked when required; marketing opt-ins are left alone.
        if re.search(r"agree|consent|acknowledge|certify|confirm|accept|privacy|terms|accurate|true", label, re.I):
            if required and not f.get("checked"):
                self.loc(f["id"]).check()
                result.answers.append(FieldAnswer(label, "checked", "profile", "checkbox", required))
            return
        if required and not f.get("checked"):
            result.unanswered.append(label)
            result.answers.append(FieldAnswer(label, "", "skipped", "checkbox", required))

    def _handle_group(self, f: dict[str, Any], label: str, required: bool, result: FillResult) -> None:
        options = [clean_label(o) for o in f.get("options", [])]
        value, source = self._decide(label, options, required, kind_hint="group")
        if value is None:
            if required:
                result.unanswered.append(label)
            result.answers.append(FieldAnswer(label, "", "skipped", f["type"], required))
            return
        idx = options.index(value)
        self.loc(f["optionIds"][idx]).check(force=True)
        result.answers.append(FieldAnswer(label, value, source, f["type"], required))

    def _handle_select(self, f: dict[str, Any], label: str, required: bool, options: list[str], result: FillResult) -> None:
        value, source = self._decide(label, options, required, kind_hint="select")
        if value is None:
            if required:
                result.unanswered.append(label)
            result.answers.append(FieldAnswer(label, "", "skipped", "select", required))
            return
        self.loc(f["id"]).select_option(label=value)
        result.answers.append(FieldAnswer(label, value, source, "select", required))

    OPTION_SELECTOR = (
        "[role=option]:visible, [role=listbox] li:visible, [role=listbox] [role=treeitem]:visible, "
        "[class*='select__option']:visible, [class*='__option']:visible, [class*='menu'] [class*='option']:visible, "
        "ul[class*='autocomplete'] li:visible, [class*='dropdown'] li:visible"
    )

    def _open_combobox(self, el) -> None:
        """Open a custom dropdown. react-select opens on mousedown on its control."""
        el.scroll_into_view_if_needed()
        try:
            control = el.locator("xpath=ancestor::*[contains(@class,'control') or contains(@class,'select') or @role='combobox'][1]")
            if control.count():
                control.first.click()
            else:
                el.click()
        except Exception:  # noqa: BLE001
            el.click()
        self.page.wait_for_timeout(500)
        if not self._menu_open():
            try:
                el.focus()
                self.page.keyboard.press("ArrowDown")
                self.page.wait_for_timeout(400)
            except Exception:  # noqa: BLE001
                pass

    def _wait_options_loaded(self, timeout_ms: int = 6000) -> None:
        """Wait until the dropdown shows real options (not a 'Loading...' row)."""
        waited = 0
        while waited < timeout_ms:
            self.page.wait_for_timeout(400)
            waited += 400
            try:
                opts = self.page.locator(self.OPTION_SELECTOR).all()
                if opts:
                    texts = [o.inner_text(timeout=200).strip().lower() for o in opts[:3]]
                    if not any(t.startswith("loading") for t in texts):
                        return
            except Exception:  # noqa: BLE001
                pass

    def _menu_open(self) -> bool:
        try:
            return self.page.locator(self.OPTION_SELECTOR).count() > 0
        except Exception:  # noqa: BLE001
            return False

    def _handle_combobox(self, f: dict[str, Any], label: str, required: bool, result: FillResult) -> None:
        el = self.loc(f["id"])
        self._open_combobox(el)
        options = self._read_open_options()
        value, source = self._decide(label, options or None, required, kind_hint="select")
        if value is None:
            self.page.keyboard.press("Escape")
            if required:
                result.unanswered.append(label)
            result.answers.append(FieldAnswer(label, "", "skipped", "combobox", required))
            return
        picked = self._pick_open_option(value)
        if not picked:
            # Type to filter (typing, not fill, so the widget's key handlers fire), then pick.
            try:
                el.click()
            except Exception:  # noqa: BLE001
                el.focus()
            self.page.keyboard.type(value[:60], delay=20)
            # Async lists (schools, locations) can take a moment to load.
            self._wait_options_loaded()
            picked = self._pick_open_option(value) or self._pick_open_option(value, loose=True)
            if not picked and len(value.split()) > 2:
                # Long names (schools) often match better on a shorter prefix.
                try:
                    el.fill("")
                except Exception:  # noqa: BLE001
                    pass
                self.page.keyboard.type(" ".join(value.split()[:3]), delay=20)
                self._wait_options_loaded()
                picked = self._pick_open_option(value) or self._pick_open_option(value, loose=True)
            if not picked:
                # react-select and most autocompletes select the highlighted (first) match on Enter.
                self.page.keyboard.press("Enter")
                self.page.wait_for_timeout(500)
                picked = self._combobox_shows(el, value)
        else:
            picked = self._combobox_shows(el, value) or picked
        if not picked:
            self.page.keyboard.press("Escape")
            log.warning("Could not select %r for %r", value, label)
            if required:
                result.unanswered.append(f"{label} (could not select '{value}')")
            result.answers.append(FieldAnswer(label, "", "skipped", "combobox", required))
            return
        result.answers.append(FieldAnswer(label, value, source, "combobox", required))

    def _combobox_shows(self, el, value: str) -> bool:
        """True if the widget now displays ``value`` (or its first words) as selected."""
        try:
            control = el.locator("xpath=ancestor::*[contains(@class,'control') or contains(@class,'select__') or contains(@class,'combobox')][1]")
            text = ""
            if control.count():
                text = control.first.inner_text(timeout=500) or ""
            if not text.strip():
                text = el.input_value(timeout=500) or ""
            head = " ".join(value.split()[:2]).lower()
            if head in text.lower() and not text.strip().lower().startswith("select"):
                return True
            # Native-ish widgets expose the choice on the input itself.
            v = (el.get_attribute("value") or "").lower()
            return bool(v) and head in v
        except Exception:  # noqa: BLE001
            return False

    def _read_open_options(self) -> list[str]:
        try:
            opts = self.page.locator(self.OPTION_SELECTOR).all()
            texts = [clean_label(o.inner_text(timeout=300)) for o in opts[:400]]
            return [t for t in texts if t and not re.match(r"^(no options|loading|type to search)", t, re.I)]
        except Exception:  # noqa: BLE001
            return []

    def _pick_open_option(self, value: str, loose: bool = False) -> bool:
        try:
            opts = self.page.locator(self.OPTION_SELECTOR).all()
            for o in opts[:400]:
                t = clean_label(o.inner_text(timeout=300))
                if t.lower() == value.lower() or (loose and (value.lower() in t.lower() or t.lower() in value.lower()) and len(t) > 3):
                    o.click()
                    self.page.wait_for_timeout(400)
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _handle_text(self, f: dict[str, Any], label: str, required: bool, result: FillResult) -> None:
        if f.get("value") and f["type"] != "textarea":
            # Already prefilled (e.g. autofill); leave it.
            result.answers.append(FieldAnswer(label, f["value"], "prefilled", f["type"], required))
            return
        value, source = self._decide(label, None, required, kind_hint=f["type"], maxlength=f.get("maxlength"))
        if value is None:
            if required:
                result.unanswered.append(label)
            result.answers.append(FieldAnswer(label, "", "skipped", f["type"], required))
            return
        el = self.loc(f["id"])
        el.fill(value)
        # Autocomplete inputs (e.g. location) show suggestions; accept the first one.
        if f.get("combobox"):
            self.page.wait_for_timeout(800)
            self._pick_open_option(value, loose=True)
        try:
            actual = el.input_value(timeout=1000)
        except Exception:  # noqa: BLE001
            actual = value
        if actual.strip() != value.strip() and not f.get("combobox"):
            log.warning("Field %r did not keep value %r (now %r)", label, value, actual)
            if required:
                result.unanswered.append(f"{label} (value did not stick)")
            result.answers.append(FieldAnswer(label, actual, "skipped", f["type"], required))
            return
        result.answers.append(FieldAnswer(label, value, source, f["type"], required))

    # -- decide a value for a label ------------------------------------ #

    def _decide(
        self,
        label: str,
        options: list[str] | None,
        required: bool,
        kind_hint: str = "text",
        maxlength: int | None = None,
    ) -> tuple[str | None, str]:
        """Return (value, source). value None means leave blank."""
        rule = match_rule(label)
        if rule:
            pv = profile_value(self.profile, rule)
            if pv:
                if options:
                    if rule.kind == "date_month":
                        chosen = choose_month(options, pv)
                    elif rule.kind == "date_year":
                        chosen = next((o for o in options if pv in o), None)
                    elif rule.kind == "consent":
                        chosen = choose_consent(options)
                    else:
                        chosen = choose_option(options, pv, rule.kind)
                        if not chosen and rule.path == "education.gpa":
                            chosen = choose_numeric_range(options, pv)
                    if chosen:
                        return chosen, "profile"
                else:
                    if rule.kind == "yesno" and kind_hint == "text":
                        return pv, "profile"
                    return pv, "profile"
            elif rule.path.startswith(("eeo.", "work_authorization.")) and options:
                # Unfilled EEO answer: prefer a decline option rather than guessing.
                decline = choose_option(options, "decline", "select") or choose_option(options, "prefer not", "select")
                if decline and not required:
                    return None, "skipped"
                if decline:
                    return decline, "profile"
            # Known field but profile has no usable value -> never guess.
            if required:
                return None, "skipped"
            return None, "skipped"

        # Unknown question: only ask the LLM when it is required, or when it's a
        # free-text question worth answering (optional selects are left blank).
        if not required and options:
            return None, "skipped"
        if not self.llm.enabled:
            return None, "skipped"
        ans = self.llm.answer(
            label,
            options=options,
            company=self.listing.company if self.listing else "",
            role=self.listing.title if self.listing else "",
            max_length=maxlength,
        )
        if ans.acceptable(self.llm.min_confidence):
            return ans.answer, "llm"
        return None, "skipped"
