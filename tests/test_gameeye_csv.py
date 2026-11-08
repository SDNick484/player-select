from __future__ import annotations

import datetime
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ged import FIXTURE_ROWS, build_csv, csv_row, fixture_csv_rows  # noqa: E402
from ps.gameeye_csv import (CSV_COLUMNS, CopyKey, CsvError, match, meta_fields,  # noqa: E402
                            parse_csv)
from ps.idmaps import condition_label, load  # noqa: E402

MAPS = load()
PS4 = MAPS.platforms[46]


def key(n: int, **kw) -> CopyKey:
    r = FIXTURE_ROWS[n - 1]
    base = dict(source_id=r["uuid"], platform=MAPS.platforms[r["platform_id"]],
                raw_title=r["title"], created_at=r["created_at"],
                condition=condition_label(r["ownership_mask"]))
    base.update(kw)
    return CopyKey(**base)


def ts(y, mo, d, h=12, mi=0) -> int:
    return int(datetime.datetime(y, mo, d, h, mi, tzinfo=datetime.timezone.utc).timestamp())


class ParseTest(unittest.TestCase):
    def one(self, **kw):
        rows = parse_csv(build_csv([csv_row(**kw)]))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_columns(self):
        self.assertEqual(len(CSV_COLUMNS), 24)
        self.assertEqual(CSV_COLUMNS[0], "Platform")
        self.assertEqual(CSV_COLUMNS[-1], "metacritic")

    def test_25_fields_with_trailing_comma(self):
        rows = parse_csv(build_csv(fixture_csv_rows(), trailing_comma=True))
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0].title, "Bloodborne")

    def test_last_line_24_fields(self):
        rows = parse_csv(build_csv(fixture_csv_rows(), last_line_trailing=False))
        self.assertEqual(len(rows), 6)

    def test_no_trailing_commas_at_all(self):
        self.assertEqual(len(parse_csv(build_csv(fixture_csv_rows(), trailing_comma=False))), 6)

    def test_header_with_trailing_empty_field_accepted(self):
        data = build_csv([csv_row()]).replace(b"metacritic\r\n", b"metacritic,\r\n", 1)
        self.assertEqual(len(parse_csv(data)), 1)

    def test_bom_accepted(self):
        self.assertEqual(len(parse_csv(b"\xef\xbb\xbf" + build_csv([csv_row()]))), 1)

    def test_wrong_field_count_raises(self):
        lines = build_csv([csv_row()]).decode().split("\r\n")
        lines[1] = ",".join(lines[1].split(",")[:23])
        with self.assertRaises(CsvError):
            parse_csv("\r\n".join(lines).encode())

    def test_25th_field_must_be_empty(self):
        with self.assertRaises(CsvError):
            parse_csv(build_csv([csv_row()]).rstrip(b"\r\n") + b'"x"\r\n')

    def test_header_mismatch_raises(self):
        data = build_csv([csv_row()]).replace(b"Publisher", b"Publisherr", 1)
        with self.assertRaises(CsvError):
            parse_csv(data)

    def test_empty_raises(self):
        with self.assertRaises(CsvError):
            parse_csv(b"")

    def test_not_utf8_raises(self):
        with self.assertRaises(CsvError):
            parse_csv(b"\xff\xfe\x00")

    def test_sentinels(self):
        r = self.one(Publisher="?", Developer="Missing Field", metacritic="-1.0",
                     PriceLoose="0.0", PriceCIB="", PriceNew="-1.0", Genre="?", Country="")
        self.assertIsNone(r.publisher)
        self.assertIsNone(r.developer)
        self.assertIsNone(r.metacritic)
        self.assertIsNone(r.price_loose)
        self.assertIsNone(r.price_cib)
        self.assertIsNone(r.price_new)
        self.assertIsNone(r.genre)
        self.assertIsNone(r.country)

    def test_leading_space_quoted(self):
        rows = parse_csv(build_csv([csv_row(Publisher="Sega", PriceNew="-1.0")],
                                   leading_space=True))
        self.assertEqual(rows[0].publisher, "Sega")
        self.assertIsNone(rows[0].price_new)

    def test_spaces_inside_quotes_stripped(self):
        self.assertEqual(self.one(Publisher="  Sega  ").publisher, "Sega")

    def test_created_date(self):
        self.assertEqual(self.one(CreatedAt="Nov 8, 2026").created_date, datetime.date(2026, 11, 8))

    def test_bad_date_raises(self):
        with self.assertRaises(CsvError):
            parse_csv(build_csv([csv_row(CreatedAt="yesterday")]))

    def test_missing_required_raises(self):
        with self.assertRaises(CsvError):
            parse_csv(build_csv([csv_row(Title="")]))

    def test_genre_split(self):
        self.assertEqual(self.one(Genre="Action, Adventure").genre, ("Action", "Adventure"))

    def test_numbers(self):
        r = self.one(metacritic="87", PriceCIB="24.99")
        self.assertEqual(r.metacritic, 87)
        self.assertEqual(r.price_cib, 24.99)

    def test_thousands_separator(self):
        self.assertEqual(self.one(PriceNew="1,021.1").price_new, 1021.1)

    def test_bad_number_raises(self):
        with self.assertRaises(CsvError):
            parse_csv(build_csv([csv_row(PriceCIB="lots")]))

    def test_meta_fields(self):
        r = self.one(Genre="Action, Adventure", metacritic="87", PriceCIB="24.99",
                     Publisher="?", PriceLoose="0.0")
        self.assertEqual(meta_fields(r), {"genre": '["Action", "Adventure"]',
                                          "metacritic": "87", "price_cib": "24.99"})
        self.assertEqual(json.loads(meta_fields(r)["genre"]), ["Action", "Adventure"])

    def test_meta_fields_all(self):
        r = self.one(Publisher="P", Developer="D", PriceLoose="1", PriceNew="3.5")
        m = meta_fields(r)
        self.assertEqual((m["publisher"], m["developer"], m["price_loose"], m["price_new"]),
                         ("P", "D", "1.00", "3.50"))


