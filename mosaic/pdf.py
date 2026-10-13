"""PDF text extraction via pymupdf (optional dependency)."""

from __future__ import annotations

from pathlib import Path


def _fitz():
    """Import pymupdf; prefers the modern ``pymupdf`` module name.

    ``import fitz`` works too but prints a deprecation warning to stdout —
    which would corrupt ``--json`` CLI output.
    """
    try:
        import pymupdf

        return pymupdf
    except ImportError:
        import fitz

        return fitz


def is_available() -> bool:
    """Return True if pymupdf (fitz) is importable."""
    try:
        _fitz()
        return True
    except ImportError:
        return False


def extract_text(path: str | Path) -> str:
    """Extract plain text from a PDF file using pymupdf.

    Returns an empty string on any failure (encrypted, corrupted, image-only).
    Never raises -- extraction failures must not block indexing.
    """
    try:
        fitz = _fitz()
    except ImportError as e:
        raise ImportError(
            "pymupdf is required for full-text PDF indexing. Run: pipx inject mosaic-search pymupdf"
        ) from e

    try:
        doc = fitz.open(str(path))
        if doc.is_encrypted:
            return ""
        parts = []
        for page in doc:
            parts.append(page.get_text())
        doc.close()
        text = "\n".join(parts)
        # Collapse excessive blank lines
        import re

        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    except Exception:
        return ""
