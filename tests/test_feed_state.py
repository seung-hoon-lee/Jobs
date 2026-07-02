from unittest.mock import MagicMock, patch

from src.feed_state import load_feed_state


def _page(page_id, url, source, is_archived=False, deadline=None, status=None, last_liveness=None):
    return {
        "id": page_id,
        "in_trash": is_archived,
        "properties": {
            "링크": {"url": url},
            "마감일": {"date": {"start": deadline} if deadline else None},
            "모집상태": {"select": {"name": status} if status else None},
            "discoveredAt": {"created_time": "2026-06-01T00:00:00.000Z"},
            "lastLivenessCheckAt": {"date": {"start": last_liveness} if last_liveness else None},
            "출처": {"select": {"name": source} if source else None},
        },
    }


def test_active_and_archived_rows_are_both_indexed_by_url():
    active_page = _page("p-active", "https://example.com/1", "원티드", deadline="2026-07-10", status="모집중")
    archived_page = _page("p-archived", "https://example.com/2", "사람인", is_archived=True, status="마감")

    client = MagicMock()

    def fake_query(data_source_id, page_size, **body):
        if body.get("in_trash"):
            return {"results": [archived_page], "has_more": False}
        return {"results": [active_page], "has_more": False}

    client.data_sources.query.side_effect = fake_query

    with patch("src.feed_state.Client", return_value=client):
        state = load_feed_state("token", "feed-db")

    assert set(state.by_url) == {"https://example.com/1", "https://example.com/2"}
    assert state.by_url["https://example.com/1"]["is_archived"] is False
    assert state.by_url["https://example.com/1"]["source"] == "원티드"
    assert state.by_url["https://example.com/2"]["is_archived"] is True

    # Confirms the earlier databases.query -> data_sources.query fix: must call
    # with data_source_id, not database_id.
    client.data_sources.query.assert_any_call(data_source_id="feed-db", page_size=100)


def test_pagination_follows_next_cursor():
    page_1 = _page("p1", "https://example.com/1", "원티드")
    page_2 = _page("p2", "https://example.com/2", "원티드")

    client = MagicMock()
    call_count = {"n": 0}

    def fake_query(data_source_id, page_size, **body):
        if body.get("in_trash"):
            return {"results": [], "has_more": False}
        call_count["n"] += 1
        if call_count["n"] == 1:
            assert "start_cursor" not in body
            return {"results": [page_1], "has_more": True, "next_cursor": "cursor-2"}
        assert body["start_cursor"] == "cursor-2"
        return {"results": [page_2], "has_more": False}

    client.data_sources.query.side_effect = fake_query

    with patch("src.feed_state.Client", return_value=client):
        state = load_feed_state("token", "feed-db")

    assert len(state.by_url) == 2
    assert call_count["n"] == 2


def test_archived_query_failure_falls_back_to_active_only():
    from notion_client.errors import APIErrorCode, APIResponseError
    import httpx

    active_page = _page("p-active", "https://example.com/1", "원티드")
    client = MagicMock()

    def fake_query(data_source_id, page_size, **body):
        if body.get("in_trash"):
            raise APIResponseError(
                code=APIErrorCode.ValidationError, status=400, message="unsupported",
                headers=httpx.Headers(), raw_body_text="{}",
            )
        return {"results": [active_page], "has_more": False}

    client.data_sources.query.side_effect = fake_query

    with patch("src.feed_state.Client", return_value=client):
        state = load_feed_state("token", "feed-db")  # must not raise

    assert set(state.by_url) == {"https://example.com/1"}


def test_pages_missing_link_url_are_skipped():
    page_without_url = _page("p-no-url", url=None, source="원티드")
    page_without_url["properties"]["링크"] = {"url": None}

    client = MagicMock()
    client.data_sources.query.return_value = {"results": [page_without_url], "has_more": False}

    with patch("src.feed_state.Client", return_value=client):
        state = load_feed_state("token", "feed-db")

    assert state.by_url == {}
