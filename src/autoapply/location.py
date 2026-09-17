"""US location validation.

Every job goes through ``classify_location`` *before* any browser automation:

  text -> split into individual locations -> normalise -> country per location
       -> verdict: US | NON_US | MIXED | UNKNOWN, plus ``eligible``.

Rules
- A location is US when it names the country, a US state or territory (name or
  abbreviation after a comma), a well-known US city, or "remote" tied to the US.
- A location is non-US when it names another country, a well-known non-US city,
  or a Canadian province abbreviation after a comma.
- Several specific US locations mixed with international offices are eligible
  (the US office is a real option). Country-level ambiguity ("United States /
  Canada", "US / UK") or bare "Remote" is UNKNOWN and is never auto-applied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC", "puerto rico": "PR", "guam": "GU", "u.s. virgin islands": "VI", "american samoa": "AS",
    "northern mariana islands": "MP",
}
US_STATE_ABBR = set(US_STATES.values())
# Abbreviations that also mean something else are only trusted after a comma ("Boston, MA").
US_CITIES = {
    "new york city", "nyc", "san francisco", "sf", "south sf", "bay area", "san francisco bay area", "silicon valley", "los angeles", "la",
    "seattle", "boston", "austin", "chicago", "atlanta", "denver", "dallas", "houston", "san jose", "san diego", "sunnyvale",
    "mountain view", "palo alto", "menlo park", "redwood city", "cupertino", "santa clara", "oakland", "berkeley", "redmond",
    "bellevue", "kirkland", "portland", "phoenix", "scottsdale", "tempe", "salt lake city", "las vegas", "miami", "tampa", "orlando",
    "washington dc", "washington d.c.", "arlington", "reston", "mclean", "baltimore", "philadelphia", "pittsburgh", "detroit",
    "ann arbor", "minneapolis", "st. louis", "saint louis", "kansas city", "nashville", "charlotte", "raleigh", "durham", "richmond",
    "cambridge", "somerville", "waltham", "burlington", "amherst", "worcester", "providence", "hartford", "stamford", "jersey city",
    "hoboken", "newark", "princeton", "brooklyn", "manhattan", "queens", "long island", "albany", "buffalo", "rochester", "ithaca",
    "columbus", "cincinnati", "cleveland", "indianapolis", "milwaukee", "madison", "boulder", "fort collins", "irvine", "santa monica",
    "pasadena", "san mateo", "foster city", "south san francisco", "fremont", "sacramento", "san antonio", "plano", "irving", "frisco",
    "huntsville", "birmingham", "new orleans", "omaha", "des moines", "boise", "albuquerque", "tucson", "honolulu", "anchorage",
    "hillsboro", "santa barbara", "san luis obispo", "chandler", "mesa", "folsom", "el segundo", "hawthorne", "long beach", "newport beach",
    "jacksonville", "fort lauderdale", "boca raton", "st. petersburg", "charlottesville", "blacksburg", "state college", "wilmington",
    "falls church", "chantilly", "herndon", "laurel", "columbia", "bethesda", "rockville", "gaithersburg", "annapolis", "norfolk",
    "west lafayette", "urbana", "champaign", "evanston", "naperville", "schaumburg", "lincoln", "louisville", "lexington", "knoxville",
    "chattanooga", "memphis", "little rock", "oklahoma city", "tulsa", "wichita", "fargo", "sioux falls", "cheyenne", "helena",
    "billings", "missoula", "spokane", "tacoma", "eugene", "reno", "provo", "ogden", "colorado springs", "aurora", "lakewood",
}
NON_US_COUNTRIES = {
    "canada", "united kingdom", "uk", "u.k.", "england", "scotland", "wales", "ireland", "germany", "france", "spain", "italy",
    "netherlands", "belgium", "switzerland", "austria", "sweden", "norway", "denmark", "finland", "poland", "czech republic",
    "czechia", "hungary", "romania", "portugal", "greece", "turkey", "israel", "uae", "united arab emirates", "saudi arabia",
    "india", "china", "japan", "south korea", "korea", "singapore", "hong kong", "taiwan", "vietnam", "thailand", "malaysia",
    "indonesia", "philippines", "australia", "new zealand", "mexico", "brazil", "argentina", "chile", "colombia", "peru",
    "south africa", "nigeria", "kenya", "egypt", "pakistan", "bangladesh", "sri lanka", "russia", "ukraine", "lithuania", "latvia",
    "estonia", "luxembourg", "europe", "emea", "apac", "latam", "asia", "africa", "worldwide", "global",
}
NON_US_CITIES = {
    "toronto", "vancouver", "montreal", "montréal", "ottawa", "calgary", "waterloo", "kitchener", "edmonton", "mississauga", "london",
    "manchester", "cambridge uk", "oxford", "edinburgh", "dublin", "cork", "paris", "berlin", "munich", "hamburg", "frankfurt",
    "amsterdam", "rotterdam", "zurich", "zürich", "geneva", "lausanne", "stockholm", "copenhagen", "oslo", "helsinki", "warsaw",
    "krakow", "prague", "vienna", "madrid", "barcelona", "lisbon", "milan", "rome", "tel aviv", "haifa", "dubai", "abu dhabi",
    "bangalore", "bengaluru", "hyderabad", "chennai", "mumbai", "pune", "delhi", "new delhi", "gurgaon", "gurugram", "noida",
    "kolkata", "beijing", "shanghai", "shenzhen", "hangzhou", "tokyo", "osaka", "seoul", "taipei", "sydney", "melbourne",
    "brisbane", "perth", "auckland", "wellington", "mexico city", "guadalajara", "monterrey", "são paulo", "sao paulo",
    "buenos aires", "santiago", "bogota", "bogotá", "lima", "cape town", "johannesburg", "nairobi", "lagos", "cairo", "singapore",
    "hong kong", "kuala lumpur", "jakarta", "manila", "ho chi minh city", "hanoi", "bangkok", "riga", "vilnius", "tallinn",
}
CANADIAN_PROVINCES = {"ON", "BC", "QC", "AB", "MB", "SK", "NS", "NB", "NL", "PE", "YT", "NT", "NU"}
_US_COUNTRY = re.compile(r"\b(united states(?: of america)?|u\.?s\.?a\.?|u\.s\.)(?=\W|$)", re.I)
_US_TOKEN = re.compile(r"(?<![A-Za-z])US(?![A-Za-z])")  # case-sensitive standalone "US"
_REMOTE = re.compile(r"\bremote\b|\bwork from home\b|\bwfh\b|\bvirtual\b", re.I)
_SPLIT = re.compile(r"\s*(?:;|\||\n|/|\band\b|\bor\b|•|·)\s*|\s*,\s*(?=(?:[A-Z][\w.' ]+,\s*[A-Z]{2}\b|[A-Z][a-z]+ [A-Z]{2}\b))", re.I)


@dataclass
class LocationResult:
    verdict: str  # US | NON_US | MIXED | UNKNOWN
    eligible: bool
    reason: str
    us_locations: list[str] = field(default_factory=list)
    non_us_locations: list[str] = field(default_factory=list)
    unknown_locations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _norm(s: str) -> str:
    s = s.strip().strip("()[]").replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s)
    return s


def country_of(loc: str) -> tuple[str, str]:
    """Return (country, detail) for ONE location string: US | NON_US | UNKNOWN."""
    raw = _norm(loc)
    if not raw:
        return "UNKNOWN", "empty"
    low = raw.lower()
    parts = [p.strip() for p in re.split(r"\s*[-,]\s*", raw) if p.strip()]
    low_parts = [p.lower() for p in parts]

    # A trailing US state code ("North Wales, PA") settles it before any country-word lookup,
    # unless an explicit non-US country is also named ("Toronto, ON, Canada" ends in Canada).
    if len(parts) >= 2 and len(parts[-1]) == 2 and parts[-1].upper() in US_STATE_ABBR and parts[-1].upper() not in CANADIAN_PROVINCES:
        return "US", f"state: {parts[-1].upper()}"

    # Explicit countries
    us_country = bool(_US_COUNTRY.search(raw) or _US_TOKEN.search(raw))
    non_us_hits = [c for c in NON_US_COUNTRIES if re.search(r"(?<![a-z])" + re.escape(c) + r"(?![a-z])", low)]
    if non_us_hits and not us_country:
        return "NON_US", f"country: {non_us_hits[0]}"
    if us_country and not non_us_hits:
        return "US", "country stated"
    if us_country and non_us_hits:
        return "UNKNOWN", f"both US and {non_us_hits[0]}"

    # State names / abbreviations (abbreviation must follow a comma or be the trailing token)
    for p in low_parts:
        if p in US_STATES:
            return "US", f"state: {p}"
    for i, p in enumerate(parts):
        if p.upper() in US_STATE_ABBR and len(p) == 2 and i > 0:
            return "US", f"state: {p.upper()}"
        if p.upper() in CANADIAN_PROVINCES and len(p) == 2 and i > 0 and p.upper() not in US_STATE_ABBR:
            return "NON_US", f"province: {p.upper()}"
    # Non-US cities
    for c in NON_US_CITIES:
        if c in low_parts or re.search(r"(?<![a-z])" + re.escape(c) + r"(?![a-z])", low) and c not in ("london",) :
            return "NON_US", f"city: {c}"
    if "london" in low_parts and not any(p.upper() in ("KY", "OH", "ON") for p in parts):
        # London, UK unless a US state is present; "London, ON" is Canada (handled above)
        return "NON_US", "city: london"
    # US cities
    for c in US_CITIES:
        if c in low_parts or low == c or (len(c) > 3 and re.search(r"(?<![a-z])" + re.escape(c) + r"(?![a-z])", low)):
            return "US", f"city: {c}"
    if _REMOTE.search(raw):
        return "UNKNOWN", "remote without a country"
    return "UNKNOWN", "unrecognised"


def split_locations(text: str) -> list[str]:
    if not text:
        return []
    # Keep "City, ST" pairs together: first split on strong separators only.
    chunks = re.split(r"\s*(?:;|\||\n|•|·)\s*|\s+/\s+|\s+(?:and|or)\s+", text)
    out: list[str] = []
    for ch in chunks:
        ch = ch.strip()
        if not ch:
            continue
        # "Seattle, WA, San Francisco, CA" -> split after a state code when another "City, ST" follows
        sub = re.split(r"(?<=[A-Z]{2}),\s*(?=[A-Z][A-Za-z .'-]+,\s*[A-Z]{2}(?:,|\s*$))", ch)
        out.extend(s.strip() for s in sub if s.strip())
    return out


def classify_location(text: str | list[str]) -> LocationResult:
    locs = text if isinstance(text, list) else split_locations(text)
    locs = [l for l in (_norm(x) for x in locs) if l]
    if not locs:
        return LocationResult("UNKNOWN", False, "no location given")
    us, non, unk = [], [], []
    details: dict[str, str] = {}
    for l in locs:
        c, d = country_of(l)
        details[l] = d
        (us if c == "US" else non if c == "NON_US" else unk).append(l)
    if us and not non:
        reason = "US: " + details[us[0]]
        if unk:
            reason += f" (also unrecognised: {', '.join(unk[:2])})"
        return LocationResult("US", True, reason, us, non, unk)
    if non and not us:
        return LocationResult("NON_US", False, "non-US: " + details[non[0]], us, non, unk)
    if us and non:
        # Specific US city/state alongside international offices -> the US office is a real option.
        specific = [l for l in us if not details[l].startswith("country")]
        if specific:
            return LocationResult("MIXED", True, f"US office listed ({specific[0]}) alongside international locations", us, non, unk)
        return LocationResult("UNKNOWN", False, "country-level mix (e.g. 'United States / Canada'); US eligibility unclear", us, non, unk)
    return LocationResult("UNKNOWN", False, "location unrecognised: " + ", ".join(unk[:3]), us, non, unk)
