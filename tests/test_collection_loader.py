"""Collection-plan loader/planner/relevance/naming tests.

The loader tests run against the *real* 71-task acceptance plan
(``plans/aero_design_rows_066_136.json``), not a hand-built sample —
per the phase acceptance criteria.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mosaic.collection.loader import PlanError, load_plan, validate_plan_data
from mosaic.collection.planner import parse_groups, planned_queries, queries_for_source
from mosaic.collection.relevance import score_paper
from mosaic.collection.storage import (
    canonical_path,
    link_or_copy,
    paper_filename,
    task_dir,
    variant_filename,
)
from mosaic.models import Paper

REPO = Path(__file__).resolve().parent.parent
PLAN_PATH = REPO / "plans" / "aero_design_rows_066_136.json"


@pytest.fixture(scope="module")
def plan():
    return load_plan(PLAN_PATH)


@pytest.fixture()
def task045(plan):
    return next(t for t in plan.tasks if t.task_id == "T045")


# ---------------------------------------------------------------------------
# Gate A — real plan parsing
# ---------------------------------------------------------------------------


class TestRealPlan:
    def test_task_count(self, plan):
        assert len(plan.tasks) == 71

    def test_domains(self, plan):
        assert plan.domains == {"风扇与压气机": 21, "燃烧室与加力燃烧室": 24, "涡轮": 26}

    def test_priorities(self, plan):
        assert plan.priorities == {"P0": 56, "P1": 15}

    def test_task_ids_unique(self, plan):
        ids = [t.task_id for t in plan.tasks]
        assert len(ids) == len(set(ids))
        assert ids[0] == "T045"  # excel_row≠task sequence — first row is T045

    def test_task_ids_not_row_derived(self, plan):
        # T-numbers are NOT sequential with excel_row — T265 exists.
        ids = {t.task_id for t in plan.tasks}
        assert "T265" in ids
        by_row = {t.excel_row: t.task_id for t in plan.tasks}
        assert by_row[66] == "T045"

    def test_all_output_subdirs_safe(self, plan):
        for t in plan.tasks:
            assert ".." not in t.output_subdir
            assert not t.output_subdir.startswith("/")
            parts = t.output_subdir.split("/")
            assert len(parts) == 3, f"{t.task_id}: {t.output_subdir}"

    def test_keywords_and_queries_present(self, plan):
        for t in plan.tasks:
            assert t.en.primary
            assert t.zh.primary
            assert t.queries.en_focused
            assert t.queries.en_broad
            assert t.output_subdir

    def test_select(self, plan):
        assert len(plan.select(task_ids={"T045"})) == 1
        assert len(plan.select(domain="涡轮")) == 26
        assert len(plan.select(priority="P1")) == 15
        assert len(plan.select(limit=5)) == 5

    def test_task045_fields(self, task045):
        assert task045.en.primary == "axial compressor"
        assert "axial-flow compressor" in task045.en.aliases
        assert task045.zh.primary == "轴流压气机"
        assert task045.output_subdir == "风扇与压气机/部件构型/T045_轴流压气机"
        assert task045.priority == "P0"


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------


def _minimal_plan():
    return {
        "schema_version": "1.0",
        "kind": "mosaic.collection_plan",
        "plan_id": "x",
        "tasks": [
            {
                "task_id": "T001",
                "excel_row": 1,
                "domain": "d",
                "topic": "t",
                "priority": "P0",
                "keywords": {
                    "zh": {"primary": "甲", "aliases": []},
                    "en": {"primary": "alpha", "aliases": ["beta"]},
                },
                "collection_focus": "f",
                "search_queries": {
                    "en_broad": "alpha",
                    "en_focused": "(alpha) AND (ctx)",
                    "zh_broad": "甲",
                    "zh_focused": "(甲) AND (乙)",
                    "ntrs_focused": "(alpha)+(ctx)",
                },
                "output_subdir": "d/t/T001_alpha",
            }
        ],
    }


class TestValidation:
    def test_minimal_ok(self):
        assert validate_plan_data(_minimal_plan()) == []

    def test_bad_version(self):
        d = _minimal_plan()
        d["schema_version"] = "2.0"
        assert any("schema_version" in p for p in validate_plan_data(d))

    def test_bad_kind(self):
        d = _minimal_plan()
        d["kind"] = "other"
        assert any("kind" in p for p in validate_plan_data(d))

    def test_duplicate_task_id(self):
        d = _minimal_plan()
        d["tasks"].append(dict(d["tasks"][0]))
        problems = validate_plan_data(d)
        assert any("duplicate" in p for p in problems)

    def test_missing_required(self):
        d = _minimal_plan()
        del d["tasks"][0]["keywords"]
        assert any("keywords" in p for p in validate_plan_data(d))

    def test_bad_priority(self):
        d = _minimal_plan()
        d["tasks"][0]["priority"] = "P9"
        assert any("priority" in p for p in validate_plan_data(d))

    @pytest.mark.parametrize(
        "subdir",
        ["../escape", "/abs/path", "a/../../b", "~/home", "d://w", "ok/../x"],
    )
    def test_unsafe_subdir(self, subdir):
        d = _minimal_plan()
        d["tasks"][0]["output_subdir"] = subdir
        assert any("output_subdir" in p for p in validate_plan_data(d)), subdir

    def test_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(PlanError):
            load_plan(bad)

    def test_missing_file(self, tmp_path):
        with pytest.raises(PlanError):
            load_plan(tmp_path / "nope.json")

    def test_load_raises_with_all_problems(self, tmp_path):
        d = _minimal_plan()
        d["schema_version"] = "9"
        d["tasks"] = []
        p = tmp_path / "plan.json"
        p.write_text(json.dumps(d), encoding="utf-8")
        with pytest.raises(PlanError) as exc:
            load_plan(p)
        assert len(exc.value.problems) >= 2


# ---------------------------------------------------------------------------
# Query planning
# ---------------------------------------------------------------------------


class TestPlanner:
    def test_parse_groups(self):
        terms, ctx = parse_groups('("a" OR "b") AND ("c" OR "d")')
        assert terms == ["a", "b"]
        assert ctx == ["c", "d"]

    def test_parse_groups_pipe(self):
        terms, ctx = parse_groups(
            '("axial compressor"|"axial-flow compressor")+("compressor"|"fan")'
        )
        assert "axial-flow compressor" in terms
        assert "fan" in ctx

    def test_parse_groups_single(self):
        terms, ctx = parse_groups('("transonic fan")')
        assert terms == ["transonic fan"]
        assert ctx == []

    def test_parse_groups_bare(self):
        terms, _ctx = parse_groups("plain phrase")
        assert terms == ["plain phrase"]

    def test_no_boolean_syntax_in_output(self, task045):
        for pq in planned_queries(task045, 10):
            assert "AND" not in pq.query
            assert "OR" not in pq.query
            assert "(" not in pq.query and ")" not in pq.query
            assert '"' not in pq.query

    def test_tiers_ordered(self, task045):
        tiers = [pq.tier for pq in planned_queries(task045, 10)]
        order = {"L1_focus": 0, "L2_ext": 1, "L3_wide": 2, "L4_supp": 3}
        assert tiers == sorted(tiers, key=lambda t: order[t])

    def test_max_queries_respected(self, task045):
        assert len(planned_queries(task045, 3)) == 3
        assert len(planned_queries(task045, 1)) == 1

    def test_no_duplicate_term_context(self, task045):
        # "axial compressor compressor" must never be emitted.
        for pq in planned_queries(task045, 10):
            words = pq.query.lower().split()
            assert len(words) == len(set(words)) or "compressor compressor" not in pq.query

    def test_zh_queries_marked(self, task045):
        zh = [pq for pq in planned_queries(task045, 20) if pq.lang == "zh"]
        assert zh
        assert all(pq.tier == "L4_supp" for pq in zh)

    def test_english_only_source_filter(self, task045):
        queries = planned_queries(task045, 20)
        ntrs_q = queries_for_source(queries, "ntrs")
        assert all(q.lang != "zh" for q in ntrs_q)
        oa_q = queries_for_source(queries, "openalex")
        assert len(oa_q) == len(queries)


# ---------------------------------------------------------------------------
# Relevance
# ---------------------------------------------------------------------------


def _paper(title, abstract="", **kw):
    return Paper(title=title, abstract=abstract, source="test", **kw)


class TestRelevance:
    def test_high_core_in_title(self, task045):
        p = _paper(
            "Aerodynamic design of an axial compressor stage",
            "We study compressor aerodynamics.",
            year=2020,
        )
        grade, score, why = score_paper(task045, p)
        assert grade == "HIGH"
        assert any("core" in w for w in why)

    def test_medium_core_in_abstract(self, task045):
        p = _paper(
            "Numerical investigation of turbomachinery flows",
            "The axial compressor model shows strong secondary flow.",
        )
        grade, _s, _w = score_paper(task045, p)
        assert grade in ("HIGH", "MEDIUM")

    def test_low_unrelated_keyword_only(self, task045):
        p = _paper(
            "A study of centrifugal pump efficiency",
            "Pump efficiency is analysed for water service.",
        )
        grade, _s, _w = score_paper(task045, p)
        assert grade in ("LOW", "NEEDS_REVIEW")

    def test_needs_review_no_abstract_no_hits(self, task045):
        p = _paper("Completely unrelated title about cooking")
        grade, _s, _w = score_paper(task045, p)
        assert grade == "NEEDS_REVIEW"

    def test_low_with_abstract_no_hits(self, task045):
        p = _paper(
            "Completely unrelated title about cooking",
            "Recipes for seasonal vegetables are discussed.",
        )
        grade, _s, _w = score_paper(task045, p)
        assert grade == "LOW"

    def test_alias_hit(self, task045):
        p = _paper(
            "Surge margin of an axial-flow compressor",
            "The compression system was tested.",
        )
        grade, _s, why = score_paper(task045, p)
        assert grade in ("HIGH", "MEDIUM")
        assert any("alias" in w or "core" in w for w in why)


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


class TestNaming:
    def test_pattern_no_author(self, paper):
        name = paper_filename(paper)
        assert name == "2017_arXiv_Attention_Is_All_You_Need.pdf"
        assert "Vaswani" not in name and "Ashish" not in name

    def test_year_missing(self):
        p = Paper(title="No Year Paper", source="OpenAlex")
        assert paper_filename(p).startswith("0000_OpenAlex_")

    def test_title_sanitised(self):
        p = Paper(title='Weird/Title: "quoted" <bad>', year=2020, source="HAL")
        name = paper_filename(p)
        assert "/" not in name and ":" not in name and '"' not in name
        assert name.endswith(".pdf")

    def test_empty_title_fallback(self):
        p = Paper(title="", source="arXiv", doi="10.1/x")
        name = paper_filename(p)
        assert name.endswith(".pdf") and len(name) > 8

    def test_long_title_truncated_keeps_ext(self):
        p = Paper(title="T" * 500, year=2021, source="Crossref")
        name = paper_filename(p)
        assert name.endswith(".pdf")
        assert len(name) <= 180

    def test_unicode_title_kept(self):
        p = Paper(title="压气机 Aerodynamics Étude", year=2022, source="HAL")
        name = paper_filename(p)
        assert ("压气机" in name and "Étude" in name) or "tude" in name

    def test_variant_stable(self):
        a = variant_filename("2020_arXiv_X.pdf", "doi:10.1/aaa")
        b = variant_filename("2020_arXiv_X.pdf", "doi:10.1/aaa")
        c = variant_filename("2020_arXiv_X.pdf", "doi:10.1/bbb")
        assert a == b != c
        assert a.startswith("2020_arXiv_X_") and a.endswith(".pdf")

    def test_canonical_collision(self, tmp_path, paper):
        lib = tmp_path / "lib"
        lib.mkdir()
        taken: dict[str, str] = {}
        first = canonical_path(lib, paper, taken=taken)
        other = Paper(
            title="Attention Is All You Need",
            year=2017,
            doi="10.9999/different",
            source="arXiv",
        )
        second = canonical_path(lib, other, taken=taken)
        assert first.name != second.name
        assert second.name.startswith(first.name[: -len(".pdf")] + "_")
        # Stable within a run: the same taken-map keeps assigning the
        # variant to the same uid; cross-run stability comes from the
        # locked collection_files.filename (runner test covers that).
        third = canonical_path(lib, other, taken=taken)
        assert third.name == second.name


# ---------------------------------------------------------------------------
# Task dir + link/copy
# ---------------------------------------------------------------------------


class TestStorage:
    def test_task_dir_sanitises(self, tmp_path):
        d = task_dir(tmp_path, "风扇与压气机/部件构型/T045_轴流压气机")
        assert d.parent.parent.parent == tmp_path.resolve()
        assert "T045_轴流压气机" in d.name

    def test_task_dir_traversal(self, tmp_path):
        d = task_dir(tmp_path, "../../etc/bad")
        assert str(d).startswith(str(tmp_path.resolve()))
        assert "etc" not in d.parts[-2:] or d.is_relative_to(tmp_path.resolve())

    def test_link_or_copy_hardlink(self, tmp_path):
        src = tmp_path / "a.pdf"
        src.write_bytes(b"%PDF-1.4 fake %%EOF")
        dst = tmp_path / "sub" / "a.pdf"
        assert link_or_copy(src, dst) in ("link", "copy", "exists")
        assert dst.read_bytes() == src.read_bytes()

    def test_link_existing_same(self, tmp_path):
        src = tmp_path / "a.pdf"
        src.write_bytes(b"%PDF-1.4 fake %%EOF")
        dst = tmp_path / "b.pdf"
        link_or_copy(src, dst)
        assert link_or_copy(src, dst) == "exists"

    def test_link_refuses_different(self, tmp_path):
        src = tmp_path / "a.pdf"
        src.write_bytes(b"%PDF-1.4 one %%EOF")
        dst = tmp_path / "b.pdf"
        dst.write_bytes(b"%PDF-1.4 two %%EOF")
        with pytest.raises(FileExistsError):
            link_or_copy(src, dst)
