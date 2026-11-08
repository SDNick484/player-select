"""The documentation matches the code: the README's API table lists every route, the README carries the
no-authentication warning, and the adapter contract's JSON example has the keys the spec (section 8) names."""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import player_select_server  # noqa: E402


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def section(text: str, heading: str) -> str:
    """The body under `## heading`, up to the next `## `."""
    m = re.search(r"^## " + re.escape(heading) + r"\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert m, f"no '## {heading}' section"
    return m.group(1)


class ReadmeTest(unittest.TestCase):
    def test_readme_lists_every_route(self):
        api = section(read("README.md"), "API")
        rows = [ln for ln in api.splitlines() if ln.startswith("|")]
        for method, path in player_select_server.ROUTES:
            with self.subTest(method=method, path=path):
                self.assertTrue(any(f"`{path}`" in r and method in r for r in rows),
                                f"{method} {path} is not in the README API table")

    def test_readme_has_security_warning(self):
        sec = section(read("README.md"), "Security").lower()
        self.assertIn("no authentication", sec)
        self.assertIn("authenticating", sec)
        self.assertIn("lan", sec)
        self.assertIn("vpn", sec)


    def test_install_installs_git_before_cloning(self):
        steps = section(read("README.md"), "Install on Alpine (OpenRC)")
        self.assertIn("git clone", steps)
        self.assertIn("apk add git", steps)
        self.assertLess(steps.index("apk add git"), steps.index("git clone"))


class AdapterContractTest(unittest.TestCase):
    def test_adapter_example_parses(self):
        text = read("adapters/README.md")
        m = re.search(r"```json\n(.*?)```", text, re.S)
        self.assertIsNotNone(m, "no ```json block")
        doc = json.loads(m.group(1))
        self.assertEqual(set(doc), {"schema", "source", "account", "fetched_at", "items"})
        self.assertEqual(doc["schema"], 1)
        self.assertTrue(doc["items"])
        for item in doc["items"]:
            self.assertEqual(set(item), {"source_id", "title", "platform", "form", "hours", "last_played"})

    def test_adapter_readme_says_no_adapter_in_v1(self):
        self.assertIn("no adapter", read("adapters/README.md").lower())


if __name__ == "__main__":
    unittest.main()