class MatchTest(unittest.TestCase):
    def rows(self):
        return parse_csv(build_csv(fixture_csv_rows()))

    def test_match_unique(self):
        r = match([key(5)], self.rows(), MAPS)
        self.assertEqual((r.enriched, r.ambiguous), (1, 0))
        self.assertEqual(r.meta[key(5).source_id]["publisher"], "Test Publisher C")
        self.assertEqual(r.meta[key(5).source_id]["metacritic"], "88")

    def test_unmatched_counts_unrelated_rows(self):
        r = match([key(5)], self.rows(), MAPS)
        self.assertEqual(r.unmatched, 5)

    def test_match_narrows_by_ownership(self):
        a, b = key(1, created_at=ts(2026, 3, 1)), key(2, created_at=ts(2026, 3, 1, 20))
        rows = [csv_row(Platform=MAPS.platforms[46], Title="Bloodborne", Ownership="Boxed",
                        CreatedAt="Mar 1, 2026", PriceCIB="1.00"),
                csv_row(Platform=MAPS.platforms[46], Title="Bloodborne", Ownership="CIB",
                        CreatedAt="Mar 1, 2026", PriceCIB="2.00")]
        r = match([a, b], parse_csv(build_csv(rows)), MAPS)
        self.assertEqual((r.enriched, r.ambiguous, r.unmatched), (2, 0, 0))
        self.assertEqual(r.meta[a.source_id]["price_cib"], "1.00")
        self.assertEqual(r.meta[b.source_id]["price_cib"], "2.00")

    def test_match_same_day_same_condition_ambiguous(self):
        r = match([key(3), key(4)], self.rows(), MAPS)
        self.assertEqual((r.enriched, r.ambiguous), (0, 2))
        self.assertEqual(r.meta, {})
        self.assertEqual(r.unmatched, 4)

    def test_ambiguous_candidates_are_not_unmatched(self):
        r = match([key(3), key(4)], [x for x in self.rows() if x.title == "Dead to Rights"], MAPS)
        self.assertEqual((r.enriched, r.ambiguous, r.unmatched), (0, 2, 0))

    def test_two_copies_one_row_both_ambiguous(self):
        a, b = key(3), key(4)
        rows = [x for x in self.rows() if x.title == "Dead to Rights"][:1]
        r = match([a, b], rows, MAPS)
        self.assertEqual((r.enriched, r.ambiguous, r.unmatched), (0, 2, 0))

    def test_match_ignores_country(self):
        rows = parse_csv(build_csv([csv_row(Platform=PS4, Title="Bloodborne", Country="World",
                                            CreatedAt="Nov 14, 2023", Ownership="Boxed")]))
        r = match([key(1)], rows, MAPS)
        self.assertEqual(r.enriched, 1)

    def test_match_utc_day(self):
        c = key(1, created_at=ts(2026, 11, 9, 7, 30))
        for text, want in (("Nov 9, 2026", 1), ("Nov 8, 2026", 0)):
            rows = parse_csv(build_csv([csv_row(Platform=PS4, Title="Bloodborne",
                                                CreatedAt=text, Ownership="Boxed")]))
            self.assertEqual(match([c], rows, MAPS).enriched, want, text)

    def test_match_title_exact_with_brackets(self):
        t = "Zelda Test [Foil Cover Art]"
        a = key(1, source_id="a", raw_title=t, created_at=ts(2026, 1, 5))
        b = key(2, source_id="b", raw_title="Zelda Test", created_at=ts(2026, 1, 5))
        rows = parse_csv(build_csv([csv_row(Platform=PS4, Title=t, CreatedAt="Jan 5, 2026",
                                            Ownership="Boxed", Publisher="X")]))
        r = match([a, b], rows, MAPS)
        self.assertEqual(set(r.meta), {"a"})
        self.assertEqual((r.enriched, r.ambiguous, r.unmatched), (1, 0, 0))

    def test_unknown_platform_label_unmatched(self):
        rows = parse_csv(build_csv([csv_row(Platform="Imaginary Console", Title="Bloodborne",
                                            CreatedAt="Nov 14, 2023", Ownership="Boxed")]))
        c = key(1, platform="Imaginary Console")
        r = match([c], rows, MAPS)
        self.assertEqual((r.enriched, r.unmatched), (0, 1))

    def test_unknown_category_label_unmatched(self):
        rows = parse_csv(build_csv([csv_row(Platform=PS4, Category="Mystery", Title="Bloodborne",
                                            CreatedAt="Nov 14, 2023", Ownership="Boxed")]))
        r = match([key(1)], rows, MAPS)
        self.assertEqual((r.enriched, r.unmatched), (0, 1))

    def test_copy_without_created_at_gets_nothing(self):
        r = match([key(1, created_at=None)], self.rows(), MAPS)
        self.assertEqual((r.enriched, r.ambiguous), (0, 0))

    def test_fixture_end_to_end(self):
        copies = [key(n, created_at=FIXTURE_ROWS[n - 1]["created_at"]) for n in range(1, 7)]
        r = match(copies, self.rows(), MAPS)
        self.assertEqual((r.enriched, r.ambiguous, r.unmatched), (4, 2, 0))
        self.assertNotEqual(r.meta[key(1).source_id]["price_cib"],
                            r.meta[key(2).source_id]["price_cib"])


if __name__ == "__main__":
    unittest.main()
