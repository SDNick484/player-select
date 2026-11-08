from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ps.db import connect  # noqa: E402
from _support import PERSONS, ROOT, Server, raw_request  # noqa: E402

PAGES = ["/", "/games", "/items", "/up-next", "/duplicates", "/import"]


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server(nav=["Other=https://other.example.com/"]).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.cleanup()

    def cat(self):
        return self.srv.get("/api/catalog").json()

    def person(self, name="Player A"):
        return next(p["id"] for p in self.cat()["persons"] if p["name"] == name)

    def work(self, title):
        return next(w["id"] for w in self.cat()["works"] if w["title"] == title)

    def assert_error(self, r, status, code):
        self.assertEqual(r.status, status, r.text())
        body = r.json()
        self.assertEqual(set(body), {"error", "detail"})
        self.assertEqual(body["error"], code)
        self.assertIsInstance(body["detail"], str)

    def put_state(self, **kw):
        return self.srv.put("/api/play-state", kw.pop("body", None) or kw, kw.pop("headers", None))


class TestBasics(ServerCase):
    def test_headers_on_every_response(self):
        def check(h):
            self.assertEqual(h.get("x-playerselect-server"), "1")
            self.assertEqual(h.get("x-playerselect-api"), "1")
        for path in ("/healthz", "/api/catalog", "/nope"):
            check(self.srv.get(path).headers)
        out = raw_request(self.srv.port, b"GET / HTTP/x.y\r\n\r\n")
        low = out.lower()
        self.assertIn(b"x-playerselect-server: 1", low)
        self.assertIn(b"x-playerselect-api: 1", low)
        self.assertIn(b'"error"', out)

    def test_unsupported_method_json(self):
        r = self.srv.req("DELETE", "/api/catalog")
        self.assert_error(r, 501, "bad_request")
        self.assertEqual(r.headers.get("x-playerselect-api"), "1")

    def test_page_routes(self):
        for p in PAGES + [x + "/" for x in PAGES[1:]]:
            r = self.srv.get(p)
            self.assertEqual(r.status, 200, p)
            self.assertTrue(r.headers["content-type"].startswith("text/html"))
            self.assertIn("window.APP_CONFIG", r.text())
            self.assertIn("Other", r.text())
            self.assertIn("Player Select", r.text())

    def test_healthz(self):
        r = self.srv.get("/healthz")
        self.assertEqual((r.status, r.text()), (200, "ok"))

    def test_unknown_route_404(self):
        self.assert_error(self.srv.get("/nope"), 404, "not_found")
        self.assert_error(self.srv.put("/api/nope", {}), 404, "not_found")

    def test_persons_seeded_at_start(self):
        self.assertEqual([p["name"] for p in self.cat()["persons"]], [p["name"] for p in PERSONS])

    def test_missing_page_500(self):
        with Server(page=ROOT / "no-such-page.html") as s:
            self.assert_error(s.get("/"), 500, "page_missing")


