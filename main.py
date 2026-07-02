"""Daily job-posting recommendation pipeline entrypoint (plan Phase 7).

Stage order: 0 (load config + feed state) -> 1 (discovery) -> 2 (liveness
recheck, independent of discovery outcome) -> 3 (matching) -> 4 (Notion sync).
Exits non-zero only on fail-closed conditions (bad Settings DB) or a fully
unhandled exception; partial per-source scraping failures are isolated and
logged, and the run still completes and exits 0.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Set

from src.config import ConfigError, load_user_config
from src.feed_state import FeedState, load_feed_state
from src.liveness import check_closed, select_recheck_candidates
from src.matching import rescore, score_and_rank
from src.notion_sync import sync
from src.sources import company_pages, jobkorea, saramin, wanted, zighang
from src.sources.base import JobPosting

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")

# Watched-company (관심기업) career pages need a name -> URL mapping that the
# Settings DB's 관심기업 multi-select can't carry (it only holds company
# names). This repo-committed file fills that gap -- e.g.
# {"토스": "https://boards.greenhouse.io/toss"} -- so URLs can be added
# without a code change (must stay committed: GitHub Actions only sees
# what's checked into the repo).
COMPANY_URLS_PATH = Path(__file__).parent / "company_urls.json"

SOURCE_FETCHERS = {
    "원티드": lambda config: wanted.fetch_postings(),
    "사람인": lambda config: saramin.fetch_postings(config.keywords),
    "잡코리아": lambda config: jobkorea.fetch_postings(),
    "직행": lambda config: zighang.fetch_postings(),
}

WATCHED_COMPANY_SOURCE = "관심기업"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        logger.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


def _load_company_urls() -> Dict[str, str]:
    if not COMPANY_URLS_PATH.exists():
        return {}
    try:
        return json.loads(COMPANY_URLS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Failed to read %s (%s); watched-company scraping skipped this run.",
            COMPANY_URLS_PATH, exc,
        )
        return {}


def _dedupe_by_url(postings: List[JobPosting]) -> List[JobPosting]:
    by_url: Dict[str, JobPosting] = {}
    for posting in postings:
        by_url.setdefault(posting.url, posting)
    return list(by_url.values())


def _bucket_discovered(discovered: List[JobPosting], feed_state: FeedState):
    """Split today's discovery set into never-seen vs. archived-rediscovered.

    Already-active URLs are neither: their D-day/모집상태 refresh is handled
    directly inside notion_sync.sync() by iterating feed_state, independent
    of what's passed as new_admissions.
    """
    never_seen: List[JobPosting] = []
    archived_rediscovered: List[JobPosting] = []
    for posting in discovered:
        row = feed_state.by_url.get(posting.url)
        if row is None:
            never_seen.append(posting)
        elif row.get("is_archived"):
            archived_rediscovered.append(posting)
    return never_seen, archived_rediscovered


def main() -> int:
    notion_token = _require_env("NOTION_TOKEN")
    recommendation_db_id = _require_env("RECOMMENDATION_DB_ID")
    config_db_id = _require_env("CONFIG_DB_ID")

    try:
        config = load_user_config(notion_token, config_db_id)
    except ConfigError as exc:
        logger.error("Config load failed, aborting (fail-closed): %s", exc)
        return 1

    feed_state = load_feed_state(notion_token, recommendation_db_id)
    today = date.today()

    # Stage 1: Discovery
    discovered: List[JobPosting] = []
    sources_that_failed: Set[str] = set()

    for source_name, fetch_fn in SOURCE_FETCHERS.items():
        try:
            postings = fetch_fn(config)
            discovered.extend(postings)
            logger.info("%s: discovered %d postings", source_name, len(postings))
        except Exception as exc:
            sources_that_failed.add(source_name)
            logger.warning("%s: discovery failed, skipping this source this run: %s", source_name, exc)

    company_urls = _load_company_urls()
    try:
        watched_postings = company_pages.fetch_postings(config.watched_companies, company_urls)
        discovered.extend(watched_postings)
        logger.info("%s: discovered %d postings", WATCHED_COMPANY_SOURCE, len(watched_postings))
    except Exception as exc:
        sources_that_failed.add(WATCHED_COMPANY_SOURCE)
        logger.warning("%s: discovery failed, skipping this source this run: %s", WATCHED_COMPANY_SOURCE, exc)

    discovered = _dedupe_by_url(discovered)
    never_seen, archived_rediscovered = _bucket_discovered(discovered, feed_state)

    # Stage 2: Liveness Recheck (independent of Stage 1's outcome)
    recheck_candidates = select_recheck_candidates(feed_state, today)
    closed_urls = [url for url in recheck_candidates if check_closed(url)]

    # Stage 3: Matching
    top_new_admissions = score_and_rank(never_seen, config, today)
    rediscovered_admissions = rescore(archived_rediscovered, config, today)

    # Stage 4: Notion Sync
    result = sync(
        new_admissions=top_new_admissions + rediscovered_admissions,
        closed_urls=closed_urls,
        sources_that_failed=sources_that_failed,
        feed_state=feed_state,
        notion_token=notion_token,
        feed_db_id=recommendation_db_id,
        rechecked_urls=set(recheck_candidates),
    )

    logger.info(
        "Run summary: discovered=%d never_seen=%d admitted_new=%d archived_rediscovered=%d "
        "created=%d un_archived=%d updated=%d archived=%d skipped_due_to_source_failure=%d "
        "failed_sources=%s",
        len(discovered),
        len(never_seen),
        len(top_new_admissions),
        len(rediscovered_admissions),
        result.created,
        result.un_archived,
        result.updated,
        result.archived,
        result.skipped_due_to_source_failure,
        sorted(sources_that_failed) or "none",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
