"""SQLite persistence for application attempts."""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

STATUSES = ("applied", "failed", "needs_manual", "skipped", "dry_run")

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL,
    company TEXT NOT NULL,
    role TEXT NOT NULL,
    location TEXT,
    url TEXT NOT NULL,
    ats TEXT,
    status TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    run_id TEXT,
    mode TEXT,
    error TEXT,
    unanswered_questions TEXT,   -- JSON list of strings
    answers TEXT,                -- JSON list of {label, value, source}
    screenshot_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_applications_listing ON applications(listing_id);
CREATE INDEX IF NOT EXISTS idx_applications_url ON applications(url);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT,
    dry_run INTEGER,
    listings_considered INTEGER,
    attempted INTEGER
);
"""


@dataclass
class ApplicationRecord:
    listing_id: str
    company: str
    role: str
    url: str
    status: str
    location: str = ""
    ats: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    run_id: str = ""
    mode: str = ""
    error: str = ""
    unanswered_questions: list[str] = field(default_factory=list)
    answers: list[dict[str, Any]] = field(default_factory=list)
    screenshot_path: str = ""
    id: int | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"invalid status {self.status!r}; expected one of {STATUSES}")

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ApplicationRecord":
        return cls(
            id=row["id"],
            listing_id=row["listing_id"],
            company=row["company"],
            role=row["role"],
            location=row["location"] or "",
            url=row["url"],
            ats=row["ats"] or "",
            status=row["status"],
            timestamp=row["timestamp"],
            run_id=row["run_id"] or "",
            mode=row["mode"] or "",
            error=row["error"] or "",
            unanswered_questions=json.loads(row["unanswered_questions"] or "[]"),
            answers=json.loads(row["answers"] or "[]"),
            screenshot_path=row["screenshot_path"] or "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "company": self.company,
            "role": self.role,
            "location": self.location,
            "url": self.url,
            "ats": self.ats,
            "status": self.status,
            "run_id": self.run_id,
            "mode": self.mode,
            "error": self.error,
            "unanswered_questions": self.unanswered_questions,
            "answers": self.answers,
            "screenshot_path": self.screenshot_path,
            "listing_id": self.listing_id,
        }


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- writes ---------------------------------------------------------- #

    def insert(self, rec: ApplicationRecord) -> int:
        cur = self.conn.execute(
            """INSERT INTO applications
               (listing_id, company, role, location, url, ats, status, timestamp, run_id, mode,
                error, unanswered_questions, answers, screenshot_path)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec.listing_id,
                rec.company,
                rec.role,
                rec.location,
                rec.url,
                rec.ats,
                rec.status,
                rec.timestamp,
                rec.run_id,
                rec.mode,
                rec.error,
                json.dumps(rec.unanswered_questions),
                json.dumps(rec.answers),
                rec.screenshot_path,
            ),
        )
        self.conn.commit()
        rec.id = int(cur.lastrowid)
        return rec.id

    def start_run(self, run_id: str, mode: str, dry_run: bool, listings_considered: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at, mode, dry_run, listings_considered, attempted) VALUES (?,?,?,?,?,0)",
            (run_id, datetime.now().isoformat(timespec="seconds"), mode, int(dry_run), listings_considered),
        )
        self.conn.commit()

    def finish_run(self, run_id: str, attempted: int) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, attempted=? WHERE run_id=?",
            (datetime.now().isoformat(timespec="seconds"), attempted, run_id),
        )
        self.conn.commit()

    # ---- reads ----------------------------------------------------------- #

    def latest_status(self, listing_id: str, url: str = "") -> str | None:
        """Most recent status recorded for a listing (by id, or by URL)."""
        row = self.conn.execute(
            "SELECT status FROM applications WHERE listing_id=? OR (url=? AND ?!='') ORDER BY id DESC LIMIT 1",
            (listing_id, url, url),
        ).fetchone()
        return row["status"] if row else None

    def already_handled(self, listing_id: str, url: str = "", retry_statuses: Iterable[str] = ()) -> bool:
        """True if the listing should be skipped.

        A listing is skipped when it has any record other than a ``dry_run``
        (a dry run never submitted anything). Statuses listed in
        ``retry_statuses`` are treated as not handled so they can be retried.
        """
        status = self.latest_status(listing_id, url)
        if status is None or status == "dry_run":
            return False
        return status not in set(retry_statuses)

    def query(
        self,
        status: str | None = None,
        company: str | None = None,
        since: str | None = None,
        until: str | None = None,
        run_id: str | None = None,
        search: str | None = None,
        limit: int | None = None,
    ) -> list[ApplicationRecord]:
        sql = "SELECT * FROM applications WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if company:
            sql += " AND lower(company) LIKE ?"
            params.append(f"%{company.lower()}%")
        if since:
            sql += " AND timestamp >= ?"
            params.append(since)
        if until:
            sql += " AND timestamp <= ?"
            params.append(until + ("T23:59:59" if len(until) == 10 else ""))
        if run_id:
            sql += " AND run_id=?"
            params.append(run_id)
        if search:
            sql += " AND (lower(company) LIKE ? OR lower(role) LIKE ? OR lower(location) LIKE ?)"
            params.extend([f"%{search.lower()}%"] * 3)
        sql += " ORDER BY timestamp DESC, id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [ApplicationRecord.from_row(r) for r in self.conn.execute(sql, params).fetchall()]

    def counts_by_status(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM applications GROUP BY status").fetchall()
        counts = {s: 0 for s in STATUSES}
        for r in rows:
            counts[r["status"]] = r["n"]
        return counts

    def runs(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM runs ORDER BY started_at DESC").fetchall()]

    # ---- export ---------------------------------------------------------- #

    CSV_COLUMNS = [
        "id", "timestamp", "company", "role", "location", "url", "ats", "status",
        "run_id", "mode", "error", "unanswered_questions", "answers", "screenshot_path", "listing_id",
    ]

    def export_csv(self, out_path: Path | str, **filters: Any) -> int:
        records = self.query(**filters)
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.CSV_COLUMNS)
            w.writeheader()
            for rec in records:
                d = rec.to_dict()
                d["unanswered_questions"] = " | ".join(rec.unanswered_questions)
                d["answers"] = json.dumps(rec.answers, ensure_ascii=False)
                w.writerow({k: d.get(k, "") for k in self.CSV_COLUMNS})
        return len(records)
