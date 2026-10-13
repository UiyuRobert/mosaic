"""Persistent download queue and scheduler.

``DownloadManager`` consumes rows from the ``download_jobs`` table
(see :mod:`mosaic.db`) with two interchangeable backends:

- ``native`` — MOSAIC's own HTTP pipeline (temp file → verify → rename)
  executed in a thread pool.
- ``aria2`` — a local aria2 daemon driven over JSON-RPC.

The scheduler enforces a global concurrency cap, a per-host concurrency
cap and a minimum start-interval per host, independent of any per-file
limits aria2 applies internally. Jobs survive application restarts: on
startup, non-terminal jobs are reconciled back into the queue.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from mosaic.aria2 import Aria2Client, Aria2Error, ensure_client
from mosaic.db import Cache
from mosaic.downloader import (
    _candidates_for,
    _destination_for,
    _fetch,
    _is_doi_url,
    _quick_pdf_check,
    _try_browser_download,
    download_still_valid,
    host_of,
)
from mosaic.errors import TRANSIENT_REASONS, DownloadError
from mosaic.models import Paper
from mosaic.pdfcheck import PdfVerificationError, sha256_file, verify_pdf

log = logging.getLogger(__name__)

TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
ACTIVE = frozenset({"QUEUED", "DOWNLOADING", "VERIFYING", "RETRY_WAIT"})

_BACKOFF_BASE = 5.0
_BACKOFF_MAX = 300.0

# aria2 errorCode → DownloadError-ish reason (best effort; errorMessage
# containing an HTTP status takes precedence when parseable).
_HOST_FAIL_MAX = 3

_ARIA2_REASONS = {
    2: "timeout",
    3: "not_found",
    9: "disk_full",
    13: "exists",
    19: "dns_error",
    24: "access_denied",
    27: "access_denied",
    28: "access_denied",
}


def _aria2_reason(code: int, message: str) -> str:
    msg = (message or "").lower()
    for needle, reason in (
        ("404", "not_found"),
        ("403", "access_denied"),
        ("401", "access_denied"),
        ("429", "rate_limited"),
        ("resolve", "dns_error"),
        ("timeout", "timeout"),
        ("space", "disk_full"),
    ):
        if needle in msg:
            return reason
    return _ARIA2_REASONS.get(code, "unreachable")


class DownloadManager:
    """Schedules and supervises persistent download jobs."""

    def __init__(self, cache: Cache, cfg: dict) -> None:
        self.cache = cache
        self.cfg = cfg
        self.dl_cfg = cfg.get("download", {})
        self.max_concurrent = int(self.dl_cfg.get("max_concurrent", 4))
        self.per_host = int(self.dl_cfg.get("per_host_concurrent", 1))
        self.per_host_delay = float(self.dl_cfg.get("per_host_delay", 1.5))
        self.max_tries = int(self.dl_cfg.get("max_tries", 3))

        self.backend = self._resolve_backend()
        self._aria2: Aria2Client | None = None
        self._executor = ThreadPoolExecutor(max_workers=self.max_concurrent)
        # Consecutive-failure counter per host — a tarpitting or bot-blocking
        # host is skipped after _HOST_FAIL_MAX failures so it cannot stall the
        # whole queue. Reset on any successful fetch from that host.
        self._host_fails: dict[str, int] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._paused_jobs: set[str] = set()  # user-pause markers (native abort)
        self._cancelled_jobs: set[str] = set()
        self._inflight: dict[str, Future | None] = {}
        # job_id -> (last_completed_bytes, last_change_ts) for stall detection
        self._progress: dict[str, tuple[int, float]] = {}
        self._host_active: dict[str, int] = {}
        self._host_last: dict[str, float] = {}
        self._thread: threading.Thread | None = None
        self._managed_shutdown = False

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _resolve_backend(self) -> str:
        backend = str(self.dl_cfg.get("backend", "auto")).lower()
        if backend not in ("native", "aria2", "auto"):
            log.warning("Unknown download.backend %r — using 'auto'", backend)
            backend = "auto"
        return backend

    def start(self) -> None:
        """Reconcile leftover state and start the scheduler thread."""
        self._reconcile()
        if self.backend in ("aria2", "auto") and self._aria2 is None:
            self._aria2 = ensure_client(
                self.cfg, state_dir=Path(self.cfg.get("db_path", ".")).parent
            )
            if self._aria2 is None and self.backend == "aria2":
                if self.dl_cfg.get("aria2_fallback_native"):
                    log.warning("aria2 unavailable — falling back to native backend")
                    self.backend = "native"
                else:
                    log.warning(
                        "aria2 backend requested but unavailable. Install aria2 "
                        "(e.g. 'brew install aria2' / 'apt install aria2') or set "
                        "download.backend=native"
                    )
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="mosaic-dlqueue", daemon=True)
            self._thread.start()

    def shutdown(self, wait: bool = False) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._executor.shutdown(wait=wait, cancel_futures=not wait)
        if self._aria2 and self._aria2.managed and not self._managed_shutdown:
            self._aria2.shutdown()
            self._managed_shutdown = True

    def update_cfg(self, cfg: dict) -> None:
        """Refresh live configuration (called after the user saves settings)."""
        self.cfg = cfg
        self.dl_cfg = cfg.get("download", {})
        self.max_concurrent = int(self.dl_cfg.get("max_concurrent", 4))
        self.per_host = int(self.dl_cfg.get("per_host_concurrent", 1))
        self.per_host_delay = float(self.dl_cfg.get("per_host_delay", 1.5))
        self.max_tries = int(self.dl_cfg.get("max_tries", 3))
        self.backend = self._resolve_backend()

    def _reconcile(self) -> None:
        """Requeue jobs left in non-terminal states by a previous run.

        aria2 jobs keep their ``aria2_gid``: the daemon often outlives the
        MOSAIC process, so the transfer may still be running — ``_start_aria2``
        re-attaches to a live GID instead of double-submitting the same URL
        to the same ``.part`` file.
        """
        for job in self.cache.list_jobs(status=list(ACTIVE), limit=10_000):
            keep_gid = job.get("backend") == "aria2" and job.get("aria2_gid")
            self.cache.update_job(
                job["job_id"],
                status="QUEUED",
                aria2_gid=job.get("aria2_gid") if keep_gid else None,
                last_error=(job.get("last_error") or "") + " [requeued on startup]",
            )

    # ── Enqueue ──────────────────────────────────────────────────────────────

    def enqueue_papers(
        self,
        papers: list[Paper],
        *,
        download_dir: str | None = None,
        filename_pattern: str | None = None,
        dest_fn: Callable[[Paper], Path] | None = None,
    ) -> list[str]:
        """Create QUEUED jobs for *papers*; returns job_ids.

        Papers are persisted first so the queue survives restarts and alias
        resolution works. A paper already in-flight — under this or an alias
        UID — is not enqueued again; its existing job_id is returned instead,
        so a second click "joins" the running download rather than
        duplicating it. Papers with a verified prior download are skipped
        entirely (no job created) — matching the ``--download`` skip logic.
        A partial unique index on ``download_jobs(paper_uid)`` makes the
        one-active-job guarantee hold across processes, not just threads.

        Args:
            download_dir: Per-call override for the destination directory
                (collection plans download into their own library without
                mutating the shared config).
            filename_pattern: Per-call override for the filename pattern.
            dest_fn: Optional callable returning the exact destination
                path per paper — takes precedence over
                ``_destination_for`` so callers can apply their own naming
                and collision policy.
        """
        dl_dir = Path(download_dir or self.cfg["download_dir"])
        pattern = filename_pattern or self.cfg.get(
            "filename_pattern", "{year}_{source}_{author}_{title}"
        )
        skip_existing = self.dl_cfg.get("skip_existing", True)
        # Resolve the backend once per batch so job stamps match reality.
        if self.backend in ("aria2", "auto"):
            self._ensure_aria2()
        # Snapshot active jobs once for the whole batch: alias-aware dedup
        # and destination reservation both read this set.
        active = self.cache.list_jobs(status=[*ACTIVE, "PAUSED"], limit=10_000)
        active_by_uid: dict[str, dict] = {}
        reserved_dests: set[str] = set()
        for j in active:
            active_by_uid.setdefault(j["paper_uid"], j)
            if j.get("destination"):
                reserved_dests.add(j["destination"])
        job_ids: list[str] = []
        for paper in papers:
            self.cache.save(paper)
            canonical = self.cache.resolve_uid(paper.uid)
            hit_uids = {canonical} | {self.cache.resolve_uid(ident) for ident in paper.identifiers}
            existing = next((active_by_uid[u] for u in hit_uids if u in active_by_uid), None)
            if existing is not None:
                job_ids.append(existing["job_id"])
                continue
            if skip_existing:
                rec = self.cache.get_download(paper.uid)
                if rec and rec["status"] == "ok" and download_still_valid(rec):
                    continue
            job_id = uuid.uuid4().hex[:16]
            dest = (
                Path(dest_fn(paper))
                if dest_fn is not None
                else (_destination_for(paper, dl_dir, pattern))
            )
            if str(dest) in reserved_dests:
                # Reserved by another *queued* job (nothing on disk yet, so
                # dedupe_path can't see it) — generate a __N variant ourselves.
                for i in range(2, 1000):
                    candidate = dest.with_name(f"{dest.stem}__{i}{dest.suffix}")
                    if str(candidate) not in reserved_dests and not candidate.exists():
                        dest = candidate
                        break
            reserved_dests.add(str(dest))
            # Cheap candidates only (source-supplied URLs). Unpaywall
            # resolution is deferred to the worker so enqueue stays fast.
            candidates = self._cheap_candidates(paper)
            first = next(
                (
                    c.download_url
                    for c in candidates
                    if not self._host_cooled(host_of(c.download_url))
                ),
                None,
            )
            try:
                self.cache.enqueue_job(
                    {
                        "job_id": job_id,
                        "paper_uid": canonical,
                        "url": first,
                        "backend": self._effective_backend(),
                        "destination": str(dest),
                        "host": host_of(first) if first else "",
                        "candidates_json": json.dumps([c.to_dict() for c in candidates]),
                    }
                )
            except sqlite3.IntegrityError:
                # Another process won the enqueue race — join its job.
                winner = self.cache.active_job_for_uids({canonical})
                if winner is not None:
                    job_ids.append(winner["job_id"])
                continue
            active_by_uid[canonical] = {"job_id": job_id, "paper_uid": canonical}
            job_ids.append(job_id)
        return job_ids

    @staticmethod
    def _cheap_candidates(paper: Paper) -> list:
        """Source-supplied candidates without any network resolution."""
        from mosaic.models import PDFCandidate

        out: list[PDFCandidate] = []
        seen: set[str] = set()
        for raw in paper.pdf_candidates:
            try:
                c = PDFCandidate.from_dict({k: v for k, v in raw.items() if k != "filename"})
            except TypeError:
                continue
            if c.download_url and c.download_url not in seen:
                seen.add(c.download_url)
                if _is_doi_url(c.download_url):
                    continue
                out.append(c)
        if paper.pdf_url and paper.pdf_url not in seen and not _is_doi_url(paper.pdf_url):
            out.append(
                PDFCandidate(
                    paper_uid=paper.uid,
                    download_url=paper.pdf_url,
                    landing_url=paper.url or "",
                    source=paper.source or "source",
                    confidence=0.85,
                )
            )
        out.sort(key=lambda c: c.confidence, reverse=True)
        return out

    def _effective_backend(self) -> str:
        """Report the backend that would be used now — read-only, no
        side effects (does not try to connect or spawn aria2)."""
        if self.backend == "auto":
            return "aria2" if self._aria2 is not None else "native"
        return self.backend

    @property
    def aria2_available(self) -> bool:
        """True after a connect attempt left a usable aria2 RPC client."""
        return self._aria2 is not None

    def _ensure_aria2(self) -> Aria2Client | None:
        """Lazily connect to / spawn the daemon when a backend needs it."""
        if self._aria2 is None:
            self._aria2 = ensure_client(
                self.cfg, state_dir=Path(self.cfg.get("db_path", ".")).parent
            )
        return self._aria2

    # ── Control ───────────────────────────────────────────────────────────────

    def pause(self, job_id: str) -> bool:
        job = self.cache.get_job(job_id)
        if not job or job["status"] in TERMINAL or job["status"] == "PAUSED":
            return False
        with self._lock:
            self._paused_jobs.add(job_id)
        if job["backend"] == "aria2" and job.get("aria2_gid") and self._aria2:
            try:
                self._aria2.pause(job["aria2_gid"])
            except Aria2Error as e:
                log.debug("aria2 pause failed for %s: %s", job_id, e)
        self.cache.update_job(job_id, status="PAUSED")
        return True

    def resume(self, job_id: str) -> bool:
        job = self.cache.get_job(job_id)
        if not job or job["status"] != "PAUSED":
            return False
        with self._lock:
            self._paused_jobs.discard(job_id)
        if job["backend"] == "aria2" and job.get("aria2_gid") and self._aria2:
            try:
                self._aria2.unpause(job["aria2_gid"])
                self.cache.update_job(job_id, status="DOWNLOADING")
                return True
            except Aria2Error as e:
                log.debug("aria2 unpause failed for %s: %s", job_id, e)
        # Requeue for a fresh dispatch — drop the stale inflight entry so the
        # re-dispatch does not double-count this host's concurrency slot.
        with self._lock:
            inflight = self._inflight.pop(job_id, {}) or {}
        self._unmark_host(inflight.get("host") or job.get("host") or "")
        self.cache.update_job(job_id, status="QUEUED")
        return True

    def cancel(self, job_id: str) -> bool:
        job = self.cache.get_job(job_id)
        if not job or job["status"] in TERMINAL:
            return False
        with self._lock:
            self._cancelled_jobs.add(job_id)
            self._paused_jobs.discard(job_id)
            inflight = self._inflight.pop(job_id, {}) or {}
        # Release the concurrency slot and per-host allowance — otherwise each
        # cancel leaks an _inflight entry and the scheduler eventually refuses
        # to dispatch anything (active_count >= max_concurrent forever).
        self._unmark_host(inflight.get("host") or job.get("host") or "")
        if job["backend"] == "aria2" and job.get("aria2_gid") and self._aria2:
            self._aria2.remove(job["aria2_gid"])
        self._cleanup_partials(job)
        self.cache.update_job(job_id, status="CANCELLED")
        return True

    def retry(self, job_id: str) -> bool:
        job = self.cache.get_job(job_id)
        if not job or job["status"] not in TERMINAL | {"PAUSED"}:
            return False
        self.cache.update_job(
            job_id, status="QUEUED", retry_count=0, last_error="", next_retry_at=None
        )
        return True

    def pause_all(self) -> int:
        n = 0
        for job in self.cache.list_jobs(status=["QUEUED", "DOWNLOADING"], limit=10_000):
            if self.pause(job["job_id"]):
                n += 1
        return n

    def resume_all(self) -> int:
        n = 0
        for job in self.cache.list_jobs(status="PAUSED", limit=10_000):
            if self.resume(job["job_id"]):
                n += 1
        return n

    def retry_failed(self) -> int:
        n = 0
        for job in self.cache.list_jobs(status="FAILED", limit=10_000):
            if self.retry(job["job_id"]):
                n += 1
        return n

    def clear(self, statuses: list[str]) -> int:
        """Remove job records; downloaded PDFs are never touched."""
        return self.cache.clear_jobs(statuses)

    def _cleanup_partials(self, job: dict) -> None:
        dest = job.get("destination") or ""
        for suffix in (".part", ".aria2"):
            try:
                Path(dest + suffix).unlink(missing_ok=True)
            except OSError:
                pass

    # ── Scheduler ─────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.debug("download scheduler tick failed", exc_info=True)
            self._stop.wait(0.7)

    def _tick(self) -> None:
        now = time.time()

        # Re-arm jobs whose backoff expired.
        for job in self.cache.list_jobs(status="RETRY_WAIT", limit=10_000):
            nra = job.get("next_retry_at") or 0
            if nra <= now:
                self.cache.update_job(job["job_id"], status="QUEUED")

        # Poll aria2 jobs for progress/completion.
        self._poll_aria2_jobs()

        # Dispatch QUEUED jobs within concurrency/host limits.
        queued = self.cache.list_jobs(status="QUEUED", limit=10_000)
        queued.sort(key=lambda j: j["created_at"])  # FIFO
        for job in queued:
            if self._stop.is_set():
                return
            if self._active_count() >= self.max_concurrent:
                return
            self._dispatch(job)

    def _host_cooled(self, host: str) -> bool:
        return bool(host) and self._host_fails.get(host, 0) >= _HOST_FAIL_MAX

    def _note_host_fail(self, host: str) -> None:
        if host:
            self._host_fails[host] = self._host_fails.get(host, 0) + 1

    def _note_host_ok(self, host: str) -> None:
        if host:
            self._host_fails.pop(host, None)

    def _active_count(self) -> int:
        with self._lock:
            return len(self._inflight)

    def _host_allows(self, host: str) -> bool:
        if not host:
            return True
        with self._lock:
            if self._host_active.get(host, 0) >= self.per_host:
                return False
            last = self._host_last.get(host, 0.0)
        return time.time() - last >= self.per_host_delay

    def _mark_host_start(self, host: str) -> None:
        with self._lock:
            self._host_active[host] = self._host_active.get(host, 0) + 1
            self._host_last[host] = time.time()

    def _unmark_host(self, host: str) -> None:
        with self._lock:
            n = self._host_active.get(host, 0) - 1
            if n <= 0:
                self._host_active.pop(host, None)
            else:
                self._host_active[host] = n

    def _dispatch(self, job: dict) -> None:
        host = job.get("host") or ""
        if not self._host_allows(host):
            return
        backend = job.get("backend") or "native"
        if backend == "aria2":
            if self._aria2 is None or not self._aria2.is_alive():
                self._ensure_aria2()
            if self._aria2 is None:
                if self.dl_cfg.get("aria2_fallback_native"):
                    backend = "native"
                    self.cache.update_job(job["job_id"], backend="native")
                else:
                    self._fail(job, "unreachable", "aria2 daemon unavailable")
                    return
            else:
                self._start_aria2(job)
                return
        self._start_native(job, backend=backend)

    # ── Native backend ─────────────────────────────────────────────────────────

    def _start_native(self, job: dict, backend: str = "native") -> None:
        job_id = job["job_id"]
        self.cache.update_job(job_id, status="DOWNLOADING", backend=backend)
        self._mark_host_start(job.get("host") or "")
        future = self._executor.submit(self._run_native, dict(job))
        with self._lock:
            self._inflight[job_id] = {"future": future, "host": job.get("host") or ""}
        future.add_done_callback(lambda f, jid=job_id: self._native_done(jid, f))

    def _run_native(self, job: dict) -> tuple[str, str]:
        """Blocking pipeline: returns (dest_path, final_url)."""
        job_id = job["job_id"]
        paper = self.cache.get_by_uid(job["paper_uid"])
        if paper is None:
            raise DownloadError("not_found", "paper no longer in cache")

        dest = Path(job["destination"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dl_cfg = self.dl_cfg
        max_bytes = int(dl_cfg.get("max_file_mb", 300)) * 1024 * 1024
        verify = dl_cfg.get("verify_pdf", True)
        allow_private = dl_cfg.get("allow_private_hosts", False)

        # Destination already satisfied by a valid file → skip download.
        if dest.exists() and _quick_pdf_check(dest):
            return str(dest), ""

        candidates = self._job_candidates(job, paper)
        # Native workers retry within one attempt — start from the job's
        # current URL so the last-tried candidate is hit again first.
        current = job.get("url")
        if current:
            candidates.sort(key=lambda c: 0 if c.download_url == current else 1)

        last_p = [0.0, 0]

        def _progress(done: int, total: int) -> None:
            # Throttle DB writes — at most every ~64 KB or 500 ms.
            now = time.time()
            if done - last_p[1] >= 65536 or now - last_p[0] >= 0.5:
                last_p[0], last_p[1] = now, done
                self.cache.update_job(job_id, downloaded_bytes=done, total_bytes=total or None)

        def _abort() -> None:
            with self._lock:
                if job_id in self._cancelled_jobs:
                    raise DownloadError("cancelled", "cancelled by user")
                if job_id in self._paused_jobs:
                    raise DownloadError("cancelled", "paused by user")

        last: DownloadError = DownloadError(
            "no_candidates",
            "no downloadable PDF candidates (no public URL and no OA version found)",
        )
        for cand in candidates:
            _abort()
            try:
                final = _fetch(
                    cand.download_url,
                    str(dest),
                    max_bytes=max_bytes,
                    verify=verify,
                    progress=_progress,
                    abort_check=_abort,
                    allow_private=allow_private,
                )
                return str(dest), final
            except DownloadError as e:
                last = e
                if e.reason == "cancelled":
                    raise
                continue
        # Last resort — parity with the old sequential pipeline: a saved
        # authenticated browser session can reach paywalled PDFs no plain
        # URL candidate can.
        if _try_browser_download(paper, dest):
            return str(dest), ""
        raise last

    def _native_done(self, job_id: str, future: Future) -> None:
        job = self.cache.get_job(job_id) or {}
        with self._lock:
            inflight = self._inflight.pop(job_id, {}) or {}
        self._unmark_host(inflight.get("host") or job.get("host") or "")
        try:
            dest, final_url = future.result()
        except DownloadError as e:
            if e.reason == "cancelled":
                self._after_cancel(job_id)
            else:
                self._handle_failure(job, e)
            return
        except Exception as e:
            self._handle_failure(job, DownloadError("unreachable", str(e)))
            return

        # _fetch already verified + renamed; record final state.
        self._record_complete(job.get("paper_uid", ""), dest)
        self.cache.update_job(
            job_id,
            status="COMPLETED",
            url=final_url or job.get("url"),
            downloaded_bytes=Path(dest).stat().st_size if Path(dest).exists() else None,
        )

    def _after_cancel(self, job_id: str) -> None:
        with self._lock:
            cancelled = job_id in self._cancelled_jobs
            paused = job_id in self._paused_jobs
        if cancelled:
            self.cache.update_job(job_id, status="CANCELLED")
        elif paused:
            self.cache.update_job(job_id, status="PAUSED")
        else:
            self.cache.update_job(job_id, status="QUEUED")
        with self._lock:
            self._cancelled_jobs.discard(job_id)

    # ── aria2 backend ───────────────────────────────────────────────────────────

    def _start_aria2(self, job: dict) -> None:
        job_id = job["job_id"]
        assert self._aria2 is not None
        dest = Path(job["destination"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and _quick_pdf_check(dest):
            self._complete(job, str(dest))
            return

        # Re-attach to an existing gid when the job was requeued after a
        # restart or pause — avoids duplicating the download in the daemon.
        old_gid = job.get("aria2_gid")
        if old_gid:
            try:
                st = self._aria2.tell_status(old_gid)
            except Aria2Error:
                st = None  # gid forgotten by daemon — fall through to addUri
            if st is not None:
                status = st.get("status")
                if status in ("active", "waiting", "paused"):
                    self._mark_host_start(job.get("host") or "")
                    with self._lock:
                        self._inflight[job_id] = {
                            "gid": old_gid,
                            "host": job.get("host") or "",
                        }
                    self.cache.update_job(job_id, status="DOWNLOADING")
                    if status == "paused":
                        try:
                            self._aria2.unpause(old_gid)
                        except Aria2Error:
                            pass
                    return
                if status == "complete":
                    # The daemon finished while we were away — verify the
                    # .part file and archive it, don't fetch again.
                    self._mark_host_start(job.get("host") or "")
                    with self._lock:
                        self._inflight[job_id] = {
                            "gid": old_gid,
                            "host": job.get("host") or "",
                        }
                    self.cache.update_job(job_id, status="DOWNLOADING")
                    self._finish_aria2(job, st)
                    return
                # error / removed / unknown → stale gid; clear and resubmit.
                self.cache.update_job(job_id, aria2_gid=None)

        url = job.get("url")
        if not url:
            self._handle_failure(job, DownloadError("not_found", "no download URL"))
            return
        options = {
            "dir": str(dest.parent),
            "out": dest.name + ".part",
            "continue": "true",
            "max-connection-per-server": str(self.dl_cfg.get("max_connections", 1)),
            "split": str(self.dl_cfg.get("max_split", 1)),
            "max-tries": "3",
            "retry-wait": "2",
            # Hard per-attempt caps — without them a tarpitted host (slow
            # drip-feed instead of a clean error) can hold a queue slot for
            # many minutes and starve the job's viable candidates.
            "timeout": "45",
            "connect-timeout": "15",
            "user-agent": "MOSAIC/1.5 (literature search)",
        }
        limit = self.dl_cfg.get("aria2_max_download_limit", "")
        if limit:
            options["max-download-limit"] = str(limit)
        try:
            gid = self._aria2.add_uri(url, options)
        except Aria2Error as e:
            self._handle_failure(job, DownloadError("unreachable", str(e)))
            return
        self._mark_host_start(job.get("host") or "")
        with self._lock:
            self._inflight[job_id] = {"gid": gid, "host": job.get("host") or ""}
        self.cache.update_job(job_id, status="DOWNLOADING", aria2_gid=gid)

    def _poll_aria2_jobs(self) -> None:
        if self._aria2 is None:
            return
        jobs = self.cache.list_jobs(status="DOWNLOADING", limit=10_000)
        for job in jobs:
            if job.get("backend") != "aria2" or not job.get("aria2_gid"):
                continue
            try:
                st = self._aria2.tell_status(job["aria2_gid"])
            except Aria2Error as e:
                if "not found" in str(e).lower():
                    # The daemon forgot this GID (restart without session,
                    # purged result). Requeue so the download restarts —
                    # continue=true plus any .part file resumes bytes.
                    lost_id = job["job_id"]
                    self._progress.pop(lost_id, None)
                    with self._lock:
                        inflight = self._inflight.pop(lost_id, {}) or {}
                    self._unmark_host(inflight.get("host") or job.get("host") or "")
                    self.cache.update_job(
                        lost_id,
                        status="QUEUED",
                        aria2_gid=None,
                        last_error=(job.get("last_error") or "") + " [gid lost — requeued]",
                    )
                else:
                    log.debug("tellStatus failed for %s: %s", job["job_id"], e)
                continue
            status = st.get("status")
            job_id = job["job_id"]
            if status in ("active", "waiting"):
                nbytes = int(st.get("completedLength") or 0)
                total = int(st.get("totalLength") or 0)
                # aria2 has no per-file size cap — enforce max_file_mb here
                # so an oversized/aborted-blob transfer cannot fill the disk.
                max_bytes = int(self.dl_cfg.get("max_file_mb", 300)) * 1024 * 1024
                if (total and total > max_bytes) or nbytes > max_bytes:
                    log.warning(
                        "job %s exceeds max_file_mb (%d > %d bytes) — aborting gid",
                        job_id,
                        total or nbytes,
                        max_bytes,
                    )
                    try:
                        self._aria2.remove(job["aria2_gid"])
                    except Exception:
                        pass
                    with self._lock:
                        inflight = self._inflight.pop(job_id, {}) or {}
                    self._unmark_host(inflight.get("host") or job.get("host") or "")
                    self._progress.pop(job_id, None)
                    self._handle_failure(
                        job,
                        DownloadError(
                            "too_large",
                            f"file exceeds {max_bytes} byte limit",
                        ),
                    )
                    continue
                prev_bytes, _t = self._progress.get(job_id, (-1, 0.0))
                if nbytes != prev_bytes:
                    self._progress[job_id] = (nbytes, time.time())
                elif time.time() - self._progress.get(job_id, (0, time.time()))[1] > float(
                    self.dl_cfg.get("stall_timeout", 240)
                ):
                    # Transfer holds a slot but moves no bytes (dead host
                    # trickling, connect-phase hang aria2 doesn't flag).
                    # Kill the gid and treat it as a failed attempt so the
                    # queue can advance candidates or give up cleanly.
                    log.warning("job %s stalled at %d bytes — forcing failure", job_id, nbytes)
                    try:
                        self._aria2.remove(job["aria2_gid"])
                    except Exception:
                        pass
                    with self._lock:
                        inflight = self._inflight.pop(job_id, {}) or {}
                    self._unmark_host(inflight.get("host") or job.get("host") or "")
                    self._progress.pop(job_id, None)
                    self._handle_failure(
                        job,
                        DownloadError("timeout", "no download progress — stalled transfer"),
                    )
                    continue
                self.cache.update_job(
                    job_id,
                    downloaded_bytes=nbytes,
                    total_bytes=int(st.get("totalLength") or 0) or None,
                )
            elif status == "paused":
                if job["status"] != "PAUSED":
                    self.cache.update_job(job_id, status="PAUSED")
            elif status == "complete":
                self._finish_aria2(job, st)
            elif status == "error":
                with self._lock:
                    inflight = self._inflight.pop(job_id, {}) or {}
                self._unmark_host(inflight.get("host") or job.get("host") or "")
                self._progress.pop(job_id, None)
                reason = _aria2_reason(int(st.get("errorCode") or 0), st.get("errorMessage") or "")
                self._handle_failure(job, DownloadError(reason, st.get("errorMessage") or reason))
            elif status == "removed":
                with self._lock:
                    inflight = self._inflight.pop(job_id, {}) or {}
                self._progress.pop(job_id, None)
                self._unmark_host(inflight.get("host") or job.get("host") or "")
                if job["status"] not in ("CANCELLED", "PAUSED"):
                    self.cache.update_job(job_id, status="CANCELLED")

    def _finish_aria2(self, job: dict, st: dict) -> None:
        """Verify the completed .part file then atomically rename it."""
        job_id = job["job_id"]
        with self._lock:
            inflight = self._inflight.pop(job_id, {}) or {}
        self._unmark_host(inflight.get("host") or job.get("host") or "")
        self._progress.pop(job_id, None)
        self.cache.update_job(job_id, status="VERIFYING")
        dest = Path(job["destination"])
        part = Path(str(dest) + ".part")
        try:
            if not part.exists():
                # aria2 may have renamed it already when 'continue' combined
                # with a finished download — accept a valid final file too.
                if dest.exists() and _quick_pdf_check(dest):
                    self._complete(job, str(dest))
                    return
                raise DownloadError("not_found", "download finished but file missing")
            if self.dl_cfg.get("verify_pdf", True):
                verify_pdf(part)
            os.replace(part, dest)
            self._complete(job, str(dest))
        except PdfVerificationError as e:
            part.unlink(missing_ok=True)
            Path(str(dest) + ".aria2").unlink(missing_ok=True)
            self._handle_failure(job, DownloadError(e.reason, str(e)))
        except DownloadError as e:
            self._handle_failure(job, e)

    def _record_complete(self, paper_uid: str, dest: str) -> None:
        """Store the completed download with its integrity fingerprint."""
        path = Path(dest)
        try:
            self.cache.set_download(
                paper_uid,
                dest,
                "ok",
                size_bytes=path.stat().st_size,
                sha256=sha256_file(path),
            )
        except OSError:
            self.cache.set_download(paper_uid, dest, "ok")

    def _complete(self, job: dict, dest: str) -> None:
        self._note_host_ok(job.get("host") or host_of(job.get("url") or ""))
        self._record_complete(job.get("paper_uid", ""), dest)
        self.cache.update_job(
            job["job_id"],
            status="COMPLETED",
            downloaded_bytes=Path(dest).stat().st_size if Path(dest).exists() else None,
        )

    # ── Candidates / failure policy ────────────────────────────────────────────

    def _job_candidates(self, job: dict, paper: Paper) -> list:
        from mosaic.models import PDFCandidate

        stored: list[PDFCandidate] = []
        try:
            raw = json.loads(job.get("candidates_json") or "[]")
            stored = [
                PDFCandidate.from_dict({k: v for k, v in c.items() if k != "filename"}) for c in raw
            ]
            # Jobs queued before the doi.org filter may still carry DOI
            # redirect URLs — drop them here too so old lists don't revive.
            stored = [c for c in stored if not _is_doi_url(c.download_url)]
        except (TypeError, ValueError):
            stored = []
        # Merge in deferred resolution (Unpaywall alternates for DOI
        # papers) — deduplicated by URL, stored candidates keep priority.
        known = {c.download_url for c in stored}
        for c in _candidates_for(paper, self.cfg.get("unpaywall", {}).get("email", "")):
            if c.download_url not in known:
                known.add(c.download_url)
                stored.append(c)
        # NOTE: the list must keep a *stable* order — ``_handle_failure``
        # locates the job's current URL inside it to advance to the next
        # untried candidate. Reordering here (e.g. current-first) makes
        # every position lookup return index 0 and the advance loop
        # ping-pong forever between two URLs without consuming retries.
        return stored

    def _handle_failure(self, job: dict, err: DownloadError) -> None:
        job_id = job["job_id"]
        retry_count = int(job.get("retry_count") or 0)
        if err.reason != "cancelled":
            self._note_host_fail(job.get("host") or "")

        # Advance to the next untried candidate before consuming a retry —
        # only meaningful for aria2 (one URL per attempt); the native worker
        # already walks the full candidate list inside a single attempt.
        # The full list is recomputed so deferred candidates (Unpaywall)
        # are reached after the stored ones are exhausted. Candidates on
        # hosts past the failure breaker are skipped outright.
        candidates: list = []
        if job.get("backend") == "aria2" and err.reason != "cancelled":
            paper = self.cache.get_by_uid(job.get("paper_uid", ""))
            if paper is not None:
                candidates = self._job_candidates(job, paper)
            urls = [c.download_url for c in candidates]
            cur = job.get("url")
            idx = urls.index(cur) if cur in urls else -1
            nxt = next(
                (u for u in urls[idx + 1 :] if not self._host_cooled(host_of(u))),
                None,
            )
            if nxt is not None:
                self.cache.update_job(
                    job_id,
                    url=nxt,
                    host=host_of(nxt),
                    candidates_json=json.dumps([c.to_dict() for c in candidates]),
                    status="QUEUED",
                    last_error=f"{err.reason}: {err}",
                )
                return

        if (
            err.reason in TRANSIENT_REASONS
            or err.reason in ("html_response", "invalid_pdf", "missing_eof", "unparseable")
        ) and retry_count + 1 < self.max_tries:
            base = float(self.dl_cfg.get("retry_backoff_base", _BACKOFF_BASE))
            delay = min(
                _BACKOFF_MAX,
                (err.retry_after if err.retry_after else base * 2**retry_count),
            )
            self.cache.update_job(
                job_id,
                status="RETRY_WAIT",
                retry_count=retry_count + 1,
                last_error=f"{err.reason}: {err}",
                next_retry_at=time.time() + delay,
                # reset to the first candidate for the next attempt round
                url=(candidates[0].download_url if candidates else job.get("url")),
            )
            return
        self._fail(job, err.reason, str(err))

    def _fail(self, job: dict, reason: str, detail: str) -> None:
        self.cache.update_job(job["job_id"], status="FAILED", last_error=f"{reason}: {detail}")
        self.cache.set_download(job.get("paper_uid", ""), "", f"error: {reason}")

    # ── Synchronous wait (CLI) ─────────────────────────────────────────────────

    def wait_all(
        self,
        job_ids: list[str],
        *,
        poll: float = 0.5,
        progress_cb: object = None,
        timeout: float = 0,
    ) -> dict[str, str]:
        """Block until all *job_ids* reach a terminal state.

        Args:
            timeout: optional wall-clock bound in seconds. ``0`` (default)
                waits forever — callers that cannot block indefinitely
                should pass a deadline and treat still-pending jobs as
                timed out.

        Returns:
            ``{paper_uid: local_path}`` for COMPLETED jobs.
        """
        pending = set(job_ids)
        deadline = time.monotonic() + timeout if timeout else None
        while pending:
            for job_id in list(pending):
                job = self.cache.get_job(job_id)
                if job is None or job["status"] in TERMINAL:
                    pending.discard(job_id)
                    if progress_cb and job:
                        progress_cb(job)
            if pending:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                time.sleep(poll)
        result: dict[str, str] = {}
        for job_id in job_ids:
            job = self.cache.get_job(job_id)
            if job and job["status"] == "COMPLETED" and job.get("destination"):
                result[job["paper_uid"]] = job["destination"]
        return result


# ---------------------------------------------------------------------------
# Manager registry — one DownloadManager per cache/DB file, shared by CLI & UI
# ---------------------------------------------------------------------------

_MANAGERS: dict[str, DownloadManager] = {}
_MANAGERS_LOCK = threading.Lock()


def get_manager(cfg: dict, cache: Cache) -> DownloadManager:
    """Return the shared DownloadManager for the given cache's database."""
    key = getattr(cache, "_db_path", "") or ":memory:"
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(key)
        if manager is None:
            manager = DownloadManager(cache, cfg)
            manager.start()
            _MANAGERS[key] = manager
        else:
            manager.update_cfg(cfg)
        return manager


def shutdown_managers() -> None:
    """Stop all managers (used on application exit)."""
    with _MANAGERS_LOCK:
        for manager in _MANAGERS.values():
            manager.shutdown()
        _MANAGERS.clear()
