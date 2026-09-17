import pytest

from autoapply.location import classify_location, country_of, split_locations


@pytest.mark.parametrize("text,verdict,eligible", [
    ("Boston, MA", "US", True),
    ("Boston, Massachusetts", "US", True),
    ("New York, NY, United States", "US", True),
    ("United States", "US", True),
    ("Remote - US", "US", True),
    ("Remote, USA", "US", True),
    ("US Remote", "US", True),
    ("Remote in USA", "US", True),
    ("Multiple Locations - United States", "US", True),
    ("Seattle, WA; SF; NYC", "US", True),
    ("Sunnyvale, CA", "US", True),
    ("Washington, D.C.", "US", True),
    ("San Juan, PR", "US", True),
    ("Toronto, ON, Canada", "NON_US", False),
    ("Toronto, ON", "NON_US", False),
    ("London, UK", "NON_US", False),
    ("London", "NON_US", False),
    ("Bangalore, India", "NON_US", False),
    ("Bengaluru", "NON_US", False),
    ("Berlin, Germany", "NON_US", False),
    ("Sydney", "NON_US", False),
    ("Remote - Canada", "NON_US", False),
    ("Vancouver, BC", "NON_US", False),
    ("United States / Canada", "UNKNOWN", False),
    ("US / UK", "UNKNOWN", False),
    ("Remote", "UNKNOWN", False),
    ("", "UNKNOWN", False),
    ("Seattle, WA; Toronto, ON, Canada", "MIXED", True),
    ("New York, NY | London, UK | Bangalore", "MIXED", True),
    ("Kitchener, Ontario", "NON_US", False),
    ("Cambridge, MA", "US", True),
    ("Amherst, MA", "US", True),
])
def test_classify_location(text, verdict, eligible):
    r = classify_location(text)
    assert (r.verdict, r.eligible) == (verdict, eligible), r.reason


def test_split_and_country_details():
    assert split_locations("Seattle, WA; Toronto, ON, Canada") == ["Seattle, WA", "Toronto, ON, Canada"]
    assert split_locations("Remote - US | New York, NY") == ["Remote - US", "New York, NY"]
    assert country_of("Austin, TX")[0] == "US"
    assert country_of("Ontario")[0] == "UNKNOWN"  # bare province name without country is not asserted either way
    assert classify_location(["Boston, MA", "Remote"]).eligible  # remote alongside a US office is fine
