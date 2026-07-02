from datetime import date
from unittest.mock import patch

import requests

from src.feed_state import FeedState
from src.liveness import check_closed, select_recheck_candidates

TODAY = date(2026, 7, 2)


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


# --- check_closed ---

def test_check_closed_404_is_closed():
    with patch("src.liveness.requests.get", return_value=_FakeResponse(404)):
        assert check_closed("https://x.com/gone") is True


def test_check_closed_410_is_closed():
    with patch("src.liveness.requests.get", return_value=_FakeResponse(410)):
        assert check_closed("https://x.com/gone") is True


def test_check_closed_200_with_closure_marker_is_closed():
    with patch("src.liveness.requests.get", return_value=_FakeResponse(200, text="<html>이 채용은 마감 되었습니다</html>")):
        assert check_closed("https://x.com/job") is True


def test_check_closed_200_normal_content_is_not_closed():
    with patch("src.liveness.requests.get", return_value=_FakeResponse(200, text="<html>백엔드 엔지니어 채용중</html>")):
        assert check_closed("https://x.com/job") is False


def test_check_closed_network_exception_resolves_to_not_closed():
    with patch("src.liveness.requests.get", side_effect=requests.exceptions.Timeout("timed out")):
        assert check_closed("https://x.com/job") is False


def test_check_closed_unexpected_status_resolves_to_not_closed():
    with patch("src.liveness.requests.get", return_value=_FakeResponse(500)):
        assert check_closed("https://x.com/job") is False


# --- select_recheck_candidates ---

def test_archived_row_never_selected():
    fs = FeedState(by_url={"u": {"is_archived": True, "마감일": "2026-07-03", "lastLivenessCheckAt": None}})
    assert select_recheck_candidates(fs, TODAY) == []


def test_deadline_within_7_days_selected():
    fs = FeedState(by_url={"u": {"is_archived": False, "마감일": "2026-07-05", "lastLivenessCheckAt": None}})
    assert select_recheck_candidates(fs, TODAY) == ["u"]


def test_deadline_past_is_still_selected():
    fs = FeedState(by_url={"u": {"is_archived": False, "마감일": "2026-06-01", "lastLivenessCheckAt": None}})
    assert select_recheck_candidates(fs, TODAY) == ["u"]


def test_deadline_beyond_7_days_not_selected():
    fs = FeedState(by_url={"u": {"is_archived": False, "마감일": "2026-08-01", "lastLivenessCheckAt": None}})
    assert select_recheck_candidates(fs, TODAY) == []


def test_null_deadline_never_checked_is_selected():
    fs = FeedState(by_url={"u": {"is_archived": False, "마감일": None, "lastLivenessCheckAt": None}})
    assert select_recheck_candidates(fs, TODAY) == ["u"]


def test_null_deadline_checked_over_a_week_ago_is_selected():
    fs = FeedState(by_url={"u": {"is_archived": False, "마감일": None, "lastLivenessCheckAt": "2026-06-24"}})  # 8 days ago
    assert select_recheck_candidates(fs, TODAY) == ["u"]


def test_null_deadline_checked_recently_is_not_selected():
    fs = FeedState(by_url={"u": {"is_archived": False, "마감일": None, "lastLivenessCheckAt": "2026-06-30"}})  # 2 days ago
    assert select_recheck_candidates(fs, TODAY) == []
