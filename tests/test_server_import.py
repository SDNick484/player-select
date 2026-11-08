from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ged import FIXTURE_ROWS, build_csv, build_ged, fixture_csv_rows  # noqa: E402
from _support import Server, multipart, raw_request  # noqa: E402
from player_select_server import parse_multipart  # noqa: E402


class ParseMultipartTest(unittest.TestCase):
    def test_parse_multipart_binary_exact(self):
        payload = b"\r\n--x\r\n\x00\xff\r\n\r\n"
        ctype, body = multipart({"ged": ("a.ged", payload), "csv": ("b.csv", b"")})
        parts = parse_multipart(ctype, body)
        self.assertEqual(parts["ged"], payload)
        self.assertEqual(parts["csv"], b"")

    def test_parse_multipart_no_boundary(self):
        with self.assertRaises(ValueError):
            parse_multipart("multipart/form-data", b"--x\r\n\r\nhi\r\n--x--\r\n")

    def test_parse_multipart_not_multipart(self):
        with self.assertRaises(ValueError):
            parse_multipart("application/json", b"{}")

    def test_repeated_name_keeps_first(self):
        b = "XX"
        body = (f'--{b}\r\nContent-Disposition: form-data; name="a"\r\n\r\none\r\n'
                f'--{b}\r\nContent-Disposition: form-data; name="a"\r\n\r\ntwo\r\n--{b}--\r\n').encode()
        self.assertEqual(parse_multipart(f"multipart/form-data; boundary={b}", body), {"a": b"one"})


class FreshImportTest(unittest.TestCase):
    """An empty database, so the first upload is a real import."""

    def setUp(self):
        self.srv = Server(seed=False).start()
        self.addCleanup(self.srv.cleanup)

    def upload(self, fields):
        ctype, body = multipart(fields)
        return self.srv.post("/api/import/gameeye", body, {"Content-Type": ctype})

    def ged(self):
        return build_ged(FIXTURE_ROWS)

    def test_stage_and_apply(self):
        r = self.upload({"ged": ("x.ged", self.ged()), "csv": ("x.csv", build_csv(fixture_csv_rows()))})
        self.assertEqual(r.status, 200, r.text())
        staged = r.json()
        self.assertTrue(staged["dry_run"])
        self.assertIsInstance(staged["import_id"], int)
        self.assertEqual(self.srv.get("/api/catalog").json()["copies"], [])
        a = self.srv.post(f"/api/import/{staged['import_id']}/apply", {})
        self.assertEqual(a.status, 200, a.text())
        self.assertEqual(a.json()["import_id"], staged["import_id"])
        self.assertEqual(a.json()["summary"]["added"], 7)
        self.assertEqual(len(self.srv.get("/api/catalog").json()["copies"]), 7)

    def test_unchanged_has_no_import_id(self):
        staged = self.upload({"ged": ("x.ged", self.ged())}).json()
        self.srv.post(f"/api/import/{staged['import_id']}/apply", {})
        again = self.upload({"ged": ("x.ged", self.ged())})
        self.assertEqual(again.status, 200, again.text())
        self.assertIsNone(again.json()["import_id"])

    def test_stage_without_csv(self):
        r = self.upload({"ged": ("x.ged", self.ged())})
        self.assertEqual(r.status, 200, r.text())
        self.assertIsInstance(r.json()["import_id"], int)

    def test_empty_csv_is_absent(self):
        r = self.upload({"ged": ("x.ged", self.ged()), "csv": ("x.csv", b"")})
        self.assertEqual(r.status, 200, r.text())

    def test_missing_ged_400(self):
        for fields in ({"csv": ("x.csv", b"a")}, {"ged": ("x.ged", b"")}):
            r = self.upload(fields)
            self.assertEqual((r.status, r.json()["error"]), (400, "missing_ged"))

    def test_bad_upload_is_importer_error(self):
        r = self.upload({"ged": ("x.ged", b"not a zip")})
        self.assertEqual(r.status, 400, r.text())
        self.assertEqual(r.json()["error"], "not_zip")

    def test_broken_multipart_400(self):
        r = self.srv.post("/api/import/gameeye", b"garbage", {"Content-Type": "multipart/form-data"})
        self.assertEqual((r.status, r.json()["error"]), (400, "bad_request"))

    def test_not_multipart_415(self):
        r = self.srv.post("/api/import/gameeye", {"ged": "x"})
        self.assertEqual((r.status, r.json()["error"]), (415, "unsupported_media_type"))

    def test_oversize_413(self):
        ctype, _ = multipart({"ged": ("x", b"")})
        head = (f"POST /api/import/gameeye HTTP/1.1\r\nHost: x\r\nContent-Type: {ctype}\r\n"
                "Content-Length: 33554433\r\n\r\n").encode()
        raw = raw_request(self.srv.port, head)
        self.assertTrue(raw.startswith(b"HTTP/1.0 413") or raw.startswith(b"HTTP/1.1 413"), raw[:80])
        self.assertIn(b'"too_large"', raw)

    def test_missing_or_bad_length_400(self):
        ctype, _ = multipart({"ged": ("x", b"")})
        for cl in (None, "abc", "-5"):
            lines = f"POST /api/import/gameeye HTTP/1.1\r\nHost: x\r\nContent-Type: {ctype}\r\n"
            if cl is not None:
                lines += f"Content-Length: {cl}\r\n"
            raw = raw_request(self.srv.port, (lines + "\r\n").encode())
            self.assertIn(b" 400 ", raw.split(b"\r\n")[0], cl)
            self.assertIn(b'"bad_request"', raw)


class ApplyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server().start()  # seeded with the fixture backup

    @classmethod
    def tearDownClass(cls):
        cls.srv.cleanup()

    def stage(self, rows):
        ctype, body = multipart({"ged": ("x.ged", build_ged(rows))})
        r = self.srv.post("/api/import/gameeye", body, {"Content-Type": ctype})
        self.assertEqual(r.status, 200, r.text())
        return r.json()

    def err(self, r, status, code):
        self.assertEqual((r.status, r.json()["error"]), (status, code), r.text())

    def test_apply_requires_json(self):
        r = self.srv.post("/api/import/1/apply", b"x", {"Content-Type": "text/plain"})
        self.err(r, 415, "unsupported_media_type")

    def test_apply_bad_id_404(self):
        self.err(self.srv.post("/api/import/abc/apply", {}), 404, "not_found")
        self.err(self.srv.post("/api/import/-1/apply", {}), 404, "not_found")

    def test_apply_unknown_404(self):
        self.err(self.srv.post("/api/import/99999/apply", {}), 404, "not_found")

    def test_confirm_removals_must_be_bool(self):
        self.err(self.srv.post("/api/import/1/apply", {"confirm_removals": "yes"}), 400, "bad_request")

    def test_apply_errors_pass_through(self):
        staged = self.stage(FIXTURE_ROWS[:2])
        self.assertTrue(staged["removal_guard"])
        path = f"/api/import/{staged['import_id']}/apply"
        self.err(self.srv.post(path, {}), 428, "confirm_removals")
        self.err(self.srv.post(path, {"confirm_removals": False}), 428, "confirm_removals")
        ok = self.srv.post(path, {"confirm_removals": True})
        self.assertEqual(ok.status, 200, ok.text())
        self.err(self.srv.post(path, {}), 409, "not_staged")


if __name__ == "__main__":
    unittest.main()
