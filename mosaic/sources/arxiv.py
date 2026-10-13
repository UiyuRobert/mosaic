"""arXiv API source (no auth required, all OA)."""

from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET

import httpx

from mosaic.errors import SourceError
from mosaic.models import Paper, SearchFilters
from mosaic.parsing import arxiv_base_id
from mosaic.sources.base import BaseSource, extract_year_range

log = logging.getLogger(__name__)

_BASE = "https://export.arxiv.org/api/query"
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
_UA = "mosaic-search/1.5 (paper search tool; https://szaghi.github.io/mosaic/)"

# Shared API throttle: every ArxivSource instance in the process waits on
# the same clock, so fan-out searches, single-paper id lookups and the web
# UI cannot burst the arXiv API. arXiv asks for ~3 s between requests from
# one client — the delay floor is enforced in the factory default, and the
# shared timestamp here means a fresh instance can never reset it.
_RATE_LOCK = threading.Lock()
_last_request_ts = 0.0

_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 5.0
_BACKOFF_MAX = 60.0
_RETRYABLE = frozenset({429, 500, 502, 503, 504})

# Queries that already use arXiv field syntax or boolean operators are sent
# through unchanged (power users / --raw-query callers).
_FIELD_SYNTAX = re.compile(r"(?i)\b(?:ti|abs|all|au|co|jr|cat|rn|id)\s*:|\b(?:AND|OR|ANDNOT)\b")


def build_arxiv_query(query: str, field: str = "all") -> str:
    """Map a free-text query to ``search_query`` arXiv syntax.

    Plain words become ``field:word AND field:word …``; a ``"quoted
    phrase"`` becomes ``field:"quoted phrase"``. Queries already written in
    arXiv syntax (field prefixes, boolean operators, grouping) are returned
    unchanged. Characters that break the query parser (``:``, unbalanced
    quotes) are stripped from bare terms.
    """
    q = query.strip()
    if not q:
        return ""
    if _FIELD_SYNTAX.search(q) or "(" in q or ")" in q:
        return q
    prefix = {"title": "ti", "abstract": "abs"}.get(field, "all")
    parts: list[str] = []
    for phrase, word in re.findall(r'"([^"]+)"|(\S+)', q):
        term = (phrase or word).replace(":", " ").strip().strip('"')
        if not term:
            continue
        if phrase or " " in term:
            parts.append(f'{prefix}:"{term}"')
        else:
            parts.append(f"{prefix}:{term}")
    return " AND ".join(parts)


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        raw = resp.headers.get("Retry-After")
    except Exception:
        return None
    try:
        return float(raw) if raw else None
    except (ValueError, TypeError):
        return None


