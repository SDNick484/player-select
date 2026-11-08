"""Helpers shared by the test modules. Standard library only.

Not a test module itself (unittest discovery only collects test*.py).
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
FIXTURES = TESTS / "fixtures"
SERVER = ROOT / "player_select_server.py"
PERSONS = [{"name": "Player A", "color": "#1f77b4"}, {"name": "Player B", "color": "#ff7f0e"}]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.05, what: str = "condition"):
    """Poll until predicate() returns something truthy; return it. Raises AssertionError on timeout."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what} (last value: {last!r})")


# ------------------------------------------------------------------ HTTP client

class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers  # lower-cased names
        self.body = body

    def json(self):
        return json.loads(self.body)

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def request(port: int, method: str, path: str, body=None, headers: dict[str, str] | None = None,
            timeout: float = 15.0) -> Response:
    """One request on a fresh connection. `body` may be bytes, str, or anything
    JSON-serializable (sent as application/json unless headers say otherwise)."""
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        if isinstance(body, bytes):
            data = body
        elif isinstance(body, str):
            data = body.encode()
        else:
            data = json.dumps(body).encode()
            if not any(k.lower() == "content-type" for k in hdrs):
                hdrs["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=data, headers=hdrs)
        r = conn.getresponse()
        payload = r.read()
        return Response(r.status, {k.lower(): v for k, v in r.getheaders()}, payload)
    finally:
        conn.close()


def raw_request(port: int, head: bytes, timeout: float = 10.0) -> bytes:
    """Send raw bytes and read until the server closes (a malformed request line, a lying
    Content-Length)."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(head)
        chunks = []
        while True:
            try:
                c = s.recv(65536)
            except (ConnectionResetError, socket.timeout):
                break
            if not c:
                break
            chunks.append(c)
    return b"".join(chunks)


def multipart(fields: dict[str, tuple[str, bytes]]) -> tuple[str, bytes]:
    """fields: name -> (filename, content). Returns (content type, body)."""
    boundary = "----ps" + uuid.uuid4().hex
    parts = []
    for name, (filename, content) in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n".encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


# ------------------------------------------------------------------ the server

class Server:
    """player_select_server.py running as a child process on a free port with its own data dir.

    seed: run tests._seed.seed (the fixture backup and CSV) into the database before start.
    persons: written to a temp file and passed as --persons (None: no flag).
    """

    def __init__(self, *, persons=PERSONS, nav: list[str] | None = None, extra: list[str] | None = None,
                 seed: bool = True, page: Path | None = None, timeout_start: float = 15.0):
        self._tmp = Path(tempfile.mkdtemp(prefix="ps-test-"))
        self.data = self._tmp / "data"
        self.port = free_port()
        self.persons_file = self._tmp / "persons.json"
        self.cmd = [sys.executable, str(SERVER), "--port", str(self.port), "--bind", "127.0.0.1",
                    "--data", str(self.data)]
        if persons is not None:
            self.persons_file.write_text(json.dumps(persons), encoding="utf-8")
            self.cmd += ["--persons", str(self.persons_file)]
        if page:
            self.cmd += ["--page", str(page)]
        for n in nav or []:
            self.cmd += ["--nav", n]
        self.cmd += list(extra or [])
        self.env = {**os.environ, "HOME": str(self._tmp), "PYTHONUNBUFFERED": "1"}
        self.timeout_start = timeout_start
        self.proc: subprocess.Popen | None = None
        self._log = self._tmp / "server.log"
        self._seed = seed
        self._seeded = False

    def seed_db(self) -> None:
        """Migrate and import the fixture backup, once, before the first start."""
        if self._seeded:
            return
        self._seeded = True
        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(TESTS))
        from _seed import seed
        from ps import idmaps
        from ps.db import Store, connect, migrate
        (self.data / "imports").mkdir(parents=True, exist_ok=True)
        db = self.data / "player_select.db"
        conn = connect(db, writer=True)
        migrate(conn, now=1_800_000_000)
        conn.close()
        store = Store(db)
        maps = idmaps.load()
        with store.write() as c:
            idmaps.sync_platforms(c, maps)
        seed(store, maps, self.data)
        store.close()

    # ---- lifecycle
    def start(self) -> "Server":
        if self._seed:
            self.seed_db()
        with open(self._log, "ab") as log:
            self.proc = subprocess.Popen(self.cmd, stdout=log, stderr=log, env=self.env, cwd=str(self._tmp))
        try:
            wait_for(self._healthy, self.timeout_start, what="the server to answer /healthz")
        except AssertionError:
            self.stop()
            raise AssertionError("server did not start:\n" + self.log_text())
        return self

    def _healthy(self) -> bool:
        if self.proc and self.proc.poll() is not None:
            raise AssertionError(f"server exited early ({self.proc.returncode}):\n{self.log_text()}")
        try:
            return request(self.port, "GET", "/healthz", timeout=2).status == 200
        except OSError:
            return False

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def cleanup(self) -> None:
        self.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def __enter__(self) -> "Server":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.cleanup()

    # ---- conveniences
    @property
    def tmp(self) -> Path:
        return self._tmp

    def log_text(self) -> str:
        try:
            return self._log.read_text(errors="replace")
        except OSError:
            return ""

    def req(self, method: str, path: str, body=None, headers=None, **kw) -> Response:
        return request(self.port, method, path, body, headers, **kw)

    def get(self, path: str, headers=None) -> Response:
        return self.req("GET", path, headers=headers)

    def put(self, path: str, body, headers=None) -> Response:
        return self.req("PUT", path, body, headers)

    def post(self, path: str, body=None, headers=None) -> Response:
        return self.req("POST", path, body, headers)
