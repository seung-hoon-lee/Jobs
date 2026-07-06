from datetime import date

from src.config import UserConfig
from src.matching import (
    DDAY_URGENCY_WINDOW_DAYS,
    MAX_NEW_ADMISSIONS,
    NULL_DEADLINE_URGENCY,
    SCORE_THRESHOLD,
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


def test_career_level_any_passes_regardless_of_filter():
    # "경력무관" is the posting's claim that it accepts every career level,
    # not a level of its own -- it must pass any configured 경력 filter.
    config = _config(keywords=["python"], career_levels=["신입"])
    posting = _posting(title="Python 개발자", career_level="경력무관")
    assert len(score_and_rank([posting], config, TODAY)) == 1


def test_career_level_containment_matches_freeform_format():
    # 원티드 emits ranges like "신입~3년" rather than the bare Settings-DB
    # label -- containment, not equality, must decide the match.
    config = _config(keywords=["python"], career_levels=["신입"])
    posting = _posting(title="Python 개발자", career_level="신입~3년")
    assert len(score_and_rank([posting], config, TODAY)) == 1


def test_empty_career_level_is_not_rejected():
    # A source that can't determine 경력 leaves it "" -- unknown must not be
    # treated as a mismatch, or a 경력 preference would drop every posting
    # from those sources.
    config = _config(keywords=["python"], career_levels=["신입"])
    posting = _posting(title="Python 개발자", career_level="")
    assert len(score_and_rank([posting], config, TODAY)) == 1


def test_optional_region_filter_allows_match():
    config = _config(keywords=["python"], regions=["서울"])
    posting = _posting(title="Python 개발자", region="서울")
    assert len(score_and_rank([posting], config, TODAY)) == 1


def test_optional_region_filter_allows_compound_region_containing_label():
    # 사람인/잡코리아 ("경기 성남시") sources populate region with more than the
    # bare label the Settings DB offers -- containment, not exact equality,
    # must decide the match.
    config = _config(keywords=["python"], regions=["경기"])
    posting = _posting(
        title="Python 개발자",
        region="대한민국 경기도 성남시 판교역로241번길 20",
    )
    assert len(score_and_rank([posting], config, TODAY)) == 1


def test_optional_region_filter_excludes_non_matching_compound_region():
    config = _config(keywords=["python"], regions=["서울"])
    posting = _posting(title="Python 개발자", region="부산 해운대구")
    assert score_and_rank([posting], config, TODAY) == []


def test_empty_employment_type_is_not_rejected():
    # 원티드 never exposes 채용유형 (always "") -- the bug that stalled the feed:
    # an employment-type preference must not reject a posting whose type is
    # simply unknown.
    config = _config(keywords=["python"], employment_types=["정규직"])
    posting = _posting(title="Python 개발자", employment_type="")
    assert len(score_and_rank([posting], config, TODAY)) == 1


def test_employment_type_mismatch_excluded():
    config = _config(keywords=["python"], employment_types=["정규직"])
    posting = _posting(title="Python 개발자", employment_type="인턴")
    assert score_and_rank([posting], config, TODAY) == []


def test_empty_optional_filters_allow_everything():
    config = _config(keywords=["python"])  # career/region/employment all empty -> unrestricted
    posting = _posting(title="Python 개발자", region="아무데나", career_level="아무거나")
    assert len(score_and_rank([posting], config, TODAY)) == 1


# --- score threshold ---

def test_below_threshold_is_excluded():
    # Matches 1 of 5 keywords (ratio 0.2 -> 0.12) with a null deadline (0.12):
    # score 0.24 < SCORE_THRESHOLD, so it's dropped even though a keyword hit.
    config = _config(keywords=["python", "golang", "rust", "scala", "kotlin"])
    posting = _posting(title="Python Developer", deadline=None)
    result = score_and_rank([posting], config, TODAY)
    assert result == []
    assert 0.2 * 0.6 + NULL_DEADLINE_URGENCY * 0.4 < SCORE_THRESHOLD


# --- per-company dedup ---

def test_dedup_keeps_highest_scoring_posting_per_company():
    config = _config(keywords=["python", "backend"])
    # Same company, both match; the one matching more keywords scores higher.
    weaker = _posting(title="Python 개발자", url="https://x.com/weak", company="동일회사")
    stronger = _posting(title="Python Backend 개발자", url="https://x.com/strong", company="동일회사")
    result = score_and_rank([weaker, stronger], config, TODAY)
    assert len(result) == 1
    assert result[0].url == "https://x.com/strong"


def test_distinct_companies_are_all_kept():
    config = _config(keywords=["python"])
    a = _posting(title="Python A", url="https://x.com/a", company="A사")
    b = _posting(title="Python B", url="https://x.com/b", company="B사")
    result = score_and_rank([a, b], config, TODAY)
    assert {p.company for p in result} == {"A사", "B사"}


def test_company_already_in_feed_is_excluded():
    config = _config(keywords=["python"])
    posting = _posting(title="Python 개발자", company="이미있는회사")
    result = score_and_rank([posting], config, TODAY, existing_companies=frozenset({"이미있는회사"}))
    assert result == []


def test_empty_company_names_are_never_deduped_together():
    config = _config(keywords=["python"])
    a = _posting(title="Python A", url="https://x.com/a", company="")
    b = _posting(title="Python B", url="https://x.com/b", company="")
    result = score_and_rank([a, b], config, TODAY)
    assert len(result) == 2


# --- deterministic tie-break (distinct companies so dedup doesn't collapse) ---

def test_tie_break_earlier_deadline_wins_on_equal_score():
    config = _config(keywords=["python"])
    later = _posting(url="https://x.com/later", company="B사", deadline="2026-10-01")   # >30d -> urgency 0
    earlier = _posting(url="https://x.com/earlier", company="A사", deadline="2026-09-01")  # >30d -> urgency 0
    result = score_and_rank([later, earlier], config, TODAY)
    assert result[0].relevance_score == result[1].relevance_score
    assert [p.url for p in result] == ["https://x.com/earlier", "https://x.com/later"]


def test_tie_break_falls_back_to_url_lexicographic():
    config = _config(keywords=["python"])
    b = _posting(url="https://x.com/b", company="B사", deadline=None)
    a = _posting(url="https://x.com/a", company="A사", deadline=None)
    result = score_and_rank([b, a], config, TODAY)
    assert [p.url for p in result] == ["https://x.com/a", "https://x.com/b"]


# --- admission cap (replaces the old fixed top-10) ---

def test_admission_cap_limits_flood_and_keeps_best_first():
    config = _config(keywords=["python"])
    # Distinct companies (so dedup keeps them all) exceeding the cap; equal
    # score + null deadline means the tie-break is url-lexicographic.
    candidates = [
        _posting(url=f"https://x.com/{i:03d}", company=f"회사{i:03d}", deadline=None)
        for i in range(MAX_NEW_ADMISSIONS + 10)
    ]
    result = score_and_rank(candidates, config, TODAY)
    assert len(result) == MAX_NEW_ADMISSIONS
    assert [p.url for p in result] == [f"https://x.com/{i:03d}" for i in range(MAX_NEW_ADMISSIONS)]


# --- rescore(): no filter, no dedup, no cap (archived-rediscovery path) ---

def test_rescore_does_not_drop_posting_missing_all_keywords():
    config = _config(keywords=["golang", "rust"])  # posting's title matches none of these
    posting = _posting(title="Python 백엔드 개발자", deadline=None)
    result = rescore([posting], config, TODAY)
    assert len(result) == 1, "rescore must not apply score_and_rank's hard keyword filter"
    assert result[0].matched_keywords == []
    assert result[0].relevance_score == NULL_DEADLINE_URGENCY * 0.4


def test_rescore_does_not_cap_or_dedupe():
    config = _config(keywords=["python"])
    # Same company + below-cap-irrelevant count: rescore keeps every one.
    candidates = [_posting(url=f"https://x.com/{i}", company="동일회사", deadline=None) for i in range(15)]
    result = rescore(candidates, config, TODAY)
    assert len(result) == 15
