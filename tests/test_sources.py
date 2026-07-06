from datetime import date, timedelta
from unittest.mock import patch

from src.sources import jobkorea, saramin, wanted, zighang

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
