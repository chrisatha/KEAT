"""An invented Kindle as a small FAT32 image.

Shared by the regression tests and by tools/make_screenshots.py. Titles,
notes, identifiers and times are made up. The image has four content
files with sidecars, My Clippings.txt, a vocabulary database, a deleted
document and the deleted sidecar folder of a removed book. It also has an
activity log with a write-ahead log, an audiobook database, store page
data, a browser cache and cover thumbnails.
"""

import json
import sqlite3
import struct
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


def sidecar(annotations, position, read_ms, history=()):
    """A sidecar file. annotations: [(kind, start, end, created_ms, note)].

    history: [(position, ms)] for page.history.store.
    """
    parts = []
    for kind, start, end, created, note in annotations:
        parts.append(k_name("annotation.personal." + kind) + k_str(start) + k_str(end)
                     + k_long(created) + k_long(created) + k_str("0￼0")
                     + (k_str(note) if note else b"") + b"\xff")
    tree = k_name("saved.avl.interval.tree") + k_int(len(parts)) + b"".join(parts) + b"\xff"
    cache = k_name("annotation.cache.object") + k_int(1) + k_int(1) + tree + b"\xff"
    lpr = k_name("lpr") + b"\x07\x02" + k_str(position) + k_long(read_ms) + b"\xff"
    if not history:
        return krds.SIGNATURE + k_long(1) + k_int(2) + cache + lpr
    jumps = b"".join(k_name("page.history.record") + k_str(where) + k_long(when) + b"\xff"
                     for where, when in history)
    pages = k_name("page.history.store") + k_int(len(history)) + jumps + b"\xff"
    return krds.SIGNATURE + k_long(1) + k_int(3) + cache + lpr + pages


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
    "SYNTHETICACCOUNT0001", "portal.example", "harbour/tides", "America/Chicago",
    "Synthetic phone app", "B0SYNTHAU1", "G000XX0000000000",
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


ACCOUNT = "amzn1.account.SYNTHETICACCOUNT0001"
SERIAL = "G000XX0000000000"


