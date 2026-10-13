"""Flask route handlers for the MOSAIC web UI."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    stream_with_context,
    url_for,
)
from markupsafe import escape

from mosaic.models import Paper, SearchFilters
from mosaic.search import search_all
from mosaic.services import build_filters, filter_papers
from mosaic.source_registry import SHORTHAND_TO_CFG_KEY, SRC_MAP, build_sources

bp = Blueprint("ui", __name__)


# Maximum upload size (10 MB, enforced manually)
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg():
    return current_app.config["MOSAIC_CFG"]


def _cache():
    return current_app.config["MOSAIC_CACHE"]


def _jobs():
    return current_app.config["JOB_MANAGER"]


def _purge_stale():
    """Remove transient app.config keys for jobs that have been garbage-collected."""
    jm = _jobs()
    for jid in jm.stale_job_ids():
        current_app.config.pop(f"export_{jid}", None)
        current_app.config.pop(f"job_meta_{jid}", None)
    jm._cleanup()


def _version():
    from mosaic import __version__

    return __version__


def _safe_int(value, default: int = 10, lo: int = 1, hi: int = 200) -> int:
    """Parse an integer from form input, clamping to [lo, hi]."""
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


def _build_filters(form) -> tuple[SearchFilters | None, str | None]:
    """Return (filters, optional_warning). Warning is set on invalid year format."""
    return build_filters(
        year=form.get("year", "").strip(),
        author=form.get("author", "").strip(),
        journal=form.get("journal", "").strip(),
        field=form.get("field", "all"),
        raw_query=form.get("raw_query", "").strip(),
    )


def _run_search(sources, query, max_per_source, filters, progress=None):
    """Executed in a worker thread."""
    errors: list[str] = []
    stats: dict = {}

    def _on_progress(source_name: str, status: str) -> None:
        if progress is not None:
            progress[source_name] = status

    papers = search_all(
        sources,
        query,
        max_per_source=max_per_source,
        filters=filters,
        errors=errors,
        stats=stats,
        progress_callback=_on_progress,
    )
    return {"papers": papers, "errors": errors, "stats": stats}


def _run_similar(identifier, max_results, oa_email, ss_api_key):
    """Executed in a worker thread."""
    from mosaic.similar import find_similar

    seed_title, papers = find_similar(
        identifier,
        max_results=max_results,
        oa_email=oa_email,
        ss_api_key=ss_api_key,
    )
    return {"seed_title": seed_title, "papers": papers}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@bp.route("/")
def search_page():
    cfg = _cfg()
    src_cfg = cfg.get("sources", {})
    # Build source list with enabled status
    source_list = []
    for key, display_name in SRC_MAP.items():
        cfg_key = SHORTHAND_TO_CFG_KEY.get(key, key)
        enabled = src_cfg.get(cfg_key, {}).get("enabled", True)
        source_list.append({"key": key, "name": display_name, "enabled": enabled})

    prefill_query = request.args.get("q", "")
    prefill_filters = {
        "year": request.args.get("year", ""),
        "author": request.args.get("author", ""),
        "journal": request.args.get("journal", ""),
        "field": request.args.get("field", ""),
    }
    return render_template(
        "search.html",
        sources=source_list,
        version=_version(),
        prefill_query=prefill_query,
        prefill_filters=prefill_filters,
    )


@bp.route("/search", methods=["POST"])
def search_submit():
    _purge_stale()
    cfg = _cfg()
    query = request.form.get("query", "").strip()
    if not query:
        return render_template(
            "partials/results.html",
            papers=[],
            errors=["Please enter a search query."],
            stats={},
            version=_version(),
        )

    max_results = _safe_int(request.form.get("max_results", 10))
    filters, year_warning = _build_filters(request.form)
    cached_only = request.form.get("cached") == "on"
    prefer_cache = request.form.get("prefer_cache") == "on"
    sort_by = request.form.get("sort_by", "")

    # Cache-only mode: skip network, search local DB directly
    if cached_only:
        cache = _cache()
        papers = cache.search_local(query)
        if filters:
            papers = [p for p in papers if filters.match(p)]
        from mosaic.services import filter_papers as _fp

        papers = _fp(
            papers,
            oa_only=request.form.get("oa_only") == "on",
            pdf_only=request.form.get("pdf_only") == "on",
            sort_by=sort_by if sort_by != "relevance" else "",
        )
        if sort_by == "relevance":
            from mosaic.services import sort_by_relevance as _sbr

            papers = _sbr(query, papers, cfg)
        cache.save_search(query=query, filters_json=json.dumps({}), result_count=len(papers))
        import uuid

        job_id = str(uuid.uuid4())
        current_app.config[f"export_{job_id}"] = papers
        errors = [year_warning] if year_warning else []
        return render_template(
            "partials/results.html",
            papers=papers,
            errors=errors,
            stats={},
            job_id=job_id,
            version=_version(),
        )

    # Build sources filtered by user selection
    selected = request.form.getlist("sources")
    all_sources = build_sources(cfg)
    if selected:
        selected_names = {SRC_MAP[k] for k in selected if k in SRC_MAP}
        sources = [s for s in all_sources if s.name in selected_names]
    elif request.form.get("_has_sources"):
        # User explicitly deselected all sources in the search form
        errors = ["No sources selected. Please select at least one source."]
        if year_warning:
            errors.insert(0, year_warning)
        return render_template(
            "partials/results.html", papers=[], errors=errors, stats={}, version=_version()
        )
    else:
        sources = all_sources

    source_count = len(sources)
    # Shared dict — written by _run_search worker, read by job_status polling
    progress: dict[str, str] = {s.name: "pending" for s in sources}
    jm = _jobs()
    job_id = jm.submit(_run_search, sources, query, max_results, filters, progress)
    job = jm.get(job_id)
    if job is not None:
        job.progress = progress

    # Store form state for the export link and history re-run
    current_app.config[f"job_meta_{job_id}"] = {
        "query": query,
        "oa_only": request.form.get("oa_only") == "on",
        "pdf_only": request.form.get("pdf_only") == "on",
        "sort_by": sort_by,
        "prefer_cache": prefer_cache,
        "year_warning": year_warning,
        "filters": {
            "year": request.form.get("year", ""),
            "author": request.form.get("author", ""),
            "journal": request.form.get("journal", ""),
            "field": request.form.get("field", "all"),
        },
    }

    return render_template(
        "partials/job_status.html",
        job_id=job_id,
        source_count=source_count,
        status_url=url_for("ui.search_status", job_id=job_id),
        job_type="search",
        progress=progress,
    )


@bp.route("/search/status/<job_id>")
def search_status(job_id):
    job = _jobs().get(job_id)
    if job is None:
        return render_template(
            "partials/results.html",
            papers=[],
            errors=["Job not found."],
            stats={},
            version=_version(),
        )

    if job.status == "running":
        return render_template(
            "partials/job_status.html",
            job_id=job_id,
            source_count=0,
            status_url=url_for("ui.search_status", job_id=job_id),
            job_type="search",
            progress=job.progress,
        )

    if job.status == "error":
        _jobs().pop(job_id)
        return render_template(
            "partials/results.html",
            papers=[],
            errors=[job.error_message],
            stats={},
            version=_version(),
        )

    # Done
    result = job.result
    papers = result["papers"]
    errors = result["errors"]
    stats = result["stats"]

    # Apply post-filters from form state
    meta = current_app.config.pop(f"job_meta_{job_id}", {})
    if meta.get("year_warning"):
        errors.insert(0, meta["year_warning"])
    sort_by = meta.get("sort_by", "")
    papers = filter_papers(
        papers,
        oa_only=meta.get("oa_only", False),
        pdf_only=meta.get("pdf_only", False),
        sort_by=sort_by if sort_by != "relevance" else "",
    )
    if sort_by == "relevance":
        from mosaic.services import sort_by_relevance as _sbr

        papers = _sbr(meta.get("query", ""), papers, _cfg())

    # Save to cache and record search history
    cache = _cache()
    if meta.get("prefer_cache"):
        rich = cache.rich_uids()
        papers = [cache.get_by_uid(p.uid) or p if p.uid in rich else p for p in papers]
    for p in papers:
        cache.save(p)
    cache.save_search(
        query=meta.get("query", ""),
        filters_json=json.dumps(meta.get("filters", {})),
        result_count=len(papers),
    )

    # Store papers for export
    current_app.config[f"export_{job_id}"] = papers

    return render_template(
        "partials/results.html",
        papers=papers,
        errors=errors,
        stats=stats,
        job_id=job_id,
        version=_version(),
    )


@bp.route("/stream/<job_id>")
def stream_job(job_id):
    """SSE endpoint — pushes progress updates until the job finishes."""

    def _generate():
        job = _jobs().get(job_id)
        if job is None:
            yield "event: done\ndata: {}\n\n"
            return
        while True:
            done = job.wait(timeout=1.0)
            progress_json = json.dumps(job.progress)
            yield f"event: progress\ndata: {progress_json}\n\n"
            if done or job.status != "running":
                yield "event: done\ndata: {}\n\n"
                return

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.route("/paper/<path:uid>")
def paper_detail(uid):
    uid = unquote(uid)
    paper = _cache().get_by_uid(uid)
    if paper is None:
        flash("Paper not found in cache.", "warning")
        return redirect(url_for("ui.search_page"))

    dl = _cache().get_download(uid)
    download_status = None
    if dl:
        download_status = {"path": dl["local_path"], "status": dl["status"]}

    return render_template(
        "detail.html", paper=paper, download_status=download_status, version=_version(), quote=quote
    )


def _manager():
    """Shared DownloadManager for the app's cache/DB."""
    from mosaic.dlqueue import get_manager

    return get_manager(_cfg(), _cache())


