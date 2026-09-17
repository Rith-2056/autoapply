"""API smoke tests with an isolated tracker and no background threads."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autoapply.config import Settings, load_settings
from autoapply.tracker import EventType as E, Tracker
from autoapply.web.app import AppState, create_app


@pytest.fixture
def client(tmp_path: Path):
    settings = load_settings()
    settings.data.setdefault("email", {})["enabled"] = False
    settings.data.setdefault("run", {})["database"] = str(tmp_path / "legacy.db")
    st = AppState(settings=settings, tracker=Tracker(tmp_path / "t.db"))
    app = create_app(st)
    with TestClient(app) as c:
        yield c, st


def seed(t: Tracker):
    job = t.upsert_job("Google", "SWE Intern", "https://boards.greenhouse.io/google/jobs/1", "NYC")
    app = t.create_application(job["id"], ats="greenhouse")
    t.record_event(app["id"], E.APPLICATION_STARTED)
    t.record_event(app["id"], E.APPLICATION_SUBMITTED)
    return app


def test_dashboard_and_applications(client):
    c, st = client
    app = seed(st.tracker)
    d = c.get("/api/dashboard").json()
    assert d["metrics"]["total_applications"] == 1 and d["session"]["session"] is None
    rows = c.get("/api/applications?search=google").json()
    assert rows[0]["status_label"] == "Submitted"
    detail = c.get(f"/api/applications/{app['id']}").json()
    assert detail["application"]["company"] == "Google" and len(detail["events"]) == 3
    r = c.patch(f"/api/applications/{app['id']}", json={"status": "ASSESSMENT"})
    assert r.status_code == 200 and r.json()["status"] == "ASSESSMENT"
    r = c.patch(f"/api/applications/{app['id']}", json={"status": "DISCOVERED"})
    assert r.status_code == 400  # invalid transition surfaced, not silently applied
    assert c.get("/api/applications/999").status_code == 404


def test_actions_notifications_answers(client):
    c, st = client
    app = seed(st.tracker)
    a = c.post("/api/actions", json={"application_id": app["id"], "title": "Complete OA", "priority": "high", "deadline": "2030-01-01T10:00"}).json()
    assert c.get("/api/actions").json()[0]["id"] == a["id"]
    assert c.get("/api/applications?needs_action=true").json()[0]["id"] == app["id"]
    c.patch(f"/api/actions/{a['id']}", json={"status": "done"})
    assert c.get("/api/actions").json() == []
    q = st.tracker.add_question(app["id"], "Why Google?", "aa1", "textarea", "company_motivation", required=True)
    r = c.post(f"/api/applications/{app['id']}/answers", json={"question_id": q["id"], "answer": "Because search.", "source": "typed", "save_reusable": True})
    assert r.json()["queued"] is False
    assert c.get("/api/approved-answers?question=Why%20do%20you%20want%20to%20work%20at%20Google").json()[0]["answer"] == "Because search."
    st.tracker.notify("email", "New assessment", "x", app["id"])
    assert len(c.get("/api/notifications?unread=true").json()) == 1
    c.post("/api/notifications/read_all")
    assert c.get("/api/notifications?unread=true").json() == []


def test_voice_clean_and_session_controls(client):
    c, st = client
    r = c.post("/api/voice/clean", json={"raw": "i built the data lack with pie torch", "question": "Tell us about a project"}).json()
    assert r["cleaned"] == "I built the data lake with PyTorch."
    assert c.get("/api/autoapply/current").json()["running"] is False
    assert c.post("/api/autoapply/1/pause").status_code == 409
    assert c.post("/api/autoapply/1/command", json={"type": "bogus"}).status_code == 400
    assert c.get("/api/email/status").json()["connected"] is False
    assert c.post("/api/email/sync").status_code == 400
    s = c.get("/api/settings").json()
    assert "filters" in s and "has_anthropic_key" in s and "ANTHROPIC" not in str(s)
    assert c.get("/").status_code == 200 and "AutoApplier" in c.get("/").text


def test_platforms_endpoints(client, tmp_path, monkeypatch):
    c, st = client
    import autoapply.platforms as pl
    import autoapply.web.app as web

    monkeypatch.setattr(web, "PlatformProfiles", lambda: pl.PlatformProfiles(tmp_path / "platforms.yaml"))
    d = c.get("/api/platforms").json()
    assert set(d["platforms"]) >= {"workday", "greenhouse", "lever", "ashby", "smartrecruiters"}
    assert any(f["path"] == "education.major" for f in d["platforms"]["workday"]["fields"])
    r = c.patch("/api/platforms/workday", json={"fields": {"education.major": "Computer Science"}, "answers": {"Do you have a Workday account?": "Yes"}}).json()
    assert next(f for f in r["fields"] if f["path"] == "education.major")["override"] == "Computer Science"
    assert r["answers"]["Do you have a Workday account?"] == "Yes"
    assert c.patch("/api/platforms/nope", json={"fields": {}}).status_code == 404
