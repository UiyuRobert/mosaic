# arXiv Download Acceptance — Gate A/B/C Evidence

Date: 2026-10-13 · Branch: `feat/ntrs-aria2`
Baseline commit: `4b13f9113476d5f4db1d7c253b16d28e39771e62`
(all changes below are uncommitted working-tree edits on top of that baseline)

## 1. Environment

| Item | Value |
|---|---|
| Git HEAD | `4b13f91` `fix: real-network 321-term validation — aria2 lifecycle, queue leaks, host breaker` |
| Remote | `origin → https://github.com/UiyuRobert/mosaic.git` (fork) |
| Python | 3.12.14 (`.venv/bin/python`; system `python3` is 3.9.6 — the venv is required) |
| `mosaic` | 1.5.5, editable install → `/Users/liuyu31/航发模型/下载脚本/mosaic/__init__.py` |
| `mosaic` binary | `.venv/bin/mosaic` (shebang → `.venv/bin/python`) |
| aria2 | 1.37.0 at `/opt/homebrew/bin/aria2c` |
| OS | macOS (Darwin 27.0.0, Apple Silicon) |
| Network | Direct outbound HTTPS; `export.arxiv.org` answers `GET /api/query` HTTP 200 in ~0.35 s; `arxiv.org/pdf/*` serves `application/pdf` 200 |

### Isolation used for every check

Every run below used a fake `HOME` under `local-smoke/<run>/home` so the
real user library was never touched:

- config → `local-smoke/<run>/home/.config/mosaic/config.toml`
- SQLite → `local-smoke/<run>/home/.local/share/mosaic/cache.db`
- PDFs → `local-smoke/<run>/pdfs/` (and `aria2-pdfs/`)

Evidence JSON for the scripted run: `local-smoke/final2/smoke-evidence.json`.

## 2. Bugs found and fixed

| # | Bug | Where | Fix |
|---|---|---|---|
| 1 | `mosaic get <arxiv-id>` could never download — it built a bare `Paper(doi=…)` with no metadata and no PDF candidate, printed "Could not find a downloadable PDF" and still exited 0 | `mosaic/cli.py` | Identifier resolver `_resolve_get_paper()` recognises bare arXiv IDs, `arxiv:` prefixes, `arxiv.org/abs` & `/pdf` URLs and `10.48550/arXiv.*` DOIs; fetches real metadata via `ArxivSource.get_by_id` (official `id_list` API) with a PDF-URL fallback when the API is down; invalid input → `exit 2`, download failure → `exit 1`. |
| 2 | `mosaic search --json --download` silently ignored `--download` (JSON branch returned before post-processing) | `mosaic/cli.py` | The `--json` path now runs `download_papers` and embeds `{uid: path}` as a `downloads` key. |
| 3 | `mosaic config --download-backend` printed "updated" but **never wrote the file** — `_dl_changed` was missing from `_any_changed` | `mosaic/cli.py` | Added; every download config flag now persists to `config.toml`. |
| 4 | `wait_all()`/`download_papers()` had no bounded wait — a hung job could block the CLI forever | `workflows.py`, `dlqueue.py`, `config.py` | New `download.wait_timeout` (default 600 s); `download_papers` returns an `outcome` with `done`/`failed`/`pending`/`job_ids`; the CLI prints "still queued" for unfinished jobs instead of hanging. |
| 5 | Re-enqueueing an in-flight paper created a second job for the same uid | `dlqueue.py`, `db.py` | `enqueue_papers` snapshots active jobs and **joins** the existing one (returns its job id); a partial unique index `uq_jobs_active_uid` makes a second active job impossible even under a race. |
| 6 | Two different papers generating the same filename shared one destination → could overwrite each other | `dlqueue.py` | Queued jobs reserve their destination in-memory; later papers in the same batch get a `__N` suffix. |
| 7 | Restarting MOSAIC re-submitted a still-running aria2 transfer (duplicate GID) | `dlqueue.py` | `_reconcile` keeps `aria2_gid` on aria2 jobs; `_start_aria2` `tellStatus`-probes the GID — re-attaches when live/complete, finalizes a `.part` file when complete, re-submits only when the GID is gone or errored. |
| 8 | `downloads.status='ok'` was trusted without checking the file → corrupt/missing files were never re-fetched | `downloader.py`, `db.py` | Schema v3 adds `size_bytes` + `sha256` to `downloads`; `download_still_valid()` verifies path + size + hash (legacy rows fall back to the `%PDF-` + `%%EOF` check) — a stale record now triggers a re-download. |
| 9 | `max_file_mb` was never enforced for aria2 (aria2 has no `--max-file-size` option) | `dlqueue.py` | The polling path measures `totalLength`/`completedLength` and removes + fails the job when it exceeds the limit. |
| 10 | pymupdf's deprecation warning prints to **stdout**, corrupting `--json` output | `pdfcheck.py`, `pdf.py` | Import `pymupdf` first (`import fitz` only as legacy fallback). |
| 11 | A source raising (e.g. NTRS HTTP 403) produced only "No results found." — the error was hidden behind `--verbose` | `cli.py` | Source errors now always go to stderr, HTML-stripped and truncated. |
| 12 | `search --download`/`get --from` exited 0 on any non-total failure | `cli.py` | Exit 1 whenever ≥1 eligible download failed or is still pending (documented semantics: 0 = everything eligible completed). |
| 13 | `_free_port` returned the busy preferred port when its 8-port scan was exhausted → spawned aria2c just to bind-fail | `aria2.py` | Scan widened to 32 ports; exhaustion returns `None` → clean "daemon cannot be started" instead of a confusing immediate exit. |
| 14 | arXiv query builder passed raw user text straight into `all:` — colons broke the query, multi-word input produced invalid syntax, a `title=Error` API entry could be misparsed as a paper | `sources/arxiv.py` | `build_arxiv_query()` handles bare/quoted/multi-word/prefixed input; shared 3 s throttle (class-level), bounded 429/5xx backoff; `_feed_error` surfaces API error entries; non-XML bodies rejected; pdf links normalised to HTTPS; `get_by_id` added. |

