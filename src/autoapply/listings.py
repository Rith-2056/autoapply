"""Fetch and parse internship listings from the SimplifyJobs repository.

Primary source: ``.github/scripts/listings.json`` (structured, one object per
listing). Fallback: the HTML table embedded in ``README.md``.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("autoapply.listings")

# Short category names -> canonical value used in listings.json
CATEGORY_ALIASES: dict[str, str] = {
    "software": "Software",
    "software engineering": "Software",
    "swe": "Software",
    "ai/ml/data": "AI/ML/Data",
    "ai": "AI/ML/Data",
    "ml": "AI/ML/Data",
    "ml/ai": "AI/ML/Data",
    "ai/ml": "AI/ML/Data",
    "machine learning": "AI/ML/Data",
    "data science": "AI/ML/Data",
    "data": "AI/ML/Data",
    "data science, ai & machine learning": "AI/ML/Data",
    "quant": "Quant",
    "quantitative finance": "Quant",
    "hardware": "Hardware",
    "hardware engineering": "Hardware",
    "product": "Product",
    "product management": "Product",
}


def normalize_category(value: str) -> str:
    return CATEGORY_ALIASES.get(value.strip().lower(), value.strip())


@dataclass
class Listing:
    id: str
    company: str
    title: str
    url: str
    locations: list[str] = field(default_factory=list)
    category: str = ""
    terms: list[str] = field(default_factory=list)
    sponsorship: str = ""
    degrees: list[str] = field(default_factory=list)
    date_posted: int = 0
    date_updated: int = 0
    active: bool = True
    is_visible: bool = True
    source: str = ""
    company_url: str = ""

    @property
    def location(self) -> str:
        return "; ".join(self.locations)

    @property
    def posted(self) -> datetime:
        return datetime.fromtimestamp(self.date_posted) if self.date_posted else datetime.min

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "company": self.company,
            "title": self.title,
            "url": self.url,
            "locations": self.locations,
            "category": self.category,
            "terms": self.terms,
            "sponsorship": self.sponsorship,
            "degrees": self.degrees,
            "date_posted": self.date_posted,
            "active": self.active,
        }


# --------------------------------------------------------------------------- #
# Repository sync
# --------------------------------------------------------------------------- #


def sync_repo(local_path: Path, repo_url: str, branch: str = "dev") -> None:
    """Clone the listings repo if missing, otherwise ``git pull``."""
    if (local_path / ".git").exists():
        log.info("Pulling latest listings in %s", local_path)
        try:
            subprocess.run(
                ["git", "-C", str(local_path), "pull", "--ff-only", "origin", branch],
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.CalledProcessError as e:
            log.warning("git pull failed (%s); using existing checkout", e.stderr.strip())
    else:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        log.info("Cloning %s (branch %s) into %s", repo_url, branch, local_path)
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", branch, repo_url, str(local_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )


# --------------------------------------------------------------------------- #
# Structured JSON parser
# --------------------------------------------------------------------------- #


def parse_listing(raw: dict[str, Any]) -> Listing:
    return Listing(
        id=str(raw.get("id", "")),
        company=str(raw.get("company_name", "")).strip(),
        title=str(raw.get("title", "")).strip(),
        url=str(raw.get("url", "")).strip(),
        locations=[str(x) for x in raw.get("locations", []) or []],
        category=normalize_category(str(raw.get("category", "") or "")),
        terms=[str(x) for x in raw.get("terms", []) or []],
        sponsorship=str(raw.get("sponsorship", "") or ""),
        degrees=[str(x) for x in raw.get("degrees", []) or []],
        date_posted=int(raw.get("date_posted", 0) or 0),
        date_updated=int(raw.get("date_updated", 0) or 0),
        active=bool(raw.get("active", False)),
        is_visible=bool(raw.get("is_visible", True)),
        source=str(raw.get("source", "") or ""),
        company_url=str(raw.get("company_url", "") or ""),
    )


def load_listings_json(path: Path) -> list[Listing]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path} does not contain a JSON list")
    return [parse_listing(x) for x in data]


# --------------------------------------------------------------------------- #
# README fallback parser
# --------------------------------------------------------------------------- #

_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_HREF_RE = re.compile(r'href="([^"]+)"')
_TAG_RE = re.compile(r"<[^>]+>")
_SIMPLIFY_RE = re.compile(r"simplify\.jobs", re.I)


_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\U0001F1E6-\U0001F1FF\uFE0F]")


def _strip_tags(html: str) -> str:
    text = _TAG_RE.sub("", html)
    text = _EMOJI_RE.sub("", text)
    return re.sub(r"\s+", " ", text).replace("&amp;", "&").strip()


def parse_readme(markdown: str, active_only: bool = True) -> list[Listing]:
    """Parse the README's HTML table. Used only if listings.json is missing.

    Rows starting with ``↳`` belong to the company of the previous row.
    A row whose Role cell contains ``🔒`` is closed.
    """
    listings: list[Listing] = []
    current_company = ""
    for row_html in _ROW_RE.findall(markdown):
        cells = _CELL_RE.findall(row_html)
        if len(cells) < 4:
            continue
        company_cell, role_cell, location_cell, apply_cell = cells[:4]
        company = _strip_tags(company_cell)
        if company == "↳":
            company = current_company
        else:
            current_company = company
        role = _strip_tags(role_cell)
        closed = "🔒" in role_cell or "🔒" in apply_cell
        role = role.replace("🔒", "").strip()
        if active_only and closed:
            continue
        links = [h for h in _HREF_RE.findall(apply_cell) if not _SIMPLIFY_RE.search(h)]
        if not links:
            continue
        url = links[0].split("?utm_source")[0]
        locations = [_strip_tags(loc) for loc in re.split(r"<\s*/?\s*br\s*/?\s*>", location_cell, flags=re.I)]
        locations = [loc for loc in locations if loc]
        listings.append(
            Listing(
                id=url,
                company=company,
                title=role,
                url=url,
                locations=locations,
                active=not closed,
                source="readme",
            )
        )
    return listings


# --------------------------------------------------------------------------- #
# Loading with fallback
# --------------------------------------------------------------------------- #


def load_listings(repo_path: Path, json_rel_path: str = ".github/scripts/listings.json") -> tuple[list[Listing], str]:
    """Return (listings, source_name). Prefers JSON; falls back to README.md."""
    json_path = repo_path / json_rel_path
    if json_path.exists():
        return load_listings_json(json_path), str(json_rel_path)
    readme = repo_path / "README.md"
    if readme.exists():
        log.warning("listings.json not found; falling back to README.md parsing")
        return parse_readme(readme.read_text(encoding="utf-8")), "README.md"
    raise FileNotFoundError(f"No listings.json or README.md under {repo_path}")


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #


@dataclass
class ListingFilters:
    term: str = "Summer 2027"
    categories: list[str] = field(default_factory=list)
    title_keywords: list[str] = field(default_factory=list)
    exclude_title_keywords: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    exclude_companies: list[str] = field(default_factory=list)
    exclude_sponsorship: list[str] = field(default_factory=list)
    require_degree: str = ""

    @classmethod
    def from_settings(cls, settings_data: dict[str, Any]) -> "ListingFilters":
        f = settings_data.get("filters", {}) or {}
        return cls(
            term=str((settings_data.get("listings", {}) or {}).get("term", "Summer 2027")),
            categories=[normalize_category(c) for c in f.get("categories", []) or []],
            title_keywords=list(f.get("title_keywords", []) or []),
            exclude_title_keywords=list(f.get("exclude_title_keywords", []) or []),
            locations=list(f.get("locations", []) or []),
            exclude_companies=list(f.get("exclude_companies", []) or []),
            exclude_sponsorship=list(f.get("exclude_sponsorship", []) or []),
            require_degree=str(f.get("require_degree", "") or ""),
        )


def _contains_any(text: str, needles: Iterable[str]) -> bool:
    t = text.lower()
    return any(n.lower() in t for n in needles if n)


def matches(listing: Listing, f: ListingFilters) -> tuple[bool, str]:
    """Return (keep, reason). Reason explains a rejection."""
    if not listing.active or not listing.is_visible:
        return False, "inactive"
    if f.term and not any(f.term in t for t in listing.terms):
        # README fallback rows have no terms; treat them as matching the term.
        if listing.terms or listing.source != "readme":
            return False, f"term != {f.term}"
    if f.categories and listing.category and listing.category not in f.categories:
        return False, f"category {listing.category}"
    if f.title_keywords and not _contains_any(listing.title, f.title_keywords):
        return False, "title keywords"
    if f.exclude_title_keywords and _contains_any(listing.title, f.exclude_title_keywords):
        return False, "excluded title keyword"
    if f.locations and listing.locations and not any(_contains_any(loc, f.locations) for loc in listing.locations):
        return False, "location"
    if f.exclude_companies and _contains_any(listing.company, f.exclude_companies):
        return False, "excluded company"
    if f.exclude_sponsorship and listing.sponsorship in f.exclude_sponsorship:
        return False, f"sponsorship: {listing.sponsorship}"
    if f.require_degree and listing.degrees and f.require_degree not in listing.degrees:
        return False, "degree"
    if not listing.url.startswith("http"):
        return False, "no url"
    return True, ""


def filter_listings(listings: Iterable[Listing], f: ListingFilters) -> list[Listing]:
    kept = [l for l in listings if matches(l, f)[0]]
    # newest first
    kept.sort(key=lambda l: (l.date_posted, l.company.lower()), reverse=True)
    return kept
