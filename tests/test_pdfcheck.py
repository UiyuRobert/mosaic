"""Tests for PDF verification and filename/URL safety."""

from __future__ import annotations

import pytest

from mosaic.pdfcheck import (
    PdfVerificationError,
    dedupe_path,
    ensure_within,
    is_safe_url,
    looks_like_html,
    sanitize_filename,
    sha256_file,
    verify_pdf,
)


def _minimal_pdf() -> bytes:
    body = b"%PDF-1.4\n" + b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n" + b"x" * 1024
    return body + b"\n%%EOF\n"


class TestVerifyPdf:
    def test_valid_pdf_passes(self, tmp_path):
        f = tmp_path / "ok.pdf"
        f.write_bytes(_minimal_pdf())
        result = verify_pdf(f, full_check=False)
        assert result["ok"] is True
        assert result["size"] > 1024
        assert len(result["sha256"]) == 64

    def test_html_page_rejected(self, tmp_path):
        f = tmp_path / "bad.pdf"
        f.write_bytes(b"<!DOCTYPE html><html><body>404</body></html>" + b"x" * 2048)
        with pytest.raises(PdfVerificationError) as e:
            verify_pdf(f)
        assert e.value.reason == "html_response"

    def test_missing_magic_rejected(self, tmp_path):
        f = tmp_path / "bad.pdf"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 2048)
        with pytest.raises(PdfVerificationError) as e:
            verify_pdf(f)
        assert e.value.reason == "not_pdf"

    def test_empty_rejected(self, tmp_path):
        f = tmp_path / "empty.pdf"
        f.write_bytes(b"")
        with pytest.raises(PdfVerificationError) as e:
            verify_pdf(f)
        assert e.value.reason == "empty"

    def test_too_small_rejected(self, tmp_path):
        f = tmp_path / "tiny.pdf"
        f.write_bytes(b"%PDF-1.4")
        with pytest.raises(PdfVerificationError) as e:
            verify_pdf(f)
        assert e.value.reason == "too_small"

    def test_missing_eof_rejected(self, tmp_path):
        f = tmp_path / "truncated.pdf"
        f.write_bytes(b"%PDF-1.4\n" + b"x" * 4096)  # no %%EOF marker
        with pytest.raises(PdfVerificationError) as e:
            verify_pdf(f)
        assert e.value.reason == "missing_eof"

    def test_sha256_stable(self, tmp_path):
        f = tmp_path / "a.pdf"
        f.write_bytes(_minimal_pdf())
        assert sha256_file(f) == sha256_file(f)


class TestHtmlDetection:
    def test_html_samples(self):
        assert looks_like_html(b"<!DOCTYPE html><html>")
        assert looks_like_html(b"  <html><head>")
        assert looks_like_html(b"<?xml version='1.0'?>")
        assert not looks_like_html(b"%PDF-1.4 binary stuff")


class TestFilenames:
    def test_strips_windows_illegal_chars(self):
        name = sanitize_filename('a<b>c:d"e/f\\g|h?i*j.pdf')
        assert "/" not in name and "?" not in name and ":" not in name
        assert name.endswith(".pdf")

    def test_reserved_names_escaped(self):
        assert sanitize_filename("CON.pdf").startswith("_")
        assert sanitize_filename("nul").startswith("_")

    def test_length_capped(self):
        name = sanitize_filename("x" * 500 + ".pdf", max_len=100)
        assert len(name) <= 100
        assert name.endswith(".pdf")

    def test_empty_becomes_untitled(self):
        assert sanitize_filename("///") == "untitled"

    def test_dedupe_path(self, tmp_path):
        f = tmp_path / "paper.pdf"
        f.write_bytes(b"x")
        new = dedupe_path(f)
        assert new.name == "paper__2.pdf"
        new.write_bytes(b"x")
        assert dedupe_path(f).name == "paper__3.pdf"

    def test_ensure_within_blocks_traversal(self, tmp_path):
        base = tmp_path / "dl"
        base.mkdir()
        with pytest.raises(PdfVerificationError) as e:
            ensure_within(base, base / ".." / "evil.pdf")
        assert e.value.reason == "path_traversal"
        assert ensure_within(base, base / "ok.pdf").parent == base.resolve()


class TestUrlSafety:
    def test_http_https_allowed(self):
        assert is_safe_url("https://ntrs.nasa.gov/api/citations/1/downloads/x.pdf")[0]
        assert is_safe_url("http://arxiv.org/pdf/1234.pdf")[0]

    def test_bad_scheme_rejected(self):
        ok, reason = is_safe_url("file:///etc/passwd")
        assert not ok and reason == "bad_scheme"
        ok, _ = is_safe_url("ftp://x.com/f.pdf")
        assert not ok

    def test_loopback_blocked(self):
        ok, reason = is_safe_url("http://127.0.0.1:6800/x")
        assert not ok and reason in ("blocked_host", "blocked_ip")
        ok, _ = is_safe_url("http://localhost/x")
        assert not ok

    def test_private_ip_blocked(self):
        ok, _ = is_safe_url("http://192.168.1.1/f.pdf")
        assert not ok
        ok, _ = is_safe_url("http://10.0.0.5/f.pdf")
        assert not ok

    def test_metadata_endpoint_blocked(self):
        ok, _ = is_safe_url("http://169.254.169.254/latest/meta-data")
        assert not ok

    def test_allow_private_override(self):
        ok, _ = is_safe_url("http://127.0.0.1:6800/jsonrpc", allow_private=True)
        assert ok
