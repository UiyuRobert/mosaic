#!/usr/bin/env python3
"""LIVE arXiv download smoke test — real network, real PDFs, real CLI.

Drives the installed ``mosaic`` CLI inside an isolated HOME: the config,
SQLite cache and download directories all live under ``--workdir`` so the
user's real library is never touched. Every check performs real HTTP(S)
requests against export.arxiv.org / arxiv.org — nothing is mocked or
pre-seeded. Success requires:

* ``mosaic search "transformer" --source arxiv --download`` → real PDFs
* ``mosaic get arxiv:1706.03762`` → real PDF (metadata via arXiv id_list)
* ``mosaic get 10.48550/arXiv.2106.09685`` → arXiv DOI form works
* repeat ``get`` → no duplicate file
* invalid identifier → non-zero exit
* ``--json --download`` → JSON carries the download map
* (optional) ``--backend aria2`` → daemon-driven download

Usage:
    .venv/bin/python tools/smoke_arxiv.py [--workdir ./local-smoke]
                                          [--backend native|aria2|all]
                                          [--aria2-absent]

``--aria2-absent`` additionally re-runs the get path with ``aria2c``
removed from PATH to prove the native path is independent of aria2.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOSAIC = REPO / ".venv" / "bin" / "mosaic"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Evidence:
    env: dict = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    papers: list[dict] = field(default_factory=list)


def run(cmd: list[str], env: dict, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=env
    )


def mosaic(args: list[str], env: dict, timeout: int = 300) -> subprocess.CompletedProcess:
    return run([str(MOSAIC), *args], env, timeout)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pdf_pages(path: Path) -> int:
    try:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf

        doc = pymupdf.open(str(path))
        n = doc.page_count
        doc.close()
        return n
    except Exception:
        # structural fallback — header + trailer is weaker but non-zero evidence
        data = path.read_bytes()
        return -1 if (data.startswith(b"%PDF-") and b"%%EOF" in data[-1024:]) else -2


def snapshot_pdfs(d: Path) -> dict[str, dict]:
    out = {}
    for p in sorted(d.glob("*.pdf")):
        data = p.read_bytes()
        out[p.name] = {
            "bytes": p.stat().st_size,
            "sha256": sha256(p),
            "pages": pdf_pages(p),
            "header": data[:5].decode("latin1"),
        }
    return out


def db_rows(db: Path, table: str) -> list[dict]:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(f"SELECT * FROM {table}")]  # noqa: S608
    finally:
        con.close()


def report(ev: Evidence) -> int:
    print("\n" + "=" * 72)
    print("SMOKE ARXIV — LIVE ACCEPTANCE")
    print("=" * 72)
    for k, v in ev.env.items():
        print(f"  {k:<14} {v}")
    print("-" * 72)
    ok_all = True
    for c in ev.checks:
        mark = "PASS" if c.ok else "FAIL"
        ok_all &= c.ok
        print(f"  [{mark}] {c.name:<56} {c.detail}")
    print("-" * 72)
    for p in ev.papers:
        print(
            f"  {p['arxiv_id']:<14} {p['title'][:52]:<52} {p['bytes']:>9,}B "
            f"{p.get('pages', '?'):>3}p  sha256:{p['sha256'][:12]}"
        )
        print(f"      -> {p['path']}")
    print("=" * 72)
    print("RESULT:", "ALL CHECKS PASSED" if ok_all else "FAILURES PRESENT")
    return 0 if ok_all else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=str(REPO / "local-smoke"))
    ap.add_argument("--backend", default="all", choices=["native", "aria2", "all"])
    ap.add_argument("--aria2-absent", action="store_true")
    args = ap.parse_args()

    workdir = Path(args.workdir).resolve()
    home = workdir / "home"
    pdfs = workdir / "pdfs"
    aria2_pdfs = workdir / "aria2-pdfs"
    home.mkdir(parents=True, exist_ok=True)
    pdfs.mkdir(parents=True, exist_ok=True)
    aria2_pdfs.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ, HOME=str(home))
    ev = Evidence()
    ev.env["mosaic_bin"] = str(MOSAIC)
    ev.env["workdir"] = str(workdir)
    ev.env["python"] = sys.version.split()[0]
    ev.env["aria2c"] = shutil.which("aria2c") or "(not installed)"

    def check(name: str, ok: bool, detail: str = "") -> bool:
        ev.checks.append(Check(name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} — {detail}")
        return ok

    t0 = time.time()

    # ── native backend ─────────────────────────────────────────────────────
    r = mosaic(["config", "--download-backend", "native"], env)
    cfg_toml = home / ".config" / "mosaic" / "config.toml"
    check(
        "config --download-backend native persists",
        cfg_toml.exists() and 'backend = "native"' in cfg_toml.read_text(),
        cfg_toml.read_text().splitlines()[-1][:60] if cfg_toml.exists() else "no toml",
    )

    before = snapshot_pdfs(pdfs)
    t = time.time()
    r = mosaic(
        [
            "search", "transformer", "--source", "arxiv", "--max", "3",
            "--download", "--download-dir", str(pdfs),
        ],
        env,
    )
    dur = time.time() - t
    new = {k: v for k, v in snapshot_pdfs(pdfs).items() if k not in before}
    check(
        'search "transformer" --source arxiv --download',
        r.returncode == 0 and len(new) >= 1,
        f"exit={r.returncode} new_pdfs={len(new)} in {dur:.0f}s",
    )
    for name, info in list(new.items())[:3]:
        ev.papers.append(
            {
                "arxiv_id": "?",
                "title": name,
                "path": str(pdfs / name),
                **info,
            }
        )

    db = home / ".local" / "share" / "mosaic" / "cache.db"
    jobs = db_rows(db, "download_jobs") if db.exists() else []
    completed = [j for j in jobs if j["status"] == "COMPLETED"]
    check(
        "SQLite: COMPLETED jobs have real URLs + files",
        bool(completed) and all(
            j["url"].startswith("http") and Path(j["destination"]).exists()
            for j in completed
        ),
        f"completed={len(completed)}",
    )

    # ── get by arXiv id ────────────────────────────────────────────────────
    t = time.time()
    r = mosaic(["get", "arxiv:1706.03762", "--download-dir", str(pdfs)], env)
    dur = time.time() - t
    target = next((p for p in pdfs.glob("*Attention_Is_All_You_Need*.pdf")), None)
    check(
        "get arxiv:1706.03762",
        r.returncode == 0 and target is not None,
        f"exit={r.returncode} {dur:.0f}s",
    )
    if target:
        info = snapshot_pdfs(pdfs)[target.name]
        check("Attention PDF parses (pages>=1)", info["pages"] >= 1, f"pages={info['pages']}")
        ev.papers.append(
            {
                "arxiv_id": "1706.03762",
                "title": "Attention Is All You Need",
                "path": str(target),
                **info,
            }
        )
        recs = [
            d
            for d in db_rows(db, "downloads")
            if d["uid"] == "arxiv:1706.03762" and d["status"] == "ok"
        ]
        check(
            "SQLite records uid + size + sha256",
            bool(recs)
            and recs[0]["size_bytes"] == info["bytes"]
            and recs[0]["sha256"] == info["sha256"],
            (recs[0]["sha256"] or "")[:12] if recs else "no record",
        )

    # ── get by arXiv DOI ───────────────────────────────────────────────────
    r = mosaic(
        ["get", "10.48550/arXiv.2106.09685", "--download-dir", str(pdfs)], env
    )
    lora = next((p for p in pdfs.glob("*LoRA*.pdf")), None)
    check(
        "get 10.48550/arXiv.2106.09685 (arXiv DOI)",
        r.returncode == 0 and lora is not None,
        f"exit={r.returncode}",
    )

    # ── dedup on repeat ────────────────────────────────────────────────────
    count0 = len(list(pdfs.glob("*.pdf")))
    r = mosaic(["get", "arxiv:1706.03762", "--download-dir", str(pdfs)], env)
    count1 = len(list(pdfs.glob("*.pdf")))
    check(
        "repeat get reuses file (no duplicate)",
        r.returncode == 0 and count1 == count0 and "Already downloaded" in r.stdout,
        f"files {count0}→{count1}",
    )

    # ── invalid identifier ─────────────────────────────────────────────────
    r = mosaic(["get", "not-a-real-identifier"], env)
    check("invalid identifier → non-zero exit", r.returncode != 0, f"exit={r.returncode}")

    # ── --json --download really downloads ────────────────────────────────
    jdir = pdfs / "jsontest"
    jdir.mkdir(exist_ok=True)
    r = mosaic(
        [
            "search", "quantum diffusion monte carlo", "--source", "arxiv",
            "--max", "1", "--download", "--download-dir", str(jdir), "--json",
        ],
        env,
    )
    try:
        doc = json.loads(r.stdout)
        dl = doc.get("downloads") or {}
        dl_paths = [Path(v) for v in dl.values() if not v.startswith("_")]
        check(
            "--json --download performs real download",
            r.returncode == 0 and bool(dl_paths) and all(p.exists() for p in dl_paths),
            f"downloads={len(dl_paths)}",
        )
    except json.JSONDecodeError:
        check(
            "--json --download performs real download",
            False,
            f"unparseable JSON: {r.stdout[:80]!r} / err {r.stderr[:80]!r}",
        )

    # ── no-aria2 isolation ─────────────────────────────────────────────────
    if args.aria2_absent:
        env2 = dict(env)
        env2["PATH"] = ":".join(
            p for p in env["PATH"].split(":") if "homebrew" not in p and "/local/bin" not in p
        )
        gone = shutil.which("aria2c") is None or "aria2c" not in os.popen(
            f"PATH={env2['PATH']} command -v aria2c || true"
        ).read()
        r = mosaic(
            ["get", "arxiv:2307.09288", "--download-dir", str(pdfs)],
            dict(env2, PATH=env2["PATH"]),
        )
        check(
            "native download with aria2c absent",
            r.returncode == 0 and gone,
            f"exit={r.returncode} aria2c_hidden={gone}",
        )

    # ── aria2 backend ──────────────────────────────────────────────────────
    if args.backend in ("aria2", "all"):
        if not shutil.which("aria2c"):
            check("aria2 backend real download", False, "aria2c not installed")
        else:
            r = mosaic(["config", "--download-backend", "aria2"], env)
            check(
                "config --download-backend aria2 persists",
                cfg_toml.exists() and 'backend = "aria2"' in cfg_toml.read_text(),
                "",
            )
            t = time.time()
            r = mosaic(
                [
                    "search", "graph neural network", "--source", "arxiv",
                    "--max", "2", "--download", "--download-dir", str(aria2_pdfs),
                ],
                env,
            )
            dur = time.time() - t
            files = list(aria2_pdfs.glob("*.pdf"))
            a2_jobs = [
                j
                for j in db_rows(db, "download_jobs")
                if j["status"] == "COMPLETED" and j["backend"] == "aria2" and j["aria2_gid"]
            ]
            check(
                "aria2 backend real download",
                r.returncode == 0 and len(files) >= 1 and bool(a2_jobs),
                f"exit={r.returncode} files={len(files)} gid_jobs={len(a2_jobs)} {dur:.0f}s",
            )
            mosaic(["config", "--download-backend", "native"], env)

    ev.env["duration_s"] = f"{time.time() - t0:.0f}"

    # Reap the managed daemon this workdir spawned (it detaches by design;
    # each smoke HOME would otherwise leak a daemon per run).
    daemon_state = home / ".local" / "share" / "mosaic" / "aria2.daemon.json"
    if daemon_state.exists():
        try:
            st = json.loads(daemon_state.read_text())
            pid = int(st.get("pid") or 0)
            if pid:
                import signal

                os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass

    (workdir / "smoke-evidence.json").write_text(
        json.dumps(
            {
                "env": ev.env,
                "checks": [vars(c) for c in ev.checks],
                "papers": ev.papers,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    print(f"\nevidence -> {workdir / 'smoke-evidence.json'}")
    return report(ev)


if __name__ == "__main__":
    sys.exit(main())
