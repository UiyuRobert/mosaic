"""Tiered query planning for collection tasks.

Plans carry *boolean-flavoured* query strings
(``("a" OR "b") AND ("c" OR "d")``). Source APIs in MOSAIC each speak
their own dialect, so the planner *parses* those strings into term
alternates / context alternates and emits plain-text queries — it never
forwards AND/OR/parenthesis syntax verbatim to an API.

Tiers produced (highest precision first):

- ``L1_focus``   — core term x each context alternate (+ term alternates x first context)
- ``L2_ext``     — remaining aliases and the abbreviation, each with first context
- ``L3_wide``    — core term / alternates alone (broad recall)
- ``L4_supp``    — non-English (zh) queries and related-term rescue queries
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mosaic.collection.loader import CollectionTask

_TIER_ORDER = ("L1_focus", "L2_ext", "L3_wide", "L4_supp")

# Sources that only index English-language records — zh-tier queries are
# skipped for them to avoid wasted, guaranteed-empty API calls.
ENGLISH_ONLY_SOURCES = {"arxiv", "ntrs", "dblp", "nasa_ads"}


@dataclass
class PlannedQuery:
    query: str
    tier: str
    lang: str = "en"  # "en" | "zh" | "any"


def parse_groups(query: str) -> tuple[list[str], list[str]]:
    """Parse ``("a" OR "b") AND ("c" OR "d")`` → (term_alts, ctx_alts).

    Tolerates ``|`` separators (NTRS-style), ``+`` group joins, and bare
    unquoted input. Returns ``(alts_of_first_group, alts_of_second_group)``;
    a single group yields ``(alts, [])`` and a bare string ``([text], [])``.
    """
    if not query or not query.strip():
        return [], []
    groups = re.findall(r"\(([^)]+)\)", query)
    alts = [
        [m.strip().strip('"').strip("'") for m in re.split(r"\s+OR\s+|\||\s+or\s+", g)]
        for g in groups
    ]
    alts = [[a for a in g if a] for g in alts]
    if len(alts) >= 2:
        return alts[0], alts[1]
    if len(alts) == 1:
        return alts[0], []
    # No parenthesised groups — treat the whole string as one phrase.
    bare = re.sub(r"\s+", " ", query).strip().strip('"')
    return ([bare] if bare else []), []


def task_context(task: CollectionTask) -> list[str]:
    """Context phrases for *task* (2nd group of the focused queries).

    These mark the surrounding subject area — fully plan-driven, nothing
    domain-specific is hardcoded here.
    """
    _t, en_ctx = parse_groups(task.queries.en_focused)
    _t2, zh_ctx = parse_groups(task.queries.zh_focused)
    _t3, ntrs_ctx = parse_groups(task.queries.ntrs_focused)
    out: list[str] = []
    for c in [*en_ctx, *ntrs_ctx, *zh_ctx]:
        if c and c.lower() not in {x.lower() for x in out}:
            out.append(c)
    return out


def _clean(q: str) -> str:
    """Normalise a query to a plain-text phrase (no boolean syntax/quotes)."""
    q = re.sub(r'"', " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def planned_queries(task: CollectionTask, max_queries: int) -> list[PlannedQuery]:
    """Return deduplicated, tier-ordered queries for *task* (≤ *max_queries*)."""
    out: list[PlannedQuery] = []
    seen: set[str] = set()

    def add(q: str, tier: str, lang: str = "en") -> None:
        q = _clean(q)
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(PlannedQuery(q, tier, lang))

    core = task.en.primary
    ctx = task_context(task)
    # Context words already inside a term add nothing ("axial compressor
    # compressor") — drop them per-term when emitting focused queries.
    term_alts, _ = parse_groups(task.queries.en_focused)
    broad_alts, _ = parse_groups(task.queries.en_broad)
    all_en = task.en.all_terms
    for alt in [*term_alts, *broad_alts, *all_en]:
        if alt and alt.lower() not in {t.lower() for t in all_en}:
            all_en.append(alt)

    def _with_ctx(term: str, c: str) -> str:
        return term if c.lower() in term.lower() else f"{term} {c}"

    ctx0 = next((c for c in ctx if c.lower() not in core.lower()), ctx[0] if ctx else "")
    # L1 — focused: core x each context (bounded), then alternates x ctx0.
    for c in ctx[:3]:
        add(_with_ctx(core, c), "L1_focus")
    for alt in all_en[1:]:
        if ctx:
            add(_with_ctx(alt, ctx0), "L1_focus")
    # L2 — extensions & abbreviation with context.
    if task.en.abbreviation:
        add(_with_ctx(task.en.abbreviation, ctx0 or core), "L2_ext")
    for alt in all_en[1:]:
        for c in ctx[1:3]:
            add(_with_ctx(alt, c), "L2_ext")
    # L3 — wide recall.
    add(core, "L3_wide")
    for alt in all_en[1:]:
        add(alt, "L3_wide")
    # L4 — supplementary: zh queries + related terms.
    zh_alts, zh_ctx = parse_groups(task.queries.zh_focused)
    zh_broad_alts, _ = parse_groups(task.queries.zh_broad)
    zh_all = task.zh.all_terms
    for alt in [*zh_alts, *zh_broad_alts, *zh_all]:
        if alt and alt not in zh_all:
            zh_all.append(alt)
    if zh_all:
        anchor = zh_ctx[0] if zh_ctx else ""
        add(f"{zh_all[0]} {anchor}".strip(), "L4_supp", "zh")
        for alt in zh_all[1:]:
            add(alt, "L4_supp", "zh")
    for rel in task.related_terms[:2]:
        add(f"{rel} {ctx[0]}".strip() if ctx else rel, "L4_supp", "zh" if _has_cjk(rel) else "en")

    # Stable tier ordering (construction order mostly guarantees it already).
    out.sort(key=lambda pq: _TIER_ORDER.index(pq.tier))
    return out[: max(1, max_queries)]


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def queries_for_source(queries: list[PlannedQuery], source_key: str) -> list[PlannedQuery]:
    """Filter planned queries to those appropriate for *source_key*."""
    if source_key in ENGLISH_ONLY_SOURCES:
        return [q for q in queries if q.lang != "zh"]
    return list(queries)
