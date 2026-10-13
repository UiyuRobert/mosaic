"""Collection-plan loading and validation.

Implements the ``mosaic.collection_plan`` document format (schema v1 —
see ``docs/schemas/collection-plan-v1.schema.json``). Validation is
hand-rolled rather than delegated to a JSON-Schema dependency so MOSAIC
keeps its slim dependency footprint; every check produces a clear,
actionable message and *all* problems are reported at once.

Schema v1 quick reference::

    {
      "schema_version": "1.0",
      "kind": "mosaic.collection_plan",
      "plan_id": "…",
      "defaults": {"search": {…}, "download": {…}, "storage": {…}},
      "tasks": [
        {
          "task_id": "T045", "excel_row": 66,
          "domain": "…", "topic": "…", "priority": "P0",
          "keywords": {"zh": {"primary": "…", "aliases": […]},
                       "en": {"primary": "…", "aliases": […], "abbreviation": "…"}},
          "related_terms": […],
          "collection_focus": "…", "retrieval_boundary": "…",
          "search_queries": {"en_broad": "…", "en_focused": "…",
                             "zh_broad": "…", "zh_focused": "…",
                             "ntrs_focused": "…"},
          "output_subdir": "domain/topic/T045_label"
        }
      ]
    }
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from mosaic.errors import MosaicError

SCHEMA_VERSION = "1.0"
PLAN_KIND = "mosaic.collection_plan"

PRIORITIES = {"P0", "P1", "P2"}
_QUERY_KEYS = ("en_broad", "en_focused", "zh_broad", "zh_focused", "ntrs_focused")
# Loose task-id shape — strict schemas may pin ``^T\d{3}$``; the loader
# only requires something filename- and identifier-friendly.
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,63}$")
# Path segments that would escape the output root.
_UNSAFE_SEGMENT = re.compile(r"^(\.\.?|~)$|[<>:\"|?*\x00-\x1f]")


class PlanError(MosaicError):
    """Raised when a collection plan cannot be loaded or is invalid."""

    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


@dataclass
class WordGroup:
    """A primary term plus its aliases (and optional abbreviation)."""

    primary: str
    aliases: list[str] = field(default_factory=list)
    abbreviation: str = ""

    @property
    def all_terms(self) -> list[str]:
        out = [self.primary]
        for a in self.aliases:
            if a and a.lower() not in {t.lower() for t in out}:
                out.append(a)
        return out


@dataclass
class SearchQueries:
    """The five pre-computed query strings carried by every v1 task."""

    en_broad: str = ""
    en_focused: str = ""
    zh_broad: str = ""
    zh_focused: str = ""
    ntrs_focused: str = ""


@dataclass
class CollectionTask:
    """One acquisition task inside a :class:`CollectionPlan`."""

    task_id: str
    domain: str
    output_subdir: str
    zh: WordGroup
    en: WordGroup
    queries: SearchQueries
    excel_row: int = 0
    topic: str = ""
    priority: str = "P2"
    related_terms: list[str] = field(default_factory=list)
    collection_focus: str = ""
    retrieval_boundary: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    def to_json(self) -> str:
        """Serialise back to the original JSON blob (for state storage)."""
        return json.dumps(self.raw, ensure_ascii=False)


@dataclass
class CollectionPlan:
    """A parsed, validated collection plan."""

    plan_id: str
    tasks: list[CollectionTask]
    title: str = ""
    schema_version: str = SCHEMA_VERSION
    source: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    defaults: dict = field(default_factory=dict)
    path: str = ""
    sha256: str = ""

    # ── convenience selectors ────────────────────────────────────────────

    def select(
        self,
        *,
        task_ids: set[str] | None = None,
        domain: str = "",
        priority: str = "",
        limit: int = 0,
    ) -> list[CollectionTask]:
        """Filter tasks by id/domain/priority; *limit* caps the result."""
        out = self.tasks
        if task_ids:
            out = [t for t in out if t.task_id in task_ids]
        if domain:
            out = [t for t in out if t.domain == domain]
        if priority:
            out = [t for t in out if t.priority == priority]
        if limit:
            out = out[:limit]
        return list(out)

    @property
    def domains(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in self.tasks:
            counts[t.domain] = counts.get(t.domain, 0) + 1
        return counts

    @property
    def priorities(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in self.tasks:
            counts[t.priority] = counts.get(t.priority, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _is_unsafe_subdir(subdir: str) -> str | None:
    """Return a reason string when *subdir* is unsafe, else ``None``."""
    if not subdir or not subdir.strip():
        return "empty"
    norm = subdir.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
        return "absolute paths are not allowed"
    for seg in norm.split("/"):
        if _UNSAFE_SEGMENT.search(seg) or seg in ("", ".", ".."):
            return f"unsafe path segment {seg!r}"
    return None


def _check_wordgroup(data, where: str, problems: list[str], *, need_abbr: bool = False) -> None:
    if not isinstance(data, dict):
        problems.append(f"{where}: must be an object")
        return
    primary = data.get("primary")
    if not isinstance(primary, str) or not primary.strip():
        problems.append(f"{where}.primary: non-empty string required")
    aliases = data.get("aliases")
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        problems.append(f"{where}.aliases: array of strings required")


def validate_plan_data(data) -> list[str]:
    """Validate a decoded plan document; returns a list of problems (empty = ok)."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["plan root must be a JSON object"]

    if data.get("schema_version") != SCHEMA_VERSION:
        problems.append(
            f"schema_version: expected {SCHEMA_VERSION!r}, got {data.get('schema_version')!r}"
        )
    if data.get("kind") != PLAN_KIND:
        problems.append(f"kind: expected {PLAN_KIND!r}, got {data.get('kind')!r}")
    if not isinstance(data.get("plan_id"), str) or not data.get("plan_id", "").strip():
        problems.append("plan_id: non-empty string required")

    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        problems.append("defaults: must be an object")

    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        problems.append("tasks: non-empty array required")
        return problems

    seen_ids: dict[str, int] = {}
    required = (
        "task_id",
        "domain",
        "priority",
        "keywords",
        "collection_focus",
        "search_queries",
        "output_subdir",
    )
    for i, t in enumerate(tasks):
        where = f"tasks[{i}]"
        if not isinstance(t, dict):
            problems.append(f"{where}: must be an object")
            continue
        tid = t.get("task_id") or f"#{i}"
        where = f"task {tid}"
        for key in required:
            if key not in t:
                problems.append(f"{where}: missing required field {key!r}")

        task_id = t.get("task_id")
        if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id or ""):
            problems.append(
                f"{where}.task_id: must match {_TASK_ID_RE.pattern!r} (got {task_id!r})"
            )
        elif task_id in seen_ids:
            problems.append(
                f"{where}.task_id: duplicate id {task_id!r} (also at index {seen_ids[task_id]})"
            )
        else:
            seen_ids[task_id] = i

        if "excel_row" in t and (not isinstance(t["excel_row"], int) or t["excel_row"] < 1):
            problems.append(f"{where}.excel_row: integer >= 1 required")
        if not isinstance(t.get("domain"), str) or not str(t.get("domain", "")).strip():
            problems.append(f"{where}.domain: non-empty string required")
        if t.get("priority") not in PRIORITIES:
            problems.append(
                f"{where}.priority: expected one of {sorted(PRIORITIES)}, got {t.get('priority')!r}"
            )

        kw = t.get("keywords")
        if not isinstance(kw, dict):
            problems.append(f"{where}.keywords: object with 'zh' and 'en' required")
        else:
            _check_wordgroup(kw.get("zh"), f"{where}.keywords.zh", problems)
            _check_wordgroup(kw.get("en"), f"{where}.keywords.en", problems)

        sq = t.get("search_queries")
        if not isinstance(sq, dict):
            problems.append(f"{where}.search_queries: object required")
        else:
            for k in _QUERY_KEYS:
                if k not in sq:
                    problems.append(f"{where}.search_queries: missing key {k!r}")
                elif not isinstance(sq[k], str):
                    problems.append(f"{where}.search_queries.{k}: string required")

        subdir = t.get("output_subdir")
        if not isinstance(subdir, str):
            problems.append(f"{where}.output_subdir: string required")
        else:
            bad = _is_unsafe_subdir(subdir)
            if bad:
                problems.append(f"{where}.output_subdir: {bad} ({subdir!r})")

        for key in ("related_terms",):
            if key in t and (
                not isinstance(t[key], list) or not all(isinstance(x, str) for x in t[key])
            ):
                problems.append(f"{where}.{key}: array of strings required")

    return problems


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _wordgroup(data: dict | None) -> WordGroup:
    data = data or {}
    return WordGroup(
        primary=str(data.get("primary", "")).strip(),
        aliases=[a.strip() for a in data.get("aliases", []) if a and a.strip()],
        abbreviation=str(data.get("abbreviation", "") or "").strip(),
    )


