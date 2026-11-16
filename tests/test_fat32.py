"""FAT32 reader and image extraction, tested on images built in memory."""

import csv
import hashlib
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindle_acquire.extract import extract_image  # noqa: E402
from kindle_acquire.fat32 import (Fat32Error, Fat32Volume, _checksum,  # noqa: E402
                                  _first_alias_byte)
from kindle_acquire.parsers import krds  # noqa: E402
from kindle_acquire.report import parse_acquisition  # noqa: E402
from kindle_acquire.verify import verify  # noqa: E402

BPS = 512
SPC = 2
CLUSTER = BPS * SPC
RESERVED = 32
CLUSTERS = 400
EOC = 0x0FFFFFFF


def fat_date(y, m, d):
    return ((y - 1980) << 9) | (m << 5) | d


def fat_time(h, mi, s):
    return (h << 11) | (mi << 5) | (s // 2)


class MiniFat:
    """Builds a small FAT32-structured image."""

    def __init__(self):
        self.fat = [0] * (CLUSTERS + 2)
        self.fat[0], self.fat[1] = 0x0FFFFFF8, EOC
        self.data = bytearray(CLUSTERS * CLUSTER)
        self.dirs = {}
        self.alias = 0
        self.root = self.new_dir(None)

    def allocate(self, clusters):
        for a, b in zip(clusters, clusters[1:]):
            self.fat[a] = b
        self.fat[clusters[-1]] = EOC

    def free_run(self, count, start=2):
        c = start
        while True:
            if all(self.fat[c + i] == 0 for i in range(count)):
                return list(range(c, c + count))
            c += 1

    def put(self, clusters, payload):
        for i, c in enumerate(clusters):
            part = payload[i * CLUSTER:(i + 1) * CLUSTER]
            pos = (c - 2) * CLUSTER
            self.data[pos:pos + len(part)] = part

    def new_dir(self, parent, start=2):
        clusters = self.free_run(2, start)
        self.allocate(clusters)
        self.dirs[clusters[0]] = {"clusters": clusters, "entries": []}
        if parent is not None:
            self.dirs[clusters[0]]["entries"] += [
                self._short(b".          ", 0x10, clusters[0], 0),
                self._short(b"..         ", 0x10, 0 if parent == self.root else parent, 0)]
        return clusters[0]

    def _short(self, short, attr, first, size, case=0,
               when=(2026, 11, 15, 4, 13, 20), tenths=100):
        y, m, d, h, mi, s = when
        return struct.pack("<11sBBBHHHHHHHI", short, attr, case, tenths,
                           fat_time(h, mi, s), fat_date(y, m, d), fat_date(y, m, d),
                           first >> 16, fat_time(h, mi, s), fat_date(y, m, d),
                           first & 0xFFFF, size)

    def _long(self, name, short):
        raw = name.encode("utf-16-le") + b"\x00\x00"
        raw += b"\xff" * (-len(raw) % 26)
        parts = [raw[i:i + 26] for i in range(0, len(raw), 26)]
        out = []
        for seq in range(len(parts), 0, -1):
            p = parts[seq - 1]
            first = seq | (0x40 if seq == len(parts) else 0)
            out.append(bytes([first]) + p[0:10] + b"\x0f\x00" + bytes([_checksum(short)])
                       + p[10:22] + b"\x00\x00" + p[22:26])
        return out

    def add(self, parent, name, attr=0x20, first=0, size=0, long_name=True,
            short=None, case=0):
        """Add an entry. Returns the indexes of the directory slots used."""
        if short is None:
            # Like a real driver: the alias starts with the name's first character.
            self.alias += 1
            lead = _first_alias_byte(name) or ord("F")
            short = bytes([lead]) + ("%07d" % self.alias).encode("ascii") + b"TXT"
        entries = self.dirs[parent]["entries"]
        start = len(entries)
        if long_name:
            entries += self._long(name, short)
        entries.append(self._short(short, attr, first, size, case))
        return list(range(start, len(entries)))

    def add_file(self, parent, name, payload, clusters=None, **kw):
        count = -(-len(payload) // CLUSTER)
        if count:
            clusters = clusters or self.free_run(count)
            self.allocate(clusters)
            self.put(clusters, payload)
        return self.add(parent, name, first=clusters[0] if count else 0,
                        size=len(payload), **kw), clusters

    def add_dir(self, parent, name, start=2):
        cluster = self.new_dir(parent, start)
        slots = self.add(parent, name, attr=0x10, first=cluster)
        return cluster, slots

    def delete(self, parent, slots, clusters):
        entries = self.dirs[parent]["entries"]
        for i in slots:
            entries[i] = b"\xe5" + entries[i][1:]
        for c in clusters or []:
            self.fat[c] = 0

    def build(self, lead_sectors=0):
        for info in self.dirs.values():
            raw = b"".join(info["entries"])
            assert len(raw) <= len(info["clusters"]) * CLUSTER
            self.put(info["clusters"], raw.ljust(len(info["clusters"]) * CLUSTER, b"\x00"))
        fat_sectors = -(-(CLUSTERS + 2) * 4 // BPS)
        total = RESERVED + 2 * fat_sectors + CLUSTERS * SPC
        boot = bytearray(BPS)
        boot[0:3] = b"\xeb\x58\x90"
        boot[3:11] = b"MSDOS5.0"
        struct.pack_into("<HBHBHHBH", boot, 11, BPS, SPC, RESERVED, 2, 0, 0, 0xF8, 0)
        struct.pack_into("<II", boot, 32, total, fat_sectors)
        struct.pack_into("<I", boot, 44, self.root)
        struct.pack_into("<I", boot, 67, 0x1234ABCD)
        boot[71:82] = b"KINDLE     "
        boot[82:90] = b"FAT32   "
        boot[510:512] = b"\x55\xaa"
        fat = struct.pack("<%dI" % len(self.fat), *self.fat).ljust(fat_sectors * BPS, b"\x00")
        volume = bytes(boot) + b"\x00" * ((RESERVED - 1) * BPS) + fat + fat + bytes(self.data)
        if not lead_sectors:
            return volume
        mbr = bytearray(BPS)
        mbr[446 + 4] = 0x0C
        struct.pack_into("<II", mbr, 446 + 8, lead_sectors, total)
        mbr[510:512] = b"\x55\xaa"
        return bytes(mbr) + b"\x00" * ((lead_sectors - 1) * BPS) + volume


def pattern(seed, length):
    block = hashlib.sha256(seed.encode()).digest()
    return (block * (length // len(block) + 1))[:length]


def build_sample():
    fs = MiniFat()
    content = {}
    fs.add_file(fs.root, "readme.txt", b"short name only", long_name=False,
                short=b"README  TXT", case=0x18)
    content["readme.txt"] = b"short name only"
    docs, _ = fs.add_dir(fs.root, "documents")
    # A fragmented file: three clusters that are not adjacent.
    frag = pattern("frag", 2 * CLUSTER + 300)
    fs.add_file(docs, "My Clippings.txt", frag, clusters=[40, 90, 60])
    content["documents/My Clippings.txt"] = frag
    fs.add_file(docs, "empty file.bin", b"")
    content["documents/empty file.bin"] = b""
    inner, _ = fs.add_dir(docs, "Ünïcode ☃ folder")
    uni = pattern("uni", 700)
    fs.add_file(inner, "exactly13char", uni)
    content["documents/Ünïcode ☃ folder/exactly13char"] = uni
    # Deleted, clusters still free: recoverable.
    gone = pattern("gone", 2 * CLUSTER + 500)
    slots, clusters = fs.add_file(docs, "deleted report draft.pdf", gone, clusters=[200, 201, 202])
    fs.delete(docs, slots, clusters)
    # Deleted, first cluster reused by a later file: not recoverable.
    lost = pattern("lost", CLUSTER)
    slots, clusters = fs.add_file(docs, "overwritten.pdf", lost, clusters=[210])
    fs.delete(docs, slots, clusters)
    newer = pattern("newer", CLUSTER)
    fs.add_file(docs, "newer file.txt", newer, clusters=[210])
    content["documents/newer file.txt"] = newer
    # Deleted with a short name only: the first character is lost.
    slots, clusters = fs.add_file(docs, "OLDNOTE.TXT", b"old note", long_name=False,
                                  short=b"OLDNOTE TXT", clusters=[220])
    fs.delete(docs, slots, clusters)
    # A deleted folder with one deleted file inside.
    trash, tslots = fs.add_dir(fs.root, "removed folder")
    inside = pattern("inside", 900)
    slots, clusters = fs.add_file(trash, "inside removed folder.txt", inside, clusters=[230])
    fs.delete(trash, slots, clusters)
    fs.delete(fs.root, tslots, fs.dirs[trash]["clusters"])
    # A deleted sidecar file holding one bookmark.
    def k_str(text):
        raw = text.encode("utf-8")
        return b"\x03\x00" + struct.pack(">H", len(raw)) + raw
    name = b"annotation.personal.bookmark"
    sidecar = (b"\x00\x00\x00\x00\x00\x1a\xb1\x26" + b"\xfe\x00" + struct.pack(">H", len(name))
               + name + k_str("AAAA:10") + k_str("AAAA:10")
               + b"\x02" + struct.pack(">q", 1794715200533) * 1
               + b"\x02" + struct.pack(">q", 1794715200533) + b"\xff")
    # Placed away from the other deleted folder so neither overwrites the other.
    sdr, sdr_slots = fs.add_dir(docs, "Old Book_B000000009.sdr", start=300)
    slots, clusters = fs.add_file(sdr, "Old Book_B000000009.yjr", sidecar, clusters=[240])
    fs.delete(sdr, slots, clusters)
    fs.delete(docs, sdr_slots, fs.dirs[sdr]["clusters"])
    recoverable = {"deleted report draft.pdf": gone, "_LDNOTE.TXT": b"old note",
                   "inside removed folder.txt": inside,
                   "Old Book_B000000009.yjr": sidecar}
    return fs, content, recoverable


class Fat32Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.fs, self.content, self.recoverable = build_sample()

    def tearDown(self):
        self._tmp.cleanup()

    def _image(self, lead_sectors=0):
        path = self.tmp / ("image_%d.dd" % lead_sectors)
        path.write_bytes(self.fs.build(lead_sectors))
        return path

    def test_volume_facts_and_walk(self):
        with open(self._image(), "rb", buffering=0) as fh:
            vol = Fat32Volume.open(fh)
            self.assertEqual(vol.serial, "1234-ABCD")
            self.assertEqual(vol.cluster_size, CLUSTER)
            self.assertEqual(vol.cluster_count, CLUSTERS)
            entries = dict(vol.walk())
        live = {p for p, e in entries.items() if not e["deleted"] and not e["is_dir"]}
        self.assertEqual(live, set(self.content))
        entry = entries["documents/My Clippings.txt"]
        self.assertEqual(entry["modified_stored"], "2026-11-15T04:13:20")
        self.assertEqual(entry["created_stored"], "2026-11-15T04:13:21")
        self.assertEqual(entry["accessed_stored"], "2026-11-15")
        self.assertEqual(entries["readme.txt"]["name_source"], "short")
        gone = entries["documents/deleted report draft.pdf"]
        self.assertTrue(gone["deleted"])
        self.assertEqual(gone["name_source"], "long")
        self.assertTrue(entries["removed folder"]["deleted"])
        self.assertTrue(entries["removed folder/inside removed folder.txt"]["deleted"])
        self.assertEqual(entries["documents/_LDNOTE.TXT"]["name_source"],
                         "short, first character lost")

    def test_partitioned_image(self):
        with open(self._image(lead_sectors=63), "rb", buffering=0) as fh:
            vol = Fat32Volume.open(fh)
            self.assertEqual(vol.offset, 63 * BPS)
            names = {p for p, e in vol.walk(include_deleted=False)}
        self.assertIn("documents/My Clippings.txt", names)
        self.assertNotIn("documents/deleted report draft.pdf", names)

    def test_not_fat32(self):
        path = self.tmp / "junk.dd"
        path.write_bytes(b"\x00" * 4096)
        with open(path, "rb", buffering=0) as fh:
            with self.assertRaises(Fat32Error):
                Fat32Volume.open(fh)

    def test_extract(self):
        out = self.tmp / "extracted"
        log = extract_image(str(self._image()), out, quiet=True)
        self.assertEqual(log["errors"], [])
        self.assertEqual(log["counts"]["files_ok"], len(self.content))
        for rel, data in self.content.items():
            self.assertEqual((out / "files" / rel).read_bytes(), data, rel)
        with open(out / "manifest.csv", newline="", encoding="utf-8") as fh:
            rows = {r["rel_path"]: r for r in csv.DictReader(fh)}
        frag = rows["documents/My Clippings.txt"]
        self.assertEqual(frag["sha256"],
                         hashlib.sha256(self.content["documents/My Clippings.txt"]).hexdigest())
        self.assertEqual(frag["modified_stored"], "2026-11-15T04:13:20")
        self.assertEqual(frag["modified_utc"], "")

        with open(out / "deleted_entries.csv", newline="", encoding="utf-8") as fh:
            deleted = {r["rel_path"].split("/")[-1]: r for r in csv.DictReader(fh)}
        self.assertEqual(set(deleted), {"deleted report draft.pdf", "overwritten.pdf",
                                        "_LDNOTE.TXT", "removed folder",
                                        "inside removed folder.txt",
                                        "Old Book_B000000009.sdr",
                                        "Old Book_B000000009.yjr"})
        for name, data in self.recoverable.items():
            row = deleted[name]
            self.assertEqual(row["recovery"], "recovered", name)
            self.assertEqual((out / row["recovered_file"]).read_bytes(), data)
            self.assertEqual(row["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(deleted["overwritten.pdf"]["recovery"], "not recovered")
        self.assertIn("in use", deleted["overwritten.pdf"]["detail"])
        self.assertEqual(deleted["removed folder"]["recovery"], "not attempted")
        self.assertEqual(log["counts"]["deleted_entries"], 7)
        self.assertEqual(log["counts"]["deleted_files_recovered"], 4)

        self.assertTrue(verify(out)["ok"])
        recovered = out / deleted["deleted report draft.pdf"]["recovered_file"]
        recovered.write_bytes(b"tampered")
        self.assertFalse(verify(out)["ok"])

    def test_parse_reads_an_extraction(self):
        out = self.tmp / "extracted"
        extract_image(str(self._image()), out, quiet=True)
        # Reference set: one live file by SHA-256, one deleted file by MD5.
        live = self.content["documents/newer file.txt"]
        gone = self.recoverable["deleted report draft.pdf"]
        known = self.tmp / "known.csv"
        known.write_text(
            "sha256,name\n%s,Policy.txt\n%s  Draft report.pdf\n%s,absent.pdf\n" % (
                hashlib.sha256(live).hexdigest().upper(), hashlib.md5(gone).hexdigest(),
                "0" * 64), encoding="utf-8")
        summary = parse_acquisition(out, known=str(known))
        self.assertEqual(summary["deleted_entries"], 7)
        self.assertEqual(summary["parse_errors"], 0)
        self.assertEqual(summary["known_matches"], 1)
        import sqlite3
        con = sqlite3.connect(str(out / "parsed" / "kindle_parsed.db"))
        # The bookmark in the deleted sidecar file is decoded and labelled.
        self.assertEqual(
            con.execute("SELECT book_key, file_state, kind, created_utc FROM annotations").fetchall(),
            [("documents/Old Book_B000000009", "deleted, read back", "bookmark",
              "2026-11-15T04:00:00.533+00:00")])
        self.assertEqual(
            con.execute("SELECT rel_path FROM known_matches").fetchall(),
            [("documents/newer file.txt",)])
        self.assertEqual(
            con.execute("SELECT rel_path FROM deleted_entries WHERE known_match <> ''").fetchall(),
            [("documents/deleted report draft.pdf",)])
        con.close()

    def test_known_folder(self):
        out = self.tmp / "extracted"
        extract_image(str(self._image()), out, quiet=True)
        ref = self.tmp / "reference"
        ref.mkdir()
        (ref / "original clippings.txt").write_bytes(self.content["documents/My Clippings.txt"])
        (ref / "unrelated.txt").write_bytes(b"not on the device")
        summary = parse_acquisition(out, known=str(ref))
        self.assertEqual(summary["known_matches"], 1)

    def test_broken_chain_is_reported(self):
        self.fs.fat[90] = 0  # cut the fragmented file's chain
        out = self.tmp / "extracted"
        log = extract_image(str(self._image()), out, quiet=True)
        self.assertEqual(log["counts"]["files_error"], 1)
        self.assertEqual(log["errors"][0]["rel_path"], "documents/My Clippings.txt")
        self.assertIn("unallocated", log["errors"][0]["detail"])


class HostileImageTests(unittest.TestCase):
    """Cases from review: odd names, damaged images, leftover directory slots."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, data):
        path = self.tmp / "image.dd"
        path.write_bytes(data)
        return str(path)

    def _walk(self, fs):
        with open(self._write(fs.build()), "rb", buffering=0) as fh:
            return list(Fat32Volume.open(fh).walk())

    def test_names_that_collide_or_are_reserved(self):
        fs = MiniFat()
        payloads = {}
        for name in ("a?b.txt", "a_b.txt", "Notes.txt", "NOTES.TXT", "NUL.txt", "x:y"):
            payloads[name] = pattern(name, 300)
            fs.add_file(fs.root, name, payloads[name])
        out = self.tmp / "out"
        log = extract_image(self._write(fs.build()), out, quiet=True)
        self.assertEqual(log["errors"], [])
        self.assertEqual(log["counts"]["files_ok"], 6)
        stored = {"a_b.txt": "a?b.txt", "a_b~2.txt": "a_b.txt", "Notes.txt": "Notes.txt",
                  "NOTES~2.TXT": "NOTES.TXT", "_NUL.txt": "NUL.txt", "x_y": "x:y"}
        for on_disk, original in stored.items():
            self.assertEqual((out / "files" / on_disk).read_bytes(), payloads[original], on_disk)
        with open(out / "manifest.csv", newline="", encoding="utf-8") as fh:
            rows = {r["rel_path"]: r for r in csv.DictReader(fh)}
        self.assertIn("'a?b.txt'", rows["a_b.txt"]["detail"])
        self.assertIn("'NOTES.TXT'", rows["NOTES~2.TXT"]["detail"])
        self.assertEqual(rows["Notes.txt"]["detail"], "")
        self.assertTrue(verify(out)["ok"])

    def test_traversal_names_stay_inside_the_output(self):
        fs = MiniFat()
        fs.add_file(fs.root, "../../escape.txt", b"x")
        fs.add_file(fs.root, "C:\\abs.txt", b"y")
        out = self.tmp / "out"
        extract_image(self._write(fs.build()), out, quiet=True)
        self.assertEqual(sorted(p.name for p in (out / "files").iterdir()),
                         [".._.._escape.txt", "C__abs.txt"])
        self.assertFalse((self.tmp / "escape.txt").exists())

    def test_truncated_image_still_gives_a_log(self):
        fs, content, _ = build_sample()
        image = fs.build()
        data_start = len(image) - CLUSTERS * CLUSTER
        out = self.tmp / "out"
        log = extract_image(self._write(image[:data_start + 148 * CLUSTER]), out, quiet=True)
        self.assertTrue((out / "acquisition_log.json").is_file())
        failed = {e["rel_path"]: e for e in log["errors"]}
        self.assertIn("documents/newer file.txt", failed)                 # allocated, past the end
        self.assertIn("documents/deleted report draft.pdf", failed)       # deleted, past the end
        self.assertEqual((out / "files" / "documents" / "My Clippings.txt").read_bytes(),
                         content["documents/My Clippings.txt"])
        self.assertTrue(verify(out)["ok"])

    def test_deleted_long_name_with_a_reused_slot(self):
        fs = MiniFat()
        name = "a rather long deleted file name over 26 chars.pdf"
        slots, clusters = fs.add_file(fs.root, name, b"data", clusters=[100])
        fs.delete(fs.root, slots, clusters)
        fs.dirs[fs.root]["entries"][slots[0]] = fs._short(b"NEWFILE TXT", 0x20, 0, 0)
        entries = {e["name"]: e for _p, e in self._walk(fs)}
        partial = [e for n, e in entries.items() if n.startswith("a rather long")]
        self.assertEqual(len(partial), 1)
        self.assertNotEqual(partial[0]["name"], name)
        self.assertEqual(partial[0]["name_source"], "long, may be incomplete")

    def test_deleted_entry_does_not_take_another_files_name(self):
        fs = MiniFat()
        # Deleted long-name parts of one file, then an unrelated deleted short entry.
        parts = fs._long("Quarterly board minutes.pdf", b"Q0000001PDF")
        entries = fs.dirs[fs.root]["entries"]
        entries += [b"\xe5" + p[1:] for p in parts]
        entries.append(b"\xe5" + fs._short(b"SECRET  TXT", 0x20, 0, 0)[1:])
        found = [e for _p, e in self._walk(fs)]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name_source"], "long, not confirmed by checksum")

    def test_stray_part_before_a_live_long_name(self):
        fs = MiniFat()
        stray = fs._long("zz", b"OTHER   TXT")[0]
        fs.dirs[fs.root]["entries"].append(b"\x01" + stray[1:])
        fs.add_file(fs.root, "My Clippings.txt", b"text")
        entries = self._walk(fs)
        self.assertEqual([(p, e["name_source"]) for p, e in entries],
                         [("My Clippings.txt", "long")])

    def test_deleted_folder_does_not_run_into_other_data(self):
        fs = MiniFat()
        folder, slots = fs.add_dir(fs.root, "full folder", start=300)
        for i in range(62):   # with "." and ".." this fills both clusters exactly
            fs.add(folder, "X", long_name=False, short=("E%07dTXT" % i).encode("ascii"))
        entries = fs.dirs[folder]["entries"]
        self.assertEqual(len(entries) * 32, 2 * CLUSTER)
        for i in range(2, len(entries)):
            entries[i] = b"\xe5" + entries[i][1:]
        fs.delete(fs.root, slots, fs.dirs[folder]["clusters"])
        fs.put([302, 303, 304], pattern("stale file data", 3 * CLUSTER))
        inside = [p for p, _e in self._walk(fs) if p.startswith("full folder/")]
        self.assertEqual(len(inside), 62)

    def test_two_names_for_one_chain_are_flagged(self):
        fs = MiniFat()
        slots, clusters = fs.add_file(fs.root, "first.bin", pattern("x", 3 * CLUSTER))
        fs.add(fs.root, "second.bin", first=clusters[0], size=3 * CLUSTER)
        log = extract_image(self._write(fs.build()), self.tmp / "out", quiet=True)
        self.assertEqual([e["rel_path"] for e in log["errors"]], ["second.bin"])
        self.assertIn("shares its clusters", log["errors"][0]["detail"])

    def test_deeply_nested_sidecar_is_refused(self):
        with self.assertRaises(krds.KrdsError):
            krds.decode(krds.SIGNATURE + b"\xfe\x01" * 5000)


if __name__ == "__main__":
    unittest.main()
