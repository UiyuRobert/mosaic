"""Tests for the PDF downloader."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

from mosaic.downloader import download
from mosaic.models import Paper, PDFCandidate

# Minimal byte content that passes the quick PDF sanity check
# (%PDF- magic + enough size). Full structural verification is tested
# separately in test_pdfcheck.py.
_MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\nstartxref 0\n%%EOF\n"
)


def _paper(doi="10.1/test", pdf_url=None):
    return Paper(
        title="Test Paper",
        authors=["Smith"],
        year=2020,
        doi=doi,
        pdf_url=pdf_url,
        source="test",
    )


def _candidate(url: str) -> PDFCandidate:
    return PDFCandidate(paper_uid="x", download_url=url, source="unpaywall")


async def _fake_browser_download(url, dest, session):
    Path(dest).write_bytes(_MINIMAL_PDF)
    return True


class TestDownload:
    def test_skips_if_already_cached_and_file_exists(self, tmp_path, tmp_cache):
        dest = tmp_path / "paper.pdf"
        dest.write_bytes(_MINIMAL_PDF)
        p = _paper()
        tmp_cache.set_download(p.uid, str(dest), "ok")
        with patch("mosaic.downloader._fetch") as mock_fetch:
            result = download(p, str(tmp_path), tmp_cache)
        mock_fetch.assert_not_called()
        assert result == str(dest)

    def test_redownloads_when_cached_file_is_not_pdf(self, tmp_path, tmp_cache):
        """status='ok' is not trusted blindly — an HTML stub is re-fetched."""
        dest = tmp_path / "paper.pdf"
        dest.write_bytes(b"<html>error page</html>")
        p = _paper(pdf_url="https://example.com/paper.pdf")
        tmp_cache.set_download(p.uid, str(dest), "ok")
        with patch("mosaic.downloader._fetch") as mock_fetch:
            download(p, str(tmp_path), tmp_cache)
        mock_fetch.assert_called_once()

    def test_downloads_from_pdf_url(self, tmp_path, tmp_cache):
        p = _paper(pdf_url="https://example.com/paper.pdf")
        with patch("mosaic.downloader._fetch") as mock_fetch:
            result = download(p, str(tmp_path), tmp_cache)
        mock_fetch.assert_called_once()
        assert "https://example.com/paper.pdf" in mock_fetch.call_args.args
        assert result is not None

    def test_falls_back_to_unpaywall_when_no_pdf_url(self, tmp_path, tmp_cache):
        p = _paper(doi="10.1/x", pdf_url=None)
        with (
            patch(
                "mosaic.sources.unpaywall.resolve_candidates",
                return_value=[_candidate("https://repo.org/paper.pdf")],
            ),
            patch("mosaic.downloader._fetch"),
        ):
            result = download(p, str(tmp_path), tmp_cache, unpaywall_email="me@uni.edu")
        assert result is not None

    def test_returns_none_when_no_pdf_and_no_doi(self, tmp_path, tmp_cache):
        p = Paper(title="T", authors=[], doi=None, pdf_url=None)
        result = download(p, str(tmp_path), tmp_cache, unpaywall_email="me@uni.edu")
        assert result is None

    def test_returns_none_and_records_error_on_fetch_failure(self, tmp_path, tmp_cache):
        p = _paper(pdf_url="https://example.com/paper.pdf")
        with patch("mosaic.downloader._fetch", side_effect=Exception("connection refused")):
            result = download(p, str(tmp_path), tmp_cache)
        assert result is None
        rec = tmp_cache.get_download(p.uid)
        assert rec is not None
        assert "error" in rec["status"]

    def test_cache_recorded_as_ok_after_success(self, tmp_path, tmp_cache):
        p = _paper(pdf_url="https://example.com/paper.pdf")
        with patch("mosaic.downloader._fetch"):
            download(p, str(tmp_path), tmp_cache)
        rec = tmp_cache.get_download(p.uid)
        assert rec["status"] == "ok"

    def test_falls_back_to_browser_when_steps_1_and_2_fail(self, tmp_path, tmp_cache):
        p = _paper(doi="10.1/x", pdf_url=None)
        p.url = "https://sciencedirect.com/article/pii/S123"
        with (
            patch("mosaic.sources.unpaywall.resolve_candidates", return_value=[]),
            patch("mosaic.auth.find_session_for_url", return_value="elsevier"),
            patch(
                "mosaic.auth.browser_download", new=AsyncMock(side_effect=_fake_browser_download)
            ),
        ):
            result = download(p, str(tmp_path), tmp_cache, unpaywall_email="me@uni.edu")
        assert result is not None

    def test_skips_browser_when_no_session_matches(self, tmp_path, tmp_cache):
        p = _paper(doi="10.1/x", pdf_url=None)
        p.url = "https://nature.com/articles/123"
        with (
            patch("mosaic.sources.unpaywall.resolve_candidates", return_value=[]),
            patch("mosaic.auth.find_session_for_url", return_value=None),
            patch("mosaic.auth.list_sessions", return_value=[]),
            patch("mosaic.auth.browser_download") as mock_bd,
        ):
            download(p, str(tmp_path), tmp_cache, unpaywall_email="me@uni.edu")
        mock_bd.assert_not_called()

    def test_browser_result_cached_as_ok(self, tmp_path, tmp_cache):
        p = _paper(doi="10.1/x", pdf_url=None)
        p.url = "https://sciencedirect.com/article/pii/S123"
        with (
            patch("mosaic.sources.unpaywall.resolve_candidates", return_value=[]),
            patch("mosaic.auth.find_session_for_url", return_value="elsevier"),
            patch("mosaic.auth.browser_download", AsyncMock(side_effect=_fake_browser_download)),
        ):
            download(p, str(tmp_path), tmp_cache, unpaywall_email="me@uni.edu")
        rec = tmp_cache.get_download(p.uid)
        assert rec["status"] == "ok"

    def test_browser_html_result_not_marked_ok(self, tmp_path, tmp_cache):
        """A browser 'success' that produced HTML is not recorded as ok."""

        async def _html(url, dest, session):
            Path(dest).write_bytes(b"<html>login wall</html>")
            return True

        p = _paper(doi="10.1/x", pdf_url=None)
        p.url = "https://sciencedirect.com/article/pii/S123"
        with (
            patch("mosaic.sources.unpaywall.resolve_candidates", return_value=[]),
            patch("mosaic.auth.find_session_for_url", return_value="elsevier"),
            patch("mosaic.auth.browser_download", AsyncMock(side_effect=_html)),
        ):
            result = download(p, str(tmp_path), tmp_cache, unpaywall_email="me@uni.edu")
        assert result is None
        rec = tmp_cache.get_download(p.uid)
        assert "error" in rec["status"]

    def test_all_steps_fail_returns_none(self, tmp_path, tmp_cache):
        p = _paper(doi="10.1/x", pdf_url=None)
        with (
            patch("mosaic.sources.unpaywall.resolve_candidates", return_value=[]),
            patch("mosaic.auth.find_session_for_url", return_value=None),
            patch("mosaic.auth.list_sessions", return_value=[]),
            patch("mosaic.downloader._resolve_redirect", side_effect=lambda u: u),
        ):
            result = download(p, str(tmp_path), tmp_cache, unpaywall_email="me@uni.edu")
        assert result is None