class TestCatalog(ServerCase):
    def test_catalog_etag_and_304(self):
        r = self.srv.get("/api/catalog")
        self.assertEqual(r.status, 200)
        etag = r.headers["etag"]
        self.assertEqual(self.srv.get("/api/catalog", {"If-None-Match": etag}).status, 304)
        self.assertEqual(self.srv.get("/api/catalog", {"If-None-Match": "W/" + etag}).status, 304)
        w = self.work("Bloodborne")
        self.assertEqual(self.put_state(person=self.person(), work=w, status="playing").status, 200)
        r2 = self.srv.get("/api/catalog", {"If-None-Match": etag})
        self.assertEqual(r2.status, 200)
        self.assertNotEqual(r2.headers["etag"], etag)
        self.put_state(person=self.person(), work=w, status="")  # restore

    def test_play_state_put(self):
        a, w = self.person("Player B"), self.work("Dead to Rights")
        r = self.put_state(person=a, work=w, status="finished", note="great")
        self.assertEqual(r.status, 200, r.text())
        b = r.json()
        self.assertEqual((b["ok"], b["person"], b["work"], b["status"], b["note"], b["previous"]),
                         (True, a, w, "finished", "great", None))
        self.assertEqual(r.headers["etag"], self.srv.get("/api/catalog").headers["etag"])
        # note absent keeps the note; null clears
        r = self.put_state(person=a, work=w, status="abandoned")
        self.assertEqual(r.json()["note"], "great")
        self.assertEqual(r.json()["previous"], {"status": "finished", "note": "great"})
        r = self.put_state(person=a, work=w, status="abandoned", note=None)
        self.assertIsNone(r.json()["note"])
        self.put_state(person=a, work=w, status="")

    def test_play_state_if_match(self):
        a, w = self.person(), self.work("Donkey Kong Bananza")
        old = self.srv.get("/api/catalog").headers["etag"]
        self.assertEqual(self.put_state(person=a, work=w, status="playing").status, 200)
        r = self.put_state(person=a, work=w, status="finished", headers={"If-Match": old})
        self.assert_error(r, 412, "precondition_failed")
        cur = self.srv.get("/api/catalog").headers["etag"]
        self.assertEqual(r.headers["etag"], cur)
        r = self.put_state(person=a, work=w, status="finished", headers={"If-Match": cur})
        self.assertEqual(r.status, 200)
        self.put_state(person=a, work=w, status="")

    def test_play_state_if_match_weak(self):
        # Cloudflare weakens a strong ETag for compressed responses (W/"..."), and the page echoes it back:
        # If-Match compares with the W/ prefix removed on both sides (a deliberate RFC 9110 deviation).
        a, w = self.person(), self.work("Donkey Kong Bananza")
        old = self.srv.get("/api/catalog").headers["etag"]
        self.assertEqual(self.put_state(person=a, work=w, status="playing").status, 200)
        r = self.put_state(person=a, work=w, status="finished", headers={"If-Match": "W/" + old})
        self.assert_error(r, 412, "precondition_failed")
        cur = self.srv.get("/api/catalog").headers["etag"]
        r = self.put_state(person=a, work=w, status="finished", headers={"If-Match": "W/" + cur})
        self.assertEqual(r.status, 200, r.text())
        cur = r.headers["etag"]
        r = self.put_state(person=a, work=w, status="backlog", headers={"If-Match": f'"zzz", W/{cur}'})
        self.assertEqual(r.status, 200, r.text())
        r = self.put_state(person=a, work=w, status="playing", headers={"If-Match": "*"})
        self.assertEqual(r.status, 200, r.text())
        self.put_state(person=a, work=w, status="")

    def test_unknown_person_404(self):
        self.assert_error(self.put_state(person=9999, work=self.work("Bloodborne"), status="playing"),
                          404, "not_found")

    def test_play_state_bad_status(self):
        self.assert_error(self.put_state(person=self.person(), work=self.work("Bloodborne"), status="zzz"),
                          400, "bad_request")


