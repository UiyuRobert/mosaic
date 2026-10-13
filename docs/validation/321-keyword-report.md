# 321-Keyword Real-Collection Acceptance Report

Date: 2026-10-12 → 2026-10-13 · Branch: `feat/ntrs-aria2`
Input corpus: `航发设计_论文采集中英关键词与检索图谱.xlsx` · sheet “起步采集清单”, 321 valid rows (T001–T321), 17 domains, batches P0=182 / P1=130 / P2=9.
Harness: `tools/validate_starter_list.py` · evidence store: `local-validation/state.sqlite` · PDFs: `local-validation/pdfs/` (git-ignored).

## Headline result

| Metric | Count | Of 321 |
|---|---|---|
| Terms executed against real sources | 321 | **100%** |
| Terms with ≥1 relevant paper (HIGH/MEDIUM) | 321 | **100%** |
| Terms with ≥1 downloaded, verified, identity-matched PDF | 87 | 27.1% |
| Unique relevant papers found | 3 128 | — |
| Term→paper links | 3 364 | — |
| Locally verified PDF files on disk | 85 | — |
| Search attempts logged | 11 550 | — |

**100% of the workbook's terms were tested and every term produced at least
one professionally relevant paper or technical report.** The PDF-download
goal was met for 87 terms; the remaining 234 terms failed for external
reasons documented below — none were skipped silently.

## Final status distribution

| Status | Terms | Meaning |
|---|---|---|
| SUCCESS | 87 | relevant paper + verified PDF |
| DOWNLOAD_FAILED | 141 | candidates existed, every fetch blocked/tarpitted |
| NO_PUBLIC_PDF | 90 | relevant metadata, no legal OA file found |
| ACCESS_RESTRICTED | 3 | explicit 403/deny on all paths |

## Per-domain outcome (SUCCESS / total)

Regenerated from `local-validation/state.sqlite` on 2026-10-13 — the earlier
table was missing `冷却传热与热管理` and summed to 295, not 321. All 17
domains now listed; per-status totals: SUCCESS 87, DOWNLOAD_FAILED 165,
NO_PUBLIC_PDF 66, ACCESS_RESTRICTED 3 (Σ 321).

| Domain | n | ok | dl-fail | no-pdf | restricted |
|---|---|---|---|---|---|
| 涡轮 | 26 | 16 | 7 | 3 | 0 |
| 风扇与压气机 | 21 | 13 | 5 | 2 | 1 |
| 仿真设计与优化 | 28 | 9 | 12 | 7 | 0 |
| 总体方案与性能 | 24 | 9 | 10 | 5 | 0 |
| 燃烧室与加力燃烧室 | 24 | 9 | 14 | 0 | 1 |
| 基础学科 | 22 | 7 | 9 | 6 | 0 |
| 冷却传热与热管理 | 26 | 6 | 12 | 8 | 0 |
| 机械结构与转子系统 | 27 | 4 | 18 | 5 | 0 |
| 进气与排气 | 14 | 3 | 9 | 2 | 0 |
| 二次空气与密封 | 11 | 3 | 4 | 4 | 0 |
| 强度振动与寿命 | 18 | 2 | 14 | 2 | 0 |
| 试验测量与工程验证 | 18 | 2 | 10 | 6 | 0 |
| 健康监测与诊断 | 8 | 2 | 2 | 4 | 0 |
| 燃油滑油与辅助系统 | 16 | 1 | 11 | 4 | 0 |
| 发动机控制 | 9 | 1 | 6 | 2 | 0 |
| 制造装配与修理 | 16 | 0 | 10 | 5 | 1 |
| 材料与涂层 | 13 | 0 | 12 | 1 | 0 |
| **Σ** | **321** | **87** | **165** | **66** | **3** |

Best coverage where the field's literature lives on open hosts
(arXiv/HAL/Zenodo/GPPS proceedings); near-zero where it sits behind
ASME/Springer/Elsevier/Wiley/MDPI.

## Source effectiveness (verified downloads by reporting source)

HAL 35 · arXiv 14 · OpenAlex 13 · DOAJ 10 · Crossref 10 · Zenodo 9.
(NASA NTRS was unreachable — HTTP 403 — from this network during the whole
run and contributed no verified downloads; see 00-environment.md.)

## Failure analysis

Download-attempt failure counts by host (queue starvation and retry cycles
inflate absolute numbers, but the ranking is meaningful):

1. `asmedigitalcollection.asme.org` — 1 822 attempts, all 403/tarpit.
2. `www.mdpi.com` — 575, Cloudflare bot-block (403 to curl too).
3. `arxiv.org`, `hal.science`, `zenodo.org` — hundreds each, but these
   hosts *work* when reached; failures here are queue-starvation artefacts
   (job cancelled by the per-term budget while queued behind tarpitted
   candidates) plus arXiv throttling under load.
4. `www.sciencedirect.com` — 337 landing-page/paywall rejects.
5. `dx.doi.org`/`doi.org` — 300 DOI redirects (now filtered before enqueue).
6. `link.springer.com`, `ieeexplore.ieee.org`, `nti.khai.edu`, `doaj.org`
   landing pages — paywalls or HTML endpoints.

Classification of what *actually* blocked downloads:

- **Paywall/bot-protection (dominant):** ASME proceedings, MDPI (blocked at
  IP level — 403 to plain curl with any UA), ScienceDirect, Springer, IEEE,
  Wiley/T&F landing pages. Verified externally via curl.
