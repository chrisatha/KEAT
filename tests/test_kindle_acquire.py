"""Tests run on synthetic data only. No device and no real user data."""

import csv
import hashlib
import json
import os
import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindle_acquire.image import acquire_image  # noqa: E402
from kindle_acquire.logical import acquire_logical  # noqa: E402
from kindle_acquire.parsers import krds  # noqa: E402
from kindle_acquire.parsers.clippings import parse_clippings  # noqa: E402
from kindle_acquire.report import parse_acquisition  # noqa: E402
from kindle_acquire.verify import verify  # noqa: E402


def k_str(text):
    if text == "":
        return b"\x03\x01"
    raw = text.encode("utf-8")
    return b"\x03\x00" + struct.pack(">H", len(raw)) + raw


def k_name(text):
    raw = text.encode("utf-8")
    return b"\xfe\x00" + struct.pack(">H", len(raw)) + raw


def k_int(value):
    return b"\x01" + struct.pack(">i", value)


def k_long(value):
    return b"\x02" + struct.pack(">q", value)


CREATED_MS = 1794715200533   # 2026-11-15T04:00:00.533Z
MODIFIED_MS = 1794715260000  # one minute later


def sample_sidecar():
    note = (k_name("annotation.personal.note") + k_str("AAAA:100") + k_str("AAAA:120")
            + k_long(CREATED_MS) + k_long(MODIFIED_MS) + k_str("0￼0")
            + k_str("check clause 4") + b"\xff")
    tree = k_name("saved.avl.interval.tree") + k_int(1) + note + b"\xff"
    cache = k_name("annotation.cache.object") + k_int(1) + k_int(2) + tree + b"\xff"
    lpr = k_name("lpr") + b"\x07\x02" + k_str("14 0 0 0") + k_long(CREATED_MS) + b"\xff"
    flag = k_name("sync_lpr") + b"\x00\x01" + b"\xff"
    return krds.SIGNATURE + k_long(1) + k_int(3) + cache + lpr + flag


CLIPPINGS = (
    "﻿Sample Title (Writer, Ann)\r\n"
    "- Your Highlight on page 98 | Location 1292-1293 | Added on Sunday, July 5, 2026 11:39:10 PM\r\n"
    "\r\n"
    "first line of text\r\n"
    "==========\r\n"
    "Report (Draft) v2 (Team (Legal))\r\n"
    "- Your Bookmark on page 12 | Location 150 | Added on Monday, July 13, 2026 5:53:12 PM\r\n"
    "\r\n"
    "\r\n"
    "==========\r\n"
    "﻿Sample Title (Writer, Ann)\r\n"
    "- Your Note on Location 200 | Added on Tuesday, 14 July 2026 17:00:01\r\n"
    "\r\n"
    "two line\r\nnote\r\n"
    "==========\r\n"
)


class KrdsTests(unittest.TestCase):
    def test_decode_and_extract(self):
        decoded = krds.decode(sample_sidecar())
        self.assertEqual(decoded["header"], [1, 3])
        self.assertEqual([f["name"] for f in decoded["fields"]],
                         ["annotation.cache.object", "lpr", "sync_lpr"])
        notes = krds.annotations(decoded)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["kind"], "note")
        self.assertEqual(notes[0]["start_position"], "AAAA:100")
        self.assertEqual(notes[0]["end_position"], "AAAA:120")
        self.assertEqual(notes[0]["created_utc"], "2026-11-15T04:00:00.533+00:00")
        self.assertEqual(notes[0]["modified_utc"], "2026-11-15T04:01:00.000+00:00")
        self.assertEqual(notes[0]["note"], "check clause 4")
        pos = krds.positions(decoded)
        self.assertEqual(pos[0]["position"], "14 0 0 0")
        self.assertEqual(pos[0]["time_utc"], "2026-11-15T04:00:00.533+00:00")
        self.assertIs(decoded["fields"][2]["values"][0], True)

    def test_rejects_bad_input(self):
        with self.assertRaises(krds.KrdsError):
            krds.decode(b"not a sidecar file")
        with self.assertRaises(krds.KrdsError):
            krds.decode(sample_sidecar()[:-5])
        with self.assertRaises(krds.KrdsError):
            krds.decode(krds.SIGNATURE + b"\x63")


