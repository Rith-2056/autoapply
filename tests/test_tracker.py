from datetime import datetime
from pathlib import Path

import pytest

from autoapply.tracker import ApplicationStatus as S, EventType as E, InvalidTransition, Tracker, can_transition, status_after_event
from autoapply.tracker.dedupe import external_job_id, find_duplicate, normalize_url


# ---- state machine -------------------------------------------------------

@pytest.mark.parametrize("frm,to,ok", [
    (S.APPLYING, S.SUBMITTED, True), (S.SUBMITTED, S.ASSESSMENT, True), (S.ASSESSMENT, S.INTERVIEW, True),
    (S.INTERVIEW, S.REJECTED, True), (S.SUBMITTED, S.REJECTED, True), (S.APPLYING, S.NEEDS_INPUT, True), (S.NEEDS_INPUT, S.APPLYING, True),
    (S.REJECTED, S.INTERVIEW, False), (S.OFFER, S.SUBMITTED, False), (S.SUBMITTED, S.APPLYING, False), (S.DISCOVERED, S.OFFER, False),
    (S.INTERVIEW, S.INTERVIEW, True),
])
def test_transitions(frm, to, ok):
    assert can_transition(frm, to) is ok


def test_status_after_event_forward_and_stale():
    assert status_after_event(S.APPLYING, E.APPLICATION_SUBMITTED) == S.SUBMITTED
    assert status_after_event(S.SUBMITTED, E.ASSESSMENT_RECEIVED) == S.ASSESSMENT
    assert status_after_event(S.ASSESSMENT, E.INTERVIEW_INVITATION_RECEIVED) == S.INTERVIEW
    assert status_after_event(S.INTERVIEW, E.REJECTED) == S.REJECTED
    # a late confirmation email never moves an application backwards
    assert status_after_event(S.ASSESSMENT, E.CONFIRMATION_EMAIL_RECEIVED) == S.ASSESSMENT
    # a rejected application is not revived by an old email
    assert status_after_event(S.REJECTED, E.ASSESSMENT_RECEIVED) == S.REJECTED
    # no-status events leave it alone
    assert status_after_event(S.SUBMITTED, E.RECRUITER_CONTACT) == S.SUBMITTED
    with pytest.raises(InvalidTransition):
        status_after_event(S.DISCOVERED, E.STATUS_SET_MANUALLY, {"status": "OFFER"})


# ---- dedupe ----------------------------------------------------------------

def test_normalize_url_and_ids():
    assert normalize_url("https://www.job-boards.greenhouse.io/x/jobs/123?utm_source=Simplify&ref=Simplify") == normalize_url("https://job-boards.greenhouse.io/x/jobs/123/")
    assert external_job_id("https://job-boards.greenhouse.io/x/jobs/8170944?gh_src=abc") == "8170944"
    assert external_job_id("https://jobs.lever.co/acme/4efbdbce-a753-41b5-8ed7-0661cd193178/apply") == "4efbdbce-a753-41b5-8ed7-0661cd193178"
    assert external_job_id("https://gdit.wd5.myworkdayjobs.com/x/job/USA/Intern_RQ228405") == "rq228405"


def test_find_duplicate_signals():
    existing = [
        {"id": 1, "company": "Microsoft", "title": "Software Engineer Intern", "location": "Redmond, WA", "url": "https://jobs.careers.microsoft.com/job/1234567/"},
        {"id": 2, "company": "Microsoft", "title": "Product Manager Intern", "location": "Redmond, WA", "url": "https://jobs.careers.microsoft.com/job/7654321/"},
    ]
    assert find_duplicate({"company": "Microsoft", "title": "SWE Intern", "location": "", "url": "https://jobs.careers.microsoft.com/job/1234567?utm_source=x"}, existing).job_id == 1
    assert find_duplicate({"company": "Microsoft", "title": "Software Engineer Intern", "location": "Redmond, WA", "url": "https://linkedin.com/jobs/view/999"}, existing).job_id == 1
    assert find_duplicate({"company": "Microsoft", "title": "Software Engineer Intern - Summer 2027", "location": "Redmond, WA", "url": "https://other.com/x"}, existing).job_id == 1
    assert find_duplicate({"company": "Microsoft", "title": "Data Scientist Intern", "location": "Redmond, WA", "url": "https://other.com/y"}, existing) is None
    assert find_duplicate({"company": "Google", "title": "Software Engineer Intern", "location": "Redmond, WA", "url": "https://other.com/z"}, existing) is None


