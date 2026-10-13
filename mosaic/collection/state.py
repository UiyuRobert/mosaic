"""Persistent collection-plan state — lives inside the main cache.db.

Tables (schema v4, see :mod:`mosaic.db` migrations):

- ``collection_plans``   — plan registry (plan_id, path, content hash)
- ``collection_tasks``   — per-task status/counters + original task JSON
- ``collection_queries`` — every query executed (tier, source, result)
- ``collection_matches`` — task ↔ paper links with relevance grade
- ``collection_files``   — canonical library files per paper (path+sha256)
- ``collection_links``   — task-dir files (hardlink/copy of canonical)
- ``collection_source_health`` — per-plan source circuit breaker

The store serialises through the *Cache's* lock — both share one SQLite
connection, so a single lock protects every writer.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime

from mosaic.db import Cache

# Task lifecycle — PENDING→SEARCHING→FOUND→DOWNLOADING→COMPLETED/PARTIAL.
TASK_STATUSES = (
    "PENDING",
    "SEARCHING",
    "FOUND",
    "DOWNLOADING",
    "COMPLETED",
    "PARTIAL",
    "METADATA_ONLY",
    "FAILED",
    "NEEDS_REVIEW",
)

# Reason codes (mirrors the acceptance failure taxonomy).
REASONS = (
    "NO_RELEVANT_RESULT",
    "NO_PUBLIC_PDF",
    "ACCESS_RESTRICTED",
    "SOURCE_BLOCKED",
    "RATE_LIMITED",
    "DOWNLOAD_FAILED",
    "PDF_INVALID",
    "PDF_MISMATCH",
    "DISK_FULL",
    "OK",
)

# Statuses that count as "successfully finished" (skip on resume).
DONE_STATUSES = {"COMPLETED"}
# Statuses retried by `collect retry --failed`.
FAILED_STATUSES = {
    "FAILED",
    "NEEDS_REVIEW",
    "PARTIAL",
    "METADATA_ONLY",
    "NO_RELEVANT_RESULT",
    "NO_PUBLIC_PDF",
    "ACCESS_RESTRICTED",
    "SOURCE_BLOCKED",
    "RATE_LIMITED",
    "DOWNLOAD_FAILED",
    "PDF_INVALID",
    "PDF_MISMATCH",
    "DISK_FULL",
}
# In-flight statuses reconciled back to PENDING at runner start.
IN_FLIGHT = {"SEARCHING", "FOUND", "DOWNLOADING"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CollectionStore:
    """Read/write collection-plan state inside a :class:`Cache`."""

    def __init__(self, cache: Cache, plan_id: str):
        self.cache = cache
        self.con = cache.con
        self._lock = cache._lock  # share the cache's connection lock
        self.plan_id = plan_id

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self.con.execute(sql, args).fetchall()

    def x(self, sql: str, args=()) -> None:
        with self._lock:
            self.con.execute(sql, args)
            self.con.commit()

    # ── plans ─────────────────────────────────────────────────────────────

    def save_plan(self, *, title: str, path: str, sha256: str, task_count: int) -> None:
        self.x(
            """INSERT INTO collection_plans(plan_id,title,path,sha256,task_count)
               VALUES(?,?,?,?,?)
               ON CONFLICT(plan_id) DO UPDATE SET title=excluded.title,
                   path=excluded.path, sha256=excluded.sha256,
                   task_count=excluded.task_count,
                   updated_at=datetime('now')""",
            (self.plan_id, title, path, sha256, task_count),
        )

    def get_plan(self) -> dict | None:
        rows = self.q("SELECT * FROM collection_plans WHERE plan_id=?", (self.plan_id,))
        return dict(rows[0]) if rows else None

    # ── tasks ─────────────────────────────────────────────────────────────

    def register_task(
        self,
        task_id: str,
        *,
        excel_row: int,
        domain: str,
        topic: str,
        priority: str,
        zh_primary: str,
        en_primary: str,
        output_subdir: str,
        target_pdfs: int,
        task_json: str,
    ) -> None:
        """Insert a task row (idempotent — existing status/counters kept)."""
        self.x(
            """INSERT INTO collection_tasks
                 (plan_id,task_id,excel_row,domain,topic,priority,zh_primary,
                  en_primary,output_subdir,target_pdfs,task_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(plan_id,task_id) DO UPDATE SET
                  excel_row=excluded.excel_row, domain=excluded.domain,
                  topic=excluded.topic, priority=excluded.priority,
                  zh_primary=excluded.zh_primary, en_primary=excluded.en_primary,
                  output_subdir=excluded.output_subdir,
                  target_pdfs=excluded.target_pdfs,
                  task_json=excluded.task_json""",
            (
                self.plan_id,
                task_id,
                excel_row,
                domain,
                topic,
                priority,
                zh_primary,
                en_primary,
                output_subdir,
                target_pdfs,
                task_json,
            ),
        )

    def task_status(self, task_id: str) -> str | None:
        rows = self.q(
            "SELECT status FROM collection_tasks WHERE plan_id=? AND task_id=?",
            (self.plan_id, task_id),
        )
        return rows[0]["status"] if rows else None

    def set_task(self, task_id: str, status: str, **kw) -> None:
        """Update task status + counters (queries_run accumulates)."""
        cols = {
            "candidates": kw.get("candidates"),
            "relevant": kw.get("relevant"),
            "pdf_count": kw.get("pdf_count"),
            "reason": kw.get("reason"),
            "detail": kw.get("detail"),
        }
        sets = ["status=?", "updated_at=?"]
        args: list = [status, _now()]
        for k, v in cols.items():
            if v is not None:
                sets.append(f"{k}=?")
                args.append(v)
        if kw.get("queries_run"):
            sets.append("queries_run=queries_run+?")
            args.append(int(kw["queries_run"]))
        args += [self.plan_id, task_id]
        self.x(
            f"UPDATE collection_tasks SET {', '.join(sets)} WHERE plan_id=? AND task_id=?",  # noqa: S608
            args,
        )

    def reset_inflight(self) -> int:
        """SEARCHING/FOUND/DOWNLOADING → PENDING (crash reconciliation)."""
        with self._lock:
            cur = self.con.execute(
                """UPDATE collection_tasks SET status='PENDING', updated_at=?
                   WHERE plan_id=? AND status IN ('SEARCHING','FOUND','DOWNLOADING')""",
                (_now(), self.plan_id),
            )
            self.con.commit()
            return cur.rowcount

    def reset_statuses(
        self, statuses: set[str], to: str = "PENDING", task_ids: set[str] | None = None
    ) -> int:
        """Bulk-reset tasks in *statuses* (used by retry --failed)."""
        if not statuses:
            return 0
        marks = ",".join("?" * len(statuses))
        sql = (
            f"UPDATE collection_tasks SET status=?, updated_at=? "  # noqa: S608
            f"WHERE plan_id=? AND status IN ({marks})"
        )
        args: list = [to, _now(), self.plan_id, *statuses]
        if task_ids:
            id_marks = ",".join("?" * len(task_ids))
            sql += f" AND task_id IN ({id_marks})"
            args += list(task_ids)
        with self._lock:
            cur = self.con.execute(sql, args)
            self.con.commit()
            return cur.rowcount

    def list_tasks(self, plan_id: str | None = None) -> list[dict]:
        rows = self.q(
            "SELECT * FROM collection_tasks WHERE plan_id=? ORDER BY task_id",
            (plan_id or self.plan_id,),
        )
        return [dict(r) for r in rows]

    # ── queries ───────────────────────────────────────────────────────────

    def log_query(
        self,
        task_id: str,
        tier: str,
        query: str,
        source: str,
        status: str,
        n_results: int,
        error: str = "",
    ) -> None:
        self.x(
            """INSERT INTO collection_queries
               (plan_id,task_id,tier,query,source,status,n_results,error)
               VALUES(?,?,?,?,?,?,?,?)""",
            (self.plan_id, task_id, tier, query, source, status, n_results, error[:500]),
        )

    def queries_for_task(self, task_id: str) -> list[dict]:
        rows = self.q(
            "SELECT * FROM collection_queries WHERE plan_id=? AND task_id=?",
            (self.plan_id, task_id),
        )
        return [dict(r) for r in rows]

    # ── matches ───────────────────────────────────────────────────────────

    def save_match(
        self,
        task_id: str,
        paper_uid: str,
        grade: str,
        score: float,
        why: str = "",
        downloaded: bool = False,
    ) -> None:
        self.x(
            """INSERT INTO collection_matches(plan_id,task_id,paper_uid,grade,score,why,downloaded)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(plan_id,task_id,paper_uid) DO UPDATE SET
                   grade=excluded.grade, score=excluded.score, why=excluded.why,
                   downloaded=max(collection_matches.downloaded, excluded.downloaded)""",
            (self.plan_id, task_id, paper_uid, grade, score, why[:800], int(downloaded)),
        )

    def mark_match_downloaded(self, task_id: str, paper_uid: str) -> None:
        self.x(
            """UPDATE collection_matches SET downloaded=1
               WHERE plan_id=? AND task_id=? AND paper_uid=?""",
            (self.plan_id, task_id, paper_uid),
        )

    def matches_for_task(
        self, task_id: str, grades: tuple[str, ...] = ("HIGH", "MEDIUM")
    ) -> list[dict]:
        marks = ",".join("?" * len(grades))
        rows = self.q(
            f"""SELECT * FROM collection_matches
                WHERE plan_id=? AND task_id=? AND grade IN ({marks})
                ORDER BY CASE grade WHEN 'HIGH' THEN 0 ELSE 1 END, score DESC""",  # noqa: S608
            (self.plan_id, task_id, *grades),
        )
        return [dict(r) for r in rows]

    def count_matches(self, task_id: str) -> int:
        return self.q(
            "SELECT COUNT(*) c FROM collection_matches WHERE plan_id=? AND task_id=?",
            (self.plan_id, task_id),
        )[0]["c"]

    # ── canonical files ───────────────────────────────────────────────────

    def save_file(
        self,
        paper_uid: str,
        *,
        canonical_path: str,
        filename: str,
        sha256: str = "",
        size_bytes: int = 0,
        status: str = "ok",
        error: str = "",
        identity: str = "",
    ) -> None:
        self.x(
            """INSERT INTO collection_files
                 (paper_uid,canonical_path,filename,sha256,size_bytes,status,error,identity)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(paper_uid) DO UPDATE SET
                   canonical_path=excluded.canonical_path,
                   filename=excluded.filename,
                   sha256=COALESCE(NULLIF(excluded.sha256,''),collection_files.sha256),
                   size_bytes=COALESCE(NULLIF(excluded.size_bytes,0),collection_files.size_bytes),
                   status=excluded.status, error=excluded.error,
                   identity=COALESCE(NULLIF(excluded.identity,''),collection_files.identity),
                   downloaded_at=datetime('now')""",
            (
                paper_uid,
                canonical_path,
                filename,
                sha256,
                size_bytes,
                status,
                error[:400],
                identity,
            ),
        )

    def get_file(self, paper_uid: str) -> dict | None:
        rows = self.q("SELECT * FROM collection_files WHERE paper_uid=?", (paper_uid,))
        return dict(rows[0]) if rows else None

    def filename_owner(self, filename: str) -> str | None:
        """paper_uid that already owns *filename* in the canonical library."""
        rows = self.q("SELECT paper_uid FROM collection_files WHERE filename=?", (filename,))
        return rows[0]["paper_uid"] if rows else None

    def list_files(self) -> list[dict]:
        rows = self.q("SELECT * FROM collection_files ORDER BY paper_uid")
        return [dict(r) for r in rows]

    # ── task-dir links ────────────────────────────────────────────────────

    def save_link(self, task_id: str, paper_uid: str, link_path: str) -> None:
        self.x(
            """INSERT INTO collection_links(plan_id,task_id,paper_uid,link_path)
               VALUES(?,?,?,?)
               ON CONFLICT(plan_id,task_id,paper_uid) DO UPDATE SET
                   link_path=excluded.link_path""",
            (self.plan_id, task_id, paper_uid, link_path),
        )

    def links_for_task(self, task_id: str) -> list[dict]:
        rows = self.q(
            "SELECT * FROM collection_links WHERE plan_id=? AND task_id=?",
            (self.plan_id, task_id),
        )
        return [dict(r) for r in rows]

    def list_links(self, plan_id: str | None = None) -> list[dict]:
        rows = self.q(
            "SELECT * FROM collection_links WHERE plan_id=?",
            (plan_id or self.plan_id,),
        )
        return [dict(r) for r in rows]

    # ── download attempt log ──────────────────────────────────────────────

    def log_download(
        self,
        task_id: str,
        paper_uid: str,
        backend: str,
        url: str,
        result: str,
        reason: str = "",
        path: str = "",
        sha256: str = "",
    ) -> None:
        self.x(
            """INSERT INTO collection_downloads
               (plan_id,task_id,paper_uid,backend,url,result,reason,path,sha256)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (self.plan_id, task_id, paper_uid, backend, url, result, reason[:400], path, sha256),
        )

    def download_reasons(self, task_id: str) -> list[str]:
        rows = self.q(
            """SELECT DISTINCT reason FROM collection_downloads
               WHERE plan_id=? AND task_id=? AND result!='ok'""",
            (self.plan_id, task_id),
        )
        return [r["reason"] for r in rows if r["reason"]]

    # ── source health (circuit breaker) ──────────────────────────────────

    def source_ok(self, source: str) -> bool:
        rows = self.q(
            "SELECT cooldown_until FROM collection_source_health WHERE plan_id=? AND source=?",
            (self.plan_id, source),
        )
        return not (rows and rows[0]["cooldown_until"] > time.time())

    def source_success(self, source: str) -> None:
        self.x(
            """INSERT INTO collection_source_health(plan_id,source,consec_errors,cooldown_until,last_error)
               VALUES(?,?,0,0,'')
               ON CONFLICT(plan_id,source) DO UPDATE SET consec_errors=0, last_error=''""",
            (self.plan_id, source),
        )

    def source_fail(self, source: str, error: str, *, cooldown: float = 0) -> None:
        rows = self.q(
            "SELECT consec_errors FROM collection_source_health WHERE plan_id=? AND source=?",
            (self.plan_id, source),
        )
        n = (rows[0]["consec_errors"] if rows else 0) + 1
        cd = max(cooldown, 900.0) if n >= 3 else cooldown
        until = time.time() + cd if cd else 0
        self.x(
            """INSERT INTO collection_source_health(plan_id,source,consec_errors,cooldown_until,last_error)
               VALUES(?,?,?,?,?)
               ON CONFLICT(plan_id,source) DO UPDATE SET consec_errors=?,
                   cooldown_until=max(cooldown_until,?), last_error=?""",
            (self.plan_id, source, n, until, error[:300], n, until, error[:300]),
        )

    def source_health(self) -> list[dict]:
        rows = self.q("SELECT * FROM collection_source_health WHERE plan_id=?", (self.plan_id,))
        return [dict(r) for r in rows]

    # ── misc ──────────────────────────────────────────────────────────────

    def task_blob(self, task_id: str) -> dict | None:
        rows = self.q(
            "SELECT task_json FROM collection_tasks WHERE plan_id=? AND task_id=?",
            (self.plan_id, task_id),
        )
        if rows and rows[0]["task_json"]:
            try:
                return json.loads(rows[0]["task_json"])
            except json.JSONDecodeError:
                return None
        return None
