# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Activate the virtual environment first (system Python is externally managed)
source .venv/bin/activate

# Preview what the next changelog would look like (unreleased commits)
git cliff --unreleased

# Regenerate CHANGELOG.md and docs/guide/changelog.md manually
git cliff -o CHANGELOG.md
{ printf -- "---\ntitle: Changelog\n---\n\n"; awk '/^## \[/{found=1} found' CHANGELOG.md; } > docs/guide/changelog.md

# Install for development (creates .venv if needed)
make dev

# Run all tests with coverage
make test

# Run a single test file
.venv/bin/pytest tests/test_search.py

# Run a single test
.venv/bin/pytest tests/test_search.py::test_name

# Check linting and formatting (no fixes)
make lint

# Auto-fix lint issues and apply formatting
make fmt

# Remove build artifacts
make clean

# Run the CLI
python -m mosaic search "transformer attention"
mosaic search "transformer attention" --max 5 --source arxiv
mosaic get 10.1234/example.doi
mosaic config --show
```

## Architecture

**MOSAIC** is a CLI tool (`mosaic` entry point → `mosaic/cli.py`) that fans out paper searches across multiple scientific sources, deduplicates results, caches them locally, and can download PDFs.

### Data flow

1. `cli.py` loads config, instantiates enabled sources via `source_registry.py:build_sources()`, calls `search_all()`.
2. `search.py:search_all()` iterates sources sequentially, merges duplicates by `Paper.uid` (preferring richer data), applies `SearchFilters` as a post-processing safety net.
3. Results are saved to a SQLite cache (`db.py:Cache`) and optionally downloaded via the persistent queue (`dlqueue.py`) which delegates to `downloader.py` (native) or `aria2.py` (aria2 JSON-RPC).

### Key modules

- **`models.py`** — `Paper` dataclass (central data model), `SearchFilters` (year/author/journal filtering), and `PDFCandidate` (a downloadable-URL candidate with provenance). `Paper.uid` is the deduplication key: prefers DOI > arxiv_id > pii > `ntrs:{id}` > title slug; `Paper.identifiers` collects all strong IDs so cross-source records merge into one canonical uid.
- **`sources/base.py`** — `BaseSource` ABC with `search()` and `available()`. All sources in `sources/` implement this interface.
- **`sources/`** — 22 sources: `arxiv`, `semantic_scholar`, `sciencedirect` (API key or browser session), `sciencedirect_browser`, `springer_browser` (Playwright, shorthand `sp`), `springer_api` (free API key, shorthand `springer`), `doaj`, `europepmc`, `openalex`, `base_search`, `core` (free API key), `nasa_ads` (free API token), `ntrs` (NASA STI repository — public technical reports, no auth, shorthand `ntrs`), `ieee` (free API key, shorthand `ieee`), `zenodo` (no auth required), `crossref` (no auth required), `dblp` (no auth required), `hal` (no auth required), `pubmed` (no auth required, API key optional), `pmc` (PubMed Central, always OA + direct PDF, API key optional; same NCBI key as pubmed), `biorxiv` (bioRxiv + medRxiv, shorthand `rxiv`; searches both servers via website search, fetches metadata from `api.biorxiv.org`; always OA), `pedro` (physiotherapy evidence, shorthand `pedro`; requires `acknowledge_fair_use` config flag), `scopus` (Elsevier, shorthand `scopus`; API key or browser session). `unpaywall.py` is a helper (not a search source) used by the downloader.
- **`source_registry.py`** — Source factory registry, shorthand maps (`SRC_MAP`, `SHORTHAND_TO_CFG_KEY`), and `build_sources(cfg)` which instantiates all enabled sources from config.
- **`services.py`** — Shared business logic: `build_filters()` (construct `SearchFilters` from user input), `filter_papers()` (OA/PDF/sort post-processing), `merge_papers()` (deduplication by `Paper.uid`).
- **`workflows.py`** — Multi-step orchestration: `download_papers()`, `push_to_zotero()`, `push_to_obsidian()`. Used by both CLI and web UI.
- **`parsing.py`** — Shared parsing utilities: `parse_year()`, `normalise_doi()`, `strip_html()`, `parse_authors_name_key()`, `parse_authors_given_family()`, `split_authors()`, `extract_first()`.
- **`errors.py`** — Custom exception hierarchy (`MosaicError` → `SourceError`, `DownloadError`, `ConfigError`) and central logging setup.
- **`similar.py`** — `find_similar(identifier, max_results, *, oa_email, ss_api_key)` fans out to OpenAlex `related_works` (always) and Semantic Scholar recommendations (when API key configured), deduplicates by `Paper.uid`, and returns `(seed_title, papers)`. Used by `mosaic similar` CLI command.
- **`bulk.py`** — `read_dois(path)` extracts DOIs from `.bib` (regex) or `.csv` (DictReader) files. Used by `mosaic get --from`.
- **`zotero.py`** — `ZoteroClient` class supporting both local API (`http://localhost:23119`) and web API (`https://api.zotero.org`). Auto-detects mode from config (`zotero.api_key`). Key methods: `is_reachable()`, `discover_user_id()`, `ensure_collection(name)`, `add_papers(papers)`, `attach_pdf(item_key, path)`. PDF attachment is local-only (linked_file); web mode is metadata-only in v1.
- **`gui_launcher.py`** — Entry point for standalone desktop app (PyInstaller). Opens web UI in a Chromium `--app` window.
- **`db.py`** — SQLite with versioned migrations (`PRAGMA user_version`, auto-backup on migrate): `papers` (upsert on uid), `downloads` (final state per uid), `download_jobs` (persistent queue: status/progress/aria2_gid/candidates), `paper_identifiers` (uid aliases for cross-source dedup). Download lookups resolve aliases so `ntrs:{id}` and `doi:{doi}` records share one download entry.
- **`downloader.py`** — single-paper pipeline: collect `PDFCandidate`s (source URL → Unpaywall OA locations → browser session), stream to `*.part`, verify (`pdfcheck.py`), atomic rename.
- **`pdfcheck.py`** — PDF verification (`%PDF-` header, `%%EOF` trailer, HTML-page detection, pymupdf page count when installed), safe filenames, SSRF URL guard, path-dedup.
- **`dlqueue.py`** — `DownloadManager`: SQLite-backed scheduler with global/per-host concurrency, per-host start delay, finite retries with backoff + `Retry-After`, pause/resume/cancel, restart reconciliation. `get_manager(cfg, cache)` returns a shared per-DB manager.
- **`aria2.py`** — aria2 JSON-RPC client; `ensure_client()` attaches to a running daemon or spawns a managed one bound to `127.0.0.1` with an in-memory RPC secret.
- **`config.py`** — Reads/writes `~/.config/mosaic/config.toml`; deep-merges user config over defaults. DB lives at `~/.local/share/mosaic/cache.db`, downloads at `~/mosaic-papers/`. Zotero config under `[zotero]` section (`api_key`, `user_id`).

### Adding a new source

1. Create `mosaic/sources/myname.py` with a class extending `BaseSource`.
2. Set `name` class attribute and implement `search()` returning `list[Paper]`.
3. Export from `mosaic/sources/__init__.py`.
4. Wire into `source_registry.py` (factory function + `_SOURCE_REGISTRY` + shorthand maps).

### Tests

Tests use `unittest.mock` to patch `httpx` calls — no real network requests. `conftest.py` provides `tmp_cache` (in-memory SQLite) and `paper` fixtures, and a `make_response()` helper for building mock httpx responses. Coverage JSON is written to `docs/public/` after each test run.

### Docs

VitePress site in `docs/`. Build with `npm run docs:build` from the `docs/` directory.

## Workflow rules

- **Never run `git commit`** — generate commit messages with `/semantic-commit`
  and let the user paste them in their own terminal (GPG signing requires a TTY
  that Claude Code does not have).
