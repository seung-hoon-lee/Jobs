"""JobKorea (jobkorea.co.kr) job listing scraper.

Lightweight list-page GET + BeautifulSoup parse. Verified live against
https://www.jobkorea.co.kr/Recruit/Joblist, a server-rendered results table
(`tr.devloopArea` rows). Note: JobKorea's real pagination is an AJAX
fragment endpoint gated by session/referrer state that a plain GET can't
reach, and `Page_No`/similar query params on the full page are ignored (
verified: page 1 and a `Page_No=2` request return the identical 50 rows) --
so this only fetches the single default results page (most-recent postings)
rather than paginating, a known limitation vs. the other sources.
"""
import re
from datetime import date, datetime
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

from src.sources.base import JobPosting, is_domestic

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT}
REQUEST_TIMEOUT_SECONDS = 10

BASE_URL = "https://www.jobkorea.co.kr"
LIST_URL = "https://www.jobkorea.co.kr/Recruit/Joblist"

SELECTOR_ROW = "tr.devloopArea"
SELECTOR_COMPANY_LINK = "td.tplCo a.link.normalLog"
SELECTOR_TITLE_LINK = "td.tplTit strong a.link.normalLog"
SELECTOR_ETC_CELLS = "td.tplTit p.etc span.cell"
SELECTOR_DEADLINE = "td.odd .date"

# Index order of td.tplTit p.etc span.cell, verified live.
ETC_CAREER_IDX = 0
ETC_REGION_IDX = 2
ETC_EMPLOYMENT_IDX = 3


def _normalize_deadline(raw: Optional[str], today: Optional[date] = None) -> Optional[str]:
    if not raw:
        return None
    today = today or date.today()
    text = re.sub(r"\([^)]*\)", "", raw.strip()).lstrip("~").strip()
    if not text or "상시" in text:
        return None

    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue

    for fmt in ("%m-%d", "%m.%d", "%m/%d"):
        try:
            parsed = datetime.strptime(text, fmt).date().replace(year=today.year)
            if parsed < today:
                parsed = parsed.replace(year=today.year + 1)
            return parsed.isoformat()
        except ValueError:
            continue

    return None


def fetch_postings() -> List[JobPosting]:
    postings: List[JobPosting] = []

    response = requests.get(LIST_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    for row in soup.select(SELECTOR_ROW):
        company_el = row.select_one(SELECTOR_COMPANY_LINK)
        title_el = row.select_one(SELECTOR_TITLE_LINK)
        if company_el is None or title_el is None:
            continue

        company = company_el.get_text(strip=True)
        etc_cells = row.select(SELECTOR_ETC_CELLS)
        region = (
            etc_cells[ETC_REGION_IDX].get_text(strip=True)
            if len(etc_cells) > ETC_REGION_IDX
            else ""
        )
        if not is_domestic(region, company):
            continue

        career_level = (
            etc_cells[ETC_CAREER_IDX].get_text(strip=True) if len(etc_cells) > ETC_CAREER_IDX else ""
        )
        employment_type = (
            etc_cells[ETC_EMPLOYMENT_IDX].get_text(strip=True)
            if len(etc_cells) > ETC_EMPLOYMENT_IDX
            else ""
        )
        deadline_el = row.select_one(SELECTOR_DEADLINE)

        href = title_el.get("href", "")
        job_url = href if href.startswith("http") else f"{BASE_URL}{href}"
        title = title_el.get("title") or title_el.get_text(strip=True)

        postings.append(
            JobPosting(
                title=title,
                company=company,
                url=job_url,
                deadline=_normalize_deadline(deadline_el.get_text(strip=True) if deadline_el else None),
                region=region,
                employment_type=employment_type,
                career_level=career_level,
                source="잡코리아",
            )
        )

    return postings
