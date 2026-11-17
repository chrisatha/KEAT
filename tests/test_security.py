"""Records and device text that try to reach outside their place.

An acquisition folder can come from someone else, and every name and
text on a device is written by its user. These tests hold the tool to
reading only inside the folder it was given and to writing output that
does not run in a spreadsheet or a browser.
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from kindle_acquire import analysis, device_records  # noqa: E402
from kindle_acquire.compare import compare, write_differences  # noqa: E402
from kindle_acquire.extract import resolve_source  # noqa: E402
from kindle_acquire.logical import MANIFEST_COLUMNS, acquire_logical  # noqa: E402
from kindle_acquire.report import parse_acquisition  # noqa: E402
from kindle_acquire.util import csv_safe, hash_file, inside, write_json  # noqa: E402
from kindle_acquire.verify import verify  # noqa: E402

SECRET = "SECRET-OUTSIDE-THE-FOLDER"
FORMULA = "=HYPERLINK(\"http://example.invalid\",\"open\")"


class PathsFromRecords(unittest.TestCase):
    def test_paths_that_leave_the_folder_are_refused(self):
        root = Path("root")
        for rel in ("../x", "a/../../x", "/etc/passwd", "\\\\host\\share\\x", "..\\x",
                    "a//b", "", ".", "a/./b"):
            with self.assertRaises(ValueError, msg=rel):
                inside(root, rel)
        if os.name == "nt":
            for rel in ("C:\\Windows\\win.ini", "C:x", "a/b:stream"):
                with self.assertRaises(ValueError, msg=rel):
                    inside(root, rel)

    def test_paths_inside_the_folder_are_kept(self):
        self.assertEqual(inside(Path("root"), "documents/My Clippings.txt"),
                         Path("root") / "documents" / "My Clippings.txt")
        self.assertEqual(inside(Path("root"), "a..b/..c"), Path("root") / "a..b" / "..c")


class HostileAcquisitionFolder(unittest.TestCase):
    """A folder whose manifest points at files outside it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="keat_sec_"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        source = self.tmp / "source"
        (source / "documents").mkdir(parents=True)
        (source / "documents" / "note.txt").write_text("inside", encoding="utf-8")
        self.acq = self.tmp / "acq"
        acquire_logical(str(source), str(self.acq), quiet=True)
        # Files on the examination computer that the folder must not reach.
        (self.tmp / "My Clippings.txt").write_text(
            "%s (Author)\n- Your Highlight on page 1 | Added on Monday, 1 January 2024 "
            "10:00:00\n\n%s\n==========\n" % (SECRET, SECRET), encoding="utf-8")
        con = sqlite3.connect(str(self.tmp / "private.localstorage"))
        con.execute("CREATE TABLE ItemTable (key TEXT, value BLOB)")
        con.execute("INSERT INTO ItemTable VALUES (?, ?)", ("token", SECRET))
        con.commit()
        con.close()

    def _add_rows(self, rel_paths):
        manifest = self.acq / "manifest.csv"
        with open(manifest, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        for rel in rel_paths:
            row = dict.fromkeys(MANIFEST_COLUMNS, "")
            target = (self.acq / "files" / rel).resolve()
            row.update({"rel_path": rel, "type": "file", "status": "ok",
                        "size": target.stat().st_size,
                        "sha256": hash_file(target)["sha256"],
                        "md5": hashlib.md5(target.read_bytes()).hexdigest()})
            rows.append(row)
        with open(manifest, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=MANIFEST_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        log = json.loads((self.acq / "acquisition_log.json").read_text(encoding="utf-8"))
        log["manifest_sha256"] = hash_file(manifest)["sha256"]
        write_json(self.acq / "acquisition_log.json", log)

    def test_parse_does_not_read_files_outside_the_folder(self):
        outside = ["../../My Clippings.txt", "../../private.localstorage"]
        self._add_rows(outside)
        summary = parse_acquisition(str(self.acq))
        parsed = Path(summary["output"])
        for path in parsed.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes(), str(path))
        con = sqlite3.connect(str(parsed / "kindle_parsed.db"))
        refused = [r[0] for r in con.execute(
            "SELECT file FROM parse_errors WHERE parser = 'manifest'")]
        con.close()
        self.assertEqual(sorted(refused), sorted(outside))

    def test_verify_reports_the_row_and_does_not_pass(self):
        self._add_rows(["../../My Clippings.txt"])
        result = verify(str(self.acq))
        self.assertFalse(result["ok"])
        self.assertTrue(any("outside its folder" in p for p in result["problems"]),
                        result["problems"])

    def test_image_log_cannot_name_a_file_outside_its_folder(self):
        folder = self.tmp / "image"
        folder.mkdir()
        write_json(folder / "acquisition_log.json", {
            "kind": "image", "image_file": "../My Clippings.txt",
            "source_hashes": {"sha256": "0" * 64}})
        with self.assertRaises(ValueError):
            resolve_source(str(folder))
        with self.assertRaises(ValueError):
            verify(str(folder))


@unittest.skipUnless(os.name == "nt", "junctions are a Windows feature")
class Junctions(unittest.TestCase):
    def test_logical_copy_does_not_follow_a_junction(self):
        import subprocess
        tmp = Path(tempfile.mkdtemp(prefix="keat_sec_"))
        self.addCleanup(shutil.rmtree, str(tmp), ignore_errors=True)
        source, outside = tmp / "source", tmp / "outside"
        source.mkdir()
        outside.mkdir()
        (outside / "private.txt").write_text(SECRET, encoding="utf-8")
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(source / "jump"), str(outside)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if made.returncode != 0:
            self.skipTest("cannot create a junction here")
        self.addCleanup(lambda: os.path.isdir(str(source / "jump"))
                        and os.rmdir(str(source / "jump")))
        acquire_logical(str(source), str(tmp / "acq"), quiet=True)
        self.assertFalse((tmp / "acq" / "files" / "jump" / "private.txt").exists())
        with open(tmp / "acq" / "manifest.csv", newline="", encoding="utf-8") as fh:
            rows = {r["rel_path"]: r for r in csv.DictReader(fh)}
        self.assertEqual(rows["jump"]["status"], "skipped")


class SpreadsheetFormulas(unittest.TestCase):
    def test_formula_text_gets_an_apostrophe(self):
        for text in (FORMULA, "+1+cmd|' /C calc'!A0", "-2+3", "@SUM(1)", "\t=1", "\r=1"):
            self.assertEqual(csv_safe(text), "'" + text)

    def test_other_values_are_unchanged(self):
        for value in ("-05:00", "+01:00", "-240", "+1,234.5", "a=b", "", "- note", 5, -5,
                      None, 1.5):
            expected = "'- note" if value == "- note" else value
            self.assertEqual(csv_safe(value), expected)

    def test_parsed_csv_does_not_start_a_cell_with_a_formula(self):
        tmp = Path(tempfile.mkdtemp(prefix="keat_sec_"))
        self.addCleanup(shutil.rmtree, str(tmp), ignore_errors=True)
        source = tmp / "source"
        (source / "documents").mkdir(parents=True)
        (source / "documents" / "My Clippings.txt").write_text(
            "%s (Author)\n- Your Highlight on page 1 | Added on Monday, 1 January 2024 "
            "10:00:00\n\n%s\n==========\n" % (FORMULA, FORMULA), encoding="utf-8")
        acquire_logical(str(source), str(tmp / "acq"), quiet=True)
        parsed = Path(parse_acquisition(str(tmp / "acq"))["output"])
        with open(parsed / "csv" / "clippings.csv", newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["text"], "'" + FORMULA)
        for name in os.listdir(parsed / "csv"):
            with open(parsed / "csv" / name, newline="", encoding="utf-8-sig") as fh:
                for row in csv.reader(fh):
                    for cell in row:
                        self.assertFalse(cell.startswith(("=", "@")), "%s: %r" % (name, cell))
        # The database keeps the text as the device wrote it.
        con = sqlite3.connect(str(parsed / "kindle_parsed.db"))
        self.assertEqual(con.execute("SELECT text FROM clippings").fetchone()[0], FORMULA)
        con.close()
        # The report shows it as text and carries a policy that blocks scripts.
        report = (parsed / "report.html").read_text(encoding="utf-8")
        self.assertIn("Content-Security-Policy", report)
        self.assertIn("default-src 'none'", report)
        self.assertNotIn('"http://example.invalid"', report)

    def test_difference_list_is_made_safe(self):
        tmp = Path(tempfile.mkdtemp(prefix="keat_sec_"))
        self.addCleanup(shutil.rmtree, str(tmp), ignore_errors=True)
        for name, files in (("a", ["=1+1.txt"]), ("b", [])):
            source = tmp / ("source_" + name)
            source.mkdir()
            for file_name in files:
                (source / file_name).write_text("x", encoding="utf-8")
            acquire_logical(str(source), str(tmp / name), quiet=True)
        out = tmp / "differences.csv"
        write_differences(compare(str(tmp / "a"), str(tmp / "b")), str(out))
        with open(out, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["rel_path"], "'=1+1.txt")


class DeeplyNestedJson(unittest.TestCase):
    """A record nested deeper than the interpreter allows must not stop a parse."""

    def test_nested_record_is_treated_as_not_decoded(self):
        deep = "[" * 200000
        self.assertEqual(device_records._json(deep), {})
        self.assertEqual(device_records._json('{"a":' * 200000), {})
        self.assertEqual(analysis._display_names(deep), "")


if __name__ == "__main__":
    unittest.main()
