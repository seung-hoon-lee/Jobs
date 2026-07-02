"""Notion sync for the "추천 공고" (recommendation feed) database.

Isolation note: this module reads from and writes to ONLY the 추천 공고
data source identified by the `feed_db_id` passed explicitly into `sync()`.
It must never read or write the legacy "취업 지원 현황" tracker database --
that database is out of scope for this automation and no code path here
references it, hardcodes its id, or accepts it as a fallback.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Set

from notion_client import APIErrorCode, APIResponseError, Client

from src.feed_state import FeedState
from src.sources.base import JobPosting

logger = logging.getLogger(__name__)

_NOTION_API_VERSION = "2025-09-03"  # required for data_source_id parents

_PROP_TITLE = "제목"
_PROP_COMPANY = "회사명"
_PROP_KEYWORDS = "매칭키워드"
_PROP_SOURCE = "출처"
_PROP_URL = "링크"
_PROP_DEADLINE = "마감일"
_PROP_SCORE = "관련도점수"
_PROP_STATUS = "모집상태"
_PROP_LAST_LIVENESS = "lastLivenessCheckAt"

_STATUS_OPEN = "모집중"
_STATUS_CLOSING_SOON = "마감임박"

_RETRY_DELAYS_SECONDS = (1, 2, 4)  # max 3 retries on 429, per plan Phase 6


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    archived: int = 0
    un_archived: int = 0
    skipped_due_to_source_failure: int = 0


def should_archive(url: str, feed_state: FeedState, sources_that_failed: Set[str]) -> bool:
    """Pure decision function for the plan's ADR safety gate.

    A positively-closed URL is only safe to archive if the source that
    originally supplied it did NOT fail to scrape this run -- otherwise we
    cannot distinguish "actually closed" from "source scraper broke".
    """
    row = feed_state.by_url.get(url)
    if row is None or row.get("is_archived"):
        return False
    return row.get("source") not in sources_that_failed


def _retry_on_rate_limit(fn: Callable, *args, sleep_fn: Callable[[float], None] = time.sleep, **kwargs):
    for attempt, delay in enumerate(_RETRY_DELAYS_SECONDS):
        try:
            return fn(*args, **kwargs)
        except APIResponseError as e:
            if e.code != APIErrorCode.RateLimited:
                raise
            logger.warning(
                "Notion API rate limited, retrying in %ss (attempt %d/%d)",
                delay, attempt + 1, len(_RETRY_DELAYS_SECONDS),
            )
            sleep_fn(delay)
    return fn(*args, **kwargs)  # final attempt; propagates if it still fails


def _keywords_str(matched_keywords: Optional[List[str]]) -> str:
    return ", ".join(matched_keywords) if matched_keywords else ""


def _derive_status(deadline: str, today: date) -> str:
    try:
        deadline_date = datetime.strptime(deadline, "%Y-%m-%d").date()
    except ValueError:
        logger.warning("Unparseable deadline %r, defaulting status to %s", deadline, _STATUS_OPEN)
        return _STATUS_OPEN
    days_remaining = (deadline_date - today).days
    if 0 <= days_remaining <= 3:
        return _STATUS_CLOSING_SOON
    return _STATUS_OPEN


def _title_prop(value: str) -> dict:
    return {"title": [{"text": {"content": value}}]}


def _rich_text_prop(value: str) -> dict:
    return {"rich_text": [{"text": {"content": value}}] if value else []}


def _select_prop(value: Optional[str]) -> dict:
    return {"select": {"name": value} if value else None}


def _url_prop(value: str) -> dict:
    return {"url": value}


def _date_prop(value: Optional[str]) -> dict:
    return {"date": {"start": value} if value else None}


def _number_prop(value: Optional[float]) -> dict:
    return {"number": value}


def _full_properties(posting: JobPosting, status: str) -> dict:
    return {
        _PROP_TITLE: _title_prop(posting.title),
        _PROP_COMPANY: _rich_text_prop(posting.company),
        _PROP_KEYWORDS: _rich_text_prop(_keywords_str(posting.matched_keywords)),
        _PROP_SOURCE: _select_prop(posting.source),
        _PROP_URL: _url_prop(posting.url),
        _PROP_DEADLINE: _date_prop(posting.deadline),
        _PROP_SCORE: _number_prop(posting.relevance_score),
        _PROP_STATUS: _select_prop(status),
    }


def _create_page(client: Client, feed_db_id: str, posting: JobPosting) -> None:
    properties = _full_properties(posting, _STATUS_OPEN)
    _retry_on_rate_limit(
        client.pages.create,
        parent={"type": "data_source_id", "data_source_id": feed_db_id},
        properties=properties,
    )


def _unarchive_and_refresh(client: Client, page_id: str, posting: JobPosting, today: date) -> None:
    status = _derive_status(posting.deadline, today) if posting.deadline else _STATUS_OPEN
    properties = _full_properties(posting, status)
    _retry_on_rate_limit(client.pages.update, page_id=page_id, archived=False, properties=properties)


def _update_status_only(
    client: Client,
    page_id: str,
    deadline: Optional[str],
    today: date,
    stamp_liveness: bool = False,
) -> bool:
    properties: Dict[str, dict] = {}
    if deadline:
        properties[_PROP_STATUS] = _select_prop(_derive_status(deadline, today))
    if stamp_liveness:
        properties[_PROP_LAST_LIVENESS] = _date_prop(today.isoformat())
    if not properties:
        return False
    _retry_on_rate_limit(client.pages.update, page_id=page_id, properties=properties)
    return True


def _archive_page(client: Client, page_id: str, today_iso: str) -> None:
    properties = {_PROP_LAST_LIVENESS: _date_prop(today_iso)}
    _retry_on_rate_limit(client.pages.update, page_id=page_id, archived=True, properties=properties)


def sync(
    new_admissions: List[JobPosting],
    closed_urls: List[str],
    sources_that_failed: Set[str],
    feed_state: FeedState,
    notion_token: str,
    feed_db_id: str,
    rechecked_urls: Optional[Set[str]] = None,
    today: Optional[date] = None,
) -> SyncResult:
    """Sync this run's results into the 추천 공고 data source.

    `rechecked_urls` is optional and additive: it should contain every URL
    the liveness module actually re-requested this run (closed or not), so
    lastLivenessCheckAt can be stamped even when a row was checked but found
    still open. If omitted, lastLivenessCheckAt is only stamped for rows in
    `closed_urls` (the subset we can positively confirm without it).

    `today` defaults to `date.today()` and only exists so tests can pin a
    fixed date instead of depending on the real calendar date.
    """
    client = Client(auth=notion_token, notion_version=_NOTION_API_VERSION, retry=False)
    result = SyncResult()
    today = today or date.today()
    today_iso = today.isoformat()
    rechecked = rechecked_urls or set()

    admitted_urls: Set[str] = set()
    for posting in new_admissions:
        admitted_urls.add(posting.url)
        row = feed_state.by_url.get(posting.url)
        if row is None:
            _create_page(client, feed_db_id, posting)
            result.created += 1
        elif row.get("is_archived"):
            _unarchive_and_refresh(client, row["notion_page_id"], posting, today)
            result.un_archived += 1
        else:
            logger.warning(
                "%s already active in feed but present in new_admissions; "
                "applying defensive status-only update",
                posting.url,
            )
            if _update_status_only(client, row["notion_page_id"], posting.deadline, today):
                result.updated += 1

    to_archive_urls = {
        url for url in closed_urls if should_archive(url, feed_state, sources_that_failed)
    }
    result.skipped_due_to_source_failure = len(set(closed_urls) - to_archive_urls)

    for url, row in feed_state.by_url.items():
        if row.get("is_archived") or url in admitted_urls or url in to_archive_urls:
            continue
        if _update_status_only(
            client, row["notion_page_id"], row.get("마감일"), today, stamp_liveness=url in rechecked
        ):
            result.updated += 1

    for url in to_archive_urls:
        row = feed_state.by_url[url]
        _archive_page(client, row["notion_page_id"], today_iso)
        result.archived += 1

    return result
