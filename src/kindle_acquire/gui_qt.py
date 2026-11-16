"""Qt window for KEAT. Runs on PySide6 or PyQt6, whichever is installed.

Layout: device details in a fixed panel on the left. On the right, a menu
of actions with a description beside each one, sub-menus with a Back
button, and a progress screen while a step runs.

The acquisition code does not depend on Qt. This module shows the device,
starts the steps in jobs.py on a worker thread, and lets the examiner
browse the parsed tables.
"""

import html
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    Signal = QtCore.Signal
    BINDING = "PySide6"
except ImportError:  # the same API under another name
    from PyQt6 import QtCore, QtGui, QtWidgets
    Signal = QtCore.pyqtSignal
    BINDING = "PyQt6"

from . import __version__, jobs, util

Qt = QtCore.Qt
MONO = '"Cascadia Mono", Consolas, monospace'

BLUE = "#1f6aa5"
BLUE_DARK = "#144870"
PANEL = "#2b2b2b"
MAIN = "#333333"
TEXT = "#dce4ee"
MUTED = "#8d949b"
WARN = "#e8a33d"

STYLE = """
QWidget { font-family: "Segoe UI"; font-size: 10.5pt; color: %(text)s; }
QMainWindow, QWidget#page, QStackedWidget { background: %(main)s; }
QFrame#side { background: %(panel)s; }
QLabel#logo { font-size: 22pt; font-weight: 600; color: #ffffff; }
QLabel#logoSub { color: %(muted)s; font-size: 9pt; }
QLabel#sideHead { color: %(muted)s; font-size: 9pt; }
QLabel#device { font-family: %(mono)s; font-size: 10pt; }
QLabel#crumb { color: %(muted)s; font-size: 10pt; }
QLabel#desc { font-size: 10.5pt; }
QLabel#workTitle { font-size: 15pt; color: #ffffff; }
QLabel#status { color: %(text)s; }
QLabel#hint { color: %(muted)s; font-size: 9.5pt; }
QLabel#field { color: %(text)s; }
QPushButton#menu, QPushButton#menuDark { border: none; border-radius: 6px; font-size: 10.5pt;
    padding: 4px 10px; }
QPushButton#menu { background: %(blue)s; }
QPushButton#menu:hover { background: %(blue_dark)s; }
QPushButton#menuDark { background: %(panel)s; }
QPushButton#menuDark:hover { background: #3d3d3d; }
QPushButton#small { background: %(blue)s; border: none; border-radius: 6px; padding: 6px 18px;
    min-height: 18px; }
QPushButton#small:hover { background: %(blue_dark)s; }
QPushButton#small:disabled { background: #4a4d50; color: %(muted)s; }
QPushButton#small::menu-indicator { subcontrol-position: right center; right: 6px; }
QLineEdit, QComboBox { background: #343638; border: 2px solid #565b5e; border-radius: 6px;
    padding: 4px 8px; min-height: 20px; selection-background-color: %(blue)s; }
QLineEdit:focus, QComboBox:focus { border: 2px solid %(blue)s; }
QComboBox QAbstractItemView { background: %(panel)s; selection-background-color: %(blue_dark)s;
    border: 1px solid #565b5e; }
QProgressBar { background: #4a4d50; border: none; border-radius: 7px; min-height: 14px;
    max-height: 14px; }
QProgressBar::chunk { background: %(blue)s; border-radius: 7px; }
QPlainTextEdit#log, QPlainTextEdit#detail { background: #1d1e1e; border: none; border-radius: 6px;
    padding: 8px; font-family: %(mono)s; font-size: 9.5pt; color: %(text)s; }
QTableView { background: %(panel)s; alternate-background-color: #303030; gridline-color: #444444;
    border: none; border-radius: 6px; selection-background-color: %(blue_dark)s;
    selection-color: #ffffff; }
QHeaderView::section { background: #3a3a3a; border: none; border-right: 1px solid #444444;
    padding: 6px 8px; color: %(text)s; }
QTableCornerButton::section { background: #3a3a3a; border: none; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 18px; height: 18px; border: 2px solid #565b5e; border-radius: 5px;
    background: #343638; }
QCheckBox::indicator:checked { background: %(blue)s; border-color: %(blue)s; }
QMenu { background: %(panel)s; border: 1px solid #565b5e; padding: 4px; }
QMenu::item { padding: 6px 18px; border-radius: 4px; }
QMenu::item:selected { background: %(blue_dark)s; }
QSplitter::handle { background: %(main)s; height: 8px; }
""" % {"text": TEXT, "main": MAIN, "panel": PANEL, "muted": MUTED, "blue": BLUE,
       "blue_dark": BLUE_DARK, "mono": MONO}


