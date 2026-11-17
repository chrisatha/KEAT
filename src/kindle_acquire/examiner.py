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
from .util import hash_file, open_db_readonly, utc_now

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


_USE_COLUMNS = ("annotations_and_positions", "clippings", "lookups", "activity_log",
                "audiobooks", "store_pages", "reading_statistics")
_FILE_COLUMNS = ("browser_cache", "files_created", "files_modified", "deleted_entry_times")


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
        self.con = open_db_readonly(path)
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
    sessions = db.rows("reading_sessions", "rowid")
    apps = db.rows("app_sessions", "rowid")
    records = db.rows("activity_records", "rowid")
    connectivity = db.rows("connectivity", "rowid")
    power = db.rows("power_events", "rowid")
    audio_positions = db.rows("audiobook_positions", "rowid")
    audio_links = db.rows("audiobook_links", "rowid")
    store_rows = db.rows("store_storage", "rowid")
    cache = db.rows("browser_cache", "rowid")
    thumbs = db.rows("thumbnails", "rowid")
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
    # Second line of defence behind the escaping: the browser runs no script and
    # loads nothing from outside the file.
    w("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
      "img-src data:; style-src 'unsafe-inline'; base-uri 'none'; "
      "form-action 'none'\">")
    w('<meta name="referrer" content="no-referrer">')
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
      '<a href="#clippings">Clippings</a><a href="#sessions">Reading sessions</a>'
      '<a href="#audio">Audiobooks</a><a href="#network">Network and power</a>'
      '<a href="#web">Store and browser</a><a href="#deleted">Deleted records</a>'
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
    sentences.append("Reading sessions in the activity log: %s. Activity records: %s, of "
                     "which %s are no longer in the database and were read from earlier "
                     "page versions." % (
                         _n(len(sessions)), _n(len(records)),
                         _n(sum(1 for r in records if r["state"] != "current row"))))
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
                          ("Reading sessions", len(sessions)),
                          ("Activity records", len(records)),
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

    # --- reading sessions ------------------------------------------------------------------
    w('<h2 id="sessions">Reading sessions</h2>')
    w("<p>One row per reading session in the device's activity log. The device rewrites a "
      "session while it runs. The last version found is shown. Page turns are the page "
      "turn records that carry the session's number.</p>")
    w(_table(["Session", "Title", "ASIN", "Start (UTC)", "End (UTC)", "Seconds", "Complete",
              "Page turns", "Records", "First position", "Last position", "Closed by",
              "State"],
             [[r["reading_session_id"], r["title"], r["asin"], r["start_utc"], r["end_utc"],
               r["duration_seconds"], r["is_complete"], r["page_turns"], r["records"],
               r["first_position_read"], r["last_position_read"], r["close_method"],
               r["state"]] for r in sessions],
             ["n", "", "", "t", "t", "n", "", "n", "n", "n", "n", "", ""],
             lambda i: "del" if sessions[i]["state"] != "current row" else ""))
    if apps:
        w("<h3>App sessions</h3>")
        w("<p>The device opens an app session when it wakes and records its settings in "
          "it.</p>")
        w(_table(["Session", "Start (UTC)", "End (UTC)", "Seconds", "Time zone", "Records",
                  "State"],
                 [[r["app_session_id"], r["start_utc"], r["end_utc"], r["duration_seconds"],
                   r["time_zone"], r["records"], r["state"]] for r in apps],
                 ["n", "t", "t", "n", "", "n", ""],
                 lambda i: "del" if apps[i]["state"] != "current row" else ""))
    kinds = {}
    for record in records:
        entry = kinds.setdefault(record["schema_name"], [0, record["created_utc"], ""])
        entry[0] += 1
        entry[2] = record["created_utc"]
    if kinds:
        w("<h3>Activity records by kind</h3>")
        w(_table(["Kind", "Records", "First (UTC)", "Last (UTC)"],
                 [[name, v[0], v[1], v[2]] for name, v in sorted(kinds.items())],
                 ["", "n", "t", "t"]))

    # --- audiobooks ------------------------------------------------------------------------
    w('<h2 id="audio">Audiobooks</h2>')
    w(_table(["Title", "ASIN", "Type", "Position", "Created (UTC)", "Updated (UTC)",
              "Set by", "LTO"],
             [[r["title"], r["asin"], r["type"], r["position"], r["created_utc"],
               r["updated_utc"], r["source_device"], r["lto"]] for r in audio_positions],
             ["", "", "", "t", "t", "t", "", ""]))
    if audio_links:
        pairs_seen = {}
        for link in audio_links:
            key = (link["ebook_asin"], link["audiobook_asin"])
            entry = pairs_seen.setdefault(key, [link, set()])
            entry[1].add(link["ebook_format"] or "")
        w("<h3>Books linked to audiobooks</h3>")
        w(_table(["Book", "Book ASIN", "Book formats", "Audiobook", "Audiobook ASIN",
                  "Owned", "Last fetched (UTC)"],
                 [[link["ebook_title"], link["ebook_asin"], ", ".join(sorted(formats)),
                   link["audiobook_title"], link["audiobook_asin"], link["audiobook_owned"],
                   link["last_fetched_utc"]] for link, formats in pairs_seen.values()],
                 ["", "", "", "", "", "", "t"]))

    # --- network and power -------------------------------------------------------------------
    w('<h2 id="network">Network and power</h2>')
    w("<p>Connection changes the device logged. The log names the kind of connection. It "
      "does not name the network.</p>")
    w(_table(["Time (UTC)", "Connection", "State", "Changed", "App session", "Record state"],
             [[r["time_utc"], r["connection_name"], r["connection_state"], r["is_changed"],
               r["app_session_id"], r["state"]] for r in connectivity],
             ["t", "", "", "", "n", ""]))
    if power:
        w("<h3>Power and screen</h3>")
        w(_table(["Time (UTC)", "Event", "Detail", "Record state"],
                 [[r["time_utc"], r["event"], r["detail"], r["state"]] for r in power],
                 ["t", "", "", ""]))

    # --- store and browser ---------------------------------------------------------------------
    w('<h2 id="web">Store and browser</h2>')
    w("<h3>Store page data</h3>")
    w(_table(["Time (UTC)", "Kind", "Address", "ASIN", "Bytes"],
             [[r["time_utc"], r["kind"], r["url"] or r["key"], r["asin"], r["value_bytes"]]
              for r in store_rows], ["t", "", "", "", "n"]))
    hosts = {}
    for entry in cache:
        when = entry["created_utc"] or entry["created_stored"]
        item = hosts.setdefault(entry["host"] or "(no host)", [0, when, ""])
        item[0] += 1
        item[1] = min(item[1], when)
        item[2] = max(item[2], when)
    w("<h3>Browser cache by host</h3>")
    w("<p>Each cache entry is one item the browser saved. An entry record carries its "
      "creation time in UTC. A cache file without one is dated by its stored file time. "
      "The full addresses are in the browser_cache table.</p>")
    w(_table(["Host", "Entries", "First created", "Last created"],
             [[host, v[0], v[1], v[2]] for host, v in sorted(hosts.items())],
             ["", "n", "t", "t"]))
    if thumbs:
        codes = {}
        for thumb in thumbs:
            entry = codes.setdefault(thumb["content_code"], [set(), set()])
            entry[0].add(thumb["item_id"])
            if thumb["document_present"] == "yes":
                entry[1].add(thumb["item_id"])
        w("<h3>Cover thumbnails</h3>")
        w("<p>The device keeps a cover for each item it has listed. A cover does not show "
          "that the item was downloaded.</p>")
        w(_table(["Content code", "Items", "Items with a document file present"],
                 [[code, len(v[0]), len(v[1])] for code, v in sorted(codes.items())],
                 ["", "n", "n"]))

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
    w("<p>Counts of timeline events by date. File system and browser cache dates are "
      "excluded from the bar. "
      "Each event is counted on the date in its own time basis.</p>")
    rows, peak = [], 1
    for day in activity:
        use = sum(day[column] for column in _USE_COLUMNS)
        peak = max(peak, use)
        rows.append((day, use))
    if rows:
        w("<table><tr><th>Day</th>")
        for column in _USE_COLUMNS + _FILE_COLUMNS:
            w("<th class='n'>%s</th>" % _e(column.replace("_", " ").capitalize()))
        w("<th>Use</th></tr>")
        for day, use in rows:
            w("<tr><td class='t'>%s</td>" % _e(day["day"]))
            for column in _USE_COLUMNS + _FILE_COLUMNS:
                w("<td class='n'>%s</td>" % _n(day[column]))
            w("<td><span class='bar' style='width:%dpx'></span></td></tr>" % (
                round(160.0 * use / peak)))
        w("</table>")
    else:
        w('<p class="none">No records.</p>')

    # --- timeline ----------------------------------------------------------------------------
    w('<h2 id="timeline">Timeline</h2>')
    shown = [t for t in timeline if t["source"] not in ("file system", "browser cache")
             and (t["source"] != "activity log" or t["event"].startswith("reading session"))]
    w("<p>%s events from sidecar files, clippings, device databases and reading sessions. "
      "The other %s events are file system times, browser cache entries and single "
      "activity records. They are in csv/timeline.csv and in the timeline table.</p>" % (
          _n(len(shown)), _n(len(timeline) - len(shown))))
    w(_table(["Time", "Basis", "Source", "Event", "Title", "Detail", "State"],
             [[t["time"], t["time_basis"].split(",")[0], t["source"], t["event"], t["title"],
               t["detail"], t["file_state"]] for t in shown],
             ["t", "", "", "", "", "", ""],
             lambda i: "del" if shown[i]["file_state"].startswith("deleted") else ""))

    # --- device and acquisition ----------------------------------------------------
    w('<h2 id="device">Device and acquisition</h2>')
    for section in ("Device", "Library", "Acquisition", "Content", "Records", "Deleted",
                    "Times"):
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
      "<li>An activity record marked as an earlier page version was read from an old "
      "copy of a database page. It was in the database once. It has since been removed "
      "or changed.</li>"
      "<li>The activity log covers the records the device had not yet discarded. It is "
      "not a full history of use.</li>"
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
