"""PDF verification, safe filenames and URL safety checks.

Shared by the native download pipeline and the download queue finaliser so
that both backends apply identical quality control before a file is marked
as successfully downloaded.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Minimum plausible size for a real PDF document.
MIN_PDF_BYTES = 1024
# Bytes tolerated after %%EOF when scanning the tail (trailing whitespace,
# incremental-update markers some producers append).
_EOF_TAIL = 4096


class PdfVerificationError(Exception):
    """Raised when a downloaded file fails PDF verification."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(detail or reason)


def looks_like_html(sample: bytes) -> bool:
    """Return True if the byte sample looks like an HTML/XML error page."""
    head = sample[:2048].lstrip().lower()
    return (
        head.startswith((b"<!doctype html", b"<html", b"<?xml", b"<head", b"<!--"))
        or b"<html" in head[:512]
    )


def sha256_file(path: str | Path) -> str:
    """Compute the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_pdf(
    path: str | Path,
    *,
    full_check: bool = True,
    min_bytes: int = MIN_PDF_BYTES,
) -> dict:
    """Verify that *path* is a plausible, parseable PDF.

    Checks (in order): non-empty file, minimum size, ``%PDF-`` magic header,
    ``%%EOF`` marker in the tail, and — when *full_check* is set and
    ``pymupdf`` is installed — that the document opens and has >= 1 page.

    Args:
        path: File to verify.
        full_check: Also open the document with pymupdf when available.
        min_bytes: Minimum acceptable file size.

    Returns:
        A dict ``{"ok": True, "pages": int|None, "size": int, "sha256": str}``.

    Raises:
        PdfVerificationError: with ``reason`` in
            ``empty | too_small | not_pdf | html_response | missing_eof |
            unparseable``.
    """
    path = Path(path)
    size = path.stat().st_size
    if size == 0:
        raise PdfVerificationError("empty", "file is empty")

    with open(path, "rb") as f:
        head = f.read(2048)
        if size <= min_bytes:
            raise PdfVerificationError("too_small", f"file too small ({size} bytes)")
        if looks_like_html(head):
            raise PdfVerificationError(
                "html_response", "server returned an HTML/XML page, not a PDF"
            )
        if not head.startswith(b"%PDF-"):
            raise PdfVerificationError("not_pdf", "missing %PDF- header")
        if size <= _EOF_TAIL:
            f.seek(0)
            tail = f.read(size)
        else:
            f.seek(-_EOF_TAIL, os.SEEK_END)
            tail = f.read(_EOF_TAIL)

    if b"%%EOF" not in tail:
        raise PdfVerificationError("missing_eof", "missing %%EOF trailer marker")

    pages: int | None = None
    if full_check:
        try:
            import fitz
        except ImportError:
            try:
                import pymupdf as fitz
            except ImportError:
                fitz = None
        if fitz is not None:
            try:
                doc = fitz.open(str(path))
                pages = doc.page_count
                encrypted = doc.is_encrypted
                doc.close()
                if encrypted:
                    raise PdfVerificationError("unparseable", "PDF is encrypted/password-protected")
                if pages < 1:
                    raise PdfVerificationError("unparseable", "PDF has zero pages")
            except PdfVerificationError:
                raise
            except Exception as e:
                raise PdfVerificationError("unparseable", f"PDF could not be opened: {e}") from e

    return {"ok": True, "pages": pages, "size": size, "sha256": sha256_file(path)}


# ---------------------------------------------------------------------------
# Filename safety
# ---------------------------------------------------------------------------

# Characters not allowed in filenames on Windows (superset covers macOS/Linux).
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, max_len: int = 200) -> str:
    """Return a filename safe on Windows, macOS and Linux.

    Strips path separators, control chars and Windows-reserved characters,
    collapses whitespace, avoids reserved device names and trailing
    dots/spaces, and caps the length.
    """
    name = _BAD_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if not name or not re.search(r"[^\W_]", name):
        name = "untitled"
    stem = name.split(".", 1)[0]
    if stem.upper() in _RESERVED:
        name = f"_{name}"
    # keep extension within the length budget
    if len(name) > max_len:
        _base, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = name[: max_len - len(ext) - 1] + "." + ext
        else:
            name = name[:max_len]
        name = name.rstrip(" .")
    return name


def dedupe_path(dest: Path) -> Path:
    """Return *dest* or a non-colliding variant ``name__N.ext`` if it exists."""
    if not dest.exists():
        return dest
    for i in range(2, 1000):
        candidate = dest.with_name(f"{dest.stem}__{i}{dest.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not dedupe filename: {dest}")


def ensure_within(base_dir: Path, target: Path) -> Path:
    """Resolve *target* and ensure it stays inside *base_dir* (path-traversal guard)."""
    base = base_dir.resolve()
    resolved = target.resolve()
    try:
        resolved.relative_to(base)
    except ValueError as e:
        raise PdfVerificationError(
            "path_traversal", f"refusing path outside download dir: {resolved}"
        ) from e
    return resolved


# ---------------------------------------------------------------------------
# URL safety (SSRF protection)
# ---------------------------------------------------------------------------

_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain"}


def _ip_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable → block
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def is_safe_url(url: str, *, allow_private: bool = False) -> tuple[bool, str]:
    """Check that *url* is safe to fetch (scheme + public host).

    Args:
        url: Candidate URL.
        allow_private: Allow private/loopback addresses (e.g. a user-owned
            local repository). Defaults to False.

    Returns:
        ``(ok, reason)`` — reason is empty on success, else a short code:
        ``bad_scheme | bad_url | blocked_host | blocked_ip | dns_error``.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "bad_url"
    if parsed.scheme not in ("http", "https"):
        return False, "bad_scheme"
    host = parsed.hostname
    if not host:
        return False, "bad_url"
    if allow_private:
        return True, ""
    if host.lower().rstrip(".") in _BLOCKED_HOSTNAMES or host.lower().endswith(
        (".local", ".internal", ".lan")
    ):
        return False, "blocked_host"
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        return False, "dns_error"
    except Exception:
        return False, "dns_error"
    for _family, _type, _proto, _canon, sockaddr in infos:
        if _ip_blocked(sockaddr[0]):
            return False, "blocked_ip"
    return True, ""
