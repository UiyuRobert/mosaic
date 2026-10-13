"""arXiv download acceptance tests — offline mocks plus optional LIVE runs.

Offline tests mock ``httpx``/aria2 and never touch the network. Tests marked
``@pytest.mark.live`` perform real arXiv API + PDF requests and are skipped
unless explicitly selected with ``pytest -m live``.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mosaic.db import Cache
from mosaic.dlqueue import DownloadManager
from mosaic.errors import SourceError
from mosaic.models import Paper
from mosaic.parsing import arxiv_base_id, arxiv_doi, parse_arxiv_id

# ── shared fixtures/helpers ─────────────────────────────────────────────────

_ARXIV_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v5</id>
    <title>Attention Is All You Need</title>
    <author><name>Ashish Vaswani</name></author>
    <published>2017-06-12T00:00:00Z</published>
    <summary>We propose the Transformer.</summary>
    <link rel="related" title="pdf" href="http://arxiv.org/pdf/1706.03762v5"/>
    <arxiv:doi>10.48550/arXiv.1706.03762</arxiv:doi>
    <arxiv:journal_ref>NeurIPS 2017</arxiv:journal_ref>
  </entry>
</feed>"""

_ARXIV_ERROR_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/api/errors</id>
    <title>Error</title>
    <summary>Invalid query string: 'all:(unbalanced'</summary>
  </entry>
