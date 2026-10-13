"""CollectionRunner — executes a collection plan end to end.

Per task: tiered queries across the plan's sources (per-source circuit
breaker) → dedup → relevance grading → candidate selection → download
through the shared :class:`~mosaic.dlqueue.DownloadManager` (native or
aria2 — the runner never implements its own fetch pipeline) → PDF
identity check → canonical-library archiving + task-dir links →
persistent status.

Every step is resumable: query logs, matches, canonical files and
task links all live in the main cache.db, so re-running a plan never
re-downloads an already-verified paper and never duplicates files.
"""

from __future__ import annotations

import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from mosaic.collection.loader import CollectionPlan, CollectionTask, _task_from
from mosaic.collection.planner import planned_queries, queries_for_source, task_context
from mosaic.collection.relevance import pdf_identity, rank_for_download, score_paper
from mosaic.collection.state import (
    DONE_STATUSES,
    FAILED_STATUSES,
    CollectionStore,
)
from mosaic.collection.storage import (
    COLLECTION_FILENAME_PATTERN,
    canonical_path,
    library_dir,
    link_or_copy,
    manifest_dir,
    paper_filename,
    task_dir,
)
from mosaic.db import Cache
from mosaic.dlqueue import TERMINAL, DownloadManager, get_manager
from mosaic.downloader import download_still_valid
from mosaic.models import Paper
from mosaic.pdfcheck import sha256_file, verify_pdf
from mosaic.services import merge_papers
from mosaic.source_registry import SHORTHAND_TO_CFG_KEY, build_sources

log = logging.getLogger(__name__)

# Default free/no-auth sources for collection runs (config keys).
DEFAULT_SOURCES = [
    "openalex",
    "ntrs",
    "crossref",
    "hal",
    "zenodo",
    "arxiv",
    "doaj",
    "semantic_scholar",
]


@dataclass
class RunOptions:
    """Execution parameters — resolved from plan defaults + CLI overrides."""

    output_dir: str = "./collected-papers"
    sources: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCES))
    max_queries_per_task: int = 5
    max_results_per_source: int = 10
    max_pdfs_per_task: int = 3
    max_dl_candidates: int = 0  # 0 → derived (target*2 + 2)
    max_disk_gb: float = 20.0
    filename_pattern: str = COLLECTION_FILENAME_PATTERN
    backend: str = "auto"
    download_enabled: bool = True
    task_dl_budget: int = 900  # seconds of download waiting per task
    dry_run: bool = False
    include_completed: bool = False
    early_stop_high: int = 2  # stop querying once this many HIGH matches exist
    min_queries_before_stop: int = 2

    @property
    def dl_attempt_cap(self) -> int:
        return self.max_dl_candidates or (self.max_pdfs_per_task * 2 + 2)


def options_from_plan(plan: CollectionPlan, overrides: dict | None = None) -> RunOptions:
    """Build :class:`RunOptions` from plan ``defaults`` + CLI *overrides*.

    *overrides* maps RunOptions field names → values; ``None`` values are
    ignored so unset CLI flags never clobber plan defaults.
    """
    d = plan.defaults or {}
    search = d.get("search", {}) or {}
    download = d.get("download", {}) or {}
    storage = d.get("storage", {}) or {}

    sources = search.get("sources") or list(DEFAULT_SOURCES)
    pattern = storage.get("filename_pattern") or COLLECTION_FILENAME_PATTERN
    # Historical plan files may carry a "{title}.pdf" storage template —
    # the collection rule ({year}_{source}_{title}) is the product default;
    # only honour templates that actually produce the year_source_title shape
    # or that the caller explicitly passed via overrides.
    opts = RunOptions(
        output_dir=storage.get("root_dir") or "./collected-papers",
        sources=[str(s) for s in sources],
        max_queries_per_task=int(search.get("max_queries_per_task", 5)),
        max_results_per_source=int(search.get("max_results_per_source", 10)),
        max_pdfs_per_task=int(download.get("max_pdfs_per_task", 3)),
        max_dl_candidates=int(download.get("max_dl_candidates", 0)),
        max_disk_gb=float(storage.get("max_disk_gb", 20.0)),
        filename_pattern=pattern
        if "{year}" in pattern and "{source}" in pattern
        else COLLECTION_FILENAME_PATTERN,
        backend=str(download.get("backend", "auto")),
        download_enabled=bool(download.get("enabled", True)),
    )
    for k, v in (overrides or {}).items():
        if v is not None and hasattr(opts, k):
            setattr(opts, k, v)
    return opts


