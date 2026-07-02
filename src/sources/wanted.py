"""Wanted (wanted.co.kr) job listing source.

Wanted's list page is client-rendered React (no job data in the server HTML),
so this uses their public listing JSON API instead of HTML scraping --
verified live: GET /api/v4/jobs?country=kr&job_sort=job.latest_order&
locations=all&years=-1&limit={n}&offset={n} returns `{"data": [...], "links":
{"next": "..."}}` with one object per posting. The list API doesn't include
the posting body, so each domestic posting also gets one extra request to
GET /api/v4/jobs/{id} (verified live) for `job.detail`, a dict of plain-text
sections (requirements/main_tasks/intro/benefits/preferred_points) used to
populate JobPosting.description for keyword matching.
"""
import logging
import time
from typing import List, Optional

import requests

from src.sources.base import JobPosting, is_domestic

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT}
REQUEST_TIMEOUT_SECONDS = 10
REQUEST_DELAY_SECONDS = 0.3

API_URL = "https://www.wanted.co.kr/api/v4/jobs"
DETAIL_API_URL_TEMPLATE = "https://www.wanted.co.kr/api/v4/jobs/{id}"
JOB_URL_TEMPLATE = "https://www.wanted.co.kr/wd/{id}"
PAGE_LIMIT = 100
MAX_PAGES = 3


def _career_level(annual_from: Optional[int], annual_to: Optional[int]) -> str:
    if annual_from is None and annual_to is None:
        return ""
    if annual_from in (0, None) and not annual_to:
        return "신입"
    if annual_from in (0, None):
        return f"신입~{annual_to}년"
    if not annual_to:
        return f"{annual_from}년+"
    return f"{annual_from}~{annual_to}년"


def _fetch_description(job_id: int) -> str:
    try:
        response = requests.get(
            DETAIL_API_URL_TEMPLATE.format(id=job_id), headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        detail = (response.json().get("job") or {}).get("detail") or {}
        return "\n".join(value for value in detail.values() if isinstance(value, str))
    except Exception as exc:
        logger.warning("원티드 상세 조회 실패 (id=%s), 본문 없이 진행: %s", job_id, exc)
        return ""


def fetch_postings() -> List[JobPosting]:
    postings: List[JobPosting] = []

    offset = 0
    for _ in range(MAX_PAGES):
        response = requests.get(
            API_URL,
            headers=HEADERS,
            params={
                "country": "kr",
                "job_sort": "job.latest_order",
                "locations": "all",
                "years": "-1",
                "limit": PAGE_LIMIT,
                "offset": offset,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        jobs = payload.get("data", [])
        if not jobs:
            break

        for job in jobs:
            address = job.get("address") or {}
            region = address.get("location") or ""
            country = address.get("country") or ""
            company = (job.get("company") or {}).get("name", "")
            if not is_domestic(region, company) and country != "한국":
                continue

            postings.append(
                JobPosting(
                    title=job.get("position", ""),
                    company=company,
                    url=JOB_URL_TEMPLATE.format(id=job["id"]),
                    deadline=job.get("due_time"),
                    region=region,
                    employment_type="",
                    career_level=_career_level(job.get("annual_from"), job.get("annual_to")),
                    source="원티드",
                    description=_fetch_description(job["id"]),
                )
            )
            time.sleep(REQUEST_DELAY_SECONDS)

        if not payload.get("links", {}).get("next"):
            break
        offset += PAGE_LIMIT

    return postings