## 3. CLI evidence (real network, isolated HOME)

Harness: `tools/smoke_arxiv.py` — drives the installed CLI end-to-end.
Final scripted run (`local-smoke/final2`): **13/13 checks passed in 28 s**.

### `mosaic search "transformer" --source arxiv --download`

```text
$ mosaic config --download-backend native
$ mosaic search "transformer" --source arxiv --max 3 --download \
      --download-dir ./local-smoke/final2/pdfs
…3 results (arXiv)…
  ✓ 2021_arXiv_Jinxing_Learning_to_Cluster_Faces_via_Transformer.pdf
  ✓ 2022_arXiv_Kai_PyramidTNT_Improved_Transformer-in-Transformer_Baselines_wit.pdf
  ✓ 2024_arXiv_Sihao_MLP_Can_Be_A_Good_Transformer_Learner.pdf
Done: 3 downloaded            exit=0, ~8 s
```

### `mosaic get arxiv:1706.03762`

```text
$ mosaic get arxiv:1706.03762 --download-dir ./local-smoke/final2/pdfs
Attention Is All You Need
Saved: …/pdfs/2017_arXiv_Ashish_Attention_Is_All_You_Need.pdf   exit=0, ~3 s
```

Repeat run → `Already downloaded: …` (same path, no duplicate file), exit 0.
`mosaic get 10.48550/arXiv.2106.09685` → LoRA PDF downloaded, uid `arxiv:2106.09685` (alias-deduped with the bare-id form).
`mosaic get not-a-real-identifier` → `Could not parse identifier …` + supported-forms hint, **exit 2**.
`mosaic get 10.7554/eLife.09560` (non-arXiv DOI) → downloaded via Unpaywall/manual candidates — no regression.

### `--json --download`

```text
$ mosaic search "quantum diffusion monte carlo" --source arxiv --max 1 \
      --download --download-dir ./local-smoke/final2/pdfs/jsontest --json
{"status":"ok", …, "downloads":{"doi:10.1063/1.1632120":"…/1632120…pdf"}}
```

## 4. Downloaded files (final2 run)

| arXiv | Title | File | Bytes | Pages | SHA-256 (prefix) |
|---|---|---|---|---|---|
| 2104.11502 | Learning to Cluster Faces via Transformer (2021) | `2021_arXiv_Jinxing_Learning_to_Cluster_Faces_via_Transformer.pdf` | 784 909 | 10 | `f139afdb13b5` |
| 2201.00978 | PyramidTNT (2022) | `2022_arXiv_Kai_PyramidTNT_…wit.pdf` | 280 023 | 6 | `4ac4f297c386` |
| 2404.05657 | MLP Can Be A Good Transformer Learner (2024) | `2024_arXiv_Sihao_MLP_…Learner.pdf` | 816 561 | 14 | `94fae021a91c` |
| 1706.03762 | Attention Is All You Need (2017) | `2017_arXiv_Ashish_Attention_Is_All_You_Need.pdf` | 2 215 244 | 15 | `bdfaa68d8984` |
| 2106.09685 | LoRA (2021) | `2021_arXiv_Edward_LoRA_…Models.pdf` | 1 609 513 | 26 | `e9a0d3128767` |
| 2307.09288 | Llama 2 (2023) | `2023_arXiv_Hugo_Llama_2_…Models.pdf` | 13 661 300 | 77 | `1df284ce95f7` |

Page counts via pymupdf; every file starts with `%PDF-` and ends `%%EOF`.

### Final SQLite state (final2/home, download_jobs / downloads)

```text
download_jobs:  COMPLETED=9   (backend=native ×7, backend=aria2 ×2, all with gid)
downloads:      9 rows status='ok', size_bytes + sha256 recorded for each
queue residue:  no QUEUED/DOWNLOADING/RETRY_WAIT jobs; no .part files on disk
```

