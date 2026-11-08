#!/usr/bin/env python3
"""
player_select_server.py - serve the Player Select console catalog.

Standard library only. Serves:
  GET  /, /games, /items, /up-next, /duplicates, /import   the catalog page (a trailing slash is fine)
  GET  /api/catalog     the whole read model; ETag, honors If-None-Match (304)
  PUT  /api/play-state  {"person", "work", "status", "note"?}; honors If-Match (412)
  PUT  /api/up-next     {"person", "order": [work, ...]}
  PUT  /api/copy/<id>   {"removed_reason": "sold" | null}
  POST /api/work/merge  {"into", "from"}
  POST /api/work/split  {"copy", "title"}
  POST /api/dup-ack     {"work", "platform"}
  POST /api/import/gameeye      multipart: ged (required), csv; stages a dry run (32 MiB)
  POST /api/import/<id>/apply   {"confirm_removals"?: bool}
  GET  /healthz         "ok"

Every response carries X-PlayerSelect-Server: 1 and X-PlayerSelect-Api: 1. Every 4xx/5xx body is
{"error": "<code>", "detail": "<text>"}, the standard library's own errors included. JSON request
bodies must be application/json and at most 64 KiB.

There is no authentication. Bind it to your LAN or a VPN, or put it behind an authenticating
proxy; don't expose it directly.

Usage:
  player_select_server.py [--port 8486] [--bind 0.0.0.0] [--data DIR] [--page catalog.html]
                          [--nav LABEL=URL] ... [--persons FILE]
"""

from __future__ import annotations

import argparse
import email.parser
import email.policy
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ps import idmaps
from ps.catalog import CatalogError, ack_dup, catalog, merge, seed_persons, set_play_state, \
    set_removed_reason, set_up_next, split
from ps.db import Store, connect, migrate
from pathlib import Path

from ps import importer
from ps.importer import ImportFailure

API_VERSION = "1"
JSON_MAX = 64 * 1024
DB_NAME = "player_select.db"
PAGES = ["/", "/games", "/items", "/up-next", "/duplicates", "/import"]
ROUTES: list[tuple[str, str]] = (
    [("GET", p) for p in PAGES]
    + [("GET", "/healthz"), ("GET", "/api/catalog"), ("PUT", "/api/play-state"), ("PUT", "/api/up-next"),
       ("PUT", "/api/copy/<id>"), ("POST", "/api/work/merge"), ("POST", "/api/work/split"),
       ("POST", "/api/dup-ack"), ("POST", "/api/import/gameeye"), ("POST", "/api/import/<id>/apply")]
)
COPY_PATH = re.compile(r"^/api/copy/(\d+)$")
APPLY_PATH = re.compile(r"^/api/import/([0-9]+)/apply$", re.ASCII)
ERROR_CODES = {404: "not_found", 412: "precondition_failed", 413: "too_large",
               415: "unsupported_media_type"}

# catalog.html is a page body; wrap it in a minimal document here.
SKELETON_HEAD = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
    "<style>:root{color-scheme:light;padding-top:env(safe-area-inset-top,0px);"
    "padding-bottom:env(safe-area-inset-bottom,0px)}body{margin:0;font:14px/1.5 system-ui,"
    "-apple-system,'Segoe UI',sans-serif}img{max-width:100%}[hidden]{display:none!important}"
    "</style>{config}</head><body>"
)
SKELETON_TAIL = "</body></html>"


def atomic_write(path: str, data: bytes) -> None:
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def dump(obj) -> bytes:
    """The JSON bytes the API sends. ETags are computed over exactly these."""
    return json.dumps(obj).encode()


def etag_of(body: bytes) -> str:
    """Strong validator: the first 32 hex characters of SHA-256 over the exact bytes served."""
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


ETAG_TOKEN = re.compile(r'(?:W/)?"[^"]*"|\*')


def strip_weak(tag: str) -> str:
    return tag[2:] if tag.startswith("W/") else tag


def none_match(header: str | None, etag: str) -> bool:
    """True if an If-None-Match value matches `etag` under weak comparison (W/"x" matches "x")."""
    if not header:
        return False
    for tok in ETAG_TOKEN.findall(header):
        if tok == "*" or strip_weak(tok) == etag:
            return True
    return False


