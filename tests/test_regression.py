"""Regression tests on the invented Kindle (tests/synthetic_kindle.py).

The golden file holds the full expected output of extract and parse. Any
change in behaviour shows up as a difference. When a change is intended,
regenerate it and review the diff before committing:

    set KEAT_UPDATE_GOLDEN=1
    python -m unittest tests.test_regression
"""

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kindle_acquire.compare import compare, describe  # noqa: E402
from kindle_acquire.extract import extract_image  # noqa: E402
from kindle_acquire.image import LOST_SOURCE_BYTES, acquire_image  # noqa: E402
from kindle_acquire.logical import acquire_logical  # noqa: E402
from kindle_acquire.report import parse_acquisition  # noqa: E402
from kindle_acquire.survey import survey  # noqa: E402
from kindle_acquire.verify import verify  # noqa: E402
import synthetic_kindle  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden" / "synthetic_kindle.json"
# SQLite writes its own version into a database file, so the bytes of
# vocab.db differ between Python builds. Its hash and size are left out.
VARIABLE_FILES = ("system/vocabulary/vocab.db",)
# Rows of device_summary that change with the run, the version or vocab.db.
VARIABLE_ITEMS = ("started (UTC)", "finished (UTC)", "tool version",
                  "parsed with tool version", "manifest SHA-256", "source image SHA-256")


def snapshot(out):
    """Everything extract and parse produced, in a comparable form."""
    with open(out / "manifest.csv", newline="", encoding="utf-8") as fh:
        manifest = []
        for row in csv.DictReader(fh):
            if row["rel_path"] in VARIABLE_FILES:
                row["sha256"] = row["md5"] = row["size"] = "<varies>"
            manifest.append(row)
    with open(out / "deleted_entries.csv", newline="", encoding="utf-8") as fh:
        deleted = list(csv.DictReader(fh))
    tables = {}
    con = sqlite3.connect(str(out / "parsed" / "kindle_parsed.db"))
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        cur = con.execute('SELECT * FROM "%s"' % name)
        columns = [d[0] for d in cur.description]
        rows = [list(r) for r in cur]
        for row in rows:
            record = dict(zip(columns, row))
            if any(record.get(k) in VARIABLE_FILES for k in ("rel_path", "sidecar_file")):
                for key in ("sha256", "md5", "size"):
                    if key in record:
                        row[columns.index(key)] = "<varies>"
            if name == "device_summary" and record["item"] in VARIABLE_ITEMS:
                row[columns.index("value")] = "<varies>"
        tables[name] = {"columns": columns, "rows": rows}
    con.close()
    return {"manifest": manifest, "deleted_entries": deleted, "tables": tables}


