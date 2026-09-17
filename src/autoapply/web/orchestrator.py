"""AutoApply sessions for the web app.

A session runs the existing ``Runner`` in a worker thread. ``WebInteraction``
implements the runner's ``Interaction`` interface by blocking the worker until
the browser UI sends a command (answer / skip / continue / submit / captcha
solved). Playwright objects stay on the worker thread; the API only enqueues
commands.
"""

from __future__ import annotations

import logging
import queue
import threading
import traceback
from dataclasses import asdict
from typing import Any

from ..ats.base import FieldAnswer, FillResult
from ..browser import captcha_present
from ..config import Profile, Secrets, Settings
from ..interaction import SessionCancelled
from ..runner import RunOptions, Runner
from ..tracker import EventType, Tracker
from .events import EventBus

log = logging.getLogger("autoapply.web.orchestrator")


class SessionState:
    def __init__(self, session_id: int):
        self.id = session_id
        self.pause = threading.Event()
        self.cancel = threading.Event()
        self.commands: queue.Queue = queue.Queue()
        self.waiting: dict[str, Any] | None = None  # what the worker is blocked on
        self.current_app_id: int | None = None
        self.thread: threading.Thread | None = None
        self.error = ""


class WebInteraction:
    def __init__(self, state: SessionState, tracker: Tracker, bus: EventBus):
        self.state = state
        self.tracker = tracker
        self.bus = bus

    # -- helpers ----------------------------------------------------------- #

    def _set_waiting(self, payload: dict[str, Any] | None) -> None:
        self.state.waiting = payload
        status = "WaitingForUser" if payload else ("Paused" if self.state.pause.is_set() else "Running")
        self.tracker.update_session(self.state.id, status=status, current_application_id=self.state.current_app_id)
        self.bus.publish({"kind": "session", "session_id": self.state.id, "status": status, "waiting": payload, "current_application_id": self.state.current_app_id})

    def _next_command(self) -> dict[str, Any]:
        while True:
            self.checkpoint()
            try:
                return self.state.commands.get(timeout=0.5)
            except queue.Empty:
                continue

    def checkpoint(self) -> None:
        if self.state.cancel.is_set():
            raise SessionCancelled()
        if self.state.pause.is_set():
            self.tracker.update_session(self.state.id, status="Paused")
            self.bus.publish({"kind": "session", "session_id": self.state.id, "status": "Paused"})
            while self.state.pause.is_set():
                if self.state.cancel.is_set():
                    raise SessionCancelled()
                self.state.pause.wait(0.5)
            self.tracker.update_session(self.state.id, status="WaitingForUser" if self.state.waiting else "Running")
            self.bus.publish({"kind": "session", "session_id": self.state.id, "status": "Running"})

    def notice(self, message: str, app_id: int | None = None) -> None:
        self.bus.publish({"kind": "notice", "message": message, "application_id": app_id})

    # -- questions --------------------------------------------------------- #

    def _question_rows(self, app_id: int, fill: FillResult) -> dict[str, dict[str, Any]]:
        rows = {q["field_id"]: q for q in self.tracker.questions(app_id) if q.get("field_id")}
        return {p.field_id: rows.get(p.field_id, {}) for p in fill.pending}

    def _pending_payload(self, app_id: int, fill: FillResult) -> dict[str, Any]:
        rows = self._question_rows(app_id, fill)
        app = self.tracker.get_application(app_id) or {}
        return {
            "type": "questions",
            "application_id": app_id,
            "company": app.get("company", ""),
            "title": app.get("title", ""),
            "questions": [
                {"field_id": p.field_id, "question_id": rows.get(p.field_id, {}).get("id"), "label": p.label, "kind": p.kind,
                 "options": p.options, "required": p.required, "category": p.category, "reason": p.reason, "draft": p.draft}
                for p in fill.pending
            ],
            "answers": [asdict(a) for a in fill.answers],
        }

    def resolve_pending(self, handler: Any, fill: FillResult, app_id: int) -> None:
        self.state.current_app_id = app_id
        self._set_waiting(self._pending_payload(app_id, fill))
        try:
            while fill.pending:
                cmd = self._next_command()
                ctype = cmd.get("type")
                if ctype == "answer":
                    self._answer(handler, fill, app_id, cmd)
                elif ctype == "skip_question":
                    fid = cmd.get("field_id")
                    fill.pending = [p for p in fill.pending if p.field_id != fid]
                    for a in fill.answers:
                        if a.field_id == fid and a.status in ("needs_user", "draft"):
                            if a.status == "draft":
                                handler.apply_answer(fid, "")
                                a.value = ""
                            a.status = "needs_user"
                            a.reason = "skipped by you"
                    rows = self._question_rows(app_id, fill)
                    q = self.tracker.questions(app_id)
                    for row in q:
                        if row.get("field_id") == fid:
                            self.tracker.set_question_status(row["id"], "skipped")
                    self._set_waiting(self._pending_payload(app_id, fill))
                elif ctype in ("continue", "edit_done"):
                    if ctype == "edit_done":
                        fill.pending = []
                        fill.answers.append(FieldAnswer("(edited in browser)", "see browser", "user", status="filled", reason="edited by you"))
                    break
                elif ctype in ("skip", "cancel_application"):
                    # Leave the questions unanswered; the review step will record 'manual'.
                    self.state.commands.put({"type": "manual"})
                    break
                else:
                    log.info("ignoring command %s while resolving questions", ctype)
        finally:
            fill.unanswered = [p.label for p in fill.pending if p.required]
            self._set_waiting(None)

    def _answer(self, handler: Any, fill: FillResult, app_id: int, cmd: dict[str, Any]) -> None:
        fid = cmd.get("field_id", "")
        value = str(cmd.get("value", "")).strip()
        pending = next((p for p in fill.pending if p.field_id == fid), None)
        if pending is None:
            self.bus.publish({"kind": "answer_result", "application_id": app_id, "field_id": fid, "ok": False, "message": "question no longer pending"})
            return
        ok, msg = handler.apply_answer(fid, value) if value else (True, "cleared")
        rows = self._question_rows(app_id, fill)
        qrow = rows.get(fid) or {}
        if ok and value:
            fill.pending = [p for p in fill.pending if p.field_id != fid]
            for a in fill.answers:
                if a.field_id == fid:
                    a.value, a.source, a.status, a.reason = value, ("draft" if cmd.get("source") == "draft" else "user"), "filled", f"answered by you ({cmd.get('source', 'typed')})"
            if qrow.get("id"):
                self.tracker.add_answer(qrow["id"], value, cmd.get("source", "typed"), raw=cmd.get("raw", ""), cleaned=cmd.get("cleaned", ""),
                                        confidence=cmd.get("confidence", ""), ai_modified=bool(cmd.get("ai_modified")), user_approved=True)
            self.tracker.record_event(app_id, EventType.ANSWER_PROVIDED, f"Answered: {pending.label[:80]}", {"field_id": fid, "source": cmd.get("source", "typed")}, enforce=False)
            app = self.tracker.get_application(app_id) or {}
            if cmd.get("save_reusable") and len(value) > 40:
                self.tracker.save_approved_answer(pending.label, value, app.get("company", ""))
            if cmd.get("remember_platform") and app.get("ats"):
                try:
                    from ..platforms import PlatformProfiles

                    PlatformProfiles().set_answer(app["ats"], pending.label, value)
                except Exception as e:  # noqa: BLE001
                    log.warning("could not store platform answer: %s", e)
            if app.get("ats"):
                from ..platforms import normalize_question

                self.tracker.record_platform_question(app["ats"], normalize_question(pending.label), pending.label, pending.category, pending.kind, pending.options, value, cmd.get("source", "typed"))
        self.bus.publish({"kind": "answer_result", "application_id": app_id, "field_id": fid, "ok": ok, "message": msg})
        self._set_waiting(self._pending_payload(app_id, fill))

    # -- review ------------------------------------------------------------ #

    def review(self, fill: FillResult, app_id: int) -> str:
        self.state.current_app_id = app_id
        app = self.tracker.get_application(app_id) or {}
        blocking = [p for p in fill.pending if p.required or p.draft]
        self._set_waiting({"type": "review", "application_id": app_id, "company": app.get("company", ""), "title": app.get("title", ""),
                           "answers": [asdict(a) for a in fill.answers], "blocking": [p.label for p in blocking]})
        try:
            while True:
                cmd = self._next_command()
                ctype = cmd.get("type")
                if ctype == "submit":
                    if blocking:
                        self.bus.publish({"kind": "notice", "message": "Cannot submit: required questions or unapproved drafts remain.", "application_id": app_id})
                        continue
                    return "submit"
                if ctype == "skip":
                    return "skip"
                if ctype == "manual":
                    return "manual"
                if ctype == "edit_done":
                    fill.pending = []
                    fill.unanswered = []
                    blocking = []
                    fill.answers.append(FieldAnswer("(edited in browser)", "see browser", "user", status="filled", reason="edited by you"))
                    self._set_waiting({"type": "review", "application_id": app_id, "company": app.get("company", ""), "title": app.get("title", ""),
                                       "answers": [asdict(a) for a in fill.answers], "blocking": []})
        finally:
            self._set_waiting(None)

    # -- captcha ----------------------------------------------------------- #

    def captcha(self, page: Any, stage: str, app_id: int) -> bool:
        self.state.current_app_id = app_id
        app = self.tracker.get_application(app_id) or {}
        self._set_waiting({"type": "captcha", "application_id": app_id, "company": app.get("company", ""), "title": app.get("title", ""), "stage": stage})
        try:
            while True:
                cmd = self._next_command()
                if cmd.get("type") == "captcha_solved":
                    if not captcha_present(page):
                        return True
                    self.bus.publish({"kind": "notice", "message": "The CAPTCHA still appears to be present.", "application_id": app_id})
                elif cmd.get("type") in ("skip", "manual", "cancel_application"):
                    return False
        finally:
            self._set_waiting(None)


