"""SQLite cache for papers and download status."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path

from mosaic.models import Paper, canonical_identifier

log = logging.getLogger(__name__)


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    if db_path != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    _init(con, db_path)
    return con


def _load_sqlite_vec(con: sqlite3.Connection) -> bool:
    """Try to load the sqlite-vec extension.  Returns True on success."""
    try:
        import sqlite_vec

        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        return True
    except Exception:
        return False


_SCHEMA_VERSION = 4

# Versioned migrations. Index `n` holds statements upgrading schema v(n+1)
# to v(n+2). Every statement must be idempotent-safe (we tolerate
# OperationalError) so re-running a migration never corrupts data.
_MIGRATIONS: list[list[str]] = [
    # v1 → v2: NTRS support, identifier alias table, persistent download queue
    [
        "ALTER TABLE papers ADD COLUMN ntrs_id TEXT",
        "ALTER TABLE papers ADD COLUMN report_number TEXT",
        "ALTER TABLE papers ADD COLUMN pub_type TEXT",
        "ALTER TABLE papers ADD COLUMN pdf_candidates TEXT",
        """CREATE TABLE IF NOT EXISTS paper_identifiers (
            identifier TEXT PRIMARY KEY,
            uid        TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_pi_uid ON paper_identifiers(uid)",
        """CREATE TABLE IF NOT EXISTS download_jobs (
            job_id           TEXT PRIMARY KEY,
            paper_uid        TEXT NOT NULL,
            url              TEXT,
            backend          TEXT,
            aria2_gid        TEXT,
            destination      TEXT,
            downloaded_bytes INTEGER DEFAULT 0,
            total_bytes      INTEGER DEFAULT 0,
            status           TEXT NOT NULL DEFAULT 'QUEUED',
            retry_count      INTEGER DEFAULT 0,
            last_error       TEXT,
            next_retry_at    REAL,
            host             TEXT,
            candidates_json  TEXT,
            created_at       TEXT DEFAULT (datetime('now')),
            updated_at       TEXT DEFAULT (datetime('now'))
        )""",
        "ALTER TABLE download_jobs ADD COLUMN candidates_json TEXT",
        "CREATE INDEX IF NOT EXISTS idx_dj_status ON download_jobs(status)",
        "CREATE INDEX IF NOT EXISTS idx_dj_paper ON download_jobs(paper_uid)",
        "CREATE INDEX IF NOT EXISTS idx_dj_host ON download_jobs(host)",
    ],
    # v2 → v3: download integrity metadata + one-active-job-per-paper guard
    [
        "ALTER TABLE downloads ADD COLUMN size_bytes INTEGER",
        "ALTER TABLE downloads ADD COLUMN sha256 TEXT",
        # Retire duplicate active jobs (keep the oldest row per paper) before
        # the uniqueness guard goes in — pre-v3 code allowed duplicates.
        """UPDATE download_jobs SET status='CANCELLED',
                last_error=COALESCE(last_error,'') || ' [superseded duplicate]'
           WHERE status IN ('QUEUED','DOWNLOADING','VERIFYING','RETRY_WAIT','PAUSED')
             AND rowid NOT IN (
                 SELECT MIN(rowid) FROM download_jobs
                 WHERE status IN ('QUEUED','DOWNLOADING','VERIFYING','RETRY_WAIT','PAUSED')
                 GROUP BY paper_uid
             )""",
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_dj_active_uid
           ON download_jobs(paper_uid)
           WHERE status IN ('QUEUED','DOWNLOADING','VERIFYING','RETRY_WAIT','PAUSED')""",
    ],
    # v3 → v4: JSON collection plans — task state, queries, matches, files
    [
        """CREATE TABLE IF NOT EXISTS collection_plans (
            plan_id      TEXT PRIMARY KEY,
            title        TEXT,
            path         TEXT,
            sha256       TEXT,
            task_count   INTEGER DEFAULT 0,
            created_at   TEXT DEFAULT (datetime('now')),
            updated_at   TEXT DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS collection_tasks (
            plan_id      TEXT NOT NULL,
            task_id      TEXT NOT NULL,
            excel_row    INTEGER,
            domain       TEXT,
            topic        TEXT,
            priority     TEXT,
            zh_primary   TEXT,
            en_primary   TEXT,
            output_subdir TEXT,
            status       TEXT NOT NULL DEFAULT 'PENDING',
            queries_run  INTEGER DEFAULT 0,
            candidates   INTEGER DEFAULT 0,
            relevant     INTEGER DEFAULT 0,
            pdf_count    INTEGER DEFAULT 0,
            target_pdfs  INTEGER DEFAULT 3,
            reason       TEXT,
            detail       TEXT,
            task_json    TEXT,
            updated_at   TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (plan_id, task_id)
        )""",
        """CREATE TABLE IF NOT EXISTS collection_queries (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id      TEXT NOT NULL,
            task_id      TEXT NOT NULL,
            tier         TEXT,
            query        TEXT,
            source       TEXT,
            status       TEXT,
            n_results    INTEGER DEFAULT 0,
            error        TEXT,
            ran_at       TEXT DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_cq_task ON collection_queries(plan_id, task_id)",
        """CREATE TABLE IF NOT EXISTS collection_matches (
            plan_id      TEXT NOT NULL,
            task_id      TEXT NOT NULL,
            paper_uid    TEXT NOT NULL,
            grade        TEXT,
            score        REAL,
            why          TEXT,
            downloaded   INTEGER DEFAULT 0,
            PRIMARY KEY (plan_id, task_id, paper_uid)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_cm_paper ON collection_matches(paper_uid)",
        """CREATE TABLE IF NOT EXISTS collection_files (
            paper_uid      TEXT PRIMARY KEY,
            canonical_path TEXT,
            filename       TEXT,
            sha256         TEXT,
            size_bytes     INTEGER,
            status         TEXT,
            error          TEXT,
            identity       TEXT,
            downloaded_at  TEXT DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_cf_filename ON collection_files(filename)",
        """CREATE TABLE IF NOT EXISTS collection_links (
            plan_id      TEXT NOT NULL,
            task_id      TEXT NOT NULL,
            paper_uid    TEXT NOT NULL,
            link_path    TEXT,
            PRIMARY KEY (plan_id, task_id, paper_uid)
        )""",
        """CREATE TABLE IF NOT EXISTS collection_downloads (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id      TEXT NOT NULL,
            task_id      TEXT NOT NULL,
            paper_uid    TEXT NOT NULL,
            backend      TEXT,
            url          TEXT,
            result       TEXT,
            reason       TEXT,
            path         TEXT,
            sha256       TEXT,
            ran_at       TEXT DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_cd_task ON collection_downloads(plan_id, task_id)",
        """CREATE TABLE IF NOT EXISTS collection_source_health (
            plan_id        TEXT NOT NULL,
            source         TEXT NOT NULL,
            consec_errors  INTEGER DEFAULT 0,
            cooldown_until REAL DEFAULT 0,
            last_error     TEXT,
            PRIMARY KEY (plan_id, source)
        )""",
    ],
]


def _backup_db_file(db_path: str, tag: str) -> None:
    """Copy the DB file to ``<db>.bak-<tag>`` before a schema migration."""
    import shutil

    src = Path(db_path)
    if not src.is_file() or db_path == ":memory:":
        return
    dst = src.with_name(src.name + f".bak-{tag}")
    try:
        shutil.copy2(src, dst)
        log.info("Database backed up to %s before migration", dst)
    except OSError as e:
        log.warning("Could not back up database before migration: %s", e)


def _migrate(con: sqlite3.Connection, db_path: str) -> None:
    """Apply pending schema migrations tracked by ``PRAGMA user_version``.

    Databases created before versioning report ``user_version = 0`` and are
    treated as schema v1 (all legacy statements already ran). A backup copy
    of the DB file is written before the first migration batch.
    """
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        version = 1  # pre-versioning databases are schema v1
    if version >= _SCHEMA_VERSION:
        return
    _backup_db_file(db_path, f"v{version}")
    for target in range(version + 1, _SCHEMA_VERSION + 1):
        for stmt in _MIGRATIONS[target - 2]:
            try:
                con.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column/table already present — migration is idempotent
        con.execute(f"PRAGMA user_version = {target}")
    con.commit()


def _init(con: sqlite3.Connection, db_path: str = "") -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS papers (
            uid          TEXT PRIMARY KEY,
            title        TEXT NOT NULL,
            authors      TEXT,
            year         INTEGER,
            doi          TEXT,
            arxiv_id     TEXT,
            pii          TEXT,
            abstract     TEXT,
            journal      TEXT,
            volume       TEXT,
            issue        TEXT,
            pages        TEXT,
            pdf_url      TEXT,
            source       TEXT,
            is_open_access INTEGER DEFAULT 0,
            url          TEXT,
            citation_count INTEGER,
            openalex_id  TEXT
        );
        CREATE TABLE IF NOT EXISTS paper_citations (
            source_uid  TEXT NOT NULL,
            target_uid  TEXT NOT NULL,
            provider    TEXT NOT NULL DEFAULT 'openalex',
            PRIMARY KEY (source_uid, target_uid)
        );
        CREATE INDEX IF NOT EXISTS idx_pc_source ON paper_citations(source_uid);
        CREATE INDEX IF NOT EXISTS idx_pc_target ON paper_citations(target_uid);
        CREATE INDEX IF NOT EXISTS idx_papers_openalex ON papers(openalex_id);
        CREATE TABLE IF NOT EXISTS downloads (
            uid        TEXT PRIMARY KEY,
            local_path TEXT,
            status     TEXT,
            downloaded_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS searches (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            query        TEXT NOT NULL,
            filters_json TEXT,
            sources_json TEXT,
            created_at   TEXT DEFAULT (datetime('now')),
            result_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS exports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            uid         TEXT NOT NULL,
            format      TEXT NOT NULL,
            destination TEXT DEFAULT '',
            exported_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS exports_uid_fmt ON exports(uid, format, destination);
        CREATE TABLE IF NOT EXISTS rag_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    con.commit()
    # Migrations — add columns / tables introduced after the initial schema
    for stmt in (
        "ALTER TABLE papers ADD COLUMN citation_count INTEGER",
        "ALTER TABLE papers ADD COLUMN openalex_id TEXT",
        """CREATE TABLE IF NOT EXISTS paper_citations (
            source_uid  TEXT NOT NULL,
            target_uid  TEXT NOT NULL,
            provider    TEXT NOT NULL DEFAULT 'openalex',
            PRIMARY KEY (source_uid, target_uid)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_pc_source ON paper_citations(source_uid)",
        "CREATE INDEX IF NOT EXISTS idx_pc_target ON paper_citations(target_uid)",
        "CREATE INDEX IF NOT EXISTS idx_papers_openalex ON papers(openalex_id)",
        """CREATE TABLE IF NOT EXISTS paper_chunks (
            chunk_id   TEXT PRIMARY KEY,
            uid        TEXT NOT NULL,
            chunk_idx  INTEGER NOT NULL,
            text       TEXT NOT NULL,
            char_start INTEGER NOT NULL,
            char_end   INTEGER NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_paper_chunks_uid ON paper_chunks(uid)",
    ):
        try:
            con.execute(stmt)
            con.commit()
        except sqlite3.OperationalError:
            pass  # already exists
    _migrate(con, db_path)


def upsert(con: sqlite3.Connection, paper: Paper, uid: str | None = None) -> None:
    """Insert *paper* or enrich the existing row with field-level merge rules.

    Rules (applied only on conflict):
    - abstract       : keep the longer version
    - pdf_url        : preserve the existing value (never overwrite with a new one)
    - is_open_access : True supersedes False (OR / MAX)
    - citation_count : keep the higher value; NULL is treated as "unknown"
    - authors        : keep the longer JSON array
    - doi/arxiv_id/pii/ntrs_id/report_number/pub_type, journal, volume,
      issue, pages, url : fill if empty
    - pdf_candidates : keep the longer JSON array
    - title, year, source : keep the first-recorded value

    Args:
        uid: Canonical UID override — used when the caller has resolved
            aliases to a different UID than ``paper.uid``.
    """
    con.execute(
        """
        INSERT INTO papers
            (uid, title, authors, year, doi, arxiv_id, pii, abstract,
             journal, volume, issue, pages, pdf_url, source, is_open_access,
             url, citation_count, openalex_id, ntrs_id, report_number,
             pub_type, pdf_candidates)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(uid) DO UPDATE SET
            abstract = CASE
                WHEN excluded.abstract IS NULL THEN papers.abstract
                WHEN papers.abstract IS NULL   THEN excluded.abstract
                WHEN length(excluded.abstract) > length(papers.abstract) THEN excluded.abstract
                ELSE papers.abstract
            END,
            pdf_url = COALESCE(papers.pdf_url, excluded.pdf_url),
            is_open_access = MAX(papers.is_open_access, excluded.is_open_access),
            citation_count = CASE
                WHEN excluded.citation_count IS NULL THEN papers.citation_count
                WHEN papers.citation_count IS NULL   THEN excluded.citation_count
                ELSE MAX(papers.citation_count, excluded.citation_count)
            END,
            authors = CASE
                WHEN json_array_length(excluded.authors) > json_array_length(COALESCE(papers.authors, '[]'))
                    THEN excluded.authors
                ELSE papers.authors
            END,
            doi         = COALESCE(papers.doi,         excluded.doi),
            arxiv_id    = COALESCE(papers.arxiv_id,    excluded.arxiv_id),
            pii         = COALESCE(papers.pii,         excluded.pii),
            journal     = COALESCE(papers.journal,     excluded.journal),
            volume      = COALESCE(papers.volume,      excluded.volume),
            issue       = COALESCE(papers.issue,       excluded.issue),
            pages       = COALESCE(papers.pages,       excluded.pages),
            url         = COALESCE(papers.url,         excluded.url),
            openalex_id = COALESCE(papers.openalex_id, excluded.openalex_id),
            ntrs_id      = COALESCE(papers.ntrs_id,      excluded.ntrs_id),
            report_number = COALESCE(papers.report_number, excluded.report_number),
            pub_type     = COALESCE(papers.pub_type,     excluded.pub_type),
            pdf_candidates = CASE
                WHEN json_array_length(COALESCE(excluded.pdf_candidates, '[]'))
                     > json_array_length(COALESCE(papers.pdf_candidates, '[]'))
                    THEN excluded.pdf_candidates
                ELSE papers.pdf_candidates
            END
        """,
        (
            uid or paper.uid,
            paper.title,
            json.dumps(paper.authors),
            paper.year,
            paper.doi,
            paper.arxiv_id,
            paper.pii,
            paper.abstract,
            paper.journal,
            paper.volume,
            paper.issue,
            paper.pages,
            paper.pdf_url,
            paper.source,
            int(paper.is_open_access),
            paper.url,
            paper.citation_count,
            paper.openalex_id,
            paper.ntrs_id,
            paper.report_number,
            paper.pub_type,
            json.dumps(paper.pdf_candidates) if paper.pdf_candidates else None,
        ),
    )
    con.commit()


def canonical_uid_of(identifiers: set[str]) -> str:
    """Return the highest-priority identifier from a set of alias keys."""
    return canonical_identifier(identifiers)


def register_identifiers(con: sqlite3.Connection, identifiers: set[str], uid: str) -> None:
    """Map each identifier in *identifiers* to the canonical *uid*."""
    for ident in identifiers:
        con.execute(
            "INSERT OR REPLACE INTO paper_identifiers(identifier, uid) VALUES (?,?)",
            (ident, uid),
        )
    con.commit()


def resolve_uid(con: sqlite3.Connection, uid: str) -> str:
    """Resolve an alias UID to the canonical papers-table UID."""
    row = con.execute("SELECT uid FROM paper_identifiers WHERE identifier=?", (uid,)).fetchone()
    return row[0] if row else uid


def uids_for_identifiers(con: sqlite3.Connection, identifiers: set[str]) -> set[str]:
    """Return canonical UIDs already mapped from any of *identifiers*."""
    if not identifiers:
        return set()
    placeholders = ",".join("?" * len(identifiers))
    rows = con.execute(
        f"SELECT DISTINCT uid FROM paper_identifiers WHERE identifier IN ({placeholders})",  # noqa: S608
        list(identifiers),
    ).fetchall()
    return {r[0] for r in rows}


def migrate_uid(con: sqlite3.Connection, old_uid: str, new_uid: str) -> None:
    """Re-key every record from *old_uid* to *new_uid* (canonical change).

    Papers rows are merged field-by-field via :func:`upsert`; download
    records keep the better status; identifier aliases are repointed.
    """
    if old_uid == new_uid:
        return
    row = con.execute("SELECT * FROM papers WHERE uid=?", (old_uid,)).fetchone()
    if row:
        paper = row_to_paper(row)
        upsert(con, paper, uid=new_uid)
        con.execute("DELETE FROM papers WHERE uid=?", (old_uid,))
        # Repoint every identifier the migrated record carried (covers rows
        # saved before the identifier table existed — old_uid included).
        register_identifiers(con, paper.identifiers | {old_uid}, new_uid)

    # downloads: repoint; if a row already exists for new_uid keep the best.
    old_dl = con.execute("SELECT * FROM downloads WHERE uid=?", (old_uid,)).fetchone()
    new_dl = con.execute("SELECT * FROM downloads WHERE uid=?", (new_uid,)).fetchone()
    if old_dl and not new_dl:
        con.execute("UPDATE downloads SET uid=? WHERE uid=?", (new_uid, old_uid))
    elif old_dl and new_dl:
        if old_dl["status"] == "ok" and new_dl["status"] != "ok":
            con.execute(
                "UPDATE downloads SET local_path=?, status=? WHERE uid=?",
                (old_dl["local_path"], old_dl["status"], new_uid),
            )
        con.execute("DELETE FROM downloads WHERE uid=?", (old_uid,))

    for table, col in (
        ("paper_identifiers", "uid"),
        ("download_jobs", "paper_uid"),
        ("exports", "uid"),
        ("paper_chunks", "uid"),
        ("paper_citations", "source_uid"),
        ("paper_citations", "target_uid"),
    ):
        try:
            con.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (new_uid, old_uid))  # noqa: S608
        except sqlite3.OperationalError:
            pass  # table may not exist on very old schemas
    con.commit()


def set_download(
    con: sqlite3.Connection,
    uid: str,
    local_path: str,
    status: str,
    size_bytes: int | None = None,
    sha256: str | None = None,
) -> None:
    """Record a download outcome; integrity fields are kept when already set."""
    try:
        con.execute(
            """
            INSERT INTO downloads(uid, local_path, status, size_bytes, sha256)
            VALUES (?,?,?,?,?)
            ON CONFLICT(uid) DO UPDATE SET
                local_path=excluded.local_path,
                status=excluded.status,
                size_bytes=COALESCE(excluded.size_bytes, downloads.size_bytes),
                sha256=COALESCE(excluded.sha256, downloads.sha256)
        """,
            (uid, local_path, status, size_bytes, sha256),
        )
    except sqlite3.OperationalError:
        # Pre-v3 schema without integrity columns.
        con.execute(
            """
            INSERT INTO downloads(uid, local_path, status)
            VALUES (?,?,?)
            ON CONFLICT(uid) DO UPDATE SET local_path=excluded.local_path, status=excluded.status
        """,
            (uid, local_path, status),
        )
    con.commit()


def get_download(con: sqlite3.Connection, uid: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM downloads WHERE uid=?", (uid,)).fetchone()


def row_to_paper(row: sqlite3.Row) -> Paper:
    keys = row.keys()
    return Paper(
        title=row["title"],
        authors=json.loads(row["authors"] or "[]"),
        year=row["year"],
        doi=row["doi"],
        arxiv_id=row["arxiv_id"],
        pii=row["pii"],
        abstract=row["abstract"],
        journal=row["journal"],
        volume=row["volume"],
        issue=row["issue"],
        pages=row["pages"],
        pdf_url=row["pdf_url"],
        source=row["source"],
        is_open_access=bool(row["is_open_access"]),
        url=row["url"],
        citation_count=row["citation_count"],
        openalex_id=row["openalex_id"] if "openalex_id" in keys else None,
        ntrs_id=row["ntrs_id"] if "ntrs_id" in keys else None,
        report_number=row["report_number"] if "report_number" in keys else None,
        pub_type=row["pub_type"] if "pub_type" in keys else None,
        pdf_candidates=(
            json.loads(row["pdf_candidates"])
            if "pdf_candidates" in keys and row["pdf_candidates"]
            else []
        ),
    )


class Cache:
    """Thread-safe SQLite cache.

    All public methods are protected by a reentrant lock so that the cache
    can be shared safely across ``ThreadPoolExecutor`` workers.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self.con = _connect(db_path)
        self._lock = threading.RLock()
        self._vec_available = _load_sqlite_vec(self.con)
        # Alias for internal methods that use _conn naming convention
        self._conn = self.con

    # ── Basic read/write ──────────────────────────────────────────────────────

    def _canonical_for(self, paper: Paper) -> str:
        """Resolve the canonical UID for *paper* via the alias table.

        If any of the paper's identifiers already map to existing UIDs, all
        mapped records are re-keyed to the highest-priority identifier
        (doi > arxiv > pii > ntrs > title) and that UID is returned.
        """
        idents = paper.identifiers
        existing = uids_for_identifiers(self.con, idents)
        candidates = existing | {paper.uid}
        canonical = canonical_uid_of(candidates)
        for uid in existing:
            if uid != canonical:
                migrate_uid(self.con, uid, canonical)
        return canonical

    def save(self, paper: Paper) -> None:
        with self._lock:
            canonical = self._canonical_for(paper)
            upsert(self.con, paper, uid=canonical)
            register_identifiers(self.con, paper.identifiers, canonical)
            # Backfill identifiers carried by the merged stored record —
            # covers rows saved before the identifier table existed.
            row = self.con.execute("SELECT * FROM papers WHERE uid=?", (canonical,)).fetchone()
            if row:
                register_identifiers(self.con, row_to_paper(row).identifiers, canonical)

    def resolve_uid(self, uid: str) -> str:
        """Resolve an alias (e.g. ``ntrs:x`` for a DOI-canonical paper)."""
        with self._lock:
            return resolve_uid(self.con, uid)

    def set_download(
        self,
        uid: str,
        local_path: str,
        status: str,
        size_bytes: int | None = None,
        sha256: str | None = None,
    ) -> None:
        with self._lock:
            set_download(
                self.con,
                resolve_uid(self.con, uid),
                local_path,
                status,
                size_bytes=size_bytes,
                sha256=sha256,
            )

    def get_download(self, uid: str) -> sqlite3.Row | None:
        with self._lock:
            return get_download(self.con, resolve_uid(self.con, uid))

    def get_by_uid(self, uid: str) -> Paper | None:
        with self._lock:
            resolved = resolve_uid(self.con, uid)
            row = self.con.execute("SELECT * FROM papers WHERE uid=?", (resolved,)).fetchone()
            return row_to_paper(row) if row else None

    def search_local(self, query: str) -> list[Paper]:
        with self._lock:
            rows = self.con.execute(
                "SELECT * FROM papers WHERE title LIKE ? OR abstract LIKE ?",
                (f"%{query}%", f"%{query}%"),
            ).fetchall()
            return [row_to_paper(r) for r in rows]

    def save_search(
        self, query: str, filters_json: str = "", sources_json: str = "", result_count: int = 0
    ) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO searches(query, filters_json, sources_json, result_count) VALUES (?,?,?,?)",
                (query, filters_json, sources_json, result_count),
            )
            self.con.commit()

    def list_searches(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT * FROM searches ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Richness predicate (Phase 5) ──────────────────────────────────────────

    def is_rich(self, uid: str) -> bool:
        """True when the cached record has abstract, year, and at least one author."""
        with self._lock:
            row = self.con.execute(
                "SELECT abstract, year, authors FROM papers WHERE uid=?", (uid,)
            ).fetchone()
            if not row:
                return False
            authors = json.loads(row["authors"] or "[]")
            return bool(row["abstract"] and row["year"] and authors)

    def rich_uids(self) -> set[str]:
        """Return the set of UIDs whose records are considered rich (abstract + year + authors)."""
        with self._lock:
            rows = self.con.execute(
                "SELECT uid, abstract, year, authors FROM papers "
                "WHERE abstract IS NOT NULL AND year IS NOT NULL"
            ).fetchall()
            result: set[str] = set()
            for row in rows:
                if json.loads(row["authors"] or "[]"):
                    result.add(row["uid"])
            return result

    # ── Cache management (Phase 4) ────────────────────────────────────────────

    def stats(self) -> dict:
        """Return a summary dict of cache contents."""
        with self._lock:
            papers = self.con.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            downloaded = self.con.execute(
                "SELECT COUNT(*) FROM downloads WHERE status='ok'"
            ).fetchone()[0]
            searches = self.con.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
            with_abstract = self.con.execute(
                "SELECT COUNT(*) FROM papers WHERE abstract IS NOT NULL"
            ).fetchone()[0]
            with_pdf_url = self.con.execute(
                "SELECT COUNT(*) FROM papers WHERE pdf_url IS NOT NULL"
            ).fetchone()[0]
            open_access = self.con.execute(
                "SELECT COUNT(*) FROM papers WHERE is_open_access=1"
            ).fetchone()[0]
            exports_total = self.con.execute("SELECT COUNT(*) FROM exports").fetchone()[0]

        try:
            db_bytes = os.path.getsize(self._db_path)
        except OSError:
            db_bytes = 0

        return {
            "papers": papers,
            "downloaded": downloaded,
            "searches": searches,
            "with_abstract": with_abstract,
            "with_pdf_url": with_pdf_url,
            "open_access": open_access,
            "exports": exports_total,
            "db_bytes": db_bytes,
        }

    def list_papers(self, limit: int = 50, offset: int = 0, query: str = "") -> list[Paper]:
        """Return cached papers, newest-first, with optional substring filter."""
        with self._lock:
            if query:
                rows = self.con.execute(
                    "SELECT * FROM papers WHERE title LIKE ? OR abstract LIKE ? "
                    "ORDER BY rowid DESC LIMIT ? OFFSET ?",
                    (f"%{query}%", f"%{query}%", limit, offset),
                ).fetchall()
            else:
                rows = self.con.execute(
                    "SELECT * FROM papers ORDER BY rowid DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            return [row_to_paper(r) for r in rows]

    def count_papers(self, query: str = "") -> int:
        """Total number of papers matching *query* (or all papers if empty)."""
        with self._lock:
            if query:
                return self.con.execute(
                    "SELECT COUNT(*) FROM papers WHERE title LIKE ? OR abstract LIKE ?",
                    (f"%{query}%", f"%{query}%"),
                ).fetchone()[0]
            return self.con.execute("SELECT COUNT(*) FROM papers").fetchone()[0]

    def verify_downloads(self) -> list[dict]:
        """Check whether each tracked download file still exists on disk.

        Returns a list of dicts with keys ``uid``, ``local_path``, ``exists``.
        """
        with self._lock:
            rows = self.con.execute(
                "SELECT uid, local_path FROM downloads WHERE status='ok'"
            ).fetchall()
        return [
            {
                "uid": row["uid"],
                "local_path": row["local_path"],
                "exists": bool(row["local_path"] and Path(row["local_path"]).exists()),
            }
            for row in rows
        ]

    def clean_stubs(self) -> int:
        """Remove download records whose files no longer exist on disk.

        Returns the number of records removed.
        """
        with self._lock:
            rows = self.con.execute(
                "SELECT uid, local_path FROM downloads WHERE status='ok'"
            ).fetchall()
            removed = 0
            for row in rows:
                if not row["local_path"] or not Path(row["local_path"]).exists():
                    self.con.execute("DELETE FROM downloads WHERE uid=?", (row["uid"],))
                    removed += 1
            if removed:
                self.con.commit()
            return removed

    def clear(self) -> None:
        """Wipe all papers, downloads, searches, exports, vector index, and citation graph."""
        with self._lock:
            self.con.executescript(
                "DELETE FROM papers; DELETE FROM downloads; "
                "DELETE FROM searches; DELETE FROM exports; "
                "DELETE FROM paper_citations; DELETE FROM rag_meta; "
                "DELETE FROM paper_chunks;"
            )
            self.con.commit()
        self.rebuild_vec_table()  # drops vec_papers/vec_chunks; safe whether or not sqlite-vec is loaded

    # ── Export tracking (Phase 4) ─────────────────────────────────────────────

    def track_export(self, uid: str, fmt: str, destination: str = "") -> None:
        """Record that *uid* was exported in format *fmt* to *destination*."""
        with self._lock:
            self.con.execute(
                "INSERT INTO exports(uid, format, destination) VALUES (?,?,?)",
                (uid, fmt, destination),
            )
            self.con.commit()

    def was_exported(self, uid: str, fmt: str, destination: str = "") -> bool:
        """True if *uid* was previously exported in *fmt* to *destination*."""
        with self._lock:
            return (
                self.con.execute(
                    "SELECT 1 FROM exports WHERE uid=? AND format=? AND destination=?",
                    (uid, fmt, destination),
                ).fetchone()
                is not None
            )

    # ── RAG / vector index ────────────────────────────────────────────────────

    def get_rag_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM rag_meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def set_rag_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO rag_meta (key, value) VALUES (?, ?)", (key, value)
            )
            self._conn.commit()

    def _ensure_vec_table(self, dim: int) -> None:
        """Create vec_papers virtual table for given embedding dimension if not exists."""
        if not self._vec_available:
            raise RuntimeError(
                "sqlite-vec is not installed. Run: pipx inject mosaic-search sqlite-vec"
            )
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_papers USING vec0(uid TEXT PRIMARY KEY, embedding float[{dim}])"
        )
        self._conn.commit()

    def upsert_embedding(self, uid: str, embedding: list[float], dim: int) -> None:
        with self._lock:
            self._ensure_vec_table(dim)
            self._conn.execute(
                "INSERT OR REPLACE INTO vec_papers(uid, embedding) VALUES (?, ?)",
                (uid, json.dumps(embedding)),
            )
            self._conn.commit()

    def upsert_embeddings_batch(self, rows: list[tuple[str, list[float]]], dim: int) -> None:
        """Batch upsert for efficiency."""
        with self._lock:
            self._ensure_vec_table(dim)
            self._conn.executemany(
                "INSERT OR REPLACE INTO vec_papers(uid, embedding) VALUES (?, ?)",
                [(uid, json.dumps(emb)) for uid, emb in rows],
            )
            self._conn.commit()

    def get_indexed_uids(self) -> set[str]:
        """Return set of UIDs already present in the chunk index (or legacy vec_papers)."""
        with self._lock:
            # Primary: paper_chunks table (new format)
            try:
                rows = self._conn.execute("SELECT DISTINCT uid FROM paper_chunks").fetchall()
                if rows:
                    return {r[0] for r in rows}
            except Exception:
                pass
            # Fallback: legacy vec_papers table
            if not self._vec_available:
                return set()
            try:
                rows = self._conn.execute("SELECT uid FROM vec_papers").fetchall()
                return {r[0] for r in rows}
            except Exception:
                return set()

    def vector_search(self, query_embedding: list[float], k: int) -> list[str]:
        """Return up to k UIDs ordered by distance (closest first)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT uid
                FROM vec_papers
                WHERE embedding MATCH ?
                  AND k = ?
                ORDER BY distance
                """,
                (json.dumps(query_embedding), k),
            ).fetchall()
            return [r[0] for r in rows]

    def vector_search_scored(self, query_embedding: list[float], k: int) -> list[tuple[str, float]]:
        """Return up to k (uid, distance) pairs ordered by distance (closest first)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT uid, distance
                FROM vec_papers
                WHERE embedding MATCH ?
                  AND k = ?
                ORDER BY distance
                """,
                (json.dumps(query_embedding), k),
            ).fetchall()
            return [(r[0], float(r[1])) for r in rows]

    def get_downloaded_uids(self) -> set[str]:
        """Return UIDs of papers with at least one successful local download."""
        with self._lock:
            rows = self._conn.execute("SELECT uid FROM downloads WHERE status = 'ok'").fetchall()
            return {r[0] for r in rows}

    def rebuild_vec_table(self) -> None:
        """Drop and recreate vec tables (needed when embedding model changes)."""
        with self._lock:
            self._conn.execute("DROP TABLE IF EXISTS vec_papers")
            self._conn.execute("DROP TABLE IF EXISTS vec_chunks")
            self._conn.execute("DELETE FROM paper_chunks")
            self._conn.commit()

    def _ensure_vec_chunks_table(self, dim: int) -> None:
        """Create vec_chunks virtual table for given embedding dimension if not exists."""
        if not self._vec_available:
            raise RuntimeError(
                "sqlite-vec is not installed. Run: pipx inject mosaic-search sqlite-vec"
            )
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(chunk_id TEXT PRIMARY KEY, embedding float[{dim}])"
        )
        self._conn.commit()

    def upsert_chunks_batch(
        self,
        rows: list[tuple[str, str, int, str, int, int, list[float]]],
        dim: int,
    ) -> None:
        """Upsert chunk metadata + embeddings atomically.

        Each row is (chunk_id, uid, chunk_idx, text, char_start, char_end, embedding).
        """
        if not rows:
            return
        with self._lock:
            self._ensure_vec_chunks_table(dim)
            self._conn.executemany(
                "INSERT OR REPLACE INTO paper_chunks "
                "(chunk_id, uid, chunk_idx, text, char_start, char_end) "
                "VALUES (?,?,?,?,?,?)",
                [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows],
            )
            self._conn.executemany(
                "INSERT OR REPLACE INTO vec_chunks(chunk_id, embedding) VALUES (?,?)",
                [(r[0], json.dumps(r[6])) for r in rows],
            )
            self._conn.commit()

    def vector_search_chunks(self, query_embedding: list[float], k: int) -> list[tuple[str, float]]:
        """Return up to k (chunk_id, distance) pairs from vec_chunks (closest first)."""
        with self._lock:
            try:
                rows = self._conn.execute(
                    """
                    SELECT chunk_id, distance
                    FROM vec_chunks
                    WHERE embedding MATCH ?
                      AND k = ?
                    ORDER BY distance
                    """,
                    (json.dumps(query_embedding), k),
                ).fetchall()
                return [(r[0], r[1]) for r in rows]
            except Exception as exc:
                if "no such table" in str(exc).lower():
                    raise
                return []

    def get_chunk_texts(self, chunk_ids: list[str]) -> dict[str, str]:
        """Return {chunk_id: text} for the given chunk IDs."""
        if not chunk_ids:
            return {}
        with self._lock:
            placeholders = ",".join("?" * len(chunk_ids))
            rows = self._conn.execute(
                f"SELECT chunk_id, text FROM paper_chunks WHERE chunk_id IN ({placeholders})",  # noqa: S608
                chunk_ids,
            ).fetchall()
            return {r[0]: r[1] for r in rows}

    def get_papers_by_uids(self, uids: list[str]) -> list[Paper]:
        """Fetch Paper objects for the given UIDs from the papers table."""
        if not uids:
            return []
        with self._lock:
            placeholders = ",".join("?" * len(uids))
            rows = self._conn.execute(
                f"SELECT * FROM papers WHERE uid IN ({placeholders})",  # noqa: S608
                uids,
            ).fetchall()
            return [row_to_paper(r) for r in rows]

    def get_all_papers(self) -> list[Paper]:
        """Return all papers from the cache."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM papers").fetchall()
            return [row_to_paper(r) for r in rows]

    # ── Citation graph ────────────────────────────────────────────────────────

    def upsert_citation_edges(self, edges: list[tuple[str, str, str]]) -> None:
        """Insert (source_uid, target_uid, provider) triples, ignoring duplicates.

        Args:
            edges: List of ``(source_uid, target_uid, provider)`` tuples.
        """
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO paper_citations (source_uid, target_uid, provider) VALUES (?,?,?)",
                edges,
            )
            self._conn.commit()

    def get_citation_links(self, uid: str, candidate_uids: set[str]) -> int:
        """Count citation edges between *uid* and any uid in *candidate_uids*.

        Both directions are counted: edges where *uid* is the source (cites
        a candidate) and edges where *uid* is the target (cited by a
        candidate).

        Args:
            uid: The paper whose citation links are counted.
            candidate_uids: Set of other UIDs to check for edges against.

        Returns:
            Total number of bidirectional citation links found.
        """
        if not candidate_uids:
            return 0
        with self._lock:
            placeholders = ",".join("?" * len(candidate_uids))
            candidates = list(candidate_uids)
            outgoing = self._conn.execute(
                f"SELECT COUNT(*) FROM paper_citations WHERE source_uid=? AND target_uid IN ({placeholders})",  # noqa: S608
                [uid, *candidates],
            ).fetchone()[0]
            incoming = self._conn.execute(
                f"SELECT COUNT(*) FROM paper_citations WHERE target_uid=? AND source_uid IN ({placeholders})",  # noqa: S608
                [uid, *candidates],
            ).fetchone()[0]
            return outgoing + incoming

    def get_citation_neighbors(self, uid: str) -> list[str]:
        """Return UIDs of cached papers cited by *uid*.

        Only returns target UIDs that are present in the papers table.

        Args:
            uid: Source paper UID.

        Returns:
            List of target UIDs present in the local cache.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT pc.target_uid FROM paper_citations pc
                INNER JOIN papers p ON p.uid = pc.target_uid
                WHERE pc.source_uid = ?
                """,
                (uid,),
            ).fetchall()
            return [r[0] for r in rows]

    def get_enriched_uids(self) -> set[str]:
        """Return source UIDs already present in paper_citations.

        Used to skip re-enrichment of already-processed papers.  Note that
        papers which were queried but had no local citation matches will NOT
        appear here and will be re-attempted on the next enrichment run.

        Returns:
            Set of source UIDs with at least one stored citation edge.
        """
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT source_uid FROM paper_citations").fetchall()
            return {r[0] for r in rows}

    def get_all_citation_edges(self) -> list[tuple[str, str]]:
        """Return all (source_uid, target_uid) pairs stored in paper_citations.

        Used by the citation network analyser to build the full graph in
        memory before traversal and clustering.

        Returns:
            List of ``(source_uid, target_uid)`` tuples.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_uid, target_uid FROM paper_citations"
            ).fetchall()
            return [(r[0], r[1]) for r in rows]

    # ── Download queue (persistent jobs) ─────────────────────────────────────

    def enqueue_job(self, job: dict) -> None:
        """Insert a download job row. Required keys: job_id, paper_uid, url,
        backend, destination, host. Optional: status, next_retry_at."""
        with self._lock:
            self.con.execute(
                """
                INSERT INTO download_jobs
                    (job_id, paper_uid, url, backend, destination, host,
                     status, next_retry_at, candidates_json)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    job["job_id"],
                    job["paper_uid"],
                    job.get("url"),
                    job.get("backend"),
                    job.get("destination"),
                    job.get("host"),
                    job.get("status", "QUEUED"),
                    job.get("next_retry_at"),
                    job.get("candidates_json"),
                ),
            )
            self.con.commit()

    def update_job(self, job_id: str, **fields) -> None:
        """Update arbitrary columns of a download job and bump updated_at."""
        allowed = {
            "url",
            "backend",
            "aria2_gid",
            "destination",
            "downloaded_bytes",
            "total_bytes",
            "status",
            "retry_count",
            "last_error",
            "next_retry_at",
            "host",
            "candidates_json",
        }
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        with self._lock:
            cols = ", ".join(f"{k}=?" for k in sets)
            self.con.execute(
                f"UPDATE download_jobs SET {cols}, updated_at=datetime('now') WHERE job_id=?",  # noqa: S608
                (*sets.values(), job_id),
            )
            self.con.commit()

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            row = self.con.execute(
                "SELECT * FROM download_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_jobs(
        self,
        status: str | list[str] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        """List download jobs, newest first. *status* filters on one or more
        status values; ``None`` returns all jobs."""
        with self._lock:
            if status is None:
                rows = self.con.execute(
                    "SELECT * FROM download_jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            else:
                statuses = [status] if isinstance(status, str) else list(status)
                placeholders = ",".join("?" * len(statuses))
                rows = self.con.execute(
                    f"SELECT * FROM download_jobs WHERE status IN ({placeholders}) "  # noqa: S608
                    "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (*statuses, limit, offset),
                ).fetchall()
            return [dict(r) for r in rows]

    def job_counts(self) -> dict[str, int]:
        """Return ``{status: count}`` across the download queue."""
        with self._lock:
            rows = self.con.execute(
                "SELECT status, COUNT(*) AS n FROM download_jobs GROUP BY status"
            ).fetchall()
            return {r["status"]: r["n"] for r in rows}

    def delete_job(self, job_id: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM download_jobs WHERE job_id=?", (job_id,))
            self.con.commit()

    def clear_jobs(self, statuses: list[str]) -> int:
        """Delete jobs in the given statuses; returns rows removed."""
        if not statuses:
            return 0
        with self._lock:
            placeholders = ",".join("?" * len(statuses))
            cur = self.con.execute(
                f"DELETE FROM download_jobs WHERE status IN ({placeholders})",  # noqa: S608
                statuses,
            )
            self.con.commit()
            return cur.rowcount

    def jobs_for_paper(self, paper_uid: str) -> list[dict]:
        """All jobs for a paper, including rows stored under alias UIDs."""
        with self._lock:
            resolved = resolve_uid(self.con, paper_uid)
            alias_rows = self.con.execute(
                "SELECT identifier FROM paper_identifiers WHERE uid=?", (resolved,)
            ).fetchall()
            uids = {r[0] for r in alias_rows} | {resolved, paper_uid}
            placeholders = ",".join("?" * len(uids))
            rows = self.con.execute(
                f"SELECT * FROM download_jobs WHERE paper_uid IN ({placeholders}) "  # noqa: S608
                "ORDER BY created_at DESC",
                list(uids),
            ).fetchall()
            return [dict(r) for r in rows]

    def active_job_for_uids(self, uids: set[str]) -> dict | None:
        """Return a non-terminal download job for any of *uids*, or None.

        "Active" includes PAUSED — a paused job still owns the paper's
        download slot, so it must block duplicate enqueues.
        """
        if not uids:
            return None
        with self._lock:
            resolved = {resolve_uid(self.con, u) for u in uids} | uids
            placeholders = ",".join("?" * len(resolved))
            row = self.con.execute(
                f"SELECT * FROM download_jobs WHERE paper_uid IN ({placeholders}) "  # noqa: S608
                "AND status IN ('QUEUED','DOWNLOADING','VERIFYING','RETRY_WAIT','PAUSED') "
                "LIMIT 1",
                list(resolved),
            ).fetchone()
            return dict(row) if row else None