def _run_download(uid, cfg, db_path):
    """Executed in a worker thread — routes through the persistent queue."""
    from mosaic.db import Cache
    from mosaic.dlqueue import get_manager

    cache = Cache(db_path)
    paper = cache.get_by_uid(uid)
    if paper is None:
        return {"ok": False, "msg": "Paper not found."}

    manager = get_manager(cfg, cache)
    job_ids = manager.enqueue_papers([paper])
    if not job_ids:
        # Already downloaded (verified skip)
        rec = cache.get_download(paper.uid)
        if rec and rec["status"] == "ok":
            return {"ok": True, "msg": f"Already downloaded: {Path(rec['local_path']).name}"}
        return {"ok": False, "msg": "Could not find a downloadable PDF."}
    timeout = float(cfg.get("download", {}).get("wait_timeout", 600))
    pdf_map = manager.wait_all(job_ids, poll=0.5, timeout=timeout)
    path = pdf_map.get(paper.uid) or pdf_map.get(cache.resolve_uid(paper.uid))
    if path:
        return {"ok": True, "msg": f"PDF saved: {Path(path).name}"}
    job = cache.get_job(job_ids[0])
    # Still running after the wait budget → report it honestly instead of
    # letting the htmx spinner hang forever. The job keeps going and shows
    # up on the Downloads page.
    if job and job["status"] not in ("COMPLETED", "FAILED", "CANCELLED"):
        return {
            "ok": True,
            "msg": "Download is taking longer than expected — it keeps "
            "running in the queue; track it on the Downloads page.",
        }
    reason = (job or {}).get("last_error") or "no pdf found"
    return {"ok": False, "msg": f"Download failed: {reason}"}


@bp.route("/download/<path:uid>", methods=["POST"])
def download_paper(uid):
    uid = unquote(uid)
    paper = _cache().get_by_uid(uid)
    if paper is None:
        return "<p>Paper not found.</p>", 404

    cfg = _cfg()
    job_id = _jobs().submit(_run_download, uid, cfg, cfg["db_path"])
    status_url = url_for("ui.download_status", job_id=job_id)
    return (
        f'<div hx-get="{status_url}" hx-trigger="every 1s" hx-target="#download-status" hx-swap="innerHTML">'
        f'<span aria-busy="true">Downloading&hellip;</span></div>'
    )


@bp.route("/download/status/<job_id>")
def download_status(job_id):
    job = _jobs().get(job_id)
    if job is None:
        return "<mark>Download job not found.</mark>"

    if job.status == "running":
        status_url = url_for("ui.download_status", job_id=job_id)
        return (
            f'<div hx-get="{status_url}" hx-trigger="every 1s" hx-target="#download-status" hx-swap="innerHTML">'
            f'<span aria-busy="true">Downloading&hellip;</span></div>'
        )

    _jobs().pop(job_id)
    if job.status == "error":
        return f"<mark>Download failed: {escape(job.error_message)}</mark>"

    result = job.result
    return f"<mark>{escape(result['msg'])}</mark>"


# ---------------------------------------------------------------------------
# Download queue
# ---------------------------------------------------------------------------


@bp.route("/downloads")
def downloads_page():
    """Download queue management page."""
    return render_template("downloads.html", version=_version())


def _jobs_table():
    """Render the jobs table partial with progress data."""
    manager = _manager()
    jobs = _manager().cache.list_jobs(limit=200)
    downloaded = manager.cache.get_downloaded_uids()
    papers = {j["paper_uid"]: manager.cache.get_by_uid(j["paper_uid"]) for j in jobs}
    active = any(j["status"] in ("QUEUED", "DOWNLOADING", "VERIFYING") for j in jobs)
    return render_template(
        "partials/downloads_table.html",
        jobs=jobs,
        papers=papers,
        downloaded=downloaded,
        active=active,
        backend=manager._effective_backend(),
    )


@bp.route("/downloads/table")
def downloads_table():
    """HTMX polling target — refreshed while jobs are active."""
    return _jobs_table()


@bp.route("/downloads/enqueue", methods=["POST"])
def downloads_enqueue():
    """Enqueue selected papers (checkbox uids) or a whole result set."""
    mode = request.form.get("mode", "selected")
    manager = _manager()

    if mode == "all":
        job_id = request.form.get("job_id", "")
        papers = current_app.config.get(f"export_{job_id}") or []
    else:
        uids = request.form.getlist("uid")
        papers = [p for u in uids if (p := _cache().get_by_uid(u)) is not None]

    if not papers:
        return "<p class='stats-line'>Nothing to download.</p>"

    job_ids = manager.enqueue_papers(papers)
    skipped = len(papers) - len(job_ids)
    if not job_ids:
        return (
            f"<p class='stats-line'>All {len(papers)} selected paper(s) already "
            "downloaded (or unavailable).</p>"
        )
    return (
        f"<p class='stats-line'>{len(job_ids)} download(s) queued"
        + (f", {skipped} already downloaded" if skipped else "")
        + f' — track progress on <a href="{url_for("ui.downloads_page")}">Downloads</a>.</p>'
    )


@bp.route("/downloads/<job_id>/<action>", methods=["POST"])
def downloads_action(job_id, action):
    """Pause/resume/retry/cancel a single job; returns refreshed table."""
    manager = _manager()
    if action == "pause":
        manager.pause(job_id)
    elif action == "resume":
        manager.resume(job_id)
    elif action == "retry":
        manager.retry(job_id)
    elif action == "cancel":
        manager.cancel(job_id)
    else:
        return f"<p>Unknown action: {escape(action)}</p>", 400
    return _jobs_table()


