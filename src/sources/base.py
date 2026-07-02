"""Shared job posting type and cross-source helpers used by all scraper modules."""
from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from typing import List, Optional

_HTML_TAG_RE = re.compile(r"<[^>]+>")

# "등" (etc.) in the spec -- these are the 9 explicitly named regions plus the
# remaining Korean province/city short-names, so genuinely ambiguous locations
# fall through to the domain/blocklist checks below rather than silently
# failing to match.
DOMESTIC_REGION_NAMES = [
    "서울", "경기", "부산", "인천", "대구", "대전", "광주", "울산", "세종",
    "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
    "충청북도", "충청남도", "전라북도", "전라남도", "경상북도", "경상남도",
]
DOMESTIC_REGION_SUFFIXES = ("특별시", "광역시", "특별자치시", "특별자치도", "시", "도")

# Watched-company career pages (Greenhouse/Lever) are English-locale ATS
# platforms, so location text there reads "Seoul, South Korea" rather than
# "서울" -- matched case-insensitively against the Korean patterns above.
ENGLISH_DOMESTIC_LOCATION_PATTERNS = [
    "korea", "seoul", "busan", "pusan", "incheon", "daegu", "taegu",
    "daejeon", "taejon", "gwangju", "kwangju", "ulsan", "sejong",
    "gyeonggi", "kyunggi",
]

# Small, best-effort starting list -- not exhaustive, not a legal/corporate
# registry lookup. See is_domestic() docstring for the limitation this covers.
FOREIGN_PARENT_COMPANY_BLOCKLIST = ["Google", "Amazon", "Meta", "Microsoft"]


@dataclass
class JobPosting:
    title: str
    company: str
    url: str
    deadline: Optional[str]  # ISO date string (YYYY-MM-DD); None means 상시채용(rolling)
    region: str
    employment_type: str
    career_level: str
    source: str  # one of 원티드/사람인/잡코리아/직행/관심기업
    matched_keywords: Optional[List[str]] = None
    relevance_score: Optional[float] = None
    # Full posting body text, used for keyword matching alongside title
    # (matching.py). Empty string where a source can't cheaply fetch it
    # without a headless browser (사람인/잡코리아, verified live: their
    # detail pages render the description client-side and it's absent from
    # the server HTML) -- for those, keyword matching falls back to
    # title-only, same as before this field existed.
    description: str = ""


def strip_html(html: str) -> str:
    """Best-effort HTML-to-text for description fields sourced as markup
    (Greenhouse `content`, GreetingHR `detail`, Zighang's JSON-LD
    `description`) -- only used for keyword-substring matching, not display,
    so it doesn't need to preserve structure."""
    return unescape(_HTML_TAG_RE.sub(" ", html)).strip()


def is_domestic(location: str, company: str = "", company_domain: str = "") -> bool:
    """Best-effort proxy for "domestic (Korean) job posting", based on work location.

    This is a location-based heuristic, NOT an exact corporate-nationality
    determination -- a Korean branch office of a foreign-headquartered company
    will pass this check. Resolution order: (1) Korean region-name pattern in
    `location`, (2) `company_domain` ending in `.kr`, (3) `company` matched
    against a small foreign-parent blocklist (returns False). If none of these
    signals apply, the function defaults to False: the spec's "해외 기업은
    제외" (exclude overseas companies) is a hard filter requirement, not a soft
    preference, so an inconclusive case must resolve to exclude rather than
    include.
    """
    if location:
        stripped = location.strip()
        if any(region in stripped for region in DOMESTIC_REGION_NAMES):
            return True
        if stripped.endswith(DOMESTIC_REGION_SUFFIXES):
            return True
        lowered_location = stripped.lower()
        if any(pattern in lowered_location for pattern in ENGLISH_DOMESTIC_LOCATION_PATTERNS):
            return True

    if company_domain and company_domain.strip().lower().rstrip("/").endswith(".kr"):
        return True

    if company:
        lowered = company.lower()
        if any(blocked.lower() in lowered for blocked in FOREIGN_PARENT_COMPANY_BLOCKLIST):
            return False

    return False
