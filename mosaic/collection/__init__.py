"""JSON collection plans — structured multi-task paper harvesting.

A *collection plan* is a JSON document describing a set of related
acquisition tasks (keywords in one or more languages, pre-computed search
queries, a target output sub-directory, per-task priorities). The modules
here only handle plan parsing, query planning, relevance grading,
scheduling, on-disk archiving and reporting — the actual search and
download work is delegated to the existing MOSAIC engines
(:mod:`mosaic.search`, :mod:`mosaic.dlqueue`, :mod:`mosaic.downloader`).

Nothing in this package is specific to any scientific domain: all
vocabulary (core terms, aliases, abbreviations, context words) comes from
the plan document itself.
"""

from mosaic.collection.loader import (
    CollectionPlan,
    CollectionTask,
    PlanError,
    WordGroup,
    load_plan,
    validate_plan_data,
)
from mosaic.collection.runner import CollectionRunner, RunOptions

__all__ = [
    "CollectionPlan",
    "CollectionRunner",
    "CollectionTask",
    "PlanError",
    "RunOptions",
    "WordGroup",
    "load_plan",
    "validate_plan_data",
]