class CollectionRunner:
    """Runs a :class:`CollectionPlan` against live sources."""

    def __init__(
        self,
        plan: CollectionPlan,
        cfg: dict,
        cache: Cache,
        options: RunOptions,
    ) -> None:
        self.plan = plan
        self.cfg = cfg
        self.cache = cache
        self.opts = options
        self.store = CollectionStore(cache, plan.plan_id)
        self.root = Path(options.output_dir).expanduser().resolve()
        self.libdir = library_dir(self.root)
        self.mandir = manifest_dir(self.root)
        self._taken: dict[str, str] = {}  # filename → uid (this run)
        self._manager: DownloadManager | None = None
        # Set by callers (web UI) — checked between tasks so a running
        # plan pauses cleanly at task boundaries.
        self.stop_event = None
        self._sources = self._build_sources()
        self._ctx_cache: dict[str, list[str]] = {}

    # ── setup ─────────────────────────────────────────────────────────────

    def _build_sources(self) -> dict[str, object]:
        """Instantiate plan-requested sources via the shared registry."""
        wanted: list[str] = []
        for s in self.opts.sources:
            key = SHORTHAND_TO_CFG_KEY.get(s, s)
            if key not in wanted:
                wanted.append(key)
        cfg = dict(self.cfg)
        src_cfg = {k: dict(v) for k, v in (self.cfg.get("sources") or {}).items()}
        for key in src_cfg:
            src_cfg[key]["enabled"] = key in wanted
        cfg["sources"] = src_cfg
        out: dict[str, object] = {}
        for src in build_sources(cfg):
            # Recover the config key that produced this instance.
            key = self._key_for_source(src, wanted)
            out[key or src.name] = src
        return out

    @staticmethod
    def _key_for_source(src, wanted: list[str]) -> str:
        name = getattr(src, "name", "")
        lowered = name.lower().replace(" ", "_")
        for key in wanted:
            if key in lowered or lowered.startswith(key):
                return key
        # Config-key → display-name special cases.
        for key, needle in (
            ("ntrs", "ntrs"),
            ("openalex", "openalex"),
            ("semantic_scholar", "semantic"),
            ("nasa_ads", "ads"),
            ("europepmc", "europe"),
            ("biorxiv", "rxiv"),
            ("pmc", "pubmed central"),
        ):
            if key in wanted and needle in lowered:
                return key
        return lowered

    def _ctx(self, task: CollectionTask) -> list[str]:
        if task.task_id not in self._ctx_cache:
            self._ctx_cache[task.task_id] = task_context(task)
        return self._ctx_cache[task.task_id]

    def manager(self) -> DownloadManager:
        if self._manager is None:
            self._manager = get_manager(self.cfg, self.cache)
        return self._manager

    def register(self) -> None:
        """Persist the plan + task rows; reconcile crashed in-flight states."""
        self.store.save_plan(
            title=self.plan.title,
            path=self.plan.path,
            sha256=self.plan.sha256,
            task_count=len(self.plan.tasks),
        )
        target = self.opts.max_pdfs_per_task
        for t in self.plan.tasks:
            self.store.register_task(
                t.task_id,
                excel_row=t.excel_row,
                domain=t.domain,
                topic=t.topic,
                priority=t.priority,
                zh_primary=t.zh.primary,
                en_primary=t.en.primary,
                output_subdir=t.output_subdir,
                target_pdfs=target,
                task_json=t.to_json(),
            )
        self.store.reset_inflight()

    # ── search ────────────────────────────────────────────────────────────

    def _search_one(self, key: str, src, query: str):
        if not self.store.source_ok(key):
            return key, [], "SKIPPED_COOLDOWN", ""
        try:
            papers = src.search(query, max_results=self.opts.max_results_per_source)
            return key, papers, "ok", ""
        except Exception as e:
            return key, [], "error", f"{type(e).__name__}: {e}"

    def search_task(self, task: CollectionTask) -> tuple[list[Paper], int, int]:
        """Run tiered queries for *task*; returns (deduped papers, raw count, queries run)."""
        queries = planned_queries(task, self.opts.max_queries_per_task)
        seen: dict[str, Paper] = {}
        ident_index: dict[str, str] = {}
        raw = 0
        ran = 0
        stop = False
        for pq in queries:
            if stop:
                break
            active = {
                k: s
                for k, s in self._sources.items()
                if self.store.source_ok(k) and pq in queries_for_source([pq], k)
            }
            for key in set(self._sources) - set(active):
                self.store.log_query(task.task_id, pq.tier, pq.query, key, "SKIPPED_COOLDOWN", 0)
            if not active:
                continue
            ran += 1
            with ThreadPoolExecutor(max_workers=min(4, len(active))) as ex:
                results = list(
                    ex.map(lambda kv, q=pq.query: self._search_one(kv[0], kv[1], q), active.items())
                )
            for key, papers, status, err in results:
                if status == "ok":
                    self.store.source_success(key)
                elif status == "error":
                    cooldown = 3600.0 if "403" in err else (1200.0 if "429" in err else 0)
                    self.store.source_fail(key, err, cooldown=cooldown)
                self.store.log_query(task.task_id, pq.tier, pq.query, key, status, len(papers), err)
                raw += len(papers)
                for p in papers:
                    self.cache.save(p)
                    merge_papers(seen, p, ident_index)
            # Early stop: enough strong matches after the minimum query spend.
            if ran >= self.opts.min_queries_before_stop and len(seen) >= 4:
                grades = [score_paper(task, p, self._ctx(task))[0] for p in seen.values()]
                if grades.count("HIGH") >= self.opts.early_stop_high:
                    stop = True
        return list(seen.values()), raw, ran

    # ── download / archive ────────────────────────────────────────────────

    def _disk_ok(self) -> bool:
        try:
            used = sum(f.stat().st_size for f in self.libdir.rglob("*.pdf"))
        except OSError:
            used = 0
        if used >= self.opts.max_disk_gb * 1024**3:
            return False
        try:
            return shutil.disk_usage(self.libdir).free > 1 * 1024**3
        except OSError:
            return True

    def _dest_fn(self):
        taken = self._taken

        def resolve(paper: Paper) -> Path:
            # Locked filename for papers already in the library — the name
            # never changes once a canonical file exists.
            rec = self.store.get_file(paper.uid)
            if rec and rec.get("filename"):
                return self.libdir / rec["filename"]
            owner = self.store.filename_owner(paper_filename(paper, self.opts.filename_pattern))
            if owner and owner != paper.uid:
                taken[paper_filename(paper, self.opts.filename_pattern)] = owner
            return canonical_path(
                self.libdir,
                paper,
                pattern=self.opts.filename_pattern,
                taken=taken,
            )

        return resolve

    def _link_into_task(self, task: CollectionTask, canonical: Path) -> str:
        """Ensure the task dir shows *canonical* under its filename."""
        dst = task_dir(self.root, task.output_subdir) / canonical.name
        link_or_copy(canonical, dst)
        return str(dst)

    def _adopt_existing(self, task: CollectionTask, paper: Paper) -> str | None:
        """Re-archive a previously verified file; returns canonical path."""
        rec = self.cache.get_download(paper.uid)
        if not rec or rec["status"] != "ok" or not rec["local_path"]:
            return None
        path = Path(rec["local_path"])
        if not path.exists() or not download_still_valid(rec):
            return None
        # Already inside our library → reuse as canonical directly.
        try:
            path.resolve().relative_to(self.libdir)
            canonical = path
        except ValueError:
            # Outside the library (e.g. an old ~/mosaic-papers download):
            # hardlink it in under the collection naming rule — no re-download.
            canonical = canonical_path(
                self.libdir, paper, pattern=self.opts.filename_pattern, taken=self._taken
            )
            try:
                link_or_copy(path, canonical)
            except OSError as e:
                log.debug("adopt %s failed: %s", paper.uid, e)
                return None
        self._record_canonical(task, paper, canonical, identity="adopted")
        return str(canonical)

    def _record_canonical(
        self,
        task: CollectionTask,
        paper: Paper,
        canonical: Path,
        *,
        identity: str = "",
        identity_detail: str = "",
    ) -> None:
        sha = sha256_file(canonical)
        self.store.save_file(
            paper.uid,
            canonical_path=str(canonical),
            filename=canonical.name,
            sha256=sha,
            size_bytes=canonical.stat().st_size,
            status="ok" if identity != "mismatch" else "mismatch",
            error=identity_detail,
            identity=identity,
        )
        # Task-dir link — but only for identity-verified files; a
        # mismatched PDF stays in the library for review without being
        # presented as a successful task artefact.
        if identity != "mismatch":
            link_path = self._link_into_task(task, canonical)
            self.store.save_link(task.task_id, paper.uid, link_path)
            self.store.mark_match_downloaded(task.task_id, paper.uid)
        self.store.log_download(
            task.task_id,
            paper.uid,
            self.opts.backend,
            paper.pdf_url or "",
            "ok" if identity != "mismatch" else "pdf_mismatch",
            identity_detail or identity,
            str(canonical),
            sha,
        )

    def _record_failure(self, task: CollectionTask, paper: Paper, result: str, reason: str) -> None:
        self.store.log_download(
            task.task_id,
            paper.uid,
            self.opts.backend,
            paper.pdf_url or "",
            result,
            reason,
            "",
            "",
        )

    def download_task(self, task: CollectionTask, ranked: list[tuple[Paper, str, float]]) -> int:
        """Download/archive candidates until target met; returns verified pdf count.

        Runs in rounds sized to the remaining gap (``target - ok``) so a
        task never fetches more PDFs than needed; failed candidates are
        replaced by the next-best ranked papers on the following round.
        """
        ok = 0
        target = self.opts.max_pdfs_per_task
        attempted: set[str] = set()
        idx = 0
        disk_full = False

        while ok < target and self.opts.download_enabled and not disk_full:
            batch: list[tuple[Paper, str, float]] = []
            while idx < len(ranked) and len(batch) < (target - ok):
                paper, grade, _s = ranked[idx]
                idx += 1
                if paper.uid in attempted:
                    continue
                if len(attempted) >= self.opts.dl_attempt_cap:
                    break
                attempted.add(paper.uid)
                # 1) Canonical file already in the library -> relink, no network.
                frec = self.store.get_file(paper.uid)
                if (
                    frec
                    and frec["status"] == "ok"
                    and frec.get("canonical_path")
                    and Path(frec["canonical_path"]).exists()
                ):
                    canonical = Path(frec["canonical_path"])
                    link_path = self._link_into_task(task, canonical)
                    self.store.save_link(task.task_id, paper.uid, link_path)
                    self.store.mark_match_downloaded(task.task_id, paper.uid)
                    ok += 1
                    continue
                # 2) Verified download elsewhere (core downloads table)?
                if self._adopt_existing(task, paper):
                    ok += 1
                    continue
                if not (paper.doi or paper.pdf_url or paper.pdf_candidates or paper.ntrs_id):
                    self._record_failure(
                        task, paper, "failed", "no_candidates: no DOI/URL/attachment id"
                    )
                    continue
                if not self._disk_ok():
                    self._record_failure(task, paper, "failed", "disk_full: budget exhausted")
                    disk_full = True
                    break
                batch.append((paper, grade, _s))
            if not batch:
                break
            ok += self._download_batch(task, batch)
            if idx >= len(ranked):
                break
        return ok

    def _download_batch(self, task: CollectionTask, batch: list[tuple[Paper, str, float]]) -> int:
        """Enqueue one batch through the DownloadManager; return verified count."""
        mgr = self.manager()
        ok = 0
        job_ids = mgr.enqueue_papers(
            [p for p, _g, _s in batch],
            download_dir=str(self.libdir),
            filename_pattern=self.opts.filename_pattern,
            dest_fn=self._dest_fn(),
        )
        # Papers silently skipped by enqueue (verified file already on disk
        # in the core downloads table) still satisfy the task.
        by_uid = {p.uid: (p, g, s) for p, g, s in batch}
        queued: dict[str, tuple[Paper, str, float]] = {}
        for jid in job_ids:
            job = self.cache.get_job(jid)
            rec = by_uid.get(job["paper_uid"]) if job else None
            if rec:
                queued[jid] = rec
        results = mgr.wait_all(list(queued), poll=0.4, timeout=self.opts.task_dl_budget)

        queued_uids = {rec[0].uid for rec in queued.values()}
        for paper, _g, _s in batch:
            if paper.uid in queued_uids:
                continue
            if self._adopt_existing(task, paper):
                ok += 1
            else:
                self._record_failure(task, paper, "failed", "not_enqueued")

        for jid, (paper, _g, _s) in queued.items():
            job = self.cache.get_job(jid)
            canonical = results.get(paper.uid) or results.get(self.cache.resolve_uid(paper.uid))
            if not canonical and job and job["status"] == "COMPLETED" and job.get("destination"):
                canonical = job["destination"]
            if not canonical:
                if job and job["status"] not in TERMINAL:
                    mgr.cancel(jid)
                reason = (job or {}).get("last_error") or "unknown"
                self._record_failure(task, paper, "failed", reason[:300])
                continue
            cpath = Path(canonical)
            # PDF structural re-verification (queue already did one - cheap).
            try:
                verify_pdf(cpath)
            except Exception as e:
                self._record_failure(task, paper, "failed", f"pdf_invalid: {e}")
                continue
            ident, detail = pdf_identity(cpath, paper)
            self._record_canonical(task, paper, cpath, identity=ident, identity_detail=detail)
            if ident != "mismatch":
                ok += 1
        return ok

    # ── classification ────────────────────────────────────────────────────

    def _classify(
        self,
        task: CollectionTask,
        candidates: list[Paper],
        ranked: list[tuple[Paper, str, float]],
        pdf_ok: int,
    ) -> tuple[str, str]:
        """Return (status, reason) — reason is '' on full/partial success."""
        if pdf_ok >= self.opts.max_pdfs_per_task:
            return "COMPLETED", "OK"
        if pdf_ok > 0:
            return "PARTIAL", "OK"
        if not candidates:
            if self._sources and all(not self.store.source_ok(k) for k in self._sources):
                return "FAILED", "SOURCE_BLOCKED"
            return "FAILED", "NO_RELEVANT_RESULT"
        if not ranked:
            return "FAILED", "NO_RELEVANT_RESULT"
        reasons = set()
        for r in self.store.download_reasons(task.task_id):
            reasons.add(r.split(":")[0].strip())
        if reasons & {"access_denied"}:
            return "FAILED", "ACCESS_RESTRICTED"
        if reasons & {"rate_limited"}:
            return "FAILED", "RATE_LIMITED"
        if reasons & {"html_response", "invalid_pdf", "not_pdf", "missing_eof", "unparseable"}:
            return "FAILED", "PDF_INVALID"
        if reasons & {"disk_full"}:
            return "FAILED", "DISK_FULL"
        if reasons & {"pdf_mismatch"}:
            return "FAILED", "PDF_MISMATCH"
        if reasons & {"not_found", "unreachable", "timeout", "server_error", "dns_error"}:
            return "FAILED", "DOWNLOAD_FAILED"
        if reasons & {"no_candidates"} or not any(
            p.doi or p.pdf_url or p.pdf_candidates or p.ntrs_id for p, _g, _s in ranked
        ):
            if any(p.doi for p, _g, _s in ranked):
                return "FAILED", "NO_PUBLIC_PDF"
            return "METADATA_ONLY", "NO_PUBLIC_PDF"
        return "FAILED", "NO_PUBLIC_PDF"

    # ── public pipeline ───────────────────────────────────────────────────

    def process_task(self, task: CollectionTask, *, reuse_matches: bool = False) -> str:
        """Run the full search→grade→download→archive pipeline for *task*."""
        opts = self.opts
        self.store.set_task(task.task_id, "SEARCHING")
        ranked: list[tuple[Paper, str, float]] = []
        candidates: list[Paper] = []
        raw = 0

        if reuse_matches:
            rows = self.store.matches_for_task(task.task_id)
            papers = {
                p.uid: p for p in self.cache.get_papers_by_uids([r["paper_uid"] for r in rows])
            }
            for r in rows:
                p = papers.get(r["paper_uid"])
                if p:
                    ranked.append((p, r["grade"], r["score"]))
            candidates = [p for p, _g, _s in ranked]

        ran_queries = 0
        if not ranked:
            candidates, raw, ran_queries = self.search_task(task)
            scored = [(p, *score_paper(task, p, self._ctx(task))) for p in candidates]
            for p, grade, score, why in scored:
                if grade in ("HIGH", "MEDIUM"):
                    self.store.save_match(task.task_id, p.uid, grade, score, ";".join(why))
            ranked = rank_for_download(scored)
        else:
            raw = len(candidates)

        self.store.set_task(
            task.task_id,
            "FOUND" if ranked else "FAILED",
            candidates=raw or len(candidates),
            relevant=len(ranked),
            queries_run=ran_queries,
            reason="" if ranked else "NO_RELEVANT_RESULT",
        )
        if not ranked:
            return self._finish(task, candidates, ranked, 0)

        pdf_ok = 0
        if opts.download_enabled:
            self.store.set_task(task.task_id, "DOWNLOADING")
            try:
                pdf_ok = self.download_task(task, ranked)
            except Exception as e:
                log.exception("download phase failed for %s", task.task_id)
                self.store.log_download(
                    task.task_id, "", opts.backend, "", "failed", f"runner: {e}"
                )
        return self._finish(task, candidates, ranked, pdf_ok)

    def _finish(
        self,
        task: CollectionTask,
        candidates: list[Paper],
        ranked: list[tuple[Paper, str, float]],
        pdf_ok: int,
    ) -> str:
        if not self.opts.download_enabled and ranked:
            status, reason = "METADATA_ONLY", "download_disabled"
        else:
            status, reason = self._classify(task, candidates, ranked, pdf_ok)
        self.store.set_task(task.task_id, status, reason=reason, pdf_count=pdf_ok)
        return status

    # ── plan-level driver ─────────────────────────────────────────────────

    def run(
        self,
        tasks: list[CollectionTask],
        *,
        retry_failed: bool = False,
        progress_cb=None,
    ) -> dict[str, str]:
        """Execute *tasks*; returns ``{task_id: status}``.

        ``retry_failed`` resets non-completed tasks to PENDING first.
        ``progress_cb(task, status)`` fires after each task.
        """
        self.register()
        if self.opts.dry_run:
            return {t.task_id: "DRY_RUN" for t in tasks}
        if retry_failed:
            self.store.reset_statuses(FAILED_STATUSES, task_ids={t.task_id for t in tasks})

        results: dict[str, str] = {}
        for task in tasks:
            if self.stop_event is not None and self.stop_event.is_set():
                break
            status = self.store.task_status(task.task_id)
            if status in DONE_STATUSES and not self.opts.include_completed:
                results[task.task_id] = status
                self._rearchive_task(task)
                if progress_cb:
                    progress_cb(task, status, skipped=True)
                continue
            # Retry after a reset starts from PENDING → fresh search; a
            # resumable task keeps its recorded matches instead.
            reuse = bool(status and status != "PENDING" and not retry_failed)
            try:
                results[task.task_id] = self.process_task(task, reuse_matches=reuse)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.exception("task %s crashed", task.task_id)
                self.store.set_task(task.task_id, "NEEDS_REVIEW", reason=f"runner: {e}")
                results[task.task_id] = "NEEDS_REVIEW"
            if progress_cb:
                progress_cb(task, results[task.task_id], skipped=False)
        return results

    def _rearchive_task(self, task: CollectionTask) -> None:
        """Re-materialise a completed task's links under the current root.

        ``--output-dir`` changes must never trigger re-downloads: every
        already-verified canonical file is simply re-linked (or copied)
        into the new task directory.
        """
        for row in self.store.links_for_task(task.task_id):
            frec = self.store.get_file(row["paper_uid"])
            if not frec or frec["status"] != "ok":
                continue
            canonical = Path(frec["canonical_path"] or "")
            if not canonical.exists():
                continue
            dst = task_dir(self.root, task.output_subdir) / frec["filename"]
            if str(dst) != row["link_path"] or not dst.exists():
                try:
                    link_or_copy(canonical, dst)
                    self.store.save_link(task.task_id, row["paper_uid"], str(dst))
                except OSError as e:
                    log.debug("rearchive %s: %s", row["paper_uid"], e)

    def stored_task(self, task_id: str) -> CollectionTask | None:
        """Rebuild a task from its stored JSON blob (status-only commands)."""
        blob = self.store.task_blob(task_id)
        return _task_from(blob) if blob else None
