"""An invented Kindle as a small FAT32 image.

Shared by the regression tests and by tools/make_screenshots.py. Titles,
notes, identifiers and times are made up. The image has four content
files with sidecars, My Clippings.txt, a vocabulary database, a deleted
document and the deleted sidecar folder of a removed book.
"""

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kindle_acquire.parsers import krds  # noqa: E402
from test_fat32 import MiniFat  # noqa: E402
from test_kindle_acquire import k_int, k_long, k_name, k_str  # noqa: E402


def ms(month, day, hour, minute, second=0):
    moment = datetime(2026, month, day, hour, minute, second, tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def sidecar(annotations, position, read_ms):
    """A sidecar file. annotations: [(kind, start, end, created_ms, note)]."""
    parts = []
    for kind, start, end, created, note in annotations:
        parts.append(k_name("annotation.personal." + kind) + k_str(start) + k_str(end)
                     + k_long(created) + k_long(created) + k_str("0￼0")
                     + (k_str(note) if note else b"") + b"\xff")
    tree = k_name("saved.avl.interval.tree") + k_int(len(parts)) + b"".join(parts) + b"\xff"
    cache = k_name("annotation.cache.object") + k_int(1) + k_int(1) + tree + b"\xff"
    lpr = k_name("lpr") + b"\x07\x02" + k_str(position) + k_long(read_ms) + b"\xff"
    return krds.SIGNATURE + k_long(1) + k_int(2) + cache + lpr


CLIPPINGS = "﻿" + "".join(
    "%s\r\n- Your %s | Added on %s\r\n\r\n%s\r\n==========\r\n" % row for row in (
        ("Tidewater Engineering Handbook (Okafor, Dana)",
         "Highlight on page 41 | Location 612-614", "Tuesday, March 3, 2026 9:14:22 PM",
         "A culvert sized for the ten-year storm will be overtopped within its service life."),
        ("Tidewater Engineering Handbook (Okafor, Dana)",
         "Note on page 41 | Location 614", "Tuesday, March 3, 2026 9:15:40 PM",
         "Compare with the figures in the Q3 review."),
        ("A Short History of Lighthouses (Brandt, Elise)",
         "Bookmark on page 118 | Location 1790", "Saturday, March 7, 2026 7:02:11 AM", ""),
        ("A Short History of Lighthouses (Brandt, Elise)",
         "Highlight on page 120 | Location 1822-1825", "Saturday, March 7, 2026 7:09:48 AM",
         "The keeper's log was the only record of which ships had passed in the night."),
        ("Notes on Contract Drafting (Hale, Morgan)",
         "Highlight on page 9 | Location 131-133", "Monday, March 16, 2026 12:31:05 PM",
         "Define the term once and use it the same way every time it appears."),
        ("Q3 operations review",
         "Note on page 4", "Thursday, March 19, 2026 4:47:30 PM",
         "Ask finance for the source of the freight number."),
    ))

# Text that must never appear in a support profile (see test_survey).
PRIVATE_STRINGS = (
    "Tidewater", "Lighthouses", "Contract Drafting", "Okafor", "Brandt", "Hale",
    "Q3_operations", "Harbour Pilot", "Draft_supplier", "culvert", "freight number",
    "B0SYNTH001", "0A1B2C3D4E5F60718293A4B5C6D7E8F9",
)


def _vocabulary_db(tmp):
    path = Path(tmp) / "vocab.db"
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    con.executescript(
        "CREATE TABLE WORDS (id TEXT, word TEXT, stem TEXT, lang TEXT, category INT, timestamp INT, profileid TEXT);"
        "CREATE TABLE LOOKUPS (id TEXT, word_key TEXT, book_key TEXT, dict_key TEXT, pos TEXT, usage TEXT, timestamp INT);"
        "CREATE TABLE BOOK_INFO (id TEXT, asin TEXT, guid TEXT, lang TEXT, title TEXT, authors TEXT);"
        "INSERT INTO BOOK_INFO VALUES ('BK1','B0SYNTH001','BK1','en','Tidewater Engineering Handbook','Okafor, Dana');"
        "INSERT INTO BOOK_INFO VALUES ('BK2','B0SYNTH002','BK2','en','A Short History of Lighthouses','Brandt, Elise');")
    lookups = (("culvert", "BK1", "A culvert sized for the ten-year storm will be overtopped.", ms(3, 4, 2, 13)),
               ("revetment", "BK1", "The revetment failed at the toe.", ms(3, 4, 2, 20)),
               ("lamp", "BK2", "The lamp burned colza oil until 1860.", ms(3, 7, 12, 1)),
               ("fathom", "BK2", "Soundings were logged in fathoms.", ms(3, 7, 12, 6)))
    for index, (word, book, usage, stamp) in enumerate(lookups):
        con.execute("INSERT INTO WORDS VALUES (?,?,?,?,?,?,?)",
                    ("en:" + word, word, word, "en", 0, stamp, ""))
        con.execute("INSERT INTO LOOKUPS VALUES (?,?,?,?,?,?,?)",
                    ("L%d" % index, "en:" + word, book, "D1", "AAAA:%d" % (100 * index), usage, stamp))
    con.commit()
    con.close()
    data = path.read_bytes()
    path.unlink()
    return data


def build_filesystem(tmp):
    """The invented Kindle as a MiniFat object (not yet written out)."""
    fs = MiniFat()
    system, _ = fs.add_dir(fs.root, "system")
    fs.add_file(system, "version.txt", b"Kindle 5.17.1 (0000000 000)\n")
    vocabulary, _ = fs.add_dir(system, "vocabulary")
    fs.add_file(vocabulary, "vocab.db", _vocabulary_db(tmp))

    documents, _ = fs.add_dir(fs.root, "documents")
    fs.add_file(documents, "My Clippings.txt", CLIPPINGS.encode("utf-8"))
    downloads, _ = fs.add_dir(documents, "Downloads")
    items, _ = fs.add_dir(downloads, "Items01")
    protected = b"\xeaDRMION\xee" + b"\x00" * 600
    books = (
        ("Tidewater Engineering Handbook_B0SYNTH001", "kfx", protected, "yjr", sidecar([
            ("highlight", "AZ0BAAA:9180", "AZ0BAAA:9212", ms(3, 4, 2, 14, 22), ""),
            ("note", "AZ0BAAA:9212", "AZ0BAAA:9212", ms(3, 4, 2, 15, 40),
             "Compare with the figures in the Q3 review."),
            ("highlight", "AZ0CAAA:14400", "AZ0CAAA:14466", ms(3, 5, 1, 2, 9), "")],
            "AZ0CAAA:14466", ms(3, 5, 1, 30))),
        ("A Short History of Lighthouses_B0SYNTH002", "kfx", protected, "yjr", sidecar([
            ("bookmark", "AY8AAAA:26850", "AY8AAAA:26850", ms(3, 7, 12, 2, 11), ""),
            ("highlight", "AY8AAAA:27330", "AY8AAAA:27391", ms(3, 7, 12, 9, 48), "")],
            "AY8AAAA:27391", ms(3, 7, 12, 40))),
        ("Notes on Contract Drafting_B0SYNTH003", "kfx", protected, "yjr", sidecar([
            ("highlight", "AX2AAAA:1965", "AX2AAAA:2010", ms(3, 16, 16, 31, 5), "")],
            "AX2AAAA:2010", ms(3, 16, 16, 50))),
        ("Q3_operations_review-cdeKey_0A1B2C3D4E5F60718293A4B5C6D7E8F9", "pdf",
         b"%PDF-1.7\n" + b"synthetic page data\n" * 120, "pds", sidecar([
             ("note", "4 0 0 0", "4 0 0 0", ms(3, 19, 20, 47, 30),
              "Ask finance for the source of the freight number.")],
             "6 0 0 0", ms(3, 19, 21, 5))),
    )
    for name, extension, payload, side_ext, side in books:
        fs.add_file(items, "%s.%s" % (name, extension), payload)
        folder, _ = fs.add_dir(items, name + ".sdr")
        fs.add_file(folder, "%s.%s" % (name, side_ext), side)
    # A document that was deleted, and the sidecar folder of a removed book.
    slots, clusters = fs.add_file(
        items, "Draft_supplier_terms-cdeKey_11223344556677889900AABBCCDDEEFF.pdf",
        b"%PDF-1.7\n" + b"deleted draft\n" * 150, clusters=[330, 331, 332])
    fs.delete(items, slots, clusters)
    gone, gone_slots = fs.add_dir(items, "Harbour Pilot Manual_B0SYNTH004.sdr", start=340)
    slots, clusters = fs.add_file(gone, "Harbour Pilot Manual_B0SYNTH004.yjr", sidecar([
        ("highlight", "AW1AAAA:5020", "AW1AAAA:5077", ms(2, 21, 23, 40, 2), ""),
        ("bookmark", "AW1AAAA:6100", "AW1AAAA:6100", ms(2, 22, 0, 5, 51), "")],
        "AW1AAAA:6100", ms(2, 22, 0, 6)), clusters=[350])
    fs.delete(gone, slots, clusters)
    fs.delete(items, gone_slots, fs.dirs[gone]["clusters"])
    return fs


def build_image(tmp, name="KDL-01.dd"):
    """Write the invented Kindle to tmp/<name>. Returns the path."""
    image = Path(tmp) / name
    image.write_bytes(build_filesystem(tmp).build())
    return image
