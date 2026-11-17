"""The examiner report: one HTML file built from a parsed acquisition.

Reads kindle_parsed.db only. Every value from the device is escaped. The
file has no scripts and no outside links, so it can be opened, printed
or sent as it is.
"""

import base64
import html
import os
import sqlite3
from pathlib import Path

from . import __version__
from .util import hash_file, utc_now

_CSS = """
body{font:14px/1.45 "Segoe UI",Arial,sans-serif;color:#1c1c1c;margin:0;background:#f3f3f1}
main{max-width:1180px;margin:0 auto;padding:24px 32px 60px;background:#fff}
header{display:flex;align-items:center;gap:18px;border-bottom:3px solid #c98a12;padding-bottom:14px}
header img{width:72px;height:72px}
h1{font-size:24px;margin:0}
h2{font-size:18px;margin:34px 0 8px;border-bottom:1px solid #bbb;padding-bottom:4px}
h3{font-size:15px;margin:22px 0 6px}
p{margin:6px 0}
.sub{color:#555}
table{border-collapse:collapse;width:100%;margin:8px 0 14px;font-size:12.5px}
th,td{border:1px solid #cfcfcf;padding:4px 7px;text-align:left;vertical-align:top}
th{background:#ececea}
td.n,th.n{text-align:right;white-space:nowrap}
td.t{white-space:nowrap}
td.h{font-family:Consolas,monospace;font-size:11.5px;word-break:break-all}
.tiles{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0}
.tile{border:1px solid #cfcfcf;border-top:3px solid #1f6aa5;padding:8px 14px;min-width:120px}
.tile b{display:block;font-size:22px}
.bar{display:inline-block;height:9px;background:#1f6aa5;vertical-align:middle}
.del{background:#fbf1dc}
.none{color:#777;font-style:italic}
nav a{margin-right:14px}
@media print{body{background:#fff}main{padding:0;max-width:none}h2{page-break-after:avoid}
tr{page-break-inside:avoid}nav{display:none}}
"""


def _e(value):
    return html.escape("" if value is None else str(value))


def _n(value):
    try:
        return "{:,}".format(int(value))
    except (TypeError, ValueError):
        return _e(value)


def _table(headers, rows, classes=None, row_class=None):
    """headers: [text]. rows: [[cell]]. classes: per-column td class."""
    if not rows:
        return '<p class="none">No records.</p>'
    classes = classes or [""] * len(headers)
    out = ["<table><tr>"]
    out += ['<th class="%s">%s</th>' % ("n" if c == "n" else "", _e(h))
            for h, c in zip(headers, classes)]
    out.append("</tr>")
    for index, row in enumerate(rows):
        mark = row_class(index) if row_class else ""
        out.append('<tr class="%s">' % mark)
        for cell, cls in zip(row, classes):
            text = _n(cell) if cls == "n" else _e(cell)
            out.append('<td class="%s">%s</td>' % (cls, text))
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def find_parsed(folder):
    """The folder that holds kindle_parsed.db: `folder` or its parsed/ subfolder."""
    for candidate in (Path(folder), Path(folder) / "parsed"):
        if (candidate / "kindle_parsed.db").is_file():
            return candidate
    raise ValueError("no kindle_parsed.db in %s. Run parse first." % folder)


class _Db:
    def __init__(self, path):
        self.con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        self.con.row_factory = sqlite3.Row
        self.tables = set(r[0] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))

    def rows(self, table, order=""):
        if table not in self.tables:
            return []
        sql = 'SELECT * FROM "%s"' % table
        return [dict(r) for r in self.con.execute(sql + (" ORDER BY " + order if order else ""))]


def write_report(folder, out_path=None):
    """Write the report. Returns its path."""
    parsed = find_parsed(folder)
    out_path = Path(out_path) if out_path else parsed / "report.html"
    db = _Db(parsed / "kindle_parsed.db")
    try:
        if "device_summary" not in db.tables:
            raise ValueError("%s was parsed by an older version. Parse the acquisition "
                             "again." % parsed)
        text = _render(db)
    finally:
        db.con.close()
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    digest = hash_file(out_path, ("sha256",))["sha256"]
    with open(str(out_path) + ".sha256", "w", encoding="utf-8", newline="\n") as fh:
        fh.write("%s  %s\n" % (digest, out_path.name))
    return str(out_path)


