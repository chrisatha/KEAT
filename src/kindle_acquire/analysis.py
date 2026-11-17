"""Analysis tables built from the parsed records.

Nothing here reads the device or the copied files. Each table is derived
from rows the parser already produced, and every row names the record it
came from. The tables state what the records hold. They do not interpret.
"""

import json
import os
import re
from datetime import datetime

# Time bases used in the timeline. A reader must not compare times across
# bases without the device clock offset (see the clock_offsets table).
UTC = "UTC, from the device clock"
LOCAL = "device local time, no time zone"
LOCAL_DATE = "device local date"
FAT = "FAT time as stored, no time zone"

_ASIN_TAIL = re.compile(r"_(B[0-9A-Z]{9})(_sample)?$")
_CDE_TAIL = re.compile(r"-cdeKey_[0-9A-Fa-f]{32}$")
_CONTENT_EXTENSIONS = (
    ".kfx", ".pdf", ".azw", ".azw3", ".azw4", ".azw8", ".mobi", ".prc", ".epub", ".txt",
    ".doc", ".docx", ".rtf", ".htm", ".html", ".kpf", ".tpz", ".aax", ".aa")

# Characters 4 to 6 of the serial number, from community device lists.
# The result is an indication. Confirm the model on the device itself.
_MODEL_CODES = {
    "Kindle Paperwhite, 10th generation (2018)": (
        "0PP 0T1 0T2 0T3 0T4 0T5 0T6 0T7 0TJ 0TK 0TL 0TM 0TN 102 103 16Q 16R 16S 16T "
        "16U 16V"),
    "Kindle Paperwhite, 11th generation (2021)": "1LG 1Q0 1PX 1VD 219 21A 2BH 2BJ 2DK",
    "Kindle, 11th generation (2022)": "22D 25T 23A 2AQ 2AP 1XH 22C",
    "Kindle Scribe (2022)": "27J 2BL 263 227 2BM 23L 23M 270",
}


def model_from_serial(serial):
    code = (serial or "")[3:6].upper()
    if len(serial or "") < 16 or not code:
        return ""
    for model, codes in _MODEL_CODES.items():
        if code in codes.split():
            return model
    return ""


def _norm(text):
    return re.sub(r"[^0-9a-z]+", "", (text or "").lower())


def _name_title(book_key):
    base = book_key.split("/")[-1]
    base = _CDE_TAIL.sub("", _ASIN_TAIL.sub("", base))
    return base.replace("_", " ").strip()


def _same_title(clipping_title, name_title, known_title):
    wanted = _norm(clipping_title)
    if not wanted:
        return False
    for candidate in (_norm(name_title), _norm(known_title)):
        if not candidate:
            continue
        if wanted == candidate:
            return True
        short, full = sorted((wanted, candidate), key=len)
        if len(short) >= 6 and full.startswith(short):
            return True
    return False


def _moment(text):
    """A parsed time without its zone, or None."""
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _display_names(text):
    """Titles and credits in freetime.db are JSON lists of display names."""
    try:
        items = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return ""
    names = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            name = item.get("name") if isinstance(item.get("name"), dict) else item
            if isinstance(name.get("display"), str):
                names.append(name["display"])
    return "; ".join(names)


