"""Unpaywall — resolve OA PDF candidates for any DOI.

``resolve()`` keeps the original single-best-URL contract for existing
callers. ``resolve_candidates()`` exposes every OA location Unpaywall
knows about, ordered best-first, carrying version/license metadata so the
downloader can try fallbacks instead of giving up after the first URL.
"""

from __future__ import annotations

import logging

import httpx

from mosaic.models import PDFCandidate

log = logging.getLogger(__name__)

_BASE = "https://api.unpaywall.org/v2/{doi}"


def resolve(doi: str, email: str) -> str | None:
    """Return the best OA PDF URL for a DOI, or None if not found."""
    candidates = resolve_candidates(doi, email)
    return candidates[0].download_url if candidates else None


def resolve_candidates(doi: str, email: str, paper_uid: str = "") -> list[PDFCandidate]:
    """Return all OA download candidates Unpaywall reports for *doi*.

    The best OA location comes first (``url_for_pdf`` when present, else the
    landing ``url`` — which may be HTML and is flagged with lower confidence
    so it is only tried after real PDF links). Remaining ``oa_locations``
    follow in API order.
    """
    if not doi or not email:
        return []
    try:
        resp = httpx.get(_BASE.format(doi=doi), params={"email": email}, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []

    if not data.get("is_oa"):
        return []

    locations: list[dict] = []
    seen_urls: set[str] = set()
    best = data.get("best_oa_location")
    for loc in [best, *(data.get("oa_locations") or [])]:
        if not loc:
            continue
        key = loc.get("url_for_pdf") or loc.get("url") or ""
        if key and key not in seen_urls:
            seen_urls.add(key)
            locations.append(loc)

    candidates: list[PDFCandidate] = []
    for i, loc in enumerate(locations):
        url_pdf = loc.get("url_for_pdf")
        url = url_pdf or loc.get("url")
        if not url:
            continue
        # Direct PDF links outrank landing pages; best location outranks rest.
        confidence = 0.9 - i * 0.05
        if not url_pdf:
            confidence -= 0.3
        candidates.append(
            PDFCandidate(
                paper_uid=paper_uid,
                download_url=url,
                landing_url=loc.get("url_for_landing_page") or loc.get("url") or "",
                source="unpaywall",
                version=loc.get("version") or "",
                license=loc.get("license") or "",
                is_open_access=True,
                confidence=max(0.1, confidence),
                discovered_at="",
            )
        )
    return candidates
