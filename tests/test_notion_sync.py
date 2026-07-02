from datetime import date

import httpx
import pytest
from notion_client.errors import APIErrorCode, APIResponseError

import src.notion_sync as ns
from src.feed_state import FeedState
from src.notion_sync import should_archive, sync
from src.sources.base import JobPosting

TODAY = date(2026, 7, 2)
FEED_DB_ID = "688618c7-0f3c-47b2-8fa4-b3bff7dc77fd"


def _posting(url, title="공고", company="회사", deadline=None, source="원티드", matched_keywords=None, relevance_score=0.5):
    return JobPosting(
        title=title, company=company, url=url, deadline=deadline, region="서울",
        employment_type="정규직", career_level="신입", source=source,
        matched_keywords=matched_keywords, relevance_score=relevance_score,
    )


def _sync(**kwargs):
    kwargs.setdefault("today", TODAY)
    return sync(**kwargs)


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
    _last_instance = None

    def __init__(self, *a, **k):
        self.pages = _FakePages()
        _FakeClient._last_instance = self


@pytest.fixture
def fake_client(monkeypatch):
    monkeypatch.setattr(ns, "Client", _FakeClient)
    return _FakeClient


# --- should_archive: pure safety-gate function ---

def test_should_archive_true_when_source_ok():
    fs = FeedState(by_url={"u": {"notion_page_id": "p", "is_archived": False, "source": "원티드"}})
    assert should_archive("u", fs, set()) is True


def test_should_archive_false_when_source_failed_this_run():
    fs = FeedState(by_url={"u": {"notion_page_id": "p", "is_archived": False, "source": "원티드"}})
    assert should_archive("u", fs, {"원티드"}) is False


def test_should_archive_false_when_already_archived():
    fs = FeedState(by_url={"u": {"notion_page_id": "p", "is_archived": True, "source": "원티드"}})
    assert should_archive("u", fs, set()) is False


def test_should_archive_false_when_url_unknown():
    fs = FeedState(by_url={})
    assert should_archive("missing", fs, set()) is False


# --- ADR-critical: source-failure archive gate (integration through sync()) ---

def test_source_failure_blocks_archiving_even_if_url_in_closed_urls(fake_client):
    feed_state = FeedState(by_url={
        "https://x.com/dead": {"notion_page_id": "p-dead", "is_archived": False, "source": "직행", "마감일": "2026-07-10"},
    })

    result = _sync(
        new_admissions=[],
        closed_urls=["https://x.com/dead"],
        sources_that_failed={"직행"},  # the row's originating source failed this run
        feed_state=feed_state,
        notion_token="t",
        feed_db_id=FEED_DB_ID,
    )

    archived_calls = [c for c in fake_client._last_instance.pages.update_calls if c.get("archived") is True]
    assert archived_calls == [], "must NOT call pages.update(archived=True) when the row's source failed this run"
    assert result.archived == 0
    assert result.skipped_due_to_source_failure == 1


def test_archiving_proceeds_when_source_succeeded(fake_client):
    feed_state = FeedState(by_url={
        "https://x.com/dead": {"notion_page_id": "p-dead", "is_archived": False, "source": "원티드", "마감일": "2026-07-10"},
    })

    result = _sync(
        new_admissions=[], closed_urls=["https://x.com/dead"], sources_that_failed=set(),
        feed_state=feed_state, notion_token="t", feed_db_id=FEED_DB_ID,
    )

    archived_calls = [c for c in fake_client._last_instance.pages.update_calls if c.get("archived") is True]
    assert len(archived_calls) == 1
    assert archived_calls[0]["page_id"] == "p-dead"
    assert result.archived == 1


# --- ADR-critical: archived-URL rediscovery -> un-archive + refresh, never a duplicate create ---

def test_rediscovered_archived_url_unarchives_instead_of_creating(fake_client):
    feed_state = FeedState(by_url={
        "https://x.com/reposted": {"notion_page_id": "p-arch", "is_archived": True, "source": "원티드"},
    })
    posting = _posting("https://x.com/reposted", deadline="2026-07-20", matched_keywords=["java"], relevance_score=0.7)

    result = _sync(
        new_admissions=[posting], closed_urls=[], sources_that_failed=set(),
        feed_state=feed_state, notion_token="t", feed_db_id=FEED_DB_ID,
    )

    pages = fake_client._last_instance.pages
    assert pages.create_calls == [], "must not create a duplicate page for a rediscovered archived URL"
    unarchive_calls = [c for c in pages.update_calls if c.get("archived") is False]
    assert len(unarchive_calls) == 1
    assert unarchive_calls[0]["page_id"] == "p-arch"
    assert unarchive_calls[0]["properties"]["매칭키워드"]["rich_text"][0]["text"]["content"] == "java"
    assert result.un_archived == 1
    assert result.created == 0


def test_genuinely_new_url_creates_a_page(fake_client):
    feed_state = FeedState(by_url={})
    posting = _posting("https://x.com/brand-new", matched_keywords=["python"], relevance_score=0.5)

    result = _sync(
        new_admissions=[posting], closed_urls=[], sources_that_failed=set(),
        feed_state=feed_state, notion_token="t", feed_db_id=FEED_DB_ID,
    )

    pages = fake_client._last_instance.pages
    assert len(pages.create_calls) == 1
    assert pages.create_calls[0]["parent"] == {"type": "data_source_id", "data_source_id": FEED_DB_ID}
    assert pages.create_calls[0]["properties"]["모집상태"]["select"]["name"] == "모집중"
    assert result.created == 1


