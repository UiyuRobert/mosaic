"""Unified paper data model."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# Identifier-type priority for canonical-UID selection (lower wins).
IDENT_PRIORITY = {"doi": 0, "arxiv": 1, "pii": 2, "ntrs": 3, "title": 4}


def ident_rank(identifier: str) -> int:
    """Rank an identifier key (``doi:x``, ``ntrs:x``…) by its type priority."""
    return IDENT_PRIORITY.get(identifier.split(":", 1)[0], 5)


def canonical_identifier(identifiers: set[str]) -> str:
    """Return the highest-priority identifier from a set of alias keys."""
    return min(identifiers, key=ident_rank) if identifiers else ""


@dataclass
class SearchFilters:
    """Filters applied both at the API level (where supported) and as post-processing."""

    year_from: int | None = None
    year_to: int | None = None
    years: list[int] | None = None  # explicit list; mutually exclusive with from/to range
    authors: list[str] = field(default_factory=list)
    journal: str = ""
    # Field-scoping: "title" | "abstract" | "all" (default)
    field: str = "all"
    # Raw query override: sent directly to the source API, bypassing field transformation
    raw_query: str = ""

    def match(self, paper: Paper) -> bool:
        """Return True if paper passes all active filters."""
        if paper.year is not None:
            if self.years is not None:
                if paper.year not in self.years:
                    return False
            else:
                if self.year_from is not None and paper.year < self.year_from:
                    return False
                if self.year_to is not None and paper.year > self.year_to:
                    return False
        if self.authors:
            combined = " ".join(paper.authors).lower()
            if not any(a.lower() in combined for a in self.authors):
                return False
        return not (
            self.journal
            and (not paper.journal or self.journal.lower() not in paper.journal.lower())
        )

    @staticmethod
    def parse_year(value: str) -> SearchFilters:
        """
        Parse a year string into a SearchFilters instance.
          "2020"           → exact year
          "2020-2024"      → inclusive range
          "2020,2022,2024" → explicit list
        """
        f = SearchFilters()
        value = value.strip()
        if "," in value:
            f.years = [int(y.strip()) for y in value.split(",")]
        elif "-" in value:
            parts = value.split("-")
            f.year_from, f.year_to = int(parts[0]), int(parts[1])
        else:
            yr = int(value)
            f.year_from = f.year_to = yr
        return f


class PaperStatus(StrEnum):
    """Lifecycle state of a paper's full-text availability.

    A search result always carries metadata; the status reflects how far
    full-text acquisition has progressed.
    """

    METADATA_ONLY = "metadata_only"
    OA_AVAILABLE = "oa_available"
    PDF_RESOLVED = "pdf_resolved"
    DOWNLOADED = "downloaded"
    NO_OA_FOUND = "no_oa_found"
    ACCESS_RESTRICTED = "access_restricted"
    DOWNLOAD_FAILED = "download_failed"


@dataclass
class PDFCandidate:
    """A single downloadable-PDF candidate for a paper.

    Ordered collection of these is built by ``downloader._candidates_for``:
    direct source URLs first, then Unpaywall best/other locations, then the
    authenticated browser-session fallback.
    """

    paper_uid: str
    download_url: str
    landing_url: str = ""
    source: str = ""  # 'source' | 'ntrs' | 'unpaywall' | 'browser'
    version: str = ""  # unpaywall: submitted/accepted/publishedVersion
    license: str = ""
    is_open_access: bool = True
    confidence: float = 0.5  # 0..1 — used for ordering
    discovered_at: str = ""  # ISO timestamp

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> PDFCandidate:
        return cls(**d)


@dataclass
class Paper:
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    pii: str | None = None
    abstract: str | None = None
    journal: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    pdf_url: str | None = None
    source: str = ""
    is_open_access: bool = False
    url: str | None = None
    citation_count: int | None = None
    relevance_score: float | None = None
    openalex_id: str | None = None
    # ── Additional identifiers / metadata (appended: backward-compatible) ──
    ntrs_id: str | None = None  # NASA NTRS 11-digit document ID
    report_number: str | None = None  # report number e.g. "NASA/TM-2020-12345"
    pub_type: str | None = None  # human-readable doc type e.g. "Technical Memorandum"
    pdf_candidates: list[dict] = field(default_factory=list)  # serialised PDFCandidate dicts

    def to_dict(self) -> dict:
        """Serialise to a plain dict (thread-safe passing, JSON export)."""
        from dataclasses import asdict

        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Paper:
        """Reconstruct a Paper from a dict produced by ``to_dict``."""
        return cls(**d)

    @property
    def uid(self) -> str:
        """Best available unique identifier."""
        if self.doi:
            doi_lower = self.doi.lower()
            # Normalize arXiv DOIs (10.48550/arxiv.XXXX) to arxiv: prefix so
            # the same paper from arXiv + OpenAlex/EPMC deduplicates correctly.
            if doi_lower.startswith("10.48550/arxiv."):
                return f"arxiv:{doi_lower.removeprefix('10.48550/arxiv.')}"
            return f"doi:{doi_lower}"
        if self.arxiv_id:
            return f"arxiv:{self.arxiv_id}"
        if self.pii:
            return f"pii:{self.pii}"
        if self.ntrs_id:
            return f"ntrs:{self.ntrs_id}"
        return f"title:{self.title.lower()[:80]}"

    @property
    def identifiers(self) -> set[str]:
        """All known identifier keys for this paper (doi/arxiv/pii/ntrs + uid).

        Used by alias-aware deduplication: two records describing the same
        work may carry different subsets of identifiers; matching on any
        single strong identifier is enough to merge them.
        """
        ids: set[str] = {self.uid}
        if self.doi:
            doi_lower = self.doi.lower()
            if doi_lower.startswith("10.48550/arxiv."):
                ids.add(f"arxiv:{doi_lower.removeprefix('10.48550/arxiv.')}")
            else:
                ids.add(f"doi:{doi_lower}")
        if self.arxiv_id:
            ids.add(f"arxiv:{self.arxiv_id}")
        if self.pii:
            ids.add(f"pii:{self.pii}")
        if self.ntrs_id:
            ids.add(f"ntrs:{self.ntrs_id}")
        return ids

    @property
    def short_authors(self) -> str:
        if not self.authors:
            return "Unknown"
        if len(self.authors) == 1:
            return self.authors[0]
        if len(self.authors) == 2:
            return f"{self.authors[0]} & {self.authors[1]}"
        return f"{self.authors[0]} et al."

    def safe_filename(self, pattern: str = "{year}_{source}_{author}_{title}") -> str:
        import re

        def _slug(text: str, max_len: int = 60) -> str:
            s = re.sub(r"[^\w\s-]", "", text)[:max_len].strip()
            return re.sub(r"\s+", "_", s)

        year = str(self.year) if self.year else "0000"
        source = _slug(self.source)
        author = _slug(self.short_authors.split()[0] if self.authors else "Unknown")
        title = _slug(self.title)
        doi = re.sub(r"[^\w.-]", "_", self.doi) if self.doi else "no_doi"
        journal = _slug(self.journal) if self.journal else "no_journal"

        name = pattern.format(
            year=year, source=source, author=author, title=title, doi=doi, journal=journal
        )
        # strip any remaining filesystem-unsafe chars and collapse separators
        name = re.sub(r"[^\w\s.\-_]", "", name).strip("_")
        return name + ".pdf"
