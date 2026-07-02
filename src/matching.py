"""Filtering, scoring, and ranking of newly-discovered job posting candidates.

Only applied to new-entry candidates (URLs not already present in the feed) --
existing rows keep their original matched keywords / relevance score
(plan Phase 5 / spec:63).
"""
from datetime import date
from typing import List, Optional

from src.config import UserConfig
from src.sources.base import JobPosting

KEYWORD_WEIGHT = 0.6
DDAY_WEIGHT = 0.4
NULL_DEADLINE_URGENCY = 0.3
DDAY_URGENCY_WINDOW_DAYS = 30
TOP_N = 10
CAREER_LEVEL_ANY = "경력무관"


def _parse_deadline(deadline_str: Optional[str]) -> Optional[date]:
    if deadline_str is None:
        return None
    return date.fromisoformat(deadline_str)


def _matched_keywords(candidate: JobPosting, keywords: List[str]) -> List[str]:
    # description is "" for sources that can't cheaply fetch the full
    # posting body (사람인/잡코리아 -- see JobPosting.description), so this
    # transparently degrades to title-only matching for those.
    haystack = f"{candidate.title}\n{candidate.description}".lower()
    return [kw for kw in keywords if kw.lower() in haystack]


def _passes_filters(candidate: JobPosting, config: UserConfig, matched_keywords: List[str]) -> bool:
    if not matched_keywords:
        return False
    # A posting labeled "경력무관" (any career level accepted) must satisfy any
    # configured 경력 filter -- it's the posting's own claim that it's open to
    # every level, not a distinct level of its own to be matched exactly.
    if (
        config.career_levels
        and candidate.career_level != CAREER_LEVEL_ANY
        and candidate.career_level not in config.career_levels
    ):
        return False
    # candidate.region is a short exact label for some sources ("서울") but a
    # compound/full-address string for others (사람인 "경기 성남시", 관심기업's
    # GreetingHR full postal address) -- containment, not equality, is the
    # only check that works across all of them.
    if config.regions and not any(region in candidate.region for region in config.regions):
        return False
    if config.employment_types and candidate.employment_type not in config.employment_types:
        return False
    # job_functions: JobPosting carries no dedicated job-function field, so a
    # title-substring match is best-effort/informational at best. Per plan
    # Phase 5 this never hard-blocks a candidate.
    return True


def _dday_urgency(deadline: Optional[date], today: date) -> float:
    if deadline is None:
        return NULL_DEADLINE_URGENCY
    days_remaining = (deadline - today).days
    return min(1.0, max(0.0, 1 - days_remaining / DDAY_URGENCY_WINDOW_DAYS))


def _sort_key(candidate: JobPosting):
    deadline = _parse_deadline(candidate.deadline)
    deadline_rank = (1, None) if deadline is None else (0, deadline)
    return (-candidate.relevance_score, deadline_rank, candidate.url)


def score_and_rank(candidates: List[JobPosting], config: UserConfig, today: date) -> List[JobPosting]:
    total_keywords = len(config.keywords)
    scored: List[JobPosting] = []

    for candidate in candidates:
        matched_keywords = _matched_keywords(candidate, config.keywords)
        if not _passes_filters(candidate, config, matched_keywords):
            continue

        keyword_match_ratio = (len(matched_keywords) / total_keywords) if total_keywords else 0.0
        dday_urgency = _dday_urgency(_parse_deadline(candidate.deadline), today)

        candidate.matched_keywords = matched_keywords
        candidate.relevance_score = keyword_match_ratio * KEYWORD_WEIGHT + dday_urgency * DDAY_WEIGHT
        scored.append(candidate)

    scored.sort(key=_sort_key)
    return scored[:TOP_N]


def rescore(candidates: List[JobPosting], config: UserConfig, today: date) -> List[JobPosting]:
    """Recompute matched_keywords/relevance_score with no filter and no top-N cap.

    Used for archived-URL rediscovery (plan spec:43/55): a posting that
    already earned a place in the feed once, and is now un-archiving, must
    have its fields re-evaluated unconditionally -- it must not be re-subject
    to Stage 3's inclusion filter (e.g. a title that no longer contains a
    configured keyword substring) or the daily top-10 admission cap, both of
    which apply only to brand-new candidates.
    """
    total_keywords = len(config.keywords)

    for candidate in candidates:
        matched_keywords = _matched_keywords(candidate, config.keywords)
        keyword_match_ratio = (len(matched_keywords) / total_keywords) if total_keywords else 0.0
        dday_urgency = _dday_urgency(_parse_deadline(candidate.deadline), today)

        candidate.matched_keywords = matched_keywords
        candidate.relevance_score = keyword_match_ratio * KEYWORD_WEIGHT + dday_urgency * DDAY_WEIGHT

    return candidates