def _activity_log(tmp):
    """fmcache.db and its write-ahead log. Returns (database bytes, log bytes).

    Session 1 and its records are written, then removed, as the device does
    after an upload. They stay in earlier frames of the log. Session 2 and
    three records are the current rows.
    """
    path = Path(tmp) / "fmcache.db"
    for suffix in ("", "-wal", "-shm"):
        if Path(str(path) + suffix).exists():
            Path(str(path) + suffix).unlink()
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        "CREATE TABLE records(id INTEGER PRIMARY KEY, schema_name TEXT NOT NULL, "
        "schema_version TEXT NOT NULL, app_session_id INT NOT NULL, reading_session_id INT, "
        "encoded_size INT NOT NULL, sequence_number INT NOT NULL, created_timestamp INT NOT NULL, "
        "priority INT NOT NULL, record TEXT NOT NULL);"
        "CREATE TABLE app_sessions(id INTEGER PRIMARY KEY, encoded_size INT NOT NULL, "
        "session TEXT NOT NULL, dummy_session INT NULL);"
        "CREATE TABLE reading_sessions(id INTEGER PRIMARY KEY, encoded_size INT NOT NULL, "
        "session TEXT NOT NULL, dummy_session INT NULL);")
    con.commit()

    def session(kind, number, start, end, payload):
        body = dict(payload, start_timestamp=start, end_timestamp=end,
                    is_complete=end is not None)
        text = json.dumps({"created_timestamp": start, "payload": body, "schema_name": kind,
                           "schema_version": 0, "sent_timestamp": end or start,
                           "sequence_number": 1}, sort_keys=True, separators=(",", ":"))
        con.execute("INSERT OR REPLACE INTO %ss VALUES (?,?,?,?)" % kind,
                    (number, len(text), text, 0))

    def record(schema, app, reading, sequence, stamp, body):
        text = json.dumps(body, sort_keys=True, separators=(",", ":"))
        con.execute("INSERT INTO records(schema_name, schema_version, app_session_id, "
                    "reading_session_id, encoded_size, sequence_number, created_timestamp, "
                    "priority, record) VALUES (?,?,?,?,?,?,?,?,?)",
                    (schema, "0", app, reading, len(text), sequence, stamp, 0, text))

    app = {"time_zone": "America/Chicago", "device_serial_number": SERIAL,
           "device_model": "A0SYNTHMODEL", "device_type": "A0SYNTHMODEL",
           "customer_id": ACCOUNT, "country_of_residence": "US",
           "preferred_marketplace_id": "A0SYNTHMARKET", "os_version": "synthetic.os",
           "app_version": "1"}
    book = {"asin": "B0SYNTH001", "content_type": "EBOK", "format": "YJ",
            "start_reading_location": 1, "end_reading_location": 5200, "max_position": 5300}
    first = ms(3, 4, 2, 10)
    session("app_session", 1, first, None, app)
    session("reading_session", 1, first + 2000, None, book)
    record("ereader_framework_connectivity", 1, None, 1, first + 500,
           {"connection_name": "wireless", "connection_state": "wifi", "is_changed": 1})
    record("ereader_open_book", 1, 1, 2, first + 2100,
           {"book_category": "EBOK", "load_method": "catalog", "start_reading_position": 9100})
    for turn in range(5):
        record("ereader_book_page_turn", 1, 1, 3 + 2 * turn, first + 60000 * (turn + 1),
               {"action_id": "NextPageTurnWithTAP", "book_category": "EBOK"})
        record("ereader_book_consume_content", 1, 1, 4 + 2 * turn, first + 60000 * (turn + 1) + 50,
               {"context": "Book:Reading:MainContent", "start_position": 9100 + 80 * turn,
                "end_position": 9180 + 80 * turn})
    record("kindle_metrics_library_size", 1, None, 13, first + 400000,
           {"all": 12, "books": 7, "books_downloaded": 3, "documents": 2,
            "documents_downloaded": 1, "audiobooks": 1, "audiobooks_downloaded": 0})
    record("ereader_close_book", 1, 1, 14, first + 420000,
           {"close_method": "GoingToScreensaver", "close_position": 9500,
            "close_timestamp": first + 420000})
    record("ereader_powerd_state_change", 1, None, 15, first + 421000,
           {"prev_state": "ACTIVE", "curr_state": "SCREEN SAVER"})
    record("ereader_framework_connectivity", 1, None, 16, first + 430000,
           {"connection_name": "wireless", "connection_state": "none", "is_changed": 1})
    con.commit()
    session("app_session", 1, first, first + 431000, app)
    session("reading_session", 1, first + 2000, first + 420000, book)
    con.commit()
    # The upload: everything from session 1 is removed.
    con.execute("DELETE FROM records")
    con.execute("DELETE FROM app_sessions")
    con.execute("DELETE FROM reading_sessions")
    con.commit()
    second = ms(3, 19, 20, 40)
    session("app_session", 2, second, None, app)
    session("reading_session", 2, second + 1000, None,
            dict(book, asin="", content_type="PDOC", format="PDF",
                 embedded_id="0A1B2C3D4E5F60718293A4B5C6D7E8F9"))
    record("ereader_hibernate_wakeup", 2, None, 1, second + 100,
           {"time_in_hibernate_ms": 3600000, "wakeup_reason": "POWERD_WAKEUP_REASON_POWER_BUTTON"})
    record("ereader_framework_connectivity", 2, None, 2, second + 300,
           {"connection_name": "wireless", "connection_state": "wifi", "is_changed": 1})
    record("ereader_book_page_turn", 2, 2, 3, second + 30000,
           {"action_id": "NextPageTurnWithTAP", "book_category": "PDOC"})
    con.commit()
    data = path.read_bytes()
    log = Path(str(path) + "-wal").read_bytes()
    con.close()
    for suffix in ("", "-wal", "-shm"):
        if Path(str(path) + suffix).exists():
            Path(str(path) + suffix).unlink()
    return data, log


def _small_db(tmp, script, rows=()):
    path = Path(tmp) / "small.db"
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    con.executescript(script)
    for sql, values in rows:
        con.execute(sql, values)
    con.commit()
    con.close()
    data = path.read_bytes()
    path.unlink()
    return data


def _audible_db(tmp):
    return _small_db(
        tmp,
        "CREATE TABLE ANNOTATIONS (ASIN TEXT, START_POS_MS INT, END_POS_MS INT, "
        "CREATION_DATE INT, LAST_UPDATED_DATE INT, NOTES TEXT, SRC_DEVICE TEXT, LTO INT, TYPE TEXT);"
        "CREATE TABLE relationships (e_asin TEXT, e_acr TEXT, e_version TEXT, e_format TEXT, "
        "a_asin TEXT, a_acr TEXT, a_version TEXT, a_format TEXT, amz_asin TEXT, "
        "sync_file_acr TEXT, is_audiobook_owned INT, time_last_fetched INT);",
        (("INSERT INTO ANNOTATIONS VALUES (?,?,?,?,?,?,?,?,?)",
          ("B0SYNTHAU1", 5025000, 5025000, ms(3, 8, 13, 5), ms(3, 8, 13, 5), None,
           "Synthetic phone app", -300, "REMOTE_LPH")),
         ("INSERT INTO relationships VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
          ("B0SYNTH002", "CR!SYNTHE1", "1", "YJBINARY", "B0SYNTHAU1", "CR!SYNTHA1", "1",
           "AAX_22", "", "CR!SYNTHS1", 1, ms(3, 8, 13, 0)))))


