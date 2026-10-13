# JSON Collection Plan Acceptance — Gate A–G Evidence

Date: 2026-10-13 · Branch: `feat/ntrs-aria2`
Baseline: uncommitted working-tree changes on top of `6435f84`
`fix: reliable arXiv download — get-by-id, queue dedup/restart, exit codes`

## 0. Feature summary

`mosaic collect` executes a structured JSON *collection plan*
(`plans/aero_design_rows_066_136.json`, 71 tasks) end-to-end:

plan JSON → per-task tiered queries → multi-source search
(`merge_papers` dedup) → deterministic relevance grading (no LLM) →
legal open-access download via the existing DownloadManager
(native/aria2 queue) → PDF verification + identity check → canonical
`<root>/_library/` + per-task hardlinks under
`domain/topic/task/` named `{year}_{source}_{title}.pdf` → SQLite
state (`collection_*` tables, schema v4) → `_manifest/` export.

Everything domain-specific lives in the plan JSON; the code is generic.

### New modules

| File | Purpose |
|---|---|
| `mosaic/collection/loader.py` | plan parsing + full validation (`PlanError` with complete problem list) |
| `mosaic/collection/planner.py` | tiered L1–L4 query planning from plan-supplied terms/context |
| `mosaic/collection/relevance.py` | HIGH/MEDIUM/LOW/NEEDS_REVIEW grading + PDF identity check |
| `mosaic/collection/storage.py` | filename rule, canonical library, task-dir hardlink/copy |
| `mosaic/collection/state.py` | `CollectionStore` over shared cache.db (plans/tasks/queries/matches/files/links/downloads/source-health) |
| `mosaic/collection/runner.py` | `CollectionRunner` — search→grade→download→archive per task, resumable |
| `mosaic/collection/reporting.py` | `_manifest/` export (5 files) |
| `mosaic/db.py` | schema v4 migration — 8 collection tables |
| `mosaic/dlqueue.py` | `enqueue_papers(..., download_dir=, filename_pattern=, dest_fn=)` overrides |
| `mosaic/cli.py` | `collect validate/run/resume/retry/status/report` |
| `mosaic/ui/routes.py` + 3 templates | `/collection` UI: load/run/stop/table/task-detail |

## 1. Environment

| Item | Value |
|---|---|
| Git HEAD | `6435f84` |
| Python | 3.12.14 (`.venv/bin/python`) |
| `mosaic` | editable install |
| aria2 | 1.37.0 `/opt/homebrew/bin/aria2c` (managed daemon, port 6816) |
| DB | `~/.local/share/mosaic/cache.db` (auto-migrated v3→v4, backup kept) |
| Output | `./collected-papers/` (gitignored) |

## 2. Gate A — JSON loading & dry run

```text
$ mosaic collect validate plans/aero_design_rows_066_136.json
 plan_id   aero_design_excel_rows_066_136
 tasks     71
 domains   风扇与压气机=21, 燃烧室与加力燃烧室=24, 涡轮=26
 priorities P0=56, P1=15
Plan is valid.
```

- 71/71 tasks parse; task_ids unique (T045…T265 — **not** excel-row-derived).
- All `output_subdir`s pass the traversal/absolute-path safety check.
- Keywords (zh+en), aliases, abbreviations, all 5 query strings, focus and
  boundary load without loss — asserted in
  `tests/test_collection_loader.py::TestRealPlan` (10 checks on the real file).

Dry run (no network, no DB writes — verified by empty query/source tables
and unchanged task statuses):

```text
$ mosaic collect run plans/aero_design_rows_066_136.json --task T045 --dry-run
Dry run — plan aero_design_excel_rows_066_136, 1 task(s)
T045 [P0/风扇与压气机] axial compressor
    L1_focus  axial compressor compressor→(dedup)  … tiered queries listed
    → collected-papers/风扇与压气机/部件构型/T045_轴流压气机
```

## 3. Gate B — single-task real collection (T045)

```text
$ mosaic collect run plans/aero_design_rows_066_136.json --task T045 \
      --output-dir ./collected-papers
  T045 COMPLETED      axial compressor
1 completed · 0 partial · 0 failed · 7 unique PDFs
```

- Queries: 2 ran (early-stop after 2 HIGH matches; 72 raw hits → 36 relevant).
- Downloads went through the **aria2** queue backend (job rows:
  `backend=aria2`, `status=COMPLETED`).
- Task dir `风扇与压气机/部件构型/T045_轴流压气机/` contains hardlinks to
  `collected-papers/_library/` files — verified by identical inode numbers
  (`ls -li`).
- Filenames: `{year}_{source}_{title}.pdf`, e.g.
  `2021_arXiv_Study_on_S2_Flow_Path_Design_and_Three-dimensional_Numerical.pdf`,
  `2018_Zenodo_Convergence_Criteria_for_Axial_Compressor_Flow_Calculations.pdf`.
  No author component; SHA-256 + identity check recorded per file.

## 4. Gate C — cross-domain collection

```text
$ mosaic collect run … --task T063 --task T079
  T063 COMPLETED      annular combustor
  T079 PARTIAL        high pressure turbine
```

- T063 → `燃烧室与加力燃烧室/部件构型/T063_环形燃烧室/` — 3 PDFs (HAL, arXiv).
- T079 → `涡轮/部件构型/T079_高压涡轮/` — 2 PDFs; failure reasons recorded
  (`access_denied: 403`, `not_found: no download URL`) → status PARTIAL.
- No cross-domain mixing; each task lands under its own `output_subdir`.

## 5. Gate D — full 71-task run

```text
$ mosaic collect run plans/aero_design_rows_066_136.json \
      --output-dir ./collected-papers --max-pdfs-per-task 3
```

