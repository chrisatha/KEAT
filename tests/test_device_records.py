"""Tests for the write-ahead log reader and the device-use records."""

import os
import random
import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kindle_acquire import device_records  # noqa: E402
from kindle_acquire.parsers import wal  # noqa: E402
import synthetic_kindle  # noqa: E402


def varint(value):
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


def record(values):
    """A SQLite record for ints, text, None and bytes."""
    header, body = b"", b""
    for value in values:
        if value is None:
            header += varint(0)
        elif isinstance(value, int):
            header += varint(6)
            body += struct.pack(">q", value)
        elif isinstance(value, bytes):
            header += varint(12 + 2 * len(value))
            body += value
        else:
            raw = value.encode("utf-8")
            header += varint(13 + 2 * len(raw))
            body += raw
    return varint(len(header) + 1) + header + body


def leaf_page(rows, size=1024):
    """A table leaf page holding [(rowid, values)]."""
    page = bytearray(size)
    end = size
    pointers = b""
    for rowid, values in rows:
        payload = record(values)
        cell = varint(len(payload)) + varint(rowid) + payload
        end -= len(cell)
        page[end:end + len(cell)] = cell
        pointers += struct.pack(">H", end)
    page[0] = 0x0D
    page[3:5] = struct.pack(">H", len(rows))
    page[5:7] = struct.pack(">H", end)
    page[8:8 + len(pointers)] = pointers
    return bytes(page)


def wal_file(frames, size=1024, salts=(1, 2)):
    """A write-ahead log. frames: [(page number, page bytes)] or with a salt pair."""
    out = struct.pack(">8I", 0x377F0682, 3007000, size, 0, salts[0], salts[1], 0, 0)
    for frame in frames:
        number, page = frame[0], frame[1]
        salt = frame[2] if len(frame) > 2 else salts
        out += struct.pack(">6I", number, 0, salt[0], salt[1], 0, 0) + page
    return out


class WalReader(unittest.TestCase):
    def test_rows_of_every_page_version(self):
        old = leaf_page([(1, [None, "first", 10]), (2, [None, "second", 20])])
        new = leaf_page([(3, [None, "third", 30])])
        rows = wal.all_rows(b"", wal_file([(2, old), (2, new)]))
        self.assertEqual([(r[2], r[3]) for r in rows],
                         [(1, [None, "first", 10]), (2, [None, "second", 20]),
                          (3, [None, "third", 30])])
        self.assertEqual(rows[0][0], "write-ahead log frame 1, generation 1")
        self.assertEqual(rows[2][0], "write-ahead log frame 2, generation 1")

    def test_frames_left_from_an_earlier_generation(self):
        page = leaf_page([(7, [None, "kept", 1])])
        rows = wal.all_rows(b"", wal_file([(2, page), (2, page, (9, 9))]))
        self.assertEqual([r[0] for r in rows],
                         ["write-ahead log frame 1, generation 1",
                          "write-ahead log frame 2, generation 2"])

    def test_values_of_each_storage_class(self):
        page = leaf_page([(5, [None, 2 ** 40, "text", b"\x00\x01", ""])])
        rows = wal.all_rows(b"", wal_file([(2, page)]))
        self.assertEqual(rows[0][2:], (5, [None, 2 ** 40, "text", b"\x00\x01", ""], True))

    def test_row_that_continues_on_an_overflow_page(self):
        page = bytearray(1024)
        payload = record([None, "short", "x" * 3000])
        # SQLite keeps 103 + (size - 103) % 1020 bytes of this row on a 1024-byte page.
        kept = 103 + (len(payload) - 103) % 1020
        cell = varint(len(payload)) + varint(4) + payload[:kept] + struct.pack(">I", 9)
        page[0] = 0x0D
        page[3:5] = struct.pack(">H", 1)
        page[8:10] = struct.pack(">H", 20)
        page[20:20 + len(cell)] = cell
        rows = wal.all_rows(b"", wal_file([(2, bytes(page))]))
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0][4])
        self.assertEqual(rows[0][3], [None, "short"])

    def test_not_a_database(self):
        with self.assertRaises(wal.WalError):
            wal.all_rows(b"", b"")
        with self.assertRaises(wal.WalError):
            wal.all_rows(b"plain text" * 40, b"\x00" * 64)
        mismatch = b"SQLite format 3\x00" + struct.pack(">H", 4096) + b"\x00" * 4078
        with self.assertRaises(wal.WalError):
            wal.all_rows(mismatch, wal_file([], size=1024))

    def test_real_sqlite_database_and_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.db")
            con = sqlite3.connect(path)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, word TEXT, n INT)")
            con.executemany("INSERT INTO t (word, n) VALUES (?, ?)",
                            [("alpha", 1), ("beta", 2), ("gamma", 3)])
            con.commit()
            con.execute("DELETE FROM t WHERE word = 'beta'")
            con.commit()
            with open(path, "rb") as fh:
                data = fh.read()
            with open(path + "-wal", "rb") as fh:
                log = fh.read()
            live = [r[0] for r in con.execute("SELECT word FROM t")]
            con.close()
        words = set(r[3][1] for r in wal.all_rows(data, log)
                    if len(r[3]) == 3 and isinstance(r[3][1], str))
        self.assertEqual(live, ["alpha", "gamma"])
        self.assertEqual(words, {"alpha", "beta", "gamma"})     # the deleted row is found

    def test_damaged_pages_do_not_raise(self):
        rng = random.Random(20260116)
        page = leaf_page([(i, [None, "word %d" % i, i * 1000, "tail"]) for i in range(1, 12)])
        base = wal_file([(2, page), (3, page), (2, page)])
        for _ in range(400 * int(os.environ.get("KEAT_FUZZ", "1"))):
            data = bytearray(base)
            for _ in range(rng.randint(1, 30)):
                data[rng.randrange(32, len(data))] = rng.randrange(256)
            if rng.random() < 0.3:
                data = data[:rng.randrange(len(data))]
            try:
                wal.all_rows(b"", bytes(data))
            except wal.WalError:
                pass


