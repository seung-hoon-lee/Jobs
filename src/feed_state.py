"""Loads existing "추천 공고" feed rows (active + archived) indexed by URL.

This is Stage 0 of the pipeline -- every later stage (discovery/liveness/
matching/sync) reads from the FeedState this module returns.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from notion_client import Client
from notion_client.errors import APIResponseError

logger = logging.getLogger(__name__)

LINK_PROPERTY = "링크"
DEADLINE_PROPERTY = "마감일"
STATUS_PROPERTY = "모집상태"
DISCOVERED_AT_PROPERTY = "discoveredAt"
LAST_LIVENESS_CHECK_PROPERTY = "lastLivenessCheckAt"
SOURCE_PROPERTY = "출처"

PAGE_SIZE = 100


@dataclass
class FeedState:
    by_url: Dict[str, dict] = field(default_factory=dict)


def _query_all_pages(client: Client, data_source_id: str, **extra_body) -> List[dict]:
    pages: List[dict] = []
    start_cursor: Optional[str] = None

    while True:
        body = dict(extra_body)
        if start_cursor:
            body["start_cursor"] = start_cursor
        response = client.data_sources.query(data_source_id=data_source_id, page_size=PAGE_SIZE, **body)
        pages.extend(response.get("results", []))
        if not response.get("has_more"):
            break
        start_cursor = response.get("next_cursor")

    return pages


def _is_page_archived(page: dict) -> bool:
    # Field naming has shifted across Notion API versions (archived -> is_archived
    # -> in_trash); check all three so this doesn't silently misread status after
    # an API/library upgrade.
    for key in ("in_trash", "is_archived", "archived"):
        if key in page:
            return bool(page[key])
    return False


def _extract_url(page: dict, prop_name: str) -> Optional[str]:
    prop = page.get("properties", {}).get(prop_name) or {}
    return prop.get("url")


def _extract_date(page: dict, prop_name: str) -> Optional[str]:
    prop = page.get("properties", {}).get(prop_name) or {}
    date_obj = prop.get("date")
    return date_obj.get("start") if date_obj else None


def _extract_select(page: dict, prop_name: str) -> Optional[str]:
    prop = page.get("properties", {}).get(prop_name) or {}
    select_obj = prop.get("select")
    return select_obj.get("name") if select_obj else None


def _extract_created_time(page: dict, prop_name: str) -> Optional[str]:
    prop = page.get("properties", {}).get(prop_name) or {}
    return prop.get("created_time")


def load_feed_state(notion_token: str, feed_db_id: str) -> FeedState:
    client = Client(auth=notion_token)

    active_pages = _query_all_pages(client, feed_db_id)

    archived_pages: List[dict] = []
    try:
        archived_pages = _query_all_pages(client, feed_db_id, is_archived=True)
    except APIResponseError:
        logger.warning(
            "Archived-page query (is_archived=True) failed against %s; "
            "continuing with active rows only for this run.",
            feed_db_id,
        )

    by_url: Dict[str, dict] = {}
    for page in active_pages + archived_pages:
        url = _extract_url(page, LINK_PROPERTY)
        if not url:
            continue
        by_url[url] = {
            "notion_page_id": page["id"],
            "is_archived": _is_page_archived(page),
            "마감일": _extract_date(page, DEADLINE_PROPERTY),
            "모집상태": _extract_select(page, STATUS_PROPERTY),
            "discoveredAt": _extract_created_time(page, DISCOVERED_AT_PROPERTY),
            "lastLivenessCheckAt": _extract_date(page, LAST_LIVENESS_CHECK_PROPERTY),
            "source": _extract_select(page, SOURCE_PROPERTY),
        }

    return FeedState(by_url=by_url)