class SyntheticKindle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.image = synthetic_kindle.build_image(cls.tmp)
        cls.out = cls.tmp / "extract"
        cls.log = extract_image(str(cls.image), cls.out, quiet=True)
        cls.summary = parse_acquisition(cls.out)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_image_is_reproducible(self):
        # The builder must give the same bytes each time, apart from vocab.db.
        again = synthetic_kindle.build_filesystem(self.tmp)
        first = synthetic_kindle.build_filesystem(self.tmp)
        self.assertEqual(len(again.build()), len(first.build()))

    def test_run_is_clean(self):
        self.assertEqual(self.log["errors"], [])
        self.assertEqual(self.summary["parse_errors"], 0)
        self.assertTrue(verify(self.out)["ok"])

    def test_matches_golden_output(self):
        actual = json.loads(json.dumps(snapshot(self.out)))   # tuples to lists
        if os.environ.get("KEAT_UPDATE_GOLDEN"):
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(json.dumps(actual, indent=1, ensure_ascii=False) + "\n",
                              encoding="utf-8", newline="\n")
            self.skipTest("golden file rewritten")
        self.assertTrue(GOLDEN.is_file(), "no golden file: set KEAT_UPDATE_GOLDEN=1 to create it")
        expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(sorted(actual["tables"]), sorted(expected["tables"]))
        for name in expected["tables"]:
            self.assertEqual(actual["tables"][name]["columns"],
                             expected["tables"][name]["columns"], name)
            self.assertEqual(actual["tables"][name]["rows"],
                             expected["tables"][name]["rows"], name)
        self.assertEqual(actual["manifest"], expected["manifest"])
        self.assertEqual(actual["deleted_entries"], expected["deleted_entries"])

    def _rows(self, sql):
        con = sqlite3.connect(str(self.out / "parsed" / "kindle_parsed.db"))
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def test_clock_offsets_follow_the_clippings(self):
        # Clippings before the March clock change sit five hours behind UTC,
        # the later ones four.
        offsets = self._rows("SELECT clipping_entry, offset_minutes, matched_on "
                             "FROM clock_offsets ORDER BY clipping_entry")
        self.assertEqual([(e, m) for e, m, _ in offsets],
                         [(1, -300), (2, -300), (3, -300), (4, -300), (5, -240), (6, -240)])
        self.assertEqual(set(basis for _e, _m, basis in offsets), {"title, kind and time"})

    def test_documents_table_states(self):
        states = dict(self._rows("SELECT title, state FROM documents"))
        self.assertEqual(states["Q3 operations review"], "file present")
        self.assertEqual(states["Draft supplier terms"], "deleted entry, read back")
        self.assertEqual(states["Harbour Pilot Manual"], "deleted sidecar only")
        row = self._rows("SELECT kind, protected, highlights, notes, clippings, lookups, "
                         "authors FROM documents WHERE asin = 'B0SYNTH001'")[0]
        self.assertEqual(row, ("store book", "yes", 2, 1, 2, 2, "Okafor, Dana"))

    def test_timeline_is_ordered_and_sourced(self):
        rows = self._rows("SELECT time, time_basis, record FROM timeline ORDER BY rowid")
        self.assertEqual([r[0][:19] for r in rows], sorted(r[0][:19] for r in rows))
        self.assertTrue(all(basis and record for _t, basis, record in rows))
        events = self._rows("SELECT COUNT(*) FROM timeline WHERE source = 'sidecar' "
                            "AND file_state = 'deleted, read back'")[0][0]
        self.assertEqual(events, 3)
        total = self._rows("SELECT SUM(total) FROM activity_by_day")[0][0]
        self.assertEqual(total, len(rows))

    def test_observations(self):
        checks = [r[0] for r in self._rows('SELECT "check" FROM observations')]
        for wanted in ("deleted document entry", "deleted sidecar, no content file",
                       "sidecar annotation with no clipping made in the same second"):
            self.assertIn(wanted, checks)
        self.assertNotIn("time later than the acquisition", checks)

    def test_examiner_report(self):
        report = self.out / "parsed" / "report.html"
        text = report.read_text(encoding="utf-8")
        for wanted in ("Kindle examination record", "Tidewater Engineering Handbook",
                       "Ask finance for the source of the freight number.", "UTC-05:00",
                       "Draft supplier terms"):
            self.assertIn(wanted, text)
        self.assertNotIn("<script", text.lower())
        self.assertNotIn("http", text.replace("data:image", ""))
        recorded = (self.out / "parsed" / "report.html.sha256").read_text().split()[0]
        self.assertEqual(recorded, hashlib.sha256(report.read_bytes()).hexdigest())

    def test_report_escapes_device_text(self):
        from kindle_acquire.examiner import write_report
        copy = self.tmp / "hostile_parsed"
        copy.mkdir()
        shutil.copy(self.out / "parsed" / "kindle_parsed.db", copy / "kindle_parsed.db")
        hostile = '<script>alert(1)</script><img src=x onerror="x">'
        con = sqlite3.connect(str(copy / "kindle_parsed.db"))
        con.execute("UPDATE clippings SET title = ?, text = ?", (hostile, hostile))
        con.execute("UPDATE documents SET title = ?, authors = ?", (hostile, hostile))
        con.execute("UPDATE annotations SET note = ?", (hostile,))
        con.execute("UPDATE timeline SET detail = ?, title = ?", (hostile, hostile))
        con.execute("UPDATE device_summary SET value = ?", (hostile,))
        con.execute("UPDATE observations SET item = ?", (hostile,))
        con.commit()
        con.close()
        text = Path(write_report(copy)).read_text(encoding="utf-8")
        self.assertNotIn("<script", text)
        self.assertNotIn("<img src=x", text)
        self.assertIn("&lt;script&gt;alert(1)", text)

    def test_report_needs_the_analysis_tables(self):
        from kindle_acquire.examiner import write_report
        old = self.tmp / "old_parsed"
        old.mkdir()
        con = sqlite3.connect(str(old / "kindle_parsed.db"))
        con.execute("CREATE TABLE annotations (book_key)")
        con.commit()
        con.close()
        with self.assertRaises(ValueError):
            write_report(old)
        with self.assertRaises(ValueError):
            write_report(self.tmp / "no_such_folder")

    def test_model_from_serial(self):
        from kindle_acquire.analysis import model_from_serial
        self.assertIn("Paperwhite, 11th", model_from_serial("G001PX0000000000"))
        self.assertEqual(model_from_serial("G00ZZZ0000000000"), "")
        self.assertEqual(model_from_serial("B024"), "")
        self.assertEqual(model_from_serial(None), "")

    def test_second_extraction_compares_equal(self):
        second = self.tmp / "extract_again"
        extract_image(str(self.image), second, quiet=True)
        result = compare(self.out, second)
        self.assertTrue(result["same_content"])
        self.assertTrue(result["same_records"])
        self.assertEqual(result["counts"]["files_same_sha256"], result["counts"]["files_in_both"])

    def test_compare_reports_each_kind_of_difference(self):
        other = self.tmp / "extract_changed"
        shutil.copytree(self.out, other, ignore=shutil.ignore_patterns("parsed"))
        manifest = other / "manifest.csv"
        with open(manifest, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        columns = list(rows[0])
        clippings = next(r for r in rows if r["rel_path"] == "documents/My Clippings.txt")
        clippings["sha256"] = "0" * 64
        version = next(r for r in rows if r["rel_path"] == "system/version.txt")
        version["modified_stored"] = "2026-01-01T00:00:00"
        rows = [r for r in rows if not r["rel_path"].endswith(".pds")]
        rows.append(dict.fromkeys(columns, ""))
        rows[-1].update({"rel_path": "documents/new.txt", "type": "file", "status": "ok"})
        with open(manifest, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        result = compare(self.out, other)
        self.assertFalse(result["same_content"])
        kinds = result["counts"]["by_difference"]
        self.assertEqual(kinds, {"sha256": 1, "modified_stored": 1, "only in a": 1, "only in b": 1})
        self.assertIn("DIFFERENT", describe(result))

    def test_logical_copy_of_the_extracted_files_compares_equal(self):
        # The same files through the other acquisition path: content must match.
        copy = self.tmp / "logical"
        acquire_logical(self.out / "files", copy, quiet=True)
        result = compare(self.out, copy)
        self.assertTrue(result["same_content"], result["differences"][:5])

    def test_support_profile_has_structure_and_no_content(self):
        profile, path = survey(self.out)
        text = path.read_text(encoding="utf-8")
        for private in synthetic_kindle.PRIVATE_STRINGS:
            self.assertNotIn(private.lower(), text.lower(), private)
        self.assertNotIn(str(self.tmp).lower(), text.lower())
        self.assertEqual(profile["device"]["firmware"], "Kindle 5.17.1 (0000000 000)")
        self.assertEqual(profile["counts"]["files"], 11)
        self.assertEqual(profile["file_types_by_area"]["documents"],
                         {".kfx": 3, ".pdf": 1, ".txt": 1})
        self.assertEqual(profile["signatures_by_extension"][".yjr"], {"kindle sidecar": 3})
        fields = profile["sidecar_fields_by_extension"][".yjr"]
        self.assertIn("annotation.cache.object", fields)
        self.assertEqual(fields["lpr"]["value_shapes"], [["int", "str", "int"]])
        self.assertEqual(profile["annotation_kinds"]["highlight (allocated)"], 4)
        self.assertEqual(profile["annotation_kinds"]["bookmark (deleted, read back)"], 1)
        self.assertEqual(profile["clippings"]["kinds"], {"highlight": 3, "note": 2, "bookmark": 1})
        self.assertEqual(profile["content_files"]["name_patterns"],
                         {"name_has_asin": 3, "name_has_cde_key": 1, "sample": 0, "neither": 1})
        database = profile["databases"]["system/vocabulary/vocab.db"]
        self.assertEqual(database["LOOKUPS"]["rows"], 4)
        self.assertIn("usage", database["LOOKUPS"]["columns"])
        self.assertEqual(profile["deleted_entries"]["by_recovery"]["file, recovered"], 2)
        self.assertNotIn("label", profile["volume"])
        self.assertNotIn("serial", profile["volume"])


class FlakySource:
    """A file that refuses reads inside chosen byte ranges."""

    def __init__(self, path, bad_ranges):
        self._fh = open(path, "rb", buffering=0)
        self._bad = bad_ranges

    def fileno(self):
        return self._fh.fileno()

    def seek(self, position, whence=0):
        return self._fh.seek(position, whence)

    def read(self, count):
        start = self._fh.tell()
        for lo, hi in self._bad:
            if start < hi and start + count > lo:
                raise OSError(23, "data error (cyclic redundancy check)")
        return self._fh.read(count)

    def close(self):
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class ImageFaults(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data = os.urandom(6 * 1024 * 1024)
        self.source = self.tmp / "disk.bin"
        self.source.write_bytes(self.data)

    def tearDown(self):
        self._tmp.cleanup()

    def test_bad_sectors_are_zero_filled_and_listed(self):
        bad = [(1024 * 1024 + 512, 1024 * 1024 + 2048), (3 * 1024 * 1024, 3 * 1024 * 1024 + 512)]
        out = self.tmp / "img"
        log = acquire_image(str(self.source), out, name="bad", quiet=True, verify_source=True,
                            opener=lambda p: FlakySource(p, bad))
        self.assertEqual(log["status"], "review")
        self.assertEqual(log["unreadable_ranges"],
                         [{"offset": lo, "length": hi - lo} for lo, hi in bad])
        self.assertEqual(log["second_read_unreadable_ranges"], log["unreadable_ranges"])
        expected = bytearray(self.data)
        for lo, hi in bad:
            expected[lo:hi] = b"\x00" * (hi - lo)
        self.assertEqual((out / "bad.dd").read_bytes(), bytes(expected))
        self.assertEqual(log["source_hashes"]["sha256"], hashlib.sha256(expected).hexdigest())
        self.assertTrue(log["image_matches_source_read"])
        self.assertTrue(log["source_second_read_matches"])
        self.assertTrue(verify(out)["ok"])     # the folder matches its own records

    def test_lost_source_stops_with_a_log(self):
        gone_at = 1024 * 1024 + 4096
        self.assertGreater(len(self.data) - gone_at, LOST_SOURCE_BYTES)
        out = self.tmp / "img"
        log = acquire_image(str(self.source), out, name="lost", quiet=True, verify_source=True,
                            opener=lambda p: FlakySource(p, [(gone_at, len(self.data))]))
        self.assertEqual(log["status"], "incomplete")
        self.assertEqual(log["stopped_at_offset"], gone_at)
        self.assertLess(log["image_size_bytes"], log["source_size_bytes"])
        image = (out / "lost.dd").read_bytes()
        self.assertEqual(len(image), log["image_size_bytes"])
        self.assertEqual(image[:gone_at], self.data[:gone_at])
        self.assertEqual(set(image[gone_at:]), {0})
        self.assertEqual(log["unreadable_ranges"],
                         [{"offset": gone_at, "length": len(image) - gone_at}])
        self.assertEqual(log["source_hashes"]["sha256"], hashlib.sha256(image).hexdigest())
        self.assertFalse(log["source_second_read_matches"])
        result = verify(out)
        self.assertFalse(result["ok"])
        self.assertIn("incomplete", " ".join(result["problems"]))


if __name__ == "__main__":
    unittest.main()