class ActivityLog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            cls.data, cls.log = synthetic_kindle._activity_log(tmp)

    def _tables(self, current=None):
        return device_records.activity_log(self.data, self.log, "fmcache.db", current or {})

    def test_removed_records_are_found_in_the_log(self):
        tables = self._tables()
        records = tables["activity_records"][1]
        self.assertEqual(len(records), 19)
        self.assertEqual([r["created_timestamp"] for r in records],
                         sorted(r["created_timestamp"] for r in records))
        self.assertEqual(sum(1 for r in records if r["schema_name"] == "ereader_book_page_turn"), 6)

    def test_current_rows_are_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fmcache.db")
            with open(path, "wb") as fh:
                fh.write(self.data)
            with open(path + "-wal", "wb") as fh:
                fh.write(self.log)
            con = sqlite3.connect(path)
            con.row_factory = sqlite3.Row
            current = dict((name, [dict(r) for r in con.execute("SELECT * FROM " + name)])
                           for name in ("records", "app_sessions", "reading_sessions"))
            con.close()
        tables = self._tables(current)
        states = [r["state"] for r in tables["activity_records"][1]]
        self.assertEqual(states.count(device_records.CURRENT), 3)
        self.assertEqual(states.count(device_records.EARLIER), 16)
        sessions = tables["reading_sessions"][1]
        self.assertEqual([s["state"] for s in sessions],
                         [device_records.EARLIER, device_records.CURRENT])

    def test_reading_session_figures(self):
        first, second = self._tables()["reading_sessions"][1]
        self.assertEqual(first["asin"], "B0SYNTH001")
        self.assertEqual(first["duration_seconds"], 418.0)
        self.assertEqual(first["is_complete"], "yes")
        self.assertEqual(first["page_turns"], 5)
        self.assertEqual(first["open_position"], 9100)
        self.assertEqual((first["first_position_read"], first["last_position_read"]),
                         (9100, 9500))
        self.assertEqual(first["close_method"], "GoingToScreensaver")
        self.assertEqual(second["end_utc"], "")
        self.assertEqual(second["is_complete"], "no")
        self.assertEqual(second["page_turns"], 1)

    def test_app_sessions_connectivity_and_power(self):
        tables = self._tables()
        apps = tables["app_sessions"][1]
        self.assertEqual([a["time_zone"] for a in apps], ["America/Chicago"] * 2)
        self.assertEqual(apps[0]["duration_seconds"], 431.0)
        self.assertEqual([c["connection_state"] for c in tables["connectivity"][1]],
                         ["wifi", "none", "wifi"])
        self.assertEqual([p["detail"] for p in tables["power_events"][1]],
                         ["ACTIVE -> SCREEN SAVER",
                          "POWERD_WAKEUP_REASON_POWER_BUTTON after 3600000 ms in hibernation"])

    def test_damaged_log_gives_rows_or_a_clean_error(self):
        rng = random.Random(77)
        for _ in range(60 * int(os.environ.get("KEAT_FUZZ", "1"))):
            log = bytearray(self.log)
            for _ in range(rng.randint(1, 200)):
                log[rng.randrange(len(log))] = rng.randrange(256)
            try:
                device_records.activity_log(self.data, bytes(log), "fmcache.db", {})
            except wal.WalError:
                pass


class BrowserCache(unittest.TestCase):
    ROW = {"rel_path": "system/browser/kindle_browser/Cache/data_1", "size": "0",
           "created_stored": "2026-03-12T18:40:00"}

    def test_block_file_entries(self):
        rows = device_records.browser_block_file(synthetic_kindle._browser_block_file(), self.ROW)
        self.assertEqual([(r["created_utc"], r["host"], r["body_bytes"]) for r in rows],
                         [("2026-03-12T18:30:00.000+00:00", "portal.example.net", 4100),
                          ("2026-03-12T18:34:00.000+00:00", "www.example.org", 15250)])
        self.assertEqual(rows[1]["url"], "https://www.example.org/harbour/tides")

    def test_simple_file_entry_and_index(self):
        entry = device_records.browser_cache_entry(synthetic_kindle._browser_simple_file(),
                                                   self.ROW)
        self.assertEqual(entry["host"], "static.example.org")
        self.assertEqual(entry["created_utc"], "")
        index = struct.pack("<QIII", 0xFCFB6D1BA7725C30, 5, 0, 0) + b"\x00" * 64
        self.assertIsNone(device_records.browser_cache_entry(index, self.ROW))
        self.assertIsNone(device_records.browser_cache_entry(b"not a cache file" * 4, self.ROW))

    def test_damaged_block_file_does_not_raise(self):
        rng = random.Random(5)
        base = synthetic_kindle._browser_block_file()
        for _ in range(300 * int(os.environ.get("KEAT_FUZZ", "1"))):
            data = bytearray(base)
            for _ in range(rng.randint(1, 60)):
                data[rng.randrange(len(data))] = rng.randrange(256)
            device_records.browser_block_file(bytes(data[:rng.randrange(8000, len(data) + 1)]),
                                              self.ROW)


if __name__ == "__main__":
    unittest.main()