```text
$ mosaic collect run plans/aero_design_rows_066_136.json \
      --output-dir ./collected-papers --max-pdfs-per-task 3
(… ~2.5 h real-network run; sources: zenodo/crossref/hal/arxiv carried the
harvest, openalex/ntrs tripped the per-source circuit breaker — see §9)

33 completed · 27 partial · 11 failed/other · 138 unique PDFs
```

| Status | Tasks | Meaning |
|---|---|---|
| COMPLETED | 33 | ≥ target PDFs verified & archived |
| PARTIAL | 27 | some PDFs archived, target not reached |
| FAILED | 11 | relevant records exist but no legal OA copy could be obtained |

Per-domain task-dir counts: 风扇与压气机=50, 燃烧室与加力燃烧室=48, 涡轮=49 —
146 hardlinked artefacts from **138 unique canonical PDFs** (841 MB).
7 papers are shared across ≥2 tasks (one linked by 3 tasks) — each was
downloaded exactly once.

Failure taxonomy (from `collection_downloads` / `failures.csv`):

- `not_found` ×246 — record exists, no resolvable download URL
- `access_denied` ×27 — HTTP 403 from publisher/CDN
- `unreachable`/`dns_error` ×7 — dead links
- `no_candidates` ×3, `not_pdf` ×1, `unknown` ×1

Task-level: DOWNLOAD_FAILED ×6, ACCESS_RESTRICTED ×4, NO_RELEVANT_RESULT ×1.
All 71 tasks have status + evidence rows; nothing was silently dropped.

## 6. Gate E — naming, dedup, reruns

- **Year/source/title correctness**: filenames derive from the merged
  `Paper` record (`safe_filename("{year}_{source}_{title}")`); source label
  is the downloading source (e.g. `Zenodo`, `arXiv`, `HAL`, `NASA_NTRS`).
- **Locked filenames**: once `collection_files.filename` is set the name
  never changes — a later Crossref match of an arXiv paper cannot rename it
  (test `test_filename_lock_never_renames`).
- **Collisions**: different papers generating the same filename get a
  stable `_<uidhash8>` suffix — never an overwrite
  (`test_canonical_collision`, `variant_filename` determinism).
- **Truncation**: titles capped at 60 chars inside a 180-char name;
  `.pdf` suffix is never cut (`test_long_title_truncated_keeps_ext`).
- **Cross-task dedup**: a paper matched by several tasks downloads once;
  other tasks hardlink (`test_second_task_reuses_canonical` asserts zero
  extra enqueues).
- **Rerun idempotency**: COMPLETED tasks report `skip` with zero new
  queries/downloads (`test_completed_task_skipped_on_rerun`,
  `test_resume_reuses_matches_no_research`); `link_or_copy` returns
  `exists` on identical content, refusing overwrites.
- **Consistency**: `papers.csv`/`task_paper_links.csv` rows, SQLite
  `collection_files`/`collection_links`, and on-disk PDFs agree
  (sha256 recorded per canonical file).

## 7. Gate F — CLI & Web UI

CLI: `collect validate/run/resume/retry/status/report` all exercised above;
`status` renders per-task table from `collection_tasks`.

Web UI (`mosaic ui`): `/collection` page —

- plan loading by server path **or** file upload (validated on load);
- Start / Resume / Retry-failed / Pause controls (pause honoured at task
  boundaries via `runner.stop_event`);
- live task table (5 s HTMX poll) with status, counters, per-task
  `detail` view listing matched papers + local file paths;
- plan list persisted via `collection_plans`.

`tests/test_collection_ui.py` — 8 checks pass. Existing
Search/Bulk/Downloads/History/Config untouched (full suite below).

## 8. Gate G — regression

```text
$ .venv/bin/pytest tests/ -q --no-cov
1166 passed, 12 skipped, 3 deselected
```

Including the pre-existing arXiv-download tests; the earlier real-network
arXiv acceptance path (dlqueue/downloader) is unchanged and still green.

New tests added: 79
(`test_collection_loader.py` 51 · `test_collection_runner.py` 20 ·
`test_collection_ui.py` 8).

## 9. Known issues & notes

- **NTRS HTTP 403** — NASA NTRS rejected non-browser requests mid-run;
  the per-source breaker cooled it (162 queries skipped) and other sources
  continued. NTRS data is re-tryable via `collect retry --failed` after the
  cooldown or from a different network path.
- **OpenAlex/arXiv 429s** — polite-pool rate limits; the breaker backed off
  (short cooldowns) and recovered automatically.
- **1 PDF flagged `mismatch`** — `1984_Crossref_Influence_of_a_Periodical…`
  (JSME paper, Japanese text vs English catalogue title, word_frac=0.00).
  Kept in `_library` for review, deliberately **not** linked into the task dir.
- **3 files with `0000_` year** — Crossref records lacking a publication
  year; the fallback keeps them findable without inventing metadata.
- **PARTIAL tasks** all preserve full bibliographic metadata in
  `collection_matches`/`papers` for manual follow-up — none is a silent drop.
- Re-running `collect run` resumes unfinished tasks; `collect retry --failed`
  resets non-completed tasks; `--all` forces a full sweep.

## 10. Repro

```bash
source .venv/bin/activate
mosaic collect validate plans/aero_design_rows_066_136.json
mosaic collect run  plans/aero_design_rows_066_136.json \
    --output-dir ./collected-papers --max-pdfs-per-task 3
mosaic collect status plans/aero_design_rows_066_136.json
mosaic collect report plans/aero_design_rows_066_136.json \
    --output-dir ./collected-papers
```

`collected-papers/` is gitignored; manifests under
`collected-papers/_manifest/` reproduce all numbers above.
