"""FastAPI backend for AutoApplier (local, single user).

Run with: ``autoapply web`` (binds to 127.0.0.1 by default).
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import tempfile
import threading
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import CONFIG_DIR, PROJECT_ROOT, Profile, load_profile, load_secrets, load_settings, resolve_path
from ..dictation import DictationContext, TranscriptCleaner
from ..mail.classify import EmailClassifier
from ..mail.monitor import EmailMonitor, EmailProcessor
from ..tracker import ApplicationStatus, Tracker
from ..tracker.migrate import migrate_legacy
from ..tracker.states import STATUS_LABELS
from .events import EventBus
from .orchestrator import SessionManager

log = logging.getLogger("autoapply.web")
STATIC = Path(__file__).with_name("static")


class AppPatch(BaseModel):
    status: str | None = None
    next_action: str | None = None
    note: str | None = None


class AnswerBody(BaseModel):
    question_id: int | None = None
    field_id: str | None = None
    answer: str
    source: str = "typed"  # typed | voice | draft | reused
    raw: str = ""
    cleaned: str = ""
    confidence: str = ""
    ai_modified: bool = False
    save_reusable: bool = False


class ActionPatch(BaseModel):
    status: str | None = None
    deadline: str | None = None
    title: str | None = None


class ActionCreate(BaseModel):
    application_id: int | None = None
    title: str
    type: str = "manual"
    description: str = ""
    deadline: str | None = None
    priority: str = "medium"
    url: str = ""


class LinkBody(BaseModel):
    application_id: int | None = None


class CleanBody(BaseModel):
    raw: str
    application_id: int | None = None
    question_id: int | None = None
    question: str = ""




class AppState:
    def __init__(self, settings=None, tracker: Tracker | None = None):
        self.settings = settings or load_settings()
        self.secrets = load_secrets()
        db_path = resolve_path(str(self.settings.get("run.tracker_database", "./data/autoapply.db")))
        self.tracker = tracker or Tracker(db_path)
        self.bus = EventBus()
        self.sessions = SessionManager(self.tracker, self.bus, self.settings, load_profile, load_secrets)
        self.cleaner = TranscriptCleaner(self.secrets.anthropic_api_key, str(self.settings.get("llm.fast_model", "claude-haiku-4-5")),
                                         use_llm=bool(self.settings.get("voice.llm_cleanup", True)))
        classifier = EmailClassifier(self.secrets.anthropic_api_key, str(self.settings.get("llm.model", "claude-opus-5")),
                                     use_llm=bool(self.settings.get("email.llm_classification", True)))
        self.email_processor = EmailProcessor(self.tracker, classifier, self.bus.publish)
        cred_file = Path(os.environ.get("GOOGLE_OAUTH_CLIENT_FILE") or resolve_path(str(self.settings.get("email.google_client_file", "./config/google_oauth_client.json"))))
        self.email_monitor = EmailMonitor(self.tracker, self.email_processor, PROJECT_ROOT, cred_file,
                                          interval_seconds=int(self.settings.get("email.poll_interval_seconds", 300)), on_event=self.bus.publish)
        self._stt = None
        self.migrated = 0

    def start_background(self) -> None:
        legacy = resolve_path(str(self.settings.get("run.database", "./data/applications.db")))
        try:
            self.migrated = migrate_legacy(self.tracker, legacy)
        except Exception as e:  # noqa: BLE001
            log.warning("legacy migration failed: %s", e)
        if bool(self.settings.get("email.enabled", True)):
            self.email_monitor.start()

    def stop_background(self) -> None:
        self.email_monitor.stop()


def _label(status: str) -> str:
    try:
        return STATUS_LABELS[ApplicationStatus(status)]
    except (ValueError, KeyError):
        return status


def create_app(state: AppState | None = None) -> FastAPI:
    st = state or AppState()
    app = FastAPI(title="AutoApplier", version="0.2.0")
    app.state.ctx = st

    @app.on_event("startup")
    def _startup() -> None:
        st.start_background()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        st.stop_background()

    # ------------------------------------------------------------------ #
    # Dashboard / applications
    # ------------------------------------------------------------------ #

    @app.get("/api/dashboard")
    def dashboard() -> dict[str, Any]:
        t = st.tracker
        return {
            "metrics": t.metrics(),
            "actions": t.actions("open")[:8],
            "session": st.sessions.status(),
            "recent_applications": [{**a, "status_label": _label(a["status"])} for a in t.list_applications(limit=8)],
            "notifications": t.notifications(unread_only=True, limit=10),
            "email": {"connected": bool(t.email_account()), **(t.email_account() or {})},
            "migrated": st.migrated,
        }

    @app.get("/api/applications")
    def applications(status: str | None = None, company: str | None = None, search: str | None = None, needs_action: bool = False,
                     since: str | None = None, until: str | None = None, location: str | None = None, source: str | None = None,
                     limit: int | None = None) -> list[dict[str, Any]]:
        rows = st.tracker.list_applications(status=status, company=company, search=search, needs_action=needs_action, since=since,
                                            until=until, location=location, source=source, limit=limit)
        return [{**a, "status_label": _label(a["status"])} for a in rows]

    @app.get("/api/applications/{app_id}")
    def application(app_id: int) -> dict[str, Any]:
        t = st.tracker
        a = t.get_application(app_id)
        if not a:
            raise HTTPException(404, "application not found")
        job = t.get_job(a["job_id"]) or {}
        return {"application": {**a, "status_label": _label(a["status"]), "description": job.get("description", "")},
                "events": t.events(app_id), "questions": t.questions(app_id), "emails": t.email_events(application_id=app_id),
                "actions": t.actions(None, app_id)}


    @app.patch("/api/applications/{app_id}")
    def patch_application(app_id: int, body: AppPatch) -> dict[str, Any]:
        t = st.tracker
        if not t.get_application(app_id):
            raise HTTPException(404, "application not found")
        if body.status:
            try:
                t.set_status(app_id, ApplicationStatus(body.status), body.note or "")
            except Exception as e:  # noqa: BLE001
                raise HTTPException(400, str(e)) from e
        if body.next_action is not None:
            t.update_application(app_id, next_action=body.next_action)
        st.bus.publish({"kind": "application_updated", "application_id": app_id})
        return t.get_application(app_id)  # type: ignore[return-value]

    @app.post("/api/applications/{app_id}/retry")
    def retry_application(app_id: int) -> dict[str, Any]:
        a = st.tracker.get_application(app_id)
        if not a:
            raise HTTPException(404, "application not found")
        try:
            return st.sessions.start({"listing_ids": [a.get("listing_id") or a["job_url"]], "max_applications": 1, "retry": True, "no_sync": True, "label": f"Retry {a['company']}"})
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/api/applications/{app_id}/events")
    def app_events(app_id: int) -> list[dict[str, Any]]:
        return st.tracker.events(app_id)

    @app.get("/api/applications/{app_id}/questions")
    def app_questions(app_id: int) -> list[dict[str, Any]]:
        return st.tracker.questions(app_id)


    @app.post("/api/applications/{app_id}/answers")
    def post_answer(app_id: int, body: AnswerBody) -> dict[str, Any]:
        t = st.tracker
        q = t.get_question(body.question_id) if body.question_id else None
        field_id = body.field_id or (q or {}).get("field_id", "")
        sess = st.sessions.status()
        waiting = sess.get("waiting") or {}
        if sess.get("running") and waiting.get("type") == "questions" and waiting.get("application_id") == app_id and field_id:
            st.sessions.command(sess["session"]["id"], {"type": "answer", "field_id": field_id, "value": body.answer, "source": body.source,
                                                          "raw": body.raw, "cleaned": body.cleaned, "confidence": body.confidence,
                                                          "ai_modified": body.ai_modified, "save_reusable": body.save_reusable})
            return {"queued": True}
        if q is None:
            raise HTTPException(400, "question_id required when no session is waiting on this application")
        ans = t.add_answer(q["id"], body.answer, body.source, body.raw, body.cleaned, body.confidence, body.ai_modified, True)
        if body.save_reusable:
            a = t.get_application(app_id) or {}
            t.save_approved_answer(q["question_text"], body.answer, a.get("company", ""))
        return {"queued": False, "answer": ans}

    @app.get("/api/approved-answers")
    def approved_answers(question: str | None = None) -> list[dict[str, Any]]:
        from ..tracker.dedupe import question_similarity

        rows = st.tracker.approved_answers()
        if question:
            scored = [(question_similarity(question, r["question_text"]), r) for r in rows]
            # Suggestions only: the user still has to click "Use answer".
            return [dict(r, similarity=round(s, 2)) for s, r in sorted(scored, key=lambda x: -x[0]) if s >= 0.6]
        return rows

    # ------------------------------------------------------------------ #
    # AutoApply sessions
    # ------------------------------------------------------------------ #

    @app.post("/api/autoapply/start")
    def start(config: dict[str, Any]) -> dict[str, Any]:
        try:
            return st.sessions.start(config)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/api/autoapply/current")
    def current() -> dict[str, Any]:
        return st.sessions.status()

    @app.get("/api/autoapply/{session_id}/status")
    def session_status(session_id: int) -> dict[str, Any]:
        s = st.sessions.status()
        if not s.get("session") or s["session"]["id"] != session_id:
            sess = st.tracker.get_session(session_id)
            if not sess:
                raise HTTPException(404, "session not found")
            return {"session": sess, "running": False, "waiting": None, "applications": st.tracker.session_applications(session_id)}
        return s

    def _control(session_id: int, fn) -> dict[str, Any]:
        try:
            fn(session_id)
        except KeyError as e:
            raise HTTPException(409, str(e)) from e
        return st.sessions.status()

    @app.post("/api/autoapply/{session_id}/pause")
    def pause(session_id: int) -> dict[str, Any]:
        return _control(session_id, st.sessions.pause)

    @app.post("/api/autoapply/{session_id}/resume")
    def resume(session_id: int) -> dict[str, Any]:
        return _control(session_id, st.sessions.resume)

    @app.post("/api/autoapply/{session_id}/cancel")
    def cancel(session_id: int) -> dict[str, Any]:
        return _control(session_id, st.sessions.cancel)

    @app.post("/api/autoapply/{session_id}/command")
    def command(session_id: int, cmd: dict[str, Any]) -> dict[str, Any]:
        allowed = {"submit", "skip", "manual", "continue", "edit_done", "captcha_solved", "skip_question", "answer", "cancel_application"}
        if cmd.get("type") not in allowed:
            raise HTTPException(400, f"unknown command; allowed: {sorted(allowed)}")
        return _control(session_id, lambda sid: st.sessions.command(sid, cmd))

    @app.get("/api/listings/preview")
    def listings_preview(target_roles: str = "", locations: str = "", categories: str = "", excluded_companies: str = "", limit: int = 25) -> dict[str, Any]:
        from ..runner import RunOptions, Runner

        opts = RunOptions(no_sync=True, title_keywords=[x.strip() for x in target_roles.split(",") if x.strip()],
                          locations=[x.strip() for x in locations.split(",") if x.strip()],
                          categories=[x.strip() for x in categories.split(",") if x.strip()],
                          exclude_companies=[x.strip() for x in excluded_companies.split(",") if x.strip()])
        try:
            runner = Runner(st.settings, load_profile(), st.secrets, st.tracker, opts)
            cands, stats = runner.collect_candidates()
        except FileNotFoundError:
            return {"stats": {"error": "listings not synced yet; start a session to clone the repo"}, "listings": []}
        return {"stats": stats, "listings": [c.to_dict() for c in cands[:limit]]}

    # ------------------------------------------------------------------ #
    # Actions / notifications
    # ------------------------------------------------------------------ #

    @app.get("/api/actions")
    def actions(status: str | None = "open") -> list[dict[str, Any]]:
        return st.tracker.actions(status if status != "all" else None)


    @app.patch("/api/actions/{action_id}")
    def patch_action(action_id: int, body: ActionPatch) -> dict[str, Any]:
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        if fields.get("status") not in (None, "open", "done", "dismissed"):
            raise HTTPException(400, "status must be open, done or dismissed")
        st.tracker.update_action(action_id, **fields)
        st.bus.publish({"kind": "action_updated", "action_id": action_id})
        return st.tracker.get_action(action_id) or {}


    @app.post("/api/actions")
    def create_action(body: ActionCreate) -> dict[str, Any]:
        return st.tracker.add_action(body.application_id, body.type, body.title, body.description, body.deadline, body.priority, body.url)

    @app.get("/api/notifications")
    def notifications(unread: bool = False) -> list[dict[str, Any]]:
        return st.tracker.notifications(unread_only=unread)

    @app.patch("/api/notifications/{nid}/read")
    def read_notification(nid: int) -> dict[str, str]:
        st.tracker.mark_notification_read(nid)
        return {"ok": "true"}

    @app.post("/api/notifications/read_all")
    def read_all() -> dict[str, str]:
        st.tracker.mark_notification_read(None)
        return {"ok": "true"}

    # ------------------------------------------------------------------ #
    # Profile / settings
    # ------------------------------------------------------------------ #

    @app.get("/api/profile")
    def get_profile() -> dict[str, Any]:
        p = load_profile()
        return {"profile": p.data, "placeholders": p.placeholders(), "resume_text": p.resume_text()[:20000], "resume_pdf": str(p.resume_pdf), "resume_exists": p.resume_pdf.exists()}

    @app.patch("/api/profile")
    def patch_profile(body: dict[str, Any]) -> dict[str, Any]:
        path = CONFIG_DIR / "profile.yaml"
        current = load_profile().data
        for section, values in body.items():
            if isinstance(values, dict):
                current.setdefault(section, {})
                if not isinstance(current[section], dict):
                    current[section] = {}
                current[section].update(values)
            else:
                current[section] = values
        path.write_text(yaml.safe_dump(current, sort_keys=False, allow_unicode=True), encoding="utf-8")
        p = Profile(current)
        return {"profile": p.data, "placeholders": p.placeholders()}

    SAFE_SETTINGS = ("filters", "run", "llm", "voice", "email", "listings")

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        s = load_settings().data
        return {k: s.get(k, {}) for k in SAFE_SETTINGS} | {"has_anthropic_key": bool(st.secrets.anthropic_api_key),
                                                             "google_client_file": str(st.email_monitor.credentials_file),
                                                             "google_client_exists": st.email_monitor.credentials_file.exists()}

    @app.patch("/api/settings")
    def patch_settings(body: dict[str, Any]) -> dict[str, Any]:
        path = CONFIG_DIR / "settings.yaml"
        current = load_settings().data
        for section, values in body.items():
            if section in SAFE_SETTINGS and isinstance(values, dict):
                current.setdefault(section, {}).update(values)
        path.write_text(yaml.safe_dump(current, sort_keys=False, allow_unicode=True), encoding="utf-8")
        st.settings = load_settings()
        return get_settings()

    # ------------------------------------------------------------------ #
    # Email
    # ------------------------------------------------------------------ #

    @app.get("/api/email/status")
    def email_status() -> dict[str, Any]:
        acct = st.tracker.email_account()
        return {"connected": bool(acct), "account": acct, "syncing": st.email_monitor.syncing, "last_error": st.email_monitor.last_error,
                "client_file": str(st.email_monitor.credentials_file), "client_file_exists": st.email_monitor.credentials_file.exists(),
                "needs_confirmation": len(st.tracker.email_events(match_status="needs_confirmation")),
                "unmatched": len(st.tracker.email_events(match_status="unmatched"))}

    @app.post("/api/email/connect")
    def email_connect() -> dict[str, Any]:
        if st.tracker.email_account():
            return {"connected": True, "account": st.tracker.email_account()}
        if not st.email_monitor.credentials_file.exists():
            raise HTTPException(400, f"Google OAuth client file missing at {st.email_monitor.credentials_file}. See README → Email.")

        def run() -> None:
            try:
                email = st.email_monitor.connect()
                st.email_monitor.start()
                threading.Thread(target=st.email_monitor.sync_once, daemon=True).start()
                log.info("Gmail connected: %s", email)
            except Exception as e:  # noqa: BLE001
                st.email_monitor.last_error = str(e)[:300]
                st.bus.publish({"kind": "email_status", "connected": False, "error": str(e)[:300]})

        threading.Thread(target=run, daemon=True).start()
        return {"started": True, "message": "A Google sign-in window was opened in your browser. Grant read-only Gmail access."}

    @app.post("/api/email/disconnect")
    def email_disconnect() -> dict[str, Any]:
        st.email_monitor.disconnect()
        return {"connected": False}

    @app.post("/api/email/sync")
    def email_sync() -> dict[str, Any]:
        if not st.tracker.email_account():
            raise HTTPException(400, "Gmail is not connected")
        threading.Thread(target=st.email_monitor.sync_once, daemon=True).start()
        return {"started": True}

    @app.get("/api/email/events")
    def email_events(match_status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = st.tracker.email_events(match_status=match_status, limit=limit)
        for r in rows:
            if r.get("application_id"):
                a = st.tracker.get_application(r["application_id"])
                r["application"] = {"company": a["company"], "title": a["title"]} if a else None
        return rows


    @app.post("/api/email/events/{email_id}/link")
    def link_email(email_id: int, body: LinkBody) -> dict[str, Any]:
        if not st.tracker.get_email_event(email_id):
            raise HTTPException(404, "email not found")
        st.email_processor.confirm_match(email_id, body.application_id)
        st.bus.publish({"kind": "email", "message": "Email match updated", "email_id": email_id})
        return st.tracker.get_email_event(email_id) or {}

    # ------------------------------------------------------------------ #
    # Voice
    # ------------------------------------------------------------------ #


    @app.post("/api/voice/clean")
    def voice_clean(body: CleanBody) -> dict[str, Any]:
        p = load_profile()
        ctx = DictationContext(question=body.question, resume_text=p.resume_text(), profile_summary=p.summary_for_llm())
        if body.application_id:
            a = st.tracker.get_application(body.application_id)
            if a:
                job = st.tracker.get_job(a["job_id"]) or {}
                ctx.company, ctx.role, ctx.job_text = a["company"], a["title"], (job.get("description") or "")[:6000]
        if body.question_id and not body.question:
            q = st.tracker.get_question(body.question_id)
            if q:
                ctx.question = q["question_text"]
        return st.cleaner.clean(body.raw, ctx).to_dict()

    @app.post("/api/voice/transcribe")
    async def voice_transcribe(file: UploadFile) -> dict[str, Any]:
        data = await file.read()
        if st._stt is None:
            try:
                from ..voice import FasterWhisperTranscriber

                st._stt = FasterWhisperTranscriber(str(st.settings.get("voice.whisper_model", "base.en")), str(st.settings.get("voice.language", "en")))
            except Exception as e:  # noqa: BLE001
                raise HTTPException(501, f"Server-side speech-to-text unavailable ({e}). Use a browser with speech recognition (Chrome/Edge/Safari) or install the voice extras.") from e
        suffix = Path(file.filename or "audio.webm").suffix or ".webm"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            path = tmp.name
        try:
            segments, _ = await asyncio.to_thread(lambda: st._stt.model.transcribe(path, language=st._stt.language or None, beam_size=5, vad_filter=True))
            text = " ".join(s.text.strip() for s in segments).strip()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return {"text": text}

    # ------------------------------------------------------------------ #
    # Events / debug
    # ------------------------------------------------------------------ #

    @app.get("/api/events/stream")
    async def stream(request: Request):
        q = st.bus.subscribe()

        async def gen():
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.to_thread(q.get, True, 15)
                        yield EventBus.sse(ev)
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                st.bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/events/recent")
    def recent(limit: int = 100, session_id: int | None = None) -> list[dict[str, Any]]:
        return st.tracker.recent_events(limit, session_id)

    @app.get("/api/debug")
    def debug() -> dict[str, Any]:
        s = st.sessions.status()
        return {"session": s, "bus_history": list(st.bus.history)[-100:], "email": email_status(), "settings": get_settings()}

    # ------------------------------------------------------------------ #
    # Static SPA
    # ------------------------------------------------------------------ #

    if STATIC.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            target = STATIC / path
            if path and target.is_file():
                return FileResponse(str(target))
            return FileResponse(str(STATIC / "index.html"))

    return app


def serve(host: str = "127.0.0.1", port: int = 8710, open_browser: bool = True) -> None:
    import uvicorn

    if open_browser:
        import webbrowser

        threading.Timer(1.5, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