class TestBodies(ServerCase):
    def test_json_content_type_required(self):
        body = b'{"person": 1, "work": 1, "status": "playing"}'
        for method, path in (("PUT", "/api/play-state"), ("POST", "/api/dup-ack"), ("PUT", "/api/up-next")):
            r = self.srv.req(method, path, body, {"Content-Type": "text/plain"})
            self.assert_error(r, 415, "unsupported_media_type")
            r = self.srv.req(method, path, body)
            self.assert_error(r, 415, "unsupported_media_type")
        r = self.srv.req("PUT", "/api/play-state", body, {"Content-Type": "application/json; charset=utf-8"})
        self.assertNotEqual(r.status, 415)

    def test_bad_json_400(self):
        h = {"Content-Type": "application/json"}
        for body in (b"{", b"[1]", b'"x"', b"null", b"\xff\xfe"):
            self.assert_error(self.srv.req("PUT", "/api/play-state", body, h), 400, "bad_request")

    def test_content_length_missing_or_invalid(self):
        head = (b"PUT /api/play-state HTTP/1.0\r\nContent-Type: application/json\r\n%s\r\n")
        out = raw_request(self.srv.port, head % b"")
        self.assertIn(b" 400 ", out.split(b"\r\n")[0])
        out = raw_request(self.srv.port, head % b"Content-Length: abc\r\n")
        self.assertIn(b" 400 ", out.split(b"\r\n")[0])
        out = raw_request(self.srv.port, head % b"Content-Length: -5\r\n")
        self.assertIn(b" 400 ", out.split(b"\r\n")[0])
        # U+00B2 SUPERSCRIPT TWO: str.isdigit() says yes, int() says no (latin-1, as http.server decodes it)
        out = raw_request(self.srv.port, head % "Content-Length: ²\r\n".encode("latin-1"))
        self.assertIn(b" 400 ", out.split(b"\r\n")[0])
        self.assertIn(b'"bad_request"', out)

    def test_ids_beyond_int64(self):
        big, neg = 2 ** 63, -(2 ** 63) - 1
        cases = [("PUT", "/api/play-state", {"person": big, "work": 1, "status": "playing"}),
                 ("PUT", "/api/play-state", {"person": 1, "work": neg, "status": "playing"}),
                 ("PUT", "/api/up-next", {"person": big, "order": []}),
                 ("PUT", "/api/up-next", {"person": 1, "order": [big]}),
                 ("POST", "/api/work/merge", {"into": big, "from": 1}),
                 ("POST", "/api/work/split", {"copy": big, "title": "x"}),
                 ("POST", "/api/dup-ack", {"work": 1, "platform": big})]
        for method, path, body in cases:
            with self.subTest(path=path, body=body):
                self.assert_error(self.srv.req(method, path, body), 400, "bad_request")
        for path in (f"/api/copy/{big}", "/api/copy/" + "9" * 40):
            with self.subTest(path=path):
                self.assert_error(self.srv.put(path, {"removed_reason": "sold"}), 404, "not_found")
        self.assert_error(self.srv.post(f"/api/import/{big}/apply", {}), 404, "not_found")
        self.assertNotIn("Traceback", self.srv.log_text())

    def test_too_large_413(self):
        big = json.dumps({"note": "x" * (64 * 1024)}).encode()
        r = self.srv.req("PUT", "/api/play-state", big, {"Content-Type": "application/json"})
        self.assert_error(r, 413, "too_large")
        out = raw_request(self.srv.port, b"PUT /api/play-state HTTP/1.0\r\nContent-Type: application/json\r\n"
                          b"Content-Length: 99999999\r\n\r\n")
        self.assertIn(b" 413 ", out.split(b"\r\n")[0])

    def test_error_shape(self):
        h = {"Content-Type": "application/json"}
        cases = [
            self.srv.get("/nope"),
            self.srv.req("PUT", "/api/play-state", b"{", h),
            self.srv.req("PUT", "/api/play-state", b"{}", {"Content-Type": "text/plain"}),
            self.srv.put("/api/play-state", {"person": 1}),
            self.srv.put("/api/copy/abc", {}),
            self.srv.req("DELETE", "/api/catalog"),
        ]
        for r in cases:
            self.assertGreaterEqual(r.status, 400)
            self.assertEqual(set(r.json()), {"error", "detail"}, r.text())


class TestEdits(unittest.TestCase):
    def setUp(self):
        self.srv = Server().start()
        c = self.srv.get("/api/catalog").json()
        self.c = c
        self.a = next(p["id"] for p in c["persons"] if p["name"] == "Player A")
        self.w = {w["title"]: w["id"] for w in c["works"]}

    def tearDown(self):
        self.srv.cleanup()

    def err(self, r, status, code):
        self.assertEqual(r.status, status, r.text())
        self.assertEqual(r.json()["error"], code)

    def test_up_next(self):
        w1, w2 = self.w["Bloodborne"], self.w["Dead to Rights"]
        for w in (w1, w2):
            self.srv.put("/api/play-state", {"person": self.a, "work": w, "status": "up_next"})
        r = self.srv.put("/api/up-next", {"person": self.a, "order": [w2, w1]})
        self.assertEqual(r.status, 200, r.text())
        self.assertTrue(r.json()["ok"])
        self.assertEqual(self.srv.get("/api/catalog").json()["up_next"], {str(self.a): [w2, w1]})
        r = self.srv.put("/api/up-next", {"person": self.a, "order": [w1]})
        self.assertEqual(r.status, 409, r.text())
        self.assertEqual(set(r.json()), {"error", "detail"})

    def test_copy_removed_reason(self):
        self.err(self.srv.put("/api/copy/1", {"removed_reason": "sold"}), 409, "not_removed")
        self.err(self.srv.put("/api/copy/9999", {"removed_reason": "sold"}), 404, "not_found")
        self.err(self.srv.put("/api/copy/x", {"removed_reason": "sold"}), 404, "not_found")

    def test_copy_removed_reason_happy_path(self):
        self.srv.cleanup()
        self.srv = Server()
        self.srv.seed_db()
        db = connect(self.srv.data / "player_select.db", writer=True)
        db.execute("UPDATE copy SET removed_at=1800000100 WHERE id=1")
        db.close()
        self.srv.start()

        def reason():
            return next(x for x in self.srv.get("/api/catalog").json()["copies"] if x["id"] == 1)["removed_reason"]
        r = self.srv.put("/api/copy/1", {"removed_reason": "sold"})
        self.assertEqual((r.status, r.json()), (200, {"ok": True}))
        self.assertEqual(reason(), "sold")
        r = self.srv.put("/api/copy/1", {"removed_reason": None})
        self.assertEqual((r.status, r.json()), (200, {"ok": True}))
        self.assertIsNone(reason())

    def test_merge(self):
        r = self.srv.post("/api/work/merge", {"into": self.w["Bloodborne"], "from": self.w["Dead to Rights"]})
        self.assertEqual(r.status, 200, r.text())
        self.assertTrue(r.json()["ok"])
        self.assertNotIn("Dead to Rights", [w["title"] for w in self.srv.get("/api/catalog").json()["works"]])
        self.err(self.srv.post("/api/work/merge", {"into": 1, "from": 9999}), 404, "not_found")
        self.err(self.srv.post("/api/work/merge", {"into": "a", "from": 2}), 400, "bad_request")

    def test_split(self):
        r = self.srv.post("/api/work/split", {"copy": 1, "title": "Bloodborne Special"})
        self.assertEqual(r.status, 200, r.text())
        wid = r.json()["work"]
        copy = next(x for x in self.srv.get("/api/catalog").json()["copies"] if x["id"] == 1)
        self.assertEqual(copy["work_id"], wid)
        self.err(self.srv.post("/api/work/split", {"copy": 9999, "title": "x"}), 404, "not_found")

    def test_dup_ack(self):
        g = self.c["dup_groups"][0]
        r = self.srv.post("/api/dup-ack", {"work": g["work"], "platform": g["platform"]})
        self.assertEqual(r.status, 200, r.text())
        self.assertEqual(len(self.srv.get("/api/catalog").json()["dup_groups"]), len(self.c["dup_groups"]) - 1)
        r = self.srv.post("/api/dup-ack", {"work": self.w["Donkey Kong Bananza"], "platform": g["platform"]})
        self.err(r, 409, "not_a_duplicate")


