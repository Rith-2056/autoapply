import csv
from pathlib import Path

import pytest

from autoapply.db import ApplicationRecord, Database


def _rec(**kw):
    base = dict(listing_id="L1", company="Acme", role="SWE Intern", url="https://jobs.lever.co/acme/1", status="applied", ats="lever")
    base.update(kw)
    return ApplicationRecord(**base)


def test_insert_and_query(tmp_path: Path):
    db = Database(tmp_path / "x.db")
    rid = db.insert(_rec(unanswered_questions=["Why us?"], answers=[{"label": "Email", "value": "a@b.c", "source": "profile"}], screenshot_path="s.png"))
    assert rid == 1
    rows = db.query()
    assert len(rows) == 1
    r = rows[0]
    assert r.company == "Acme" and r.status == "applied"
    assert r.unanswered_questions == ["Why us?"]
    assert r.answers[0]["value"] == "a@b.c"
    assert r.screenshot_path == "s.png"
    db.close()


def test_invalid_status_rejected():
    with pytest.raises(ValueError):
        _rec(status="bogus")


def test_filters(tmp_path: Path):
    db = Database(tmp_path / "x.db")
    db.insert(_rec(timestamp="2026-09-01T10:00:00", status="applied"))
    db.insert(_rec(listing_id="L2", company="Beta", role="ML Intern", url="u2", status="failed", timestamp="2026-09-10T10:00:00", error="boom"))
    db.insert(_rec(listing_id="L3", company="Gamma", url="u3", status="needs_manual", timestamp="2026-09-15T10:00:00", run_id="r1"))
    assert [r.company for r in db.query()] == ["Gamma", "Beta", "Acme"]
    assert [r.company for r in db.query(status="failed")] == ["Beta"]
    assert [r.company for r in db.query(company="gam")] == ["Gamma"]
    assert [r.company for r in db.query(since="2026-09-05")] == ["Gamma", "Beta"]
    assert [r.company for r in db.query(until="2026-09-10")] == ["Beta", "Acme"]
    assert [r.company for r in db.query(run_id="r1")] == ["Gamma"]
    assert [r.company for r in db.query(search="ml intern")] == ["Beta"]
    assert len(db.query(limit=2)) == 2
    counts = db.counts_by_status()
    assert counts["applied"] == 1 and counts["failed"] == 1 and counts["needs_manual"] == 1 and counts["skipped"] == 0


def test_already_handled_and_retry(tmp_path: Path):
    db = Database(tmp_path / "x.db")
    assert not db.already_handled("L1", "u")
    db.insert(_rec(status="dry_run"))
    assert not db.already_handled("L1"), "dry runs never count as handled"
    db.insert(_rec(status="failed"))
    assert db.already_handled("L1")
    assert not db.already_handled("L1", retry_statuses=("failed",))
    db.insert(_rec(status="applied"))
    assert db.already_handled("L1", retry_statuses=("failed", "needs_manual", "skipped"))
    # match by URL when listing id differs
    assert db.already_handled("other-id", "https://jobs.lever.co/acme/1")
    assert db.latest_status("L1") == "applied"
    assert db.latest_status("nope") is None


def test_runs_table(tmp_path: Path):
    db = Database(tmp_path / "x.db")
    db.start_run("r1", "review", False, 5)
    db.finish_run("r1", 2)
    runs = db.runs()
    assert runs[0]["run_id"] == "r1" and runs[0]["attempted"] == 2 and runs[0]["listings_considered"] == 5


def test_export_csv(tmp_path: Path):
    db = Database(tmp_path / "x.db")
    db.insert(_rec(unanswered_questions=["Q1", "Q2"]))
    db.insert(_rec(listing_id="L2", url="u2", status="skipped"))
    out = tmp_path / "out" / "apps.csv"
    n = db.export_csv(out)
    assert n == 2 and out.exists()
    with out.open() as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["company"] == "Acme"
    assert {r["status"] for r in rows} == {"applied", "skipped"}
    assert any(r["unanswered_questions"] == "Q1 | Q2" for r in rows)
    assert db.export_csv(tmp_path / "f.csv", status="skipped") == 1