# --- ADR-critical: existing active rows keep 매칭키워드/관련도점수 untouched ---

def test_existing_active_row_not_admitted_only_updates_status_fields(fake_client):
    feed_state = FeedState(by_url={
        "https://x.com/old": {"notion_page_id": "p-old", "is_archived": False, "source": "사람인", "마감일": "2026-07-04"},
    })

    result = _sync(
        new_admissions=[], closed_urls=[], sources_that_failed=set(),
        feed_state=feed_state, notion_token="t", feed_db_id=FEED_DB_ID,
    )

    pages = fake_client._last_instance.pages
    assert pages.create_calls == []
    status_calls = [c for c in pages.update_calls if c["page_id"] == "p-old"]
    assert len(status_calls) == 1
    properties = status_calls[0]["properties"]
    assert set(properties.keys()) == {"모집상태"}, "매칭키워드/관련도점수 must not appear in this update's properties"
    assert properties["모집상태"]["select"]["name"] == "마감임박"  # 2 days remaining from TODAY
    assert result.updated == 1


def test_existing_active_row_with_no_deadline_is_a_no_op(fake_client):
    feed_state = FeedState(by_url={
        "https://x.com/rolling": {"notion_page_id": "p-roll", "is_archived": False, "source": "사람인", "마감일": None},
    })

    result = _sync(
        new_admissions=[], closed_urls=[], sources_that_failed=set(),
        feed_state=feed_state, notion_token="t", feed_db_id=FEED_DB_ID,
    )

    pages = fake_client._last_instance.pages
    assert pages.update_calls == [], "no deadline means nothing to derive 모집상태 from -- must not issue a no-op write"
    assert result.updated == 0


# --- lastLivenessCheckAt stamping ---

def test_rechecked_but_still_open_row_gets_liveness_stamp(fake_client):
    feed_state = FeedState(by_url={
        "https://x.com/checked": {"notion_page_id": "p-c", "is_archived": False, "source": "원티드", "마감일": "2026-07-04"},
    })

    _sync(
        new_admissions=[], closed_urls=[], sources_that_failed=set(),
        feed_state=feed_state, notion_token="t", feed_db_id=FEED_DB_ID,
        rechecked_urls={"https://x.com/checked"},
    )

    pages = fake_client._last_instance.pages
    call = next(c for c in pages.update_calls if c["page_id"] == "p-c")
    assert call["properties"]["lastLivenessCheckAt"]["date"]["start"] == TODAY.isoformat()


def test_still_open_row_without_rechecked_urls_param_is_not_stamped(fake_client):
    feed_state = FeedState(by_url={
        "https://x.com/checked": {"notion_page_id": "p-c", "is_archived": False, "source": "원티드", "마감일": "2026-07-04"},
    })

    _sync(
        new_admissions=[], closed_urls=[], sources_that_failed=set(),
        feed_state=feed_state, notion_token="t", feed_db_id=FEED_DB_ID,
    )

    pages = fake_client._last_instance.pages
    call = next(c for c in pages.update_calls if c["page_id"] == "p-c")
    assert "lastLivenessCheckAt" not in call["properties"]


def test_archived_row_gets_liveness_stamp_without_rechecked_urls_param(fake_client):
    feed_state = FeedState(by_url={
        "https://x.com/dead": {"notion_page_id": "p-dead", "is_archived": False, "source": "원티드", "마감일": "2026-07-10"},
    })

    _sync(
        new_admissions=[], closed_urls=["https://x.com/dead"], sources_that_failed=set(),
        feed_state=feed_state, notion_token="t", feed_db_id=FEED_DB_ID,
    )

    pages = fake_client._last_instance.pages
    archive_call = next(c for c in pages.update_calls if c.get("archived") is True)
    assert archive_call["properties"]["lastLivenessCheckAt"]["date"]["start"] == TODAY.isoformat()


# --- 429 retry wrapper ---

def _rate_limited_error():
    return APIResponseError(
        code=APIErrorCode.RateLimited, status=429, message="rate limited",
        headers=httpx.Headers(), raw_body_text="{}",
    )


def test_retry_succeeds_after_transient_rate_limiting():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _rate_limited_error()
        return "ok"

    sleeps = []
    assert ns._retry_on_rate_limit(flaky, sleep_fn=sleeps.append) == "ok"
    assert calls["n"] == 3
    assert sleeps == [1, 2]


def test_retry_exhausts_after_three_retries_then_raises():
    def always_429():
        raise _rate_limited_error()

    sleeps = []
    with pytest.raises(APIResponseError):
        ns._retry_on_rate_limit(always_429, sleep_fn=sleeps.append)
    assert sleeps == [1, 2, 4]


def test_retry_does_not_catch_non_rate_limit_errors():
    def not_found():
        raise APIResponseError(
            code=APIErrorCode.ObjectNotFound, status=404, message="not found",
            headers=httpx.Headers(), raw_body_text="{}",
        )

    with pytest.raises(APIResponseError):
        ns._retry_on_rate_limit(not_found, sleep_fn=lambda s: pytest.fail("must not sleep on non-429 errors"))
