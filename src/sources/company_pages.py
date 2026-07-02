"""Watched-company career-page adapter.

Recognizes Greenhouse and Lever career-page URLs and queries their public
listing JSON APIs (`boards-api.greenhouse.io`, `api.lever.co`) instead of
scraping the career page's HTML -- verified live against real boards
(stripe/airbnb/figma on Greenhouse, palantir on Lever); both are documented,
stable third-party APIs rather than guessed markup.

GreetingHR (a Korean ATS -- e.g. `*.career.greetinghr.com`, but also often
deployed on a company's own custom domain like `career.<company>.com`, so it
can't be recognized by domain name the way Greenhouse/Lever are) is detected
by content-sniffing instead: its career pages are Next.js Pages Router and
embed the full openings list as page data in a `<script id="__NEXT_DATA__">`
JSON blob under a `["openings"]` react-query cache entry -- verified live
against three real GreetingHR-hosted boards (two on `*.greetinghr.com`, one
on a fully custom domain).

Any other page falls back to a generic best-effort list/table HTML parser
(unverified against a real page, since the whole point of the fallback is
pages with unknown markup). Any company whose page doesn't fit a supported
pattern (unsupported markup, network error, or the generic fallback finding
nothing usable) is logged and skipped -- this adapter never raises out of
fetch_postings so one broken watched company can't take down the rest of the
run.

JobPosting.description (used for keyword matching alongside title, see
matching.py) is populated per adapter: Greenhouse asks for it inline
(`content=true`), Lever's listing response already includes it
(`descriptionPlain`), and GreetingHR requires one extra per-opening detail
request (its listing query has no body text) -- all verified live. The
generic fallback leaves it empty (title-only matching), since there's no
known structure to locate a description in.
"""
import json
import logging
import re
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from src.sources.base import JobPosting, is_domestic, strip_html

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT}
REQUEST_TIMEOUT_SECONDS = 10
REQUEST_DELAY_SECONDS = 0.7
GREETINGHR_DETAIL_DELAY_SECONDS = 0.3

GREENHOUSE_DOMAIN = "boards.greenhouse.io"
LEVER_DOMAIN = "jobs.lever.co"

# content=true asks Greenhouse to include each job's full HTML body
# (job.content) alongside the listing, so no extra per-job request is
# needed to populate JobPosting.description.
GREENHOUSE_API_TEMPLATE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
LEVER_API_TEMPLATE = "https://api.lever.co/v0/postings/{slug}?mode=json"

LEVER_EMPLOYMENT_TYPE_LABELS = {
    "full-time": "정규직",
    "part-time": "파트타임",
    "intern": "인턴",
    "contract": "계약직",
    "temporary": "임시직",
}

NEXT_DATA_SCRIPT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

GREETINGHR_EMPLOYMENT_TYPE_LABELS = {
    "FULL_TIME_WORKER": "정규직",
    "CONTRACT_WORKER": "계약직",
    "INTERN_WORKER": "인턴",
    "PART_TIME_WORKER": "파트타임",
}
GREETINGHR_CAREER_TYPE_LABELS = {
    "NEW_COMER": "신입",
    "EXPERIENCED": "경력",
    "NOT_MATTER": "경력무관",
}

GENERIC_SELECTOR_ROW = "table tr, ul li, div.job-listing"
GENERIC_SELECTOR_LINK = "a"
GENERIC_SELECTOR_LOCATION = ".location, td.location, span.location"


def _slug_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    return path.split("/")[0] if path else ""


def fetch_postings(
    watched_companies: List[str],
    company_urls: Optional[Dict[str, str]] = None,
) -> List[JobPosting]:
    company_urls = company_urls or {}
    postings: List[JobPosting] = []

    for company in watched_companies:
        url = company_urls.get(company)
        if not url:
            logger.warning(
                "관심기업 %s: no supported career-page pattern detected, skipped", company
            )
            continue

        try:
            postings.extend(_fetch_company_postings(company, url))
        except Exception:
            logger.warning(
                "관심기업 %s: no supported career-page pattern detected, skipped", company
            )

        time.sleep(REQUEST_DELAY_SECONDS)

    return postings


def _fetch_company_postings(company: str, url: str) -> List[JobPosting]:
    if GREENHOUSE_DOMAIN in url:
        return _fetch_greenhouse(company, url)
    if LEVER_DOMAIN in url:
        return _fetch_lever(company, url)

    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    greetinghr_postings = _try_parse_greetinghr(company, url, response.text)
    if greetinghr_postings is not None:
        return greetinghr_postings

    soup = BeautifulSoup(response.text, "html.parser")
    return _parse_generic(company, url, soup)


