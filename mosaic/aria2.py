"""aria2 JSON-RPC client and managed-daemon lifecycle.

Only ``127.0.0.1`` is ever bound — the RPC endpoint is never exposed on a
public interface. The RPC secret is kept in memory (or read from config)
and is never written to logs or rendered into HTML.
"""

from __future__ import annotations

import logging
import secrets
import shutil
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


def find_binary() -> str | None:
    """Return the aria2c executable path, or None when not installed."""
    return shutil.which("aria2c")


def ensure_client(cfg: dict, *, state_dir: Path | None = None) -> Aria2Client | None:
    """Connect to (or start) a local aria2 daemon per ``cfg['download']``.

    Order: attach to an already-running daemon at ``aria2_rpc_url``; else
    when ``aria2_auto_start`` is set and the binary exists, spawn a managed
    daemon bound to 127.0.0.1 with a session file under *state_dir*.

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

    if not dl.get("aria2_auto_start", True):
        return None
    binary = find_binary()
    if not binary:
        return None

    # Generate an ephemeral secret when the user hasn't configured one —
    # kept in process memory only.
    if not secret:
        secret = secrets.token_hex(16)
        client = Aria2Client(rpc_url, secret)

    state_dir = state_dir or Path(cfg.get("db_path", ".")).expanduser().parent
    state_dir.mkdir(parents=True, exist_ok=True)
    session = state_dir / "aria2.session"
    session.touch(exist_ok=True)

    port = _port_of(rpc_url)
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
            "--daemon=true",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client.process = proc
    client.session_file = session

    # Wait for the RPC endpoint to come up.
    for _ in range(50):
        if proc.poll() is not None:
            log.warning("aria2c exited immediately (code %s)", proc.returncode)
            return None
        if client.is_alive():
            log.info("Started managed aria2 daemon (pid %s)", proc.pid)
            return client
        time.sleep(0.1)
    log.warning("aria2 daemon did not become ready in time")
    return None


def _port_of(rpc_url: str) -> int:
    from urllib.parse import urlparse

    port = urlparse(rpc_url).port
    return port or 6800
