from datetime import date, timedelta
from unittest.mock import patch

from src.sources import company_pages, jobkorea, saramin, wanted, zighang

EMPTY_HTML = "<html><body></body></html>"


class _FakeResponse:
    def __init__(self, text="", status_code=200, json_data=None, content=None):
        self.text = text
        self.status_code = status_code
        self._json_data = json_data
        self.content = content if content is not None else text.encode("utf-8")

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_data


def _get_returning_then_empty(first_html):
    """First call returns first_html; every subsequent call returns an empty
    listing page so the source module's page loop breaks after page 1."""
    calls = {"n": 0}

    def fake_get(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(first_html)
        return _FakeResponse(EMPTY_HTML)

    fake_get.calls = calls
    return fake_get


# --- wanted.py ---
# wanted.co.kr's list page is client-rendered React with no job data in the
# server HTML, so wanted.py calls the public listing JSON API instead
# (verified live) -- these fixtures mirror that API's real response shape.

WANTED_JSON_PAGE = {
    "data": [
        {
            "id": 12345,
            "position": "백엔드 엔지니어",
            "company": {"name": "테스트컴퍼니"},
            "address": {"location": "서울", "country": "한국"},
            "due_time": "2026-08-01",
            "annual_from": 3,
            "annual_to": 5,
        },
        {
            "id": 99999,
            "position": "Sales Manager",
            "company": {"name": "Google Korea"},
            "address": {"location": "California", "country": "미국"},
            "due_time": None,
            "annual_from": None,
            "annual_to": None,
        },
    ],
    "links": {"next": None},
}

WANTED_EMPTY_PAGE = {"data": [], "links": {"next": None}}

WANTED_JOB_DETAIL = {
    "job": {
        "detail": {
            "main_tasks": "모델 quantization 및 pruning 최적화 업무",
            "requirements": "관련 경력 3년 이상",
        }
    }
}


def test_wanted_parses_fields_and_filters_foreign_posting():
    calls = {"n": 0}

    def fake_get(url, *args, **kwargs):
        calls["n"] += 1
        if url == wanted.API_URL:
            return _FakeResponse(json_data=WANTED_JSON_PAGE)
        assert url == wanted.DETAIL_API_URL_TEMPLATE.format(id=12345), "only the domestic posting should be fetched"
        return _FakeResponse(json_data=WANTED_JOB_DETAIL)

    with patch("src.sources.wanted.requests.get", side_effect=fake_get), \
         patch("src.sources.wanted.time.sleep"):
        postings = wanted.fetch_postings()

    assert len(postings) == 1  # Google Korea / California posting filtered out
    p = postings[0]
    assert p.title == "백엔드 엔지니어"
    assert p.company == "테스트컴퍼니"
    assert p.url == "https://www.wanted.co.kr/wd/12345"
    assert p.deadline == "2026-08-01"
    assert p.region == "서울"
    assert p.employment_type == ""
    assert p.career_level == "3~5년"
    assert p.source == "원티드"
    assert "quantization" in p.description.lower()
    assert "관련 경력 3년 이상" in p.description
    # 1 list-page request + 1 detail request for the single surviving (domestic) posting
    assert calls["n"] == 2, "links.next=None must stop list pagination after the first page"


def test_wanted_stops_after_empty_page():
    calls = {"n": 0}

    def fake_get(*args, **kwargs):
        calls["n"] += 1
        return _FakeResponse(json_data=WANTED_EMPTY_PAGE)

    with patch("src.sources.wanted.requests.get", side_effect=fake_get):
        postings = wanted.fetch_postings()
    assert postings == []
    assert calls["n"] == 1, "loop must break after the first empty page, not scan MAX_PAGES"


# --- saramin.py ---
# saramin.co.kr's search endpoint only server-renders real results for a
# non-empty `searchword` (an empty one shows a client-only "인기있는
# 채용정보" recommendation widget instead, verified live) -- so
# saramin.fetch_postings is keyword-driven rather than keyword-independent.

SARAMIN_HTML = """
<html><body>
<div id="recruit_info_list">
<div class="content">
<div class="item_recruit">
  <div class="area_job">
    <h2 class="job_tit"><a href="/zf_user/jobs/relay/view?rec_idx=1" title="백엔드 개발자 (정식 타이틀)">백엔드 <b>개발자</b> 채용</a></h2>
  </div>
  <div class="area_corp"><strong class="corp_name"><a href="/zf_user/company-info/view?csn=1">사람인컴퍼니</a></strong></div>
  <div class="job_condition">
    <span>경기 성남시</span>
    <span>신입</span>
    <span>학력무관</span>
    <span>정규직</span>
  </div>
  <div class="job_date"><span class="date">D-6</span></div>
</div>
</div>
</div>
</body></html>
"""


def test_saramin_parses_fields():
    fake_get = _get_returning_then_empty(SARAMIN_HTML)
    with patch("src.sources.saramin.requests.get", side_effect=fake_get):
        postings = saramin.fetch_postings(["백엔드"])

    assert len(postings) == 1
    p = postings[0]
    assert p.title == "백엔드 개발자 (정식 타이틀)"  # prefers the <a title> attribute
    assert p.company == "사람인컴퍼니"
    assert p.url == "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=1"
    assert p.region == "경기 성남시"
    assert p.career_level == "신입"
    assert p.employment_type == "정규직"
    assert p.deadline == (date.today() + timedelta(days=6)).isoformat()
    assert p.source == "사람인"


def test_saramin_dedupes_across_keywords_and_caps_keyword_count():
    fake_get = _get_returning_then_empty(SARAMIN_HTML)
    with patch("src.sources.saramin.requests.get", side_effect=fake_get):
        postings = saramin.fetch_postings(["백엔드", "개발자", "서버", "엔지니어", "인프라", "여섯번째키워드무시됨"])

    assert len(postings) == 1, "same URL returned for multiple keywords must dedupe to one posting"
    # MAX_KEYWORDS=5 (6th keyword ignored): keyword 1's page-1 call gets the
    # only non-empty response `_get_returning_then_empty` ever returns, so
    # its page-2 call fires too (2 calls) before the empty page stops it;
    # keywords 2-5 each see an empty page-1 response immediately (1 call
    # each). Total = 2 + 4*1 = 6.
    assert fake_get.calls["n"] == 6


# --- jobkorea.py ---
# jobkorea.co.kr/Recruit/Joblist server-renders a real results table
# (verified live); its true pagination is an AJAX fragment gated by
# session/referrer state that a plain GET can't reach (verified: a
# `Page_No=2` request on the full page returns the identical first-page
# rows), so this source only fetches the single default page.

JOBKOREA_HTML = """
<html><body>
<table><tbody>
<tr class="devloopArea" data-gno="12345">
  <td class="tplCo"><a class="link normalLog" href="/Recruit/Co_Read/C/1">잡코리아컴퍼니</a></td>
  <td class="tplTit">
    <div class="titBx">
      <strong><a class="link normalLog" href="/Recruit/GI_Read/12345" title="백엔드 개발자 채용">백엔드 개발자 채용</a></strong>
      <p class="etc">
        <span class="cell">경력무관</span>
        <span class="cell">학력무관</span>
        <span class="cell">부산 해운대구</span>
        <span class="cell">정규직</span>
      </p>
    </div>
  </td>
  <td class="tplPrv"></td>
  <td class="odd"><span class="date dotum"><span class="tahoma">~07/25</span>(토)</span></td>
</tr>
</tbody></table>
</body></html>
"""


def test_jobkorea_parses_fields():
    with patch("src.sources.jobkorea.requests.get", return_value=_FakeResponse(JOBKOREA_HTML)):
        postings = jobkorea.fetch_postings()

    assert len(postings) == 1
    p = postings[0]
    assert p.title == "백엔드 개발자 채용"
    assert p.company == "잡코리아컴퍼니"
    assert p.url == "https://www.jobkorea.co.kr/Recruit/GI_Read/12345"
    assert p.region == "부산 해운대구"
    assert p.career_level == "경력무관"
    assert p.employment_type == "정규직"
    assert p.source == "잡코리아"


# --- zighang.py ---
# zighang.com's listing page streams job cards as client-side RSC payloads
# (no job data in the raw HTML) and its /api/ namespace is disallowed by
# robots.txt, so zighang.py instead walks the site's published sitemap
# (linked from robots.txt) to discover recent `/recruitment/{uuid}` detail
# URLs, then parses each detail page's schema.org JobPosting JSON-LD block
# (verified live) -- there is no listing-page HTML parser to test here.

SITEMAP_INDEX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://zighang.com/seo/sitemap/sitemap-company-1.xml</loc><lastmod>2025-09-14</lastmod></sitemap>
  <sitemap><loc>https://zighang.com/seo/sitemap/sitemap-recruitment-1.xml</loc><lastmod>2026-07-01</lastmod></sitemap>
</sitemapindex>
"""

RECRUITMENT_SHARD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://zighang.com/recruitment/aaa</loc><lastmod>2026-07-01</lastmod></url>
  <url><loc>https://zighang.com/recruitment/bbb</loc><lastmod>2026-07-01</lastmod></url>
</urlset>
"""


def _detail_html(ld_json: str) -> str:
    return f"""
<html><head>
<script type="application/ld+json">{ld_json}</script>
</head><body></body></html>
"""


DOMESTIC_JOB_LD_JSON = """{
  "@context": "https://schema.org",
  "@type": "JobPosting",
  "title": "데이터 엔지니어",
  "description": "<p>모델 quantization 및 pruning 경험 우대</p>",
  "datePosted": "2026-07-01T10:00:00",
  "validThrough": "2026-08-05T23:59:59",
  "employmentType": "CONTRACTOR",
  "hiringOrganization": {"@type": "Organization", "name": "직행컴퍼니"},
  "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress", "addressRegion": "대전", "addressCountry": "KR"}},
  "url": "https://zighang.com/recruitment/aaa"
}"""

FOREIGN_JOB_LD_JSON = """{
  "@context": "https://schema.org",
  "@type": "JobPosting",
  "title": "Remote Support Engineer",
  "datePosted": "2026-07-01T10:00:00",
  "validThrough": null,
  "employmentType": "FULL_TIME",
  "hiringOrganization": {"@type": "Organization", "name": "Foreign Co"},
  "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress", "addressRegion": "California", "addressCountry": "US"}},
  "url": "https://zighang.com/recruitment/bbb"
}"""


def test_zighang_walks_sitemap_and_parses_detail_json_ld():
    def fake_get(url, *args, **kwargs):
        if url == zighang.SITEMAP_INDEX_URL:
            return _FakeResponse(content=SITEMAP_INDEX_XML.encode("utf-8"))
        if url == "https://zighang.com/seo/sitemap/sitemap-recruitment-1.xml":
            return _FakeResponse(content=RECRUITMENT_SHARD_XML.encode("utf-8"))
        if url == "https://zighang.com/recruitment/aaa":
            return _FakeResponse(_detail_html(DOMESTIC_JOB_LD_JSON))
        if url == "https://zighang.com/recruitment/bbb":
            return _FakeResponse(_detail_html(FOREIGN_JOB_LD_JSON))
        raise AssertionError(f"unexpected URL: {url}")

    with patch("src.sources.zighang.requests.get", side_effect=fake_get), \
         patch("src.sources.zighang.time.sleep"):
        postings = zighang.fetch_postings()

    assert len(postings) == 1  # foreign California posting filtered out
    p = postings[0]
    assert p.title == "데이터 엔지니어"
    assert p.company == "직행컴퍼니"
    assert p.url == "https://zighang.com/recruitment/aaa"
    assert p.deadline == "2026-08-05"
    assert p.region == "대전"
    assert p.employment_type == "계약직"  # CONTRACTOR -> 계약직
    assert p.source == "직행"
    assert p.description == "모델 quantization 및 pruning 경험 우대"  # HTML tags stripped


# --- company_pages.py ---
# Greenhouse and Lever expose stable public listing JSON APIs
# (boards-api.greenhouse.io, api.lever.co) rather than requiring HTML
# scraping -- verified live against real boards -- so company_pages.py calls
# those instead of parsing the career page's HTML.

GREENHOUSE_JOBS_JSON = {
    "jobs": [
        {
            "title": "백엔드 엔지니어",
            "location": {"name": "서울, 대한민국"},
            "absolute_url": "https://boards.greenhouse.io/toss/jobs/111",
            "content": "<p>모델 quantization/pruning 경험자 우대</p>",
        },
        {
            "title": "Sales Rep",
            "location": {"name": "New York, US"},
            "absolute_url": "https://boards.greenhouse.io/toss/jobs/222",
            "content": "<p>Sales experience required</p>",
        },
    ]
}

LEVER_JOBS_JSON = [
    {
        "text": "프론트엔드 엔지니어",
        "hostedUrl": "https://jobs.lever.co/example/222",
        "categories": {"location": "Seoul, South Korea", "commitment": "Full-time"},
        "descriptionPlain": "React, TypeScript 경험자 우대",
    },
]

GENERIC_HTML = """
<html><body>
<ul>
  <li><a href="/careers/333">백엔드 엔지니어</a><span class="location">서울</span></li>
</ul>
</body></html>
"""

# GreetingHR (a Korean ATS, often deployed on a custom domain rather than a
# recognizable shared domain) embeds its full openings list as a react-query
# cache entry inside Next.js's __NEXT_DATA__ blob -- verified live against
# three real boards. This fixture mirrors that real shape.
GREETINGHR_NEXT_DATA = """{
  "props": {
    "pageProps": {
      "dehydratedState": {
        "queries": [
          {"queryKey": ["publicCareer", "getCareerBootInfo", {}], "state": {"data": {}}},
          {"queryKey": ["openings"], "state": {"data": [
            {
              "openingId": 444,
              "title": "Deep Learning Optimization Engineer",
              "dueDate": null,
              "openingJobPosition": {"openingJobPositions": [{
                "workspacePlace": {"place": "대한민국 서울특별시 강남구 테헤란로 1"},
                "jobPositionCareer": {"careerType": "EXPERIENCED"},
                "jobPositionEmployment": {"employmentType": "FULL_TIME_WORKER"}
              }]}
            },
            {
              "openingId": 555,
              "title": "US Sales Lead",
              "dueDate": "2026-09-01T00:00:00Z",
              "openingJobPosition": {"openingJobPositions": [{
                "workspacePlace": {"place": "United States, San Jose"},
                "jobPositionCareer": {"careerType": "EXPERIENCED"},
                "jobPositionEmployment": {"employmentType": "FULL_TIME_WORKER"}
              }]}
            }
          ]}}
        ]
      }
    }
  }
}"""

GREETINGHR_HTML = f"""
<html><head>
<script id="__NEXT_DATA__" type="application/json">{GREETINGHR_NEXT_DATA}</script>
</head><body></body></html>
"""

# The listing's `openings` query (above) has no body text -- only a single
# opening's `getOpeningById` query cache entry does (verified live), so
# fetching a description means one extra per-opening request to a page
# shaped like this.
GREETINGHR_OPENING_DETAIL_NEXT_DATA = """{
  "props": {
    "pageProps": {
      "dehydratedState": {
        "queries": [
          {"queryKey": ["career", "getOpeningById", {"openingId": 444}], "state": {"data": {
            "data": {"openingsInfo": {"detail": "<p>모델 quantization 및 pruning 최적화 경험 필수</p>"}}
          }}}
        ]
      }
    }
  }
}"""

GREETINGHR_OPENING_DETAIL_HTML = f"""
<html><head>
<script id="__NEXT_DATA__" type="application/json">{GREETINGHR_OPENING_DETAIL_NEXT_DATA}</script>
</head><body></body></html>
"""


def test_company_pages_greetinghr_pattern_detected_on_custom_domain():
    def fake_get(url, *args, **kwargs):
        if url == "https://career.example-ats.com/ko":
            return _FakeResponse(GREETINGHR_HTML)
        assert url == "https://career.example-ats.com/ko/o/444", "US Sales Lead must be filtered before the detail fetch"
        return _FakeResponse(GREETINGHR_OPENING_DETAIL_HTML)

    with patch("src.sources.company_pages.requests.get", side_effect=fake_get), \
         patch("src.sources.company_pages.time.sleep"):
        postings = company_pages.fetch_postings(
            ["모빌린트"], {"모빌린트": "https://career.example-ats.com/ko"}
        )

    assert len(postings) == 1  # US Sales Lead (San Jose) filtered out
    p = postings[0]
    assert p.title == "Deep Learning Optimization Engineer"
    assert p.company == "모빌린트"
    assert p.url == "https://career.example-ats.com/ko/o/444"
    assert p.deadline is None
    assert p.region == "대한민국 서울특별시 강남구 테헤란로 1"
    assert p.employment_type == "정규직"  # FULL_TIME_WORKER -> 정규직
    assert p.career_level == "경력"  # EXPERIENCED -> 경력
    assert p.source == "관심기업"
    assert p.description == "모델 quantization 및 pruning 최적화 경험 필수"  # HTML tags stripped


def test_company_pages_greenhouse_pattern():
    with patch("src.sources.company_pages.requests.get", return_value=_FakeResponse(json_data=GREENHOUSE_JOBS_JSON)):
        postings = company_pages.fetch_postings(["토스"], {"토스": "https://boards.greenhouse.io/toss"})

    assert len(postings) == 1  # New York posting filtered out
    p = postings[0]
    assert p.title == "백엔드 엔지니어"
    assert p.company == "토스"
    assert p.url == "https://boards.greenhouse.io/toss/jobs/111"
    assert p.source == "관심기업"
    assert p.description == "모델 quantization/pruning 경험자 우대"  # HTML tags stripped


def test_company_pages_lever_pattern():
    with patch("src.sources.company_pages.requests.get", return_value=_FakeResponse(json_data=LEVER_JOBS_JSON)):
        postings = company_pages.fetch_postings(["예시회사"], {"예시회사": "https://jobs.lever.co/example"})

    assert len(postings) == 1
    p = postings[0]
    assert p.title == "프론트엔드 엔지니어"
    assert p.url == "https://jobs.lever.co/example/222"
    assert p.employment_type == "정규직"  # Full-time -> 정규직
    assert p.description == "React, TypeScript 경험자 우대"


def test_company_pages_generic_fallback_pattern():
    with patch("src.sources.company_pages.requests.get", return_value=_FakeResponse(GENERIC_HTML)):
        postings = company_pages.fetch_postings(["일반회사"], {"일반회사": "https://careers.example.com"})

    assert len(postings) == 1
    assert postings[0].title == "백엔드 엔지니어"
    assert postings[0].url == "https://careers.example.com/careers/333"


def test_company_pages_skips_unmapped_company_without_crashing():
    postings = company_pages.fetch_postings(["매핑안된회사"], {})
    assert postings == []


def test_company_pages_skips_unparseable_page_without_crashing():
    with patch("src.sources.company_pages.requests.get", return_value=_FakeResponse(EMPTY_HTML)):
        postings = company_pages.fetch_postings(["빈페이지회사"], {"빈페이지회사": "https://careers.example.com"})
    assert postings == []


def test_company_pages_one_failing_company_does_not_block_others():
    def fake_get(url, **kwargs):
        if "broken" in url:
            raise ConnectionError("simulated network failure")
        return _FakeResponse(GENERIC_HTML)

    with patch("src.sources.company_pages.requests.get", side_effect=fake_get):
        postings = company_pages.fetch_postings(
            ["망한회사", "정상회사"],
            {"망한회사": "https://broken.example.com", "정상회사": "https://careers.example.com"},
        )

    assert len(postings) == 1
    assert postings[0].company == "정상회사"
