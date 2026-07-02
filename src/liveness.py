"""Liveness recheck for existing "추천 공고" feed rows.

Re-fetches each row's own detail page and only reports closure on positive
evidence (404/410 status or an explicit Korean closure marker in the body).
Any ambiguous outcome -- network errors, timeouts, unexpected status codes --
must resolve to "not closed" so a transient scraping failure can never be
mistaken for a closed posting (see plan ADR: destructive changes require
positive evidence only).
"""
from datetime import date, datetime
from typing import List

import requests

from src.feed_state import FeedState

REQUEST_TIMEOUT_SECONDS = 10

# "마감" alone is deliberately excluded: it's a substring of routine labels
# every detail page renders regardless of status ("마감일" deadline-date
# field, "마감기한" deadline-period copy, GreetingHR's static help text that
# ships in the page bundle for every opening) -- verified live against three
# genuinely open GreetingHR postings, all of which contain "마감" in
# boilerplate. Only phrases specific enough to not double as routine UI copy
# belong here.
CLOSURE_MARKERS = ["채용이 종료", "모집이 종료", "공고가 만료"]

RECHECK_DEADLINE_THRESHOLD_DAYS = 7
NULL_DEADLINE_RECHECK_CADENCE_DAYS = 7


def check_closed(url: str) -> bool:
    """Return True only when the detail page gives positive evidence of closure."""
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException:
        return False

    if response.status_code in (404, 410):
        return True

    if response.status_code != 200:
        return False

    return any(marker in response.text for marker in CLOSURE_MARKERS)


def _parse_date(value) -> date:
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(value).date()


def select_recheck_candidates(feed_state: FeedState, today: date) -> List[str]:
    """Return URLs of active feed rows due for a liveness recheck.

    A row qualifies when either:
      (a) 마감일 is set and days-until-deadline <= 7 (including already past), or
      (b) 마감일 is None ("상시채용") and lastLivenessCheckAt is None or was
          at least 7 days ago (weekly cadence, to avoid excessive scrape load).
    """
    candidates: List[str] = []

    for url, row in feed_state.by_url.items():
        if row.get("is_archived"):
            continue

        deadline_raw = row.get("마감일")
        if deadline_raw:
            deadline = _parse_date(deadline_raw)
            days_until_deadline = (deadline - today).days
            if days_until_deadline <= RECHECK_DEADLINE_THRESHOLD_DAYS:
                candidates.append(url)
            continue

        last_check_raw = row.get("lastLivenessCheckAt")
        if last_check_raw is None:
            candidates.append(url)
            continue

        last_check = _parse_date(last_check_raw)
        if (today - last_check).days >= NULL_DEADLINE_RECHECK_CADENCE_DAYS:
            candidates.append(url)

    return candidates
