"""Disposable Postgres for e2e scripts.

Prefers Docker (postgres:17-alpine); falls back to a brew/pg_ctl temp
instance when no Docker daemon is available. The caller owns teardown via
TempPg.stop(); everything lives on an ephemeral port + temp dir, nothing
touches system databases.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid

PG_IMAGE = "postgres:17-alpine"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TempPg:
    def __init__(self, dsn: str, stop_fn):
        self.dsn = dsn
        self._stop = stop_fn

    def stop(self) -> None:
        try:
            self._stop()
        except Exception:
            pass


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=15)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _start_docker() -> TempPg:
    port = _free_port()
    name = f"bcrc-e2e-pg-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name,
         "-e", "POSTGRES_USER=bcrc", "-e", "POSTGRES_PASSWORD=bcrc",
         "-e", "POSTGRES_DB=bcrc",
         "-p", f"127.0.0.1:{port}:5432", PG_IMAGE],
        check=True, capture_output=True)

    def stop():
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    return TempPg(f"postgresql://bcrc:bcrc@127.0.0.1:{port}/bcrc", stop)


def _start_pgctl() -> TempPg:
    for tool in ("initdb", "pg_ctl"):
        if not shutil.which(tool):
            raise RuntimeError(f"no Docker daemon and no {tool} on PATH — "
                               "cannot provision a test Postgres")
    port = _free_port()
    datadir = tempfile.mkdtemp(prefix="bcrc-e2e-pg-")
    subprocess.run(
        ["initdb", "-D", datadir, "-U", "bcrc", "--auth=trust",
         "-E", "UTF8"], check=True, capture_output=True)
    subprocess.run(
        ["pg_ctl", "-D", datadir, "-w", "-o",
         f"-p {port} -c listen_addresses=127.0.0.1 "
         f"-c unix_socket_directories={datadir}",
         "-l", os.path.join(datadir, "log"), "start"],
        check=True, capture_output=True)

    def stop():
        subprocess.run(["pg_ctl", "-D", datadir, "-m", "immediate", "stop"],
                       capture_output=True)
        shutil.rmtree(datadir, ignore_errors=True)

    return TempPg(f"postgresql://bcrc@127.0.0.1:{port}/postgres", stop)


def start() -> TempPg:
    if _docker_available():
        return _start_docker()
    return _start_pgctl()


def _main() -> None:
    import asyncio

    pg = start()
    try:
        asyncio.run(wait_ready(pg.dsn))
        print(f"DSN (this machine):     {pg.dsn}")
        print("DSN (from a container): "
              + pg.dsn.replace("127.0.0.1", "host.docker.internal"))
        print("Postgres is up — Ctrl-C to stop and tear down.", flush=True)
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        pg.stop()


async def wait_ready(dsn: str, timeout: float = 90.0) -> None:
    """Retry-connect until the server accepts queries (the docker image
    restarts once during init, so a single successful TCP dial is not
    enough — we need a real round trip)."""
    import asyncio

    import asyncpg
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            conn = await asyncpg.connect(dsn, timeout=5)
            await conn.execute("SELECT 1")
            await conn.close()
            return
        except Exception as e:  # noqa: BLE001 — retry anything until ready
            last = e
            await asyncio.sleep(0.4)
    raise RuntimeError(f"postgres not ready after {timeout}s: {last!r}")


if __name__ == "__main__":
    _main()