def parse_multipart(content_type: str, body: bytes) -> dict[str, bytes]:
    """Field name -> bytes for a multipart/form-data body. ValueError if it can't be parsed."""
    try:
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + content_type.encode("latin-1", "replace")
            + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
        if msg.get_content_maintype() != "multipart" or not msg.get_boundary():
            raise ValueError("expected multipart with a boundary")
        fields: dict[str, bytes] = {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not isinstance(name, str) or name in fields:
                continue
            fields[name] = part.get_payload(decode=True) or b""
        return fields
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001 - the email package has several ways to object
        raise ValueError(f"unparsable multipart body: {e}") from e


def config_script(cfg: dict) -> str:
    """Expose server settings (page links) to the page as window.APP_CONFIG."""
    js = json.dumps(cfg).replace("</", "<\\/")
    return f"<script>window.APP_CONFIG={js};</script>"


class App:
    def __init__(self, page: str, data_dir: str, nav: list[dict], store: Store, maps):
        self.page = page
        self.data_dir = os.path.abspath(data_dir)
        self.store = store
        self.maps = maps
        self.head = SKELETON_HEAD.replace("{config}", config_script({"nav": nav}))

    def page_bytes(self) -> bytes:
        with open(self.page, "rb") as f:
            return self.head.encode() + f.read() + SKELETON_TAIL.encode()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "player-select/1.0"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def send_response(self, code, message=None):
            # Here rather than in send() so every response carries them, including the standard
            # library's own error replies (bad request line, unsupported method, ...).
            super().send_response(code, message)
            self.send_header("X-PlayerSelect-Server", "1")
            self.send_header("X-PlayerSelect-Api", API_VERSION)

        def send_error(self, code, message=None, explain=None):
            self.close_connection = True
            if self.request_version == "HTTP/0.9":  # unparsable request line: the stdlib would omit headers
                self.request_version = "HTTP/1.0"
            self.error(code, ERROR_CODES.get(code, "bad_request"), message or explain or "error")

        def send(self, status: int, body: bytes = b"", ctype: str = "application/json",
                 headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            if status != 304:
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD" and status != 304:
                self.wfile.write(body)

        def json(self, status: int, obj, headers: dict[str, str] | None = None) -> None:
            self.send(status, dump(obj), headers=headers)

        def error(self, status: int, code: str, detail: str, headers: dict[str, str] | None = None) -> None:
            self.json(status, {"error": code, "detail": detail}, headers)

        def if_match_ok(self, current: str) -> bool:
            """No If-Match header: always fine. Otherwise compare with any W/ prefix removed on both
            sides; `*` matches. RFC 9110 says If-Match uses strong comparison, but a CDN (Cloudflare by
            default) turns the strong ETag into W/"..." for compressed responses and the page echoes that
            back, so strict comparison would fail every save behind it."""
            header = self.headers.get("If-Match")
            if header is None:
                return True
            return any(tok == "*" or strip_weak(tok) == strip_weak(current)
                       for tok in ETAG_TOKEN.findall(header))

        def drain(self) -> None:
            """Drop a small request body so an early error reply isn't a reset connection."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return
            if 0 < n <= JSON_MAX:
                self.rfile.read(n)

        def read_json(self) -> dict | None:
            """The request's JSON object, or None after replying with the error."""
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self.drain()
                self.error(415, "unsupported_media_type", "Content-Type must be application/json")
                return None
            raw_len = self.headers.get("Content-Length")
            if raw_len is None or not raw_len.strip().isdigit():
                self.error(400, "bad_request", "Content-Length is required")
                return None
            n = int(raw_len)
            if n > JSON_MAX:
                self.close_connection = True
                self.error(413, "too_large", f"JSON body is limited to {JSON_MAX} bytes")
                return None
            try:
                obj = json.loads(self.rfile.read(n))
            except (ValueError, RecursionError):
                self.error(400, "bad_request", "body is not valid JSON")
                return None
            if not isinstance(obj, dict):
                self.error(400, "bad_request", "body must be a JSON object")
                return None
            return obj

        def import_stage(self) -> None:
            ctype = self.headers.get("Content-Type") or ""
            if ctype.split(";")[0].strip().lower() != "multipart/form-data":
                self.close_connection = True
                return self.error(415, "unsupported_media_type", "Content-Type must be multipart/form-data")
            raw_len = self.headers.get("Content-Length")
            if raw_len is None or not (raw_len.strip().isascii() and raw_len.strip().isdigit()):
                self.close_connection = True
                return self.error(400, "bad_request", "Content-Length is required")
            n = int(raw_len)
            if n > importer.MAX_UPLOAD:
                self.close_connection = True
                return self.error(413, "too_large", f"upload is limited to {importer.MAX_UPLOAD} bytes")
            body = self.rfile.read(n)
            try:
                parts = parse_multipart(ctype, body)
            except ValueError as e:
                return self.error(400, "bad_request", str(e))
            ged = parts.get("ged")
            if not ged:
                return self.error(400, "missing_ged", "a non-empty 'ged' file part is required")
            result = importer.stage(app.store, app.maps, Path(app.data_dir) / "imports", ged,
                                    parts.get("csv") or None, now=int(time.time()))
            self.json(200, result)

        def import_apply(self, run_id: int, b: dict) -> None:
            confirm = b.get("confirm_removals", False)
            if not isinstance(confirm, bool):
                return self.error(400, "bad_request", "confirm_removals must be a boolean")
            result = importer.apply(app.store, app.maps, Path(app.data_dir) / "imports", run_id,
                                    confirm_removals=confirm, now=int(time.time()))
            self.json(200, result)

        # ---- routes
        def page(self) -> None:
            try:
                self.send(200, app.page_bytes(), "text/html; charset=utf-8")
            except OSError:
                self.error(500, "page_missing", "the catalog page file is missing")

        def catalog_get(self) -> None:
            with app.store.read() as conn:
                body = dump(catalog(conn))
            etag = etag_of(body)
            if none_match(self.headers.get("If-None-Match"), etag):
                return self.send(304, headers={"ETag": etag})
            self.send(200, body, headers={"ETag": etag})

        def play_state_put(self, b: dict) -> None:
            person, work = b.get("person"), b.get("work")
            with app.store.write() as conn:
                current = etag_of(dump(catalog(conn)))
                if not self.if_match_ok(current):
                    return self.error(412, "precondition_failed", "the catalog changed since you loaded it",
                                      {"ETag": current})
                note = b.get("note", ...)
                if note is ...:
                    row = conn.execute("SELECT note FROM play_state WHERE person_id=? AND work_id=?",
                                       (person, work)).fetchone() if isinstance(person, int) \
                        and isinstance(work, int) else None
                    note = row["note"] if row else None
                previous = set_play_state(conn, person, work, b.get("status"), note, now=int(time.time()))
                row = conn.execute("SELECT note FROM play_state WHERE person_id=? AND work_id=?",
                                   (person, work)).fetchone()
                etag = etag_of(dump(catalog(conn)))
            self.json(200, {"ok": True, "person": person, "work": work, "status": b.get("status"),
                            "note": row["note"] if row else None, "previous": previous},
                      headers={"ETag": etag})

        def mutate(self, fn, extra=None) -> None:
            with app.store.write() as conn:
                result = fn(conn)
            self.json(200, {"ok": True, **(extra(result) if extra else {})})

        def dispatch(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            p = path.rstrip("/") or "/"
            routes = {
                ("PUT", "/api/play-state"): self.play_state_put,
                ("PUT", "/api/up-next"): lambda b: self.mutate(
                    lambda c: set_up_next(c, b.get("person"), b.get("order"))),
                ("POST", "/api/work/merge"): lambda b: self.mutate(
                    lambda c: merge(c, b.get("into"), b.get("from"), now=int(time.time()))),
                ("POST", "/api/work/split"): lambda b: self.mutate(
                    lambda c: split(c, b.get("copy"), b.get("title")), lambda w: {"work": w}),
                ("POST", "/api/dup-ack"): lambda b: self.mutate(
                    lambda c: ack_dup(c, b.get("work"), b.get("platform"), now=int(time.time()))),
            }
            if (method, p) == ("POST", "/api/import/gameeye"):
                return self.import_stage()
            a = APPLY_PATH.match(p)
            if method == "POST" and a:
                routes[("POST", p)] = lambda b: self.import_apply(int(a.group(1)), b)
            m = COPY_PATH.match(p)
            if method == "PUT" and m:
                routes[("PUT", p)] = lambda b: self.copy_put(int(m.group(1)), b)
            fn = routes.get((method, p))
            if fn is None:
                self.drain()
                return self.error(404, "not_found", "no such route")
            body = self.read_json()
            if body is not None:
                fn(body)

        def copy_put(self, copy_id: int, b: dict) -> None:
            if "removed_reason" not in b:
                raise CatalogError(400, "bad_request", "removed_reason is required (null clears it)")
            self.mutate(lambda c: set_removed_reason(c, copy_id, b["removed_reason"]))

        def guarded(self, fn) -> None:
            try:
                fn()
            except (CatalogError, ImportFailure) as e:
                self.error(e.status, e.code, e.detail)
            except Exception as e:  # noqa: BLE001 - the client gets JSON, the log gets the traceback
                import traceback
                traceback.print_exc()
                self.error(500, "internal_error", type(e).__name__)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            p = path.rstrip("/") or "/"
            if p in PAGES:
                return self.guarded(self.page)
            if path == "/api/catalog":
                return self.guarded(self.catalog_get)
            if path == "/healthz":
                return self.send(200, b"ok", "text/plain")
            self.error(404, "not_found", "no such route")

        do_HEAD = do_GET

        def do_PUT(self):
            self.guarded(lambda: self.dispatch("PUT"))

        def do_POST(self):
            self.guarded(lambda: self.dispatch("POST"))

    return Handler


def parse_nav(items: list[str]) -> list[dict]:
    out = []
    for item in items:
        label, sep, url = item.partition("=")
        if not sep or not label.strip() or not url.strip():
            raise SystemExit(f"--nav expects LABEL=URL, got {item!r}")
        out.append({"label": label.strip(), "url": url.strip()})
    return out


def load_persons(path: str | None) -> list[dict]:
    """The persons file: absent is fine (logged); anything wrong with it is fatal (D-j)."""
    if not path:
        return []
    if not os.path.exists(path):
        print(f"persons file {path} not found; using the persons already in the database",
              file=sys.stderr, flush=True)
        return []
    try:
        with open(path, encoding="utf-8") as f:
            persons = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(f"error: persons file {path} is unreadable or not JSON: {e}")
    ok = isinstance(persons, list) and all(
        isinstance(p, dict) and isinstance(p.get("name"), str) and p["name"].strip()
        and isinstance(p.get("color", ""), str) for p in persons)
    if not ok:
        raise SystemExit(f"error: persons file {path} must be a JSON list of "
                         '{"name": "...", "color": "..."} objects (color optional)')
    return persons


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Serve the Player Select console catalog.")
    ap.add_argument("--port", type=int, default=8486)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--data", default=os.path.expanduser("~/data"),
                    help="Directory holding player_select.db and imports/ (default: ~/data)")
    ap.add_argument("--page", default=os.path.join(here, "catalog.html"))
    ap.add_argument("--nav", action="append", default=[], metavar="LABEL=URL",
                    help="Add a link to the page header, e.g. --nav 'Steam=https://steam.example.com/'")
    ap.add_argument("--persons", metavar="FILE", help="JSON list of {name, color} seeded at start-up")
    args = ap.parse_args()
    nav = parse_nav(args.nav)
    persons = load_persons(args.persons)
    imports = os.path.join(args.data, "imports")
    os.makedirs(imports, exist_ok=True)
    os.chmod(imports, 0o700)
    db = os.path.join(args.data, DB_NAME)
    conn = connect(db, writer=True)
    try:
        migrate(conn, now=int(time.time()))
    finally:
        conn.close()
    store = Store(db)
    maps = idmaps.load()
    with store.write() as c:
        idmaps.sync_platforms(c, maps)
        for line in seed_persons(c, persons):
            print(line, file=sys.stderr, flush=True)
    app = App(args.page, args.data, nav, store, maps)
    srv = ThreadingHTTPServer((args.bind, args.port), make_handler(app))
    print(f"Player Select on http://{args.bind}:{args.port}/ (data in {args.data})", file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.close()


if __name__ == "__main__":
    main()