## 5. aria2 evidence

- `mosaic config --download-backend aria2` **persists** (bug 3 fixed) — `backend = "aria2"` in `config.toml`.
- `mosaic search "graph neural network" --source arxiv --max 2 --download` → managed daemon spawned (loopback `127.0.0.1`, ephemeral secret, `aria2.daemon.json` next to the session file); both jobs `COMPLETED`, `backend='aria2'`, `aria2_gid` set.
- **Pause/resume (throttled to 30 KB/s):** `manager.pause` → `completedLength` frozen 2.5 s; `manager.resume` → transfer restarted (0 → 49 152 B).
- **Restart reconcile (live daemon):** manager "crash" leaving job `DOWNLOADING` + gid `7cafe285…`; a *new* manager instance re-attached to the same gid — no duplicate `addUri` — and finalized `COMPLETED`, file 436 085 B, sha256 recorded. (Two real processes; same SQLite DB; same live daemon.)
- **Aria2 absent:** with `aria2c` removed from PATH, `backend=auto` and `backend=native` both downloaded natively (`backend='native'` stamped); explicit `backend=aria2` printed `aria2 backend selected but daemon unavailable…` and exited 1.

## 6. Web UI evidence (real HTTP against `mosaic ui`, isolated HOME)

Driven with real POSTs (no test client) against `mosaic ui --port 5599`:

1. `POST /search` (query=`diffusion transformer`, sources=`arxiv`) → job id → `GET /search/status/<id>` → results HTML with `name="uid" value="arxiv:*"` checkboxes.
2. `POST /download/arxiv:2401.11605` → HTMX poll fragment → `GET /download/status/<job>` → `<mark>PDF saved: 2024_arXiv_Katherine_Scalable…pdf</mark>` — 11 MB real file on disk.
3. `POST /downloads/enqueue` (`mode=selected`, uids `arxiv:2505.02831` + `arxiv:2311.15435`) → `2 download(s) queued`; repeating the same POST returned the same two job ids — **no duplicate jobs** (verified in `download_jobs`: exactly one row each, `COMPLETED`).
4. `GET /downloads/table` → queue table with real states (11 COMPLETED / 1 FAILED).
5. A fresh identical search marks the downloaded paper with the 💾 "local copy" badge.

## 7. Test suite & reproducibility

- Offline: `tests/test_arxiv_download.py` — 65 tests (identifier matrix, query builder, Atom parsing incl. error entry/malformed/HTML/empty feed, 429/503 backoff, shared throttle, dedup + unique-index race, filename collision, cached-integrity check, gid re-attach/finalize/resubmit, bounded wait, `get` resolver).
- Full suite: **1095 passed, 12 skipped, 3 deselected (live)** — zero regressions.
- Lint: `ruff check` clean; `ruff format` applied.
- Reproduce everything with one command:

  ```bash
  .venv/bin/python tools/smoke_arxiv.py --workdir ./local-smoke/my --backend all --aria2-absent
  ```

  Writes `my/smoke-evidence.json` and reaps the daemon it spawned.

## 8. Gate assessment

| Gate | Requirement | Result | Evidence |
|---|---|---|---|
| **A** | arXiv by ID + keyword search → real PDF, native backend, no aria2, no Unpaywall | **PASS** | §3, §4 — `get arxiv:1706.03762` and `search … --download` fetched real PDFs natively, sha256-verified, dedup-safe, bounded wait. |
| **B** | aria2 download / pause / resume / restart-reconcile / dedup; Web UI search→download; CLI↔UI一致 | **PASS** | §5, §6 — pause froze bytes, resume restarted, restart re-attached the same gid (no double-submit), UI flows over real HTTP, same queue/records as CLI. |
| **C** | No regression: plain DOI, multi-source, NTRS tests, SQLite compat, docs | **PASS** | `get 10.7554/eLife.09560` still works; 1095 tests green (incl. NTRS mocks); v2→v3 migration auto-backups `cache.db.bak-v1`; README updated. |

## 9. Known external limits (not code bugs)

- **NASA NTRS returns HTTP 403** from this network (unchanged from prior docs) — surfaces as `Warning: NASA NTRS: NTRS request rejected (HTTP 403)` on stderr; downloads simply report no results.
- arXiv rate limiting: when the API answers 429/503 the source backs off with a bounded retry budget and then reports a `SourceError` — a transient-availability signal, not silent failure.
- A `mosaic` CLI run leaves its managed aria2 daemon alive on purpose (detached, session file, `aria2.daemon.json` for re-attach) — this is the documented restart-recovery design, not a leak; `tools/smoke_arxiv.py` reaps its own daemon at the end.
- `--max-file-size` equivalent: aria2 can't abort oversize transfers natively, so `max_file_mb` is enforced while polling (`completedLength`/`totalLength` > limit → remove + FAIL).