def _offset_text(minutes):
    sign = "-" if minutes < 0 else "+"
    return "UTC%s%02d:%02d" % (sign, abs(minutes) // 60, abs(minutes) % 60)


def _ms(value):
    from .parsers.krds import ms_to_utc
    try:
        return ms_to_utc(int(value))
    except (TypeError, ValueError):
        return ""


class _Titles:
    """Title and author by ASIN from the device databases, else the file name."""

    def __init__(self, exported, lookups):
        self.by_asin = {}
        columns, rows = exported.get("freetime_bookmetadata", ((), ()))
        for row in rows:
            title = _display_names(row.get("titles"))
            if row.get("asin") and title:
                self.by_asin[row["asin"]] = (
                    title, _display_names(row.get("credits")),
                    "system/freetime/freetime.db")
        for row in lookups:
            if row.get("book_asin") and row.get("book_title"):
                self.by_asin.setdefault(row["book_asin"], (
                    row["book_title"], row.get("book_authors") or "", row["source_file"]))

    def of(self, book_key):
        asin = _ASIN_TAIL.search(book_key.split("/")[-1])
        if asin and asin.group(1) in self.by_asin:
            return self.by_asin[asin.group(1)]
        return _name_title(book_key), "", "file name"


def clock_offsets(clippings, annotations, titles):
    """Pair clippings with sidecar annotations made in the same second.

    A clipping carries device local time. The sidecar annotation carries
    UTC. A pair gives the offset of the device clock at that moment.
    Returns (pair rows, {clipping entry: annotation}).
    """
    pairs, matched = [], {}
    for clip in clippings:
        local = _moment(clip.get("added_device_local"))
        if local is None:
            continue
        candidates = []
        for note in annotations:
            created = _moment(note.get("created_utc"))
            if created is None or note.get("kind") != clip.get("kind"):
                continue
            seconds = (local - created).total_seconds()
            minutes = int(round(seconds / 900.0)) * 15
            if abs(seconds - minutes * 60) > 2 or not -12 * 60 <= minutes <= 14 * 60:
                continue
            title, _authors, _source = titles.of(note["book_key"])
            same = _same_title(clip.get("title"), _name_title(note["book_key"]), title)
            candidates.append((not same, note["file_state"] != "allocated", abs(minutes),
                               minutes, note))
        by_title = [c for c in candidates if not c[0]]
        if by_title:
            chosen, basis = min(by_title, key=lambda c: c[:3]), "title, kind and time"
        elif len(candidates) == 1:
            chosen, basis = candidates[0], "kind and time (titles differ)"
        else:
            continue
        minutes, note = chosen[3], chosen[4]
        matched[clip["entry"]] = note
        pairs.append({
            "clipping_entry": clip["entry"], "title": clip.get("title"),
            "kind": clip.get("kind"), "clipping_local": clip["added_device_local"],
            "annotation_utc": note["created_utc"], "offset_minutes": minutes,
            "offset": _offset_text(minutes), "matched_on": basis,
            "file_state": note["file_state"], "sidecar_file": note["sidecar_file"]})
    return pairs, matched


def build_documents(content, annotations, positions, clippings, lookups, deleted_rows,
                    sidecar_files, titles):
    rows, by_key = [], {}

    def new_row(key, state):
        title, authors, source = titles.of(key)
        base = key.split("/")[-1]
        asin = _ASIN_TAIL.search(base)
        cde = re.search(r"-cdeKey_([0-9A-Fa-f]{32})$", base)
        if key.lower().startswith("audible/"):
            kind = "audiobook"
        elif asin:
            kind = "store sample" if asin.group(2) else "store book"
        elif cde:
            kind = "personal document"
        else:
            kind = "no identifier in the file name"
        row = {"book_key": key, "title": title, "title_source": source, "authors": authors,
               "kind": kind, "state": state, "asin": asin.group(1) if asin else "",
               "cde_key": cde.group(1).upper() if cde else "",
               "highlights": 0, "notes": 0, "bookmarks": 0, "other_annotations": 0,
               "annotations_in_deleted_sidecars": 0, "clippings": 0, "lookups": 0,
               "_times": []}
        rows.append(row)
        return row

    for item in content:
        if os.path.basename(item["rel_path"]).lower() == "my clippings.txt":
            continue
        key = os.path.splitext(item["rel_path"])[0]
        row = new_row(key, "file present")
        row.update({k: item[k] for k in ("rel_path", "container", "size", "sha256",
                                         "created_stored", "modified_stored",
                                         "known_match")})
        row["protected"] = "yes" if "DRM" in item["container"] else ""
        by_key.setdefault(key, row)
    for side in sidecar_files:
        if side["book_key"] not in by_key:
            state = ("sidecar present, no content file" if side["file_state"] == "allocated"
                     else "deleted sidecar only")
            by_key[side["book_key"]] = new_row(side["book_key"], state)
        elif (by_key[side["book_key"]]["state"] == "deleted sidecar only"
              and side["file_state"] == "allocated"):
            by_key[side["book_key"]]["state"] = "sidecar present, no content file"
    for entry in deleted_rows:
        low = entry["rel_path"].lower()
        # A book's own resource files sit in an assets folder beside its sidecar.
        if (entry["type"] != "file" or not low.startswith("documents/") or ".sdr/" in low
                or "/assets/" in low
                or not low.endswith(_CONTENT_EXTENSIONS)
                or os.path.basename(low) == "my clippings.txt"):
            continue
        read_back = bool(entry.get("recovered_file"))
        row = new_row(os.path.splitext(entry["rel_path"])[0],
                      "deleted entry, read back" if read_back
                      else "deleted entry, not read back")
        row.update({"rel_path": entry["rel_path"], "size": entry["size"],
                    "sha256": entry.get("sha256", ""),
                    "created_stored": entry.get("created_stored", ""),
                    "modified_stored": entry.get("modified_stored", ""),
                    "known_match": entry.get("known_match", ""),
                    "deleted_entry_index": entry.get("index", "")})

    for note in annotations:
        row = by_key.get(note["book_key"])
        if row is None:
            continue
        if note["file_state"] != "allocated":
            row["annotations_in_deleted_sidecars"] += 1
        else:
            column = {"highlight": "highlights", "note": "notes",
                      "bookmark": "bookmarks"}.get(note["kind"], "other_annotations")
            row[column] += 1
        row["_times"] += [t for t in (note.get("created_utc"), note.get("modified_utc")) if t]
    for position in positions:
        row = by_key.get(position["book_key"])
        if row is None or not position.get("time_utc"):
            continue
        row["_times"].append(position["time_utc"])
        if (position["file_state"] == "allocated"
                and position["time_utc"] >= row.get("last_position_utc", "")):
            row["last_position_utc"] = position["time_utc"]
            row["last_position"] = position.get("position", "")
    for lookup in lookups:
        for row in rows:
            if row["asin"] and row["asin"] == lookup.get("book_asin"):
                row["lookups"] += 1
                if lookup.get("lookup_utc"):
                    row["_times"].append(lookup["lookup_utc"])
    for clip in clippings:
        for row in rows:
            if _same_title(clip.get("title"), _name_title(row["book_key"]), row["title"]):
                row["clippings"] += 1
    for row in rows:
        times = sorted(row.pop("_times"))
        row["first_activity_utc"] = times[0] if times else ""
        row["last_activity_utc"] = times[-1] if times else ""
    rows.sort(key=lambda r: (r["book_key"].lower(), r["state"]))
    return rows


DOCUMENT_COLUMNS = [
    "title", "authors", "kind", "state", "container", "protected", "highlights", "notes",
    "bookmarks", "other_annotations", "annotations_in_deleted_sidecars", "clippings",
    "lookups", "first_activity_utc", "last_activity_utc", "last_position",
    "last_position_utc", "created_stored", "modified_stored", "size", "sha256", "asin",
    "cde_key", "known_match", "title_source", "deleted_entry_index", "rel_path", "book_key"]


def build_timeline(files, annotations, positions, clippings, lookups, deleted_rows,
                   exported, titles, extra):
    events = []

    def add(time, basis, source, event, key="", detail="", state="", record="", title=None):
        if not time:
            return
        if title is None:
            title = titles.of(key)[0] if key else ""
        events.append({"time": time, "time_basis": basis, "source": source, "event": event,
                       "title": title, "detail": detail, "file_state": state,
                       "book_key": key, "record": record})

    for note in annotations:
        detail = "position %s" % note.get("start_position", "")
        if note.get("note"):
            detail += " | note: %s" % note["note"]
        add(note.get("created_utc"), UTC, "sidecar", "%s created" % note["kind"],
            note["book_key"], detail, note["file_state"], note["sidecar_file"])
        if note.get("modified_utc") and note["modified_utc"] != note.get("created_utc"):
            add(note["modified_utc"], UTC, "sidecar", "%s modified" % note["kind"],
                note["book_key"], detail, note["file_state"], note["sidecar_file"])
    for position in positions:
        add(position.get("time_utc"), UTC, "sidecar",
            "reading position saved (%s)" % position["field"], position["book_key"],
            "position %s" % position.get("position", ""), position["file_state"],
            position["sidecar_file"])
    for clip in clippings:
        where = " ".join(p for p in (
            "page %s" % clip["page"] if clip.get("page") else "",
            "location %s" % clip["location"] if clip.get("location") else "") if p)
        detail = " | ".join(p for p in (where, clip.get("text")) if p)
        add(clip.get("added_device_local"), LOCAL, "My Clippings.txt",
            "%s added to clippings" % (clip.get("kind") or "entry"), "", detail, "allocated",
            "%s entry %s" % (clip["source_file"], clip["entry"]), title=clip.get("title") or "")
    for lookup in lookups:
        add(lookup.get("lookup_utc"), UTC, "vocabulary database", "word looked up", "",
            " | ".join(p for p in (lookup.get("word"), lookup.get("usage")) if p),
            "allocated", lookup["source_file"], title=lookup.get("book_title") or "")
    for row in files:
        key = _document_key(row["rel_path"])
        add(row.get("created_stored"), FAT, "file system", "file created", key,
            row["rel_path"], "allocated", "manifest.csv")
        if row.get("modified_stored") != row.get("created_stored"):
            add(row.get("modified_stored"), FAT, "file system", "file modified", key,
                row["rel_path"], "allocated", "manifest.csv")
    for entry in deleted_rows:
        key = _document_key(entry["rel_path"])
        what = "deleted %s" % entry["type"]
        record = "deleted_entries.csv index %s" % entry.get("index", "")
        state = "deleted, read back" if entry.get("recovered_file") else "deleted"
        add(entry.get("created_stored"), FAT, "file system", "%s: created" % what, key,
            entry["rel_path"], state, record)
        if entry.get("modified_stored") != entry.get("created_stored"):
            add(entry.get("modified_stored"), FAT, "file system", "%s: modified" % what, key,
                entry["rel_path"], state, record)

    # Reading statistics and queued activity records, as the device stored them.
    by_asin = dict((asin, value[0]) for asin, value in titles.by_asin.items())
    for row in exported.get("freetime_bookinfo", ((), ()))[1]:
        title = by_asin.get(row.get("asin"), "")
        detail = "ASIN %s | timeread %s" % (row.get("asin"), row.get("timeread"))
        add(_ms(row.get("lastaccesstime")), UTC, "reading statistics",
            "book last opened", "", detail, "allocated",
            "system/freetime/freetime.db bookinfo", title=title)
        add(_ms(row.get("bookcompletetime")), UTC, "reading statistics",
            "book marked complete", "", detail, "allocated",
            "system/freetime/freetime.db bookinfo", title=title)
    for row in exported.get("freetime_badgesearned", ((), ()))[1]:
        add(_ms(row.get("badgeachievedtime")), UTC, "reading statistics", "badge earned", "",
            "%s | zone offset %s ms" % (row.get("badgeid"), row.get("timezoneoffset")),
            "allocated", "system/freetime/freetime.db badgesearned", title="")
    for row in exported.get("freetime_dayinfo", ((), ()))[1]:
        date = str(row.get("accessdate") or "")
        add(date if re.match(r"^\d{4}-\d{2}-\d{2}$", date) else "", LOCAL_DATE,
            "reading statistics", "reading recorded for the day", "",
            "timeread %s | wordsread %s" % (row.get("timeread"), row.get("wordsread")),
            "allocated", "system/freetime/freetime.db dayinfo", title="")
    for row in extra["activity_records"][1]:
        add(row["created_utc"], UTC, "activity log", row["schema_name"], "", row["record"],
            row["state"], "%s records id %s" % (row["source_file"], row["row_id"]), title="")
    for row in extra["reading_sessions"][1]:
        detail = "session %s | ASIN %s | locations %s to %s" % (
            row["reading_session_id"], row.get("asin"), row.get("start_reading_location"),
            row.get("end_reading_location"))
        record = "%s reading_sessions id %s" % (row["source_file"], row["reading_session_id"])
        add(row["start_utc"], UTC, "activity log", "reading session started", "", detail,
            row["state"], record, title=row["title"])
        add(row["end_utc"], UTC, "activity log", "reading session ended", "",
            "%s | %s page turns | closed by %s" % (detail, row.get("page_turns"),
                                                   row.get("close_method") or "(not recorded)"),
            row["state"], record, title=row["title"])
    for row in extra["page_history"][1]:
        add(row["time_utc"], UTC, "sidecar", "page history record", row["book_key"],
            "position %s" % row["position"], row["file_state"], row["sidecar_file"])
    for row in extra["audiobook_positions"][1]:
        add(row["updated_utc"] or row["created_utc"], UTC, "audiobook database",
            "audiobook position saved (%s)" % row.get("type"), "",
            "ASIN %s | position %s | set by %s" % (row.get("asin"), row["position"],
                                                   row.get("source_device") or "(not recorded)"),
            "allocated", row["source_file"], title=row["title"])
    for row in extra["store_storage"][1]:
        add(row["time_utc"], UTC, "store pages", "store page data saved (%s)" % row["kind"], "",
            " | ".join(p for p in (row["url"], row["asin"]) if p) or row["key"],
            "allocated", row["source_file"], title="")
    for row in extra["browser_cache"][1]:
        if row["created_utc"]:
            add(row["created_utc"], UTC, "browser cache", "browser cache entry created", "",
                row["url"], "allocated", "%s, %s" % (row["rel_path"], row["record"]), title="")
        else:
            add(row["created_stored"], FAT, "browser cache", "browser cache file created", "",
                row["url"], "allocated", row["rel_path"], title="")

    events.sort(key=lambda e: (e["time"][:19], e["time"], e["source"], e["event"],
                               e["record"], e["detail"]))
    return events


TIMELINE_COLUMNS = ["time", "time_basis", "source", "event", "title", "detail",
                    "file_state", "book_key", "record"]


def _document_key(rel_path):
    """The book a file belongs to, or '' for a file outside the document folders."""
    low = rel_path.lower()
    if not low.startswith(("documents/", "audible/")):
        return ""
    parts = rel_path.split("/")
    for index, part in enumerate(parts):
        if part.lower().endswith(".sdr"):
            return "/".join(parts[:index] + [part[:-4]])
    if low.endswith(_CONTENT_EXTENSIONS) and os.path.basename(low) != "my clippings.txt":
        return os.path.splitext(rel_path)[0]
    return ""


_DAY_COLUMNS = {
    "sidecar": "annotations_and_positions", "My Clippings.txt": "clippings",
    "vocabulary database": "lookups", "reading statistics": "reading_statistics",
    "activity log": "activity_log", "audiobook database": "audiobooks",
    "store pages": "store_pages", "browser cache": "browser_cache"}
ACTIVITY_COLUMNS = ["day", "total", "annotations_and_positions", "clippings", "lookups",
                    "activity_log", "audiobooks", "store_pages", "browser_cache",
                    "reading_statistics", "files_created", "files_modified",
                    "deleted_entry_times"]


def build_activity(timeline):
    days = {}
    for event in timeline:
        day = days.setdefault(event["time"][:10], dict.fromkeys(ACTIVITY_COLUMNS[1:], 0))
        if event["source"] == "file system":
            if event["event"].startswith("deleted"):
                column = "deleted_entry_times"
            else:
                column = "files_created" if event["event"] == "file created" else "files_modified"
        else:
            column = _DAY_COLUMNS[event["source"]]
        day[column] += 1
        day["total"] += 1
    return [dict(counts, day=day) for day, counts in sorted(days.items())]


def build_observations(documents, annotations, clippings, matched, timeline, finished_utc):
    rows = []

    def add(check, item, detail=""):
        rows.append({"check": check, "item": item, "detail": detail})

    for clip in clippings:
        if clip.get("kind") not in ("highlight", "note", "bookmark"):
            continue
        label = "entry %s: %s, %s, %s" % (clip["entry"], clip.get("title"), clip.get("kind"),
                                          clip.get("added_device_local"))
        note = matched.get(clip["entry"])
        if note is None:
            add("clipping with no sidecar annotation made in the same second", label,
                "no live or deleted sidecar holds it")
        elif note["file_state"] != "allocated":
            add("clipping found only in a deleted sidecar", label, note["sidecar_file"])
    paired = set(id(note) for note in matched.values())
    for note in annotations:
        if note["file_state"] == "allocated" and id(note) not in paired \
                and note.get("kind") in ("highlight", "note", "bookmark"):
            add("sidecar annotation with no clipping made in the same second",
                "%s: %s, %s" % (note["book_key"], note["kind"], note.get("created_utc")),
                note["sidecar_file"])
    by_hash = {}
    for row in documents:
        if row["state"] == "sidecar present, no content file":
            add("sidecar present, no content file", row["book_key"])
        elif row["state"] == "deleted sidecar only":
            add("deleted sidecar, no content file", row["book_key"])
        elif row["state"].startswith("deleted entry"):
            add("deleted document entry", row["rel_path"], row["state"])
        elif row.get("sha256"):
            by_hash.setdefault(row["sha256"], []).append(row["rel_path"])
        if row.get("known_match"):
            add("known hash match", row["rel_path"], row["known_match"])
    for digest, paths in sorted(by_hash.items()):
        if len(paths) > 1:
            add("same content at more than one path", " | ".join(sorted(paths)), digest)
    limit = (finished_utc or "")[:19]
    for event in timeline:
        if event["time_basis"] != UTC:
            continue
        stamp = event["time"][:19]
        if stamp < "2007-11-19":
            add("time before the first Kindle was sold", "%s %s" % (event["time"], event["event"]),
                event["record"])
        elif limit and stamp > limit:
            add("time later than the acquisition", "%s %s" % (event["time"], event["event"]),
                event["record"])
    return rows


def build_summary(log, facts, manifest, documents, annotations, positions, clippings,
                  lookups, deleted_rows, pairs, timeline, errors, tool_version, extra):
    rows = []

    def add(section, item, value, source=""):
        if value not in ("", None):
            rows.append({"section": section, "item": item, "value": value, "source": source})

    log = log or {}
    device = log.get("device") or (log.get("source_image") or {}).get("device") or {}
    disk = device.get("disk") or {}
    volume = log.get("volume") or {}
    fact = dict((f["fact"], f) for f in facts)
    log_name = "acquisition_log.json"
    serial = device.get("usb_serial", "")
    add("Device", "serial number", serial, log_name)
    add("Device", "model indicated by serial number", model_from_serial(serial),
        "characters 4 to 6 of the serial, community list, not confirmed")
    if "firmware" in fact:
        add("Device", "firmware", fact["firmware"]["value"], fact["firmware"]["source"])
    if device.get("usb_vendor_id"):
        add("Device", "USB ID", "%s:%s" % (device["usb_vendor_id"],
                                           device.get("usb_product_id", "")), log_name)
    add("Device", "USB mode", device.get("mode"), log_name)
    add("Device", "disk size in bytes", disk.get("size"), log_name)
    add("Device", "volume label", volume.get("label"), log_name)
    add("Device", "volume serial", volume.get("serial"), log_name)
    if volume.get("cluster_size"):
        add("Device", "cluster size in bytes", volume["cluster_size"], log_name)
        add("Device", "clusters", volume.get("clusters"), log_name)
        add("Device", "free clusters", volume.get("free_clusters"), log_name)
    for group in (log.get("source_volume") or {},):
        add("Device", "volume label", group.get("label"), log_name)
        add("Device", "file system", group.get("filesystem"), log_name)

    apps = extra["app_sessions"][1]
    if apps:
        last, source = apps[-1], "app_sessions, session %s" % apps[-1]["app_session_id"]
        add("Device", "time zone setting", last.get("time_zone"), source)
        add("Device", "device model code", last.get("device_model"), source)
        add("Device", "serial number in the activity log", last.get("device_serial_number"),
            source)
        add("Device", "account ID", last.get("customer_id"), source)
        add("Device", "country of residence", last.get("country_of_residence"), source)
        add("Device", "software", last.get("os_version"), source)
    library = [r for r in extra["activity_records"][1]
               if r["schema_name"] == "kindle_metrics_library_size"]
    if library:
        try:
            counts = json.loads(library[-1]["record"])
        except (TypeError, ValueError, RecursionError):
            counts = {}
        if not isinstance(counts, dict):
            counts = {}
        for key in ("all", "books", "books_downloaded", "audiobooks", "audiobooks_downloaded",
                    "documents", "documents_downloaded"):
            if isinstance(counts, dict) and key in counts:
                add("Library", "library count: %s" % key.replace("_", " "), counts[key],
                    "activity_records, %s" % library[-1]["created_utc"])

    case = log.get("case") or {}
    for key, label in (("case_id", "case"), ("evidence_id", "evidence item"),
                       ("examiner", "examiner"), ("notes", "notes")):
        add("Acquisition", label, case.get(key), log_name)
    add("Acquisition", "kind", " ".join(p for p in (log.get("kind"), log.get("method")) if p),
        log_name)
    add("Acquisition", "started (UTC)", log.get("started_utc"), log_name)
    add("Acquisition", "finished (UTC)", log.get("finished_utc"), log_name)
    add("Acquisition", "tool version", log.get("tool_version"), log_name)
    add("Acquisition", "parsed with tool version", tool_version, "parse_log.json")
    add("Acquisition", "manifest SHA-256", log.get("manifest_sha256"), log_name)
    image = log.get("source_image") or {}
    add("Acquisition", "source image SHA-256", image.get("image_sha256"), log_name)
    if "image_matches_its_log" in image:
        add("Acquisition", "source image matches its log",
            "yes" if image["image_matches_its_log"] else "no", log_name)
    if device:
        protected = (device.get("usb_write_protect_policy") == 1 or disk.get("is_read_only"))
        add("Acquisition", "Windows write protection reported", "yes" if protected else "no",
            log_name)
    counts = log.get("counts") or {}
    add("Acquisition", "files copied and verified", counts.get("files_ok"), log_name)
    add("Acquisition", "files with errors", counts.get("files_error"), log_name)

    files = [r for r in manifest if r["type"] == "file"]
    add("Content", "files", len(files), "manifest.csv")
    add("Content", "folders", sum(1 for r in manifest if r["type"] == "folder"), "manifest.csv")
    present = [d for d in documents if d["state"] == "file present"]
    kinds = {}
    for row in present:
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    add("Content", "document files present", len(present), "documents")
    for kind, count in sorted(kinds.items()):
        add("Content", "document files: %s" % kind, count, "documents")
    add("Content", "document files in a DRM-protected container",
        sum(1 for d in present if d.get("protected")), "documents")
    add("Content", "documents with annotations",
        sum(1 for d in documents if d["highlights"] + d["notes"] + d["bookmarks"]
            + d["other_annotations"] + d["annotations_in_deleted_sidecars"]), "documents")

    live = [a for a in annotations if a["file_state"] == "allocated"]
    for kind in ("highlight", "note", "bookmark"):
        add("Records", "%ss in live sidecars" % kind,
            sum(1 for a in live if a["kind"] == kind), "annotations")
    add("Records", "annotations in deleted sidecars", len(annotations) - len(live),
        "annotations")
    add("Records", "annotations with note text",
        sum(1 for a in annotations if a.get("note")), "annotations")
    add("Records", "clippings", len(clippings), "clippings")
    add("Records", "reading positions with a time",
        sum(1 for p in positions if p.get("time_utc")), "reading_positions")
    add("Records", "word lookups", len(lookups), "vocabulary_lookups")
    records = extra["activity_records"][1]
    add("Records", "activity records in the database now",
        sum(1 for r in records if r["state"] == "current row"), "activity_records")
    add("Records", "activity records only in earlier page versions",
        sum(1 for r in records if r["state"] != "current row"), "activity_records")
    add("Records", "reading sessions", len(extra["reading_sessions"][1]), "reading_sessions")
    add("Records", "app sessions", len(apps), "app_sessions")
    add("Records", "connectivity records", len(extra["connectivity"][1]), "connectivity")
    add("Records", "power and screen records", len(extra["power_events"][1]), "power_events")
    add("Records", "page history records", len(extra["page_history"][1]), "page_history")
    add("Records", "audiobook positions", len(extra["audiobook_positions"][1]),
        "audiobook_positions")
    add("Records", "book and audiobook links", len(extra["audiobook_links"][1]),
        "audiobook_links")
    add("Records", "store page records", len(extra["store_storage"][1]), "store_storage")
    add("Records", "browser cache entries", len(extra["browser_cache"][1]), "browser_cache")
    add("Records", "browser cache hosts",
        len(set(r["host"] for r in extra["browser_cache"][1] if r["host"])), "browser_cache")
    thumbs = extra["thumbnails"][1]
    add("Records", "thumbnail items", len(set(r["item_id"] for r in thumbs)), "thumbnails")
    add("Records", "thumbnail items with a document file present",
        len(set(r["item_id"] for r in thumbs if r["document_present"] == "yes")),
        "thumbnails")
    add("Records", "timeline events", len(timeline), "timeline")
    add("Records", "parse errors", len(errors), "parse_errors")

    if deleted_rows:
        add("Deleted", "deleted entries listed", len(deleted_rows), "deleted_entries")
        add("Deleted", "deleted files read back",
            sum(1 for r in deleted_rows if r.get("recovered_file")), "deleted_entries")
        add("Deleted", "deleted document entries",
            sum(1 for d in documents if d["state"].startswith("deleted entry")), "documents")

    for basis, label in ((UTC, "device clock (UTC)"), (LOCAL, "clippings (device local)"),
                         (FAT, "file system (as stored)")):
        times = [e["time"] for e in timeline if e["time_basis"] == basis]
        if times:
            add("Times", "earliest, %s" % label, times[0], "timeline")
            add("Times", "latest, %s" % label, max(times), "timeline")
    offsets = {}
    for pair in pairs:
        offsets.setdefault(pair["offset"], []).append(pair["clipping_local"])
    for offset, when in sorted(offsets.items()):
        add("Times", "device clock offset %s" % offset,
            "%d matched clipping(s), %s to %s" % (len(when), min(when)[:10], max(when)[:10]),
            "clock_offsets")
    return rows


def _fill_titles(extra, documents, titles):
    """Put titles into the tables that hold only an identifier."""
    by_id = dict((asin, value[0]) for asin, value in titles.by_asin.items())
    present = set()
    for row in documents:
        for key in (row["asin"], row["cde_key"]):
            if key:
                by_id.setdefault(key, row["title"])
                if row["state"] == "file present":
                    present.add(key)
    for row in extra["reading_sessions"][1]:
        row["title"] = by_id.get(row.get("asin"), "")
        if not row["title"]:
            # A personal document has no ASIN. Its key is elsewhere in the session.
            keys = re.findall(r"[0-9A-Fa-f]{32}", row.get("session") or "")
            row["title"] = next((by_id[k.upper()] for k in keys if k.upper() in by_id), "")
    for row in extra["audiobook_positions"][1]:
        row["title"] = by_id.get(row.get("asin"), "")
    for row in extra["audiobook_links"][1]:
        row["ebook_title"] = by_id.get(row.get("ebook_asin"), "")
        row["audiobook_title"] = by_id.get(row.get("audiobook_asin"), "")
    for row in extra["thumbnails"][1]:
        row["title"] = by_id.get(row["item_id"], "")
        row["document_present"] = "yes" if row["item_id"] in present else "no"


def build(log, facts, manifest, files, content, annotations, positions, clippings, lookups,
          deleted_rows, sidecar_files, exported, errors, tool_version, extra):
    """Returns [(table name, columns, rows)] in the order to write them.

    `extra` holds the tables of device_records. Their title columns are
    filled in here.
    """
    titles = _Titles(exported, lookups)
    pairs, matched = clock_offsets(clippings, annotations, titles)
    documents = build_documents(content, annotations, positions, clippings, lookups,
                                deleted_rows, sidecar_files, titles)
    _fill_titles(extra, documents, titles)
    timeline = build_timeline(files, annotations, positions, clippings, lookups,
                              deleted_rows, exported, titles, extra)
    finished = (log or {}).get("finished_utc", "")
    observations = build_observations(documents, annotations, clippings, matched, timeline,
                                      finished)
    summary = build_summary(log, facts, manifest, documents, annotations, positions,
                            clippings, lookups, deleted_rows, pairs, timeline, errors,
                            tool_version, extra)
    return [
        ("device_summary", ["section", "item", "value", "source"], summary),
        ("documents", DOCUMENT_COLUMNS, documents),
        ("timeline", TIMELINE_COLUMNS, timeline),
        ("activity_by_day", ACTIVITY_COLUMNS, build_activity(timeline)),
        ("clock_offsets", ["clipping_entry", "title", "kind", "clipping_local",
                           "annotation_utc", "offset_minutes", "offset", "matched_on",
                           "file_state", "sidecar_file"], pairs),
        ("observations", ["check", "item", "detail"], observations),
    ]
