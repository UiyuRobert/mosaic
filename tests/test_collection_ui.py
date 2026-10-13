"""Web-UI routes for collection plans (Flask test client, no network)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from mosaic.ui import create_app

REPO = Path(__file__).resolve().parent.parent
PLAN_PATH = REPO / "plans" / "aero_design_rows_066_136.json"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """App with an isolated DB — never touches the real cache."""
    import mosaic.config as cfg_mod

    cfg = cfg_mod.load()
    cfg["db_path"] = str(tmp_path / "ui.db")
    monkeypatch.setattr(cfg_mod, "load", lambda: dict(cfg))
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_collection_page_loads(client):
    r = client.get("/collection")
    assert r.status_code == 200
    assert b"Collection" in r.data


def test_load_plan_by_path(client):
    r = client.post("/collection/load", data={"plan_path": str(PLAN_PATH)})
    assert r.status_code == 200
    assert b"T045" in r.data
    assert b"aero_design_excel_rows_066_136" in r.data


def test_load_plan_by_upload(client, tmp_path):
    data = PLAN_PATH.read_bytes()
    r = client.post(
        "/collection/load",
        data={"plan_file": (io.BytesIO(data), "plan.json")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    assert b"T045" in r.data


def test_load_invalid_plan(client, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"kind": "nope"}), encoding="utf-8")
    r = client.post("/collection/load", data={"plan_path": str(bad)})
    assert r.status_code == 200
    assert b"Invalid collection plan" in r.data


def test_load_missing_file(client):
    r = client.post("/collection/load", data={"plan_path": "/no/such/plan.json"})
    assert b"Invalid collection plan" in r.data


def test_table_and_task_detail(client):
    client.post("/collection/load", data={"plan_path": str(PLAN_PATH)})
    r = client.get("/collection/aero_design_excel_rows_066_136/table")
    assert r.status_code == 200
    assert b"T045" in r.data
    r = client.get("/collection/aero_design_excel_rows_066_136/task/T045")
    assert r.status_code == 200


def test_run_unregistered_plan_rejected(client):
    r = client.post("/collection/never_seen/run", data={})
    assert b"not registered" in r.data


def test_nav_link(client):
    assert b"/collection" in client.get("/").data
