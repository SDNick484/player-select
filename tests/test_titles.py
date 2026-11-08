from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ps.titles import display_title, normalize  # noqa: E402

VECTORS = json.loads((Path(__file__).resolve().parent / "fixtures" / "titles.json")
                     .read_text(encoding="utf-8"))


class TitlesTest(unittest.TestCase):
    def test_vectors(self):
        for v in VECTORS:
            with self.subTest(raw=v["raw"]):
                self.assertEqual(normalize(v["raw"]), (v["key"], v["edition"]))

    def test_spec_merges_present(self):
        by_raw = {v["raw"]: v for v in VECTORS}
        pairs = [("Witcher III: Wild Hunt", "Witcher III: Wild Hunt [Complete Edition]"),
                 ("The Elder Scrolls V: Skyrim", "Elder Scrolls V: Skyrim"),
                 ("Legend of Zelda: Ocarina of Time",
                  "Legend of Zelda: Ocarina of Time [Foil Cover Art]")]
        for a, b in pairs:
            with self.subTest(pair=a):
                self.assertIn(a, by_raw)
                self.assertIn(b, by_raw)
                self.assertEqual(by_raw[a]["key"], by_raw[b]["key"])
                self.assertEqual(normalize(a)[0], normalize(b)[0])

    def test_display_title(self):
        self.assertEqual(display_title("Witcher III: Wild Hunt [Complete Edition]"),
                         "Witcher III: Wild Hunt")
        self.assertEqual(display_title("Halo 2 [Limited]  [Steelbook]"), "Halo 2")
        self.assertEqual(display_title("  Spaced   Out  "), "Spaced Out")


if __name__ == "__main__":
    unittest.main()