def _store_storage(tmp):
    def text(value):
        return value.encode("utf-16-le")
    page = '{"url":"/store/detail","params":{"asin":"B0SYNTH003"}}'
    return _small_db(
        tmp, "CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB NOT NULL);",
        (("INSERT INTO ItemTable VALUES (?,?)",
          ("*prefs*_userid", text('"%s"' % ACCOUNT))),
         ("INSERT INTO ItemTable VALUES (?,?)",
          ("*prefs*_lastStoreAccess", text(str(ms(3, 15, 22, 5))))),
         ("INSERT INTO ItemTable VALUES (?,?)",
          ("*index*_" + page, text('{"created":%d,"TTL":900000,"attrs":null}' % ms(3, 15, 22, 4)))),
         ("INSERT INTO ItemTable VALUES (?,?)",
          ("*cache*_" + page, text('{"title":"Notes on Contract Drafting","price":4.99}')))))


def _webkit(month, day, hour, minute):
    return (ms(month, day, hour, minute) + 11644473600000) * 1000


def _browser_block_file():
    """A Chromium block file with two entry records and one empty block."""
    header = struct.pack("<IIhhiii", 0xC104CAC3, 0x20000, 1, 0, 256, 2, 3)
    header += b"\x00" * (8192 - len(header))
    blocks = b""
    for address, created, size in (
            ("https://portal.example.net/login?session=1", _webkit(3, 12, 18, 30), 4100),
            ("1/0/_dk_https://example.org https://example.org https://www.example.org/harbour/tides",
             _webkit(3, 12, 18, 34), 15250)):
        key = address.encode("ascii")
        entry = struct.pack("<IIIiiiQiI4i4I", 0x1234, 0, 0, 0, 0, 0, created, len(key), 0,
                            0, size, 0, 0, 0, 0, 0, 0)
        entry += b"\x00" * (96 - len(entry)) + key
        blocks += entry + b"\x00" * (256 - len(entry))
    return header + blocks + b"\x00" * 256


def _browser_simple_file():
    key = b"1/0/https://static.example.org/site.js"
    return (struct.pack("<QIII", 0xFCFB6D1BA7725C30, 5, len(key), 0x5EED) + b"\x00" * 4
            + key + b"console.log('synthetic');" + b"\x00" * 40)


def build_filesystem(tmp):
    """The invented Kindle as a MiniFat object (not yet written out)."""
    fs = MiniFat()
    system, _ = fs.add_dir(fs.root, "system")
    fs.add_file(system, "version.txt", b"Kindle 5.17.1 (0000000 000)\n")
    vocabulary, _ = fs.add_dir(system, "vocabulary")
    fs.add_file(vocabulary, "vocab.db", _vocabulary_db(tmp))
    fmcache, _ = fs.add_dir(system, "fmcache")
    database, log = _activity_log(tmp)
    fs.add_file(fmcache, "fmcache.db", database)
    fs.add_file(fmcache, "fmcache.db-wal", log)
    thumbs, _ = fs.add_dir(system, "thumbnails")
    for item, code in (("B0SYNTH001", "EBOK"), ("B0SYNTH002", "EBOK"), ("B0SYNTH777", "EBSP"),
                       ("B0SYNTHAU1", "AUDI")):
        fs.add_file(thumbs, "thumbnail_%s_%s_portrait.jpg" % (item, code),
                    b"\xff\xd8\xff\xe0" + item.encode("ascii") * 20)
    browser, _ = fs.add_dir(system, "browser")
    kindle_browser, _ = fs.add_dir(browser, "kindle_browser")
    cache, _ = fs.add_dir(kindle_browser, "Cache")
    fs.add_file(cache, "data_1", _browser_block_file())
    code_cache, _ = fs.add_dir(kindle_browser, "Code Cache")
    fs.add_file(code_cache, "0f1e2d3c4b5a6978_0", _browser_simple_file())
    audible, _ = fs.add_dir(fs.root, "audible")
    fs.add_file(audible, "default.audible.db", _audible_db(tmp))
    sandbox, _ = fs.add_dir(fs.root, ".active_content_sandbox")
    store, _ = fs.add_dir(sandbox, "store")
    fs.add_file(store, "https_store.example_0.localstorage", _store_storage(tmp))

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
            "AZ0CAAA:14466", ms(3, 5, 1, 30),
            history=[("AZ0AAAA:120", ms(3, 4, 2, 11)), ("AZ0BAAA:9000", ms(3, 5, 0, 58))])),
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