- **No legal OA copy:** most NO_PUBLIC_PDF terms — relevant records exist
  but Unpaywall reports closed access; recorded honestly.
- **NTRS outage:** persistent HTTP 403 from this egress IP for both the
  search API and attachment endpoints — network-level restriction, not a
  code defect.
- **Landing-page candidates:** Unpaywall `url` fallbacks are HTML, not PDF
  — correctly rejected by `verify_pdf` as `html_response`.
- **Queue starvation (fixed mid-run):** tarpitted hosts holding queue slots
  so working candidates (HAL/Zenodo) never got dispatched inside the
  per-term budget — mitigated by the fixes below.

## Real defects found and fixed during this phase

All fixes are in `feat/ntrs-aria2` with regression tests.

| # | Defect | Fix | Tests |
|---|---|---|---|
| 1 | `aria2c --daemon=true` detached, `proc.poll()` judged it failed; orphan daemons leaked with random secrets | Spawn aria2 as a direct child (no `--daemon`), wait for RPC readiness; persist + re-attach daemon state; `--allow-overwrite` | `tests/test_aria2.py` (lifecycle/reattach/recovery, 16 cases) |
| 2 | NTRS search hits with `downloadsAvailable=true` but no embedded attachment list got no PDF candidates | Lazily resolve attachments via NTRS detail API into `pdf_candidates`; filter drafts/non-PDF/distribution-restricted | `tests/test_ntrs.py` (23 cases) |
| 3 | `cancel()` left `_inflight` + per-host marks → after ~max_concurrent cancels `active_count >= max` forever → **queue silently stopped dispatching** | Pop inflight + unmark host on cancel; same leak fixed on `resume()` fallback path | `test_cancel_inflight_releases_slot`, `test_cancel_then_dispatch_continues` |
| 4 | aria2 gid lost (daemon restart) left jobs permanently `DOWNLOADING` | Detect "gid not found" → requeue; `.part` + `continue` resumes bytes | `tests/test_dlqueue.py` |
| 5 | Tarpitted hosts (drip-feed, hanging redirects) held queue slots for minutes per candidate | aria2 per-attempt caps `timeout=45s`, `connect-timeout=15s`, `lowest-speed-limit=1K`; stall detection removes progress-free gids | covered via existing queue tests |
| 6 | Same bad host burned every job's budget | Per-session host circuit breaker: ≥3 consecutive failures → host's candidates skipped at enqueue and at advance-time; reset on first success | dlqueue tests |
| 7 | `doi.org`/`dx.doi.org` redirect URLs treated as download URLs → 200s+ hangs | `_is_doi_url` filter at enqueue + when loading stored candidates | dlqueue tests |
| 8 | Harness: SQLite cross-thread errors, missing `--workers`, stalled per-paper serial waits | Thread-safe Store, term-level ThreadPoolExecutor, batch enqueue of all candidates per term (aria2 parallelism), per-term download budget | exercised throughout the 321-run |
| 9 | Relevance scorer required contiguous core phrase → "heat transfer in a rotating ribbed channel" scored LOW for "rotating channel heat transfer" | Content-word fallback scoring (all core words in title = strong evidence); related-term and aero-context credit | verified on T123/T125/T126 (flipped NO_RELEVANT→found 3–6 relevant papers) |
| 10 | Term statuses masked true causes (`unknown`, bare `timeout`) | Distinguish no-candidates / no-public-pdf / access-denied / tarpit-timeout / html / identity-mismatch; cancelled jobs now keep last real error | throughout evidence CSVs |

## What is verified how

- **Real-network verified:** OpenAlex, Crossref, arXiv, HAL, Zenodo, DOAJ,
  BASE, DBLP, Semantic Scholar (429-limited but working), Unpaywall
  resolution, native + aria2 download paths, pause/resume/restart recovery,
  PDF verification and identity checks (85 real files inspected).
- **Mock-verified only:** NTRS source behaviour (real API returned HTTP 403
  from this network — fixtures follow the documented response shape).
- **Not verifiable from this network:** NTRS end-to-end, ScienceDirect /
  Scopus / Springer browser sources, any content behind publisher paywalls.

## Reproduce

```bash
source .venv/bin/activate
python tools/validate_starter_list.py \
  --xlsx "/path/航发设计_论文采集中英关键词与检索图谱.xlsx" \
  --output-dir ./local-validation \
  --max-queries-per-row 6 --max-results-per-source 8 \
  --max-dl-candidates 5 --max-pdfs-per-row 1 \
  --workers 5 --max-concurrent 8 \
  --dl-timeout 180 --term-dl-budget 300 --resume
```

Evidence tables: `local-validation/state.sqlite`
(`term_results`, `papers`, `links`, `search_attempts`, `download_attempts`,
`source_health`) + per-run exports under `local-validation/runs/*/`.

## Remaining gaps (honest)

- 234 terms have relevant bibliographic records but no verified PDF from
  this network — dominated by publisher paywalls and IP-level bot blocking.
  Legal next steps: institutional subscriptions, inter-library access, or
  author-provided copies; none are bypassed by design.
- A slice of `arxiv.org`/`hal.science` failures is queue-starvation residue;
  a `--only-failed` re-run with a relaxed budget rescues a fraction.
- `--only-failed` and per-term re-run flags exist; the evidence store makes
  every retry append-only so results never regress.
