"""Regenerate the README screenshots from synthetic data.

    python tools/make_screenshots.py

Builds the invented Kindle from tests/synthetic_kindle.py (made-up titles,
notes and serial number), runs Extract and Parse on it in the Qt window, and
saves one picture per screen to docs/screenshots, with one of the examiner
report. No device is needed and
no real data is shown. Temporary paths are replaced before each picture.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from kindle_acquire import gui_qt  # noqa: E402
from kindle_acquire.gui_qt import QtWidgets  # noqa: E402
from synthetic_kindle import build_image  # noqa: E402

SHOTS = REPO / "docs" / "screenshots"
SHOWN_ROOT = r"C:\Cases\2026-0114"


def report_shot(report, target):
    """A picture of the examiner report, taken with Edge when it is installed."""
    for root in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        edge = Path(root or "") / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if edge.is_file():
            break
    else:
        print("Edge not found: report.png not rebuilt")
        return
    profile = tempfile.mkdtemp(prefix="keat_edge_")
    subprocess.run([str(edge), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--user-data-dir=%s" % profile, "--window-size=1240,1250",
                    "--screenshot=%s" % target, report.resolve().as_uri()],
                   check=False, timeout=120, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)


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
    window.table_pick.setCurrentIndex(window.table_pick.findData("timeline"))
    pump()
    window.filter_box.setText("device")
    pump()
    shot("timeline.png")
    window.close()
    report_shot(real_out / "parsed" / "report.html", SHOTS / "report.png")
    print("saved to %s" % SHOTS)
    for path in sorted(SHOTS.glob("*.png")):
        print("  %s  %d bytes" % (path.name, path.stat().st_size))


if __name__ == "__main__":
    main()
