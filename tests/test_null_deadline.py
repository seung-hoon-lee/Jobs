"""Confirms a JobPosting with deadline=None ("상시채용") flows through every
stage without raising, per plan Phase 5/6 and the Verification Steps section.
"""
from datetime import date

import pytest

from src.config import UserConfig
from src.feed_state import FeedState
from src.liveness import select_recheck_candidates
from src.matching import NULL_DEADLINE_URGENCY, rescore, score_and_rank
from src.notion_sync import sync
from src.sources.base import JobPosting

TODAY = date(2026, 7, 2)


def _config():
    return UserConfig(keywords=["python"], job_functions=[], career_levels=[], regions=[], employment_types=[])


def _rolling_posting(url="https://x.com/rolling"):
    return JobPosting(
        title="Python 백엔드 개발자 (상시채용)", company="회사", url=url, deadline=None,
        region="서울", employment_type="정규직", career_level="신입", source="원티드",
    )


def test_score_and_rank_handles_null_deadline_with_fixed_urgency():
    result = score_and_rank([_rolling_posting()], _config(), TODAY)
    assert len(result) == 1
    assert result[0].relevance_score == 1.0 * 0.6 + NULL_DEADLINE_URGENCY * 0.4


def test_rescore_handles_null_deadline_without_raising():
    result = rescore([_rolling_posting()], _config(), TODAY)
    assert len(result) == 1
    assert result[0].relevance_score == 1.0 * 0.6 + NULL_DEADLINE_URGENCY * 0.4


class _FakePages:
    def __init__(self):
        self.create_calls = []
        self.update_calls = []

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return {"id": "new-page-id"}

    def update(self, **kwargs):
        self.update_calls.append(kwargs)
        return {"id": kwargs.get("page_id")}


class _FakeClient:
    def __init__(self, *a, **k):
        self.pages = _FakePages()


def test_notion_sync_create_handles_null_deadline_without_raising(monkeypatch):
    import src.notion_sync as ns
    monkeypatch.setattr(ns, "Client", _FakeClient)

    posting = _rolling_posting()
    posting.matched_keywords = ["python"]
    posting.relevance_score = 0.72

    result = sync(
        new_admissions=[posting], closed_urls=[], sources_that_failed=set(),
        feed_state=FeedState(by_url={}), notion_token="t", feed_db_id="feed-db", today=TODAY,
    )

    assert result.created == 1


def test_notion_sync_unarchive_handles_null_deadline_without_raising(monkeypatch):
    import src.notion_sync as ns
    monkeypatch.setattr(ns, "Client", _FakeClient)

    posting = _rolling_posting("https://x.com/reposted-rolling")
    feed_state = FeedState(by_url={
        "https://x.com/reposted-rolling": {"notion_page_id": "p-arch", "is_archived": True, "source": "원티드"},
    })

    result = sync(
        new_admissions=[posting], closed_urls=[], sources_that_failed=set(),
        feed_state=feed_state, notion_token="t", feed_db_id="feed-db", today=TODAY,
    )

    assert result.un_archived == 1


def test_notion_sync_existing_active_row_with_null_deadline_is_a_no_op_not_a_crash(monkeypatch):
    import src.notion_sync as ns
    monkeypatch.setattr(ns, "Client", _FakeClient)

    feed_state = FeedState(by_url={
        "https://x.com/rolling-old": {"notion_page_id": "p-old", "is_archived": False, "source": "원티드", "마감일": None},
    })

    result = sync(
        new_admissions=[], closed_urls=[], sources_that_failed=set(),
        feed_state=feed_state, notion_token="t", feed_db_id="feed-db", today=TODAY,
    )

    assert result.updated == 0  # nothing to derive 모집상태 from, and it must not raise


def test_select_recheck_candidates_null_deadline_never_checked():
    fs = FeedState(by_url={"u": {"is_archived": False, "마감일": None, "lastLivenessCheckAt": None}})
    assert select_recheck_candidates(fs, TODAY) == ["u"]


def test_select_recheck_candidates_null_deadline_recently_checked_excluded():
    fs = FeedState(by_url={"u": {"is_archived": False, "마감일": None, "lastLivenessCheckAt": "2026-07-01"}})
    assert select_recheck_candidates(fs, TODAY) == []
