"""Tests for Unpaywall multi-candidate resolution."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mosaic.sources import unpaywall


def _resp(status: int = 200, payload: dict | None = None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload or {}
    return r


class TestResolveCandidates:
    def test_best_location_pdf_first(self):
        payload = {
            "is_oa": True,
            "best_oa_location": {
                "url_for_pdf": "https://repo.org/best.pdf",
                "url_for_landing_page": "https://repo.org/landing",
                "version": "publishedVersion",
                "license": "cc-by",
            },
            "oa_locations": [
                {
                    "url_for_pdf": "https://repo.org/best.pdf",
                    "version": "publishedVersion",
                },
                {
                    "url_for_pdf": "https://archive.org/alt.pdf",
                    "url": "https://archive.org/page",
                    "version": "acceptedVersion",
                    "license": "cc-by-nc",
                },
            ],
        }
        with patch("httpx.get", return_value=_resp(200, payload)):
            cands = unpaywall.resolve_candidates("10.1/x", "me@uni.edu")
        assert len(cands) == 2
        assert cands[0].download_url == "https://repo.org/best.pdf"
        assert cands[0].version == "publishedVersion"
        assert cands[0].license == "cc-by"
        assert cands[0].landing_url == "https://repo.org/landing"
        assert cands[1].download_url == "https://archive.org/alt.pdf"

    def test_url_only_candidate_ranked_lower(self):
        payload = {
            "is_oa": True,
            "best_oa_location": {
                "url": "https://publisher.com/article",  # HTML landing page
                "version": "publishedVersion",
            },
            "oa_locations": [],
        }
        with patch("httpx.get", return_value=_resp(200, payload)):
            cands = unpaywall.resolve_candidates("10.1/x", "me@uni.edu")
        assert len(cands) == 1
        assert cands[0].download_url == "https://publisher.com/article"
        assert cands[0].confidence < 0.7  # landing pages rank below real PDFs

    def test_not_oa_returns_empty(self):
        with patch("httpx.get", return_value=_resp(200, {"is_oa": False})):
            assert unpaywall.resolve_candidates("10.1/x", "me@uni.edu") == []

    def test_no_email_or_doi_returns_empty(self):
        assert unpaywall.resolve_candidates("10.1/x", "") == []
        assert unpaywall.resolve_candidates("", "me@uni.edu") == []

    def test_http_error_returns_empty(self):
        with patch("httpx.get", return_value=_resp(404)):
            assert unpaywall.resolve_candidates("10.1/x", "me@uni.edu") == []

    def test_resolve_returns_first_candidate_url(self):
        payload = {
            "is_oa": True,
            "best_oa_location": {"url_for_pdf": "https://repo.org/x.pdf"},
            "oa_locations": [{"url_for_pdf": "https://repo.org/x.pdf"}],
        }
        with patch("httpx.get", return_value=_resp(200, payload)):
            assert unpaywall.resolve("10.1/x", "me@uni.edu") == "https://repo.org/x.pdf"
