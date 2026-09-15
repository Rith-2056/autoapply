import json
from pathlib import Path

import pytest

from autoapply.listings import (
    Listing,
    ListingFilters,
    filter_listings,
    load_listings,
    load_listings_json,
    matches,
    normalize_category,
    parse_listing,
    parse_readme,
)

FIXTURE = Path(__file__).parent / "fixtures" / "listings_sample.json"

README_SAMPLE = """
<table>
<thead><tr><th>Company</th><th>Role</th><th>Location</th><th>Application</th><th>Age</th></tr></thead>
<tbody>
<tr>
<td>🔥 <strong><a href="https://simplify.jobs/c/DoorDash?utm_source=GHList&utm_medium=company">DoorDash</a></strong></td>
<td>Software Engineer Intern</td>
<td>SF<br>Mountain View, CA</td>
<td><div align="center"><a href="https://job-boards.greenhouse.io/doordashcanada/jobs/8170944?utm_source=Simplify&ref=Simplify"><img src="x" alt="Apply"></a> <a href="https://simplify.jobs/p/abc?utm_source=GHList"><img src="y" alt="Simplify"></a></div></td>
<td>0d</td>
</tr>
<tr>
<td>↳</td>
<td>Data Science Intern</td>
<td>Seattle, WA</td>
<td><div align="center"><a href="https://job-boards.greenhouse.io/doordashcanada/jobs/999?utm_source=Simplify&ref=Simplify"><img src="x" alt="Apply"></a></div></td>
<td>1d</td>
</tr>
<tr>
<td><strong><a href="https://simplify.jobs/c/Acme">Acme</a></strong></td>
<td>Closed Intern 🔒</td>
<td>NYC</td>
<td><div align="center"><a href="https://jobs.lever.co/acme/123"><img src="x" alt="Apply"></a></div></td>
<td>5d</td>
</tr>
</tbody>
</table>
"""


def test_parse_listing_maps_fields():
    raw = {
        "id": "abc",
        "company_name": " Foo Inc ",
        "title": "SWE Intern",
        "url": "https://job-boards.greenhouse.io/foo/jobs/1",
        "locations": ["Boston, MA", "Remote in USA"],
        "category": "Software",
        "terms": ["Summer 2027"],
        "sponsorship": "Other",
        "degrees": ["Bachelor's"],
        "date_posted": 1789430400,
        "date_updated": 1789430400,
        "active": True,
        "is_visible": True,
        "source": "Simplify",
        "company_url": "https://simplify.jobs/c/Foo",
    }
    l = parse_listing(raw)
    assert l.company == "Foo Inc"
    assert l.location == "Boston, MA; Remote in USA"
    assert l.category == "Software"
    assert l.active and l.is_visible
    assert l.posted.year == 2026


def test_normalize_category_aliases():
    assert normalize_category("software") == "Software"
    assert normalize_category("ML/AI") == "AI/ML/Data"
    assert normalize_category("Data Science") == "AI/ML/Data"
    assert normalize_category("Quant") == "Quant"
    assert normalize_category("Software Engineering") == "Software"
    assert normalize_category("Weird") == "Weird"


def test_load_fixture_and_filter():
    listings = load_listings_json(FIXTURE)
    assert len(listings) == 9
    f = ListingFilters(
        term="Summer 2027",
        categories=["Software", "AI/ML/Data"],
        exclude_title_keywords=["PhD"],
        exclude_sponsorship=["U.S. Citizenship is Required"],
        require_degree="Bachelor's",
    )
    kept = filter_listings(listings, f)
    companies = {l.company for l in kept}
    # inactive, wrong term, hardware, quant, citizenship-required and PhD are gone
    assert "Scottish Water" not in companies
    assert "Citadel" not in companies
    assert "IMC Trading" not in companies
    assert all(l.active for l in kept)
    assert all(any("Summer 2027" in t for t in l.terms) for l in kept)
    assert all(l.category in ("Software", "AI/ML/Data") for l in kept)
    # newest first
    dates = [l.date_posted for l in kept]
    assert dates == sorted(dates, reverse=True)


def test_matches_reasons():
    base = dict(id="1", company="X", title="SWE Intern", url="https://jobs.lever.co/x/1", terms=["Summer 2027"], category="Software", active=True)
    f = ListingFilters(categories=["Software"], locations=["Boston"], exclude_companies=["evil"])
    assert matches(Listing(**base, locations=["Boston, MA"]), f) == (True, "")
    assert matches(Listing(**base, locations=["Austin, TX"]), f)[1] == "location"
    assert matches(Listing(**{**base, "company": "Evil Corp"}, locations=["Boston, MA"]), f)[1] == "excluded company"
    assert matches(Listing(**{**base, "active": False}), f)[1] == "inactive"
    assert matches(Listing(**{**base, "terms": ["Fall 2026"]}), f)[1].startswith("term")
    assert matches(Listing(**{**base, "category": "Hardware"}), f)[1].startswith("category")
    f2 = ListingFilters(title_keywords=["machine learning"])
    assert matches(Listing(**base), f2)[1] == "title keywords"
    assert matches(Listing(**{**base, "title": "Machine Learning Intern"}), f2)[0]


def test_filters_from_settings():
    data = {
        "listings": {"term": "Summer 2027"},
        "filters": {"categories": ["software", "quant"], "exclude_companies": ["Foo"], "max_applications_per_run": 3},
    }
    f = ListingFilters.from_settings(data)
    assert f.categories == ["Software", "Quant"]
    assert f.exclude_companies == ["Foo"]
    assert f.term == "Summer 2027"


def test_parse_readme_fallback():
    rows = parse_readme(README_SAMPLE)
    assert [r.company for r in rows] == ["DoorDash", "DoorDash"]  # 🔒 row dropped, ↳ inherits company
    assert rows[0].title == "Software Engineer Intern"
    assert rows[0].locations == ["SF", "Mountain View, CA"]
    assert rows[0].url == "https://job-boards.greenhouse.io/doordashcanada/jobs/8170944"
    assert rows[1].title == "Data Science Intern"
    closed = parse_readme(README_SAMPLE, active_only=False)
    assert len(closed) == 3 and closed[2].active is False and closed[2].title == "Closed Intern"


def test_load_listings_prefers_json_then_readme(tmp_path: Path):
    (tmp_path / "README.md").write_text(README_SAMPLE, encoding="utf-8")
    rows, source = load_listings(tmp_path)
    assert source == "README.md" and len(rows) == 2
    jdir = tmp_path / ".github" / "scripts"
    jdir.mkdir(parents=True)
    (jdir / "listings.json").write_text(json.dumps(json.loads(FIXTURE.read_text())[:2]))
    rows, source = load_listings(tmp_path)
    assert source.endswith("listings.json") and len(rows) == 2
    with pytest.raises(FileNotFoundError):
        load_listings(tmp_path / "nope")