@bp.route("/downloads/clear", methods=["POST"])
def downloads_clear():
    """Clear job records in terminal states (PDFs are never deleted)."""
    manager = _manager()
    statuses = request.form.getlist("status") or ["COMPLETED"]
    removed = manager.clear(statuses)
    table = _jobs_table()
    return f"<p class='stats-line'>{removed} job record(s) cleared.</p>" + table


# ---------------------------------------------------------------------------
# Similar
# ---------------------------------------------------------------------------


@bp.route("/similar")
def similar_page():
    identifier = request.args.get("identifier", "")
    return render_template("similar.html", identifier=identifier, version=_version())


@bp.route("/similar", methods=["POST"])
def similar_submit():
    _purge_stale()
    identifier = request.form.get("identifier", "").strip()
    if not identifier:
        return render_template(
            "partials/results.html",
            papers=[],
            errors=["Please enter a DOI or arXiv ID."],
            stats={},
            version=_version(),
        )

    max_results = _safe_int(request.form.get("max_results", 10))
    cfg = _cfg()
    oa_email = cfg.get("unpaywall", {}).get("email", "")
    ss_api_key = cfg.get("sources", {}).get("semantic_scholar", {}).get("api_key", "")

    job_id = _jobs().submit(_run_similar, identifier, max_results, oa_email, ss_api_key)
    current_app.config[f"job_meta_{job_id}"] = {
        "oa_only": request.form.get("oa_only") == "on",
        "pdf_only": request.form.get("pdf_only") == "on",
        "sort_by": request.form.get("sort_by", ""),
    }

    return render_template(
        "partials/job_status.html",
        job_id=job_id,
        source_count=2,
        status_url=url_for("ui.similar_status", job_id=job_id),
        job_type="similar",
    )


