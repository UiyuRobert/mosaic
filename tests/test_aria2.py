"""Tests for the aria2 JSON-RPC client (daemon interaction is mocked)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mosaic.aria2 import Aria2Client, Aria2Error, ensure_client


def _rpc_ok(result):
    def handler(request):
        return _resp(200, {"jsonrpc": "2.0", "id": "x", "result": result})

    return handler


def _resp(status: int, payload: dict):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = json.dumps(payload)
    return r


def _err_resp(code: int, message: str):
    return _resp(200, {"jsonrpc": "2.0", "id": "x", "error": {"code": code, "message": message}})


class TestRpcCalls:
    def test_add_uri_sends_options(self):
        seen = {}

        def handler(request):
            payload = json.loads(request.content)
            seen["method"] = payload["method"]
            seen["params"] = payload["params"]
            return _resp(200, {"result": "gid-abc"})

        client = Aria2Client("http://127.0.0.1:6800/jsonrpc")
        with patch(
            "httpx.post", side_effect=lambda url, json=None, **kw: handler(MagicMock(content=json))
        ):
            # simpler: patch _call directly for the payload assertions
            pass
        with patch.object(Aria2Client, "_call", return_value="gid-abc") as m:
            gid = client.add_uri("https://x/f.pdf", {"dir": "/tmp", "out": "f.part"})
        assert gid == "gid-abc"
        m.assert_called_once_with(
            "aria2.addUri", [["https://x/f.pdf"], {"dir": "/tmp", "out": "f.part"}]
        )

    def test_secret_prefixed_as_token(self):
        seen = {}

        def fake_post(url, json=None, timeout=None, **kw):
            seen.update(json)
            return _resp(200, {"result": "ok"})

        client = Aria2Client("http://127.0.0.1:6800/jsonrpc", secret="s3cret")
        with patch("httpx.post", side_effect=fake_post):
            client._call("aria2.getVersion")
        assert seen["params"][0] == "token:s3cret"

    def test_rpc_error_raises(self):
        client = Aria2Client()
        with patch("httpx.post", return_value=_err_resp(1, "No such download")):
            with pytest.raises(Aria2Error, match="No such download"):
                client.tell_status("deadbeef")

    def test_unreachable_raises(self):
        import httpx

        client = Aria2Client()
        with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
            assert client.is_alive() is False  # errors are swallowed
        with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
            with pytest.raises(Aria2Error, match="unreachable"):
                client._call("aria2.getVersion")

    def test_tell_status_returns_dict(self):
        st = {"gid": "g1", "status": "active", "completedLength": "10", "totalLength": "100"}
        client = Aria2Client()
        with patch("httpx.post", return_value=_resp(200, {"result": st})):
            out = client.tell_status("g1")
        assert out["status"] == "active"


class TestEnsureClient:
    def _cfg(self, tmp_path, backend="aria2", auto_start=True):
        return {
            "db_path": str(tmp_path / "cache.db"),
            "download": {
                "backend": backend,
                "aria2_rpc_url": "http://127.0.0.1:6800/jsonrpc",
                "aria2_rpc_secret": "",
                "aria2_auto_start": auto_start,
            },
        }

    def test_attaches_to_running_daemon(self, tmp_path):
        # First call must not spawn or attach to a real daemon — pretend
        # no daemon answers and aria2c is absent (keeps the test hermetic
        # even when a live daemon happens to run on this machine).
        with (
            patch.object(Aria2Client, "is_alive", return_value=False),
            patch("mosaic.aria2.find_binary", return_value=None),
        ):
            client = ensure_client(self._cfg(tmp_path), state_dir=tmp_path)
        assert client is None
        with patch.object(Aria2Client, "is_alive", return_value=True):
            c = ensure_client(self._cfg(tmp_path), state_dir=tmp_path)
        assert c is not None

    def test_spawns_managed_daemon(self, tmp_path):
        calls = {"n": 0}

        def alive(self):
            calls["n"] += 1
            return calls["n"] > 1  # not running at first, running after spawn

        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.pid = 1234
        with (
            patch.object(Aria2Client, "is_alive", alive),
            patch("mosaic.aria2.find_binary", return_value="/usr/bin/aria2c"),
            patch("subprocess.Popen", return_value=fake_proc) as popen,
        ):
            client = ensure_client(self._cfg(tmp_path), state_dir=tmp_path)
        assert client is not None
        assert client.process is fake_proc
        args = popen.call_args.args[0]
        assert "--enable-rpc" in args
        assert "--rpc-listen-all=false" in args
        assert any("--rpc-secret=" in a for a in args)
        assert any("save-session" in a for a in args)

    def test_returns_none_without_binary(self, tmp_path):
        with (
            patch.object(Aria2Client, "is_alive", return_value=False),
            patch("mosaic.aria2.find_binary", return_value=None),
        ):
            assert ensure_client(self._cfg(tmp_path), state_dir=tmp_path) is None

    def test_no_autostart_returns_none(self, tmp_path):
        with (
            patch.object(Aria2Client, "is_alive", return_value=False),
            patch("mosaic.aria2.find_binary", return_value="/usr/bin/aria2c"),
        ):
            assert ensure_client(self._cfg(tmp_path, auto_start=False), state_dir=tmp_path) is None

    def test_spawn_is_direct_child_not_daemon(self, tmp_path):
        """Regression: --daemon made the parent exit(0) and proc.poll() then
        misreported a live daemon as a startup failure."""
        calls = {"n": 0}

        def alive(self):
            calls["n"] += 1
            return calls["n"] > 1

        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.pid = 4321
        with (
            patch.object(Aria2Client, "is_alive", alive),
            patch("mosaic.aria2.find_binary", return_value="/usr/bin/aria2c"),
            patch("subprocess.Popen", return_value=fake_proc) as popen,
        ):
            client = ensure_client(self._cfg(tmp_path), state_dir=tmp_path)
        assert client is not None
        args = popen.call_args.args[0]
        assert "--daemon=true" not in args
        assert popen.call_args.kwargs.get("start_new_session") is True

    def test_daemon_state_written_and_reattached(self, tmp_path):
        """An orphaned daemon (our crashed previous run) is re-attached via
        aria2.daemon.json instead of spawning a second one."""
        state = tmp_path / "aria2.daemon.json"
        state.write_text(
            json.dumps({"pid": 9999, "rpc_url": "http://127.0.0.1:6800/jsonrpc", "secret": "s1"})
        )

        calls = {"n": 0}

        def alive(self):
            calls["n"] += 1
            # configured secret fails; stored secret succeeds
            return self._secret == "s1" or calls["n"] > 2

        with (
            patch.object(Aria2Client, "is_alive", alive),
            patch("mosaic.aria2._pid_is_aria2", return_value=True),
            patch("mosaic.aria2.find_binary", return_value="/usr/bin/aria2c"),
            patch("subprocess.Popen") as popen,
        ):
            client = ensure_client(self._cfg(tmp_path), state_dir=tmp_path)
        assert client is not None
        assert client.managed is True
        popen.assert_not_called()

    def test_wedged_orphan_is_reaped(self, tmp_path):
        """A recorded-but-unresponsive daemon gets killed so a fresh spawn works."""
        state = tmp_path / "aria2.daemon.json"
        state.write_text(
            json.dumps({"pid": 8888, "rpc_url": "http://127.0.0.1:6800/jsonrpc", "secret": "s1"})
        )
        with (
            patch.object(Aria2Client, "is_alive", return_value=False),
            patch("mosaic.aria2._pid_is_aria2", return_value=True),
            patch("mosaic.aria2._kill_pid") as kill,
            patch("mosaic.aria2.find_binary", return_value=None),
        ):
            assert ensure_client(self._cfg(tmp_path), state_dir=tmp_path) is None
        kill.assert_called_once_with(8888)
        assert not state.exists()

    def test_free_port_skips_occupied(self):
        import socket

        from mosaic.aria2 import _free_port

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.listen(1)
            chosen = _free_port(port, scan=4)
        assert chosen == port + 1


class TestAria2BackendInQueue:
    """End-to-end queue behaviour with a mocked aria2 client."""

    def test_job_completes_via_aria2(self, tmp_path):
        from mosaic.db import Cache
        from mosaic.dlqueue import DownloadManager
        from mosaic.models import Paper

        cfg = {
            "db_path": str(tmp_path / "c.db"),
            "download_dir": str(tmp_path / "papers"),
            "filename_pattern": "{year}_{title}",
            "unpaywall": {"email": ""},
            "download": {
                "backend": "aria2",
                "max_concurrent": 2,
                "per_host_concurrent": 5,
                "per_host_delay": 0,
                "max_tries": 1,
                "verify_pdf": False,
                "skip_existing": False,
                "aria2_rpc_url": "http://127.0.0.1:6800/jsonrpc",
            },
        }
        cache = Cache(cfg["db_path"])
        m = DownloadManager(cache, cfg)

        # Inject the fake client before enqueueing so the lazy _ensure_aria2
        # path never spawns a real daemon.
        fake_client = MagicMock()
        fake_client.is_alive.return_value = True
        fake_client.add_uri.return_value = "gid-1"
        fake_client.process = None
        fake_client.managed = False
        m._aria2 = fake_client

        p = Paper(title="T1", year=2020, pdf_url="https://x.example/f.pdf", source="t")
        ids = m.enqueue_papers([p])
        job = cache.get_job(ids[0])
        assert job["backend"] == "aria2"

        dest = Path(job["destination"])
        part = Path(str(dest) + ".part")

        statuses = iter([{"status": "active", "completedLength": "1", "totalLength": "100"}])

        def tell_status(gid, *a):
            try:
                return next(statuses)
            except StopIteration:
                # simulate completed file
                part.parent.mkdir(parents=True, exist_ok=True)
                part.write_bytes(b"%PDF-1.4\n" + b"x" * 2000 + b"%%EOF")
                return {"status": "complete"}

        fake_client.tell_status.side_effect = tell_status
        m.start()
        try:
            result = m.wait_all(ids, poll=0.05)
        finally:
            m.shutdown()
        assert result.get(p.uid) == str(dest)
        assert dest.exists()
        assert cache.get_job(ids[0])["status"] == "COMPLETED"
        assert cache.get_download(p.uid)["status"] == "ok"

    def test_aria2_error_marks_failed(self, tmp_path):
        from mosaic.db import Cache
        from mosaic.dlqueue import DownloadManager
        from mosaic.models import Paper

        cfg = {
            "db_path": str(tmp_path / "c.db"),
            "download_dir": str(tmp_path / "papers"),
            "unpaywall": {"email": ""},
            "download": {
                "backend": "aria2",
                "max_concurrent": 2,
                "per_host_concurrent": 5,
                "per_host_delay": 0,
                "max_tries": 1,
                "verify_pdf": False,
                "aria2_rpc_url": "http://127.0.0.1:6800/jsonrpc",
            },
        }
        cache = Cache(cfg["db_path"])
        m = DownloadManager(cache, cfg)

        fake_client = MagicMock()
        fake_client.is_alive.return_value = True
        fake_client.add_uri.return_value = "gid-err"
        fake_client.process = None
        fake_client.managed = False
        fake_client.tell_status.return_value = {
            "status": "error",
            "errorCode": "3",
            "errorMessage": "resource not found (HTTP 404)",
        }
        m._aria2 = fake_client

        p = Paper(title="T2", year=2020, pdf_url="https://x.example/missing.pdf", source="t")
        ids = m.enqueue_papers([p])
        m.start()
        try:
            m.wait_all(ids, poll=0.05)
        finally:
            m.shutdown()
        job = cache.get_job(ids[0])
        assert job["status"] == "FAILED"
        assert "not_found" in job["last_error"]

    def test_lost_gid_requeues_job(self, tmp_path):
        """tellStatus 'GID not found' (daemon restarted without the session)
        must requeue the job instead of leaving it stuck DOWNLOADING."""
        from mosaic.db import Cache
        from mosaic.dlqueue import DownloadManager
        from mosaic.models import Paper

        cfg = {
            "db_path": str(tmp_path / "c.db"),
            "download_dir": str(tmp_path / "papers"),
            "unpaywall": {"email": ""},
            "download": {
                "backend": "aria2",
                "max_concurrent": 2,
                "per_host_concurrent": 5,
                "per_host_delay": 0,
                "max_tries": 1,
                "verify_pdf": False,
                "skip_existing": False,
                "aria2_rpc_url": "http://127.0.0.1:6800/jsonrpc",
            },
        }
        cache = Cache(cfg["db_path"])
        m = DownloadManager(cache, cfg)

        fake_client = MagicMock()
        fake_client.is_alive.return_value = True
        fake_client.add_uri.return_value = "gid-lost"
        fake_client.process = None
        fake_client.managed = False
        fake_client.tell_status.side_effect = Aria2Error("GID#gid-lost is not found (code 1)")
        m._aria2 = fake_client

        p = Paper(title="T3", year=2020, pdf_url="https://x.example/r.pdf", source="t")
        ids = m.enqueue_papers([p])
        m._poll_aria2_jobs()
        job = cache.get_job(ids[0])
        assert job["status"] == "QUEUED"
        assert job["aria2_gid"] is None
