"""Saramin (saramin.co.kr) job listing scraper.

Lightweight list-page GET + BeautifulSoup parse. Verified live against
https://www.saramin.co.kr/zf_user/search/recruit -- but only with a
non-empty `searchword`: an empty/omitted searchword (and the plain
`/zf_user/jobs/list/*` browse pages) render a client-side-only "인기있는
채용정보" recommendation widget instead of real results, both verified live
to return zero real listings. So unlike the other three sources, this one is
keyword-driven -- it queries once per configured keyword (capped at
MAX_KEYWORDS) and dedupes by URL, rather than pulling one
keyword-independent "latest postings" feed. Real results render under
`#recruit_info_list .item_recruit`.
"""
import re
import time
from datetime import date, datetime, timedelta
from typing import List, Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from src.sources.base import JobPosting, is_domestic

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT}
REQUEST_TIMEOUT_SECONDS = 10
REQUEST_DELAY_SECONDS = 0.7

BASE_URL = "https://www.saramin.co.kr"
SEARCH_URL_TEMPLATE = (
    "https://www.saramin.co.kr/zf_user/search/recruit"
    "?search_area=main&search_done=y&search_optional_item=n&searchType=search"
    "&searchword={keyword}&recruitPage={page}&recruitSort=reg_dt&recruitPageCount=40"
)
MAX_KEYWORDS = 5
PAGES_PER_KEYWORD = 2

SELECTOR_JOB_ITEM = "#recruit_info_list .item_recruit"
SELECTOR_TITLE_LINK = ".area_job h2.job_tit a"
SELECTOR_COMPANY_LINK = ".area_corp .corp_name a"
SELECTOR_CONDITION_SPANS = ".job_condition span"
SELECTOR_DEADLINE = ".job_date .date"


def _normalize_deadline(raw: Optional[str], today: Optional[date] = None) -> Optional[str]:
    if not raw:
        return None
    today = today or date.today()
    text = re.sub(r"\([^)]*\)", "", raw.strip()).lstrip("~").strip()
    if not text or "상시" in text:
        return None

    d_day = re.fullmatch(r"D-(\d+)", text)
    if d_day:
        return (today + timedelta(days=int(d_day.group(1)))).isoformat()
    if text in ("D-day", "오늘마감"):
        return today.isoformat()

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


def _fetch_keyword_postings(keyword: str) -> List[JobPosting]:
    postings: List[JobPosting] = []

    for page in range(1, PAGES_PER_KEYWORD + 1):
        url = SEARCH_URL_TEMPLATE.format(keyword=quote(keyword), page=page)
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        items = soup.select(SELECTOR_JOB_ITEM)
        if not items:
            break

        for item in items:
            title_el = item.select_one(SELECTOR_TITLE_LINK)
            company_el = item.select_one(SELECTOR_COMPANY_LINK)
            if title_el is None or company_el is None:
                continue

            company = company_el.get_text(strip=True)
            condition_spans = item.select(SELECTOR_CONDITION_SPANS)
            region = condition_spans[0].get_text(strip=True) if len(condition_spans) > 0 else ""
            if not is_domestic(region, company):
                continue

            career_level = condition_spans[1].get_text(strip=True) if len(condition_spans) > 1 else ""
            employment_type = condition_spans[3].get_text(strip=True) if len(condition_spans) > 3 else ""
            deadline_el = item.select_one(SELECTOR_DEADLINE)

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
                    source="사람인",
                )
            )

        if page < PAGES_PER_KEYWORD:
            time.sleep(REQUEST_DELAY_SECONDS)

    return postings


def fetch_postings(keywords: List[str]) -> List[JobPosting]:
    by_url: dict = {}

    for keyword in keywords[:MAX_KEYWORDS]:
        for posting in _fetch_keyword_postings(keyword):
            by_url.setdefault(posting.url, posting)
        time.sleep(REQUEST_DELAY_SECONDS)

    return list(by_url.values())