def _task_from(data: dict) -> CollectionTask:
    kw = data.get("keywords", {})
    sq = data.get("search_queries", {})
    return CollectionTask(
        task_id=data["task_id"],
        excel_row=int(data.get("excel_row", 0)),
        domain=str(data.get("domain", "")).strip(),
        topic=str(data.get("topic", "") or "").strip(),
        priority=data.get("priority", "P2"),
        zh=_wordgroup(kw.get("zh")),
        en=_wordgroup(kw.get("en")),
        related_terms=[x.strip() for x in data.get("related_terms", []) if x and x.strip()],
        collection_focus=str(data.get("collection_focus", "") or ""),
        retrieval_boundary=str(data.get("retrieval_boundary", "") or ""),
        queries=SearchQueries(**{k: str(sq.get(k, "") or "") for k in _QUERY_KEYS}),
        output_subdir=str(data.get("output_subdir", "")).strip(),
        raw=data,
    )


def load_plan(path: str | Path, *, validate: bool = True) -> CollectionPlan:
    """Load and validate a collection plan JSON file.

    Raises:
        PlanError: file unreadable, invalid JSON, or failed validation —
            the exception carries the full problem list.
    """
    p = Path(path).expanduser()
    try:
        raw_bytes = p.read_bytes()
    except OSError as e:
        raise PlanError(f"cannot read plan file {p}: {e}") from e
    try:
        data = json.loads(raw_bytes)
    except json.JSONDecodeError as e:
        raise PlanError(f"{p}: invalid JSON — {e}") from e

    if validate:
        problems = validate_plan_data(data)
        if problems:
            raise PlanError(problems)

    tasks = [_task_from(t) for t in data["tasks"]]
    return CollectionPlan(
        plan_id=data["plan_id"].strip(),
        title=str(data.get("title", "") or ""),
        schema_version=str(data.get("schema_version", SCHEMA_VERSION)),
        source=data.get("source", {}) or {},
        summary=data.get("summary", {}) or {},
        defaults=data.get("defaults", {}) or {},
        tasks=tasks,
        path=str(p),
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )
