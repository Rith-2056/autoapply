from datetime import datetime

import pytest

from autoapply.mail.classify import EmailClassifier, EmailMessage, rule_classify
from autoapply.mail.deadlines import parse_deadline
from autoapply.mail.match import match_email
from autoapply.mail.monitor import EmailProcessor
from autoapply.tracker import EventType as E, Tracker

REF = datetime(2026, 9, 17, 10, 0)


def msg(subject, body, sender="recruiting@example.com", urls=None, when=REF):
    return EmailMessage("id-" + subject[:10].replace(" ", "_"), subject, sender, body, when, urls=urls or [])


# ---- deadlines -------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Please complete the assessment within 7 days.", datetime(2026, 9, 24, 23, 59)),
    ("You have 48 hours to complete it: within 48 hours", datetime(2026, 9, 19, 10, 0)),
    ("Complete by September 24", datetime(2026, 9, 24, 23, 59)),
    ("Complete by Sept 24, 2026 at 5pm", datetime(2026, 9, 24, 17, 0)),
    ("Due 24 September", datetime(2026, 9, 24, 23, 59)),
    ("The assessment expires on 10/01", datetime(2026, 10, 1, 23, 59)),
    ("Please respond by Friday", datetime(2026, 9, 18, 23, 59)),
    ("Schedule your interview by tomorrow", datetime(2026, 9, 18, 23, 59)),
    ("by end of day", datetime(2026, 9, 17, 23, 59)),
    ("within two business days", datetime(2026, 9, 21, 23, 59)),
    ("Please complete by January 5", datetime(2027, 1, 5, 23, 59)),
    ("Thanks for applying! We will be in touch.", None),
])
def test_parse_deadline(text, expected):
    assert parse_deadline(text, REF) == expected


# ---- classification ----------------------------------------------------------

def test_rule_classification_categories():
    cases = {
        "APPLICATION_CONFIRMATION": msg("Thank you for applying to Example Company", "We received your application for the Software Engineer Intern position."),
        "ASSESSMENT": msg("Example Company: Online Assessment", "Thank you for applying to Example Company for the Software Engineer Intern position. We'd like you to complete a HackerRank assessment within 7 days.", "no-reply@hackerrank.com"),
        "INTERVIEW": msg("Interview invitation - Software Engineer Intern", "We would like to invite you to an interview with the team next week. Please confirm."),
        "INTERVIEW_SCHEDULING": msg("Schedule your interview with Microsoft", "Please use the link below to schedule your interview: https://calendly.com/x"),
        "REJECTION": msg("Update on your application", "Thank you for your interest. Unfortunately, we have decided to move forward with other candidates."),
        "RECRUITER_CONTACT": msg("Quick chat?", "Hi! I'm a recruiter at Meta and would love to connect with you about our internship roles."),
        "OFFER": msg("Your offer from Acme", "We are pleased to offer you the Software Engineer Intern position."),
    }
    for expected, m in cases.items():
        assert rule_classify(m).category == expected, m.subject
    irrelevant = msg("Your weekly job alert: 25 new jobs", "Jobs you may like based on your profile. Unsubscribe from job alerts.", "jobs-noreply@linkedin.com")
    assert rule_classify(irrelevant).category == "IRRELEVANT" and not rule_classify(irrelevant).job_related
    personal = msg("Dinner on Friday?", "Hey, want to grab dinner?", "friend@gmail.com")
    assert not rule_classify(personal).job_related


def test_extraction_from_assessment_email():
    m = msg("Example Company: Online Assessment", "Thank you for applying to Example Company for the Software Engineer Intern position.\nWe'd like you to complete a HackerRank assessment within 7 days.", "talent@examplecompany.com",
            urls=["https://www.hackerrank.com/tests/abc"])
    c = rule_classify(m)
    assert c.category == "ASSESSMENT" and c.company == "Example Company" and "Software Engineer Intern" in c.role
    assert c.platform == "hackerrank" and c.action_required and c.deadline == datetime(2026, 9, 24, 23, 59) and c.priority == "high"


def test_llm_result_is_merged_and_deadline_parsed():
    clf = EmailClassifier(api_key="", use_llm=False)
    m = msg("Next steps", "Hi Divyarith, here is the next step in the process.", "people@acme.com")
    c = clf.classify(m, llm_result={"category": "ASSESSMENT", "confidence": 0.9, "company": "Acme", "role": "SWE Intern", "platform": "CodeSignal",
                                     "action_required": True, "action_title": "Complete CodeSignal", "deadline_text": "by September 24", "priority": "high", "summary": "OA"})
    assert c.category == "ASSESSMENT" and c.source == "llm" and c.deadline == datetime(2026, 9, 24, 23, 59) and c.platform == "codesignal"


# ---- matching ----------------------------------------------------------------

APPS = [
    {"id": 1, "company": "Google", "title": "Software Engineer Intern", "job_url": "https://careers.google.com/jobs/results/1234567", "date_applied": "2026-09-10T10:00:00", "status": "SUBMITTED"},
    {"id": 2, "company": "Google", "title": "Data Scientist Intern", "job_url": "https://careers.google.com/jobs/results/7654321", "date_applied": "2026-09-11T10:00:00", "status": "SUBMITTED"},
    {"id": 3, "company": "Microsoft", "title": "Software Engineer Intern", "job_url": "https://jobs.careers.microsoft.com/job/111111", "date_applied": "2026-09-12T10:00:00", "status": "SUBMITTED"},
    {"id": 4, "company": "Stripe", "title": "Backend Engineer Intern", "job_url": "https://stripe.com/jobs/listing/x/5555555", "date_applied": "2026-09-13T10:00:00", "status": "SUBMITTED"},
]