class Cancelled(Exception):
    pass


class Bridge(QtCore.QObject):
    """Signals from worker threads to the window."""
    log = Signal(str)
    progress = Signal(str, object, object)
    output = Signal(str)
    device = Signal(object)
    parsed = Signal(str)
    finished = Signal(str, bool)


class RowsModel(QtCore.QAbstractTableModel):
    """One parsed table held in memory."""

    def __init__(self, columns, rows, parent=None):
        super().__init__(parent)
        self.columns = columns
        self.rows = rows

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        value = self.rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            if value is None:
                return ""
            text = str(value).replace("\r", " ").replace("\n", " ")
            return text if len(text) <= 400 else text[:400] + " ..."
        if role == Qt.ItemDataRole.UserRole:
            return value
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.columns[section]
        return str(section + 1)


def _label(text, name, wrap=False):
    label = QtWidgets.QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(wrap)
    return label


def _small(text, slot=None):
    button = QtWidgets.QPushButton(text)
    button.setObjectName("small")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if slot:
        button.clicked.connect(slot)
    return button


class Window(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.bridge = Bridge()
        self.cancel = threading.Event()
        self.busy = False
        self.detecting = False
        self.found = None
        self.last_output = None
        self.results_db = None
        self.model = None
        self.crumbs = []
        self._last_progress = 0.0
        self._return_to = None
        self.admin = jobs.is_admin()

        self.setWindowTitle("KEAT - Kindle E-Reader Acquisition Tool %s" % __version__)
        self.resize(1100, 640)
        self.setMinimumSize(1000, 600)

        central = QtWidgets.QWidget()
        central.setObjectName("page")
        self.setCentralWidget(central)
        row = QtWidgets.QHBoxLayout(central)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._build_side())
        self.stack = QtWidgets.QStackedWidget()
        row.addWidget(self.stack, 1)

        # Settings first: the menus read their widgets.
        self.page_settings = self._build_settings()
        self.page_main = self._menu_page([
            ("Full Acquisition", "Image the disk, extract files and deleted entries from\n"
             "the image, then parse. Use with a hardware write blocker.", self.full, False),
            ("Image the Disk", "Raw image of the Kindle's storage with MD5, SHA-1\n"
             "and SHA-256. Needs administrator rights.", self.image, False),
            ("Copy from\nMounted Volume", "Hashed copy of every file through Windows.\n"
             "Changes access dates unless the device is write-protected.", self.logical, False),
            ("Case and Output", "Case number, evidence item, examiner, output folder\n"
             "and known hashes.", lambda: self._show(self.page_settings), False),
            ("Switch to\nFile Operations", "Show options for operations with\n"
             "existing acquisitions.", lambda: self._show(self.page_files), True),
        ])
        self.page_files = self._menu_page([
            ("Extract from Image", "Read the FAT32 file system inside a raw image.\n"
             "Lists deleted entries and reads back what it can.", self.extract, False),
            ("Parse Acquisition", "Highlights, notes, reading positions, lookups and\n"
             "loaded documents into SQLite and CSV.", self.parse, False),
            ("Verify Acquisition", "Re-hash an acquisition folder against\n"
             "its own records.", self.verify, False),
            ("View Results", "Browse the tables of a parsed acquisition.",
             self._open_results, False),
        ], back=lambda: self._show(self.page_main))
        self.page_work = self._build_work()
        self.page_results = self._build_results()
        for page in (self.page_main, self.page_files, self.page_settings,
                     self.page_work, self.page_results):
            self.stack.addWidget(page)
        self._update_crumbs()

        self.bridge.log.connect(self.log)
        self.bridge.progress.connect(self._on_progress)
        self.bridge.output.connect(self._on_output)
        self.bridge.device.connect(self._show_devices)
        self.bridge.parsed.connect(self._on_parsed)
        self.bridge.finished.connect(self._on_finished)

        self.log("KEAT %s (%s). Copies data as stored. Does not remove DRM." % (
            __version__, BINDING))
        if not self.admin:
            self.log("Not running as administrator. Imaging the disk will offer to restart elevated.")
        self._show_devices(None)
        QtCore.QTimer.singleShot(0, self.detect)

    # --- left panel ---------------------------------------------------------------
    def _build_side(self):
        side = QtWidgets.QFrame()
        side.setObjectName("side")
        side.setFixedWidth(340)
        layout = QtWidgets.QVBoxLayout(side)
        layout.setContentsMargins(24, 22, 20, 18)
        layout.setSpacing(4)
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(14)
        badge = QtWidgets.QLabel()
        pixmap = QtGui.QPixmap(jobs.asset("keat.png"))
        if not pixmap.isNull():
            ratio = self.devicePixelRatioF()
            pixmap = pixmap.scaled(int(64 * ratio), int(64 * ratio),
                                   Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            pixmap.setDevicePixelRatio(ratio)
            badge.setPixmap(pixmap)
            head.addWidget(badge)
        words = QtWidgets.QVBoxLayout()
        words.setSpacing(0)
        words.addStretch(1)
        words.addWidget(_label("KEAT", "logo"))
        words.addWidget(_label("Kindle E-Reader\nAcquisition Tool", "logoSub"))
        words.addStretch(1)
        head.addLayout(words, 1)
        layout.addLayout(head)
        layout.addSpacing(20)
        self.device_text = _label("", "device")
        self.device_text.setTextFormat(Qt.TextFormat.RichText)
        self.device_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.device_text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.device_text, 1)
        self.case_text = _label("", "device")
        self.case_text.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.case_text)
        layout.addSpacing(12)
        foot = QtWidgets.QHBoxLayout()
        foot.addWidget(_label("Administrator" if self.admin else "Standard user", "sideHead"))
        foot.addStretch(1)
        self.detect_button = _small("Refresh", self.detect)
        foot.addWidget(self.detect_button)
        layout.addLayout(foot)
        return side

    @staticmethod
    def _lines(pairs):
        """Aligned 'key: value' lines as rich text. A value may be (text, colour)."""
        width = max(len(key) for key, _ in pairs) + 1
        out = []
        for key, value in pairs:
            colour = None
            if isinstance(value, tuple):
                value, colour = value
            text = html.escape(str(value))
            if colour:
                text = '<span style="color:%s">%s</span>' % (colour, text)
            out.append('<span style="color:%s">%s</span> %s' % (
                MUTED, html.escape((key + ":").ljust(width)), text))
        return '<pre style="margin:0">%s</pre>' % "\n".join(out)

    def _show_devices(self, found):
        self.found = found
        self.detecting = False
        self.detect_button.setEnabled(True)
        devices = (found or {}).get("devices", [])
        policy_on = (found or {}).get("usb_write_protect_policy") == 1
        if found is None:
            pairs = [("Device", ("looking for a Kindle...", MUTED))]
        elif not devices:
            pairs = [("Device", ("no Kindle found", WARN)),
                     ("", ""), ("", ("Connect a Kindle by USB", MUTED)),
                     ("", ("and press Refresh.", MUTED))]
        elif len(devices) > 1:
            pairs = [("Device", ("%d Kindles connected" % len(devices), WARN)),
                     ("", ("Connect only one.", MUTED))]
        else:
            dev = devices[0]
            disk = dev.get("disk")
            pairs = [("Device", "Kindle"), ("Serial", dev["usb_serial"]),
                     ("USB ID", "%s:%s" % (dev["usb_vendor_id"], dev["usb_product_id"]))]
            if dev["mode"] == "mass-storage":
                pairs.append(("Mode", "mass storage"))
            elif dev["mode"] == "mtp":
                pairs.append(("Mode", ("MTP (copy not implemented)", WARN)))
            else:
                pairs.append(("Mode", (dev["mode"], WARN)))
            if disk:
                pairs += [("Disk", "PhysicalDrive%s" % disk["number"]),
                          ("Size", "%.2f GB" % (disk["size"] / 1e9)),
                          ("Sector", "%s bytes" % disk["logical_sector_size"])]
            for volume in dev.get("volumes") or []:
                pairs += [("Volume", "%s %s" % (volume["root"], volume["filesystem"])),
                          ("Label", volume["label"])]
            if disk and disk["is_read_only"]:
                pairs.append(("Write", "disk is read-only"))
            elif policy_on:
                pairs.append(("Write", "blocked by host policy"))
            else:
                pairs.append(("Write", ("not blocked", WARN)))
        self.device_text.setText(self._lines(pairs))

    def _update_case_text(self):
        case = self._case()
        pairs = [("Case", case["case_id"] or "-"), ("Evidence", case["evidence_id"] or "-"),
                 ("Examiner", case["examiner"] or "-")]
        self.case_text.setText(self._lines(pairs))

    def detect(self):
        if self.detecting or self.busy:
            return
        self.detecting = True
        self.detect_button.setEnabled(False)

        def work():
            try:
                from .device import detect
                self.bridge.device.emit(detect())
            except Exception as exc:  # shown to the examiner, never swallowed
                self.bridge.log.emit("Detect failed. %s: %s" % (type(exc).__name__, exc))
                self.bridge.device.emit({"devices": []})

        threading.Thread(target=work, daemon=True).start()

    # --- pages ----------------------------------------------------------------------
    def _page(self):
        page = QtWidgets.QWidget()
        page.setObjectName("page")
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(30, 20, 30, 22)
        layout.setSpacing(14)
        crumb = _label("", "crumb")
        self.crumbs.append(crumb)
        layout.addWidget(crumb)
        layout.addSpacing(4)
        return page, layout

    def _update_crumbs(self):
        text = "KEAT %s  -  Output: %s" % (__version__, self.out_base.text().strip())
        for crumb in self.crumbs:
            crumb.setText(text)
        self._update_case_text()

    def _menu_page(self, rows, back=None):
        page, layout = self._page()
        for text, description, slot, dark in rows:
            button = QtWidgets.QPushButton(text)
            button.setObjectName("menuDark" if dark else "menu")
            button.setFixedSize(200, 56)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(slot)
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(40)
            row.addWidget(button)
            row.addWidget(_label(description, "desc"), 1)
            layout.addLayout(row)
        layout.addStretch(1)
        if back:
            foot = QtWidgets.QHBoxLayout()
            foot.addStretch(1)
            button = _small("Back", back)
            button.setFixedWidth(140)
            foot.addWidget(button)
            layout.addLayout(foot)
        return page

    def _build_settings(self):
        page, layout = self._page()
        form = QtWidgets.QGridLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)
        form.setColumnStretch(1, 1)
        self.case_id = QtWidgets.QLineEdit()
        self.evidence_id = QtWidgets.QLineEdit()
        self.examiner = QtWidgets.QLineEdit()
        self.notes = QtWidgets.QLineEdit()
        self.out_base = QtWidgets.QLineEdit(str(Path.home() / "Documents" / "Kindle_Acquisitions"))
        self.known = QtWidgets.QLineEdit()
        self.known.setPlaceholderText("optional: folder of reference files, or a hash list")
        rows = (("Case number", self.case_id), ("Evidence item", self.evidence_id),
                ("Examiner", self.examiner), ("Notes", self.notes))
        for index, (text, widget) in enumerate(rows):
            form.addWidget(_label(text, "field"), index, 0)
            form.addWidget(widget, index, 1, 1, 2)
        form.addWidget(_label("Output folder", "field"), 4, 0)
        form.addWidget(self.out_base, 4, 1)
        form.addWidget(_small("Browse...", lambda: self._pick_folder_into(
            self.out_base, "Output folder")), 4, 2)
        pick = _small("Choose...")
        menu = QtWidgets.QMenu(pick)
        menu.addAction("Folder of reference files...",
                       lambda: self._pick_folder_into(self.known, "Folder of reference files"))
        menu.addAction("List of MD5 or SHA-256 values...", self._pick_known_file)
        pick.setMenu(menu)
        form.addWidget(_label("Known hashes", "field"), 5, 0)
        form.addWidget(self.known, 5, 1)
        form.addWidget(pick, 5, 2)
        layout.addLayout(form)
        self.verify_source = QtWidgets.QCheckBox(
            "When imaging, read the device a second time and compare (doubles imaging time)")
        self.verify_source.setChecked(True)
        layout.addWidget(self.verify_source)
        layout.addWidget(_label("Each run writes a new folder under the output folder, named "
                                "from the evidence item, the step and the time.", "hint", True))
        layout.addStretch(1)
        foot = QtWidgets.QHBoxLayout()
        foot.addStretch(1)
        back = _small("Back", lambda: self._show(self.page_main))
        back.setFixedWidth(140)
        foot.addWidget(back)
        layout.addLayout(foot)
        for edit in (self.case_id, self.evidence_id, self.examiner, self.out_base):
            edit.textChanged.connect(self._update_crumbs)
        return page

    def _build_work(self):
        page, layout = self._page()
        self.work_title = _label("", "workTitle")
        layout.addWidget(self.work_title)
        self.status = _label("Ready", "status")
        layout.addWidget(self.status)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        layout.addWidget(self.bar)
        self.log_box = QtWidgets.QPlainTextEdit()
        self.log_box.setObjectName("log")
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box, 1)
        foot = QtWidgets.QHBoxLayout()
        self.open_button = _small("Open output folder", self._open_output)
        self.open_button.setEnabled(False)
        self.results_button = _small("View results", lambda: self._show(self.page_results))
        self.results_button.setEnabled(False)
        self.cancel_button = _small("Cancel", self._cancel)
        self.back_button = _small("Back", self._leave_work)
        for button in (self.cancel_button, self.back_button):
            button.setFixedWidth(140)
        foot.addWidget(self.open_button)
        foot.addWidget(self.results_button)
        foot.addStretch(1)
        foot.addWidget(self.cancel_button)
        foot.addWidget(self.back_button)
        layout.addLayout(foot)
        return page

    def _build_results(self):
        page, layout = self._page()
        layout.setSpacing(10)
        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(10)
        self.table_pick = QtWidgets.QComboBox()
        self.table_pick.setMinimumWidth(240)
        self.table_pick.currentIndexChanged.connect(self._load_table)
        self.filter_box = QtWidgets.QLineEdit()
        self.filter_box.setPlaceholderText("Filter rows (any column)")
        self.filter_box.setClearButtonEnabled(True)
        self.row_count = _label("", "hint")
        bar.addWidget(_small("Open...", self._pick_results))
        bar.addWidget(self.table_pick)
        bar.addWidget(self.filter_box, 1)
        bar.addWidget(self.row_count)
        layout.addLayout(bar)
        self.results_path = _label("No results loaded. Run Parse, or open a parsed folder.", "hint")
        layout.addWidget(self.results_path)

        self.proxy = QtCore.QSortFilterProxyModel(self)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)
        self.proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self.filter_box.textChanged.connect(self._apply_filter)
        self.view = QtWidgets.QTableView()
        self.view.setModel(self.proxy)
        self.view.setAlternatingRowColors(True)
        self.view.setSortingEnabled(True)
        self.view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setWordWrap(False)
        self.view.verticalHeader().setDefaultSectionSize(26)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.horizontalHeader().setHighlightSections(False)
        self.view.selectionModel().currentRowChanged.connect(self._show_row)
        self.detail = QtWidgets.QPlainTextEdit()
        self.detail.setObjectName("detail")
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("Select a row to see every column in full.")
        split = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.view)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)
        foot = QtWidgets.QHBoxLayout()
        foot.addStretch(1)
        back = _small("Back", lambda: self._show(self.page_files))
        back.setFixedWidth(140)
        foot.addWidget(back)
        layout.addLayout(foot)
        return page

    # --- small helpers ----------------------------------------------------------
    def _show(self, page):
        self.stack.setCurrentWidget(page)

    def log(self, text):
        self.log_box.appendPlainText("%s  %s" % (time.strftime("%H:%M:%S"), text))

    def _ask(self, text):
        buttons = QtWidgets.QMessageBox.StandardButton
        return QtWidgets.QMessageBox.question(
            self, "KEAT", text, buttons.Yes | buttons.No, buttons.No) == buttons.Yes

    def _warn(self, text):
        QtWidgets.QMessageBox.warning(self, "KEAT", text)

    def _pick_folder_into(self, edit, title):
        chosen = QtWidgets.QFileDialog.getExistingDirectory(self, title, edit.text().strip())
        if chosen:
            edit.setText(os.path.normpath(chosen))

    def _pick_known_file(self):
        chosen, _ = QtWidgets.QFileDialog.getOpenFileName(self, "List of MD5 or SHA-256 values")
        if chosen:
            self.known.setText(os.path.normpath(chosen))

    def _ask_folder(self, title):
        start = self.out_base.text().strip()
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, title, start if os.path.isdir(start) else "")
        return os.path.normpath(chosen) if chosen else ""

    def _open_output(self):
        if self.last_output and os.path.isdir(self.last_output):
            os.startfile(self.last_output)

    def _case(self):
        return {"case_id": self.case_id.text().strip(),
                "evidence_id": self.evidence_id.text().strip(),
                "examiner": self.examiner.text().strip(), "notes": self.notes.text().strip()}

    def _new_out(self, kind, device=None, fallback="kindle"):
        return jobs.new_output(self.out_base.text().strip(), self.evidence_id.text().strip(),
                               kind, device, fallback)

    def _device(self):
        device, problem = jobs.pick_device(self.found)
        if not device:
            self._warn(problem)
        return device

    # --- worker plumbing -----------------------------------------------------------
    def _hook(self, label, done, total, final):
        if self.cancel.is_set():
            raise Cancelled()
        now = time.monotonic()
        if total is not None and not final and now - self._last_progress < 0.15:
            return
        self._last_progress = now
        self.bridge.progress.emit(label, done, total)

    def _start(self, title, steps):
        if self.busy:
            return
        self.busy = True
        self.cancel.clear()
        if self.stack.currentWidget() is not self.page_work:
            self._return_to = self.stack.currentWidget()
        self.work_title.setText(title)
        self.status.setText("Starting...")
        self.bar.setRange(0, 0)
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self.back_button.setVisible(False)
        self.detect_button.setEnabled(False)
        self.log("--- %s ---" % title)
        self._show(self.page_work)

        def work():
            util.PROGRESS_HOOK = self._hook
            try:
                for step in steps:
                    if self.cancel.is_set():
                        raise Cancelled()
                    step()
                self.bridge.finished.emit("%s: finished" % title, True)
            except Cancelled:
                self.bridge.finished.emit(
                    "%s: CANCELLED. The output folder is incomplete and has no "
                    "acquisition log." % title, False)
            except Exception as exc:  # shown to the examiner, never swallowed
                self.bridge.finished.emit("%s: FAILED. %s: %s" % (
                    title, type(exc).__name__, exc), False)
            finally:
                util.PROGRESS_HOOK = None

        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, label, done, total):
        if total:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000.0 * done / total))
            self.status.setText("%s: %.1f%%   %.0f of %.0f MiB" % (
                label, 100.0 * done / total, done / 1048576.0, total / 1048576.0))
        else:
            self.bar.setRange(0, 0)
            self.status.setText(label)

    def _on_output(self, path):
        self.last_output = path
        self.open_button.setEnabled(True)

    def _on_parsed(self, folder):
        self.load_results(folder)

    def _on_finished(self, message, ok):
        self.bar.setRange(0, 1000)
        self.bar.setValue(1000 if ok else 0)
        self.status.setText("Finished" if ok else message.split(".")[0].split(": ", 1)[-1].title())
        self.log(message)
        self.busy = False
        self.cancel_button.setVisible(False)
        self.back_button.setVisible(True)
        self.detect_button.setEnabled(True)

    def _leave_work(self):
        self._show(self._return_to or self.page_main)

    def _cancel(self):
        if self.busy and self._ask("Stop the running operation?\n"
                                   "The output folder will be incomplete."):
            self.cancel.set()
            self.status.setText("Cancelling...")
            self.cancel_button.setEnabled(False)

    def closeEvent(self, event):
        if self.busy and not self._ask("An operation is running. Close and abandon it?"):
            event.ignore()
        else:
            event.accept()

    # --- steps (run on the worker thread) ----------------------------------------------
    def _say(self, text):
        self.bridge.log.emit(text)

    def _fresh(self, device):
        current, found = jobs.recheck(device)
        self.bridge.device.emit(found)
        return current

    def _step_image(self, device, out, case, verify_source):
        def step():
            self.bridge.output.emit(out)
            jobs.run_image(self._fresh(device), out, case, verify_source, self._say)
        return step

    def _step_logical(self, device, out, case):
        def step():
            self.bridge.output.emit(out)
            jobs.run_logical(self._fresh(device), out, case, self._say)
        return step

    def _step_extract(self, source, out, case):
        def step():
            self.bridge.output.emit(out)
            jobs.run_extract(source, out, case, self._say)
        return step

    def _step_parse(self, acquisition, known):
        def step():
            self.bridge.progress.emit("Parsing %s" % acquisition, 0, None)
            parsed = jobs.run_parse(acquisition, known, self._say)
            self.bridge.output.emit(parsed)
            self.bridge.parsed.emit(parsed)
        return step

    def _step_verify(self, acquisition):
        def step():
            self.bridge.progress.emit("Verifying %s" % acquisition, 0, None)
            jobs.run_verify(acquisition, self._say)
        return step

    # --- actions ---------------------------------------------------------------------------
    def _need_admin(self):
        if self.admin:
            return True
        if self._ask("Imaging reads the raw disk and needs administrator rights.\n\n"
                     "Restart KEAT as administrator?"):
            if jobs.relaunch_elevated():
                self.close()
            else:
                self._warn("The elevated restart was refused.")
        return False

    def image(self):
        device = self._device()
        if device and self._need_admin():
            self._start("Image the Disk", [self._step_image(
                device, self._new_out("image", device), self._case(),
                self.verify_source.isChecked())])

    def full(self):
        device = self._device()
        if device and self._need_admin():
            image_out = self._new_out("image", device)
            extract_out = self._new_out("extract", device)
            case = self._case()
            self._start("Full Acquisition", [
                self._step_image(device, image_out, case, self.verify_source.isChecked()),
                self._step_extract(image_out, extract_out, case),
                self._step_parse(extract_out, self.known.text().strip())])

    def logical(self):
        device = self._device()
        if not device:
            return
        if not device["volumes"]:
            self._warn("The Kindle disk has no mounted volume.")
            return
        if not jobs.is_write_protected(device) and not self._ask(
                "The volume is mounted read-write.\n\n"
                "Copying files through Windows changes access dates on the device. "
                "For evidence, use a write blocker and image the disk.\n\n"
                "Copy the files anyway?"):
            return
        self._start("Copy from Mounted Volume", [self._step_logical(
            device, self._new_out("logical", device), self._case())])

    def extract(self):
        source = self._ask_folder("Image acquisition folder")
        if source:
            out = self._new_out("extract", fallback=os.path.basename(source))
            self._start("Extract from Image", [self._step_extract(source, out, self._case())])

    def parse(self):
        acquisition = self._ask_folder("Logical or extracted acquisition folder")
        if acquisition:
            self._start("Parse Acquisition",
                        [self._step_parse(acquisition, self.known.text().strip())])

    def verify(self):
        acquisition = self._ask_folder("Acquisition folder")
        if acquisition:
            self._start("Verify Acquisition", [self._step_verify(acquisition)])

    # --- results viewer ------------------------------------------------------------------------
    def _open_results(self):
        if not self.results_db:
            self._pick_results()
        if self.results_db:
            self._show(self.page_results)

    def _pick_results(self):
        folder = self._ask_folder("Parsed folder, or an acquisition folder that holds one")
        if folder:
            self.load_results(folder)

    def load_results(self, folder):
        """Open kindle_parsed.db from `folder` or from its parsed/ subfolder."""
        candidates = [os.path.join(folder, "kindle_parsed.db"),
                      os.path.join(folder, "parsed", "kindle_parsed.db")]
        path = next((p for p in candidates if os.path.isfile(p)), None)
        if not path:
            self._warn("No kindle_parsed.db in\n%s" % folder)
            return
        try:
            con = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
            tables = [(name, con.execute('SELECT COUNT(*) FROM "%s"' % name.replace('"', '""'))
                       .fetchone()[0])
                      for (name,) in con.execute(
                          "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            con.close()
        except sqlite3.Error as exc:
            self._warn("Cannot read %s\n%s" % (path, exc))
            return
        self.results_db = path
        self.results_path.setText(path)
        self.results_button.setEnabled(True)
        self.table_pick.blockSignals(True)
        self.table_pick.clear()
        for name, count in tables:
            self.table_pick.addItem("%s  (%d)" % (name, count), name)
        self.table_pick.blockSignals(False)
        preferred = [i for i, (name, count) in enumerate(tables)
                     if name == "annotations" and count]
        first_filled = [i for i, (_n, count) in enumerate(tables) if count]
        self.table_pick.setCurrentIndex((preferred or first_filled or [0])[0])
        self._load_table()

    def _load_table(self, *_):
        name = self.table_pick.currentData()
        if not name or not self.results_db:
            return
        try:
            con = sqlite3.connect(Path(self.results_db).as_uri() + "?mode=ro", uri=True)
            cur = con.execute('SELECT * FROM "%s"' % name.replace('"', '""'))
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()
            con.close()
        except sqlite3.Error as exc:
            self._warn("Cannot read table %s\n%s" % (name, exc))
            return
        self.model = RowsModel(columns, rows, self)
        self.view.setSortingEnabled(False)
        self.proxy.setSourceModel(self.model)
        self.view.setSortingEnabled(True)
        self.view.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.view.resizeColumnsToContents()
        for column in range(len(columns)):
            if self.view.columnWidth(column) > 240:
                self.view.setColumnWidth(column, 240)
        self.detail.clear()
        self._apply_filter(self.filter_box.text())

    def _apply_filter(self, text):
        self.proxy.setFilterFixedString(text)
        total = self.proxy.sourceModel().rowCount() if self.proxy.sourceModel() else 0
        shown = self.proxy.rowCount()
        self.row_count.setText("%d rows" % total if shown == total
                               else "%d of %d rows" % (shown, total))

    def _show_row(self, current, _previous=None):
        if not current.isValid() or self.model is None:
            self.detail.clear()
            return
        row = self.model.rows[self.proxy.mapToSource(current).row()]
        width = max(len(c) for c in self.model.columns)
        lines = []
        for column, value in zip(self.model.columns, row):
            text = "" if value is None else str(value)
            pad = "\n" + " " * (width + 3)
            lines.append("%-*s   %s" % (width, column, text.replace("\r\n", "\n").replace("\n", pad)))
        self.detail.setPlainText("\n".join(lines))


def _dark_palette():
    """A fixed dark palette, so dialogs and menus match the style sheet."""
    palette = QtGui.QPalette()
    role = QtGui.QPalette.ColorRole
    for name, colour in (("Window", MAIN), ("WindowText", TEXT), ("Base", PANEL),
                         ("AlternateBase", "#303030"), ("Text", TEXT), ("Button", "#3a3a3a"),
                         ("ButtonText", TEXT), ("Highlight", BLUE), ("HighlightedText", "#ffffff"),
                         ("ToolTipBase", PANEL), ("ToolTipText", TEXT),
                         ("PlaceholderText", MUTED), ("Link", "#5aa9e6")):
        palette.setColor(getattr(role, name), QtGui.QColor(colour))
    disabled = QtGui.QPalette.ColorGroup.Disabled
    for name in ("WindowText", "Text", "ButtonText"):
        palette.setColor(disabled, getattr(role, name), QtGui.QColor(MUTED))
    return palette


def apply_style(app):
    icon = QtGui.QIcon(jobs.asset("keat.png"))
    if not icon.isNull():
        app.setWindowIcon(icon)
    app.setStyle("Fusion")
    app.setPalette(_dark_palette())
    app.setStyleSheet(STYLE)


def main():
    jobs.set_app_id()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    apply_style(app)
    window = Window()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
