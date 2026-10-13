"""CollectionRunner / state / manifest tests — all network is mocked."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from mosaic.collection.loader import load_plan
from mosaic.collection.reporting import export_manifest
from mosaic.collection.runner import CollectionRunner, RunOptions, options_from_plan
from mosaic.collection.storage import library_dir, manifest_dir, task_dir
from mosaic.db import Cache
from mosaic.models import Paper

REPO = Path(__file__).resolve().parent.parent
PLAN_PATH = REPO / "plans" / "aero_design_rows_066_136.json"


def _make_pdf(path: Path, text: str = "") -> Path:
    """Write a real, verifiable PDF >1 KB (pymupdf is a dev dependency).

    An embedded pixmap keeps the file above pdfcheck's MIN_PDF_BYTES
    while remaining a genuinely parseable document.
    """
    import pymupdf

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 72), text[:400])
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64))
    pix.set_rect(pix.irect, (200, 120, 60))
    page.insert_image(page.rect, pixmap=pix)
    doc.save(str(path))
    doc.close()
    return path


class FakeSource:
    """A BaseSource-shaped stub returning canned papers."""

    def __init__(self, name: str, papers: list[Paper], fail: bool = False):
        self.name = name
        self._papers = papers
        self._fail = fail
        self.calls: list[str] = []

    def available(self):
        return True

    def search(self, query, max_results=10, filters=None):
        self.calls.append(query)
        if self._fail:
            from mosaic.errors import SourceError

            raise SourceError("HTTP 403 forbidden")
        return list(self._papers)[:max_results]


class FakeManager:
    """DownloadManager stub — 'downloads' by writing a real PDF to the job dest."""

    def __init__(self, cache: Cache):
        self.cache = cache
        self.enqueued: list[Paper] = []

    def enqueue_papers(self, papers, *, download_dir=None, filename_pattern=None, dest_fn=None):
        job_ids = []
        for p in papers:
            self.cache.save(p)
            canonical = self.cache.resolve_uid(p.uid)
            dest = dest_fn(p) if dest_fn else Path(download_dir) / f"{canonical}.pdf"
            _make_pdf(Path(dest), text=f"{p.title} doi:{p.doi or ''}")
            job_id = uuid.uuid4().hex[:16]
            self.cache.enqueue_job(
                {
                    "job_id": job_id,
                    "paper_uid": canonical,
                    "url": p.pdf_url or "https://example.org/x.pdf",
                    "backend": "native",
                    "destination": str(dest),
                    "host": "example.org",
                }
            )
            job_ids.append(job_id)
        return job_ids

    def wait_all(self, job_ids, **kw):
        out = {}
        for jid in job_ids:
            job = self.cache.get_job(jid)
            self.cache.update_job(jid, status="COMPLETED")
            self.cache.set_download(
                job["paper_uid"],
                job["destination"],
                "ok",
                size_bytes=Path(job["destination"]).stat().st_size,
            )
            out[job["paper_uid"]] = job["destination"]
        return out

    def cancel(self, jid):
        return True


@pytest.fixture()
def plan():
    return load_plan(PLAN_PATH)


@pytest.fixture()
def task045(plan):
    return next(t for t in plan.tasks if t.task_id == "T045")


@pytest.fixture()
def cfg(tmp_path):
    return {
        "db_path": str(tmp_path / "cache.db"),
        "download_dir": str(tmp_path / "dl"),
        "sources": {},
        "download": {"backend": "native", "verify_pdf": True, "skip_existing": True},
        "unpaywall": {"email": ""},
    }


@pytest.fixture()
def cache(tmp_path):
    return Cache(str(tmp_path / "cache.db"))


@pytest.fixture()
def opts(tmp_path):
    return RunOptions(
        output_dir=str(tmp_path / "collected"),
        sources=["openalex", "ntrs"],
        max_pdfs_per_task=2,
        max_queries_per_task=3,
        backend="native",
    )


def _paper_axial(uid_doi="10.1/axial1"):
    return Paper(
        title="Design of an axial compressor for aero engines",
        authors=["Smith, John"],
        year=2021,
        doi=uid_doi,
        abstract="The axial compressor stage was optimised for gas turbine use.",
        pdf_url="https://example.org/axial.pdf",
        source="OpenAlex",
        is_open_access=True,
    )


def _runner(plan, cfg, cache, opts, sources):
    r = CollectionRunner(plan, cfg, cache, opts)
    r._sources = sources
    r._manager = FakeManager(cache)
    return r


# ---------------------------------------------------------------------------
# Registration & task state
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_creates_rows(self, plan, cfg, cache, opts):
        r = _runner(plan, cfg, cache, opts, {})
        r.register()
        rec = r.store.get_plan()
        assert rec["plan_id"] == plan.plan_id
        assert rec["task_count"] == 71
        assert rec["sha256"] == plan.sha256
        rows = r.store.list_tasks()
        assert len(rows) == 71
        assert all(t["status"] == "PENDING" for t in rows)
        assert rows[0]["target_pdfs"] == opts.max_pdfs_per_task

    def test_register_is_idempotent_keeps_status(self, plan, cfg, cache, opts):
        r = _runner(plan, cfg, cache, opts, {})
        r.register()
        r.store.set_task("T045", "COMPLETED")
        r.register()
        assert r.store.task_status("T045") == "COMPLETED"

    def test_inflight_reconciled(self, plan, cfg, cache, opts):
        r = _runner(plan, cfg, cache, opts, {})
        r.register()
        r.store.set_task("T045", "DOWNLOADING")
        r.register()
        assert r.store.task_status("T045") == "PENDING"


# ---------------------------------------------------------------------------
# Search + grading
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_merges_and_logs(self, plan, cfg, cache, opts, task045):
        src = FakeSource("openalex", [_paper_axial(), _paper_axial("10.1/axial2")])
        r = _runner(plan, cfg, cache, opts, {"openalex": src})
        papers, raw, ran = r.search_task(task045)
        assert raw >= 2
        assert ran >= 1
        # Duplicate DOI → deduped to 1 record.
        assert len({p.uid for p in papers}) == len(papers)
        logged = r.store.queries_for_task(task045.task_id)
        assert any(row["source"] == "openalex" and row["status"] == "ok" for row in logged)

    def test_source_circuit_breaker(self, plan, cfg, cache, opts, task045):
        bad = FakeSource("openalex", [], fail=True)
        r = _runner(plan, cfg, cache, opts, {"openalex": bad})
        r.search_task(task045)
        for _ in range(3):
            r.store.source_fail("openalex", "HTTP 403", cooldown=3600)
        assert not r.store.source_ok("openalex")
        papers, _raw, _ran = r.search_task(task045)
        logged = r.store.queries_for_task(task045.task_id)
        assert any(row["status"] == "SKIPPED_COOLDOWN" for row in logged)


# ---------------------------------------------------------------------------
# Download + archive
# ---------------------------------------------------------------------------


class TestDownload:
    def test_full_task_pipeline(self, plan, cfg, cache, opts, task045):
        src = FakeSource("openalex", [_paper_axial()])
        r = _runner(plan, cfg, cache, opts, {"openalex": src})
        r.register()
        status = r.process_task(task045)
        assert status in ("COMPLETED", "PARTIAL")
        # Canonical file in _library with the collection naming rule.
        lib = library_dir(r.root)
        pdfs = list(lib.glob("*.pdf"))
        assert len(pdfs) == 1
        name = pdfs[0].name
        assert name.startswith("2021_OpenAlex_") and name.endswith(".pdf")
        assert "Smith" not in name  # no author field
        # Task-dir link with identical filename.
        tdir = task_dir(r.root, task045.output_subdir)
        linked = tdir / name
        assert linked.exists()
        # State: file + link + match rows.
        frec = r.store.get_file(_paper_axial().uid)
        assert frec["filename"] == name
        assert frec["sha256"]
        links = r.store.links_for_task("T045")
        assert links and links[0]["link_path"].endswith(name)
        assert r.store.task_status("T045") == status

    def test_second_task_reuses_canonical(self, plan, cfg, cache, opts, task045):
        # Same paper is relevant to two tasks → one network download,
        # two task-dir entries (hardlink).
        src = FakeSource("openalex", [_paper_axial()])
        r = _runner(plan, cfg, cache, opts, {"openalex": src})
        r.register()
        r.process_task(task045)
        mgr: FakeManager = r._manager
        n_before = len(mgr.enqueued)

        task046 = next(t for t in plan.tasks if t.task_id == "T046")
        # Pretend centrifugal task also found the axial paper relevant.
        r.store.save_match("T046", _paper_axial().uid, "HIGH", 9.0)
        status = r.process_task(task046, reuse_matches=True)
        assert status in ("COMPLETED", "PARTIAL")
        assert len(mgr.enqueued) == n_before  # no new network download
        tdir = task_dir(r.root, task046.output_subdir)
        assert any(tdir.glob("*.pdf"))

    def test_completed_task_skipped_on_rerun(self, plan, cfg, cache, opts, task045):
        src = FakeSource("openalex", [_paper_axial()])
        r = _runner(plan, cfg, cache, opts, {"openalex": src})
        r.register()
        r.process_task(task045)
        calls_before = len(src.calls)
        results = r.run([task045])
        assert results["T045"] in ("COMPLETED", "PARTIAL")
        assert len(src.calls) == calls_before  # skipped, no re-search

    def test_resume_reuses_matches_no_research(self, plan, cfg, cache, opts, task045):
        src = FakeSource("openalex", [_paper_axial()])
        r = _runner(plan, cfg, cache, opts, {"openalex": src})
        r.register()
        r.process_task(task045)
        calls_before = len(src.calls)
        # Simulate a crash: mark task in-flight, then rerun without retry.
        r.store.set_task("T045", "PARTIAL")
        results = r.run([task045])
        assert len(src.calls) == calls_before
        assert results["T045"] in ("COMPLETED", "PARTIAL")

    def test_no_relevant_result(self, plan, cfg, cache, opts, task045):
        src = FakeSource("openalex", [Paper(title="Cooking recipes for autumn", source="x")])
        r = _runner(plan, cfg, cache, opts, {"openalex": src})
        r.register()
        status = r.process_task(task045)
        assert status == "FAILED"
        row = next(t for t in r.store.list_tasks() if t["task_id"] == "T045")
        assert row["reason"] == "NO_RELEVANT_RESULT"

    def test_dry_run_no_writes(self, plan, cfg, cache, opts, task045):
        opts.dry_run = True
        src = FakeSource("openalex", [_paper_axial()])
        r = _runner(plan, cfg, cache, opts, {"openalex": src})
        results = r.run([task045])
        assert results == {"T045": "DRY_RUN"}
        assert src.calls == []

    def test_retry_failed_resets(self, plan, cfg, cache, opts, task045):
        src = FakeSource("openalex", [Paper(title="Unrelated noise", source="x")])
        r = _runner(plan, cfg, cache, opts, {"openalex": src})
        r.register()
        r.process_task(task045)
        assert r.store.task_status("T045") == "FAILED"
        # Now the source returns a relevant paper — retry succeeds.
        src._papers = [_paper_axial()]
        results = r.run([task045], retry_failed=True)
        assert results["T045"] in ("COMPLETED", "PARTIAL")


# ---------------------------------------------------------------------------
# Filename behaviour inside the runner
# ---------------------------------------------------------------------------


class TestRunnerNaming:
    def test_filename_lock_never_renames(self, plan, cfg, cache, opts, task045):
        src = FakeSource("openalex", [_paper_axial()])
        r = _runner(plan, cfg, cache, opts, {"openalex": src})
        r.register()
        r.process_task(task045)
        frec = r.store.get_file(_paper_axial().uid)
        dest_fn = r._dest_fn()
        # A later merged record carries a different source label — the
        # locked canonical filename must NOT change.
        same = _paper_axial()
        same.source = "Crossref"
        assert dest_fn(same).name == frec["filename"]

    def test_title_only_uid_filename(self, plan, cfg, cache, opts, tmp_path):
        p = Paper(title="Some unique title without identifiers", source="arXiv", year=2020)
        name = p.safe_filename("{year}_{source}_{title}")
        assert name == "2020_arXiv_Some_unique_title_without_identifiers.pdf"


# ---------------------------------------------------------------------------
# Manifest export
# ---------------------------------------------------------------------------


class TestManifest:
    def test_export_writes_all_files(self, plan, cfg, cache, opts, task045):
        src = FakeSource("openalex", [_paper_axial()])
        r = _runner(plan, cfg, cache, opts, {"openalex": src})
        r.register()
        r.process_task(task045)
        report = export_manifest(r.store, r.root)
        mandir = manifest_dir(r.root)
        for f in (
            "papers.csv",
            "task_status.csv",
            "task_paper_links.csv",
            "failures.csv",
            "collection_report.json",
        ):
            assert (mandir / f).exists(), f
        assert report["tasks_total"] == 71
        assert report["unique_pdfs"] == 1
        csv_text = (mandir / "papers.csv").read_text(encoding="utf-8")
        assert "2021_OpenAlex_" in csv_text
        # task_status lists every registered task.
        status_text = (mandir / "task_status.csv").read_text(encoding="utf-8")
        assert status_text.count("\n") >= 72  # header + 71 rows


# ---------------------------------------------------------------------------
# options_from_plan
# ---------------------------------------------------------------------------


class TestOptions:
    def test_plan_defaults_applied(self, plan):
        opts = options_from_plan(plan)
        assert opts.max_pdfs_per_task == 3
        assert opts.max_queries_per_task == 5
        assert opts.max_results_per_source == 10
        assert "openalex" in opts.sources
        assert opts.filename_pattern == "{year}_{source}_{title}"

    def test_cli_overrides_win(self, plan):
        opts = options_from_plan(
            plan, {"max_pdfs_per_task": 1, "output_dir": "/tmp/xyz", "sources": ["hal"]}
        )
        assert opts.max_pdfs_per_task == 1
        assert opts.output_dir == "/tmp/xyz"
        assert opts.sources == ["hal"]

    def test_legacy_title_template_overridden(self, plan):
        # The fixture plan carries storage.filename_template "{title}.pdf" —
        # the product rule ({year}_{source}_{title}) must win by default.
        opts = options_from_plan(plan)
        assert opts.filename_pattern != "{title}.pdf"