class ClippingsTests(unittest.TestCase):
    def test_entries(self):
        entries = parse_clippings(CLIPPINGS)
        self.assertEqual(len(entries), 3)
        first, second, third = entries
        self.assertEqual((first["title"], first["author"]), ("Sample Title", "Writer, Ann"))
        self.assertEqual(first["kind"], "highlight")
        self.assertEqual(first["page"], "98")
        self.assertEqual(first["location"], "1292-1293")
        self.assertEqual(first["added_device_local"], "2026-07-05T23:39:10")
        self.assertEqual(first["text"], "first line of text")
        self.assertEqual(second["kind"], "bookmark")
        self.assertEqual(second["text"], "")
        self.assertEqual(second["title_line"], "Report (Draft) v2 (Team (Legal))")
        self.assertEqual(third["kind"], "note")
        self.assertEqual(third["page"], "")
        self.assertEqual(third["location"], "200")
        self.assertEqual(third["added_device_local"], "2026-07-14T17:00:01")
        self.assertEqual(third["text"], "two line\nnote")


def build_device_tree(root):
    """A small tree shaped like Kindle user storage."""
    docs = root / "documents" / "Downloads" / "Items01"
    docs.mkdir(parents=True)
    (root / "documents" / "My Clippings.txt").write_bytes(CLIPPINGS.encode("utf-8"))
    pdf = "board_minutes-cdeKey_0123456789ABCDEF0123456789ABCDEF"
    (docs / (pdf + ".pdf")).write_bytes(b"%PDF-1.7\n" + b"x" * 5000)
    sdr = docs / (pdf + ".sdr")
    sdr.mkdir()
    (sdr / (pdf + "d32e.pds")).write_bytes(sample_sidecar())
    (docs / "Some Book_B000000001_sample.kfx").write_bytes(b"\xeaDRMION\xee" + b"\x00" * 100)
    (docs / "empty.bin").write_bytes(b"")
    long_dir = docs / ("d" * 120) / ("e" * 120)
    long_dir.mkdir(parents=True)
    (long_dir / ("f" * 100 + ".txt")).write_text("long path", encoding="utf-8")
    system = root / "system"
    (system / "vocabulary").mkdir(parents=True)
    (system / "version.txt").write_text("Kindle 0.0.0 (test)\n", encoding="utf-8")
    con = sqlite3.connect(str(system / "vocabulary" / "vocab.db"))
    con.executescript(
        "CREATE TABLE WORDS (id TEXT, word TEXT, stem TEXT, lang TEXT, category INT, timestamp INT, profileid TEXT);"
        "CREATE TABLE LOOKUPS (id TEXT, word_key TEXT, book_key TEXT, dict_key TEXT, pos TEXT, usage TEXT, timestamp INT);"
        "CREATE TABLE BOOK_INFO (id TEXT, asin TEXT, guid TEXT, lang TEXT, title TEXT, authors TEXT);"
        "INSERT INTO WORDS VALUES ('en:lien','lien','lien','en',0,%d,'');"
        "INSERT INTO BOOK_INFO VALUES ('BK1','B000000001','BK1','en','Some Book','Writer, Ann');"
        "INSERT INTO LOOKUPS VALUES ('L1','en:lien','BK1','D1','AAAA:5','the lien was filed',%d);"
        % (CREATED_MS, CREATED_MS))
    con.commit()
    con.close()


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.src = self.tmp / "device"
        self.src.mkdir()
        build_device_tree(self.src)

    def tearDown(self):
        self._tmp.cleanup()

    def _rows(self, acq):
        with open(acq / "manifest.csv", newline="", encoding="utf-8") as fh:
            return {r["rel_path"]: r for r in csv.DictReader(fh)}

    def test_logical_copy_matches_source(self):
        acq = self.tmp / "acq"
        log = acquire_logical(self.src, acq, quiet=True)
        self.assertEqual(log["errors"], [])
        expected = 0
        rows = self._rows(acq)
        for folder, _dirs, names in os.walk(str(self.src)):
            for name in names:
                expected += 1
                full = os.path.join(folder, name)
                rel = os.path.relpath(full, str(self.src)).replace("\\", "/")
                with open("\\\\?\\" + full if os.name == "nt" else full, "rb") as fh:
                    data = fh.read()
                self.assertEqual(rows[rel]["sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(rows[rel]["md5"], hashlib.md5(data).hexdigest())
                self.assertEqual(int(rows[rel]["size"]), len(data))
        self.assertEqual(log["counts"]["files_ok"], expected)
        self.assertTrue(verify(acq)["ok"])

    def test_verify_detects_changes(self):
        acq = self.tmp / "acq"
        acquire_logical(self.src, acq, quiet=True)
        target = acq / "files" / "system" / "version.txt"
        target.write_text("changed", encoding="utf-8")
        (acq / "files" / "added.txt").write_text("extra", encoding="utf-8")
        result = verify(acq)
        self.assertFalse(result["ok"])
        text = "\n".join(result["problems"])
        self.assertIn("system/version.txt", text)
        self.assertIn("added.txt", text)

    def test_output_rules(self):
        acq = self.tmp / "acq"
        acq.mkdir()
        (acq / "old.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            acquire_logical(self.src, acq, quiet=True)
        with self.assertRaises(ValueError):
            acquire_logical(self.src, self.src / "out", quiet=True)

    def test_image_of_a_file(self):
        source = self.tmp / "disk.bin"
        data = os.urandom(3 * 1024 * 1024 + 512 * 7)
        source.write_bytes(data)
        acq = self.tmp / "img"
        log = acquire_image(str(source), acq, name="TEST 01", verify_source=True, quiet=True)
        self.assertEqual(log["status"], "ok")
        self.assertEqual(log["image_file"], "TEST_01.dd")
        self.assertEqual(log["source_size_bytes"], len(data))
        self.assertEqual(log["source_hashes"]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(log["source_hashes"]["md5"], hashlib.md5(data).hexdigest())
        self.assertEqual(log["source_hashes"]["sha1"], hashlib.sha1(data).hexdigest())
        self.assertEqual((acq / "TEST_01.dd").read_bytes(), data)
        self.assertTrue(log["source_second_read_matches"])
        self.assertTrue(verify(acq)["ok"])
        with open(acq / "TEST_01.dd", "r+b") as fh:
            fh.seek(1000)
            fh.write(b"\x00\x01\x02\x03")
        self.assertFalse(verify(acq)["ok"])

    def test_parse_survives_odd_database_names(self):
        folder = self.src / "system" / "freetime"
        folder.mkdir(parents=True)
        con = sqlite3.connect(str(folder / "freetime.db"))
        con.execute('CREATE TABLE bookinfo ("a""b" TEXT, "select" TEXT)')
        con.execute('CREATE TABLE "odd ""table" (x)')
        con.execute("INSERT INTO bookinfo VALUES ('1', '=2+2')")
        con.commit()
        con.close()
        acq = self.tmp / "acq"
        acquire_logical(self.src, acq, quiet=True)
        summary = parse_acquisition(acq)
        self.assertEqual(summary["parse_errors"], 0)
        self.assertEqual(summary["freetime_bookinfo"], 1)
        out = sqlite3.connect(str(acq / "parsed" / "kindle_parsed.db"))
        self.assertEqual(out.execute('SELECT "a""b", "select" FROM freetime_bookinfo').fetchall(),
                         [("1", "=2+2")])
        out.close()

    def test_parse(self):
        acq = self.tmp / "acq"
        acquire_logical(self.src, acq, quiet=True)
        summary = parse_acquisition(acq)
        self.assertEqual(summary["parse_errors"], 0)
        self.assertEqual(summary["clippings"], 3)
        self.assertEqual(summary["annotations"], 1)
        self.assertEqual(summary["vocabulary_lookups"], 1)
        con = sqlite3.connect(str(acq / "parsed" / "kindle_parsed.db"))
        pdf = con.execute(
            "SELECT container, cde_key, asin, annotation_count, sidecar_folder "
            "FROM content_files WHERE extension='.pdf'").fetchone()
        self.assertEqual(pdf[0], "PDF")
        self.assertEqual(pdf[1], "0123456789ABCDEF0123456789ABCDEF")
        self.assertEqual(pdf[2], "")
        self.assertEqual(pdf[3], 1)
        self.assertTrue(pdf[4].endswith(".sdr"))
        book = con.execute(
            "SELECT container, asin, is_sample FROM content_files WHERE extension='.kfx'").fetchone()
        self.assertEqual(book, ("KFX (DRM-protected)", "B000000001", "yes"))
        note = con.execute(
            "SELECT kind, note, created_utc, file_state FROM annotations").fetchone()
        self.assertEqual(note, ("note", "check clause 4", "2026-11-15T04:00:00.533+00:00",
                                "allocated"))
        lookup = con.execute(
            "SELECT word, usage, book_title, lookup_utc FROM vocabulary_lookups").fetchone()
        self.assertEqual(lookup, ("lien", "the lien was filed", "Some Book",
                                  "2026-11-15T04:00:00.533+00:00"))
        firmware = con.execute(
            "SELECT value FROM device_facts WHERE fact='firmware'").fetchone()[0]
        self.assertEqual(firmware, "Kindle 0.0.0 (test)")
        con.close()
        log = json.loads((acq / "parsed" / "parse_log.json").read_text(encoding="utf-8"))
        self.assertEqual(log["errors"], [])


if __name__ == "__main__":
    unittest.main()
