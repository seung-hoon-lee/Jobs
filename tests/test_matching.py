from datetime import date

from src.config import UserConfig
from src.matching import (
    DDAY_URGENCY_WINDOW_DAYS,
    NULL_DEADLINE_URGENCY,
    TOP_N,
    _dday_urgency,
    rescore,
    score_and_rank,
)
from src.sources.base import JobPosting

TODAY = date(2026, 7, 2)


def _config(**overrides) -> UserConfig:
    base = dict(
        keywords=["python", "backend"],
        job_functions=[],
        career_levels=[],
        regions=[],
        employment_types=[],
        watched_companies=[],
    )
    base.update(overrides)
    return UserConfig(**base)


def _posting(title="Python 백엔드 개발자", url="https://x.com/1", deadline=None, **overrides) -> JobPosting:
    base = dict(
        title=title, company="테스트컴퍼니", url=url, deadline=deadline,
        region="서울", employment_type="정규직", career_level="신입", source="원티드",
    )
    base.update(overrides)
    return JobPosting(**base)


# --- _dday_urgency formula ---

def test_dday_urgency_null_deadline_uses_fixed_value():
    assert _dday_urgency(None, TODAY) == NULL_DEADLINE_URGENCY


def test_dday_urgency_at_window_boundary_is_zero():
    far = date.fromordinal(TODAY.toordinal() + DDAY_URGENCY_WINDOW_DAYS)
    assert _dday_urgency(far, TODAY) == 0.0


def test_dday_urgency_due_today_is_one():
    assert _dday_urgency(TODAY, TODAY) == 1.0


def test_dday_urgency_midpoint():
    mid = date.fromordinal(TODAY.toordinal() + 15)
    assert _dday_urgency(mid, TODAY) == 0.5


def test_dday_urgency_past_deadline_caps_at_one():
    past = date.fromordinal(TODAY.toordinal() - 5)
    assert _dday_urgency(past, TODAY) == 1.0


def test_dday_urgency_beyond_window_caps_at_zero():
    far = date.fromordinal(TODAY.toordinal() + 90)
    assert _dday_urgency(far, TODAY) == 0.0


# --- score_and_rank: keyword ratio + filters ---

def test_keyword_match_ratio_and_score():
    # _matched_keywords is a literal case-insensitive substring match against
    # the title, not a translation -- "backend" only matches literal English
    # text, not its Korean equivalent "백엔드".
    config = _config(keywords=["python", "backend", "java"])
    posting = _posting(title="Python Backend 개발자", deadline=None)
    result = score_and_rank([posting], config, TODAY)
    assert len(result) == 1
    assert sorted(result[0].matched_keywords) == ["backend", "python"]
    expected_score = (2 / 3) * 0.6 + NULL_DEADLINE_URGENCY * 0.4
    assert result[0].relevance_score == expected_score


def test_no_matched_keywords_is_filtered_out():
    config = _config(keywords=["golang", "rust"])
    posting = _posting(title="Python Backend 개발자")
    assert score_and_rank([posting], config, TODAY) == []


def test_keyword_matches_description_when_title_lacks_it():
    # Some sources can't cheaply fetch the full body (description stays "");
    # for those that can, a keyword only mentioned in the body (e.g. a
    # required skill, not the role name) must still count as a match.
    config = _config(keywords=["quantization", "pruning"])
    posting = _posting(
        title="Deep Learning Optimization Engineer",
        description="Required: experience with model quantization and pruning techniques.",
    )
    result = score_and_rank([posting], config, TODAY)
    assert len(result) == 1
    assert sorted(result[0].matched_keywords) == ["pruning", "quantization"]


def test_optional_career_level_filter_excludes_mismatch():
    config = _config(keywords=["python"], career_levels=["경력"])
    posting = _posting(title="Python 개발자", career_level="신입")
    assert score_and_rank([posting], config, TODAY) == []


def test_optional_region_filter_allows_match():
    config = _config(keywords=["python"], regions=["서울"])
    posting = _posting(title="Python 개발자", region="서울")
    assert len(score_and_rank([posting], config, TODAY)) == 1


def test_empty_optional_filters_allow_everything():
    config = _config(keywords=["python"])  # career/region/employment all empty -> unrestricted
    posting = _posting(title="Python 개발자", region="아무데나", career_level="아무거나")
    assert len(score_and_rank([posting], config, TODAY)) == 1


# --- deterministic tie-break ---

def test_tie_break_earlier_deadline_wins_on_equal_score():
    config = _config(keywords=["python"])
    later = _posting(url="https://x.com/later", deadline="2026-10-01")  # >30d out -> urgency 0
    earlier = _posting(url="https://x.com/earlier", deadline="2026-09-01")  # >30d out -> urgency 0
    result = score_and_rank([later, earlier], config, TODAY)
    assert result[0].relevance_score == result[1].relevance_score
    assert [p.url for p in result] == ["https://x.com/earlier", "https://x.com/later"]


def test_tie_break_falls_back_to_url_lexicographic():
    config = _config(keywords=["python"])
    b = _posting(url="https://x.com/b", deadline=None)
    a = _posting(url="https://x.com/a", deadline=None)
    result = score_and_rank([b, a], config, TODAY)
    assert [p.url for p in result] == ["https://x.com/a", "https://x.com/b"]


# --- top-10 admission cap ---

def test_top_n_cap_keeps_lexicographically_first_on_full_tie():
    config = _config(keywords=["python"])
    candidates = [
        _posting(url=f"https://x.com/{i:02d}", deadline=None) for i in range(12)
    ]
    result = score_and_rank(candidates, config, TODAY)
    assert len(result) == TOP_N == 10
    assert [p.url for p in result] == [f"https://x.com/{i:02d}" for i in range(10)]


# --- rescore(): no filter, no cap (archived-rediscovery path) ---

def test_rescore_does_not_drop_posting_missing_all_keywords():
    config = _config(keywords=["golang", "rust"])  # posting's title matches none of these
    posting = _posting(title="Python 백엔드 개발자", deadline=None)
    result = rescore([posting], config, TODAY)
    assert len(result) == 1, "rescore must not apply score_and_rank's hard keyword filter"
    assert result[0].matched_keywords == []
    assert result[0].relevance_score == NULL_DEADLINE_URGENCY * 0.4


def test_rescore_does_not_cap_at_top_n():
    config = _config(keywords=["python"])
    candidates = [_posting(url=f"https://x.com/{i}", deadline=None) for i in range(15)]
    result = rescore(candidates, config, TODAY)
    assert len(result) == 15
