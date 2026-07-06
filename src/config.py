"""Loads and validates the single-row Settings DB into a UserConfig."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from notion_client import Client
from notion_client.errors import APIResponseError

KEYWORDS_PROPERTY = "키워드"
JOB_FUNCTIONS_PROPERTY = "직무"
CAREER_LEVELS_PROPERTY = "경력"
REGIONS_PROPERTY = "지역"
EMPLOYMENT_TYPES_PROPERTY = "채용유형"


class ConfigError(Exception):
    """Raised when the Settings DB is missing, malformed, or fails validation."""


@dataclass
class UserConfig:
    keywords: List[str]
    job_functions: List[str]
    career_levels: List[str]
    regions: List[str]
    employment_types: List[str]


def _extract_multi_select(properties: dict, prop_name: str) -> List[str]:
    prop = properties.get(prop_name) or {}
    options = prop.get("multi_select") or []
    return [option["name"] for option in options]


def load_user_config(notion_token: str, config_db_id: str) -> UserConfig:
    client = Client(auth=notion_token)

    try:
        response = client.data_sources.query(data_source_id=config_db_id, page_size=2)
    except APIResponseError as exc:
        raise ConfigError(f"Failed to query Settings DB: {exc}") from exc

    rows = response.get("results", [])
    if len(rows) == 0:
        raise ConfigError("Settings DB has no rows; exactly one row is required.")
    if len(rows) > 1:
        raise ConfigError("Settings DB has more than one row; exactly one row is required.")

    properties = rows[0].get("properties", {})

    keywords = _extract_multi_select(properties, KEYWORDS_PROPERTY)
    if not keywords:
        raise ConfigError(f"'{KEYWORDS_PROPERTY}' must not be empty in the Settings DB row.")

    return UserConfig(
        keywords=keywords,
        job_functions=_extract_multi_select(properties, JOB_FUNCTIONS_PROPERTY),
        career_levels=_extract_multi_select(properties, CAREER_LEVELS_PROPERTY),
        regions=_extract_multi_select(properties, REGIONS_PROPERTY),
        employment_types=_extract_multi_select(properties, EMPLOYMENT_TYPES_PROPERTY),
    )
