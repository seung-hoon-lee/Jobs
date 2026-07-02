"""Zighang (zighang.com) job listing source.

Zighang's listing page is a Next.js App Router page that streams its job
cards as client-side RSC payloads -- no job data is present in the raw HTML,
and no public list API was found (its `/api/` namespace is also disallowed
by robots.txt, so it isn't used even where guessable). Instead this uses
Zighang's published sitemap (https://zighang.com/robots.txt points at
/seo/sitemap/sitemap-index.xml), which lists individual
`/recruitment/{uuid}` detail-page URLs with a `<lastmod>` date. Each detail
page *is* server-rendered and embeds a standard schema.org JobPosting
JSON-LD block (verified live) -- that's the actual data source. Only URLs
from the most-recently-modified sitemap shard(s), capped at
MAX_DETAIL_FETCHES, are fetched to bound this source's request volume.
"""
import json
import re
import time
from datetime import date
from typing import List, Optional
from xml.etree import ElementTree

import requests

from src.sources.base import JobPosting, is_domestic, strip_html

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT}
REQUEST_TIMEOUT_SECONDS = 10
REQUEST_DELAY_SECONDS = 0.5

SITEMAP_INDEX_URL = "https://zighang.com/seo/sitemap/sitemap-index.xml"
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
RECENT_SITEMAP_SHARDS = 2
MAX_DETAIL_FETCHES = 40

EMPLOYMENT_TYPE_LABELS = {
    "FULL_TIME": "정규직",
    "PART_TIME": "파트타임",
    "CONTRACTOR": "계약직",
    "TEMPORARY": "임시직",
    "INTERN": "인턴",
    "VOLUNTEER": "봉사",
}


def _recent_recruitment_sitemap_urls() -> List[str]:
    response = requests.get(SITEMAP_INDEX_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)

    entries = []
    for sitemap in root.findall(f"{SITEMAP_NS}sitemap"):
        loc = sitemap.findtext(f"{SITEMAP_NS}loc")
        lastmod = sitemap.findtext(f"{SITEMAP_NS}lastmod") or ""
        if loc and "sitemap-recruitment" in loc:
            entries.append((lastmod, loc))

    entries.sort(reverse=True)
    return [loc for _, loc in entries[:RECENT_SITEMAP_SHARDS]]


def _recruitment_urls_from_shard(shard_url: str) -> List[str]:
    response = requests.get(shard_url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)

    urls = []
    for url_el in root.findall(f"{SITEMAP_NS}url"):
        loc = url_el.findtext(f"{SITEMAP_NS}loc")
        if loc:
            urls.append(loc)
    return urls


def _extract_job_posting_ld_json(html: str) -> Optional[dict]:
    for match in re.finditer(
        r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
    ):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if data.get("@type") == "JobPosting":
            return data
    return None


def _parse_detail_page(url: str) -> Optional[JobPosting]:
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    data = _extract_job_posting_ld_json(response.text)
    if data is None:
        return None

    hiring_org = data.get("hiringOrganization") or {}
    company = hiring_org.get("name", "")

    address = (data.get("jobLocation") or {}).get("address") or {}
    region = address.get("addressRegion", "")
    if not is_domestic(region, company) and address.get("addressCountry") != "KR":
        return None

    deadline = data.get("validThrough")
    if deadline:
        deadline = deadline.split("T")[0]
        try:
            date.fromisoformat(deadline)
        except ValueError:
            deadline = None
    else:
        deadline = None

    employment_type_raw = data.get("employmentType", "")
    employment_type = EMPLOYMENT_TYPE_LABELS.get(employment_type_raw, "")

    return JobPosting(
        title=data.get("title", ""),
        company=company,
        url=data.get("url", url),
        deadline=deadline,
        region=region,
        employment_type=employment_type,
        career_level="",
        source="직행",
        description=strip_html(data.get("description", "")),
    )


def fetch_postings() -> List[JobPosting]:
    postings: List[JobPosting] = []

    shard_urls = _recent_recruitment_sitemap_urls()
    detail_urls: List[str] = []
    for shard_url in shard_urls:
        detail_urls.extend(_recruitment_urls_from_shard(shard_url))
        if len(detail_urls) >= MAX_DETAIL_FETCHES:
            break

    for detail_url in detail_urls[:MAX_DETAIL_FETCHES]:
        posting = _parse_detail_page(detail_url)
        if posting is not None:
            postings.append(posting)
        time.sleep(REQUEST_DELAY_SECONDS)

    return postings
