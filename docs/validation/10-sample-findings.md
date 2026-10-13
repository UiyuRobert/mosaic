# 10 — Representative-sample run: findings and fixes

Date: 2026-10-12. Commit: (worktree, `feat/ntrs-aria2`).
Harness: `tools/validate_starter_list.py` over 30 terms spanning all 17
professional domains (T002, T003, T016, T021, T034, T036, T045, T047, T056,
T063, T064, T079, T101, T102, T112, T124, T135, T151, T163, T181, T182,
T193, T199, T212, T213, T227, T267, T285, T286, T320).

This run is REAL evidence: live API searches (OpenAlex/Crossref/arXiv/HAL/
Zenodo/DOAJ/BASE/DBLP), Unpaywall DOI resolution, and a real aria2 daemon
downloading real PDFs into `local-validation/pdfs/`.

## Results

| Status | Count | Terms |
|---|---|---|
| SUCCESS | 22 | T002 T003 T016 T034 T045 T047 T056 T063 T064 T079 T101 T102 T124 T135 T151 T182 T212 T213 T227 T267 T286 T320 |
| NO_PUBLIC_PDF | 7 | T021 T036 T112 T163 T181 T199 T285 |
| NO_RELEVANT_RESULT | 1 | T193 |

SUCCESS = ≥1 professionally relevant paper (HIGH/MEDIUM per domain rules)
whose PDF downloaded, passed `verify_pdf`, and whose first-page text matched
the catalogue identity (title/DOI). No fabricated or keyword-only matches.

## Real defects found by the sample and fixed in product code

1. **`dlqueue._handle_failure` — infinite candidate ping-pong.** Observed:
   a job stayed DOWNLOADING for 5+ min at 0 bytes with
   `last_error=html_response`. Root cause: `_job_candidates` re-sorted the
   stored candidates to put the *current* URL first, so `urls.index(cur)`
   was always 0 and the "advance to next candidate" logic alternated
   between exactly two URLs forever without consuming `max_tries`. Fix:
   keep the stored candidate order stable (advance uses true position);
   the current-first reorder was moved to the native path where it belongs.
   Regression: `tests/test_dlqueue.py` advance coverage; the stall is
   additionally bounded by the new stall detector below.

2. **`aria2` drip-feed stall.** A host that holds the TCP connection open
   while sending ~0 bytes kept a job in DOWNLOADING indefinitely
   (`https://doi.org/10.15593/…` — a dead journal server). Two fixes:
   `--lowest-speed-limit=1K` added to the managed-daemon spawn, and an
   in-queue stall detector (`stall_timeout`, default 240 s) that cancels a
   gid whose completed byte count does not change, then routes the job
   through the normal failure/candidate-advance path.

3. **`wait_all` had no timeout.** Callers blocking on a queued job could
   wait forever. Added `timeout=` (default 0 = unchanged behaviour); the
   validation harness uses it with `--dl-timeout` plus a per-term download
   budget (`--term-dl-budget`) so a pathological term cannot pin a worker.

4. **`doi.org` URLs counted as download candidates.** Unpaywall/semantic-
   scholar records sometimes carry `url=https://doi.org/…` (a redirect,
   never a PDF stream). Trying them cost a full connect-timeout cycle each.
   `_is_doi_url()` now filters them in `downloader._candidates_for`,
   `dlqueue._cheap_candidates`, and when rehydrating stored
   `candidates_json` (so old queues don't revive them).

5. **Harness bugs (test-only):** SQLite `check_same_thread` crash when
   fan-out threads touched the evidence store (fixed: `check_same_thread=
   False` + RLock, cooldown checks moved to the main thread); a missing
   `max_results_per_source` attribute that silently poisoned every source
   health counter (would have looked like `SOURCE_BLOCKED` — caught before
   being mistaken for an API outage).

6. **Relevance scoring edge (harness):** `气膜孔加工` (T193) — a Chinese
   paper whose title literally contains the term only reached MEDIUM
   (+3.0 < the 5.0 HIGH threshold) and the domain does not accept MEDIUM.
   `zh_core` in title now grades HIGH.

## External limitations confirmed (not code bugs)

- **NASA NTRS 403** — every live request (health, search, downloads) is
  refused by NASA's WAF from this network, browser-like headers included.
  Terms that are NTRS-heavy (e.g. T036 S-duct) lose their best OA source;
  the harness records `SOURCE_BLOCKED`-style evidence per attempt and the
  circuit breaker cools the source for 1 h after the first 403.
- **Semantic Scholar 429** — rate-limited without an API key; breaker
  cools it for 20 min on each 429. Other sources cover the gap.
- **Closed-access aero literature** — T021, T036, T112, T163, T181, T199,
  T285 each found genuinely relevant records but all resolvable full texts
  are paywalled (ASME GT proceedings, Wiley, Elsevier) or on dead
  regional journal sites. Classified `NO_PUBLIC_PDF`, never SUCCESS.

## Still open going into the full run

- The per-term download budget caps worst-case time, but hosts that fail
  slowly still cost minutes; `NO_PUBLIC_PDF` terms may improve with CNKI
  or institutional access (recorded as suggestions, not auto-solved).
- NTRS cannot be validated live from this network — the lazy attachment
  path is covered by mocked-response regression tests instead.