@bp.route("/similar/status/<job_id>")
def similar_status(job_id):
    job = _jobs().get(job_id)
    if job is None:
        return render_template(
            "partials/results.html",
            papers=[],
            errors=["Job not found."],
            stats={},
            version=_version(),
        )

    if job.status == "running":
        return render_template(
            "partials/job_status.html",
            job_id=job_id,
            source_count=2,
            status_url=url_for("ui.similar_status", job_id=job_id),
            job_type="similar",
        )

    if job.status == "error":
        _jobs().pop(job_id)
        return render_template(
            "partials/results.html",
            papers=[],
            errors=[job.error_message],
            stats={},
            version=_version(),
        )

    result = job.result
    papers = result["papers"]
    seed_title = result.get("seed_title")

    meta = current_app.config.pop(f"job_meta_{job_id}", {})
    sort_by = meta.get("sort_by", "")
    papers = filter_papers(
        papers,
        oa_only=meta.get("oa_only", False),
        pdf_only=meta.get("pdf_only", False),
        sort_by=sort_by if sort_by != "relevance" else "",
    )
    if sort_by == "relevance" and result.get("seed_title"):
        from mosaic.services import sort_by_relevance as _sbr

        papers = _sbr(result["seed_title"], papers, _cfg())

    cache = _cache()
    for p in papers:
        cache.save(p)

    current_app.config[f"export_{job_id}"] = papers

    return render_template(
        "partials/results.html",
        papers=papers,
        errors=[],
        stats={},
        seed_title=seed_title,
        job_id=job_id,
        version=_version(),
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@bp.route("/config")
def config_page():
    import mosaic.config as cfg_mod

    cfg = cfg_mod.load()
    return render_template("config.html", cfg=cfg, version=_version())


@bp.route("/config", methods=["POST"])
def config_save():
    import mosaic.config as cfg_mod

    cfg = cfg_mod.load()

    # General settings
    dl_dir = request.form.get("download_dir", "").strip()
    if dl_dir:
        cfg["download_dir"] = dl_dir
    fn_pattern = request.form.get("filename_pattern", "").strip()
    if fn_pattern:
        cfg["filename_pattern"] = fn_pattern
    rate_limit = request.form.get("rate_limit_delay", "").strip()
    if rate_limit:
        try:
            cfg["rate_limit_delay"] = float(rate_limit)
        except ValueError:
            pass

    # API keys — shared registry with CLI
    from mosaic.config import API_KEY_PATHS, apply_api_keys

    apply_api_keys(cfg, {k: request.form.get(k, "") for k, _ in API_KEY_PATHS})

    # Also set PMC key to same as PubMed NCBI key
    ncbi_val = request.form.get("ncbi_key", "").strip()
    if ncbi_val:
        cfg.setdefault("sources", {}).setdefault("pmc", {})["api_key"] = ncbi_val

    # Unpaywall email
    email = request.form.get("unpaywall_email", "").strip()
    if email:
        cfg.setdefault("unpaywall", {})["email"] = email

    # Zotero
    zotero_key = request.form.get("zotero_key", "").strip()
    if zotero_key:
        cfg.setdefault("zotero", {})["api_key"] = zotero_key

    # Download settings
    if request.form.get("_download_section"):
        dl = cfg.setdefault("download", {})
        backend = request.form.get("download_backend", "auto").strip()
        if backend in ("native", "aria2", "auto"):
            dl["backend"] = backend
        for key, form_key, cast in (
            ("max_concurrent", "download_max_concurrent", int),
            ("per_host_concurrent", "download_per_host", int),
            ("per_host_delay", "download_per_host_delay", float),
            ("max_tries", "download_max_tries", int),
        ):
            raw = request.form.get(form_key, "").strip()
            if raw:
                try:
                    dl[key] = cast(raw)
                except ValueError:
                    pass
        dl["verify_pdf"] = request.form.get("download_verify_pdf") == "on"
        dl["skip_existing"] = request.form.get("download_skip_existing") == "on"
        rpc_url = request.form.get("download_aria2_rpc_url", "").strip()
        if rpc_url:
            dl["aria2_rpc_url"] = rpc_url
        secret = request.form.get("download_aria2_rpc_secret", "").strip()
        if secret:
            dl["aria2_rpc_secret"] = secret
        dl["aria2_auto_start"] = request.form.get("download_aria2_auto_start") == "on"
        dl["aria2_fallback_native"] = request.form.get("download_aria2_fallback") == "on"
        dl["aria2_max_download_limit"] = request.form.get(
            "download_aria2_max_download_limit", ""
        ).strip()
        # Live-update the running manager so new settings apply immediately.
        try:
            from mosaic.dlqueue import get_manager

            get_manager(cfg, _cache()).update_cfg(cfg)
        except Exception:
            pass

    # Source toggles — only update if the form actually included the sources
    # section (HTML checkboxes are absent when unchecked; a hidden sentinel
    # field tells us the section was present in the submitted form).
    if request.form.get("_sources_section"):
        src_cfg = cfg.get("sources", {})
        all_cfg_keys = set(SHORTHAND_TO_CFG_KEY.values())
        enabled_sources = request.form.getlist("enabled_sources")
        for cfg_key in all_cfg_keys:
            src_cfg.setdefault(cfg_key, {})["enabled"] = cfg_key in enabled_sources

    # PEDro settings (separate from the enabled toggle)
    if request.form.get("_pedro_section"):
        pedro_cfg = cfg.setdefault("sources", {}).setdefault("pedro", {})
        pedro_cfg["acknowledge_fair_use"] = request.form.get("pedro_acknowledge_fair_use") == "on"
        pedro_cfg["fetch_details"] = request.form.get("pedro_fetch_details") == "on"
        pedro_delay = request.form.get("pedro_rate_limit_delay", "").strip()
        if pedro_delay:
            try:
                pedro_cfg["rate_limit_delay"] = float(pedro_delay)
            except ValueError:
                pass

    # Obsidian
    if request.form.get("_obsidian_section"):
        obs = cfg.setdefault("obsidian", {})
        vault_path = request.form.get("obsidian_vault_path", "").strip()
        obs["vault_path"] = vault_path
        subfolder = request.form.get("obsidian_subfolder", "papers").strip()
        obs["subfolder"] = subfolder
        fn_pattern = request.form.get("obsidian_filename_pattern", "").strip()
        if fn_pattern:
            obs["filename_pattern"] = fn_pattern
        tags_raw = request.form.get("obsidian_tags", "paper").strip()
        obs["tags"] = [t.strip() for t in tags_raw.split(",") if t.strip()] or ["paper"]
        obs["wikilinks"] = request.form.get("obsidian_wikilinks") == "on"

    # LLM settings
    if request.form.get("_llm_section"):
        llm = cfg.setdefault("llm", {})
        provider = request.form.get("llm_provider", "").strip()
        llm["provider"] = provider
        api_key = request.form.get("llm_api_key", "").strip()
        if api_key:
            llm["api_key"] = api_key
        model = request.form.get("llm_model", "").strip()
        llm["model"] = model
        base_url = request.form.get("llm_base_url", "").strip()
        llm["base_url"] = base_url

    # RAG / embedding settings
    if request.form.get("_rag_section"):
        rag = cfg.setdefault("rag", {})
        emb_provider = request.form.get("rag_embedding_provider", "").strip()
        rag["embedding_provider"] = emb_provider
        emb_model = request.form.get("rag_embedding_model", "").strip()
        rag["embedding_model"] = emb_model
        emb_base_url = request.form.get("rag_embedding_base_url", "").strip()
        rag["embedding_base_url"] = emb_base_url
        emb_api_key = request.form.get("rag_embedding_api_key", "").strip()
        if emb_api_key:
            rag["embedding_api_key"] = emb_api_key
        top_k = request.form.get("rag_top_k", "").strip()
        if top_k:
            try:
                rag["top_k"] = int(top_k)
            except ValueError:
                pass
        rag["auto_index"] = request.form.get("rag_auto_index") == "on"

    # Advanced: db_path
    db_path = request.form.get("db_path", "").strip()
    if db_path:
        cfg["db_path"] = db_path

    cfg_mod.save(cfg)

    # Refresh app config
    current_app.config["MOSAIC_CFG"] = cfg

    if request.headers.get("HX-Request"):
        return '<article style="padding:.5rem 1rem;"><ins>Configuration saved.</ins></article>'

    flash("Configuration saved.", "success")
    return redirect(url_for("ui.config_page"))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@bp.route("/export/<job_id>")
def export_results(job_id):
    fmt = request.args.get("format", "csv")
    papers = current_app.config.get(f"export_{job_id}")
    if not papers:
        flash("Export results have expired. Please re-run your search.", "warning")
        return redirect(url_for("ui.search_page"))

    from mosaic.exporter import export

    ext_map = {"csv": ".csv", "json": ".json", "bib": ".bib", "md": ".md"}
    ext = ext_map.get(fmt, ".csv")
    mime_map = {
        ".csv": "text/csv",
        ".json": "application/json",
        ".bib": "application/x-bibtex",
        ".md": "text/markdown",
    }

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp_path = Path(tmp.name)

    export(papers, tmp_path)
    return send_file(
        tmp_path,
        mimetype=mime_map.get(ext, "text/plain"),
        as_attachment=True,
        download_name=f"mosaic_results{ext}",
    )


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@bp.route("/history")
def history_page():
    searches = _cache().list_searches(limit=50)
    for s in searches:
        try:
            s["filters"] = json.loads(s.get("filters_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            s["filters"] = {}
    return render_template("history.html", searches=searches, version=_version())


# ---------------------------------------------------------------------------
# Zotero export
# ---------------------------------------------------------------------------


def _run_zotero_export(papers_data, cfg, collection_name):
    """Executed in a worker thread."""
    from mosaic.workflows import push_to_zotero

    papers = [Paper.from_dict(d) for d in papers_data]
    return push_to_zotero(papers, cfg, collection_name=collection_name)


@bp.route("/zotero/export/<job_id>", methods=["POST"])
def zotero_export(job_id):
    papers = current_app.config.get(f"export_{job_id}")
    if not papers:
        return "<mark>Export results have expired. Please re-run your search.</mark>"

    cfg = _cfg()
    collection_name = request.form.get("zotero_collection", "").strip()

    # Serialise Paper objects for the worker thread
    papers_data = [p.to_dict() for p in papers]
    zot_job_id = _jobs().submit(_run_zotero_export, papers_data, cfg, collection_name)
    status_url = url_for("ui.zotero_export_status", job_id=zot_job_id)
    return (
        f'<div hx-get="{status_url}" hx-trigger="every 1s" hx-target="#zotero-status" hx-swap="innerHTML">'
        f'<span aria-busy="true">Sending to Zotero&hellip;</span></div>'
    )


@bp.route("/zotero/export/status/<job_id>")
def zotero_export_status(job_id):
    job = _jobs().get(job_id)
    if job is None:
        return "<mark>Zotero export job not found.</mark>"
    if job.status == "running":
        status_url = url_for("ui.zotero_export_status", job_id=job_id)
        return (
            f'<div hx-get="{status_url}" hx-trigger="every 1s" hx-target="#zotero-status" hx-swap="innerHTML">'
            f'<span aria-busy="true">Sending to Zotero&hellip;</span></div>'
        )
    _jobs().pop(job_id)
    if job.status == "error":
        return f"<mark>Zotero export failed: {escape(job.error_message)}</mark>"
    result = job.result
    if result["ok"]:
        return f"<ins>{escape(result['msg'])}</ins>"
    return f"<mark>{escape(result['msg'])}</mark>"


@bp.route("/zotero/paper/<path:uid>", methods=["POST"])
def zotero_export_paper(uid):
    uid = unquote(uid)
    paper = _cache().get_by_uid(uid)
    if paper is None:
        return "<mark>Paper not found.</mark>"

    cfg = _cfg()
    collection_name = request.form.get("zotero_collection", "").strip()
    papers_data = [paper.to_dict()]
    zot_job_id = _jobs().submit(_run_zotero_export, papers_data, cfg, collection_name)
    status_url = url_for("ui.zotero_export_status", job_id=zot_job_id)
    return (
        f'<div hx-get="{status_url}" hx-trigger="every 1s" hx-target="#zotero-status" hx-swap="innerHTML">'
        f'<span aria-busy="true">Sending to Zotero&hellip;</span></div>'
    )


# ---------------------------------------------------------------------------
# Obsidian export
# ---------------------------------------------------------------------------


def _run_obsidian_export(papers_data, cfg):
    """Executed in a worker thread."""
    from mosaic.workflows import push_to_obsidian

    papers = [Paper.from_dict(d) for d in papers_data]
    return push_to_obsidian(papers, cfg)


@bp.route("/obsidian/export/<job_id>", methods=["POST"])
def obsidian_export(job_id):
    papers = current_app.config.get(f"export_{job_id}")
    if not papers:
        return "<mark>Export results have expired. Please re-run your search.</mark>"

    cfg = _cfg()
    papers_data = [p.to_dict() for p in papers]
    obs_job_id = _jobs().submit(_run_obsidian_export, papers_data, cfg)
    status_url = url_for("ui.obsidian_export_status", job_id=obs_job_id)
    return (
        f'<div hx-get="{status_url}" hx-trigger="every 1s" hx-target="#obsidian-status" hx-swap="innerHTML">'
        f'<span aria-busy="true">Exporting to Obsidian&hellip;</span></div>'
    )


@bp.route("/obsidian/export/status/<job_id>")
def obsidian_export_status(job_id):
    job = _jobs().get(job_id)
    if job is None:
        return "<mark>Obsidian export job not found.</mark>"
    if job.status == "running":
        status_url = url_for("ui.obsidian_export_status", job_id=job_id)
        return (
            f'<div hx-get="{status_url}" hx-trigger="every 1s" hx-target="#obsidian-status" hx-swap="innerHTML">'
            f'<span aria-busy="true">Exporting to Obsidian&hellip;</span></div>'
        )
    _jobs().pop(job_id)
    if job.status == "error":
        return f"<mark>Obsidian export failed: {escape(job.error_message)}</mark>"
    result = job.result
    if result["ok"]:
        return f"<ins>{escape(result['msg'])}</ins>"
    return f"<mark>{escape(result['msg'])}</mark>"


# ---------------------------------------------------------------------------
# NotebookLM
# ---------------------------------------------------------------------------


def _run_notebook_from_query(name, query, max_results, filters, oa_only, pdf_only, artifacts, cfg):
    """Executed in a worker thread — search → download → create notebook."""
    import asyncio

    from mosaic.db import Cache
    from mosaic.downloader import download as dl_paper
    from mosaic.notebooklm_bridge import _require_notebooklm, create_notebook

    _require_notebooklm()

    sources = build_sources(cfg)
    errors: list[str] = []
    papers = search_all(sources, query, max_per_source=max_results, filters=filters, errors=errors)
    papers = filter_papers(papers, oa_only=oa_only, pdf_only=pdf_only)

    if not papers:
        return {"ok": False, "msg": "No papers found for this query."}

    cache = Cache(cfg["db_path"])
    email = cfg.get("unpaywall", {}).get("email", "")
    dl_dir = cfg["download_dir"]
    pattern = cfg.get("filename_pattern", "{year}_{source}_{author}_{title}")

    papers_with_paths = []
    for p in papers:
        path = dl_paper(p, dl_dir, cache, email, pattern)
        papers_with_paths.append((p, Path(path) if path else None))

    nb_id = asyncio.run(create_notebook(name, papers_with_paths, artifacts=artifacts))
    added = sum(1 for _, path in papers_with_paths if path)
    return {
        "ok": True,
        "nb_id": nb_id,
        "paper_count": len(papers),
        "downloaded": added,
    }


def _run_notebook_from_dir(name, from_dir, artifacts, cfg):
    """Executed in a worker thread — import PDFs from directory."""
    import asyncio

    from mosaic.notebooklm_bridge import _require_notebooklm, create_notebook_from_dir

    _require_notebooklm()

    directory = Path(from_dir).expanduser()
    if not directory.is_dir():
        return {"ok": False, "msg": f"Directory not found: {from_dir}"}

    try:
        nb_id = asyncio.run(create_notebook_from_dir(name, directory, artifacts=artifacts))
    except ValueError as e:
        return {"ok": False, "msg": str(e)}

    return {"ok": True, "nb_id": nb_id}


@bp.route("/notebook")
def notebook_page():
    from mosaic.notebooklm_bridge import check_notebooklm_status

    nb_status = check_notebooklm_status()
    return render_template("notebook.html", version=_version(), nb_status=nb_status)


@bp.route("/notebook", methods=["POST"])
def notebook_submit():
    _purge_stale()
    name = request.form.get("name", "").strip()
    if not name:
        return "<article>Please enter a notebook name.</article>"

    input_mode = request.form.get("input_mode", "query")
    artifacts: set[str] = set(request.form.getlist("artifacts"))
    cfg = _cfg()

    if input_mode == "dir":
        from_dir = request.form.get("from_dir", "").strip()
        if not from_dir:
            return "<article>Please enter a PDF directory path.</article>"
        job_id = _jobs().submit(_run_notebook_from_dir, name, from_dir, artifacts, cfg)
    else:
        query = request.form.get("query", "").strip()
        if not query:
            return "<article>Please enter a search query.</article>"
        max_results = _safe_int(request.form.get("max_results", 10))
        filters, year_warning = _build_filters(request.form)
        if year_warning:
            return f"<article>{year_warning}</article>"
        oa_only = request.form.get("oa_only") == "on"
        pdf_only = request.form.get("pdf_only") == "on"
        job_id = _jobs().submit(
            _run_notebook_from_query,
            name,
            query,
            max_results,
            filters,
            oa_only,
            pdf_only,
            artifacts,
            cfg,
        )

    status_url = url_for("ui.notebook_status", job_id=job_id)
    return (
        f'<div hx-get="{status_url}" hx-trigger="every 2s" hx-target="#nb-results" hx-swap="innerHTML">'
        f'<article aria-busy="true">Creating notebook <strong>{escape(name)}</strong>&hellip; '
        f"(this may take a minute)</article></div>"
    )


@bp.route("/notebook/status/<job_id>")
def notebook_status(job_id):
    job = _jobs().get(job_id)
    if job is None:
        return "<article>Job not found.</article>"

    if job.status == "running":
        status_url = url_for("ui.notebook_status", job_id=job_id)
        return (
            f'<div hx-get="{status_url}" hx-trigger="every 2s" hx-target="#nb-results" hx-swap="innerHTML">'
            f'<article aria-busy="true">Creating notebook&hellip; (this may take a minute)</article></div>'
        )

    _jobs().pop(job_id)
    if job.status == "error":
        return (
            f"<article><mark>Notebook creation failed: {escape(job.error_message)}</mark></article>"
        )

    result = job.result
    if not result["ok"]:
        return f"<article><mark>{escape(result['msg'])}</mark></article>"

    nb_id = result["nb_id"]
    nb_url = f"https://notebooklm.google.com/notebook/{nb_id}"
    lines = ["<ins>Notebook created successfully!</ins>"]
    if "paper_count" in result:
        lines.append(
            f"<p>{result['paper_count']} paper(s) found, {result['downloaded']} PDF(s) added.</p>"
        )
    lines.append(
        f'<p><a href="{nb_url}" target="_blank" rel="noopener">Open in NotebookLM &rarr;</a></p>'
    )
    return "".join(lines)


# ---------------------------------------------------------------------------
# Bulk download
# ---------------------------------------------------------------------------


@bp.route("/bulk")
def bulk_page():
    return render_template("bulk.html", version=_version())


def _run_bulk_download(dois, cfg, db_path):
    """Executed in a worker thread — routes through the persistent queue."""
    from mosaic.db import Cache
    from mosaic.errors import DownloadError
    from mosaic.workflows import download_papers

    cache = Cache(db_path)
    papers = [Paper(title=doi, doi=doi, source="manual") for doi in dois]
    try:
        pdf_map = download_papers(papers, cfg, cache)
    except DownloadError as e:
        return {"ok": 0, "fail": len(dois), "results": [], "error": str(e)}

    ok = fail = 0
    results = []
    for doi, paper in zip(dois, papers, strict=True):
        path = pdf_map.get(paper.uid)
        if path:
            ok += 1
            results.append({"doi": doi, "status": "ok", "path": Path(path).name})
        else:
            fail += 1
            results.append({"doi": doi, "status": "fail", "path": ""})

    return {"ok": ok, "fail": fail, "results": results}


@bp.route("/bulk", methods=["POST"])
def bulk_submit():
    _purge_stale()

    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return "<article>Please select a .bib or .csv file.</article>"

    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in (".bib", ".csv"):
        return "<article>Unsupported file type. Use .bib or .csv.</article>"

    # Save to a temp file for processing
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        content = uploaded.read(_MAX_UPLOAD_BYTES + 1)
        if len(content) > _MAX_UPLOAD_BYTES:
            return "<article>File too large (max 10 MB).</article>"
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        from mosaic.bulk import read_dois

        dois = read_dois(tmp_path)
    except ValueError as e:
        return f"<article>{e}</article>"
    finally:
        tmp_path.unlink(missing_ok=True)

    if not dois:
        return "<article>No DOIs found in the uploaded file.</article>"

    cfg = _cfg()
    job_id = _jobs().submit(_run_bulk_download, dois, cfg, cfg["db_path"])
    current_app.config[f"job_meta_{job_id}"] = {"doi_count": len(dois)}

    status_url = url_for("ui.bulk_status", job_id=job_id)
    return (
        f'<div hx-get="{status_url}" hx-trigger="every 2s" hx-target="#results" hx-swap="innerHTML">'
        f'<article aria-busy="true">Downloading {len(dois)} DOI(s)&hellip;</article></div>'
    )


@bp.route("/bulk/status/<job_id>")
def bulk_status(job_id):
    job = _jobs().get(job_id)
    if job is None:
        return "<article>Job not found.</article>"

    if job.status == "running":
        meta = current_app.config.get(f"job_meta_{job_id}", {})
        count = meta.get("doi_count", "?")
        status_url = url_for("ui.bulk_status", job_id=job_id)
        return (
            f'<div hx-get="{status_url}" hx-trigger="every 2s" hx-target="#results" hx-swap="innerHTML">'
            f'<article aria-busy="true">Downloading {count} DOI(s)&hellip;</article></div>'
        )

    current_app.config.pop(f"job_meta_{job_id}", None)
    _jobs().pop(job_id)

    if job.status == "error":
        return f"<article>Bulk download failed: {escape(job.error_message)}</article>"

    result = job.result
    if result.get("error"):
        return f"<article>Bulk download failed: {escape(result['error'])}</article>"
    html = f"<p><strong>Done:</strong> {result['ok']} downloaded, {result['fail']} failed.</p>"
    if result["results"]:
        html += '<table role="grid"><thead><tr><th>DOI</th><th>Status</th><th>File</th></tr></thead><tbody>'
        for r in result["results"]:
            icon = (
                '<span class="badge-oa">&#10003;</span>'
                if r["status"] == "ok"
                else '<span class="badge-closed">&#10007;</span>'
            )
            html += (
                f"<tr><td>{escape(r['doi'])}</td><td>{icon}</td><td>{escape(r['path'])}</td></tr>"
            )
        html += "</tbody></table>"
    return html


# ---------------------------------------------------------------------------
# RAG — Index, Ask, Chat
# ---------------------------------------------------------------------------

# In-process chat history: session_id → list of {"role": "user"|"assistant", "content": str}
_chat_histories: dict[str, list[dict]] = {}


def _rag_index_status(cache):
    """Return dict with index stats for the template."""
    total = cache.count_papers()
    indexed = len(cache.get_indexed_uids())
    model = cache.get_rag_meta("embedding_model") or ""
    return {"total_papers": total, "indexed_count": indexed, "embedding_model": model}


@bp.route("/rag")
def rag_landing():
    cache = _cache()
    info = _rag_index_status(cache)
    cfg = _cfg()
    llm_configured = bool(cfg.get("llm", {}).get("provider") and cfg.get("llm", {}).get("api_key"))
    return render_template(
        "rag_landing.html", version=_version(), llm_configured=llm_configured, **info
    )


@bp.route("/rag/index")
def rag_index_page():
    cache = _cache()
    info = _rag_index_status(cache)
    return render_template("rag_index.html", version=_version(), **info)


def _run_rag_index(cfg, db_path, reindex, batch_size):
    """Executed in a worker thread."""
    from mosaic.db import Cache
    from mosaic.rag import index_papers

    cache = Cache(db_path)
    papers = cache.list_papers(limit=100_000)
    newly, skipped = index_papers(papers, cfg, cache, reindex=reindex, progress=False)
    return {"newly": newly, "skipped": skipped, "total": len(papers)}


@bp.route("/rag/index", methods=["POST"])
def rag_index_submit():
    cfg = _cfg()
    reindex = request.form.get("reindex") == "on"
    batch_size = _safe_int(request.form.get("batch_size", 96), default=96, lo=1, hi=512)
    job_id = _jobs().submit(_run_rag_index, cfg, cfg["db_path"], reindex, batch_size)
    status_url = url_for("ui.rag_index_status", job_id=job_id)
    return (
        f'<div hx-get="{status_url}" hx-trigger="every 2s" hx-target="#index-job" hx-swap="innerHTML">'
        f'<article aria-busy="true">Indexing papers&hellip;</article></div>'
    )


@bp.route("/rag/index/status/<job_id>")
def rag_index_status(job_id):
    job = _jobs().get(job_id)
    if job is None:
        return "<article>Job not found.</article>"
    if job.status == "running":
        status_url = url_for("ui.rag_index_status", job_id=job_id)
        return (
            f'<div hx-get="{status_url}" hx-trigger="every 2s" hx-target="#index-job" hx-swap="innerHTML">'
            f'<article aria-busy="true">Indexing papers&hellip;</article></div>'
        )
    _jobs().pop(job_id)
    if job.status == "error":
        return f"<article><mark>Indexing failed: {escape(job.error_message)}</mark></article>"
    r = job.result
    return (
        f"<article><ins>Done.</ins> {r['newly']} paper(s) newly indexed, "
        f"{r['skipped']} skipped (already indexed), {r['total']} total in cache.</article>"
    )


@bp.route("/rag/ask")
def rag_ask_page():
    return render_template("rag_ask.html", version=_version())


def _run_rag_ask(query, cfg, db_path, mode, top_k, subset_query, year):
    """Executed in a worker thread — full RAG pipeline."""
    from mosaic.db import Cache
    from mosaic.rag import ask
    from mosaic.services import build_filters

    cache = Cache(db_path)
    pre_filter = None
    if subset_query or year:
        filters, _ = build_filters(year=year)
        candidates = (
            cache.search_local(subset_query) if subset_query else cache.list_papers(limit=100_000)
        )
        if filters:
            candidates = [p for p in candidates if filters.match(p)]
        pre_filter = [p.uid for p in candidates]

    answer, papers = ask(query, cfg, cache, mode=mode, k=top_k or None, pre_filter=pre_filter)
    return {"answer": answer, "papers": [p.to_dict() for p in papers]}


@bp.route("/rag/ask", methods=["POST"])
def rag_ask_submit():
    query = request.form.get("query", "").strip()
    if not query:
        return "<article>Please enter a question.</article>"

    cfg = _cfg()
    mode = request.form.get("mode", "synthesis")
    top_k_raw = request.form.get("top_k", "").strip()
    top_k = int(top_k_raw) if top_k_raw.isdigit() else None
    subset_query = request.form.get("subset_query", "").strip()
    year = request.form.get("year", "").strip()
    show_sources = request.form.get("show_sources") == "on"

    job_id = _jobs().submit(
        _run_rag_ask, query, cfg, cfg["db_path"], mode, top_k, subset_query, year
    )
    current_app.config[f"job_meta_{job_id}"] = {"show_sources": show_sources}
    status_url = url_for("ui.rag_ask_status", job_id=job_id)
    return (
        f'<div hx-get="{status_url}" hx-trigger="every 2s" hx-target="#ask-result" hx-swap="innerHTML">'
        f'<article aria-busy="true">Retrieving papers and generating answer&hellip;</article></div>'
    )


@bp.route("/rag/ask/status/<job_id>")
def rag_ask_status(job_id):
    job = _jobs().get(job_id)
    if job is None:
        return "<article>Job not found.</article>"
    if job.status == "running":
        status_url = url_for("ui.rag_ask_status", job_id=job_id)
        return (
            f'<div hx-get="{status_url}" hx-trigger="every 2s" hx-target="#ask-result" hx-swap="innerHTML">'
            f'<article aria-busy="true">Retrieving papers and generating answer&hellip;</article></div>'
        )
    meta = current_app.config.pop(f"job_meta_{job_id}", {})
    _jobs().pop(job_id)
    if job.status == "error":
        return f"<article><mark>Error: {escape(job.error_message)}</mark></article>"

    result = job.result
    answer = result["answer"]
    papers = result["papers"]
    show_sources = meta.get("show_sources", True)

    html = f'<article><div class="rag-answer">{escape(answer)}</div></article>'
    if show_sources and papers:
        html += '<details open><summary><strong>Source papers</strong></summary><ol class="source-list">'
        for p in papers:
            from mosaic.models import Paper as _Paper

            pobj = _Paper.from_dict(p)
            title = escape(pobj.title or "Untitled")
            authors = escape(", ".join(pobj.authors[:3]))
            year_str = escape(str(pobj.year or ""))
            detail_url = url_for("ui.paper_detail", uid=pobj.uid)
            html += f'<li><a href="{detail_url}">{title}</a> &mdash; {authors} ({year_str})</li>'
        html += "</ol></details>"
    return html


@bp.route("/rag/chat")
def rag_chat_page():
    from flask import session

    session.permanent = True
    sid = session.setdefault("chat_id", str(__import__("uuid").uuid4()))
    history = _chat_histories.get(sid, [])
    return render_template("rag_chat.html", version=_version(), history=history)


def _run_rag_chat(query, cfg, db_path, mode):
    """Executed in a worker thread."""
    from mosaic.db import Cache
    from mosaic.rag import ask

    cache = Cache(db_path)
    answer, _ = ask(query, cfg, cache, mode=mode)
    return {"answer": answer}


def _render_chat_thread(history, thinking: bool = False) -> str:
    """Return HTML for the chat thread content."""
    if not history and not thinking:
        return '<p class="stats-line"><em>No messages yet. Ask your first question below.</em></p>'
    parts = []
    for msg in history:
        role = escape(msg["role"])
        content = escape(msg["content"])
        parts.append(f'<div class="chat-msg chat-{role}">{content}</div>')
    if thinking:
        parts.append(
            '<div class="chat-msg chat-assistant" style="opacity:.6;">'
            '<span aria-busy="true">Thinking&hellip;</span></div>'
        )
    return "\n".join(parts)


def _oob_poll(status_url: str) -> str:
    """OOB swap that keeps the poll trigger alive outside #chat-thread."""
    return (
        f'<div id="chat-poll" hx-swap-oob="true">'
        f'<div hx-get="{status_url}" hx-trigger="every 2s"'
        f' hx-target="#chat-thread" hx-swap="innerHTML"></div>'
        f"</div>"
    )


def _oob_poll_stop() -> str:
    """OOB swap that clears #chat-poll, stopping the poll loop."""
    return '<div id="chat-poll" hx-swap-oob="true"></div>'


@bp.route("/rag/chat/send", methods=["POST"])
def rag_chat_send():
    from flask import session

    query = request.form.get("query", "").strip()
    mode = request.form.get("mode", "synthesis")
    sid = session.get("chat_id", "default")
    history = _chat_histories.setdefault(sid, [])

    if not query:
        return _render_chat_thread(history)

    history.append({"role": "user", "content": query})
    cfg = _cfg()
    job_id = _jobs().submit(_run_rag_chat, query, cfg, cfg["db_path"], mode)
    current_app.config[f"job_meta_{job_id}"] = {"sid": sid}

    # Main swap → #chat-thread; OOB swap → #chat-poll (outside thread, survives swaps)
    status_url = url_for("ui.rag_chat_status", job_id=job_id)
    return _render_chat_thread(history, thinking=True) + "\n" + _oob_poll(status_url)


@bp.route("/rag/chat/status/<job_id>")
def rag_chat_status(job_id):
    from flask import session

    job = _jobs().get(job_id)
    if job is None:
        sid = session.get("chat_id", "default")
        return _render_chat_thread(_chat_histories.get(sid, [])) + "\n" + _oob_poll_stop()

    if job.status == "running":
        meta = current_app.config.get(f"job_meta_{job_id}", {})
        sid = meta.get("sid", session.get("chat_id", "default"))
        history = _chat_histories.get(sid, [])
        status_url = url_for("ui.rag_chat_status", job_id=job_id)
        return _render_chat_thread(history, thinking=True) + "\n" + _oob_poll(status_url)

    meta = current_app.config.pop(f"job_meta_{job_id}", {})
    _jobs().pop(job_id)
    sid = meta.get("sid", session.get("chat_id", "default"))
    history = _chat_histories.setdefault(sid, [])

    if job.status == "error":
        history.append({"role": "assistant", "content": f"[Error: {job.error_message}]"})
    else:
        history.append({"role": "assistant", "content": job.result["answer"]})

    return _render_chat_thread(history) + "\n" + _oob_poll_stop()


@bp.route("/rag/chat/clear", methods=["POST"])
def rag_chat_clear():
    from flask import session

    sid = session.get("chat_id", "default")
    _chat_histories.pop(sid, None)
    return '<p class="stats-line"><em>Conversation cleared.</em></p>' + "\n" + _oob_poll_stop()


# ---------------------------------------------------------------------------
# Auth sessions
# ---------------------------------------------------------------------------


@bp.route("/sessions")
def sessions_page():
    from mosaic.auth import list_sessions

    sessions = list_sessions()
    return render_template("sessions.html", sessions=sessions, version=_version())


@bp.route("/sessions/delete/<name>", methods=["POST"])
def session_delete(name):
    from mosaic.auth import delete_session

    if delete_session(name):
        flash(f"Session '{name}' deleted.", "success")
    else:
        flash(f"No session found for '{name}'.", "warning")
    return redirect(url_for("ui.sessions_page"))


# ---------------------------------------------------------------------------
# JSON collection plans
# ---------------------------------------------------------------------------

# Per-plan stop events — set by the Stop button, honoured between tasks.
_collect_stops: dict = {}


def _collect_store(plan_id: str):
    from mosaic.collection.state import CollectionStore

    return CollectionStore(_cache(), plan_id)


def _plan_cards() -> list[dict]:
    """All registered collection plans with task counts."""
    cache = _cache()
    try:
        rows = cache.con.execute(
            "SELECT * FROM collection_plans ORDER BY updated_at DESC"
        ).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        rec = dict(r)
        counts = cache.con.execute(
            "SELECT status, COUNT(*) c FROM collection_tasks WHERE plan_id=? GROUP BY status",
            (rec["plan_id"],),
        ).fetchall()
        rec["counts"] = {x["status"]: x["c"] for x in counts}
        out.append(rec)
    return out


@bp.route("/collection")
def collection_page():
    return render_template("collection.html", plans=_plan_cards(), version=_version())


def _validate_plan_file(plan_path: Path):
    """Return (plan, error_html)."""
    from mosaic.collection.loader import PlanError, load_plan

    try:
        return load_plan(plan_path), None
    except PlanError as e:
        items = "".join(f"<li>{escape(p)}</li>" for p in e.problems[:15])
        return None, f"<mark>Invalid collection plan:</mark><ul>{items}</ul>"


def _register_plan(plan) -> None:
    """Persist plan + task rows so the UI can show/track state."""
    from mosaic.collection.runner import CollectionRunner, options_from_plan

    opts = options_from_plan(plan)
    runner = CollectionRunner(plan, _cfg(), _cache(), opts)
    runner.register()


@bp.route("/collection/load", methods=["POST"])
def collection_load():
    """Load a plan from a server-side path or an uploaded JSON file."""
    plan_file = request.files.get("plan_file")
    plan_path_str = request.form.get("plan_path", "").strip()

    if plan_file and plan_file.filename:
        if not plan_file.filename.endswith(".json"):
            return "<mark>Please upload a .json collection plan.</mark>"
        data = plan_file.read(_MAX_UPLOAD_BYTES + 1)
        if len(data) > _MAX_UPLOAD_BYTES:
            return "<mark>Plan file exceeds 10 MB.</mark>"
        plans_dir = Path(_cfg()["db_path"]).parent / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        dest = plans_dir / Path(plan_file.filename).name
        dest.write_bytes(data)
        plan_path = dest
    elif plan_path_str:
        plan_path = Path(plan_path_str).expanduser()
    else:
        return "<mark>Provide a plan path or upload a JSON file.</mark>"

    plan, err = _validate_plan_file(plan_path)
    if err:
        return err
    _register_plan(plan)
    return render_template(
        "partials/collection_plan.html",
        plan=plan,
        opts=_collect_opts(plan.plan_id),
        running=False,
        tasks=_collect_store(plan.plan_id).list_tasks(),
        version=_version(),
    )


def _collect_opts(plan_id: str) -> dict:
    return current_app.config.get(f"collection_opts_{plan_id}", {})


def _run_collection_ui(plan_path, task_ids, opts_over, stop_event, cfg, db_path, progress):
    """Worker: run (a subset of) a collection plan in this thread."""
    from mosaic.collection.loader import load_plan
    from mosaic.collection.reporting import export_manifest
    from mosaic.collection.runner import CollectionRunner, options_from_plan
    from mosaic.db import Cache

    plan = load_plan(plan_path)
    cache = Cache(db_path)
    cfg = dict(cfg)
    cfg["download"] = dict(
        cfg.get("download", {}),
        backend=opts_over.get("backend") or cfg.get("download", {}).get("backend", "auto"),
    )
    opts = options_from_plan(plan, opts_over)
    runner = CollectionRunner(plan, cfg, cache, opts)
    runner.stop_event = stop_event
    tasks = plan.select(task_ids=set(task_ids) if task_ids else None)

    def _cb(task, status, skipped=False):
        progress[task.task_id] = status

    results = runner.run(tasks, retry_failed=opts_over.get("retry_failed", False), progress_cb=_cb)
    report = export_manifest(runner.store, runner.root)
    return {"results": results, "report": report}


@bp.route("/collection/<plan_id>/run", methods=["POST"])
def collection_run(plan_id):
    """Start/resume a plan as a background job (single task or all)."""
    _purge_stale()
    store = _collect_store(plan_id)
    rec = store.get_plan()
    if not rec or not rec.get("path"):
        return "<mark>Plan not registered — load the JSON first.</mark>"

    task_id = request.form.get("task_id", "").strip() or None
    retry_failed = request.form.get("retry_failed") == "1"
    output_dir = request.form.get("output_dir", "").strip()
    max_pdfs = _safe_int(request.form.get("max_pdfs_per_task", 0), default=0, lo=0, hi=20)

    opts_over = {
        "output_dir": output_dir or None,
        "max_pdfs_per_task": max_pdfs or None,
        "retry_failed": retry_failed,
    }
    current_app.config[f"collection_opts_{plan_id}"] = opts_over

    progress: dict = {}
    import threading

    stop_event = threading.Event()
    _collect_stops[plan_id] = stop_event
    cfg = _cfg()
    job_id = _jobs().submit(
        _run_collection_ui,
        rec["path"],
        [task_id] if task_id else [],
        opts_over,
        stop_event,
        cfg,
        cfg["db_path"],
        progress,
    )
    job = _jobs().get(job_id)
    if job is not None:
        job.progress = progress
    current_app.config[f"collection_job_{plan_id}"] = job_id
    return _collection_plan_partial(plan_id)


@bp.route("/collection/<plan_id>/stop", methods=["POST"])
def collection_stop(plan_id):
    """Pause the running plan at the next task boundary."""
    ev = _collect_stops.get(plan_id)
    if ev:
        ev.set()
    return _collection_plan_partial(plan_id)


def _plan_and_rec(plan_id):
    """(plan_obj, db_rec) for a registered plan."""
    store = _collect_store(plan_id)
    rec = store.get_plan()
    if not rec or not rec.get("path"):
        return None, rec
    plan, _err = _validate_plan_file(Path(rec["path"]))
    return plan, rec


def _collection_plan_partial(plan_id):
    plan, _rec = _plan_and_rec(plan_id)
    if plan is None:
        return "<mark>Plan file missing — reload it.</mark>"
    job_id = current_app.config.get(f"collection_job_{plan_id}")
    job = _jobs().get(job_id) if job_id else None
    running = bool(job and job.status == "running")
    return render_template(
        "partials/collection_plan.html",
        plan=plan,
        opts=_collect_opts(plan_id),
        running=running,
        job_id=job_id if running else None,
        tasks=_collect_store(plan_id).list_tasks(),
        version=_version(),
    )


@bp.route("/collection/<plan_id>/table")
def collection_table(plan_id):
    """HTMX-polling task table — reads live statuses from collection_tasks."""
    return render_template(
        "partials/collection_tasks.html",
        plan_id=plan_id,
        tasks=_collect_store(plan_id).list_tasks(),
    )


@bp.route("/collection/<plan_id>/status")
def collection_status(plan_id):
    """HTMX status line for a running collection job."""
    job_id = current_app.config.get(f"collection_job_{plan_id}")
    job = _jobs().get(job_id) if job_id else None
    if job is None:
        return ""
    if job.status == "running":
        return (
            f'<div hx-get="{url_for("ui.collection_status", plan_id=plan_id)}" '
            f'hx-trigger="every 3s" hx-swap="outerHTML">'
            f'<span aria-busy="true">Collection running — '
            f"{len(job.progress or {})} task(s) finished.</span></div>"
        )
    if job.status == "error":
        _jobs().pop(job_id)
        return f"<mark>Collection job failed: {escape(job.error_message)}</mark>"
    _jobs().pop(job_id)
    return "<p class='stats-line'>Collection finished — table refreshed below.</p>"


@bp.route("/collection/<plan_id>/task/<task_id>")
def collection_task_detail(plan_id, task_id):
    """Per-task detail: matched papers, grades, local file paths."""
    store = _collect_store(plan_id)
    rows = store.matches_for_task(task_id, grades=("HIGH", "MEDIUM", "LOW", "NEEDS_REVIEW"))
    cache = _cache()
    links = {lk["paper_uid"]: lk["link_path"] for lk in store.links_for_task(task_id)}
    papers = []
    for m in rows:
        p = cache.get_by_uid(m["paper_uid"])
        if p:
            papers.append({"paper": p, "match": m, "link": links.get(m["paper_uid"], "")})
    task = next((t for t in store.list_tasks() if t["task_id"] == task_id), None)
    return render_template(
        "partials/collection_task.html",
        plan_id=plan_id,
        task=task,
        papers=papers,
        version=_version(),
    )
