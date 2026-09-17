from pathlib import Path

from autoapply.eligibility import evaluate
from autoapply.listings import Listing, ListingFilters
from autoapply.tracker import EventType as E, Tracker


def L(company="Acme", title="Software Engineer Intern", locs=("Boston, MA",), url="https://boards.greenhouse.io/acme/jobs/1", cat="Software"):
    return Listing(id=url, company=company, title=title, url=url, locations=list(locs), category=cat, terms=["Summer 2027"], active=True)


F = ListingFilters(term="Summer 2027", categories=["Software"], exclude_companies=["Evil"])


def test_us_job_passes_all_checks(tmp_path: Path):
    t = Tracker(tmp_path / "t.db")
    g = evaluate(L(), F, t, ats_allow={"greenhouse"})
    assert g.eligible and g.failed == "" and g.location_verdict == "US"
    assert [c.name for c in g.checks] == ["us_location", "configured_criteria", "already_applied", "ats_supported"]


def test_non_us_and_ambiguous_jobs_are_rejected_before_any_browser_work(tmp_path: Path):
    t = Tracker(tmp_path / "t.db")
    for locs in (["Toronto, ON, Canada"], ["London, UK"], ["Bangalore, India"], ["United States / Canada"], ["Remote"], []):
        g = evaluate(L(locs=locs), F, t)
        assert not g.eligible and g.failed == "us_location", locs
    assert evaluate(L(locs=["Seattle, WA", "Toronto, ON"]), F, t).eligible  # a real US office is listed
    assert evaluate(L(locs=["Remote - US"]), F, t).eligible


def test_us_only_can_be_disabled_explicitly(tmp_path: Path):
    t = Tracker(tmp_path / "t.db")
    assert evaluate(L(locs=["Toronto, ON, Canada"]), F, t, us_only=False).eligible


def test_criteria_duplicates_and_ats(tmp_path: Path):
    t = Tracker(tmp_path / "t.db")
    assert evaluate(L(company="Evil Corp"), F, t).failed == "configured_criteria"
    assert evaluate(L(cat="Hardware"), F, t).failed == "configured_criteria"
    assert evaluate(L(url="https://gdit.wd5.myworkdayjobs.com/x/job/USA/Intern_RQ1"), F, t, ats_allow={"greenhouse"}).failed == "ats_supported"
    assert evaluate(L(locs=["Boston, MA"]), F, t, exclude_locations=["Boston"]).failed == "excluded_location"
    # already applied: same job seen via a different URL with tracking params
    job = t.upsert_job("Acme", "Software Engineer Intern", "https://boards.greenhouse.io/acme/jobs/1?utm_source=x", "Boston, MA")
    app = t.create_application(job["id"])
    t.record_event(app["id"], E.APPLICATION_STARTED)
    t.record_event(app["id"], E.APPLICATION_SUBMITTED)
    g = evaluate(L(), F, t)
    assert not g.eligible and g.failed == "already_applied" and "SUBMITTED" in g.reason
    # failed applications are retried only when asked
    job2 = t.upsert_job("Beta", "Software Engineer Intern", "https://jobs.lever.co/beta/1", "Austin, TX")
    app2 = t.create_application(job2["id"])
    t.record_event(app2["id"], E.APPLICATION_STARTED)
    t.record_event(app2["id"], E.APPLICATION_FAILED, "boom")
    l2 = L(company="Beta", url="https://jobs.lever.co/beta/1", locs=["Austin, TX"])
    assert evaluate(l2, F, t).failed == "already_applied"
    assert evaluate(l2, F, t, retry=True).eligible
