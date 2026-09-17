"""SQLite storage for the tracker (jobs, applications, events, Q&A, email, actions…).

One file (``data/autoapply.db``), WAL mode, a single connection guarded by a
lock so the web worker threads and the API can share it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .dedupe import external_job_id, find_duplicate
from .states import ApplicationStatus, EventType, InvalidTransition, status_after_event

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT DEFAULT '',
    url TEXT NOT NULL,
    source TEXT DEFAULT '',
    external_job_id TEXT DEFAULT '',
    category TEXT DEFAULT '',
    description TEXT DEFAULT '',
    date_discovered TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_listing ON jobs(listing_id);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    status TEXT NOT NULL,
    ats TEXT DEFAULT '',
    resume_path TEXT DEFAULT '',
    application_url TEXT DEFAULT '',
    date_applied TEXT,
    next_action TEXT DEFAULT '',
    next_action_deadline TEXT,
    error TEXT DEFAULT '',
    screenshot_path TEXT DEFAULT '',
    session_id INTEGER,
    step TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_app_job ON applications(job_id);

CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    type TEXT NOT NULL,
    message TEXT DEFAULT '',
    metadata TEXT DEFAULT '{}',
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_app ON application_events(application_id);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    field_id TEXT DEFAULT '',
    question_text TEXT NOT NULL,
    question_type TEXT DEFAULT 'text',
    category TEXT DEFAULT '',
    options TEXT DEFAULT '[]',
    required INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',      -- pending | answered | skipped | auto
    reason TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_questions_app ON questions(application_id);

CREATE TABLE IF NOT EXISTS answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id),
    raw_transcription TEXT DEFAULT '',
    cleaned_answer TEXT DEFAULT '',
    final_answer TEXT DEFAULT '',
    source TEXT DEFAULT '',             -- profile | inferred | draft | voice | typed | reused
    confidence TEXT DEFAULT '',
    ai_modified INTEGER DEFAULT 0,
    user_approved INTEGER DEFAULT 0,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approved_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_text TEXT NOT NULL,
    answer TEXT NOT NULL,
    company TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT DEFAULT 'gmail',
    provider_message_id TEXT NOT NULL UNIQUE,
    application_id INTEGER REFERENCES applications(id),
    category TEXT DEFAULT 'UNKNOWN',
    subject TEXT DEFAULT '',
    sender TEXT DEFAULT '',
    snippet TEXT DEFAULT '',
    received_at TEXT,
    extracted TEXT DEFAULT '{}',
    match_confidence REAL DEFAULT 0,
    match_status TEXT DEFAULT 'unmatched',   -- auto | confirmed | needs_confirmation | unmatched | ignored
    candidates TEXT DEFAULT '[]',
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER REFERENCES applications(id),
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    url TEXT DEFAULT '',
    deadline TEXT,
    status TEXT DEFAULT 'open',         -- open | done | dismissed
    priority TEXT DEFAULT 'medium',     -- high | medium | low
    source_email_id INTEGER,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT DEFAULT '',
    application_id INTEGER,
    read INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS autoapply_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,               -- Idle | Running | WaitingForUser | Paused | Completed | Failed | Cancelled
    config TEXT DEFAULT '{}',
    total INTEGER DEFAULT 0,
    attempted INTEGER DEFAULT 0,
    submitted INTEGER DEFAULT 0,
    current_application_id INTEGER,
    error TEXT DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS email_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    email TEXT DEFAULT '',
    token_enc BLOB,
    connected_at TEXT NOT NULL,
    last_sync_at TEXT,
    last_history_id TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("metadata", "options", "extracted", "config", "candidates"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k] or ("[]" if k in ("options", "candidates") else "{}"))
            except json.JSONDecodeError:
                pass
    return d


class Tracker:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self.lock:
            return _row(self.conn.execute(sql, tuple(params)).fetchone())

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [_row(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]  # type: ignore[misc]

    def kv_get(self, key: str, default: Any = None) -> Any:
        r = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(r["value"]) if r and r["value"] is not None else default

    def kv_set(self, key: str, value: Any) -> None:
        self._exec("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (key, json.dumps(value)))

    # ------------------------------------------------------------------ #
    # jobs
    # ------------------------------------------------------------------ #

    def find_duplicate_job(self, company: str, title: str, location: str, url: str) -> dict[str, Any] | None:
        candidates = self._all("SELECT id, company, title, location, url, external_job_id FROM jobs WHERE lower(company)=lower(?)", (company,))
        by_url = self._all("SELECT id, company, title, location, url, external_job_id FROM jobs WHERE url=?", (url,))
        match = find_duplicate({"company": company, "title": title, "location": location, "url": url}, by_url + candidates)
        if not match:
            return None
        job = self.get_job(match.job_id)
        if job:
            job["duplicate_reason"] = match.reason
            job["duplicate_confidence"] = match.confidence
        return job

    def upsert_job(self, company: str, title: str, url: str, location: str = "", source: str = "", listing_id: str = "",
                   category: str = "", description: str = "") -> dict[str, Any]:
        existing = self.find_duplicate_job(company, title, location, url)
        if existing:
            if description and not existing.get("description"):
                self._exec("UPDATE jobs SET description=? WHERE id=?", (description[:20000], existing["id"]))
            return existing
        ts = now_iso()
        cur = self._exec(
            "INSERT INTO jobs (listing_id, company, title, location, url, source, external_job_id, category, description, date_discovered, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (listing_id, company.strip(), title.strip(), location, url, source, external_job_id(url), category, description[:20000], ts, ts),
        )
        return self.get_job(int(cur.lastrowid))  # type: ignore[return-value]

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM jobs WHERE id=?", (job_id,))

    # ------------------------------------------------------------------ #
    # applications
    # ------------------------------------------------------------------ #

    APP_SELECT = """SELECT a.*, j.company, j.title, j.location, j.url AS job_url, j.source, j.category, j.listing_id, j.external_job_id
                    FROM applications a JOIN jobs j ON j.id = a.job_id"""

    def create_application(self, job_id: int, status: ApplicationStatus = ApplicationStatus.DISCOVERED, ats: str = "",
                           resume_path: str = "", session_id: int | None = None) -> dict[str, Any]:
        ts = now_iso()
        cur = self._exec(
            "INSERT INTO applications (job_id, status, ats, resume_path, session_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (job_id, status.value, ats, resume_path, session_id, ts, ts),
        )
        app_id = int(cur.lastrowid)
        self.record_event(app_id, EventType.JOB_DISCOVERED, "Job discovered", enforce=False)
        return self.get_application(app_id)  # type: ignore[return-value]

    def application_for_job(self, job_id: int) -> dict[str, Any] | None:
        return self._one(self.APP_SELECT + " WHERE a.job_id=? ORDER BY a.id DESC LIMIT 1", (job_id,))

    def get_application(self, app_id: int) -> dict[str, Any] | None:
        return self._one(self.APP_SELECT + " WHERE a.id=?", (app_id,))

    def update_application(self, app_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE applications SET {cols}, updated_at=? WHERE id=?", (*fields.values(), now_iso(), app_id))

    def list_applications(self, status: str | None = None, company: str | None = None, search: str | None = None,
                          needs_action: bool = False, since: str | None = None, until: str | None = None,
                          location: str | None = None, source: str | None = None, limit: int | None = None,
                          statuses: list[str] | None = None) -> list[dict[str, Any]]:
        sql = self.APP_SELECT + " WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND a.status=?"
            params.append(status)
        if statuses:
            sql += f" AND a.status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        if company:
            sql += " AND lower(j.company) LIKE ?"
            params.append(f"%{company.lower()}%")
        if location:
            sql += " AND lower(j.location) LIKE ?"
            params.append(f"%{location.lower()}%")
        if source:
            sql += " AND lower(j.source) LIKE ?"
            params.append(f"%{source.lower()}%")
        if since:
            sql += " AND COALESCE(a.date_applied, a.created_at) >= ?"
            params.append(since)
        if until:
            sql += " AND COALESCE(a.date_applied, a.created_at) <= ?"
            params.append(until + ("T23:59:59" if len(until) == 10 else ""))
        if search:
            s = search.strip().lower()
            if s in ("needs action", "action", "needs input"):
                needs_action = True
            elif s in ("assessment", "oa", "interview", "offer", "rejected", "submitted", "failed"):
                sql += " AND lower(a.status) LIKE ?"
                params.append(f"%{s.split()[0]}%")
            else:
                sql += " AND (lower(j.company) LIKE ? OR lower(j.title) LIKE ? OR lower(j.location) LIKE ? OR lower(a.status) LIKE ?)"
                params.extend([f"%{s}%"] * 4)
        if needs_action:
            sql += " AND (a.status IN ('NEEDS_INPUT','FAILED') OR a.id IN (SELECT application_id FROM actions WHERE status='open'))"
        sql += " ORDER BY COALESCE(a.date_applied, a.updated_at) DESC, a.id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = self._all(sql, params)
        open_actions = {r["application_id"]: r for r in self._all("SELECT * FROM actions WHERE status='open' ORDER BY deadline IS NULL, deadline ASC")}
        for r in rows:
            act = open_actions.get(r["id"])
            r["open_action"] = act
            if act:
                r["next_action"] = r.get("next_action") or act["title"]
                r["next_action_deadline"] = r.get("next_action_deadline") or act.get("deadline")
        return rows

    # ------------------------------------------------------------------ #
    # events (status derived from events)
    # ------------------------------------------------------------------ #

    def record_event(self, app_id: int, event: EventType, message: str = "", metadata: dict[str, Any] | None = None,
                     enforce: bool = True, timestamp: str | None = None) -> dict[str, Any]:
        app = self.get_application(app_id)
        if app is None:
            raise KeyError(f"no application {app_id}")
        ts = timestamp or now_iso()
        current = ApplicationStatus(app["status"])
        new_status = current
        try:
            new_status = status_after_event(current, event, metadata)
        except InvalidTransition as e:
            if enforce:
                raise
            metadata = {**(metadata or {}), "transition_error": str(e)}
        cur = self._exec(
            "INSERT INTO application_events (application_id, type, message, metadata, timestamp) VALUES (?,?,?,?,?)",
            (app_id, event.value, message, json.dumps(metadata or {}), ts),
        )
        updates: dict[str, Any] = {}
        if new_status != current:
            updates["status"] = new_status.value
        if event == EventType.APPLICATION_SUBMITTED:
            updates["date_applied"] = ts
        if updates:
            self.update_application(app_id, **updates)
        return {"id": int(cur.lastrowid), "application_id": app_id, "type": event.value, "message": message,
                "metadata": metadata or {}, "timestamp": ts, "status": new_status.value, "status_changed": new_status != current}

    def set_status(self, app_id: int, status: ApplicationStatus, note: str = "") -> dict[str, Any]:
        return self.record_event(app_id, EventType.STATUS_SET_MANUALLY, note or f"Status set to {status.value}", {"status": status.value})

    def events(self, app_id: int) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM application_events WHERE application_id=? ORDER BY timestamp ASC, id ASC", (app_id,))

    def recent_events(self, limit: int = 100, session_id: int | None = None) -> list[dict[str, Any]]:
        if session_id:
            return self._all(
                "SELECT e.*, j.company, j.title FROM application_events e JOIN applications a ON a.id=e.application_id JOIN jobs j ON j.id=a.job_id "
                "WHERE a.session_id=? ORDER BY e.id DESC LIMIT ?", (session_id, limit))
        return self._all(
            "SELECT e.*, j.company, j.title FROM application_events e JOIN applications a ON a.id=e.application_id JOIN jobs j ON j.id=a.job_id "
            "ORDER BY e.id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ #
    # questions / answers
    # ------------------------------------------------------------------ #

    def add_question(self, app_id: int, text: str, field_id: str = "", qtype: str = "text", category: str = "",
                     options: list[str] | None = None, required: bool = False, status: str = "pending", reason: str = "") -> dict[str, Any]:
        cur = self._exec(
            "INSERT INTO questions (application_id, field_id, question_text, question_type, category, options, required, status, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (app_id, field_id, text, qtype, category, json.dumps(options or []), int(required), status, reason, now_iso()),
        )
        return self.get_question(int(cur.lastrowid))  # type: ignore[return-value]

    def get_question(self, qid: int) -> dict[str, Any] | None:
        q = self._one("SELECT * FROM questions WHERE id=?", (qid,))
        if q:
            q["answers"] = self._all("SELECT * FROM answers WHERE question_id=? ORDER BY id ASC", (qid,))
        return q

    def questions(self, app_id: int, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM questions WHERE application_id=?"
        params: list[Any] = [app_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        qs = self._all(sql + " ORDER BY id ASC", params)
        for q in qs:
            q["answers"] = self._all("SELECT * FROM answers WHERE question_id=? ORDER BY id ASC", (q["id"],))
        return qs

    def add_answer(self, question_id: int, final_answer: str, source: str, raw: str = "", cleaned: str = "",
                   confidence: str = "", ai_modified: bool = False, user_approved: bool = False, status: str = "answered") -> dict[str, Any]:
        cur = self._exec(
            "INSERT INTO answers (question_id, raw_transcription, cleaned_answer, final_answer, source, confidence, ai_modified, user_approved, timestamp) VALUES (?,?,?,?,?,?,?,?,?)",
            (question_id, raw, cleaned, final_answer, source, confidence, int(ai_modified), int(user_approved), now_iso()),
        )
        self._exec("UPDATE questions SET status=? WHERE id=?", (status, question_id))
        return self._one("SELECT * FROM answers WHERE id=?", (int(cur.lastrowid),))  # type: ignore[return-value]

    def set_question_status(self, question_id: int, status: str) -> None:
        self._exec("UPDATE questions SET status=? WHERE id=?", (status, question_id))

    def save_approved_answer(self, question_text: str, answer: str, company: str = "") -> None:
        self._exec("INSERT INTO approved_answers (question_text, answer, company, created_at) VALUES (?,?,?,?)", (question_text, answer, company, now_iso()))

    def approved_answers(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM approved_answers ORDER BY id DESC")

    # ------------------------------------------------------------------ #
    # email events
    # ------------------------------------------------------------------ #

    def has_email(self, provider_message_id: str) -> bool:
        return self._one("SELECT id FROM email_events WHERE provider_message_id=?", (provider_message_id,)) is not None

    def add_email_event(self, provider_message_id: str, category: str, subject: str, sender: str, received_at: str | None,
                        extracted: dict[str, Any], application_id: int | None, match_confidence: float, match_status: str,
                        candidates: list[dict[str, Any]] | None = None, snippet: str = "", provider: str = "gmail") -> dict[str, Any]:
        cur = self._exec(
            "INSERT OR IGNORE INTO email_events (provider, provider_message_id, application_id, category, subject, sender, snippet, received_at, extracted, match_confidence, match_status, candidates, processed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (provider, provider_message_id, application_id, category, subject[:500], sender[:300], snippet[:1000], received_at, json.dumps(extracted), match_confidence, match_status, json.dumps(candidates or []), now_iso()),
        )
        return self._one("SELECT * FROM email_events WHERE provider_message_id=?", (provider_message_id,))  # type: ignore[return-value]

    def email_events(self, application_id: int | None = None, match_status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM email_events WHERE 1=1"
        params: list[Any] = []
        if application_id:
            sql += " AND application_id=?"
            params.append(application_id)
        if match_status:
            sql += " AND match_status=?"
            params.append(match_status)
        return self._all(sql + " ORDER BY received_at DESC, id DESC LIMIT ?", params + [limit])

    def get_email_event(self, email_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM email_events WHERE id=?", (email_id,))

    def link_email(self, email_id: int, application_id: int | None, match_status: str) -> None:
        self._exec("UPDATE email_events SET application_id=?, match_status=? WHERE id=?", (application_id, match_status, email_id))

    def email_account(self) -> dict[str, Any] | None:
        return self._one("SELECT id, provider, email, connected_at, last_sync_at, last_history_id FROM email_accounts ORDER BY id DESC LIMIT 1")

    def email_account_token(self) -> bytes | None:
        r = self._one("SELECT token_enc FROM email_accounts ORDER BY id DESC LIMIT 1")
        return r["token_enc"] if r else None

    def set_email_account(self, provider: str, email: str, token_enc: bytes) -> None:
        self._exec("DELETE FROM email_accounts")
        self._exec("INSERT INTO email_accounts (provider, email, token_enc, connected_at) VALUES (?,?,?,?)", (provider, email, token_enc, now_iso()))

    def update_email_token(self, token_enc: bytes) -> None:
        self._exec("UPDATE email_accounts SET token_enc=?", (token_enc,))

    def clear_email_account(self) -> None:
        self._exec("DELETE FROM email_accounts")

    def mark_email_sync(self) -> None:
        self._exec("UPDATE email_accounts SET last_sync_at=?", (now_iso(),))

    # ------------------------------------------------------------------ #
    # actions / notifications
    # ------------------------------------------------------------------ #

    def add_action(self, application_id: int | None, type_: str, title: str, description: str = "", deadline: str | None = None,
                   priority: str = "medium", url: str = "", source_email_id: int | None = None) -> dict[str, Any]:
        # Don't duplicate an identical open action.
        dup = self._one("SELECT * FROM actions WHERE application_id IS ? AND type=? AND title=? AND status='open'", (application_id, type_, title))
        if dup:
            if deadline and not dup.get("deadline"):
                self._exec("UPDATE actions SET deadline=? WHERE id=?", (deadline, dup["id"]))
            return self.get_action(dup["id"])  # type: ignore[return-value]
        cur = self._exec(
            "INSERT INTO actions (application_id, type, title, description, url, deadline, priority, source_email_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (application_id, type_, title, description, url, deadline, priority, source_email_id, now_iso()),
        )
        if application_id:
            self.update_application(application_id, next_action=title, next_action_deadline=deadline)
        return self.get_action(int(cur.lastrowid))  # type: ignore[return-value]

    def get_action(self, action_id: int) -> dict[str, Any] | None:
        return self._one("SELECT ac.*, j.company, j.title AS job_title FROM actions ac LEFT JOIN applications a ON a.id=ac.application_id LEFT JOIN jobs j ON j.id=a.job_id WHERE ac.id=?", (action_id,))

    def actions(self, status: str | None = "open", application_id: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT ac.*, j.company, j.title AS job_title FROM actions ac LEFT JOIN applications a ON a.id=ac.application_id LEFT JOIN jobs j ON j.id=a.job_id WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND ac.status=?"
            params.append(status)
        if application_id:
            sql += " AND ac.application_id=?"
            params.append(application_id)
        order = {"high": 0, "medium": 1, "low": 2}
        rows = self._all(sql + " ORDER BY ac.created_at DESC", params)
        rows.sort(key=lambda r: (order.get(r.get("priority", "medium"), 1), r.get("deadline") or "9999"))
        return rows

    def update_action(self, action_id: int, **fields: Any) -> None:
        if "status" in fields and fields["status"] in ("done", "dismissed"):
            fields["completed_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE actions SET {cols} WHERE id=?", (*fields.values(), action_id))
        act = self.get_action(action_id)
        if act and act.get("application_id") and fields.get("status") in ("done", "dismissed"):
            nxt = self._one("SELECT title, deadline FROM actions WHERE application_id=? AND status='open' ORDER BY deadline IS NULL, deadline ASC LIMIT 1", (act["application_id"],))
            self.update_application(act["application_id"], next_action=(nxt or {}).get("title", ""), next_action_deadline=(nxt or {}).get("deadline"))

    def notify(self, type_: str, title: str, message: str = "", application_id: int | None = None) -> dict[str, Any]:
        cur = self._exec("INSERT INTO notifications (type, title, message, application_id, created_at) VALUES (?,?,?,?,?)", (type_, title, message, application_id, now_iso()))
        return self._one("SELECT * FROM notifications WHERE id=?", (int(cur.lastrowid),))  # type: ignore[return-value]

    def notifications(self, unread_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM notifications" + (" WHERE read=0" if unread_only else "") + " ORDER BY id DESC LIMIT ?"
        return self._all(sql, (limit,))

    def mark_notification_read(self, nid: int | None = None) -> None:
        if nid is None:
            self._exec("UPDATE notifications SET read=1")
        else:
            self._exec("UPDATE notifications SET read=1 WHERE id=?", (nid,))

    # ------------------------------------------------------------------ #
    # autoapply sessions
    # ------------------------------------------------------------------ #

    def create_session(self, config: dict[str, Any]) -> dict[str, Any]:
        cur = self._exec("INSERT INTO autoapply_sessions (status, config, started_at) VALUES (?,?,?)", ("Running", json.dumps(config), now_iso()))
        return self.get_session(int(cur.lastrowid))  # type: ignore[return-value]

    def get_session(self, sid: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM autoapply_sessions WHERE id=?", (sid,))

    def latest_session(self) -> dict[str, Any] | None:
        return self._one("SELECT * FROM autoapply_sessions ORDER BY id DESC LIMIT 1")

    def update_session(self, sid: int, **fields: Any) -> None:
        if fields.get("status") in ("Completed", "Failed", "Cancelled"):
            fields.setdefault("finished_at", now_iso())
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE autoapply_sessions SET {cols} WHERE id=?", (*fields.values(), sid))

    def session_applications(self, sid: int) -> list[dict[str, Any]]:
        return self._all(self.APP_SELECT + " WHERE a.session_id=? ORDER BY a.id ASC", (sid,))

    # ------------------------------------------------------------------ #
    # dashboard
    # ------------------------------------------------------------------ #

    def metrics(self) -> dict[str, Any]:
        today = datetime.now().date().isoformat()
        week = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
        month = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
        applied_statuses = "('SUBMITTED','CONFIRMATION_RECEIVED','ASSESSMENT','INTERVIEW','FINAL_INTERVIEW','OFFER','REJECTED')"
        def count(where: str, params: tuple = ()) -> int:
            r = self._one(f"SELECT COUNT(*) AS n FROM applications a WHERE {where}", params)
            return int(r["n"]) if r else 0
        by_status = {r["status"]: r["n"] for r in self._all("SELECT status, COUNT(*) AS n FROM applications GROUP BY status")}
        return {
            "total_applications": count(f"a.status IN {applied_statuses}"),
            "applications_today": count(f"a.status IN {applied_statuses} AND a.date_applied >= ?", (today,)),
            "applications_this_week": count(f"a.status IN {applied_statuses} AND a.date_applied >= ?", (week,)),
            "applications_this_month": count(f"a.status IN {applied_statuses} AND a.date_applied >= ?", (month,)),
            "running": count("a.status IN ('APPLYING')"),
            "needs_action": count("a.status IN ('NEEDS_INPUT','FAILED') OR a.id IN (SELECT application_id FROM actions WHERE status='open')"),
            "assessments": by_status.get("ASSESSMENT", 0),
            "interviews": by_status.get("INTERVIEW", 0) + by_status.get("FINAL_INTERVIEW", 0),
            "rejections": by_status.get("REJECTED", 0),
            "offers": by_status.get("OFFER", 0),
            "by_status": by_status,
            "open_actions": len(self.actions("open")),
            "unread_notifications": len(self.notifications(unread_only=True)),
        }
