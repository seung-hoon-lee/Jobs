from src.sources.base import is_domestic


def test_korean_region_name_is_domestic():
    assert is_domestic("서울 강남구") is True
    assert is_domestic("경기도 성남시") is True


def test_english_ats_location_naming_korean_city_is_domestic():
    # Greenhouse/Lever career pages render English-locale location text.
    assert is_domestic("Seoul, South Korea") is True
    assert is_domestic("Busan") is True


def test_kr_company_domain_is_domestic():
    assert is_domestic("", company_domain="toss.kr") is True


def test_foreign_location_with_blocked_company_is_excluded():
    assert is_domestic("California", company="Google Korea") is False
    assert is_domestic("", company="Amazon") is False


def test_ambiguous_location_defaults_to_excluded():
    # Spec's "해외 기업은 제외" is a hard filter -- an inconclusive case must
    # resolve to exclude, not include.
    assert is_domestic("Somewhere Else", company="Unknown Foreign Co") is False
    assert is_domestic("California") is False


def test_empty_location_and_company_defaults_to_excluded():
    assert is_domestic("") is False
