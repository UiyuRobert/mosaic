"""aria2 JSON-RPC client and managed-daemon lifecycle.

Only ``127.0.0.1`` is ever bound — the RPC endpoint is never exposed on a
public interface. The RPC secret is kept in memory (or read from config)
and is never written to logs or rendered into HTML.

The managed daemon is spawned *without* ``--daemon`` so ``Popen.poll()``
stays a meaningful liveness signal; ``start_new_session`` detaches it from
our process group so a Ctrl-C or crash does not kill in-flight downloads.
Daemon coordinates (pid + ephemeral secret) are persisted to
``aria2.daemon.json`` (mode 0600) next to the session file so a later
MOSAIC run can re-attach to — or safely reap — an orphaned daemon.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_DEFAULT_RPC = "http://127.0.0.1:6800/jsonrpc"


class Aria2Error(Exception):
    """JSON-RPC or daemon-management failure."""


class Aria2Client:
    """Thin JSON-RPC wrapper for a local aria2 daemon."""

    def __init__(self, rpc_url: str = _DEFAULT_RPC, secret: str = "") -> None:
        self.rpc_url = rpc_url
        self._secret = secret
        self._id = 0
        self.process: subprocess.Popen | None = None  # set when we spawned it
        self.session_file: Path | None = None
        self.managed = False  # daemon lifecycle is ours to end

    # -- RPC plumbing ---------------------------------------------------------

    def _call(self, method: str, params: list | None = None) -> object:
        self._id += 1
        call_params = list(params or [])
        if self._secret:
            call_params.insert(0, f"token:{self._secret}")
        payload = {
            "jsonrpc": "2.0",
            "id": f"mosaic-{self._id}",
            "method": method,
            "params": call_params,
        }
        try:
            resp = httpx.post(self.rpc_url, json=payload, timeout=15)
        except httpx.HTTPError as e:
            raise Aria2Error(f"aria2 RPC unreachable at {self.rpc_url}: {e}") from e
        if resp.status_code != 200:
            raise Aria2Error(f"aria2 RPC HTTP {resp.status_code}")
        data = resp.json()
        if "error" in data:
            err = data["error"]
            raise Aria2Error(f"{err.get('message', 'RPC error')} (code {err.get('code')})")
        return data.get("result")

    def is_alive(self) -> bool:
        """True when the daemon answers aria2.getVersion."""
        try:
            self._call("aria2.getVersion")
            return True
        except Aria2Error:
            return False

    # -- Download operations ---------------------------------------------------

    def add_uri(self, url: str, options: dict[str, str]) -> str:
        """Queue ``url`` for download; returns the new GID."""
        return str(self._call("aria2.addUri", [[url], options]))

    def tell_status(self, gid: str) -> dict:
        keys = [
            "gid",
            "status",
            "totalLength",
            "completedLength",
            "downloadSpeed",
            "errorCode",
            "errorMessage",
            "dir",
            "files",
        ]
        result = self._call("aria2.tellStatus", [gid, keys])
        return result if isinstance(result, dict) else {}

    def pause(self, gid: str) -> None:
        self._call("aria2.pause", [gid])

    def unpause(self, gid: str) -> None:
        self._call("aria2.unpause", [gid])

    def remove(self, gid: str) -> None:
        """Remove a download (active → stopped → purged)."""
        try:
            self._call("aria2.remove", [gid])
        except Aria2Error:
            pass
        for method in ("aria2.removeDownloadResult", "aria2.forceRemove"):
            try:
                self._call(method, [gid])
            except Aria2Error:
                pass

    def shutdown(self) -> None:
        """Ask the daemon to exit (only used for app-managed processes)."""
        try:
            self._call("aria2.shutdown")
        except Aria2Error:
            pass
        if self.process is not None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()


def find_binary() -> str | None:
    """Return the aria2c executable path, or None when not installed."""
    return shutil.which("aria2c")


def ensure_client(cfg: dict, *, state_dir: Path | None = None) -> Aria2Client | None:
    """Connect to (or start) a local aria2 daemon per ``cfg['download']``.

    Order: attach to an already-running daemon at ``aria2_rpc_url``; else
    re-attach to a daemon we spawned in a previous run (recorded in
    ``aria2.daemon.json``); else when ``aria2_auto_start`` is set and the
    binary exists, spawn a managed daemon bound to 127.0.0.1 with a session
    file under *state_dir*.

    Returns:
        A connected :class:`Aria2Client`, or ``None`` when aria2 is
        unavailable (caller decides whether native fallback is allowed).
    """
    dl = cfg.get("download", {})
    rpc_url = dl.get("aria2_rpc_url") or _DEFAULT_RPC
    secret = dl.get("aria2_rpc_secret", "")

    client = Aria2Client(rpc_url, secret)
    if client.is_alive():
        log.info("Attached to existing aria2 daemon at %s", rpc_url)
        return client

    state_dir = state_dir or Path(cfg.get("db_path", ".")).expanduser().parent
    state_dir.mkdir(parents=True, exist_ok=True)

    # A previous run may have left a daemon orphaned (our process died while
    # aria2 kept serving). Re-attach with the persisted secret, or reap it
    # when it no longer answers so the port can be reused below.
    managed = _reattach_managed(rpc_url, secret, state_dir)
    if managed is not None:
        return managed

    if not dl.get("aria2_auto_start", True):
        return None
    binary = find_binary()
    if not binary:
        return None

    # Generate an ephemeral secret when the user hasn't configured one —
    # kept in process memory and the 0600 daemon state file only.
    if not secret:
        secret = secrets.token_hex(16)
        client = Aria2Client(rpc_url, secret)

    session = state_dir / "aria2.session"
    session.touch(exist_ok=True)

    port = _free_port(_port_of(rpc_url))
    if port is None:
        log.warning(
            "no free loopback port near %d — aria2 daemon cannot be started",
            _port_of(rpc_url),
        )
        return None
    if port != _port_of(rpc_url):
        rpc_url = f"http://127.0.0.1:{port}/jsonrpc"
        client = Aria2Client(rpc_url, secret)
    proc = subprocess.Popen(
        [
            binary,
            "--enable-rpc",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={port}",
            f"--rpc-secret={secret}",
            f"--save-session={session}",
            f"--input-file={session}",
            "--save-session-interval=30",
            "--continue=true",
            # Abort transfers that keep a connection alive while trickling
            # ~nothing — otherwise a drip-feeding host pins a queue job in
            # DOWNLOADING indefinitely.
            "--lowest-speed-limit=1K",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    client.process = proc
    client.session_file = session
    client.managed = True

    # Wait for the RPC endpoint to come up. aria2c runs as our direct child
    # (no --daemon), so an early exit really is a startup failure — most
    # commonly a stale listener on the RPC port.
    for _ in range(50):
        if proc.poll() is not None:
            log.warning("aria2c exited immediately (code %s)", proc.returncode)
            recovered = _reattach_managed(rpc_url, secret, state_dir)
            if recovered is not None:
                return recovered
            return None
        if client.is_alive():
            log.info("Started managed aria2 daemon (pid %s)", proc.pid)
            _write_daemon_state(state_dir, rpc_url, proc.pid, secret)
            return client
        time.sleep(0.1)
    log.warning("aria2 daemon did not become ready in time")
    proc.terminate()
    return None


# ---------------------------------------------------------------------------
# Managed-daemon state file
# ---------------------------------------------------------------------------

_STATE_NAME = "aria2.daemon.json"


def _state_path(state_dir: Path) -> Path:
    return state_dir / _STATE_NAME


def _write_daemon_state(state_dir: Path, rpc_url: str, pid: int, secret: str) -> None:
    """Persist daemon coordinates for orphan re-attach (mode 0600)."""
    try:
        path = _state_path(state_dir)
        path.write_text(
            json.dumps({"pid": pid, "rpc_url": rpc_url, "secret": secret}),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
    except OSError:
        log.debug("could not write aria2 daemon state file", exc_info=True)


def _pid_is_aria2(pid: int) -> bool:
    """True when *pid* is alive and its command is aria2c."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        return "aria2c" in out
    except (OSError, subprocess.TimeoutExpired):
        # PID responds to signals — treat as ours rather than leaving it wedged.
        return True


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _reattach_managed(rpc_url: str, secret: str, state_dir: Path) -> Aria2Client | None:
    """Re-attach to a previously spawned daemon, or reap a wedged one.

    The recorded daemon URL is used directly — it may differ from
    ``rpc_url`` when a previous run had to spawn on an alternate port.
    Returns a connected client when the recorded daemon still answers its
    persisted (or the configured) secret. When the recorded PID is alive
    but does not answer — a wedged orphan — it is terminated so a fresh
    daemon can be spawned; ``None`` is returned in both failure cases.
    """
    try:
        state = json.loads(_state_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    managed_url = state.get("rpc_url") or rpc_url
    pid = int(state.get("pid") or 0)
    if not pid or not _pid_is_aria2(pid):
        _state_path(state_dir).unlink(missing_ok=True)
        return None

    for candidate_secret in (secret, state.get("secret", "")):
        client = Aria2Client(managed_url, candidate_secret or "")
        if client.is_alive():
            log.info("Re-attached to managed aria2 daemon (pid %s)", pid)
            client.managed = True
            client.session_file = state_dir / "aria2.session"
            return client

    # PID is alive but the daemon answers neither secret — reap our orphan.
    log.warning("Reaping unresponsive managed aria2 daemon (pid %s)", pid)
    _kill_pid(pid)
    _state_path(state_dir).unlink(missing_ok=True)
    return None


def _port_of(rpc_url: str) -> int:
    from urllib.parse import urlparse

    port = urlparse(rpc_url).port
    return port or 6800


def _free_port(preferred: int, scan: int = 32) -> int | None:
    """Return *preferred* when it is free, else the first free port after it.

    A foreign listener on the default port (a stale daemon started by
    another tool, or an orphaned aria2c whose secret we never knew) must
    not wedge the managed-daemon path — spawning on a nearby free port
    keeps 127.0.0.1 binding without killing processes we do not own.
    Returns ``None`` when no port in the scan window is free — spawning on
    a known-busy port would just produce an immediate bind failure.
    """
    import socket

    for offset in range(scan):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                if offset:
                    log.warning(
                        "aria2 RPC port %d occupied — using %d instead",
                        preferred,
                        port,
                    )
                return port
    return None
