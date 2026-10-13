"""Tests for the persistent download queue (DownloadManager)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from mosaic.db import Cache
from mosaic.dlqueue import DownloadManager
from mosaic.errors import DownloadError
from mosaic.models import Paper, PDFCandidate


@pytest.fixture
def cfg(tmp_path):
    return {
        "db_path": str(tmp_path / "cache.db"),
        "download_dir": str(tmp_path / "papers"),
        "filename_pattern": "{year}_{title}",
        "unpaywall": {"email": ""},
        "download": {
            "backend": "native",
            "max_concurrent": 4,
            "per_host_concurrent": 5,
            "per_host_delay": 0.0,
            "max_tries": 3,
            "verify_pdf": False,  # fixtures aren't structural PDFs
            "skip_existing": True,
            "retry_backoff_base": 0.05,  # fast retries in tests
        },
    }


@pytest.fixture
def cache(cfg):
    return Cache(cfg["db_path"])


@pytest.fixture
def manager(cfg, cache):
    m = DownloadManager(cache, cfg)
    yield m
    m.shutdown()


def _paper(uid_suffix="1", pdf_url="https://example.com/a.pdf", **kw):
    defaults = {
        "title": f"Paper {uid_suffix}",
        "authors": ["Author"],
        "year": 2021,
        "pdf_url": pdf_url,
        "source": "test",
    }
    defaults.update(kw)
    return Paper(**defaults)


def _wait_status(cache, job_id, want, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = cache.get_job(job_id)
        if job and job["status"] in (want if isinstance(want, (set, frozenset)) else {want}):
            return job
        time.sleep(0.05)
    return cache.get_job(job_id)


class TestEnqueue:
    def test_enqueue_creates_queued_job(self, manager, cache):
        p = _paper()
        ids = manager.enqueue_papers([p])
        assert len(ids) == 1
        job = cache.get_job(ids[0])
        assert job["status"] == "QUEUED"
        assert job["paper_uid"] == p.uid
        assert job["backend"] == "native"
        assert job["url"] == "https://example.com/a.pdf"
        assert job["host"] == "example.com"
        assert job["destination"].endswith(".pdf")
        candidates = json.loads(job["candidates_json"])
        assert candidates[0]["download_url"] == "https://example.com/a.pdf"

    def test_enqueue_persists_paper(self, manager, cache):
        p = _paper(ntrs_id="20200000001")
        manager.enqueue_papers([p])
        assert cache.get_by_uid("ntrs:20200000001") is not None

    def test_skip_existing_verified_download(self, manager, cache, cfg):
        p = _paper()
        existing = Path(cfg["download_dir"]) / "exists.pdf"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"%PDF-1.4\n" + b"x" * 100 + b"%%EOF")
        cache.save(p)
        cache.set_download(p.uid, str(existing), "ok")
        assert manager.enqueue_papers([p]) == []

    def test_no_skip_when_file_not_pdf(self, manager, cache, cfg):
        p = _paper()
        existing = Path(cfg["download_dir"]) / "bad.pdf"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"<html>oops</html>")
        cache.save(p)
        cache.set_download(p.uid, str(existing), "ok")
        ids = manager.enqueue_papers([p])
        assert len(ids) == 1


class TestNativeExecution:
    def test_successful_download_completes(self, manager, cache, cfg):
        p = _paper()
        ids = manager.enqueue_papers([p])
        fake_pdf = b"%PDF-1.4\n" + b"x" * 2000 + b"%%EOF"

        def fake_fetch(url, dest, **kw):
            Path(dest).write_bytes(fake_pdf)
            return url

        manager.start()
        with patch("mosaic.dlqueue._fetch", side_effect=fake_fetch):
            result = manager.wait_all(ids)
        assert result == {p.uid: str(Path(job_dest(cache, ids[0])))}
        job = cache.get_job(ids[0])
        assert job["status"] == "COMPLETED"
        assert Path(job["destination"]).read_bytes() == fake_pdf
        rec = cache.get_download(p.uid)
        assert rec["status"] == "ok"

    def test_failure_retries_then_fails(self, manager, cache):
        p = _paper()
        ids = manager.enqueue_papers([p])
        manager.start()
        calls = []

        def failing(url, dest, **kw):
            calls.append(url)
            raise DownloadError("not_found", "HTTP 404")

        with patch("mosaic.dlqueue._fetch", side_effect=failing):
            manager.wait_all(ids)
        job = cache.get_job(ids[0])
        assert job["status"] == "FAILED"
        assert "not_found" in job["last_error"]
        # 404 is permanent — no wasteful retries
        assert len(calls) == 1

    def test_transient_error_uses_retry_wait(self, manager, cache):
        p = _paper()
        ids = manager.enqueue_papers([p])
        manager.start()
        attempts = []

        def flaky(url, dest, **kw):
            attempts.append(1)
            if len(attempts) < 3:
                raise DownloadError("timeout", "timed out")
            Path(dest).write_bytes(b"%PDF-1.4\nx" + b"y" * 2000 + b"%%EOF")
            return url

        with patch("mosaic.dlqueue._fetch", side_effect=flaky):
            result = manager.wait_all(ids, poll=0.05)
        assert p.uid in result
        assert len(attempts) >= 3

    def test_retry_cap_respected(self, manager, cache):
        p = _paper()
        ids = manager.enqueue_papers([p])
        manager.start()

        def always(url, dest, **kw):
            raise DownloadError("timeout", "t/o")

        with patch("mosaic.dlqueue._fetch", side_effect=always):
            manager.wait_all(ids, poll=0.05)
        job = cache.get_job(ids[0])
        assert job["status"] == "FAILED"
        assert job["retry_count"] == 2  # max_tries=3 → initial + 2 retries

    def test_multi_candidate_advances(self, manager, cache):
        """Second candidate is used when the first URL is dead."""
        p = _paper(pdf_url=None)
        p.pdf_candidates = [
            PDFCandidate(p.uid, "https://a.example/1.pdf", source="ntrs").to_dict(),
            PDFCandidate(p.uid, "https://b.example/2.pdf", source="ntrs").to_dict(),
        ]
        ids = manager.enqueue_papers([p])
        manager.start()
        seen = []

        def fetch(url, dest, **kw):
            seen.append(url)
            if "a.example" in url:
                raise DownloadError("not_found")
            Path(dest).write_bytes(b"%PDF-1.4\n" + b"z" * 2000 + b"%%EOF")
            return url

        with patch("mosaic.dlqueue._fetch", side_effect=fetch):
            manager.wait_all(ids)
        assert seen == ["https://a.example/1.pdf", "https://b.example/2.pdf"]
        assert cache.get_job(ids[0])["status"] == "COMPLETED"


class TestControls:
    def test_pause_queued_then_resume(self, manager, cache):
        p = _paper()
        ids = manager.enqueue_papers([p])
        assert manager.pause(ids[0])
        assert cache.get_job(ids[0])["status"] == "PAUSED"
        assert manager.resume(ids[0])
        assert cache.get_job(ids[0])["status"] == "QUEUED"

    def test_cancel_marks_terminal(self, manager, cache):
        p = _paper()
        ids = manager.enqueue_papers([p])
        assert manager.cancel(ids[0])
        job = cache.get_job(ids[0])
        assert job["status"] == "CANCELLED"
        assert not manager.cancel(ids[0])  # already terminal

    def test_cancel_inflight_releases_slot(self, manager, cache):
        # Regression: cancel() must release the concurrency slot and per-host
        # allowance, or repeated cancels leak _inflight entries until the
        # scheduler permanently refuses to dispatch (queue freeze).
        p = _paper()
        ids = manager.enqueue_papers([p])
        job = cache.get_job(ids[0])
        manager._inflight[ids[0]] = {"gid": "g1", "host": job["host"]}
        manager._mark_host_start(job["host"])
        assert manager.cancel(ids[0])
        assert ids[0] not in manager._inflight
        assert manager._host_active.get(job["host"], 0) == 0
        assert manager._active_count() == 0

    def test_cancel_then_dispatch_continues(self, manager, cache):
        # Fill every concurrency slot with inflight jobs, cancel them all and
        # prove the scheduler still dispatches new work afterwards.
        manager.max_concurrent = 2
        papers = [_paper("a"), _paper("b")]
        ids = manager.enqueue_papers(papers)
        for jid in ids:
            job = cache.get_job(jid)
            manager._inflight[jid] = {"gid": jid, "host": job["host"]}
        assert manager._active_count() >= manager.max_concurrent
        for jid in ids:
            manager.cancel(jid)
        assert manager._active_count() == 0
        job = cache.get_job(ids[0])
        manager._dispatch(job)  # would early-return while slots are leaked
        assert cache.get_job(ids[0])["status"] != "QUEUED" or ids[0] in manager._inflight

    def test_retry_failed_requeues(self, manager, cache):
        p = _paper()
        ids = manager.enqueue_papers([p])
        cache.update_job(ids[0], status="FAILED", last_error="x")
        assert manager.retry(ids[0])
        job = cache.get_job(ids[0])
        assert job["status"] == "QUEUED"
        assert job["retry_count"] == 0

    def test_clear_removes_records_not_files(self, manager, cache, cfg):
        p = _paper()
        ids = manager.enqueue_papers([p])
        dest = Path(cfg["download_dir"]) / "keep.pdf"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"%PDF-1.4\n" + b"x" * 100 + b"%%EOF")
        cache.update_job(ids[0], status="COMPLETED", destination=str(dest))
        assert manager.clear(["COMPLETED"]) == 1
        assert dest.exists()
        assert cache.get_job(ids[0]) is None

    def test_pause_all_resume_all(self, manager, cache):
        papers = [_paper("1"), _paper("2", pdf_url="https://x.example/b.pdf")]
        ids = manager.enqueue_papers(papers)
        assert manager.pause_all() == 2
        assert all(cache.get_job(i)["status"] == "PAUSED" for i in ids)
        assert manager.resume_all() == 2


class TestReconcile:
    def test_restart_requeues_interrupted(self, cfg):
        cache1 = Cache(cfg["db_path"])
        m1 = DownloadManager(cache1, cfg)
        p = _paper()
        ids = m1.enqueue_papers([p])
        cache1.update_job(ids[0], status="DOWNLOADING")
        m1.shutdown()
        # simulate crash recovery: new manager reconciles
        cache2 = Cache(cfg["db_path"])
        m2 = DownloadManager(cache2, cfg)
        job = cache2.get_job(ids[0])
        assert job["status"] == "QUEUED" or True  # reconcile runs on start()
        m2.start()
        m2.shutdown()
        assert cache2.get_job(ids[0])["status"] == "QUEUED"

    def test_terminal_jobs_not_requeued(self, cfg):
        cache1 = Cache(cfg["db_path"])
        m1 = DownloadManager(cache1, cfg)
        p = _paper()
        ids = m1.enqueue_papers([p])
        cache1.update_job(ids[0], status="COMPLETED")
        m1.shutdown()
        cache2 = Cache(cfg["db_path"])
        m2 = DownloadManager(cache2, cfg)
        m2.start()
        m2.shutdown()
        assert cache2.get_job(ids[0])["status"] == "COMPLETED"


class TestPerHostLimit:
    def test_host_limit_blocks_dispatch(self, cfg, cache):
        cfg["download"]["per_host_concurrent"] = 1
        m = DownloadManager(cache, cfg)
        papers = [
            _paper("1", pdf_url="https://same.example/a.pdf"),
            _paper("2", pdf_url="https://same.example/b.pdf"),
        ]
        m.enqueue_papers(papers)
        assert m._host_allows("same.example")
        m._mark_host_start("same.example")
        assert not m._host_allows("same.example")
        m._unmark_host("same.example")
        assert m._host_allows("same.example")
        m.shutdown()


def job_dest(cache, job_id):
    return cache.get_job(job_id)["destination"]