def test_match_exact_company_and_role():
    r = match_email("Microsoft", "Software Engineer Intern", "noreply@microsoft.com", "Your Microsoft application", "…", [], APPS, REF)
    assert r.status == "auto" and r.application_id == 3


def test_match_company_only_single_application_asks_or_links():
    r = match_email("Stripe", "", "recruiting@stripe.com", "Update from Stripe", "Thanks for applying to Stripe.", [], APPS, REF)
    assert r.status in ("auto", "needs_confirmation") and r.candidates[0].application_id == 4


def test_match_two_applications_same_company_is_ambiguous_without_role():
    r = match_email("Google", "", "noreply@google.com", "Your application", "Thank you for applying to Google.", [], APPS, REF)
    assert r.status == "needs_confirmation" and {c.application_id for c in r.candidates[:2]} == {1, 2}


def test_match_role_disambiguates_same_company():
    r = match_email("Google", "Data Scientist Intern", "noreply@google.com", "Data Scientist Intern - next steps", "…", [], APPS, REF)
    assert r.status == "auto" and r.application_id == 2


def test_match_job_id_beats_similar_roles():
    r = match_email("", "", "no-reply@greenhouse-mail.io", "Next steps", "Requisition 5555555 – please schedule", ["https://stripe.com/jobs/listing/x/5555555"], APPS, REF)
    assert r.status == "auto" and r.application_id == 4


def test_match_unrelated_email_is_unmatched():
    r = match_email("Netflix", "SWE Intern", "recruiting@netflix.com", "Netflix application", "Thanks for applying to Netflix", [], APPS, REF)
    assert r.status == "unmatched" and r.application_id is None


def test_email_before_application_is_penalised():
    early = datetime(2026, 9, 1, 9, 0)
    r = match_email("Microsoft", "Software Engineer Intern", "noreply@microsoft.com", "Microsoft", "…", [], APPS, early)
    assert r.status != "auto"


# ---- processor end to end ------------------------------------------------------

def test_processor_updates_status_creates_action_and_notification(tmp_path):
    t = Tracker(tmp_path / "t.db")
    job = t.upsert_job("Example Company", "Software Engineer Intern", "https://boards.greenhouse.io/example/jobs/42", "Remote")
    app = t.create_application(job["id"])
    t.record_event(app["id"], E.APPLICATION_STARTED)
    t.record_event(app["id"], E.APPLICATION_SUBMITTED, timestamp="2026-09-10T10:00:00")
    proc = EmailProcessor(t, EmailClassifier(api_key="", use_llm=False))
    m = msg("Example Company: Online Assessment", "Thank you for applying to Example Company for the Software Engineer Intern position.\nWe'd like you to complete a HackerRank assessment within 7 days.", "talent@examplecompany.com", urls=["https://www.hackerrank.com/tests/abc"])
    rec = proc.process(m)
    assert rec["match_status"] == "auto" and rec["application_id"] == app["id"]
    a = t.get_application(app["id"])
    assert a["status"] == "ASSESSMENT" and a["next_action"].startswith("Complete online assessment") and a["next_action_deadline"].startswith("2026-09-24")
    acts = t.actions("open")
    assert acts[0]["priority"] == "high" and "hackerrank" in acts[0]["url"]
    notes = t.notifications()
    assert any("assessment" in n["title"].lower() for n in notes)
    types = [e["type"] for e in t.events(app["id"])]
    assert "AssessmentReceived" in types and "DeadlineDetected" in types
    assert proc.process(m) is None  # idempotent
    # irrelevant mail is never stored
    assert proc.process(msg("Your weekly job alert", "Jobs you may like. Unsubscribe from job alerts.", "jobs@linkedin.com")) is None
    assert len(t.email_events()) == 1
    # rejection later
    proc.process(msg("Update on your application", "Thank you for applying to Example Company for the Software Engineer Intern position. Unfortunately we will not be moving forward.", "talent@examplecompany.com", when=datetime(2026, 9, 20, 9, 0)))
    assert t.get_application(app["id"])["status"] == "REJECTED"


def test_processor_low_confidence_needs_confirmation_then_confirm(tmp_path):
    t = Tracker(tmp_path / "t.db")
    for title in ("Software Engineer Intern", "Data Scientist Intern"):
        job = t.upsert_job("Google", title, f"https://careers.google.com/{title.replace(' ', '')}", "NYC")
        app = t.create_application(job["id"])
        t.record_event(app["id"], E.APPLICATION_STARTED)
        t.record_event(app["id"], E.APPLICATION_SUBMITTED, timestamp="2026-09-10T10:00:00")
    proc = EmailProcessor(t, EmailClassifier(api_key="", use_llm=False))
    rec = proc.process(msg("Interview invitation", "Thank you for applying to Google. We would like to invite you to an interview. Please confirm your availability.", "noreply@google.com"))
    assert rec["match_status"] == "needs_confirmation" and rec["application_id"] is None
    assert all(a["status"] == "SUBMITTED" for a in t.list_applications())
    target = t.list_applications(search="data scientist")[0]
    proc.confirm_match(rec["id"], target["id"])
    assert t.get_application(target["id"])["status"] == "INTERVIEW"
    assert t.get_email_event(rec["id"])["match_status"] == "confirmed"