class SessionManager:
    """One AutoApply session at a time."""

    def __init__(self, tracker: Tracker, bus: EventBus, settings: Settings, profile_loader, secrets_loader):
        self.tracker = tracker
        self.bus = bus
        self.settings = settings
        self.profile_loader = profile_loader
        self.secrets_loader = secrets_loader
        self.current: SessionState | None = None
        self._lock = threading.Lock()

    def is_running(self) -> bool:
        return bool(self.current and self.current.thread and self.current.thread.is_alive())

    def start(self, config: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.is_running():
                raise RuntimeError("An AutoApply session is already running")
            sess = self.tracker.create_session(config)
            state = SessionState(sess["id"])
            self.current = state
            state.thread = threading.Thread(target=self._run, args=(state, config), name=f"autoapply-{sess['id']}", daemon=True)
            state.thread.start()
            return sess

    def _run(self, state: SessionState, config: dict[str, Any]) -> None:
        profile: Profile = self.profile_loader()
        secrets: Secrets = self.secrets_loader()
        opts = RunOptions(
            mode="review",
            dry_run=bool(config.get("dry_run", False)),
            limit=int(config.get("max_applications", 10) or 10),
            headless=bool(config.get("headless", False)),  # visible browser by default: you review before submit
            retry=bool(config.get("retry", False)),
            no_sync=bool(config.get("no_sync", False)),
            ats_only=list(config.get("ats", []) or []),
            company=config.get("company") or None,
            session_id=state.id,
            title_keywords=list(config.get("target_roles", []) or []),
            locations=list(config.get("locations", []) or []),
            exclude_companies=list(config.get("excluded_companies", []) or []),
            exclude_locations=list(config.get("excluded_locations", []) or []),
            categories=list(config.get("categories", []) or []),
            listing_ids=list(config.get("listing_ids", []) or []),
            us_only=None if config.get("us_only") is None else bool(config.get("us_only")),
        )
        interaction = WebInteraction(state, self.tracker, self.bus)
        counters = {"attempted": 0, "submitted": 0}

        def on_event(ev: dict[str, Any]) -> None:
            if ev.get("kind") == "application_event":
                if ev.get("type") == EventType.APPLICATION_STARTED.value or ev.get("type") == EventType.APPLICATION_RESUMED.value:
                    state.current_app_id = ev["application_id"]
                    counters["attempted"] += 1
                    self.tracker.update_session(state.id, attempted=counters["attempted"], current_application_id=state.current_app_id)
                if ev.get("type") == EventType.APPLICATION_SUBMITTED.value:
                    counters["submitted"] += 1
                    self.tracker.update_session(state.id, submitted=counters["submitted"])
            ev.setdefault("session_id", state.id)
            self.bus.publish(ev)

        status = "Completed"
        try:
            self.bus.publish({"kind": "session", "session_id": state.id, "status": "Running", "message": "Session started"})
            runner = Runner(self.settings, profile, secrets, self.tracker, opts, interaction=interaction, on_event=on_event)
            candidates, stats = runner.collect_candidates()
            self.tracker.update_session(state.id, total=min(len(candidates), opts.limit or len(candidates)))
            runner.run()
            if state.cancel.is_set():
                status = "Cancelled"
        except SessionCancelled:
            status = "Cancelled"
        except Exception as e:  # noqa: BLE001
            log.error("session %s failed: %s\n%s", state.id, e, traceback.format_exc())
            state.error = f"{type(e).__name__}: {e}"[:500]
            status = "Failed"
        finally:
            state.waiting = None
            self.tracker.update_session(state.id, status=status, error=state.error, current_application_id=None)
            self.bus.publish({"kind": "session", "session_id": state.id, "status": status, "waiting": None, "error": state.error})
            self.tracker.notify("session", f"AutoApply session {status.lower()}", f"{counters['submitted']} submitted, {counters['attempted']} attempted")

    # -- controls ---------------------------------------------------------- #

    def _state(self, session_id: int) -> SessionState:
        if not self.current or self.current.id != session_id or not self.is_running():
            raise KeyError("no running session with that id")
        return self.current

    def pause(self, session_id: int) -> None:
        self._state(session_id).pause.set()

    def resume(self, session_id: int) -> None:
        self._state(session_id).pause.clear()

    def cancel(self, session_id: int) -> None:
        st = self._state(session_id)
        st.cancel.set()
        st.pause.clear()
        st.commands.put({"type": "cancel"})

    def command(self, session_id: int, cmd: dict[str, Any]) -> None:
        self._state(session_id).commands.put(cmd)

    def status(self) -> dict[str, Any]:
        sess = self.tracker.latest_session()
        if not sess:
            return {"session": None, "running": False, "waiting": None}
        running = self.is_running() and self.current is not None and self.current.id == sess["id"]
        waiting = self.current.waiting if running and self.current else None
        apps = self.tracker.session_applications(sess["id"])
        current = self.tracker.get_application(self.current.current_app_id) if running and self.current and self.current.current_app_id else None
        if not running and sess.get("status") in ("Running", "WaitingForUser", "Paused"):
            # Process restarted mid-session: mark it as failed so the UI doesn't show a ghost.
            self.tracker.update_session(sess["id"], status="Failed", error="server restarted during session")
            sess = self.tracker.get_session(sess["id"])
        return {"session": sess, "running": running, "waiting": waiting, "applications": apps, "current": current,
                "paused": bool(running and self.current and self.current.pause.is_set())}
