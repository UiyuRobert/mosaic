"""NASA NTRS — Scientific and Technical Information Repository source.

Implements the public NASA STI OpenAPI:

- ``POST /api/citations/search``            — metadata search (JSON body)
- ``GET  /api/citations/{id}``              — single citation record
- ``GET  /api/citations/{id}/downloads``    — attachment list
- ``GET  /api/citations/{id}/downloads/{filename}`` — file download

Terms of service (NASA STI OpenAPI documentation, 2022-11-22):

- Rate limit: 500 requests / 15 minutes; max 10,000 records per query;
  ``page.size`` <= 100.
- Redistribution: availability can change; when citing, prefer the record
  landing page ``https://ntrs.nasa.gov/citations/{id}`` over download URLs.
- Only ``distribution == "PUBLIC"`` records are served; non-public
  attachments are never requested.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urljoin

import httpx

from mosaic.errors import SourceError
from mosaic.models import Paper, SearchFilters
from mosaic.parsing import normalise_doi, parse_year
from mosaic.sources.base import BaseSource, extract_year_range

log = logging.getLogger(__name__)

_BASE = "https://ntrs.nasa.gov"
_API = f"{_BASE}/api"
_PAGE_MAX = 100  # API limit for page.size
_MAX_RETRIES = 3

# Human-readable labels for documented stiType enum values.
_STI_TYPE_LABELS = {
    "ACCEPTED_MANUSCRIPT": "Accepted Manuscript",
    "BOOK": "Book",
    "BOOK_CHAPTER": "Book Chapter",
    "CONFERENCE_PAPER": "Conference Paper",
    "CONFERENCE_PROCEEDINGS": "Conference Proceedings",
    "CONFERENCE_PUBLICATION": "Conference Publication",
    "CONTRACTOR_OR_GRANTEE_REPORT": "Contractor/Grantee Report",
    "CONTRACTOR_REPORT": "Contractor Report",
    "CONTRIBUTION_TO_LARGER_WORK": "Contribution to Larger Work",
    "STI_TYPE_NONE": "None",
    "OTHER": "Other",
    "POSTER": "Poster",
    "PREPRINT": "Preprint",
    "PRESENTATION": "Presentation",
    "REPRINT": "Reprint",
    "SPECIAL_PUBLICATION": "Special Publication",
    "TECHNICAL_MEMORANDUM": "Technical Memorandum",
    "TECHNICAL_PUBLICATION": "Technical Publication",
    "TECHNICAL_TRANSLATION": "Technical Translation",
    "THESIS_DISSERTATION": "Thesis/Dissertation",
    "VIDEO": "Video",
    "WHITE_PAPER": "White Paper",
}

_UA = "MOSAIC/1.5 (https://github.com/szaghi/mosaic; literature search client)"


class NTRSSource(BaseSource):
    """Search source for the NASA STI Repository (NTRS).

    Covers NASA technical reports (TM/TP/CR/SP), conference papers,
    contractor reports, theses and related documents. No API key required.
    Many records lack a DOI — they carry a stable ``ntrs:{id}`` UID instead.
    """

    name = "NASA NTRS"

    def __init__(self, delay: float = 1.0, base_url: str = _BASE) -> None:
        """Initialise the NTRS source.

        Args:
            delay: Minimum seconds between paginated API requests
                (rate-limit politeness; the service allows 500 req / 15 min).
            base_url: API host root — overridable for tests/mock servers.
        """
        self._delay = max(0.0, delay)
        self._base = base_url.rstrip("/")
        self._last_request = 0.0

    # -- HTTP plumbing -------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)

    def _request(self, method: str, path: str, *, json_body: dict | None = None) -> httpx.Response:
        """Issue an API request with bounded retries on 429/5xx.

        Honours ``Retry-After`` on HTTP 429 and applies exponential backoff
        for transient 5xx responses. Raises SourceError on persistent
        failure, client errors (4xx other than 429) or network problems.
        """
        url = f"{self._base}{path}"
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            self._throttle()
            try:
                resp = httpx.request(
                    method,
                    url,
                    json=json_body,
                    headers={"User-Agent": _UA, "Accept": "application/json"},
                    timeout=30,
                    follow_redirects=True,
                )
                self._last_request = time.monotonic()
            except httpx.TimeoutException as e:
                last_exc = e
                self._sleep_backoff(attempt, None)
                continue
            except httpx.HTTPError as e:
                raise SourceError(f"NTRS request failed: {e}") from e

            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                last_exc = SourceError("NTRS rate limit exceeded (HTTP 429)")
                self._sleep_backoff(attempt, retry_after)
                continue
            if resp.status_code >= 500:
                last_exc = SourceError(f"NTRS server error (HTTP {resp.status_code})")
                self._sleep_backoff(attempt, None)
                continue
            if resp.status_code >= 400:
                raise SourceError(
                    f"NTRS request rejected (HTTP {resp.status_code}): {resp.text[:200]}"
                )
            return resp

        raise SourceError(f"NTRS request failed after {_MAX_RETRIES} attempts: {last_exc}")

    def _sleep_backoff(self, attempt: int, retry_after: float | None) -> None:
        delay = retry_after if retry_after is not None else 2.0**attempt
        time.sleep(min(delay, 60.0))

    # -- Search --------------------------------------------------------------

    def search(
        self,
        query: str,
        max_results: int = 25,
        filters: SearchFilters | None = None,
    ) -> list[Paper]:
        """Search NTRS citations.

        Maps ``SearchFilters`` onto the API body: ``field`` scopes the query
        to title/abstract/free-text, year ranges map to ``published.gte/lte``,
        the first author maps to the (case-sensitive) ``author`` parameter.
        ``journal`` filtering has no API equivalent and is left to the
        framework post-processor, as is the explicit ``filters.years`` list
        (translated to a spanning range server-side, narrowed locally).

        Returns:
            Papers including metadata-only records (no public document).
        """
        body = self._build_body(query, filters)
        papers: list[Paper] = []
        offset = 0
        while len(papers) < max_results:
            body["page"] = {
                "size": min(_PAGE_MAX, max_results - len(papers)),
                "from": offset,
            }
            resp = self._request("POST", "/api/citations/search", json_body=body)
            try:
                data = resp.json()
            except ValueError as e:
                raise SourceError(f"NTRS returned invalid JSON: {e}") from e
            results = data.get("results") or []
            if not results:
                break
            for rec in results:
                paper = self._parse(rec)
                if paper is not None:
                    papers.append(paper)
            offset += len(results)
            if len(results) < body["page"]["size"]:
                break  # last page
        return papers

    def _build_body(self, query: str, filters: SearchFilters | None) -> dict:
        body: dict[str, Any] = {}
        if filters and filters.raw_query:
            body["q"] = filters.raw_query
        elif filters and filters.field == "title":
            body["title"] = query
        elif filters and filters.field == "abstract":
            body["abstract"] = query
        else:
            body["q"] = query

        if filters:
            y_from, y_to = extract_year_range(filters)
            published: dict[str, str] = {}
            if y_from:
                published["gte"] = str(y_from)
            if y_to:
                published["lte"] = str(y_to)
            if published:
                body["published"] = published
            if filters.authors:
                # API accepts a single, case-sensitive author value.
                body["author"] = filters.authors[0]
        return body

    # -- Record parsing ------------------------------------------------------

    def _parse(self, rec: dict) -> Paper | None:
        """Map one NTRS citation record to a Paper.

        Returns ``None`` for non-public records. Metadata-only records are
        kept (no pdf_url, is_open_access=False) so users can still cite them.
        """
        ntrs_id = str(rec.get("id") or "").strip()
        if not ntrs_id:
            return None

        # Never surface non-public records.
        if rec.get("distribution") not in (None, "PUBLIC"):
            return None

        title = (rec.get("title") or "").strip()
        if not title:
            return None

        authors = _parse_authors(rec)
        doi = _parse_doi(rec)
        year = _parse_year(rec)
        pub_type = _parse_pub_type(rec)
        report_number = _parse_report_number(rec)
        journal, volume, issue = _parse_publication(rec)
        pdf_url, candidates = self._parse_downloads(rec, ntrs_id)

        has_doc = bool(candidates) or bool(rec.get("downloadsAvailable"))
        is_oa = bool(rec.get("distribution") == "PUBLIC" and has_doc)

        return Paper(
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            abstract=rec.get("abstract") or None,
            journal=journal,
            volume=volume,
            issue=issue,
            pdf_url=pdf_url,
            source=self.name,
            is_open_access=is_oa,
            url=f"{self._base}/citations/{ntrs_id}",
            ntrs_id=ntrs_id,
            report_number=report_number,
            pub_type=pub_type,
            pdf_candidates=candidates,
        )

    def _parse_downloads(self, rec: dict, ntrs_id: str) -> tuple[str | None, list[dict]]:
        """Extract a best PDF URL plus the full candidate list.

        Selection strategy (deterministic, explainable): among non-draft
        attachments, prefer ``type == "STI"`` (full document) over
        ``"ABSTRACT"``, then entries whose ``links.pdf`` exists, then
        ``links.original`` when the mimetype/name indicates PDF. Only
        http(s) links emitted by the API are used — never synthesised.

        Returns:
            ``(best_pdf_url_or_None, candidate_dicts)``.
        """
        downloads = rec.get("downloads") or []
        candidates: list[dict] = []
        landing = f"{self._base}/citations/{ntrs_id}"

        def _rank(entry: dict) -> tuple[int, int]:
            links = entry.get("links") or {}
            is_full = 1 if entry.get("type") == "STI" else 0
            return (is_full, 1 if links.get("pdf") else 0)

        for entry in sorted(downloads, key=_rank, reverse=True):
            if entry.get("draft"):
                continue
            links = entry.get("links") or {}
            url = None
            if links.get("pdf"):
                url = links["pdf"]
            elif _is_pdfish(entry) and links.get("original"):
                url = links["original"]
            if not url:
                continue
            candidates.append(
                {
                    "paper_uid": f"ntrs:{ntrs_id}",
                    "download_url": urljoin(self._base, url),
                    "landing_url": landing,
                    "source": "ntrs",
                    "version": "",
                    "license": "",
                    "is_open_access": True,
                    "confidence": 0.95 if entry.get("type") == "STI" else 0.7,
                    "discovered_at": "",
                    "filename": entry.get("name") or "",
                }
            )

        best = candidates[0]["download_url"] if candidates else None
        return best, candidates

    # -- Auxiliary API -------------------------------------------------------

    def get_citation(self, ntrs_id: str) -> Paper | None:
        """Fetch a single citation record by NTRS document ID."""
        resp = self._request("GET", f"/api/citations/{ntrs_id}")
        try:
            return self._parse(resp.json())
        except ValueError as e:
            raise SourceError(f"NTRS returned invalid JSON: {e}") from e

    def get_downloads(self, ntrs_id: str) -> list[dict]:
        """Return the raw attachment list for a citation."""
        resp = self._request("GET", f"/api/citations/{ntrs_id}/downloads")
        try:
            data = resp.json()
        except ValueError as e:
            raise SourceError(f"NTRS returned invalid JSON: {e}") from e
        if isinstance(data, list):
            return data
        return data.get("downloads") or []


# ---------------------------------------------------------------------------
# Field extraction helpers (defensive — the API schema has evolved)
# ---------------------------------------------------------------------------


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _parse_authors(rec: dict) -> list[str]:
    affs = rec.get("authorAffiliations") or rec.get("authors") or []
    keyed = []
    for i, aff in enumerate(affs):
        if not isinstance(aff, dict):
            continue
        meta = aff.get("meta") or {}
        author = meta.get("author") or {}
        name = (author.get("name") or "").strip()
        if name:
            keyed.append((aff.get("sequence", i), name))
    keyed.sort(key=lambda kv: kv[0] if isinstance(kv[0], int) else 0)
    return [name for _seq, name in keyed]


def _parse_doi(rec: dict) -> str | None:
    for pub in rec.get("publications") or []:
        doi = normalise_doi(pub.get("doi"))
        if doi:
            return doi
    for ident in rec.get("sourceIdentifiers") or []:
        if str(ident.get("type", "")).upper() == "DOI":
            doi = normalise_doi(ident.get("number"))
            if doi:
                return doi
    return None


def _parse_year(rec: dict) -> int | None:
    """Pick the most meaningful year: first publication date, else
    distribution/release date, else record creation date."""
    for pub in rec.get("publications") or []:
        year = parse_year(pub.get("publicationDate") or pub.get("issuePublicationDate"))
        if year:
            return year
    for field in ("distributionDate", "created", "submittedDate"):
        year = parse_year(rec.get(field))
        if year:
            return year
    return None


def _sti_type_label(value: str) -> str:
    if not value:
        return ""
    if value in _STI_TYPE_LABELS:
        return _STI_TYPE_LABELS[value]
    return value.replace("_", " ").title()


def _parse_pub_type(rec: dict) -> str | None:
    label = _sti_type_label(rec.get("stiType") or "")
    details = rec.get("stiTypeDetails")
    if isinstance(details, dict):
        detail = details.get("type") or details.get("additionalInformation") or ""
    else:
        detail = details or ""
    if detail and detail != label:
        return f"{label} ({detail})" if label else str(detail)
    return label or None


def _parse_report_number(rec: dict) -> str | None:
    raw = rec.get("otherReportNumbers") or rec.get("reportNumber")
    if isinstance(raw, list):
        return str(raw[0]) if raw else None
    return str(raw) if raw else None


def _parse_publication(rec: dict) -> tuple[str | None, str | None, str | None]:
    for pub in rec.get("publications") or []:
        name = pub.get("publicationName") or pub.get("publisher")
        if name:
            return (
                str(name),
                _str_or_none(pub.get("volume")),
                _str_or_none(pub.get("issue")),
            )
    return None, None, None


def _str_or_none(value) -> str | None:
    return str(value) if value not in (None, "") else None


def _is_pdfish(entry: dict) -> bool:
    """True when mimetype or filename indicates the attachment is a PDF."""
    mimetype = (entry.get("mimetype") or "").lower()
    name = (entry.get("name") or "").lower()
    return "pdf" in mimetype or name.endswith(".pdf")