class TestLifecycle(unittest.TestCase):
    def test_bad_persons_file_exits(self):
        s = Server()
        try:
            s.persons_file.write_text("{")
            with self.assertRaises(AssertionError):
                s.start()
            self.assertIn(str(s.persons_file), s.log_text())
        finally:
            s.cleanup()

    def test_bad_persons_shapes_exit(self):
        for content in ('{"name": "x"}', '[1]', '[{"name": ""}]', '[{"name": "a", "color": 3}]', '[{}]'):
            s = Server()
            try:
                s.persons_file.write_text(content)
                with self.assertRaises(AssertionError, msg=content):
                    s.start()
                self.assertIn(str(s.persons_file), s.log_text())
            finally:
                s.cleanup()

    def test_missing_persons_file_continues(self):
        s = Server(persons=None, extra=["--persons", "/nonexistent/persons.json"])
        try:
            s.start()
            self.assertEqual(s.get("/api/catalog").status, 200)
        finally:
            s.cleanup()

    def test_persons_file_edit_applies_on_restart(self):
        s = Server(persons=[{"name": "Player 1", "color": "#4c78a8"}]).start()
        try:
            self.assertEqual([p["name"] for p in s.get("/api/catalog").json()["persons"]], ["Player 1"])
            s.stop()
            s.persons_file.write_text(json.dumps(PERSONS), encoding="utf-8")
            s.start()
            got = [(p["name"], p["color"]) for p in s.get("/api/catalog").json()["persons"]]
            self.assertEqual(got, [(p["name"], p["color"]) for p in PERSONS])
            self.assertIn("persons: removed 'Player 1'", s.log_text())
        finally:
            s.cleanup()

    def test_data_dir_modes(self):
        with Server() as s:
            self.assertEqual((s.data / "imports").stat().st_mode & 0o777, 0o700)

    def test_restart_is_idempotent(self):
        s = Server().start()
        try:
            before = s.get("/api/catalog").body
            s.stop()
            s.start()
            self.assertEqual(s.get("/api/catalog").body, before)
        finally:
            s.cleanup()


class TestRoutes(unittest.TestCase):
    def test_routes_constant(self):
        import player_select_server as ps
        for p in PAGES:
            self.assertIn(("GET", p), ps.ROUTES)
        for r in (("GET", "/api/catalog"), ("PUT", "/api/play-state"), ("PUT", "/api/up-next"),
                  ("PUT", "/api/copy/<id>"), ("POST", "/api/work/merge"), ("POST", "/api/work/split"),
                  ("POST", "/api/dup-ack"), ("GET", "/healthz")):
            self.assertIn(r, ps.ROUTES)
        self.assertEqual(ps.API_VERSION, "1")


if __name__ == "__main__":
    unittest.main()
