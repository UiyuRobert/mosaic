"""PDF download with candidate resolution and integrity verification.

Pipeline per paper:

    resolved candidates → fetch to ``<name>.part`` → verify PDF →
    atomic rename → record ``downloads.status='ok'``

Candidate order: source-supplied ``pdf_candidates``/``pdf_url`` → Unpaywall
OA locations (best first) → authenticated browser session (last resort).
Both the single-paper :func:`download` entry point and the queue-based
:class:`~mosaic.dlqueue.DownloadManager` share this pipeline, so native and
aria2 backends apply identical quality control.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from mosaic.db import Cache
from mosaic.errors import DownloadError
from mosaic.models import Paper, PDFCandidate
from mosaic.pdfcheck import (
    PdfVerificationError,
    dedupe_path,
    is_safe_url,
    sanitize_filename,
    verify_pdf,
)
from mosaic.sources import unpaywall

log = logging.getLogger(__name__)

_DEFAULT_MAX_BYTES = 300 * 1024 * 1024  # 300 MB sanity cap
_MAX_REDIRECTS = 6
_UA = "MOSAIC/1.5 (literature search; mailto:unpaywall@example.com)"
_VERIFY_QUICK_BYTES = 2048


def download(
    paper: Paper,
    download_dir: str,
    cache: Cache,
    unpaywall_email: str = "",
    filename_pattern: str = "{year}_{source}_{author}_{title}",
    cfg: dict | None = None,
) -> str | None:
    """
    Download PDF for a paper. Returns local path on success, None on failure.
    Tries: 1) known candidates (pdf_url/pdf_candidates)  2) Unpaywall
    3) browser session
    """
    dl_cfg = (cfg or {}).get("download", {})
    verify = dl_cfg.get("verify_pdf", True)
    max_bytes = int(dl_cfg.get("max_file_mb", 300)) * 1024 * 1024
    allow_private = dl_cfg.get("allow_private_hosts", False)

    rec = cache.get_download(paper.uid)
    if rec and rec["status"] == "ok" and rec["local_path"]:
        existing = Path(rec["local_path"])
        if existing.exists() and _quick_pdf_check(existing):
            return rec["local_path"]
        # status said ok but file is gone or not a real PDF — fall through
        # and re-download rather than trusting the stale record.

    dest_dir = Path(download_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _destination_for(paper, dest_dir, filename_pattern)

    last_error = "no pdf found"
    for cand in _candidates_for(paper, unpaywall_email):
        try:
            _fetch(
                cand.download_url,
                str(dest),
                max_bytes=max_bytes,
                verify=verify,
                allow_private=allow_private,
            )
        except DownloadError as e:
            last_error = e.reason
            log.debug("Candidate %s failed for %s: %s", cand.download_url, paper.uid, e)
            continue
        except Exception:
            log.debug("Candidate %s failed", cand.download_url, exc_info=True)
            continue
        cache.set_download(paper.uid, str(dest), "ok")
        return str(dest)

    # ── browser session (authenticated sources) ──────────────────────────────
    landing_url = paper.url or (f"https://doi.org/{paper.doi}" if paper.doi else None)
    if landing_url:
        try:
            import asyncio

            from mosaic.auth import browser_download, find_session_for_url, list_sessions

            # Resolve HTTP redirects so domain matching works for known publisher
            # URLs. For doi.org → linkinghub → sciencedirect chains the final JS
            # redirect is only followable by a real browser, so when direct
            # matching still fails we fall back to trying every saved session —
            # the browser will follow the full chain including JS redirects.
            resolved_url = _resolve_redirect(landing_url)
            session = find_session_for_url(resolved_url)
            sessions_to_try = [session] if session else [s["name"] for s in list_sessions()]

            async def _try_sessions() -> bool:
                for s in sessions_to_try:
                    if await browser_download(resolved_url, str(dest), s):
                        return True
                return False

            if asyncio.run(_try_sessions()):
                if _quick_pdf_check(Path(dest)):
                    cache.set_download(paper.uid, str(dest), "ok")
                    return str(dest)
                last_error = "invalid_pdf"
        except Exception:
            log.debug("Browser session download failed for %s", landing_url, exc_info=True)

    cache.set_download(paper.uid, "", f"error: {last_error}")
    return None


def _candidates_for(paper: Paper, unpaywall_email: str) -> list[PDFCandidate]:
    """Build the ordered PDF candidate list for *paper*.

    Order: source-supplied candidates → ``pdf_url`` → Unpaywall locations.
    The browser-session fallback is handled by the caller (not a URL).
    """
    candidates: list[PDFCandidate] = []
    seen: set[str] = set()

    def _add(c: PDFCandidate) -> None:
        if c.download_url and c.download_url not in seen:
            seen.add(c.download_url)
            candidates.append(c)

    for raw in paper.pdf_candidates:
        try:
            _add(PDFCandidate.from_dict({k: v for k, v in raw.items() if k != "filename"}))
        except TypeError:
            continue
    if paper.pdf_url:
        _add(
            PDFCandidate(
                paper_uid=paper.uid,
                download_url=paper.pdf_url,
                landing_url=paper.url or "",
                source=paper.source or "source",
                confidence=0.85,
            )
        )
    # NTRS search records may carry ``downloadsAvailable`` without the
    # attachment list — fetch it lazily so a public document is not
    # misjudged as undownloadable.
    if paper.ntrs_id and not candidates:
        for raw in _ntrs_attachment_candidates(paper):
            _add(raw)
    if paper.doi and unpaywall_email:
        for cand in unpaywall.resolve_candidates(paper.doi, unpaywall_email, paper.uid):
            _add(cand)
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    # A doi.org URL is always a 302 into a landing page — it can never
    # stream a PDF. Unpaywall records sometimes carry ``url`` pointing at
    # doi.org when no direct file exists; trying it wastes a full HTTP
    # round-trip for a guaranteed HTML result.
    return [c for c in candidates if not _is_doi_url(c.download_url)]


def _is_doi_url(url: str) -> bool:
    try:
        return (urlparse(url).hostname or "").lower() in ("doi.org", "dx.doi.org")
    except Exception:
        return False


def _ntrs_attachment_candidates(paper: Paper) -> list[PDFCandidate]:
    """Lazily resolve NTRS attachments when the record carried none."""
    try:
        from mosaic.sources.ntrs import NTRSSource

        out: list[PDFCandidate] = []
        for raw in NTRSSource().pdf_candidates_for(paper.ntrs_id or ""):
            try:
                cand = PDFCandidate.from_dict({k: v for k, v in raw.items() if k != "filename"})
            except TypeError:
                continue
            cand.paper_uid = paper.uid
            out.append(cand)
        return out
    except Exception:
        log.debug("NTRS attachment fetch failed for %s", paper.ntrs_id, exc_info=True)
        return []


def _quick_pdf_check(path: Path) -> bool:
    """Cheap sanity check used for skip/decision paths: %PDF- header + size."""
    try:
        if path.stat().st_size < 64:
            return False
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def _destination_for(paper: Paper, dest_dir: Path, pattern: str) -> Path:
    """Compute a collision-free destination path inside *dest_dir*."""
    name = sanitize_filename(paper.safe_filename(pattern))
    dest = (dest_dir / name).resolve()
    # Path-traversal guard: the filename must stay inside dest_dir.
    if dest.parent != dest_dir.resolve():
        dest = dest_dir / sanitize_filename(name.replace("/", "_"))
    if dest.exists() and _quick_pdf_check(dest):
        # Occupied by a valid PDF — likely a different paper with an
        # identical generated name. Never overwrite it.
        dest = dedupe_path(dest)
    return dest


def _classify_http(resp: httpx.Response) -> DownloadError:
    code = resp.status_code
    if code == 404:
        return DownloadError("not_found", f"HTTP 404 for {resp.url}")
    if code in (401, 403):
        return DownloadError("access_denied", f"HTTP {code} for {resp.url}")
    if code == 429:
        retry = resp.headers.get("Retry-After")
        try:
            ra = float(retry) if retry else None
        except ValueError:
            ra = None
        return DownloadError("rate_limited", f"HTTP 429 for {resp.url}", retry_after=ra)
    if code >= 500:
        return DownloadError("server_error", f"HTTP {code} for {resp.url}")
    return DownloadError("unreachable", f"HTTP {code} for {resp.url}")


def _classify_exc(exc: Exception) -> DownloadError:
    if isinstance(exc, DownloadError):
        return exc
    if isinstance(exc, PdfVerificationError):
        return DownloadError(exc.reason, str(exc))
    if isinstance(exc, httpx.TimeoutException):
        return DownloadError("timeout", str(exc))
    if isinstance(exc, httpx.ConnectError):
        msg = str(exc).lower()
        reason = "dns_error" if "name" in msg or "dns" in msg or "resolve" in msg else "unreachable"
        return DownloadError(reason, str(exc))
    if isinstance(exc, FileExistsError):
        return DownloadError("exists", str(exc))
    if isinstance(exc, OSError):
        if exc.errno == 28:  # ENOSPC
            return DownloadError("disk_full", str(exc))
        return DownloadError("unreachable", str(exc))
    return DownloadError("unreachable", str(exc))


def _fetch(
    url: str,
    dest: str,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    verify: bool = True,
    progress: object = None,
    abort_check: object = None,
    allow_private: bool = False,
) -> str:
    """Fetch *url* into *dest* via a temp file + verification + atomic rename.

    Redirects are followed manually so every hop can be re-validated for
    URL safety (scheme + public host). *progress*, when provided, is called
    as ``progress(downloaded_bytes, total_bytes_or_0)`` while streaming.
    *abort_check*, when provided, is called per chunk and should raise
    ``DownloadError`` to abort (used for pause/cancel). Returns the final
    URL the content was downloaded from (after redirects).

    Raises:
        DownloadError: with a classified ``reason``.
    """
    final_url = url
    hops = 0
    ok, why = is_safe_url(final_url, allow_private=allow_private)
    if not ok:
        raise DownloadError("unsafe_url", f"refusing {final_url}: {why}")

    part = Path(f"{dest}.part")
    try:
        with httpx.Client(
            timeout=httpx.Timeout(60.0, read=120.0),
            follow_redirects=False,
            headers={"User-Agent": _UA, "Accept": "application/pdf,*/*;q=0.8"},
        ) as client:
            while True:
                try:
                    with client.stream("GET", final_url) as r:
                        if r.is_redirect:
                            location = r.headers.get("Location")
                            if not location or hops >= _MAX_REDIRECTS:
                                raise DownloadError(
                                    "unreachable",
                                    f"redirect without target or too many hops from {final_url}",
                                )
                            final_url = urljoin(final_url, location)
                            hops += 1
                            ok, why = is_safe_url(final_url, allow_private=allow_private)
                            if not ok:
                                raise DownloadError(
                                    "unsafe_url", f"refusing redirect to {final_url}: {why}"
                                )
                            continue
                        if r.status_code >= 400:
                            raise _classify_http(r)
                        content_length = 0
                        try:
                            content_length = int(r.headers.get("Content-Length") or 0)
                        except ValueError:
                            pass
                        if abort_check:
                            abort_check()
                        with open(part, "wb") as f:
                            total = 0
                            for chunk in r.iter_bytes(8192):
                                if abort_check:
                                    abort_check()
                                total += len(chunk)
                                if total > max_bytes:
                                    raise DownloadError(
                                        "invalid_pdf",
                                        f"file exceeds {max_bytes} byte limit",
                                    )
                                f.write(chunk)
                                if progress:
                                    progress(total, content_length)
                except httpx.InvalidURL as e:
                    raise DownloadError("unsafe_url", str(e)) from e
                break
        if verify:
            try:
                verify_pdf(part)
            except PdfVerificationError as e:
                raise DownloadError(e.reason, str(e)) from e
        os.replace(part, dest)
        return final_url
    except DownloadError:
        part.unlink(missing_ok=True)
        raise
    except Exception as e:
        part.unlink(missing_ok=True)
        raise _classify_exc(e) from e


def _resolve_redirect(url: str) -> str:
    """Follow HTTP redirects and return the final URL (e.g. doi.org → publisher)."""
    try:
        with httpx.Client(follow_redirects=True, timeout=10) as client:
            r = client.head(url)
            return str(r.url)
    except Exception:
        log.debug("Redirect resolution failed for %s", url, exc_info=True)
        return url


def host_of(url: str) -> str:
    """Return the lowercase hostname for per-host rate limiting."""
    return (urlparse(url).hostname or "").lower()
