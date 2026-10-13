"""On-disk layout for collection plans — canonical library + task links.

Layout produced under the plan's output root::

    <root>/
      _library/                       # canonical files — one real download per paper
        2021_arXiv_Axial_Compressor_Study.pdf
      <domain>/<topic>/<Txxx_label>/  # per-task dirs (from task.output_subdir)
        2021_arXiv_Axial_Compressor_Study.pdf   # hardlink (or copy) of the canonical file
      _manifest/
        papers.csv, task_status.csv, task_paper_links.csv,
        failures.csv, collection_report.json

Rules:

- Filenames follow ``{year}_{source}_{title}.pdf`` (collection default;
  pattern-configurable). No author field.
- A colliding filename owned by a *different* paper gets a stable
  ``_<uidhash8>`` suffix — never an overwrite, never a re-numbered name
  that changes between runs.
- Task-dir copies are hardlinks when the filesystem allows, verified
  copies otherwise. Deleting a task dir never removes the canonical file.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path

from mosaic.models import Paper
from mosaic.pdfcheck import sanitize_filename, sha256_file

log = logging.getLogger(__name__)

COLLECTION_FILENAME_PATTERN = "{year}_{source}_{title}"
LIBRARY_DIRNAME = "_library"
MANIFEST_DIRNAME = "_manifest"


def library_dir(root: Path) -> Path:
    return Path(root) / LIBRARY_DIRNAME


def manifest_dir(root: Path) -> Path:
    return Path(root) / MANIFEST_DIRNAME


def _safe_segment(seg: str) -> str:
    """Make one directory segment filesystem-safe (no separators/traversal)."""
    seg = sanitize_filename(seg.replace("\\", "/").replace("/", "_"), max_len=80)
    seg = seg.strip().strip(".")
    return seg or "misc"


def task_dir(root: Path, output_subdir: str) -> Path:
    """Resolve *output_subdir* inside *root*, sanitising each segment.

    ``..`` / absolute components are defused per-segment, so the result
    can never escape *root*.
    """
    parts = [p for p in output_subdir.replace("\\", "/").split("/") if p and p != "."]
    safe = [_safe_segment(p) for p in parts]
    dest = Path(root).joinpath(*safe).resolve()
    base = Path(root).resolve()
    if dest != base and base not in dest.parents:
        # Should be unreachable given per-segment sanitisation — hard guard.
        dest = base / "_unsafe" / _safe_segment(output_subdir)
    return dest


def _uid_suffix(uid: str) -> str:
    """Stable short suffix derived from the paper uid (deterministic)."""
    return hashlib.sha1(uid.encode("utf-8")).hexdigest()[:8]  # noqa: S324 — non-crypto use


def paper_filename(paper: Paper, pattern: str = COLLECTION_FILENAME_PATTERN) -> str:
    """Filename for *paper* under the collection naming rule (no dir part)."""
    name = paper.safe_filename(pattern)
    if not name or name == ".pdf" or name.startswith(".pdf"):
        name = f"{paper.year or '0000'}_{_uid_suffix(paper.uid)}.pdf"
    name = sanitize_filename(name, max_len=180)
    if name in (".pdf", "untitled", "") or not name.endswith(".pdf"):
        stem = (name.rsplit(".pdf", 1)[0] or "untitled").strip("._ ") or "untitled"
        name = f"{stem}.pdf"
    return name


def variant_filename(filename: str, uid: str) -> str:
    """Collision variant: ``stem_<uidhash8>.pdf`` — stable for a given uid."""
    stem = filename[: -len(".pdf")] if filename.endswith(".pdf") else filename
    return f"{stem}_{_uid_suffix(uid)}.pdf"


def canonical_path(
    libdir: Path,
    paper: Paper,
    *,
    pattern: str = COLLECTION_FILENAME_PATTERN,
    taken: dict[str, str] | None = None,
) -> Path:
    """Compute the canonical library path for *paper*.

    *taken* maps ``filename → paper_uid`` for names already assigned this
    run (in-memory reservation; the caller also consults its DB). A name
    already claimed by a different uid — or occupied on disk by an
    unrelated file — yields the stable ``_<uidhash8>`` variant.
    """
    libdir = Path(libdir)
    name = paper_filename(paper, pattern)
    taken = taken if taken is not None else {}
    owner = taken.get(name)
    candidate = libdir / name
    if (owner is not None and owner != paper.uid) or (candidate.exists() and owner != paper.uid):
        name = variant_filename(name, paper.uid)
        candidate = libdir / name
    taken[name] = paper.uid
    return candidate


def link_or_copy(src: Path, dst: Path) -> str:
    """Present *src* at *dst* — hardlink preferred, verified copy fallback.

    Returns ``"exists"`` when *dst* already has identical content,
    ``"link"`` or ``"copy"`` for the action taken.
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        try:
            if dst.samefile(src):
                return "exists"
        except OSError:
            pass
        if sha256_file(dst) == sha256_file(src):
            return "exists"
        raise FileExistsError(f"refusing to overwrite different file: {dst}")
    try:
        os.link(src, dst)
        return "link"
    except OSError as e:
        # Cross-device or no-hardlink filesystem — verified copy.
        shutil.copy2(src, dst)
        if sha256_file(dst) != sha256_file(src):
            dst.unlink(missing_ok=True)
            raise OSError(f"copy verification failed for {dst}") from e
        return "copy"
