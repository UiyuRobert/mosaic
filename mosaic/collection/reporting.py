"""Manifest export — the ``_manifest/`` directory of a collection run.

Writes five artefacts describing everything the runner did:

- ``papers.csv``             — one row per unique canonical PDF
- ``task_status.csv``        — every plan task with status + counters
- ``task_paper_links.csv``   — task ↔ paper associations (grade, paths)
- ``failures.csv``           — non-completed tasks + download failure reasons
- ``collection_report.json`` — machine-readable run summary
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

from mosaic.collection.state import CollectionStore
from mosaic.collection.storage import manifest_dir


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def export_manifest(store: CollectionStore, root: Path) -> dict:
    """Write all manifest files under ``<root>/_manifest/``; returns the summary."""
    mandir = manifest_dir(root)
    mandir.mkdir(parents=True, exist_ok=True)

    tasks = store.list_tasks()
    files = store.list_files()
    links = store.list_links()

    # papers.csv — canonical library inventory joined with catalogue data.
    paper_rows: list[list] = []
    for f in files:
        paper = store.cache.get_by_uid(f["paper_uid"])
        paper_rows.append(
            [
                f["paper_uid"],
                (paper.title if paper else "") or "",
                (paper.year if paper else "") or "",
                (paper.doi if paper else "") or "",
                (paper.arxiv_id if paper else "") or "",
                (paper.ntrs_id if paper else "") or "",
                (paper.source if paper else "") or "",
                f["filename"],
                f["canonical_path"],
                f["sha256"],
                f["size_bytes"] or 0,
                f["status"],
                f.get("identity") or "",
            ]
        )
    _write_csv(
        mandir / "papers.csv",
        [
            "paper_uid",
            "title",
            "year",
            "doi",
            "arxiv_id",
            "ntrs_id",
            "source",
            "filename",
            "canonical_path",
            "sha256",
            "size_bytes",
            "status",
            "identity",
        ],
        paper_rows,
    )

    # task_status.csv — full task registry.
    _write_csv(
        mandir / "task_status.csv",
        [
            "task_id",
            "domain",
            "topic",
            "priority",
            "zh_primary",
            "en_primary",
            "status",
            "reason",
            "candidates",
            "relevant",
            "pdf_count",
            "target_pdfs",
            "queries_run",
            "output_subdir",
            "updated_at",
        ],
        [
            [
                t["task_id"],
                t["domain"],
                t["topic"] or "",
                t["priority"],
                t["zh_primary"] or "",
                t["en_primary"] or "",
                t["status"],
                t["reason"] or "",
                t["candidates"],
                t["relevant"],
                t["pdf_count"],
                t["target_pdfs"],
                t["queries_run"],
                t["output_subdir"],
                t["updated_at"],
            ]
            for t in tasks
        ],
    )

    # task_paper_links.csv — associations incl. relevance + on-disk paths.
    match_index = {}
    for t in tasks:
        for m in store.q(
            "SELECT * FROM collection_matches WHERE plan_id=? AND task_id=?",
            (store.plan_id, t["task_id"]),
        ):
            match_index[(t["task_id"], m["paper_uid"])] = dict(m)
    file_index = {f["paper_uid"]: f for f in files}
    link_rows: list[list] = []
    for link in links:
        m = match_index.get((link["task_id"], link["paper_uid"]), {})
        f = file_index.get(link["paper_uid"], {})
        link_rows.append(
            [
                link["task_id"],
                link["paper_uid"],
                m.get("grade", ""),
                m.get("score", ""),
                f.get("filename", ""),
                f.get("canonical_path", ""),
                link["link_path"] or "",
            ]
        )
    _write_csv(
        mandir / "task_paper_links.csv",
        ["task_id", "paper_uid", "grade", "score", "filename", "canonical_path", "link_path"],
        link_rows,
    )

    # failures.csv — unfinished tasks + per-task download failure reasons.
    fail_rows: list[list] = []
    for t in tasks:
        if t["status"] == "COMPLETED":
            continue
        reasons = ";".join(sorted(set(store.download_reasons(t["task_id"]))))
        fail_rows.append(
            [
                t["task_id"],
                t["domain"],
                t["priority"],
                t["en_primary"] or "",
                t["status"],
                t["reason"] or "",
                reasons or (t["detail"] or ""),
                t["candidates"],
                t["relevant"],
                t["pdf_count"],
            ]
        )
    _write_csv(
        mandir / "failures.csv",
        [
            "task_id",
            "domain",
            "priority",
            "en_primary",
            "status",
            "reason",
            "download_errors",
            "candidates",
            "relevant",
            "pdf_count",
        ],
        fail_rows,
    )

    # collection_report.json — aggregate summary.
    status_counts: dict[str, int] = {}
    for t in tasks:
        status_counts[t["status"]] = status_counts.get(t["status"], 0) + 1
    report = {
        "plan_id": store.plan_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "output_root": str(root),
        "tasks_total": len(tasks),
        "status_counts": status_counts,
        "unique_pdfs": sum(1 for f in files if f["status"] == "ok"),
        "unique_papers_seen": sum(
            1
            for _ in store.q(
                "SELECT DISTINCT paper_uid FROM collection_matches WHERE plan_id=?",
                (store.plan_id,),
            )
        ),
        "task_links": len(links),
        "tasks_completed": status_counts.get("COMPLETED", 0),
        "tasks_partial": status_counts.get("PARTIAL", 0),
        "tasks_failed": sum(
            v for k, v in status_counts.items() if k not in ("COMPLETED", "PARTIAL", "PENDING")
        ),
        "tasks_pending": status_counts.get("PENDING", 0),
    }
    (mandir / "collection_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report