def _feed_error(text: str) -> str:
    """Return the message inside an arXiv Atom *error entry*, or ""."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return ""
    for entry in root.findall("atom:entry", _NS):
        title_el = entry.find("atom:title", _NS)
        title = (title_el.text or "").strip() if title_el is not None else ""
        if title.lower() != "error":
            continue
        summary_el = entry.find("atom:summary", _NS)
        if summary_el is not None and summary_el.text:
            return summary_el.text.strip()
        return "arXiv API reported an error"
    return ""


class ArxivSource(BaseSource):
    name = "arXiv"

    def __init__(self, delay: float = 3.0):
        self._delay = delay

    def _throttle(self) -> None:
        """Wait until the shared inter-request delay has elapsed."""
        global _last_request_ts
        delay = max(self._delay, 0.0)
        with _RATE_LOCK:
            wait = delay - (time.monotonic() - _last_request_ts)
            if wait > 0:
                time.sleep(wait)
            _last_request_ts = time.monotonic()

    @staticmethod
    def _backoff(attempt: int, retry_after: float | None = None) -> None:
        delay = retry_after if retry_after else _BACKOFF_BASE * 2**attempt
        time.sleep(min(_BACKOFF_MAX, delay))

    def _request(self, params: dict) -> str:
        """GET the API with shared throttling + bounded retry on 429/5xx.

        Raises:
            SourceError: on persistent rate-limiting, server errors,
                malformed/non-XML responses, or API-reported query errors.
        """
        last_exc: Exception | None = None
        with httpx.Client(timeout=30, headers={"User-Agent": _UA}) as client:
            for attempt in range(_MAX_ATTEMPTS):
                self._throttle()
                try:
                    resp = client.get(_BASE, params=params)
                except (httpx.TimeoutException, httpx.ConnectError) as e:
                    last_exc = SourceError(f"arXiv API unreachable: {e}")
                    if attempt + 1 < _MAX_ATTEMPTS:
                        self._backoff(attempt)
                    continue
                if resp.status_code in _RETRYABLE:
                    last_exc = SourceError(f"arXiv API HTTP {resp.status_code}")
                    if attempt + 1 < _MAX_ATTEMPTS:
                        self._backoff(attempt, retry_after=_retry_after(resp))
                    continue
                if resp.status_code >= 400:
                    detail = _feed_error(resp.text)
                    raise SourceError(detail or f"arXiv API HTTP {resp.status_code}")
                text = resp.text.lstrip()
                if not text.startswith(("<?xml", "<feed")):
                    raise SourceError(
                        "arXiv API returned a non-XML response "
                        f"(HTTP 200, {len(resp.content)} bytes)"
                    )
                return resp.text
        raise last_exc or SourceError("arXiv API request failed")

    def search(
        self, query: str, max_results: int = 25, filters: SearchFilters | None = None
    ) -> list[Paper]:
        """Query the arXiv Atom API and return matching papers.

        Enforces a shared minimum inter-request delay and bounded retries on
        429/5xx. Applies field scoping (``ti:``, ``abs:``, ``all:``) plus
        author/journal/date filters before submitting the query.

        Args:
            query: Free-text search query.
            max_results: Maximum number of results to request from the API.
            filters: Optional filters for field scoping, authors, journal,
                and year range. ``filters.raw_query`` overrides ``query``
                entirely if set.

        Returns:
            A list of Paper objects parsed from the Atom feed.

        Raises:
            SourceError: when the API cannot be reached or reports an error.
        """
        if filters and filters.raw_query:
            search_query = filters.raw_query
        else:
            search_query = build_arxiv_query(query, filters.field if filters else "all")
        if filters:
            if filters.authors:
                for author in filters.authors:
                    a = author.replace('"', "").strip()
                    if a:
                        search_query += f' AND au:"{a}"' if " " in a else f" AND au:{a}"
            if filters.journal:
                j = filters.journal.replace('"', "").strip()
                if j:
                    search_query += f' AND jr:"{j}"' if " " in j else f" AND jr:{j}"
            y_from, y_to = extract_year_range(filters)
            if y_from or y_to:
                d_from = f"{y_from or '0000'}01010000"
                d_to = f"{y_to or '9999'}12312359"
                search_query += f" AND submittedDate:[{d_from} TO {d_to}]"

        text = self._request(
            {
                "search_query": search_query,
                "start": 0,
                "max_results": max_results,
            }
        )
        return self._parse_feed(text)

    def get_by_id(self, arxiv_id: str) -> Paper | None:
        """Fetch a single paper by arXiv ID via the ``id_list`` API parameter.

        Args:
            arxiv_id: New-style (``1706.03762`` / ``1706.03762v5``) or
                old-style (``hep-th/9901001``) identifier.

        Returns:
            The parsed Paper, or None when the API returns no entry.

        Raises:
            SourceError: on API errors (including invalid identifiers).
        """
        text = self._request({"id_list": arxiv_id, "max_results": 1})
        papers = self._parse_feed(text)
        return papers[0] if papers else None

    def _parse_feed(self, text: str) -> list[Paper]:
        """Parse an Atom feed into Papers.

        arXiv reports malformed queries as a pseudo ``<entry>`` whose title
        is ``Error`` (often with HTTP 200/400) — that entry is surfaced as
        a SourceError, never as a paper.
        """
        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            raise SourceError(f"arXiv API returned malformed XML: {e}") from e
        papers = []
        for entry in root.findall("atom:entry", _NS):
            id_el = entry.find("atom:id", _NS)
            entry_id = (id_el.text or "").strip() if id_el is not None else ""
            title_el = entry.find("atom:title", _NS)
            title = (title_el.text or "").strip() if title_el is not None else ""
            if title.lower() == "error":
                raise SourceError(_feed_error(text) or "arXiv API reported an error")
            if "/abs/" not in entry_id:
                continue  # not a real paper record
            papers.append(self._parse(entry))
        return papers

    def _parse(self, entry: ET.Element) -> Paper:
        """Parse a single Atom ``<entry>`` element into a Paper.

        Args:
            entry: An ``xml.etree.ElementTree.Element`` representing one arXiv
                result entry from the Atom feed.

        Returns:
            A Paper populated with title, authors, year, DOI, arXiv ID,
            abstract, journal reference, PDF URL, and open-access flag.
        """

        def txt(tag: str) -> str | None:
            el = entry.find(tag, _NS)
            return el.text.strip() if el is not None and el.text else None

        arxiv_id = (txt("atom:id") or "").split("/abs/")[-1]
        authors = [
            a.findtext("atom:name", namespaces=_NS) or "" for a in entry.findall("atom:author", _NS)
        ]
        published = txt("atom:published") or ""
        year = int(published[:4]) if published else None

        pdf_url = None
        for link in entry.findall("atom:link", _NS):
            href = link.get("href") or ""
            if (
                link.get("title") == "pdf"
                or link.get("type") == "application/pdf"
                or (link.get("rel") == "related" and "/pdf/" in href)
            ):
                pdf_url = href
                break
        if pdf_url:
            pdf_url = re.sub(r"^http://((?:export\.)?arxiv\.org)", r"https://\1", pdf_url)

        doi_el = entry.find("arxiv:doi", _NS)
        # Fall back to the canonical arXiv preprint DOI (strip version suffix).
        doi = (
            doi_el.text.strip()
            if doi_el is not None and doi_el.text
            else f"10.48550/arXiv.{arxiv_base_id(arxiv_id)}"
        )

        journal_el = entry.find("arxiv:journal_ref", _NS)
        journal = journal_el.text.strip() if journal_el is not None and journal_el.text else None

        return Paper(
            title=txt("atom:title") or "",
            authors=[a for a in authors if a],
            year=year,
            doi=doi,
            arxiv_id=arxiv_id,
            abstract=txt("atom:summary"),
            journal=journal,
            pdf_url=pdf_url,
            source=self.name,
            is_open_access=True,
            url=f"https://arxiv.org/abs/{arxiv_id}",
        )
