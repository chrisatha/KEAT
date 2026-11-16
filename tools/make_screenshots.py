"""Regenerate the README screenshots from synthetic data.

    python tools/make_screenshots.py

Builds a small FAT32 image of an invented Kindle (made-up titles, notes
and serial number), runs Extract and Parse on it in the Qt window, and
saves one picture per screen to docs/screenshots. No device is needed and
no real data is shown. Temporary paths are replaced before each picture.
"""

import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from kindle_acquire import gui_qt  # noqa: E402
from kindle_acquire.gui_qt import QtWidgets  # noqa: E402
from kindle_acquire.parsers import krds  # noqa: E402
from test_fat32 import MiniFat  # noqa: E402
from test_kindle_acquire import k_int, k_long, k_name, k_str  # noqa: E402

SHOTS = REPO / "docs" / "screenshots"
SHOWN_ROOT = r"C:\Cases\2026-0114"


def ms(month, day, hour, minute, second=0):
    moment = datetime(2026, month, day, hour, minute, second, tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def sidecar(annotations, position, read_ms):
    """A sidecar file. annotations: [(kind, start, end, created_ms, note)]."""
    parts = []
    for kind, start, end, created, note in annotations:
        parts.append(k_name("annotation.personal." + kind) + k_str(start) + k_str(end)
                     + k_long(created) + k_long(created) + k_str("0\ufffc0")
                     + (k_str(note) if note else b"") + b"\xff")
    tree = k_name("saved.avl.interval.tree") + k_int(len(parts)) + b"".join(parts) + b"\xff"
    cache = k_name("annotation.cache.object") + k_int(1) + k_int(1) + tree + b"\xff"
    lpr = k_name("lpr") + b"\x07\x02" + k_str(position) + k_long(read_ms) + b"\xff"
    return krds.SIGNATURE + k_long(1) + k_int(2) + cache + lpr


CLIPPINGS = "\ufeff" + "".join(
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


def build_image(tmp):
    """A FAT32 image of an invented Kindle, with two deleted entries."""
    fs = MiniFat()
    system, _ = fs.add_dir(fs.root, "system")
    fs.add_file(system, "version.txt", b"Kindle 5.17.1 (0000000 000)\n")
    vocabulary, _ = fs.add_dir(system, "vocabulary")
    db_path = tmp / "vocab.db"
    con = sqlite3.connect(str(db_path))
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
    fs.add_file(vocabulary, "vocab.db", db_path.read_bytes())

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
    slots, clusters = fs.add_file(items, "Draft_supplier_terms-cdeKey_11223344556677889900AABBCCDDEEFF.pdf",
                                  b"%PDF-1.7\n" + b"deleted draft\n" * 150, clusters=[330, 331, 332])
    fs.delete(items, slots, clusters)
    gone, gone_slots = fs.add_dir(items, "Harbour Pilot Manual_B0SYNTH004.sdr", start=340)
    slots, clusters = fs.add_file(gone, "Harbour Pilot Manual_B0SYNTH004.yjr", sidecar([
        ("highlight", "AW1AAAA:5020", "AW1AAAA:5077", ms(2, 21, 23, 40, 2), ""),
        ("bookmark", "AW1AAAA:6100", "AW1AAAA:6100", ms(2, 22, 0, 5, 51), "")],
        "AW1AAAA:6100", ms(2, 22, 0, 6)), clusters=[350])
    fs.delete(gone, slots, clusters)
    fs.delete(items, gone_slots, fs.dirs[gone]["clusters"])
    image = tmp / "KDL-01.dd"
    image.write_bytes(fs.build())
    return image


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="keat_shots_"))
    image = build_image(tmp)

    app = QtWidgets.QApplication(sys.argv)
    gui_qt.apply_style(app)
    window = gui_qt.Window()
    window.show()

    def pump(count=10):
        for _ in range(count):
            app.processEvents()
            time.sleep(0.02)

    def wait():
        pump(5)
        while window.busy or window.detecting:
            app.processEvents()
            time.sleep(0.02)
        pump()

    def scrub():
        """Replace the temporary folder with a neutral path wherever it shows."""
        for old in (str(tmp), str(tmp).replace("\\", "/")):
            window.log_box.setPlainText(window.log_box.toPlainText().replace(old, SHOWN_ROOT))
            window.results_path.setText(window.results_path.text().replace(old, SHOWN_ROOT))
        bar = window.log_box.verticalScrollBar()
        bar.setValue(bar.maximum())

    def shot(name):
        scrub()
        pump()
        window.grab().save(str(SHOTS / name))

    wait()
    window.log_box.clear()
    window.log("KEAT %s. Copies data as stored. Does not remove DRM." % gui_qt.__version__)
    window.case_id.setText("2026-0114")
    window.evidence_id.setText("KDL-01")
    window.examiner.setText("J. Rivera")
    window._show_devices({"usb_write_protect_policy": 1, "devices": [{
        "usb_vendor_id": "1949", "usb_product_id": "0324", "usb_serial": "G000XX0000000000",
        "mode": "mass-storage",
        "disk": {"number": 2, "size": 6702472704, "logical_sector_size": 512, "is_read_only": True},
        "volumes": [{"root": "E:\\", "filesystem": "FAT32", "label": "Kindle"}]}]})
    window.out_base.setText(SHOWN_ROOT)
    shot("main-menu.png")
    window._show(window.page_files)
    shot("file-operations.png")
    window._show(window.page_settings)
    shot("case-and-output.png")
    window._show(window.page_files)

    # Real runs on the synthetic image. The folders are temporary.
    real_out = tmp / "KDL-01_extract"
    window._start("Extract from Image", [
        window._step_extract(str(image), str(real_out), window._case())])
    wait()
    window._start("Parse Acquisition", [window._step_parse(str(real_out), "")])
    wait()
    shot("progress.png")

    window._open_results()
    window.table_pick.setCurrentIndex(window.table_pick.findData("annotations"))
    pump()
    window.view.sortByColumn(window.model.columns.index("created_utc"),
                             gui_qt.Qt.SortOrder.AscendingOrder)
    for row in range(window.proxy.rowCount()):
        index = window.proxy.index(row, window.model.columns.index("kind"))
        if window.proxy.data(index) == "note":
            window.view.selectRow(row)
            break
    shot("results.png")
    window.close()
    print("saved to %s" % SHOTS)
    for path in sorted(SHOTS.glob("*.png")):
        print("  %s  %d bytes" % (path.name, path.stat().st_size))


if __name__ == "__main__":
    main()
