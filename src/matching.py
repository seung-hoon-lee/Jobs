"""Filtering, scoring, and ranking of newly-discovered job posting candidates.

Only applied to new-entry candidates (URLs not already present in the feed) --
existing rows keep their original matched keywords / relevance score
(plan Phase 5 / spec:63).
"""
from datetime import date
from typing import FrozenSet, List, Optional

from src.config import UserConfig
from src.sources.base import JobPosting

KEYWORD_WEIGHT = 0.6
DDAY_WEIGHT = 0.4
NULL_DEADLINE_URGENCY = 0.3
DDAY_URGENCY_WINDOW_DAYS = 30
CAREER_LEVEL_ANY = "경력무관"

# Keywords are OR-matched: a posting is a candidate if it hits ANY configured
# keyword, and hitting more raises its rank. Keyword strength saturates at this
# many distinct matches (1 match -> 0.5, 2+ -> 1.0). Crucially, strength does
# NOT divide by the total number of configured keywords -- otherwise adding
# more keywords to widen the net would paradoxically lower every score and
# admit fewer postings.
KEYWORD_SATURATION = 2

# A brand-new posting must clear this relevance score to be admitted. At the
# default, a single keyword match always clears it (0.5 * 0.6 = 0.30), so the
# real volume controls are per-company dedup + the admission cap below. Raise
# this (e.g. to 0.5, requiring 2 matches or an imminent deadline) to tighten
# the feed.
SCORE_THRESHOLD = 0.3

# Hard ceiling on how many brand-new postings a single run may admit, so a
# broad keyword config can't dump hundreds of rows into Notion at once. The
# per-company dedup below already caps most of the volume; this is the backstop
# the user asked for in place of a fixed top-N.
MAX_NEW_ADMISSIONS = 50


def _parse_deadline(deadline_str: Optional[str]) -> Optional[date]:
    if deadline_str is None:
        return None
    return date.fromisoformat(deadline_str)


def _matched_keywords(candidate: JobPosting, keywords: List[str]) -> List[str]:
    # description is "" for sources that can't cheaply fetch the full posting
    # body (사람인/잡코리아 -- see JobPosting.description), so this transparently
    # degrades to title-only matching for those.
    haystack = f"{candidate.title}\n{candidate.description}".lower()
    return [kw for kw in keywords if kw.lower() in haystack]


def _matches_any(value: str, options: List[str]) -> bool:
    # Containment, not equality: source field formats vary wildly ("서울" vs
    # "경기 성남시", "신입" vs "신입~3년" vs "3년+"), so each configured option
    # is treated as a substring to look for in the candidate's value.
    return any(option in value for option in options)


def _passes_filters(candidate: JobPosting, config: UserConfig, matched_keywords: List[str]) -> bool:
    if not matched_keywords:
        return False
    # For 경력/지역/채용유형: an EMPTY field on the candidate means the source
    # couldn't determine it (원티드 never exposes 채용유형, 직행 never exposes
    # 경력, etc.). Treat "unknown" as "don't reject" rather than excluding the
    # posting outright -- otherwise a preference on a field half the sources
    # leave blank silently drops nearly everything. A 경력무관 posting
    # explicitly accepts every level, so it always satisfies a 경력 filter.
    if (
        config.career_levels
        and candidate.career_level
        and candidate.career_level != CAREER_LEVEL_ANY
        and not _matches_any(candidate.career_level, config.career_levels)
    ):
        return False
    if (
        config.regions
        and candidate.region
        and not _matches_any(candidate.region, config.regions)
    ):
        return False
    if (
        config.employment_types
        and candidate.employment_type
        and not _matches_any(candidate.employment_type, config.employment_types)
    ):
        return False
    # job_functions: JobPosting carries no dedicated job-function field, so this
    # never hard-blocks a candidate (plan Phase 5).
    return True


def _keyword_strength(matched_count: int) -> float:
    # OR-semantics with diminishing returns, independent of how many keywords
    # are configured (see KEYWORD_SATURATION).
    return min(1.0, matched_count / KEYWORD_SATURATION)


def _dday_urgency(deadline: Optional[date], today: date) -> float:
    if deadline is None:
        return NULL_DEADLINE_URGENCY
    days_remaining = (deadline - today).days
    return min(1.0, max(0.0, 1 - days_remaining / DDAY_URGENCY_WINDOW_DAYS))


def _sort_key(candidate: JobPosting):
    deadline = _parse_deadline(candidate.deadline)
    deadline_rank = (1, None) if deadline is None else (0, deadline)
    return (-candidate.relevance_score, deadline_rank, candidate.url)


def _dedupe_by_company(
    candidates: List[JobPosting], existing_companies: FrozenSet[str]
) -> List[JobPosting]:
    """Keep at most one posting per company across the whole recommendation feed.

    `candidates` must already be sorted best-first, so the first posting seen
    for a company is the highest-scoring one and the one kept. A company in
    `existing_companies` (it already has an active row in the feed) is skipped
    entirely. Postings with an empty company name are never deduped against
    each other -- we can't prove they share an employer -- so each is kept.
    """
    kept: List[JobPosting] = []
    seen = set(existing_companies)
    for candidate in candidates:
        company = candidate.company.strip()
        if company:
            if company in seen:
                continue
            seen.add(company)
        kept.append(candidate)
    return kept


def score_and_rank(
    candidates: List[JobPosting],
    config: UserConfig,
    today: date,
    existing_companies: FrozenSet[str] = frozenset(),
) -> List[JobPosting]:
    scored: List[JobPosting] = []

    for candidate in candidates:
        matched_keywords = _matched_keywords(candidate, config.keywords)
        if not _passes_filters(candidate, config, matched_keywords):
            continue

        keyword_strength = _keyword_strength(len(matched_keywords))
        dday_urgency = _dday_urgency(_parse_deadline(candidate.deadline), today)
        score = keyword_strength * KEYWORD_WEIGHT + dday_urgency * DDAY_WEIGHT
        if score < SCORE_THRESHOLD:
            continue

        candidate.matched_keywords = matched_keywords
        candidate.relevance_score = score
        scored.append(candidate)

    scored.sort(key=_sort_key)
    deduped = _dedupe_by_company(scored, existing_companies)
    return deduped[:MAX_NEW_ADMISSIONS]


def rescore(candidates: List[JobPosting], config: UserConfig, today: date) -> List[JobPosting]:
    """Recompute matched_keywords/relevance_score with no filter and no cap.

    Used for archived-URL rediscovery (plan spec:43/55): a posting that
    already earned a place in the feed once, and is now un-archiving, must
    have its fields re-evaluated unconditionally -- it must not be re-subject
    to Stage 3's inclusion filter (e.g. a title that no longer contains a
    configured keyword substring), the score threshold, the per-company dedup,
    or the admission cap, all of which apply only to brand-new candidates.
    """
    for candidate in candidates:
        matched_keywords = _matched_keywords(candidate, config.keywords)
        keyword_strength = _keyword_strength(len(matched_keywords))
        dday_urgency = _dday_urgency(_parse_deadline(candidate.deadline), today)

        candidate.matched_keywords = matched_keywords
        candidate.relevance_score = keyword_strength * KEYWORD_WEIGHT + dday_urgency * DDAY_WEIGHT

    return candidates
