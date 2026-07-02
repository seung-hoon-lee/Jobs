"""End-to-end orchestration test for main.main(), covering the ADR-critical
paths across the full pipeline with every external call (Notion, HTTP
scraping) mocked. Complements the focused per-module unit tests elsewhere in
this directory rather than re-deriving them.
"""
import os
from unittest.mock import MagicMock, patch

import pytest

import main
from src.sources.base import JobPosting


@pytest.fixture(autouse=True)
def required_env(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")
    monkeypatch.setenv("RECOMMENDATION_DB_ID", "fake-rec-db")
    monkeypatch.setenv("CONFIG_DB_ID", "fake-config-db")


def _multi_select(name, values):
    return {name: {"multi_select": [{"name": v} for v in values]}}


def _settings_row():
    props = {}
    props.update(_multi_select("키워드", ["백엔드"]))
    props.update(_multi_select("직무", []))
    props.update(_multi_select("경력", []))
    props.update(_multi_select("지역", []))
    props.update(_multi_select("채용유형", []))
    props.update(_multi_select("관심기업", []))
    return {"properties": props}


def _feed_page(url, source, is_archived=False, deadline=None, page_id=None):
    return {
        "id": page_id or f"page-{url}",
        "in_trash": is_archived,
        "properties": {
            "링크": {"url": url},
            "마감일": {"date": {"start": deadline} if deadline else None},
            "모집상태": {"select": {"name": "모집중"}},
            "discoveredAt": {"created_time": "2026-06-01T00:00:00.000Z"},
            "lastLivenessCheckAt": {"date": None},
            "출처": {"select": {"name": source}},
        },
    }


def test_full_pipeline_dedupes_creates_unarchives_and_archives():
    config_client = MagicMock()
    config_client.data_sources.query.return_value = {"results": [_settings_row()], "has_more": False}

    existing_active = _feed_page("https://x/active-1", "원티드", deadline="2026-07-05")
    existing_archived = _feed_page("https://x/archived-1", "사람인", is_archived=True)

    feed_client = MagicMock()

    def fake_feed_query(data_source_id, page_size, **body):
        if body.get("is_archived"):
            return {"results": [existing_archived], "has_more": False}
        return {"results": [existing_active], "has_more": False}

    feed_client.data_sources.query.side_effect = fake_feed_query

    new_posting = JobPosting(
        title="백엔드 신규공고", company="테스트컴퍼니", url="https://x/new-1",
        deadline="2026-08-01", region="서울", employment_type="정규직",
        career_level="경력", source="원티드",
    )
    duplicate_of_new = JobPosting(
        title="dup", company="dup", url="https://x/new-1",  # same URL -> must dedupe to 1 create
        deadline=None, region="서울", employment_type="", career_level="", source="원티드",
    )
    rediscovered = JobPosting(
        title="재게시 공고", company="회사", url="https://x/archived-1",
        deadline="2026-09-01", region="서울", employment_type="정규직",
        career_level="경력", source="사람인",
    )

    sync_client = MagicMock()
    sync_client.pages.create.return_value = {}
    sync_client.pages.update.return_value = {}

    with patch("src.config.Client", return_value=config_client), \
         patch("src.feed_state.Client", return_value=feed_client), \
         patch("src.notion_sync.Client", return_value=sync_client), \
         patch.object(main, "SOURCE_FETCHERS", {
             "원티드": lambda config: [new_posting, duplicate_of_new],
             "사람인": lambda config: [rediscovered],
             "잡코리아": lambda config: [],
             "직행": MagicMock(side_effect=RuntimeError("site structure changed")),
         }), \
         patch.object(main.company_pages, "fetch_postings", return_value=[]), \
         patch.object(main, "check_closed", side_effect=lambda url: url == "https://x/active-1"):
        exit_code = main.main()

    assert exit_code == 0

    create_urls = [c.kwargs["properties"]["링크"]["url"] for c in sync_client.pages.create.call_args_list]
    assert create_urls == ["https://x/new-1"], "duplicate URL from the same source must collapse to one create"

    unarchive_calls = [c for c in sync_client.pages.update.call_args_list if c.kwargs.get("archived") is False]
    assert len(unarchive_calls) == 1
    assert unarchive_calls[0].kwargs["page_id"] == existing_archived["id"]

    archive_calls = [c for c in sync_client.pages.update.call_args_list if c.kwargs.get("archived") is True]
    assert len(archive_calls) == 1
    assert archive_calls[0].kwargs["page_id"] == existing_active["id"]


def test_config_error_exits_nonzero_fail_closed():
    config_client = MagicMock()
    config_client.data_sources.query.return_value = {"results": [], "has_more": False}  # 0 rows -> ConfigError

    with patch("src.config.Client", return_value=config_client):
        assert main.main() == 1


def test_missing_required_env_var_exits(monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        main.main()
    assert exc_info.value.code == 1