# ---- store -------------------------------------------------------------------

def test_store_events_questions_actions(tmp_path: Path):
    t = Tracker(tmp_path / "t.db")
    job = t.upsert_job("Google", "SWE Intern", "https://boards.greenhouse.io/google/jobs/1", "NYC", source="simplify", listing_id="L1")
    assert t.upsert_job("Google", "SWE Intern", "https://boards.greenhouse.io/google/jobs/1?utm_source=x", "NYC")["id"] == job["id"]
    app = t.create_application(job["id"], ats="greenhouse")
    assert app["status"] == "DISCOVERED"
    t.record_event(app["id"], E.APPLICATION_STARTED, "start")
    t.record_event(app["id"], E.INPUT_REQUESTED, "q")
    assert t.get_application(app["id"])["status"] == "NEEDS_INPUT"
    q = t.add_question(app["id"], "Why Google?", "aa3", "textarea", "company_motivation", required=True)
    t.add_answer(q["id"], "Because of X", "voice", raw="because of x", cleaned="Because of X", confidence="high", ai_modified=True, user_approved=True)
    t.record_event(app["id"], E.ANSWER_PROVIDED)
    ev = t.record_event(app["id"], E.APPLICATION_SUBMITTED, "done")
    assert ev["status_changed"] and t.get_application(app["id"])["date_applied"]
    t.record_event(app["id"], E.ASSESSMENT_RECEIVED, "OA")
    act = t.add_action(app["id"], "assessment", "Complete OA", deadline="2030-01-01T23:59:00", priority="high")
    assert t.add_action(app["id"], "assessment", "Complete OA")["id"] == act["id"]  # no duplicate open action
    rows = t.list_applications(search="needs action")
    assert rows and rows[0]["next_action"] == "Complete OA"
    assert t.list_applications(status="ASSESSMENT")[0]["id"] == app["id"]
    assert t.list_applications(search="assessment")[0]["id"] == app["id"]
    assert t.list_applications(company="goog")[0]["id"] == app["id"]
    t.update_action(act["id"], status="done")
    assert t.get_application(app["id"])["next_action"] == ""
    m = t.metrics()
    assert m["total_applications"] == 1 and m["assessments"] == 1
    qs = t.questions(app["id"])
    assert qs[0]["answers"][0]["raw_transcription"] == "because of x"
    assert len(t.events(app["id"])) == 6
    n = t.notify("email", "New assessment", "from Google", app["id"])
    assert t.notifications(unread_only=True)[0]["id"] == n["id"]
    t.mark_notification_read(n["id"])
    assert not t.notifications(unread_only=True)
    t.close()


def test_legacy_migration(tmp_path: Path):
    from autoapply.db import ApplicationRecord, Database
    from autoapply.tracker.migrate import migrate_legacy

    legacy = tmp_path / "applications.db"
    with Database(legacy) as db:
        db.insert(ApplicationRecord("L1", "Acme", "SWE Intern", "https://jobs.lever.co/acme/1", "applied", "NYC", "lever",
                                    answers=[{"label": "Email", "value": "a@b.c", "source": "profile", "status": "filled", "kind": "text", "field_id": "aa0"}]))
        db.insert(ApplicationRecord("L2", "Beta", "ML Intern", "u2", "needs_manual", error="needs you", unanswered_questions=["Why Beta? [company_motivation, required] — draft"]))
        db.insert(ApplicationRecord("L3", "Gamma", "DS Intern", "u3", "failed", error="boom"))
        db.insert(ApplicationRecord("L4", "Delta", "X", "u4", "dry_run"))
    t = Tracker(tmp_path / "tracker.db")
    assert migrate_legacy(t, legacy) == 4
    assert migrate_legacy(t, legacy) == 0  # idempotent
    by = {a["company"]: a for a in t.list_applications()}
    assert by["Acme"]["status"] == "SUBMITTED" and by["Acme"]["date_applied"]
    assert by["Beta"]["status"] == "NEEDS_INPUT"
    assert by["Gamma"]["status"] == "FAILED" and by["Gamma"]["error"] == "boom"
    assert by["Delta"]["status"] == "READY_TO_APPLY"
    assert t.questions(by["Acme"]["id"])[0]["answers"][0]["final_answer"] == "a@b.c"
    assert t.questions(by["Beta"]["id"])[0]["status"] == "pending"
