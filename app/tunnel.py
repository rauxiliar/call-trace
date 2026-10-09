"""Optional SSH local forward. Absent ssh means a direct DSN."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

SSH_RE = re.compile(r"^[A-Za-z0-9._@-]+$")
TUNNEL_FAILED = "Não foi possível abrir o túnel SSH."
SSH_REJECTED = "Destino SSH inválido."
KEY_INVALID = "Chave SSH inválida."
KEY_MISSING = "Chave SSH não encontrada."
READY_TIMEOUT = 8.0
ASKPASS = Path(__file__).resolve().with_name("ssh_askpass.py")


class TunnelError(Exception):
    """Message is safe to show: no DSN, password, or SSH destination."""


def checked_ssh(target: str) -> str:
    if SSH_RE.fullmatch(target) is None:
        raise TunnelError(SSH_REJECTED)
    return target


def checked_ssh_key(path: str, root: Path) -> str:
    text = path.strip()
    if not text or "\x00" in text or "\n" in text or "\r" in text:
        raise TunnelError(KEY_INVALID)
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    if not candidate.is_file():
        raise TunnelError(KEY_MISSING)
    return str(candidate)


def _endpoint(dsn: str) -> tuple[str, int]:
    parts = urlsplit(dsn)
    host = parts.hostname
    if not host:
        raise TunnelError(TUNNEL_FAILED)
    return host, parts.port or 5432


def rewrite_local(dsn: str, port: int) -> str:
    parts = urlsplit(dsn)
    if "@" in parts.netloc:
        userinfo = parts.netloc.rsplit("@", 1)[0]
        netloc = f"{userinfo}@127.0.0.1:{port}"
    else:
        netloc = f"127.0.0.1:{port}"
    return urlunsplit((parts.scheme or "postgres", netloc, parts.path, parts.query, parts.fragment))


def ssh_argv(
    target: str,
    local_port: int,
    remote_host: str,
    remote_port: int,
    key: str = "",
    use_password: bool = False,
) -> list[str]:
    destination = checked_ssh(target)
    remote = f"[{remote_host}]" if ":" in remote_host else remote_host
    argv = [
        "ssh",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if key:
        argv.extend(["-i", key, "-o", "IdentitiesOnly=yes"])
    if use_password:
        methods = "publickey,password,keyboard-interactive" if key else "password,keyboard-interactive"
        argv.extend([
            "-o",
            "NumberOfPasswordPrompts=1",
            "-o",
            f"PreferredAuthentications={methods}",
        ])
    else:
        argv.extend(["-o", "BatchMode=yes"])
    argv.extend(["-L", f"127.0.0.1:{local_port}:{remote}:{remote_port}", destination])
    return argv


def askpass_env(password: str) -> dict[str, str]:
    env = os.environ.copy()
    env["SSH_ASKPASS"] = str(ASKPASS)
    env["SSH_ASKPASS_REQUIRE"] = "force"
    env["CALLTRACE_SSH_PASSWORD"] = password
    env.setdefault("DISPLAY", "localhost:0")
    return env


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_open(port: int, proc: subprocess.Popen, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise TunnelError(TUNNEL_FAILED)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TunnelError(TUNNEL_FAILED)


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=1)
    except (subprocess.TimeoutExpired, OSError):
        try:
            proc.kill()
        except OSError:
            pass


class TunnelBook:
    def __init__(self, popen=subprocess.Popen, port_for=free_local_port, timeout: float = READY_TIMEOUT) -> None:
        self._popen = popen
        self._port_for = port_for
        self._timeout = timeout
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}
        self._ports: dict[str, int] = {}

    def local_dsn(
        self,
        cloud_id: str,
        dsn: str,
        ssh_target: str,
        key: str = "",
        password: str = "",
    ) -> str:
        with self._lock:
            host, remote_port = _endpoint(dsn)
            forward_key = f"{cloud_id}\n{host}\n{remote_port}"
            proc = self._procs.get(forward_key)
            port = self._ports.get(forward_key)
            if proc is not None and port and proc.poll() is None:
                return rewrite_local(dsn, port)
            port = self._port_for()
            argv = ssh_argv(ssh_target, port, host, remote_port, key=key, use_password=bool(password))
            try:
                proc = self._popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=askpass_env(password) if password else None,
                    start_new_session=True,
                )
            except OSError as exc:
                raise TunnelError(TUNNEL_FAILED) from exc
            try:
                _wait_until_open(port, proc, self._timeout)
            except TunnelError:
                _stop(proc)
                raise
            self._procs[forward_key] = proc
            self._ports[forward_key] = port
            return rewrite_local(dsn, port)


default_tunnels = TunnelBook()


def _through_tunnel(cloud, dsn: str, tunnels: TunnelBook | None) -> str:
    target = (cloud.ssh or "").strip()
    if not target:
        return dsn
    book = default_tunnels if tunnels is None else tunnels
    return book.local_dsn(
        cloud.id,
        dsn,
        target,
        key=(cloud.ssh_key or "").strip(),
        password=cloud.ssh_password or "",
    )


def connect_dsn(cloud, tunnels: TunnelBook | None = None) -> str:
    return _through_tunnel(cloud, cloud.dsn, tunnels)


def connect_domains_dsn(cloud, tunnels: TunnelBook | None = None) -> str:
    dsn = (cloud.domains_dsn or "").strip()
    if not dsn:
        return ""
    return _through_tunnel(cloud, dsn, tunnels)
