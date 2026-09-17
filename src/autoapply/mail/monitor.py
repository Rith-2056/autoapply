"""Email monitor: poll Gmail, classify, match, and turn emails into tracker events."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..tracker import ApplicationStatus, EventType, Tracker
from .classify import Classification, EmailClassifier, EmailMessage
from .deadlines import humanize_deadline
from .gmail import DEFAULT_QUERY, GmailClient
from .match import match_email

log = logging.getLogger("autoapply.mail.monitor")

CATEGORY_EVENT = {
    "APPLICATION_CONFIRMATION": EventType.CONFIRMATION_EMAIL_RECEIVED,
    "ASSESSMENT": EventType.ASSESSMENT_RECEIVED,
    "INTERVIEW": EventType.INTERVIEW_INVITATION_RECEIVED,
    "INTERVIEW_SCHEDULING": EventType.INTERVIEW_INVITATION_RECEIVED,
    "REJECTION": EventType.REJECTED,
    "RECRUITER_CONTACT": EventType.RECRUITER_CONTACT,
    "ADDITIONAL_INFORMATION": EventType.ADDITIONAL_INFO_REQUESTED,
    "STATUS_UPDATE": EventType.STATUS_UPDATE,
    "DEADLINE": EventType.DEADLINE_DETECTED,
    "OFFER": EventType.OFFER_RECEIVED,
}
NOTIFY_CATEGORIES = {"ASSESSMENT", "INTERVIEW", "INTERVIEW_SCHEDULING", "OFFER", "REJECTION", "RECRUITER_CONTACT", "ADDITIONAL_INFORMATION"}
ACTION_TYPES = {"ASSESSMENT": "assessment", "INTERVIEW_SCHEDULING": "schedule_interview", "INTERVIEW": "interview",
                "ADDITIONAL_INFORMATION": "provide_info", "OFFER": "offer"}


class EmailProcessor:
    """Pure processing (no network): classify -> match -> record. Testable with fake messages."""

    def __init__(self, tracker: Tracker, classifier: EmailClassifier, on_event: Callable[[dict[str, Any]], None] | None = None):
        self.tracker = tracker
        self.classifier = classifier
        self.on_event = on_event or (lambda e: None)

    def process(self, msg: EmailMessage, llm_result: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if self.tracker.has_email(msg.id):
            return None
        cls = self.classifier.classify(msg, llm_result)
        if not cls.job_related or cls.category == "IRRELEVANT":
            return None  # never stored
        apps = self.tracker.list_applications(limit=2000)
        m = match_email(cls.company, cls.role, msg.sender, msg.subject, msg.body, msg.urls, apps, msg.received_at)
        record = self.tracker.add_email_event(
            msg.id, cls.category, msg.subject, msg.sender, msg.received_at.isoformat(timespec="seconds"), cls.to_dict(),
            m.application_id, m.confidence, m.status, [c.to_dict() for c in m.candidates], snippet=msg.snippet,
        )
        if m.status == "auto" and m.application_id:
            self.apply_to_application(record["id"], m.application_id, cls, msg)
        elif m.status == "needs_confirmation":
            top = m.candidates[0]
            self.tracker.notify("email_match", f"Possible match: {top.company} — {cls.category.replace('_', ' ').title()}",
                                f"An email looks related to {top.company} ({top.title}) but confidence is low. Confirm in Settings → Email.", top.application_id)
            self.on_event({"kind": "email", "message": f"Email needs confirmation: {msg.subject}", "email_id": record["id"]})
        else:
            self.on_event({"kind": "email", "message": f"Unmatched job email: {msg.subject}", "email_id": record["id"]})
        return record

    def apply_to_application(self, email_id: int, app_id: int, cls: Classification, msg: EmailMessage) -> None:
        """Record the event on the application, create actions/notifications."""
        event = CATEGORY_EVENT.get(cls.category)
        app = self.tracker.get_application(app_id)
        if not app:
            return
        label = f"{app['company']} — {app['title']}"
        if event:
            ev = self.tracker.record_event(app_id, event, cls.summary or msg.subject,
                                           {"email_id": email_id, "category": cls.category, "platform": cls.platform, "deadline": cls.deadline.isoformat(timespec="minutes") if cls.deadline else None},
                                           enforce=False, timestamp=msg.received_at.isoformat(timespec="seconds"))
            self.on_event({"kind": "application_event", **ev})
            if ev.get("status_changed"):
                self.tracker.notify("status", f"{label}: {ApplicationStatus(ev['status']).value.replace('_', ' ').title()}", cls.summary or msg.subject, app_id)
        if cls.deadline:
            self.tracker.record_event(app_id, EventType.DEADLINE_DETECTED, f"Deadline detected: {cls.deadline:%b %d, %Y %H:%M}",
                                      {"deadline": cls.deadline.isoformat(timespec="minutes"), "text": cls.deadline_text, "email_id": email_id}, enforce=False)
        if cls.action_required and cls.category in ACTION_TYPES:
            deadline = cls.deadline.isoformat(timespec="minutes") if cls.deadline else None
            link = next((u for u in msg.urls if any(p in u for p in ("hackerrank", "codesignal", "codility", "karat", "hirevue", "calendly", "goodtime", "assessment", "schedule", "interview"))), "")
            self.tracker.add_action(app_id, ACTION_TYPES[cls.category], cls.action_title or cls.category.title(), cls.summary, deadline, cls.priority, link, email_id)
            self.tracker.record_event(app_id, EventType.NOTE, f"Action created: {cls.action_title}", {"email_id": email_id}, enforce=False)
        if cls.category in NOTIFY_CATEGORIES:
            title = {"ASSESSMENT": "New assessment", "INTERVIEW": "Interview invitation", "INTERVIEW_SCHEDULING": "Interview scheduling request",
                     "OFFER": "Offer received", "REJECTION": "Rejection", "RECRUITER_CONTACT": "Recruiter responded", "ADDITIONAL_INFORMATION": "Information requested"}[cls.category]
            when = f" ({humanize_deadline(cls.deadline)})" if cls.deadline else ""
            n = self.tracker.notify("email", f"{title} from {app['company']}{when}", cls.summary or msg.subject, app_id)
            self.on_event({"kind": "notification", **n})

    def confirm_match(self, email_id: int, app_id: int | None) -> None:
        rec = self.tracker.get_email_event(email_id)
        if not rec:
            return
        if app_id is None:
            self.tracker.link_email(email_id, None, "ignored")
            return
        self.tracker.link_email(email_id, app_id, "confirmed")
        ex = rec.get("extracted") or {}
        from datetime import datetime

        cls = Classification(ex.get("category", rec["category"]), float(ex.get("confidence", 1.0)), ex.get("company", ""), ex.get("role", ""), ex.get("platform", ""),
                             bool(ex.get("action_required")), ex.get("action_title", ""), ex.get("deadline_text", ""),
                             datetime.fromisoformat(ex["deadline"]) if ex.get("deadline") else None, ex.get("priority", "medium"), ex.get("summary", ""))
        msg = EmailMessage(rec["provider_message_id"], rec["subject"], rec["sender"], rec.get("snippet", ""), datetime.fromisoformat(rec["received_at"]) if rec.get("received_at") else datetime.now())
        self.apply_to_application(email_id, app_id, cls, msg)


class EmailMonitor:
    """Background poller. Only runs when an account is connected."""

    def __init__(self, tracker: Tracker, processor: EmailProcessor, project_root: Path, credentials_file: Path,
                 interval_seconds: int = 300, query: str = DEFAULT_QUERY, on_event: Callable[[dict[str, Any]], None] | None = None):
        self.tracker = tracker
        self.processor = processor
        self.project_root = project_root
        self.credentials_file = credentials_file
        self.interval = interval_seconds
        self.query = query
        self.on_event = on_event or (lambda e: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""
        self.syncing = False

    def client(self) -> GmailClient:
        return GmailClient(self.project_root, self.credentials_file, self.tracker.email_account_token())

    def connect(self) -> str:
        client = GmailClient(self.project_root, self.credentials_file)
        token, email = client.connect_interactive()
        self.tracker.set_email_account("gmail", email, token)
        self.on_event({"kind": "email_status", "connected": True, "email": email})
        return email

    def disconnect(self) -> None:
        self.tracker.clear_email_account()
        self.on_event({"kind": "email_status", "connected": False})

    def sync_once(self, max_results: int = 100) -> dict[str, Any]:
        if not self.tracker.email_account():
            return {"processed": 0, "skipped": 0, "error": "not connected"}
        self.syncing = True
        processed = skipped = 0
        try:
            client = self.client()
            for mid in client.list_message_ids(self.query, max_results):
                if self._stop.is_set():
                    break
                if self.tracker.has_email(mid):
                    skipped += 1
                    continue
                try:
                    msg = client.fetch(mid)
                except Exception as e:  # noqa: BLE001
                    log.warning("fetch %s failed: %s", mid, e)
                    continue
                if self.processor.process(msg):
                    processed += 1
                else:
                    skipped += 1
            if client.token_enc:
                self.tracker.update_email_token(client.token_enc)
            self.tracker.mark_email_sync()
            self.last_error = ""
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)[:300]
            log.warning("email sync failed: %s", e)
        finally:
            self.syncing = False
        self.on_event({"kind": "email_sync", "processed": processed, "skipped": skipped, "error": self.last_error})
        return {"processed": processed, "skipped": skipped, "error": self.last_error}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="email-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.tracker.email_account():
                self.sync_once()
            for _ in range(int(self.interval)):
                if self._stop.is_set():
                    return
                time.sleep(1)