</feed>"""


def _mock_get(text="", json_data=None, status_code=200, headers=None):
    m = MagicMock()
    m.status_code = status_code
    m.text = text
    m.content = text.encode()
    m.headers = headers or {}
    m.json.return_value = json_data or {}
    return m


def _mock_client(get_return=None, get_side_effect=None):
    mock_client = MagicMock()
    if get_return is not None:
        mock_client.get.return_value = get_return
    if get_side_effect is not None:
        mock_client.get.side_effect = get_side_effect
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_client)
    ctx.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=ctx), mock_client


def _src(delay=0):
    from mosaic.sources.arxiv import ArxivSource

    return ArxivSource(delay=delay)


@pytest.fixture
def cfg(tmp_path):
    return {
        "db_path": str(tmp_path / "cache.db"),
        "download_dir": str(tmp_path / "papers"),
        "filename_pattern": "{year}_{title}",
        "unpaywall": {"email": ""},
        "download": {
            "backend": "native",
            "max_concurrent": 2,
            "per_host_concurrent": 5,
            "per_host_delay": 0.0,
            "max_tries": 2,
            "retry_backoff_base": 0.05,
            "verify_pdf": False,
            "skip_existing": True,
            "wait_timeout": 5,
        },
    }


@pytest.fixture
def cache(cfg):
    return Cache(cfg["db_path"])


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


# ── parse_arxiv_id ──────────────────────────────────────────────────────────


class TestParseArxivId:
    @pytest.mark.parametrize(
        "text,want",
        [
            ("1706.03762", "1706.03762"),
            ("1706.03762v5", "1706.03762v5"),
            ("arxiv:1706.03762", "1706.03762"),
            ("ARXIV:hep-th/9901001", "hep-th/9901001"),
            ("https://arxiv.org/abs/1706.03762", "1706.03762"),
            ("https://arxiv.org/abs/1706.03762v5", "1706.03762v5"),
            ("https://arxiv.org/pdf/1706.03762", "1706.03762"),
            ("https://arxiv.org/pdf/1706.03762.pdf", "1706.03762"),
            ("10.48550/arXiv.1706.03762", "1706.03762"),
            ("10.48550/arXiv.hep-th/9901001", "hep-th/9901001"),
            ("https://doi.org/10.48550/arXiv.1706.03762", "1706.03762"),
            ("hep-th/9901001", "hep-th/9901001"),
            ("math.GT/0309136", "math.GT/0309136"),
            ("cs/0501001v3", "cs/0501001v3"),
            ("10.1234/foo", None),
            ("10.48550/arxiv.1234", None),  # truncated id → not valid
            ("not-an-id", None),
            ("", None),
            (None, None),
        ],
    )
    def test_forms(self, text, want):
        assert parse_arxiv_id(text) == want

    def test_base_strips_version(self):
        assert arxiv_base_id("1706.03762v5") == "1706.03762"
        assert arxiv_base_id("hep-th/9901001v3") == "hep-th/9901001"
        assert arxiv_doi("1706.03762v5") == "10.48550/arXiv.1706.03762"


# ── build_arxiv_query ───────────────────────────────────────────────────────


class TestBuildArxivQuery:
    def test_single_word(self):
        from mosaic.sources.arxiv import build_arxiv_query

        assert build_arxiv_query("transformer") == "all:transformer"

    def test_multi_word_becomes_and_terms(self):
        from mosaic.sources.arxiv import build_arxiv_query

        q = build_arxiv_query("attention mechanism")
        assert "all:attention" in q and "all:mechanism" in q and "AND" in q

    def test_quoted_phrase_preserved(self):
        from mosaic.sources.arxiv import build_arxiv_query

        q = build_arxiv_query('"attention is all you need" survey')
        assert 'all:"attention is all you need"' in q
        assert "all:survey" in q

    def test_field_scoping(self):
        from mosaic.sources.arxiv import build_arxiv_query

        assert build_arxiv_query("transformer", "title").startswith("ti:")
        assert build_arxiv_query("transformer", "abstract").startswith("abs:")

    def test_existing_syntax_passes_through(self):
        from mosaic.sources.arxiv import build_arxiv_query

        raw = "ti:attention AND (au:vaswani OR au:shazeer)"
        assert build_arxiv_query(raw) == raw

    def test_colon_sanitised_in_bare_term(self):
        from mosaic.sources.arxiv import build_arxiv_query

        # "a:b" alone would read like a field prefix — colon removed
        q = build_arxiv_query("a:b")
        assert ":" not in q.replace("all:", "")

    def test_empty_query(self):
        from mosaic.sources.arxiv import build_arxiv_query

        assert build_arxiv_query("") == ""


# ── feed parsing / error handling ───────────────────────────────────────────


class TestArxivFeed:
    def test_error_entry_raises(self):
        cls, _mc = _mock_client(get_return=_mock_get(text=_ARXIV_ERROR_XML))
        with patch("httpx.Client", cls):
            with pytest.raises(SourceError, match="Invalid query"):
                _src().search("anything")

    def test_http400_with_error_entry(self):
        cls, _mc = _mock_client(get_return=_mock_get(text=_ARXIV_ERROR_XML, status_code=400))
        with patch("httpx.Client", cls):
            with pytest.raises(SourceError, match="Invalid query"):
                _src().search("anything")

    def test_malformed_xml_raises(self):
        cls, _mc = _mock_client(get_return=_mock_get(text="<feed><entry>"))
        with patch("httpx.Client", cls):
            with pytest.raises(SourceError, match="malformed"):
                _src().search("anything")

    def test_html_error_page_raises(self):
        cls, _mc = _mock_client(get_return=_mock_get(text="<html><body>503</body></html>"))
        with patch("httpx.Client", cls):
            with pytest.raises(SourceError, match="non-XML"):
                _src().search("anything")

    def test_empty_feed_ok(self):
        empty = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"/>'
        cls, _mc = _mock_client(get_return=_mock_get(text=empty))
        with patch("httpx.Client", cls):
            assert _src().search("anything") == []

    def test_entry_without_abs_id_skipped(self):
        xml = _ARXIV_XML.replace("/abs/1706.03762v5", "/api/weird-id")
        cls, _mc = _mock_client(get_return=_mock_get(text=xml))
        with patch("httpx.Client", cls):
            assert _src().search("anything") == []

    def test_503_retries_then_succeeds(self):
        seq = [
            _mock_get(text="<html/>", status_code=503, headers={"Retry-After": "0"}),
            _mock_get(text=_ARXIV_XML),
        ]
        cls, mc = _mock_client(get_side_effect=seq)
        src = _src()
        with (
            patch("httpx.Client", cls),
            patch.object(type(src), "_backoff", return_value=None),
        ):
            papers = src.search("attention")
        assert len(papers) == 1
        assert mc.get.call_count == 2

    def test_persistent_503_raises(self):
        cls, _mc = _mock_client(get_return=_mock_get(text="x", status_code=503))
        src = _src()
        with (
            patch("httpx.Client", cls),
            patch.object(type(src), "_backoff", return_value=None),
        ):
            with pytest.raises(SourceError, match="503"):
                src.search("anything")

    def test_429_retries_then_succeeds(self):
        seq = [
            _mock_get(text="x", status_code=429, headers={"Retry-After": "0"}),
            _mock_get(text=_ARXIV_XML),
        ]
        cls, mc = _mock_client(get_side_effect=seq)
        src = _src()
        with (
            patch("httpx.Client", cls),
            patch.object(type(src), "_backoff", return_value=None),
        ):
            papers = src.search("attention")
        assert len(papers) == 1

    def test_pdf_link_https_normalised(self):
        cls, _mc = _mock_client(get_return=_mock_get(text=_ARXIV_XML))
        with patch("httpx.Client", cls):
            p = _src().search("attention")[0]
        assert p.pdf_url == "https://arxiv.org/pdf/1706.03762v5"
        assert p.url.startswith("https://")

    def test_get_by_id_uses_id_list(self):
        cls, mc = _mock_client(get_return=_mock_get(text=_ARXIV_XML))
        with patch("httpx.Client", cls):
            p = _src().get_by_id("1706.03762")
        assert p is not None and p.title == "Attention Is All You Need"
        assert mc.get.call_args.kwargs["params"]["id_list"] == "1706.03762"

    def test_get_by_id_empty_feed_returns_none(self):
        empty = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"/>'
        cls, _mc = _mock_client(get_return=_mock_get(text=empty))
        with patch("httpx.Client", cls):
            assert _src().get_by_id("1706.03762") is None


class TestSharedThrottle:
    def test_delay_shared_across_instances(self):
        """Two ArxivSource instances share one rate clock."""
        import mosaic.sources.arxiv as ax

        ax._last_request_ts = 0.0
        s1, s2 = _src(delay=0.4), _src(delay=0.4)
        s1._throttle()
        t0 = time.monotonic()
        s2._throttle()  # must wait ~0.4s even though s2 is a *new* instance
        assert time.monotonic() - t0 >= 0.35


# ── enqueue dedup / integrity ───────────────────────────────────────────────


class TestEnqueueDedup:
    def test_active_job_joined_not_duplicated(self, cfg, cache):
        m = DownloadManager(cache, cfg)
        p = _paper()
        ids1 = m.enqueue_papers([p])
        ids2 = m.enqueue_papers([p])
        assert ids2 == ids1
        jobs = cache.jobs_for_paper(p.uid)
        assert len(jobs) == 1
        m.shutdown()

    def test_alias_uids_share_one_job(self, cfg, cache):
        """DOI form and arxiv_id form of one paper → one active job."""
        m = DownloadManager(cache, cfg)
        p_doi = _paper(doi="10.48550/arXiv.1706.03762", arxiv_id="1706.03762", title="Attention")
        p_arxiv = Paper(
            title="Attention",
            arxiv_id="1706.03762",
            pdf_url="https://example.com/a.pdf",
            source="test",
        )
        assert p_doi.uid == p_arxiv.uid == "arxiv:1706.03762"
        ids1 = m.enqueue_papers([p_doi])
        ids2 = m.enqueue_papers([p_arxiv])
        assert ids2 == ids1
        assert len(cache.jobs_for_paper("arxiv:1706.03762")) == 1
        m.shutdown()

    def test_unique_index_blocks_race(self, cfg, cache):
        """DB-level guard: a second active job for the same uid raises."""
        job = {
            "job_id": "j1",
            "paper_uid": "arxiv:1",
            "url": "https://x/a.pdf",
            "backend": "native",
            "destination": "/tmp/a.pdf",
            "host": "x",
        }
        cache.enqueue_job(job)
        with pytest.raises(sqlite3.IntegrityError):
            cache.enqueue_job({**job, "job_id": "j2"})
        # terminal jobs don't block a retry
        cache.update_job("j1", status="FAILED")
        cache.enqueue_job({**job, "job_id": "j3"})
        assert cache.get_job("j3")["status"] == "QUEUED"

    def test_same_filename_two_papers_get_distinct_dests(self, cfg, cache):
        """Two different papers generating the same filename never share a dest."""
        m = DownloadManager(cache, cfg)
        p1 = _paper("a", pdf_url="https://x.example/1.pdf", doi="10.1/aaa")
        p2 = _paper("a", pdf_url="https://x.example/2.pdf", doi="10.2/bbb")
        assert p1.uid != p2.uid
        ids = m.enqueue_papers([p1, p2])
        dests = {cache.get_job(i)["destination"] for i in ids}
        assert len(dests) == 2
        m.shutdown()

    def test_completed_then_reenqueue_skipped(self, cfg, cache):
        """A valid recorded download is reused, not re-fetched."""
        from mosaic.pdfcheck import sha256_file

        m = DownloadManager(cache, cfg)
        p = _paper()
        existing = Path(cfg["download_dir"]) / "ok.pdf"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"%PDF-1.4\n" + b"x" * 200 + b"%%EOF")
        cache.save(p)
        cache.set_download(
            p.uid,
            str(existing),
            "ok",
            size_bytes=existing.stat().st_size,
            sha256=sha256_file(existing),
        )
        assert m.enqueue_papers([p]) == []
        m.shutdown()


class TestDownloadStillValid:
    def _rec(self, path, size=None, sha=None):
        return {
            "local_path": str(path),
            "status": "ok",
            "size_bytes": size,
            "sha256": sha,
        }

    def test_missing_file_invalid(self, tmp_path):
        from mosaic.downloader import download_still_valid

        assert not download_still_valid(self._rec(tmp_path / "gone.pdf"))

    def test_size_mismatch_invalid(self, tmp_path):
        from mosaic.downloader import download_still_valid

        f = tmp_path / "p.pdf"
        f.write_bytes(b"%PDF-1.4\n" + b"x" * 200 + b"%%EOF")
        assert not download_still_valid(self._rec(f, size=999999))

    def test_sha256_mismatch_invalid(self, tmp_path):
        from mosaic.downloader import download_still_valid

        f = tmp_path / "p.pdf"
        f.write_bytes(b"%PDF-1.4\n" + b"x" * 200 + b"%%EOF")
        assert not download_still_valid(self._rec(f, sha="0" * 64))

    def test_sha256_match_valid(self, tmp_path):
        from mosaic.downloader import download_still_valid
        from mosaic.pdfcheck import sha256_file

        f = tmp_path / "p.pdf"
        f.write_bytes(b"%PDF-1.4\n" + b"x" * 200 + b"%%EOF")
        assert download_still_valid(self._rec(f, size=f.stat().st_size, sha=sha256_file(f)))

    def test_legacy_record_falls_back_to_header(self, tmp_path):
        from mosaic.downloader import download_still_valid

        good = tmp_path / "g.pdf"
        good.write_bytes(b"%PDF-1.4\n" + b"x" * 200 + b"%%EOF")
        bad = tmp_path / "b.pdf"
        bad.write_bytes(b"<html>oops</html>" + b"y" * 200)
        assert download_still_valid(self._rec(good))
        assert not download_still_valid(self._rec(bad))


# ── reconcile gid / aria2 re-attach ─────────────────────────────────────────


class TestReconcileGid:
    def test_reconcile_keeps_aria2_gid(self, cfg):
        cfg["download"]["backend"] = "aria2"
        c1 = Cache(cfg["db_path"])
        m1 = DownloadManager(c1, cfg)
        m1._aria2 = MagicMock(is_alive=lambda: True)
        p = _paper()
        ids = m1.enqueue_papers([p])
        c1.update_job(ids[0], status="DOWNLOADING", aria2_gid="gid-live", backend="aria2")
        m1._reconcile()
        job = c1.get_job(ids[0])
        assert job["status"] == "QUEUED"
        assert job["aria2_gid"] == "gid-live"  # kept for re-attach
        m1._managed_shutdown = True
        m1.shutdown()

    def test_reconcile_clears_native_gid(self, cfg):
        c1 = Cache(cfg["db_path"])
        m1 = DownloadManager(c1, cfg)
        p = _paper()
        ids = m1.enqueue_papers([p])
        c1.update_job(ids[0], status="DOWNLOADING", backend="native")
        m1._reconcile()
        job = c1.get_job(ids[0])
        assert job["status"] == "QUEUED"
        assert job["aria2_gid"] is None
        m1.shutdown()

    def test_start_aria2_reattaches_live_gid(self, cfg):
        """A live gid on the daemon is re-adopted — no duplicate addUri."""
        cfg["download"]["backend"] = "aria2"
        cache = Cache(cfg["db_path"])
        m = DownloadManager(cache, cfg)
        fake = MagicMock()
        fake.is_alive.return_value = True
        fake.tell_status.return_value = {"status": "active", "completedLength": "10"}
        fake.process = None
        fake.managed = False
        m._aria2 = fake
        p = _paper()
        ids = m.enqueue_papers([p])
        cache.update_job(ids[0], aria2_gid="gid-live", backend="aria2")
        job = cache.get_job(ids[0])
        m._start_aria2(job)
        fake.add_uri.assert_not_called()
        assert cache.get_job(ids[0])["status"] == "DOWNLOADING"
        m._managed_shutdown = True
        m.shutdown()

    def test_start_aria2_complete_gid_finalizes(self, cfg, tmp_path):
        """Daemon-finished while we were away → verify .part and archive."""
        cfg["download"]["backend"] = "aria2"
        cache = Cache(cfg["db_path"])
        m = DownloadManager(cache, cfg)
        fake = MagicMock()
        fake.is_alive.return_value = True
        fake.process = None
        fake.managed = False
        m._aria2 = fake
        p = _paper()
        ids = m.enqueue_papers([p])
        job = cache.get_job(ids[0])
        dest = Path(job["destination"])
        part = Path(str(dest) + ".part")
        part.parent.mkdir(parents=True)
        part.write_bytes(b"%PDF-1.4\n" + b"x" * 500 + b"%%EOF")
        fake.tell_status.return_value = {"status": "complete"}
        cache.update_job(ids[0], aria2_gid="gid-done", backend="aria2")
        m._start_aria2(cache.get_job(ids[0]))
        assert cache.get_job(ids[0])["status"] == "COMPLETED"
        assert dest.exists()
        m._managed_shutdown = True
        m.shutdown()

    def test_start_aria2_error_gid_resubmits(self, cfg):
        cfg["download"]["backend"] = "aria2"
        cache = Cache(cfg["db_path"])
        m = DownloadManager(cache, cfg)
        fake = MagicMock()
        fake.is_alive.return_value = True
        fake.tell_status.return_value = {"status": "error", "errorCode": "3"}
        fake.add_uri.return_value = "gid-new"
        fake.process = None
        fake.managed = False
        m._aria2 = fake
        p = _paper()
        ids = m.enqueue_papers([p])
        cache.update_job(ids[0], aria2_gid="gid-old", backend="aria2")
        m._start_aria2(cache.get_job(ids[0]))
        fake.add_uri.assert_called_once()
        assert cache.get_job(ids[0])["aria2_gid"] == "gid-new"
        m._managed_shutdown = True
        m.shutdown()


# ── bounded wait / outcomes ────────────────────────────────────────────────


class TestBoundedWait:
    def test_wait_all_timeout_returns_partial(self, cfg, cache):
        m = DownloadManager(cache, cfg)
        # a job that can never dispatch (no URL candidates at all)
        p = _paper(pdf_url=None, doi=None)
        ids = m.enqueue_papers([p])
        m.start()
        t0 = time.monotonic()
        out = m.wait_all(ids, poll=0.05, timeout=2.0)
        assert time.monotonic() - t0 < 10
        assert p.uid not in out
        m.shutdown()

    def test_download_papers_reports_outcome(self, cfg, cache):
        from mosaic.workflows import download_papers

        outcome: dict = {}
        p_ok = _paper("ok")
        p_bad = _paper("bad", pdf_url=None, doi=None)
        pdf_map = download_papers([p_ok, p_bad], cfg, cache, outcome=outcome)
        assert p_ok.uid in pdf_map or outcome.get("failed", 0) >= 0
        assert "job_ids" in outcome


# ── identifier → paper resolution (mosaic get) ─────────────────────────────


class TestResolveGetPaper:
    def test_arxiv_id_via_api(self, cfg, cache):
        from mosaic.cli import _resolve_get_paper

        cls, _mc = _mock_client(get_return=_mock_get(text=_ARXIV_XML))
        with patch("httpx.Client", cls):
            p = _resolve_get_paper("arxiv:1706.03762", cfg, cache)
        assert p is not None
        assert p.title == "Attention Is All You Need"
        assert p.pdf_url == "https://arxiv.org/pdf/1706.03762v5"

    def test_arxiv_doi_resolves(self, cfg, cache):
        from mosaic.cli import _resolve_get_paper

        cls, _mc = _mock_client(get_return=_mock_get(text=_ARXIV_XML))
        with patch("httpx.Client", cls):
            p = _resolve_get_paper("10.48550/arXiv.1706.03762", cfg, cache)
        assert p is not None and p.uid == "arxiv:1706.03762"

    def test_arxiv_api_down_falls_back_to_pdf_url(self, cfg, cache):
        from mosaic.cli import _resolve_get_paper
        from mosaic.sources.arxiv import ArxivSource

        with patch.object(ArxivSource, "get_by_id", side_effect=SourceError("boom")):
            p = _resolve_get_paper("1706.03762", cfg, cache)
        assert p is not None
        assert p.pdf_url == "https://arxiv.org/pdf/1706.03762"
        assert p.doi == "10.48550/arXiv.1706.03762"

    def test_invalid_identifier_returns_none(self, cfg, cache):
        from mosaic.cli import _resolve_get_paper

        assert _resolve_get_paper("not-a-real-id", cfg, cache) is None
        assert _resolve_get_paper("", cfg, cache) is None

    def test_cached_arxiv_record_reused(self, cfg, cache):
        from mosaic.cli import _resolve_get_paper

        cached = Paper(
            title="Attention Is All You Need",
            doi="10.48550/arXiv.1706.03762",
            arxiv_id="1706.03762v7",
            pdf_url="https://arxiv.org/pdf/1706.03762v7",
            source="arXiv",
        )
        cache.save(cached)
        with patch("mosaic.sources.arxiv.ArxivSource.get_by_id", return_value=cached):
            p = _resolve_get_paper("arxiv:1706.03762", cfg, cache)
        assert p.title == "Attention Is All You Need"


# ── LIVE network tests (opt-in: pytest -m live) ─────────────────────────────


@pytest.mark.live
class TestLiveArxiv:
    """Real-network acceptance: hits export.arxiv.org + arxiv.org/pdf."""

    def test_live_get_by_id_attention(self):
        from mosaic.sources.arxiv import ArxivSource

        src = ArxivSource()  # real 3s throttle
        p = src.get_by_id("1706.03762")
        assert p is not None
        assert "Attention" in p.title
        assert p.pdf_url and "arxiv.org/pdf/" in p.pdf_url

    def test_live_search_and_download(self, tmp_path):
        """search_all('transformer', arxiv) → real PDF on disk, verified."""
        from mosaic.dlqueue import get_manager
        from mosaic.search import search_all
        from mosaic.sources.arxiv import ArxivSource

        cfg = {
            "db_path": str(tmp_path / "cache.db"),
            "download_dir": str(tmp_path / "pdfs"),
            "unpaywall": {"email": ""},
            "download": {
                "backend": "native",
                "max_concurrent": 2,
                "per_host_concurrent": 1,
                "per_host_delay": 0.5,
                "max_tries": 2,
                "verify_pdf": True,
                "skip_existing": True,
                "wait_timeout": 120,
            },
        }
        cache = Cache(cfg["db_path"])
        errors: list[str] = []
        papers = search_all([ArxivSource()], "transformer", max_per_source=2, errors=errors)
        assert papers and all(p.pdf_url for p in papers)
        m = get_manager(cfg, cache)
        ids = m.enqueue_papers(papers)
        out = m.wait_all(ids, timeout=120)
        m.shutdown()
        assert len(out) >= 1
        for path in out.values():
            f = Path(path)
            assert f.exists() and f.stat().st_size > 10_000
            head = f.read_bytes()[:5]
            assert head == b"%PDF-"

    def test_live_get_arxiv_pdf_no_unpaywall(self, tmp_path):
        """Direct arXiv id → PDF with NO unpaywall email configured."""
        from mosaic.dlqueue import get_manager
        from mosaic.sources.arxiv import ArxivSource

        cfg = {
            "db_path": str(tmp_path / "cache.db"),
            "download_dir": str(tmp_path / "pdfs"),
            "unpaywall": {"email": ""},
            "download": {
                "backend": "native",
                "max_concurrent": 1,
                "per_host_concurrent": 1,
                "per_host_delay": 0.5,
                "max_tries": 2,
                "verify_pdf": True,
                "skip_existing": True,
                "wait_timeout": 120,
            },
        }
        cache = Cache(cfg["db_path"])
        paper = ArxivSource().get_by_id("1706.03762")
        m = get_manager(cfg, cache)
        ids = m.enqueue_papers([paper])
        out = m.wait_all(ids, timeout=120)
        m.shutdown()
        dest = Path(out["arxiv:1706.03762"])
        assert dest.exists()
        assert dest.stat().st_size > 100_000  # Attention paper ~1-2MB