def _fetch_greenhouse(company: str, career_page_url: str) -> List[JobPosting]:
    slug = _slug_from_url(career_page_url)
    response = requests.get(
        GREENHOUSE_API_TEMPLATE.format(slug=slug), headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    jobs = response.json().get("jobs", [])

    postings: List[JobPosting] = []
    for job in jobs:
        region = (job.get("location") or {}).get("name", "")
        if not is_domestic(region, company):
            continue

        postings.append(
            JobPosting(
                title=job.get("title", ""),
                company=company,
                url=job.get("absolute_url", career_page_url),
                deadline=None,
                region=region,
                employment_type="",
                career_level="",
                source="관심기업",
                description=strip_html(job.get("content", "")),
            )
        )

    if not postings:
        raise ValueError(f"no openings parsed for {company}")

    return postings


def _fetch_lever(company: str, career_page_url: str) -> List[JobPosting]:
    slug = _slug_from_url(career_page_url)
    response = requests.get(
        LEVER_API_TEMPLATE.format(slug=slug), headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    jobs = response.json()

    postings: List[JobPosting] = []
    for job in jobs:
        categories = job.get("categories") or {}
        region = categories.get("location", "")
        if not is_domestic(region, company):
            continue

        commitment = (categories.get("commitment") or "").strip().lower()

        postings.append(
            JobPosting(
                title=job.get("text", ""),
                company=company,
                url=job.get("hostedUrl", career_page_url),
                deadline=None,
                region=region,
                employment_type=LEVER_EMPLOYMENT_TYPE_LABELS.get(commitment, ""),
                career_level="",
                source="관심기업",
                # Lever's listing response already includes the full plain-text
                # body (no separate detail request needed, unlike Greenhouse/
                # GreetingHR -- verified live).
                description=job.get("descriptionPlain", ""),
            )
        )

    if not postings:
        raise ValueError(f"no postings parsed for {company}")

    return postings


def _first_job_position(opening: dict) -> Optional[dict]:
    positions = (opening.get("openingJobPosition") or {}).get("openingJobPositions") or []
    return positions[0] if positions else None


def _greetinghr_region(position: Optional[dict]) -> str:
    if not position:
        return ""
    place = position.get("workspacePlace") or {}
    return place.get("place") or place.get("location") or ""


def _greetinghr_deadline(due_date: Optional[str]) -> Optional[str]:
    return due_date.split("T")[0] if due_date else None


def _fetch_greetinghr_description(opening_url: str) -> str:
    """The `openings` listing query has no body text -- only the single-
    opening page's `getOpeningById` query cache entry does (verified live),
    so getting a description means one extra request per opening."""
    try:
        response = requests.get(opening_url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        match = NEXT_DATA_SCRIPT_RE.search(response.text)
        if match is None:
            return ""
        next_data = json.loads(match.group(1))
        queries = (
            next_data.get("props", {})
            .get("pageProps", {})
            .get("dehydratedState", {})
            .get("queries", [])
        )
        for query in queries:
            key = query.get("queryKey") or []
            if len(key) > 1 and key[1] == "getOpeningById":
                opening_info = ((query.get("state", {}).get("data") or {}).get("data") or {}).get(
                    "openingsInfo"
                ) or {}
                return strip_html(opening_info.get("detail", ""))
    except Exception as exc:
        logger.warning("GreetingHR 상세 조회 실패 (%s), 본문 없이 진행: %s", opening_url, exc)
    return ""


def _try_parse_greetinghr(company: str, url: str, html: str) -> Optional[List[JobPosting]]:
    """Returns None (not a GreetingHR page -> caller falls back to the
    generic parser) if no `__NEXT_DATA__` openings cache entry is found;
    raises ValueError (same "unsupported/empty" convention as the other
    parsers) if it is GreetingHR-shaped but nothing domestic survives.
    """
    match = NEXT_DATA_SCRIPT_RE.search(html)
    if match is None:
        return None
    try:
        next_data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

    queries = (
        next_data.get("props", {})
        .get("pageProps", {})
        .get("dehydratedState", {})
        .get("queries", [])
    )
    openings = None
    for query in queries:
        if query.get("queryKey") == ["openings"]:
            openings = query.get("state", {}).get("data")
            break
    if openings is None:
        return None

    parsed_url = urlparse(url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"

    postings: List[JobPosting] = []
    for opening in openings:
        title = opening.get("title", "")
        opening_id = opening.get("openingId")
        if not title or opening_id is None:
            continue

        position = _first_job_position(opening)
        region = _greetinghr_region(position)
        if not is_domestic(region, company):
            continue

        employment_type_raw = ((position or {}).get("jobPositionEmployment") or {}).get("employmentType", "")
        career_type_raw = ((position or {}).get("jobPositionCareer") or {}).get("careerType", "")
        opening_url = f"{base_url}/ko/o/{opening_id}"

        postings.append(
            JobPosting(
                title=title,
                company=company,
                url=opening_url,
                deadline=_greetinghr_deadline(opening.get("dueDate")),
                region=region,
                employment_type=GREETINGHR_EMPLOYMENT_TYPE_LABELS.get(employment_type_raw, ""),
                career_level=GREETINGHR_CAREER_TYPE_LABELS.get(career_type_raw, ""),
                source="관심기업",
                description=_fetch_greetinghr_description(opening_url),
            )
        )
        time.sleep(GREETINGHR_DETAIL_DELAY_SECONDS)

    if not postings:
        raise ValueError(f"no openings parsed for {company}")

    return postings


def _parse_generic(company: str, base_url: str, soup: BeautifulSoup) -> List[JobPosting]:
    postings: List[JobPosting] = []

    for row in soup.select(GENERIC_SELECTOR_ROW):
        link_el = row.select_one(GENERIC_SELECTOR_LINK)
        if link_el is None:
            continue

        title = link_el.get_text(strip=True)
        href = link_el.get("href", "")
        if not title or not href:
            continue

        location_el = row.select_one(GENERIC_SELECTOR_LOCATION)
        region = location_el.get_text(strip=True) if location_el else ""
        if not is_domestic(region, company):
            continue

        job_url = href if href.startswith("http") else f"{base_url.rstrip('/')}/{href.lstrip('/')}"

        postings.append(
            JobPosting(
                title=title,
                company=company,
                url=job_url,
                deadline=None,
                region=region,
                employment_type="",
                career_level="",
                source="관심기업",
            )
        )

    if not postings:
        raise ValueError(f"no supported career-page pattern detected for {company}")

    return postings
