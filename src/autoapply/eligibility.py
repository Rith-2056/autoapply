"""Job eligibility gate: every check a job must pass before it enters the AutoApply queue.

  US location? -> target role? -> category? -> exclusions? -> sponsorship?
  -> duplicate / already applied? -> ATS supported?

The gate runs on listing data only (no browser), and every rejection carries the
check that failed so the UI can show *why* a job was skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ats import detect_ats
from .listings import Listing, ListingFilters, matches
from .location import classify_location
from .tracker import ApplicationStatus, Tracker

FRESH = {ApplicationStatus.DISCOVERED, ApplicationStatus.READY_TO_APPLY}
RETRYABLE = {ApplicationStatus.FAILED, ApplicationStatus.NEEDS_INPUT, ApplicationStatus.WITHDRAWN}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class GateResult:
    eligible: bool
    checks: list[Check] = field(default_factory=list)
    location_verdict: str = "UNKNOWN"

    @property
    def failed(self) -> str:
        return next((c.name for c in self.checks if not c.passed), "")

    @property
    def reason(self) -> str:
        c = next((c for c in self.checks if not c.passed), None)
        return f"{c.name}: {c.detail}" if c else "eligible"

    def to_dict(self) -> dict[str, Any]:
        return {"eligible": self.eligible, "failed": self.failed, "reason": self.reason, "location_verdict": self.location_verdict,
                "checks": [c.__dict__ for c in self.checks]}


def evaluate(listing: Listing, filters: ListingFilters, tracker: Tracker | None = None, *, us_only: bool = True,
             retry: bool = False, ats_allow: set[str] | None = None, exclude_locations: list[str] | None = None) -> GateResult:
    checks: list[Check] = []
    loc = classify_location(listing.locations or listing.location)
    if us_only:
        checks.append(Check("us_location", loc.eligible, loc.reason))
    else:
        checks.append(Check("us_location", True, f"not enforced ({loc.verdict})"))
    if exclude_locations and listing.locations:
        ex = [x.lower() for x in exclude_locations]
        hit = next((l for l in listing.locations if any(x in l.lower() for x in ex)), None)
        checks.append(Check("excluded_location", hit is None, hit or ""))
    ok, why = matches(listing, filters)
    checks.append(Check("configured_criteria", ok, why))
    if tracker is not None:
        job = tracker.find_duplicate_job(listing.company, listing.title, listing.location, listing.url)
        app = tracker.application_for_job(job["id"]) if job else None
        if app:
            status = ApplicationStatus(app["status"])
            if status in FRESH:
                checks.append(Check("already_applied", True, f"tracked but not applied ({status.value})"))
            elif retry and status in RETRYABLE:
                checks.append(Check("already_applied", True, f"retrying ({status.value})"))
            else:
                checks.append(Check("already_applied", False, f"application exists: {status.value}" + (f" ({job.get('duplicate_reason')})" if job.get("duplicate_reason") else "")))
        else:
            checks.append(Check("already_applied", True, ""))
    if ats_allow:
        ats = detect_ats(listing.url)
        checks.append(Check("ats_supported", ats in ats_allow, ats))
    # Short-circuit order is preserved in ``checks`` so the first failure is the headline reason.
    return GateResult(all(c.passed for c in checks), checks, loc.verdict)
