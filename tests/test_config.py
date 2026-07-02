from unittest.mock import MagicMock, patch

import httpx
import pytest
from notion_client.errors import APIErrorCode, APIResponseError

from src.config import ConfigError, load_user_config


def _multi_select(name, values):
    return {name: {"multi_select": [{"name": v} for v in values]}}


def _row(keywords=("백엔드",), career_levels=(), regions=(), employment_types=(), job_functions=(), watched_companies=()):
    props = {}
    props.update(_multi_select("키워드", list(keywords)))
    props.update(_multi_select("직무", list(job_functions)))
    props.update(_multi_select("경력", list(career_levels)))
    props.update(_multi_select("지역", list(regions)))
    props.update(_multi_select("채용유형", list(employment_types)))
    props.update(_multi_select("관심기업", list(watched_companies)))
    return {"properties": props}


def _fake_client(rows):
    client = MagicMock()
    client.data_sources.query.return_value = {"results": rows, "has_more": False}
    return client


def test_zero_rows_raises_config_error():
    client = _fake_client([])
    with patch("src.config.Client", return_value=client):
        with pytest.raises(ConfigError):
            load_user_config("token", "config-db")


def test_multiple_rows_raises_config_error():
    client = _fake_client([_row(), _row()])
    with patch("src.config.Client", return_value=client):
        with pytest.raises(ConfigError):
            load_user_config("token", "config-db")


def test_empty_keywords_raises_config_error():
    client = _fake_client([_row(keywords=())])
    with patch("src.config.Client", return_value=client):
        with pytest.raises(ConfigError):
            load_user_config("token", "config-db")


def test_valid_single_row_returns_user_config():
    client = _fake_client([_row(
        keywords=["백엔드", "파이썬"],
        career_levels=["신입"],
        watched_companies=["토스"],
    )])
    with patch("src.config.Client", return_value=client):
        config = load_user_config("token", "config-db")

    assert config.keywords == ["백엔드", "파이썬"]
    assert config.career_levels == ["신입"]
    assert config.regions == []
    assert config.employment_types == []
    assert config.job_functions == []
    assert config.watched_companies == ["토스"]
    client.data_sources.query.assert_called_with(data_source_id="config-db", page_size=2)


def test_notion_api_error_wrapped_as_config_error_fail_closed():
    client = MagicMock()
    client.data_sources.query.side_effect = APIResponseError(
        code=APIErrorCode.ObjectNotFound, status=404, message="not found",
        headers=httpx.Headers(), raw_body_text="{}",
    )
    with patch("src.config.Client", return_value=client):
        with pytest.raises(ConfigError):
            load_user_config("token", "config-db")
