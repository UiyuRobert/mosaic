"""End-to-end acceptance tests: NTRS search → download queue → verified PDF.

Runs against a local HTTP server on 127.0.0.1 that simulates the NASA NTRS
API (per the official OpenAPI schema) and serves real PDF bytes — no live
network is needed. ``download.allow_private_hosts`` is enabled so the SSRF
guard permits loopback URLs (production default stays off).
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mosaic.db import Cache
from mosaic.dlqueue import DownloadManager
from mosaic.models import Paper, SearchFilters
from mosaic.services import merge_papers
from mosaic.sources.ntrs import NTRSSource

# A minimal but structurally valid PDF (header + xref-ish + EOF).
_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    + b"x" * 2048
    + b"\nstartxref 0\n%%EOF\n"
)

_HTML = b"<!DOCTYPE html><html><body>Not Found</body></html>" + b"y" * 2048


def _ntrs_record(doc_id: str, filename: str) -> dict:
    return {
        "id": doc_id,
        "title": f"Turbine Blade Cooling Study {doc_id}",
        "abstract": "Film cooling investigation.",
        "authorAffiliations": [
            {"sequence": 0, "primary": True, "meta": {"author": {"name": "Doe, J."}}}
        ],
        "stiType": "TECHNICAL_MEMORANDUM",
        "distribution": "PUBLIC",
        "disseminated": "DOCUMENT_AND_METADATA",
        "downloadsAvailable": True,
        "downloads": [
            {
                "draft": False,
                "mimetype": "application/pdf",
                "name": filename,
                "type": "STI",
                "links": {
                    "pdf": f"/api/citations/{doc_id}/downloads/{filename}",
                    "original": f"/api/citations/{doc_id}/downloads/{filename}",
                },
            }
        ],
        "created": "2019-01-01",
    }


class _Handler(BaseHTTPRequestHandler):
    """Simulates NTRS API + file downloads; also an HTML 404 page."""

    def log_message(self, *args):  # silence
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/octet-stream"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/citations/search":
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            payload = {
                "stats": {"total": 2},
                "results": [
                    _ntrs_record("20190000001", "blade.pdf"),
                    _ntrs_record("20190000002", "cooling.pdf"),
                ],
            }
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        self._send(404, _HTML, "text/html")

    def do_GET(self):
        if self.path == "/api/health":
            self._send(200, b'{"status":"ok"}', "application/json")
            return
        if "/downloads/bad.pdf" in self.path:
            self._send(200, _HTML, "text/html")  # error page masquerading as PDF
            return
        if "/downloads/" in self.path:
            self._send(200, _PDF, "application/pdf")
            return
        self._send(404, _HTML, "text/html")


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def cfg(tmp_path, server):
    return {
        "db_path": str(tmp_path / "cache.db"),
        "download_dir": str(tmp_path / "papers"),
        "filename_pattern": "{year}_{title}",
        "unpaywall": {"email": ""},
        "download": {
            "backend": "native",
            "max_concurrent": 2,
            "per_host_concurrent": 1,
            "per_host_delay": 0.0,
            "max_tries": 2,
            "retry_backoff_base": 0.05,
            "verify_pdf": True,
            "skip_existing": True,
            "allow_private_hosts": True,  # required for the local test server
        },
    }


def _wait_for_jobs(cache, ids, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = [cache.get_job(i) for i in ids]
        if all(j and j["status"] in ("COMPLETED", "FAILED", "CANCELLED") for j in jobs):
            return jobs
        time.sleep(0.1)
    return [cache.get_job(i) for i in ids]


class TestEndToEnd:
    """Case A + D + E + F from the acceptance spec, offline."""

    def test_case_a_ntrs_search_to_pdf(self, cfg, server):
        """A keyword searches NTRS, a PDF downloads, verifies, and is saved."""
        src = NTRSSource(delay=0, base_url=server)
        papers = src.search("turbine blade cooling", max_results=10)
        assert len(papers) == 2
        assert all(p.ntrs_id for p in papers)
        assert all(p.pdf_url.startswith(server) for p in papers)

        cache = Cache(cfg["db_path"])
        m = DownloadManager(cache, cfg)
        ids = m.enqueue_papers(papers)
        m.start()
        try:
            pdf_map = m.wait_all(ids)
        finally:
            m.shutdown()

        assert len(pdf_map) == 2
        for uid, path in pdf_map.items():
            f = Path(path)
            assert f.exists()
            assert f.read_bytes() == _PDF
            job = next(j for j in cache.list_jobs() if j["paper_uid"] == uid)
            assert job["status"] == "COMPLETED"
            assert cache.get_download(uid)["status"] == "ok"

    def test_case_d_bad_response_fails_cleanly(self, cfg, server):
        """An HTML 'PDF' is detected and the job fails with a clear reason."""
        cache = Cache(cfg["db_path"])
        m = DownloadManager(cache, cfg)
        p = Paper(
            title="Bad PDF",
            year=2021,
            pdf_url=f"{server}/api/citations/x/downloads/bad.pdf",
            source="NASA NTRS",
            ntrs_id="999",
        )
        ids = m.enqueue_papers([p])
        m.start()
        try:
            jobs = _wait_for_jobs(cache, ids)
        finally:
            m.shutdown()
        assert jobs[0]["status"] == "FAILED"
        assert "html_response" in jobs[0]["last_error"]
        # Nothing left behind at the final path
        assert not Path(jobs[0]["destination"]).exists()

    def test_case_e_restart_recovers_queued_job(self, cfg, server):
        """A restart re-queues interrupted jobs and completes them."""
        src = NTRSSource(delay=0, base_url=server)
        papers = src.search("x", max_results=1)

        cache1 = Cache(cfg["db_path"])
        m1 = DownloadManager(cache1, cfg)
        ids = m1.enqueue_papers(papers)
        cache1.update_job(ids[0], status="DOWNLOADING")  # simulate crash mid-flight
        m1.shutdown()

        # New process/instance: reconcile + finish
        cache2 = Cache(cfg["db_path"])
        m2 = DownloadManager(cache2, cfg)
        m2.start()
        try:
            pdf_map = m2.wait_all(ids, poll=0.1)
        finally:
            m2.shutdown()
        assert papers[0].uid in pdf_map
        assert Path(pdf_map[papers[0].uid]).read_bytes() == _PDF

    def test_case_f_repeat_search_skips_downloaded(self, cfg, server):
        """Second run over the same papers creates no new jobs."""
        src = NTRSSource(delay=0, base_url=server)
        papers = src.search("x", max_results=1)

        cache = Cache(cfg["db_path"])
        m = DownloadManager(cache, cfg)
        ids = m.enqueue_papers(papers)
        m.start()
        try:
            m.wait_all(ids)
        finally:
            m.shutdown()

        m2 = DownloadManager(Cache(cfg["db_path"]), cfg)
        assert m2.enqueue_papers(papers) == []  # already downloaded → skipped


class TestAliasDedup:
    """Case B + cross-source merge: NTRS record later carrying a DOI."""

    def test_same_doi_merges(self):
        seen: dict[str, Paper] = {}
        idx: dict[str, str] = {}
        ntrs_only = Paper(
            title="Same Work",
            source="NASA NTRS",
            ntrs_id="20200000001",
            is_open_access=True,
        )
        merge_papers(seen, ntrs_only, idx)
        assert "ntrs:20200000001" in seen

        # The same paper surfaces via OpenAlex with its DOI.
        with_doi = Paper(
            title="Same Work (extended)",
            source="OpenAlex",
            ntrs_id="20200000001",
            doi="10.1234/same",
            citation_count=42,
        )
        merge_papers(seen, with_doi, idx)
        assert len(seen) == 1
        merged = next(iter(seen.values()))
        assert merged.doi == "10.1234/same"
        assert merged.ntrs_id == "20200000001"
        assert merged.uid == "doi:10.1234/same"
        assert merged.citation_count == 42

    def test_db_alias_resolves_download(self, tmp_path):
        """A download recorded under ntrs:x resolves for the doi: record."""
        cache = Cache(str(tmp_path / "c.db"))
        ntrs_paper = Paper(title="W", ntrs_id="111", source="NASA NTRS")
        cache.save(ntrs_paper)
        cache.set_download("ntrs:111", "/tmp/x.pdf", "ok")

        doi_paper = Paper(title="W", ntrs_id="111", doi="10.5/w", source="OpenAlex")
        cache.save(doi_paper)
        # The DOI record merged with the existing NTRS one → one canonical row.
        assert cache.get_by_uid("doi:10.5/w") is not None
        assert cache.get_by_uid("ntrs:111") is not None
        # Download record reachable via either alias.
        assert cache.get_download("doi:10.5/w")["status"] == "ok"
        assert cache.get_download("ntrs:111")["status"] == "ok"

    def test_raw_query_passes_through(self, server):
        src = NTRSSource(delay=0, base_url=server)
        papers = src.search("ignored", filters=SearchFilters(raw_query="title:abcd"))
        assert len(papers) == 2  # local server ignores body, returns both
