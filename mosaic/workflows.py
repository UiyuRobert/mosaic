"""Shared orchestration logic used by both the CLI and the web UI.

Functions here encapsulate the *business* side of multi-step operations
(Zotero export, Obsidian export, batch PDF download) so that the CLI and
UI are thin presentation wrappers.
"""

from __future__ import annotations

import logging
from pathlib import Path

from mosaic.db import Cache
from mosaic.errors import DownloadError
from mosaic.models import Paper

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PDF batch download
# ---------------------------------------------------------------------------


def download_papers(
    papers: list[Paper],
    cfg: dict,
    cache: Cache,
    *,
    progress_cb=None,
    outcome: dict | None = None,
    wait_timeout: float | None = None,
) -> dict[str, str]:
    """Download PDFs for *papers* via the persistent download queue.

    Both backends go through ``download_jobs`` so the CLI and the web UI
    share one pipeline: concurrency caps, retries, pause/resume, restart
    reconciliation, and identical completion semantics.

    The blocking wait is bounded by ``download.wait_timeout`` (default
    600 s, ``0`` waits forever). Jobs still running when it elapses keep
    going — the queue is persistent — and are reported through *outcome*.

    Args:
        papers: Papers to fetch.
        cfg: Loaded config dict.
        cache: Local cache instance.
        progress_cb: Called with each job row as it reaches a terminal state.
        outcome: Optional dict filled with ``job_ids``, ``failed`` and
            ``pending`` counts so callers can report partial progress.
        wait_timeout: Seconds to wait for the batch (``None`` → config).

    Returns:
        A ``{paper.uid: local_path}`` mapping keyed by the *caller's* UIDs
        (canonical/alias UIDs are resolved back). Includes papers skipped
        because a verified file already exists.
    """
    from mosaic.dlqueue import TERMINAL, get_manager

    dl_cfg = cfg.get("download", {})
    backend = dl_cfg.get("backend", "auto")
    manager = get_manager(cfg, cache)
    eligible = [p for p in papers if p.pdf_url or p.doi or p.pdf_candidates or p.ntrs_id]
    job_ids = manager.enqueue_papers(eligible)
    if outcome is not None:
        outcome["job_ids"] = list(job_ids)
        outcome["manager"] = manager
    if (
        backend == "aria2"
        and not manager.aria2_available
        and not dl_cfg.get("aria2_fallback_native")
    ):
        # aria2 explicitly requested but unavailable — fail loudly.
        for job_id in job_ids:
            manager.cancel(job_id)
        raise DownloadError(
            "unreachable",
            "aria2 backend selected but daemon unavailable. Install aria2 "
            "(brew/apt install aria2) or set download.backend=native",
        )
    if not job_ids and not eligible:
        return {}
    timeout = wait_timeout
    if timeout is None:
        timeout = float(dl_cfg.get("wait_timeout", 600))
    done_map = manager.wait_all(job_ids, progress_cb=progress_cb, timeout=timeout)

    # Map results back onto the caller-facing UIDs (jobs store canonical
    # UIDs; a paper enqueued under an alias resolves to the same file).
    # Papers skipped because a verified download already exists are
    # reported as successful too — the file *is* on disk.
    pdf_map: dict[str, str] = {}
    for p in eligible:
        canonical = cache.resolve_uid(p.uid)
        path = done_map.get(canonical) or done_map.get(p.uid)
        if not path:
            rec = cache.get_download(p.uid)
            if rec and rec["status"] == "ok" and rec["local_path"]:
                path = rec["local_path"]
        if path:
            pdf_map[p.uid] = path
    if outcome is not None:
        failed = pending = 0
        for jid in job_ids:
            job = cache.get_job(jid)
            if job is None:
                continue
            if job["status"] == "FAILED" or job["status"] == "CANCELLED":
                failed += 1
            elif job["status"] not in TERMINAL:
                pending += 1
        outcome["failed"] = failed
        outcome["pending"] = pending
    return pdf_map


# ---------------------------------------------------------------------------
# Zotero export
# ---------------------------------------------------------------------------


def push_to_zotero(
    papers: list[Paper],
    cfg: dict,
    *,
    collection_name: str = "",
    force_local: bool = False,
    pdf_map: dict[str, str] | None = None,
) -> dict:
    """Export *papers* to Zotero (local or web API).

    Returns:
        A result dict ``{"ok": bool, "msg": str, "added": int, "attached": int}``.
    """
    from mosaic.zotero import ZoteroClient

    zot_cfg = cfg.get("zotero", {})
    api_key = "" if force_local else zot_cfg.get("api_key", "")
    user_id = zot_cfg.get("user_id", 0)
    client = ZoteroClient(api_key=api_key, user_id=user_id)

    if not client.is_reachable():
        if api_key:
            return {"ok": False, "msg": "Zotero web API not reachable. Check your API key."}
        return {
            "ok": False,
            "msg": "No Zotero API key configured and Zotero desktop is not running. "
            "Either set a Zotero web API key in Config, or start the Zotero desktop app.",
        }

    collection_key: str | None = None
    if collection_name:
        try:
            collection_key = client.ensure_collection(collection_name)
        except Exception as e:
            return {
                "ok": False,
                "msg": f"Could not create/find collection '{collection_name}': {e}",
            }

    item_keys = client.add_papers(papers, collection_key=collection_key)
    added = sum(1 for k in item_keys if k)

    attached = 0
    if pdf_map:
        for paper, item_key in zip(papers, item_keys, strict=False):
            if not item_key:
                continue
            local_path = pdf_map.get(paper.uid)
            if (
                local_path
                and Path(local_path).exists()
                and client.attach_pdf(item_key, Path(local_path))
            ):
                attached += 1

    label = f" to '{collection_name}'" if collection_name else ""
    return {
        "ok": True,
        "msg": f"{added} paper(s) added to Zotero{label}.",
        "added": added,
        "attached": attached,
    }


# ---------------------------------------------------------------------------
# Obsidian export
# ---------------------------------------------------------------------------


def push_to_obsidian(
    papers: list[Paper],
    cfg: dict,
    *,
    subfolder_override: str = "",
) -> dict:
    """Export *papers* as Obsidian notes.

    Returns:
        A result dict ``{"ok": bool, "msg": str}``.
    """
    from mosaic.obsidian import ObsidianVault

    obs_cfg = cfg.get("obsidian", {})
    vault_path = obs_cfg.get("vault_path", "")
    if not vault_path:
        return {"ok": False, "msg": "Obsidian vault path is not configured."}

    vault = ObsidianVault(
        vault_path=vault_path,
        subfolder=subfolder_override or obs_cfg.get("subfolder", "papers"),
        filename_pattern=obs_cfg.get("filename_pattern", "{year}_{author}_{title}"),
        tags=obs_cfg.get("tags", ["paper"]),
        wikilinks=obs_cfg.get("wikilinks", True),
    )
    added, skipped = vault.export_papers(papers)
    msg = f"{added} note(s) added"
    if skipped:
        msg += f", {skipped} skipped (already exist)"
    msg += f" → {vault.notes_dir}"
    return {"ok": True, "msg": msg}