def _logo():
    path = Path(__file__).resolve().parent / "assets" / "keat.png"
    try:
        return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return ""


def _render(db):
    summary = db.rows("device_summary")
    value = {}
    for row in summary:
        value.setdefault((row["section"], row["item"]), row["value"])

    def fact(section, item, default=""):
        return value.get((section, item), default)

    documents = db.rows("documents")
    annotations = db.rows("annotations", "book_key, created_utc")
    clippings = db.rows("clippings", "entry")
    pairs = db.rows("clock_offsets")
    timeline = db.rows("timeline", "rowid")
    observations = db.rows("observations", "rowid")
    lookups = db.rows("vocabulary_lookups", "lookup_utc")
    activity = db.rows("activity_by_day", "day")
    errors = db.rows("parse_errors")
    known = db.rows("known_matches")
    clip_by_entry = dict((c["entry"], c) for c in clippings)
    clip_for = dict(((p["sidecar_file"], p["annotation_utc"], p["kind"]),
                     clip_by_entry.get(p["clipping_entry"])) for p in pairs)

    present = [d for d in documents if d["state"] == "file present"]
    personal = [d for d in present if d["kind"] == "personal document"]
    protected = [d for d in present if d["protected"]]
    live = [a for a in annotations if a["file_state"] == "allocated"]
    deleted_notes = len(annotations) - len(live)
    part = []
    w = part.append

    title = " ".join(p for p in (fact("Acquisition", "case"),
                                 fact("Acquisition", "evidence item")) if p)
    w("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>")
    w("<title>%s</title>" % _e("Kindle examination record " + title).strip())
    w("<style>%s</style></head><body><main>" % _CSS)
    logo = _logo()
    w("<header>%s<div><h1>Kindle examination record</h1>" % (
        '<img src="%s" alt="">' % logo if logo else ""))
    line = [("Case", fact("Acquisition", "case")),
            ("Evidence item", fact("Acquisition", "evidence item")),
            ("Examiner", fact("Acquisition", "examiner")),
            ("Device serial", fact("Device", "serial number"))]
    w('<p class="sub">%s</p>' % " &nbsp;|&nbsp; ".join(
        "%s: %s" % (label, _e(v)) for label, v in line if v))
    w('<p class="sub">Written %s by KEAT %s</p></div></header>' % (_e(utc_now()),
                                                                 _e(__version__)))
    w('<nav><p><a href="#summary">Summary</a><a href="#documents">Documents</a><a href="#annotations">Annotations</a>'
      '<a href="#clippings">Clippings</a><a href="#deleted">Deleted records</a>'
      '<a href="#activity">Activity by day</a><a href="#timeline">Timeline</a>'
      '<a href="#device">Device and acquisition</a><a href="#limits">Times and limits</a></p></nav>')

    # --- summary -----------------------------------------------------------------
    w('<h2 id="summary">Summary</h2>')
    sentences = ["Files in the acquisition: %s. Document files: %s." % (
        _n(fact("Content", "files", 0)), _n(len(present)))]
    sentences.append("Personal documents among the document files: %s. Document files in "
                     "a DRM-protected container, copied as stored: %s." % (
                         _n(len(personal)), _n(len(protected))))
    sentences.append("Annotations in live sidecar files: %s. Annotations in deleted "
                     "sidecar files that were read back: %s." % (_n(len(live)),
                                                                 _n(deleted_notes)))
    sentences.append("Entries in My Clippings.txt: %s." % _n(len(clippings)))
    if fact("Deleted", "deleted entries listed"):
        sentences.append("Deleted entries listed by the file system: %s. Deleted files "
                         "read back: %s." % (_n(fact("Deleted", "deleted entries listed")),
                                             _n(fact("Deleted", "deleted files read back", 0))))
    if known:
        sentences.append("Files that match a known hash: %s." % _n(len(known)))
    sentences.append("Records that could not be parsed: %s." % _n(len(errors)))
    w("<p>%s</p>" % " ".join(sentences))
    w('<div class="tiles">')
    for label, number in (("Document files", len(present)),
                          ("Personal documents", len(personal)),
                          ("Annotations", len(annotations)), ("Clippings", len(clippings)),
                          ("Word lookups", len(lookups)),
                          ("Deleted entries", fact("Deleted", "deleted entries listed", 0)),
                          ("Timeline events", len(timeline))):
        w('<div class="tile"><b>%s</b>%s</div>' % (_n(number), _e(label)))
    w("</div>")

    # --- documents -----------------------------------------------------------------
    w('<h2 id="documents">Documents</h2>')
    w("<p>One row per document file, deleted document entry, or sidecar folder with no "
      "document file. Full hashes are in the appendix.</p>")

    def document_rows(rows):
        return [[d["title"], d["authors"], d["kind"], d["state"], d["container"],
                 d["highlights"], d["notes"], d["bookmarks"],
                 d["annotations_in_deleted_sidecars"], d["clippings"],
                 d["created_stored"], d["last_activity_utc"], d["size"]] for d in rows]

    headers = ["Title", "Author", "Kind", "State", "Container", "Highlights", "Notes",
               "Bookmarks", "In deleted sidecars", "Clippings", "Created (stored)",
               "Last activity (UTC)", "Bytes"]
    classes = ["", "", "", "", "", "n", "n", "n", "n", "n", "t", "t", "n"]
    user_loaded = [d for d in documents if d["kind"] in (
        "personal document", "no identifier in the file name")]
    store = [d for d in documents if d not in user_loaded]
    w("<h3>Personal documents and files with no store identifier</h3>")
    w(_table(headers, document_rows(user_loaded), classes,
             lambda i: "del" if user_loaded[i]["state"] != "file present" else ""))
    w("<h3>Store books, samples and audiobooks</h3>")
    w(_table(headers, document_rows(store), classes,
             lambda i: "del" if store[i]["state"] != "file present" else ""))

    # --- annotations -----------------------------------------------------------------
    w('<h2 id="annotations">Annotations by document</h2>')
    w("<p>A sidecar file records the position and time of a highlight. The highlighted "
      "text comes from the My Clippings.txt entry made in the same second, where one "
      "exists.</p>")
    title_of = dict((d["book_key"], d["title"]) for d in documents)
    by_book = {}
    for note in annotations:
        by_book.setdefault(note["book_key"], []).append(note)
    if not by_book:
        w('<p class="none">No records.</p>')
    for key in sorted(by_book, key=lambda k: (title_of.get(k, k) or k).lower()):
        notes = by_book[key]
        w("<h3>%s</h3>" % _e(title_of.get(key) or key))
        rows = []
        for note in notes:
            clip = clip_for.get((note["sidecar_file"], note["created_utc"], note["kind"])) or {}
            rows.append([note["created_utc"], note["kind"], note["start_position"],
                         clip.get("page", ""), clip.get("location", ""),
                         note["note"] or "", clip.get("text", ""), note["file_state"]])
        w(_table(["Created (UTC)", "Kind", "Position", "Page", "Location", "Note text",
                  "Clipping text", "Sidecar state"], rows,
                 ["t", "", "", "", "", "", "", ""],
                 lambda i, notes=notes: "del" if notes[i]["file_state"] != "allocated" else ""))

    # --- clippings ---------------------------------------------------------------------
    w('<h2 id="clippings">My Clippings.txt</h2>')
    w(_table(["Entry", "Added (device local)", "Title", "Author", "Kind", "Page",
              "Location", "Text"],
             [[c["entry"], c["added_device_local"], c["title"], c["author"], c["kind"],
               c["page"], c["location"], c["text"]] for c in clippings],
             ["n", "t", "", "", "", "", "", ""]))
    if lookups:
        w("<h3>Word lookups</h3>")
        w(_table(["Time (UTC)", "Word", "Book", "Usage"],
                 [[r["lookup_utc"], r["word"], r["book_title"], r["usage"]] for r in lookups],
                 ["t", "", "", ""]))

    # --- deleted and observations ----------------------------------------------------------
    w('<h2 id="deleted">Deleted records and cross-checks</h2>')
    w("<p>Each row is one record that differs between two sources on the device. The "
      "check names the two sources.</p>")
    w(_table(["Check", "Item", "Detail"],
             [[o["check"], o["item"], o["detail"]] for o in observations]))
    if known:
        w("<h3>Known hash matches</h3>")
        w(_table(["Path", "Matches", "Bytes", "SHA-256"],
                 [[k["rel_path"], k["known_match"], k["size"], k["sha256"]] for k in known],
                 ["", "", "n", "h"]))

    # --- activity ---------------------------------------------------------------------------
    w('<h2 id="activity">Activity by day</h2>')
    w("<p>Counts of timeline events by date. File system dates are excluded from the bar. "
      "Each event is counted on the date in its own time basis.</p>")
    rows, peak = [], 1
    for day in activity:
        use = (day["annotations_and_positions"] + day["clippings"] + day["lookups"]
               + day["reading_statistics"] + day["queued_activity_records"])
        peak = max(peak, use)
        rows.append((day, use))
    if rows:
        w("<table><tr><th>Day</th><th class='n'>Annotations and positions</th>"
          "<th class='n'>Clippings</th><th class='n'>Lookups</th>"
          "<th class='n'>Reading statistics</th><th class='n'>Queued records</th>"
          "<th class='n'>Files created</th><th class='n'>Files modified</th>"
          "<th class='n'>Deleted entry times</th><th>Use</th></tr>")
        for day, use in rows:
            w("<tr><td class='t'>%s</td>" % _e(day["day"]))
            for column in ("annotations_and_positions", "clippings", "lookups",
                           "reading_statistics", "queued_activity_records",
                           "files_created", "files_modified", "deleted_entry_times"):
                w("<td class='n'>%s</td>" % _n(day[column]))
            w("<td><span class='bar' style='width:%dpx'></span></td></tr>" % (
                round(160.0 * use / peak)))
        w("</table>")
    else:
        w('<p class="none">No records.</p>')

    # --- timeline ----------------------------------------------------------------------------
    w('<h2 id="timeline">Timeline</h2>')
    shown = [t for t in timeline if t["source"] != "file system"]
    w("<p>%s events from sidecar files, clippings and device databases. The %s file "
      "system events are in csv/timeline.csv and in the timeline table.</p>" % (
          _n(len(shown)), _n(len(timeline) - len(shown))))
    w(_table(["Time", "Basis", "Source", "Event", "Title", "Detail", "State"],
             [[t["time"], t["time_basis"].split(",")[0], t["source"], t["event"], t["title"],
               t["detail"], t["file_state"]] for t in shown],
             ["t", "", "", "", "", "", ""],
             lambda i: "del" if shown[i]["file_state"].startswith("deleted") else ""))

    # --- device and acquisition ----------------------------------------------------
    w('<h2 id="device">Device and acquisition</h2>')
    for section in ("Device", "Acquisition", "Content", "Records", "Deleted", "Times"):
        rows = [[r["item"], r["value"], r["source"]] for r in summary
                if r["section"] == section]
        if rows:
            w("<h3>%s</h3>" % _e(section))
            w(_table(["Item", "Value", "Source"], rows))

    # --- times and limits -----------------------------------------------------------------------
    w('<h2 id="limits">Times and limits</h2>')
    w("<ul>"
      "<li>Sidecar and database times are milliseconds since 1970 written by the device "
      "clock. They are shown in Coordinated Universal Time (UTC).</li>"
      "<li>My Clippings.txt times are the device's local clock time with no time zone.</li>"
      "<li>File system times are FAT directory values as stored, with no time zone. The "
      "tested devices write them in UTC.</li>"
      "<li>A device clock offset comes from a clipping and a sidecar annotation made in "
      "the same second. It applies to that moment only.</li>"
      "<li>A wrong device clock makes every device time wrong by the same amount.</li>"
      "<li>A deleted file was read back as one unbroken run of unallocated clusters. Its "
      "content is not confirmed to be the original file.</li>"
      "<li>DRM-protected files were copied as stored. Their text was not read.</li>"
      "<li>The model name comes from a community list of serial number codes. Confirm it "
      "on the device.</li>"
      "</ul>")

    # --- appendix -------------------------------------------------------------------------------
    w("<h2>Appendix: document hashes</h2>")
    w(_table(["Path", "State", "Bytes", "SHA-256"],
             [[d["rel_path"], d["state"], d["size"], d["sha256"]] for d in documents
              if d["sha256"]], ["", "", "n", "h"]))
    if errors:
        w("<h2>Appendix: records not parsed</h2>")
        w(_table(["File", "Parser", "Error"],
                 [[r["file"], r["parser"], r["error"]] for r in errors]))
    w("</main></body></html>\n")
    return "".join(part)


def describe(path):
    return "Examiner report written to %s (%d bytes)" % (path, os.path.getsize(path))
