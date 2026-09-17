"""Import the legacy flat ``applications`` table (db.py) into the tracker."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .states import ApplicationStatus, EventType
from .store import Tracker

log = logging.getLogger("autoapply.tracker.migrate")

LEGACY_STATUS = {
    "applied": ApplicationStatus.SUBMITTED,
    "failed": ApplicationStatus.FAILED,
    "needs_manual": ApplicationStatus.NEEDS_INPUT,
    "skipped": ApplicationStatus.WITHDRAWN,
    "dry_run": ApplicationStatus.READY_TO_APPLY,
}


def migrate_legacy(tracker: Tracker, legacy_db: Path) -> int:
    """Import every legacy record once. Returns the number of applications imported."""
    if not legacy_db.exists() or tracker.kv_get("legacy_migrated"):
        return 0
    from ..db import Database

    imported = 0
    with Database(legacy_db) as legacy:
        records = sorted(legacy.query(), key=lambda r: r.id or 0)
        for rec in records:
            job = tracker.upsert_job(rec.company, rec.role, rec.url, rec.location, source="simplify", listing_id=rec.listing_id)
            target = LEGACY_STATUS.get(rec.status, ApplicationStatus.UNKNOWN)
            app = tracker.create_application(job["id"], ats=rec.ats)
            app_id = app["id"]
            meta = {"legacy_id": rec.id, "legacy_status": rec.status, "run_id": rec.run_id, "mode": rec.mode}
            if target == ApplicationStatus.SUBMITTED:
                tracker.record_event(app_id, EventType.APPLICATION_STARTED, "Imported from legacy database", meta, timestamp=rec.timestamp)
                tracker.record_event(app_id, EventType.APPLICATION_SUBMITTED, "Application submitted (legacy)", meta, timestamp=rec.timestamp)
            elif target == ApplicationStatus.FAILED:
                tracker.record_event(app_id, EventType.APPLICATION_STARTED, "Imported from legacy database", meta, timestamp=rec.timestamp)
                tracker.record_event(app_id, EventType.APPLICATION_FAILED, rec.error or "failed (legacy)", meta, timestamp=rec.timestamp)
            elif target == ApplicationStatus.NEEDS_INPUT:
                tracker.record_event(app_id, EventType.APPLICATION_STARTED, "Imported from legacy database", meta, timestamp=rec.timestamp)
                tracker.record_event(app_id, EventType.INPUT_REQUESTED, rec.error or "needs your input (legacy)", meta, timestamp=rec.timestamp)
            elif target == ApplicationStatus.WITHDRAWN:
                tracker.record_event(app_id, EventType.APPLICATION_SKIPPED, rec.error or "skipped (legacy)", meta, timestamp=rec.timestamp)
            else:
                tracker.record_event(app_id, EventType.NOTE, f"legacy status {rec.status}", meta, timestamp=rec.timestamp)
                tracker.set_status(app_id, ApplicationStatus.READY_TO_APPLY, "dry run (legacy)")
            tracker.update_application(app_id, error=rec.error or "", screenshot_path=rec.screenshot_path or "")
            for a in rec.answers:
                status = "auto" if a.get("status") == "filled" else ("pending" if a.get("status") in ("needs_user", "draft") else "skipped")
                q = tracker.add_question(app_id, a.get("label", ""), a.get("field_id", ""), a.get("kind", "text"), a.get("category", ""), None, bool(a.get("required")), status, a.get("reason", ""))
                if a.get("value"):
                    tracker.add_answer(q["id"], a["value"], a.get("source", ""), status=status, user_approved=a.get("source") == "user")
            for uq in rec.unanswered_questions:
                if not any(uq.startswith(a.get("label", "\0")) for a in rec.answers):
                    tracker.add_question(app_id, uq, status="pending", reason="legacy unanswered")
            imported += 1
    tracker.kv_set("legacy_migrated", {"count": imported, "from": str(legacy_db)})
    log.info("Imported %d legacy applications", imported)
    return imported
