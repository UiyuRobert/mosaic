"""Tests for the NASA NTRS source.

All responses are mocked from the official NASA STI OpenAPI documentation
(2022-11-22) — no live network access is required. Live API access was
unreachable (HTTP 403) from the development environment, so fixtures carry
the authoritative field names: authorAffiliations, publications,
sourceIdentifiers, downloads[].links.pdf, disseminated, distribution.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mosaic.errors import SourceError
from mosaic.sources.ntrs import NTRSSource


def _resp(status: int = 200, payload: dict | None = None, headers: dict | None = None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.text = ""
    r.headers = headers or {}
    return r


def _record(**overrides) -> dict:
    """A realistic NTRS citation record (Technical Memorandum with PDF)."""
    rec = {
        "id": "20200000325",
        "title": "Atmospheric Flight Mechanics of a Mars Helicopter",
        "abstract": "A study of rotorcraft flight dynamics in the Martian atmosphere.",
        "authorAffiliations": [
            {
                "sequence": 0,
                "primary": True,
                "meta": {
                    "author": {"name": "Bryson, S."},
                    "organization": {"name": "NASA Ames Research Center"},
                },
            },
            {
                "sequence": 1,
                "meta": {
                    "author": {"name": "Grip, H. F."},
                    "organization": {"name": "Jet Propulsion Laboratory"},
                },
            },
        ],
        "center": {"code": "ARC", "name": "Ames Research Center"},
        "stiType": "TECHNICAL_MEMORANDUM",
        "subjectCategories": ["Aircraft Design, Testing and Performance"],
        "keywords": ["rotorcraft", "Mars"],
        "publications": [
            {
                "publicationName": "NASA/TM-2020-5002365",
                "publisher": "NASA",
                "publicationDate": "2020-05-01",
                "doi": "",
            }
        ],
        "otherReportNumbers": ["NASA/TM-2020-5002365"],
        "sourceIdentifiers": [
            {"type": "DOI", "number": "10.1109/xyz.2020.1"},
            {"type": "HANDLE", "number": "2020/12345"},
        ],
        "downloads": [
            {
                "draft": False,
                "mimetype": "text/plain",
                "name": "20200000325.txt",
                "type": "STI",
                "links": {
                    "fulltext": "/api/citations/20200000325/downloads/20200000325.txt",
                },
            },
            {
                "draft": False,
                "mimetype": "application/pdf",
                "name": "20200000325.pdf",
                "type": "STI",
                "links": {
                    "original": "/api/citations/20200000325/downloads/20200000325.pdf",
                    "pdf": "/api/citations/20200000325/downloads/20200000325.pdf",
                    "fulltext": "/api/citations/20200000325/downloads/20200000325.txt",
                },
            },
        ],
        "downloadsAvailable": True,
        "disseminated": "DOCUMENT_AND_METADATA",
        "distribution": "PUBLIC",
        "distributionDate": "2020-05-15",
        "created": "2020-01-10",
        "modified": "2020-02-01",
        "status": "CURATED",
        "onlyAbstract": False,
        "related": [],
    }
    rec.update(overrides)
    return rec


def _search_payload(records: list[dict], total: int | None = None) -> dict:
    return {
        "stats": {
            "took": 5,
            "total": total if total is not None else len(records),
            "estimate": False,
        },
        "results": records,
    }


@pytest.fixture
def ntrs() -> NTRSSource:
    return NTRSSource(delay=0)


class TestSearch:
    def test_basic_search_maps_fields(self, ntrs):
        with patch("httpx.request", return_value=_resp(200, _search_payload([_record()]))) as m:
            papers = ntrs.search("mars helicopter", max_results=10)
        assert len(papers) == 1
        p = papers[0]
        assert p.title.startswith("Atmospheric Flight Mechanics")
        assert p.authors == ["Bryson, S.", "Grip, H. F."]
        assert p.year == 2020
        assert p.doi == "10.1109/xyz.2020.1"
        assert p.ntrs_id == "20200000325"
        assert p.report_number == "NASA/TM-2020-5002365"
        assert p.pub_type == "Technical Memorandum"
        assert p.is_open_access is True
        assert p.url == "https://ntrs.nasa.gov/citations/20200000325"
        # request shape: POST to /api/citations/search with q param
        req = m.call_args
        assert req.args[0] == "POST"
        assert req.args[1].endswith("/api/citations/search")
        assert req.kwargs["json"]["q"] == "mars helicopter"

    def test_pdf_attachment_resolves_absolute_url(self, ntrs):
        with patch("httpx.request", return_value=_resp(200, _search_payload([_record()]))):
            papers = ntrs.search("x", max_results=5)
        p = papers[0]
        assert (
            p.pdf_url == "https://ntrs.nasa.gov/api/citations/20200000325/downloads/20200000325.pdf"
        )
        assert len(p.pdf_candidates) == 1
        cand = p.pdf_candidates[0]
        assert cand["source"] == "ntrs"
        assert cand["landing_url"].endswith("/citations/20200000325")

    def test_uid_prefers_doi_then_ntrs(self, ntrs):
        with patch("httpx.request", return_value=_resp(200, _search_payload([_record()]))):
            papers = ntrs.search("x")
        assert papers[0].uid == "doi:10.1109/xyz.2020.1"
        assert "ntrs:20200000325" in papers[0].identifiers

    def test_no_doi_falls_back_to_ntrs_uid(self, ntrs):
        rec = _record(publications=[], sourceIdentifiers=[])
        with patch("httpx.request", return_value=_resp(200, _search_payload([rec]))):
            papers = ntrs.search("x")
        assert papers[0].uid == "ntrs:20200000325"

    def test_metadata_only_record_kept_without_pdf(self, ntrs):
        rec = _record(
            downloads=[],
            downloadsAvailable=False,
            disseminated="METADATA_ONLY",
            publications=[],
            sourceIdentifiers=[],
        )
        with patch("httpx.request", return_value=_resp(200, _search_payload([rec]))):
            papers = ntrs.search("x")
        assert len(papers) == 1
        assert papers[0].pdf_url is None
        assert papers[0].is_open_access is False
        assert papers[0].uid == "ntrs:20200000325"

    def test_non_pdf_attachments_do_not_become_pdf_url(self, ntrs):
        rec = _record(
            downloads=[
                {
                    "draft": False,
                    "mimetype": "application/zip",
                    "name": "data.zip",
                    "type": "STI",
                    "links": {"original": "/api/citations/20200000325/downloads/data.zip"},
                }
            ],
        )
        with patch("httpx.request", return_value=_resp(200, _search_payload([rec]))):
            papers = ntrs.search("x")
        assert papers[0].pdf_url is None

    def test_original_link_used_when_mimetype_is_pdf(self, ntrs):
        rec = _record(
            downloads=[
                {
                    "draft": False,
                    "mimetype": "application/pdf",
                    "name": "doc.pdf",
                    "type": "STI",
                    "links": {"original": "/api/citations/20200000325/downloads/doc.pdf"},
                }
            ],
        )
        with patch("httpx.request", return_value=_resp(200, _search_payload([rec]))):
            papers = ntrs.search("x")
        assert papers[0].pdf_url.endswith("/downloads/doc.pdf")

    def test_draft_attachments_ignored(self, ntrs):
        rec = _record(
            downloads=[
                {
                    "draft": True,
                    "mimetype": "application/pdf",
                    "name": "draft.pdf",
                    "type": "STI",
                    "links": {"pdf": "/api/citations/x/downloads/draft.pdf"},
                }
            ],
            downloadsAvailable=False,
        )
        with patch("httpx.request", return_value=_resp(200, _search_payload([rec]))):
            papers = ntrs.search("x")
        assert papers[0].pdf_url is None

    def test_non_public_record_dropped(self, ntrs):
        rec = _record(distribution="DO_NOT_DISTRIBUTE")
        with patch("httpx.request", return_value=_resp(200, _search_payload([rec]))):
            papers = ntrs.search("x")
        assert papers == []

    def test_title_and_abstract_field_scoping(self, ntrs):
        from mosaic.models import SearchFilters

        with patch("httpx.request", return_value=_resp(200, _search_payload([]))) as m:
            ntrs.search("film cooling", filters=SearchFilters(field="title"))
        assert m.call_args.kwargs["json"]["title"] == "film cooling"
        assert "q" not in m.call_args.kwargs["json"]

        with patch("httpx.request", return_value=_resp(200, _search_payload([]))) as m:
            ntrs.search("film cooling", filters=SearchFilters(field="abstract"))
        assert m.call_args.kwargs["json"]["abstract"] == "film cooling"

    def test_year_range_mapped_to_published(self, ntrs):
        from mosaic.models import SearchFilters

        with patch("httpx.request", return_value=_resp(200, _search_payload([]))) as m:
            ntrs.search("x", filters=SearchFilters(year_from=2018, year_to=2021))
        pub = m.call_args.kwargs["json"]["published"]
        assert pub == {"gte": "2018", "lte": "2021"}

    def test_author_filter_passed_to_api(self, ntrs):
        from mosaic.models import SearchFilters

        with patch("httpx.request", return_value=_resp(200, _search_payload([]))) as m:
            ntrs.search("x", filters=SearchFilters(authors=["Bryson"]))
        assert m.call_args.kwargs["json"]["author"] == "Bryson"

    def test_pagination_fetches_until_max(self, ntrs):
        pages = [_search_payload([_record(id=f"r{i}") for i in range(100)])]
        pages.append(_search_payload([_record(id="r100")]))
        calls = iter(pages)

        with patch("httpx.request", side_effect=lambda *a, **k: _resp(200, next(calls))):
            papers = ntrs.search("x", max_results=105)
        assert len(papers) == 101
        seen = {p.ntrs_id for p in papers}
        assert len(seen) == 101

    def test_missing_fields_do_not_crash(self, ntrs):
        rec = {"id": "42", "title": "Minimal record", "distribution": "PUBLIC"}
        with patch("httpx.request", return_value=_resp(200, _search_payload([rec]))):
            papers = ntrs.search("x")
        assert len(papers) == 1
        assert papers[0].year is None
        assert papers[0].authors == []

    def test_429_retries_then_raises(self, ntrs):
        rate_limited = _resp(429, headers={"Retry-After": "0"})
        with (
            patch("httpx.request", return_value=rate_limited),
            patch("time.sleep"),
            pytest.raises(SourceError, match="rate limit"),
        ):
            ntrs.search("x")

    def test_http_error_raises_source_error(self, ntrs):
        with (
            patch("httpx.request", return_value=_resp(503)),
            patch("time.sleep"),
            pytest.raises(SourceError, match=r"server error|attempts"),
        ):
            ntrs.search("x")

    def test_4xx_raises_immediately(self, ntrs):
        with (
            patch("httpx.request", return_value=_resp(400)),
            pytest.raises(SourceError, match="400"),
        ):
            ntrs.search("x")

    def test_source_available_without_key(self, ntrs):
        assert ntrs.available() is True
        assert ntrs.name == "NASA NTRS"


class TestLazyAttachments:
    """Records that arrive with ``downloadsAvailable`` but no embedded
    ``downloads`` list must resolve attachments via /citations/{id}/downloads
    instead of being misjudged as undownloadable."""

    def test_downloads_available_without_list_marks_oa_only(self, ntrs):
        rec = _record(downloads=[], downloadsAvailable=True)
        with patch("httpx.request", return_value=_resp(200, _search_payload([rec]))):
            papers = ntrs.search("x")
        p = papers[0]
        assert p.is_open_access is True
        assert p.pdf_url is None
        assert p.pdf_candidates == []

    def test_pdf_candidates_for_fetches_attachments(self, ntrs):
        downloads = [
            {
                "draft": False,
                "mimetype": "application/pdf",
                "name": "doc.pdf",
                "type": "STI",
                "links": {"pdf": "/api/citations/20200000325/downloads/doc.pdf"},
            }
        ]
        with patch("httpx.request", return_value=_resp(200, {"downloads": downloads})) as m:
            cands = ntrs.pdf_candidates_for("20200000325")
        assert m.call_args.args[0] == "GET"
        assert m.call_args.args[1].endswith("/api/citations/20200000325/downloads")
        assert len(cands) == 1
        assert cands[0]["download_url"].endswith("/downloads/doc.pdf")
        assert cands[0]["source"] == "ntrs"

    def test_downloader_uses_lazy_ntrs_candidates(self):
        """_candidates_for must consult the downloads endpoint for an
        NTRS paper that carried no attachment list."""
        from mosaic.downloader import _candidates_for
        from mosaic.models import Paper

        p = Paper(
            title="TM report",
            ntrs_id="20200000325",
            is_open_access=True,
            source="NASA NTRS",
        )
        fake = [
            {
                "paper_uid": "ntrs:20200000325",
                "download_url": "https://ntrs.nasa.gov/api/citations/20200000325/downloads/doc.pdf",
                "landing_url": "https://ntrs.nasa.gov/citations/20200000325",
                "source": "ntrs",
                "version": "",
                "license": "",
                "is_open_access": True,
                "confidence": 0.95,
                "discovered_at": "",
            }
        ]
        with patch.object(NTRSSource, "pdf_candidates_for", return_value=fake) as m:
            cands = _candidates_for(p, "")
        m.assert_called_once_with("20200000325")
        assert [c.download_url for c in cands] == [fake[0]["download_url"]]
        assert cands[0].paper_uid == p.uid

    def test_downloader_skips_lazy_fetch_when_candidates_exist(self):
        from mosaic.downloader import _candidates_for
        from mosaic.models import Paper

        p = Paper(
            title="TM report",
            ntrs_id="20200000325",
            pdf_url="https://example.org/x.pdf",
            source="NASA NTRS",
        )
        with patch.object(NTRSSource, "pdf_candidates_for") as m:
            _candidates_for(p, "")
        m.assert_not_called()

    def test_lazy_fetch_failure_is_nonfatal(self):
        from mosaic.downloader import _candidates_for
        from mosaic.models import Paper

        p = Paper(title="TM report", ntrs_id="20200000325", source="NASA NTRS")
        with patch.object(NTRSSource, "pdf_candidates_for", side_effect=SourceError("403")):
            assert _candidates_for(p, "") == []
