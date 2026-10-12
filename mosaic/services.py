"""Shared business logic used by both the CLI and the web UI."""

from __future__ import annotations

from mosaic.models import Paper, SearchFilters


def build_filters(
    year: str = "",
    author: str | list[str] = "",
    journal: str = "",
    field: str = "all",
    raw_query: str = "",
) -> tuple[SearchFilters | None, str | None]:
    """Build a ``SearchFilters`` from user input.

    Args:
        year: Year string (``"2020"``, ``"2020-2024"``, or ``"2020,2022,2024"``).
        author: Comma-separated author string **or** a list of author names.
        journal: Journal name substring.
        field: ``"title"``, ``"abstract"``, or ``"all"``.
        raw_query: Raw query override.

    Returns:
        A tuple ``(filters_or_None, warning_or_None)``.  The warning is set
        when the year string cannot be parsed; other fields are still applied.
    """
    # Normalise author to a list
    if isinstance(author, str):
        authors = [a.strip() for a in author.split(",") if a.strip()] if author else []
    else:
        authors = list(author)

    if not any([year, authors, journal, field != "all", raw_query]):
        return None, None

    filters = SearchFilters(authors=authors, journal=journal, field=field, raw_query=raw_query)
    warning: str | None = None

    if year:
        try:
            parsed = SearchFilters.parse_year(year)
            filters.year_from = parsed.year_from
            filters.year_to = parsed.year_to
            filters.years = parsed.years
        except ValueError:
            warning = f'Invalid year format "{year}". Use: 2020, 2020-2024, or 2020,2022,2024'

    return filters, warning


def filter_papers(
    papers: list[Paper],
    *,
    oa_only: bool = False,
    pdf_only: bool = False,
    sort_by: str = "",
) -> list[Paper]:
    """Apply post-processing filters to a list of papers.

    Args:
        papers: Papers to filter (not mutated; a new list is returned).
        oa_only: Keep only open-access papers (or those with a PDF URL).
        pdf_only: Keep only papers that have a PDF URL.
        sort_by: ``"citations"`` (most cited first) or ``"year"`` (newest first).

    Returns:
        A filtered (and optionally sorted) list of papers.
    """
    result = list(papers)
    if oa_only:
        result = [p for p in result if p.is_open_access or p.pdf_url]
    if pdf_only:
        result = [p for p in result if p.pdf_url]
    if sort_by == "citations":
        result.sort(key=lambda p: p.citation_count or 0, reverse=True)
    elif sort_by == "year":
        result.sort(key=lambda p: p.year or 0, reverse=True)
    return result


def sort_by_relevance(query: str, papers: list[Paper], cfg: dict) -> list[Paper]:
    """Score and sort *papers* by relevance to *query* (highest first).

    Uses BM25 by default; falls back to an LLM scorer when ``cfg["llm"]`` is configured.
    """
    from mosaic.ranking import score_papers

    scored = score_papers(query, papers, cfg)
    return sorted(scored, key=lambda p: p.relevance_score or 0.0, reverse=True)


def _merge_fields(existing: Paper, paper: Paper) -> None:
    """Fill missing fields on *existing* from *paper* (non-destructive merge)."""
    if paper.abstract and not existing.abstract:
        existing.abstract = paper.abstract
    if paper.pdf_url and not existing.pdf_url:
        existing.pdf_url = paper.pdf_url
    if paper.doi and not existing.doi:
        existing.doi = paper.doi
    if paper.arxiv_id and not existing.arxiv_id:
        existing.arxiv_id = paper.arxiv_id
    if paper.pii and not existing.pii:
        existing.pii = paper.pii
    if paper.ntrs_id and not existing.ntrs_id:
        existing.ntrs_id = paper.ntrs_id
    if paper.report_number and not existing.report_number:
        existing.report_number = paper.report_number
    if paper.pub_type and not existing.pub_type:
        existing.pub_type = paper.pub_type
    if paper.url and not existing.url:
        existing.url = paper.url
    if paper.openalex_id and not existing.openalex_id:
        existing.openalex_id = paper.openalex_id
    if paper.is_open_access and not existing.is_open_access:
        existing.is_open_access = True
    if paper.pdf_candidates:
        known = {c.get("download_url") for c in existing.pdf_candidates}
        existing.pdf_candidates.extend(
            c for c in paper.pdf_candidates if c.get("download_url") not in known
        )
    if paper.citation_count is not None and (
        existing.citation_count is None or paper.citation_count > existing.citation_count
    ):
        existing.citation_count = paper.citation_count


def merge_papers(
    seen: dict[str, Paper],
    paper: Paper,
    ident_index: dict[str, str] | None = None,
) -> None:
    """Merge *paper* into *seen*, preferring richer metadata.

    Deduplication is identifier-aware: when *ident_index* is provided, a
    paper whose DOI/arXiv/PII/NTRS identifier matches an existing record
    under a different UID is merged into that record (e.g. an NTRS entry
    later found via its DOI).  Citation counts are updated only when the
    new count is higher.
    """
    uid = paper.uid

    if ident_index is not None:
        # First-seen record sharing any strong identifier → merge into it.
        for ident in paper.identifiers:
            existing_uid = ident_index.get(ident)
            if existing_uid and existing_uid != uid and existing_uid in seen:
                existing = seen[existing_uid]
                _merge_fields(existing, paper)
                new_uid = existing.uid  # may change after field merge
                if new_uid != existing_uid:
                    del seen[existing_uid]
                    if new_uid in seen:
                        _merge_fields(seen[new_uid], existing)
                        existing = seen[new_uid]
                    else:
                        seen[new_uid] = existing
                for i in existing.identifiers:
                    ident_index[i] = existing.uid
                return

    if uid not in seen:
        seen[uid] = paper
        if ident_index is not None:
            for i in paper.identifiers:
                ident_index[i] = uid
        return
    _merge_fields(seen[uid], paper)
    if ident_index is not None:
        for i in seen[uid].identifiers:
            ident_index[i] = seen[uid].uid
