"""Deterministic, explainable relevance grading — no LLM involved.

Scores a paper against the *plan-supplied* vocabulary of a task: primary
terms (en + zh), aliases, abbreviation, related terms and context
phrases (the second group of the task's focused query strings). All
vocabulary is data-driven; the scorer is domain-agnostic.

Grades: ``HIGH`` → download first; ``MEDIUM`` → download when HIGH is
exhausted; ``LOW`` / ``NEEDS_REVIEW`` → recorded but not fetched.
``NEEDS_REVIEW`` means there was too little metadata to decide (e.g. no
abstract and no term hits at all).
"""

from __future__ import annotations

import difflib
import re
import unicodedata

from mosaic.collection.loader import CollectionTask
from mosaic.collection.planner import task_context
from mosaic.models import Paper

GRADES = ("HIGH", "MEDIUM", "LOW", "NEEDS_REVIEW")
_ACCEPT = ("HIGH", "MEDIUM")

_WORD = re.compile(r"[a-z0-9\-]+|[一-鿿]+")
_STOP = {
    "and",
    "the",
    "for",
    "with",
    "of",
    "a",
    "an",
    "in",
    "on",
    "to",
    "between",
    "based",
    "using",
    "via",
    "from",
}
_TECH_TYPES = (
    "technical memorandum",
    "technical publication",
    "contractor report",
    "contractor/grantee report",
    "special publication",
)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").lower()
    return re.sub(r"[^a-z0-9一-鿿\- ]+", " ", text)


def _has_phrase(text_norm: str, phrase: str) -> bool:
    p = _norm(phrase).strip()
    if not p:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", text_norm) is not None


def score_paper(
    task: CollectionTask, paper: Paper, ctx_phrases: list[str] | None = None
) -> tuple[str, float, list[str]]:
    """Grade *paper* against *task*; returns (grade, score, reasons)."""
    if ctx_phrases is None:
        ctx_phrases = task_context(task)
    title = _norm(paper.title or "")
    abstract = _norm(paper.abstract or "")
    score = 0.0
    why: list[str] = []

    # ── English vocabulary ───────────────────────────────────────────────
    phrases = [(task.en.primary, 4.0, "core")]
    phrases += [(a, 2.5, "alias") for a in task.en.aliases if a]
    core_title = core_abs = False
    for p, pts, tag in phrases:
        if not p:
            continue
        if _has_phrase(title, p):
            score += pts
            why.append(f"{tag}«{p}»∈title")
            if tag == "core":
                core_title = True
        elif _has_phrase(abstract, p):
            score += pts / 2
            why.append(f"{tag}«{p}»∈abs")
            if tag == "core":
                core_abs = True

    if (
        task.en.abbreviation
        and len(task.en.abbreviation) > 1
        and _has_phrase(title, task.en.abbreviation)
    ):
        score += 1.5
        why.append(f"abbr«{task.en.abbreviation}»∈title")

    # ── Chinese vocabulary ───────────────────────────────────────────────
    zh_hit = False
    for i, term in enumerate(task.zh.all_terms):
        if not term:
            continue
        if term in (paper.title or ""):
            score += 3.0 if i == 0 else 1.5
            zh_hit = True
            why.append(f"zh«{term}»∈title")
            break
        if term in (paper.abstract or ""):
            score += 1.0 if i == 0 else 0.5
            zh_hit = True
            why.append(f"zh«{term}»∈abs")
            break

    # ── Word-level fallback on the core phrase ───────────────────────────
    # Contiguous-phrase matching misses reordered/compound variants
    # ("heat transfer in a rotating ribbed channel" never contains
    # "rotating channel heat transfer"). Award partial credit when most
    # content words of the core phrase appear in the title.
    words = [w for w in _norm(task.en.primary).split() if len(w) > 2 and w not in _STOP]
    if len(words) >= 2 and not core_title:
        hit = sum(1 for w in words if _has_phrase(title, w))
        frac = hit / len(words)
        if frac >= 1.0:
            score += 2.0
            why.append("core-words∈title")
        elif frac >= 0.6:
            score += 1.0
            why.append("core-words~∈title")
        elif hit:
            score += 0.4

    # ── Related terms & context ──────────────────────────────────────────
    rel_hits = sum(0.75 for rel in task.related_terms if _has_phrase(title, rel))
    if rel_hits:
        score += min(1.5, rel_hits)
        why.append("related∈title")

    ctx_hit = next(
        (c for c in ctx_phrases if _has_phrase(title, c) or _has_phrase(abstract, c)), None
    )
    if ctx_hit:
        score += 1.0
        why.append(f"ctx:{ctx_hit}")

    if (paper.pub_type or "").lower() in _TECH_TYPES:
        score += 0.25
        why.append("tech-report")

    # ── Grade ────────────────────────────────────────────────────────────
    if core_title or score >= 5.0 or (core_abs and ctx_hit) or (zh_hit and score >= 4.0):
        grade = "HIGH"
    elif score >= 3.0 or core_abs:
        grade = "MEDIUM"
    elif score > 0:
        grade = "LOW"
    else:
        grade = "NEEDS_REVIEW" if not paper.abstract else "LOW"
    return grade, round(score, 2), why


def rank_for_download(
    scored: list[tuple[Paper, str, float, list[str]]],
) -> list[tuple[Paper, str, float]]:
    """Order accepted candidates: HIGH first, then MEDIUM; PDF hints win ties."""
    ranked = [x for x in scored if x[1] in _ACCEPT]
    ranked.sort(
        key=lambda x: (
            x[1] != "HIGH",
            not _dl_hint(x[0]),
            -x[2],
        )
    )
    return [(p, g, s) for p, g, s, _w in ranked]


def _dl_hint(paper: Paper) -> bool:
    """Cheap 'probably resolvable to a PDF' signal used for ordering only."""
    return bool(paper.pdf_url or paper.pdf_candidates or paper.is_open_access)


# ---------------------------------------------------------------------------
# PDF identity check — does the file on disk match the catalogue record?
# ---------------------------------------------------------------------------


def pdf_identity(path, paper: Paper) -> tuple[str, str]:
    """Compare embedded first-page text with the catalogue identity.

    Returns ``(status, detail)`` where status ∈ ``ok | weak | review | mismatch``.
    ``review`` covers no-text-layer and extraction failures — it does not
    condemn the file, it flags it for a human.
    """
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz  # type: ignore[no-redef]
        except ImportError:
            return "review", "pymupdf_missing"
    try:
        doc = fitz.open(str(path))
        text = " ".join(doc[i].get_text() for i in range(min(3, doc.page_count)))
        doc.close()
    except Exception as e:
        return "review", f"extract_failed:{e}"
    if len(text.strip()) < 40:
        return "review", "no_text_layer"

    tt = _norm(paper.title or "")
    tx = _norm(text)
    title_hit = bool(tt) and (
        tt[:60] in tx or difflib.SequenceMatcher(None, tt, tx[: len(tt) + 200]).ratio() > 0.55
    )
    doi_hit = bool(paper.doi) and paper.doi.lower() in (
        text.lower().replace("doi.org/", "").replace("doi:", "doi:") or ""
    )
    rep_hit = bool(paper.report_number) and paper.report_number.lower() in text.lower()
    if doi_hit or rep_hit or title_hit:
        return "ok", f"title={bool(title_hit)} doi={bool(doi_hit)} rep={bool(rep_hit)}"
    words = [w for w in tt.split() if len(w) > 3]
    frac = sum(1 for w in words if w in tx) / max(1, len(words))
    status = "weak" if frac >= 0.6 else "mismatch"
    return status, f"word_frac={frac:.2f}"
