"""Records of device use: activity log, sessions, audiobooks, store, browser.

Each function reads copied files only and returns rows. Nothing is
inferred here beyond decoding: a row says what one stored record holds.

Sources, as found on a Paperwhite with firmware 5.19:

- system/fmcache/fmcache.db and its write-ahead log: activity records the
  device queues for upload, with the app session and reading session each
  belongs to. Earlier page versions in the log hold records that have
  since been removed from the database.
- audible/*.db: last position heard per audiobook, and the links between
  books and audiobooks.
- .active_content_sandbox/**/*.localstorage: what the store pages cached.
- system/browser/kindle_browser: the web browser's cache.
- system/thumbnails: one cover per item the device has listed.
"""

import json
import os
import re
import sqlite3
import struct

from .parsers import wal
from .parsers.krds import ms_to_utc

MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_CACHE_ENTRY_BYTES = 64 * 1024 * 1024
CURRENT = "current row"
EARLIER = "earlier page version"

_ASIN = re.compile(r"\bB[0-9A-Z]{9}\b")


def _utc(value):
    try:
        return ms_to_utc(int(value))
    except (TypeError, ValueError):
        return ""


def _json(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


# --- activity log ----------------------------------------------------------------

RECORD_COLUMNS = ["created_utc", "schema_name", "record", "app_session_id",
                  "reading_session_id", "sequence_number", "row_id", "state",
                  "page_versions", "first_found_in", "created_timestamp", "source_file"]
READING_COLUMNS = ["reading_session_id", "title", "asin", "start_utc", "end_utc",
                   "duration_seconds", "is_complete", "page_turns", "records",
                   "last_record_utc", "open_position", "first_position_read",
                   "last_position_read", "close_method", "close_position",
                   "start_reading_location", "end_reading_location", "max_position",
                   "content_type", "format", "app_session_ids", "state", "page_versions",
                   "session", "source_file"]
APP_COLUMNS = ["app_session_id", "start_utc", "end_utc", "duration_seconds", "is_complete",
               "time_zone", "device_serial_number", "device_model", "device_type",
               "customer_id", "country_of_residence", "preferred_marketplace_id",
               "os_version", "app_version", "records", "last_record_utc", "state",
               "page_versions", "session", "source_file"]
CONNECTIVITY_COLUMNS = ["time_utc", "connection_name", "connection_state", "is_changed",
                        "app_session_id", "state", "source_file"]
POWER_COLUMNS = ["time_utc", "event", "detail", "app_session_id", "state", "record",
                 "source_file"]

_POWER_SCHEMAS = {
    "ereader_powerd_state_change": lambda r: "%s -> %s" % (r.get("prev_state"),
                                                           r.get("curr_state")),
    "ereader_screensaver_navigation_events": lambda r: str(r.get("screensaver_event")),
    "ereader_hibernate_wakeup": lambda r: "%s after %s ms in hibernation" % (
        r.get("wakeup_reason"), r.get("time_in_hibernate_ms")),
    "ereader_device_active_usage_time": lambda r: "active for %s ms" % r.get("time_ms"),
}


def activity_log(db_bytes, wal_bytes, rel, current):
    """Tables from fmcache.db. `current` holds the rows SQLite returns now:
    {"records": [dict], "app_sessions": [dict], "reading_sessions": [dict]}.
    """
    live_records = set((r.get("id"), r.get("created_timestamp"), r.get("record"))
                       for r in current.get("records", ()))
    live_sessions = set(r.get("session") for kind in ("app_sessions", "reading_sessions")
                        for r in current.get(kind, ()))
    records, sessions = {}, {}
    for source, _page, rowid, values, complete in wal.all_rows(db_bytes, wal_bytes):
        if not complete:
            continue
        if (len(values) == 10 and isinstance(values[1], str) and isinstance(values[9], str)
                and isinstance(values[7], int)):
            key = (rowid,) + tuple(values[1:])
            entry = records.setdefault(key, [source, 0])
            entry[1] += 1
        elif len(values) == 4 and isinstance(values[2], str):
            body = _json(values[2])
            if body.get("schema_name") in ("reading_session", "app_session") \
                    and isinstance(body.get("payload"), dict):
                entry = sessions.setdefault((body["schema_name"], rowid, values[2]),
                                            [source, 0, body])
                entry[1] += 1

    record_rows = []
    for key, (source, seen) in records.items():
        rowid, schema, _version, app_id, reading_id, _size, sequence, stamp, _prio, text = key
        record_rows.append({
            "created_utc": _utc(stamp), "schema_name": schema, "record": text,
            "app_session_id": app_id, "reading_session_id": reading_id,
            "sequence_number": sequence, "row_id": rowid,
            "state": CURRENT if (rowid, stamp, text) in live_records else EARLIER,
            "page_versions": seen, "first_found_in": source, "created_timestamp": stamp,
            "source_file": rel})
    record_rows.sort(key=lambda r: (r["created_timestamp"], r["sequence_number"] or 0,
                                    r["row_id"], r["record"]))

    # One session is rewritten as it goes on. Keep the last version of each.
    latest = {}
    for (schema, rowid, text), (source, seen, body) in sessions.items():
        payload = body["payload"]
        identity = (schema, rowid, body.get("created_timestamp"))
        rank = (payload.get("end_timestamp") or 0, body.get("sequence_number") or 0,
                body.get("sent_timestamp") or 0, text)
        entry = latest.setdefault(identity, {"rank": None, "seen": 0, "live": False})
        entry["seen"] += seen
        entry["live"] = entry["live"] or text in live_sessions
        if entry["rank"] is None or rank > entry["rank"]:
            entry.update(rank=rank, text=text, payload=payload, body=body)

    reading_rows, app_rows = [], []
    for (schema, rowid, created), entry in latest.items():
        payload = entry["payload"]
        start = payload.get("start_timestamp") or created
        end = payload.get("end_timestamp")
        column = "reading_session_id" if schema == "reading_session" else "app_session_id"
        mine = [r for r in record_rows if r[column] == rowid
                and _within(r["created_timestamp"], start, end)]
        row = {
            column: rowid, "start_utc": _utc(start), "end_utc": _utc(end),
            "duration_seconds": _seconds(start, end),
            "is_complete": _yes_no(payload.get("is_complete")),
            "records": len(mine),
            "last_record_utc": mine[-1]["created_utc"] if mine else "",
            "state": CURRENT if entry["live"] else EARLIER,
            "page_versions": entry["seen"], "session": entry["text"], "source_file": rel,
            "_start": start if isinstance(start, int) else 0}
        if schema == "reading_session":
            row.update(_reading_details(payload, mine))
            reading_rows.append(row)
        else:
            for name in ("time_zone", "device_serial_number", "device_model", "device_type",
                         "customer_id", "country_of_residence", "preferred_marketplace_id",
                         "os_version", "app_version"):
                row[name] = payload.get(name)
            app_rows.append(row)
    for rows, column in ((reading_rows, "reading_session_id"), (app_rows, "app_session_id")):
        rows.sort(key=lambda r: (r.pop("_start"), r[column]))

    connectivity, power = [], []
    for row in record_rows:
        body = _json(row["record"])
        if row["schema_name"] == "ereader_framework_connectivity":
            connectivity.append({
                "time_utc": row["created_utc"], "connection_name": body.get("connection_name"),
                "connection_state": body.get("connection_state"),
                "is_changed": body.get("is_changed"), "app_session_id": row["app_session_id"],
                "state": row["state"], "source_file": rel})
        elif row["schema_name"] in _POWER_SCHEMAS:
            power.append({
                "time_utc": row["created_utc"], "event": row["schema_name"],
                "detail": _POWER_SCHEMAS[row["schema_name"]](body),
                "app_session_id": row["app_session_id"], "state": row["state"],
                "record": row["record"], "source_file": rel})
    return {"activity_records": (RECORD_COLUMNS, record_rows),
            "reading_sessions": (READING_COLUMNS, reading_rows),
            "app_sessions": (APP_COLUMNS, app_rows),
            "connectivity": (CONNECTIVITY_COLUMNS, connectivity),
            "power_events": (POWER_COLUMNS, power)}


def _within(stamp, start, end):
    if not isinstance(stamp, int) or not isinstance(start, int):
        return True
    if stamp < start - 5000:
        return False
    return not isinstance(end, int) or stamp <= end + 5000


def _seconds(start, end):
    if isinstance(start, int) and isinstance(end, int) and end >= start:
        return round((end - start) / 1000.0, 1)
    return ""


def _yes_no(value):
    return "" if value is None else ("yes" if value else "no")


def _reading_details(payload, records):
    row = {name: payload.get(name) for name in (
        "asin", "start_reading_location", "end_reading_location", "max_position",
        "content_type", "format")}
    row["title"] = ""
    starts, ends, apps = [], [], []
    turns = 0
    for record in records:
        body = _json(record["record"])
        schema = record["schema_name"]
        if record["app_session_id"] not in apps and record["app_session_id"] is not None:
            apps.append(record["app_session_id"])
        if schema == "ereader_book_page_turn":
            turns += 1
        elif schema == "ereader_book_consume_content":
            if isinstance(body.get("start_position"), int):
                starts.append(body["start_position"])
            if isinstance(body.get("end_position"), int):
                ends.append(body["end_position"])
        elif schema == "ereader_open_book":
            row["open_position"] = body.get("start_reading_position")
        elif schema == "ereader_close_book":
            row["close_method"] = body.get("close_method")
            row["close_position"] = body.get("close_position")
    row["page_turns"] = turns
    row["first_position_read"] = min(starts) if starts else ""
    row["last_position_read"] = max(ends) if ends else ""
    row["app_session_ids"] = ", ".join(str(a) for a in apps)
    return row


# --- sidecar page history ------------------------------------------------------------

PAGE_HISTORY_COLUMNS = ["book_key", "file_state", "position", "time_utc", "sidecar_file"]


def page_history(sidecar_fields):
    """Positions the reader jumped from, with times, from page.history.store."""
    rows = []
    for field in sidecar_fields:
        if field["field"] != "page.history.store":
            continue
        for node in field["values"]:
            if not isinstance(node, dict) or node.get("name") != "page.history.record":
                continue
            values = node.get("values") or []
            text = [v for v in values if isinstance(v, str)]
            stamp = [v for v in values if isinstance(v, int) and not isinstance(v, bool)]
            rows.append({"book_key": field["book_key"], "file_state": field["file_state"],
                         "position": text[0] if text else "",
                         "time_utc": _utc(stamp[0]) if stamp else "",
                         "sidecar_file": field["sidecar_file"]})
    return rows


# --- audiobooks -----------------------------------------------------------------------

AUDIO_POSITION_COLUMNS = ["title", "asin", "type", "position", "position_ms",
                          "end_position_ms", "created_utc", "updated_utc", "source_device",
                          "lto", "notes", "source_file"]
AUDIO_LINK_COLUMNS = ["ebook_title", "ebook_asin", "ebook_format", "audiobook_title",
                      "audiobook_asin", "audiobook_format", "audiobook_owned",
                      "last_fetched_utc", "e_acr", "a_acr", "sync_file_acr", "source_file"]


def _clock(ms):
    try:
        seconds = int(ms) // 1000
    except (TypeError, ValueError):
        return ""
    if seconds < 0:
        return ""
    return "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60)


def _tables(con):
    return set(r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'"))


def _dicts(con, table):
    cur = con.execute('SELECT * FROM "%s"' % table.replace('"', '""'))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur]


def audiobooks(con, rel):
    """Rows from one database under audible/. Returns (positions, links)."""
    positions, links = [], []
    tables = _tables(con)
    if "ANNOTATIONS" in tables:
        for row in _dicts(con, "ANNOTATIONS"):
            positions.append({
                "title": "", "asin": row.get("ASIN"), "type": row.get("TYPE"),
                "position": _clock(row.get("START_POS_MS")),
                "position_ms": row.get("START_POS_MS"),
                "end_position_ms": row.get("END_POS_MS"),
                "created_utc": _utc(row.get("CREATION_DATE")),
                "updated_utc": _utc(row.get("LAST_UPDATED_DATE")),
                "source_device": row.get("SRC_DEVICE"), "lto": row.get("LTO"),
                "notes": row.get("NOTES"), "source_file": rel})
    if "relationships" in tables:
        for row in _dicts(con, "relationships"):
            links.append({
                "ebook_title": "", "ebook_asin": row.get("e_asin"),
                "ebook_format": row.get("e_format"), "audiobook_title": "",
                "audiobook_asin": row.get("a_asin"), "audiobook_format": row.get("a_format"),
                "audiobook_owned": row.get("is_audiobook_owned"),
                "last_fetched_utc": _utc(row.get("time_last_fetched")),
                "e_acr": row.get("e_acr"), "a_acr": row.get("a_acr"),
                "sync_file_acr": row.get("sync_file_acr"), "source_file": rel})
    return positions, links


# --- store pages ----------------------------------------------------------------------

STORE_COLUMNS = ["time_utc", "kind", "url", "asin", "ttl_ms", "value_bytes", "key", "value",
                 "source_file"]
_STORE_KEY = re.compile(r"^\*(\w+)\*_(.*)$", re.DOTALL)


def store_storage(con, rel):
    """Rows of a WebKit local storage file written by the store pages."""
    if "ItemTable" not in _tables(con):
        return []
    rows = []
    for key, value in con.execute("SELECT key, value FROM ItemTable"):
        if isinstance(value, (bytes, bytearray)):
            size = len(value)
            value = bytes(value).decode("utf-16-le", "replace")
        else:
            value = "" if value is None else str(value)
            size = len(value.encode("utf-8"))
        key = "" if key is None else str(key)
        match = _STORE_KEY.match(key)
        kind, rest = (match.group(1), match.group(2)) if match else ("", key)
        url = _json(rest).get("url") or ""
        asin = _ASIN.search(rest)
        body = _json(value)
        stamp = body.get("created")
        if stamp is None and re.match(r"^1\d{12}$", value.strip()):
            stamp = value.strip()
        rows.append({"time_utc": _utc(stamp), "kind": kind, "url": url,
                     "asin": asin.group(0) if asin else "", "ttl_ms": body.get("TTL"),
                     "value_bytes": size, "key": key, "value": value, "source_file": rel})
    rows.sort(key=lambda r: (r["time_utc"] == "", r["time_utc"], r["key"]))
    return rows


# --- browser cache ----------------------------------------------------------------------

BROWSER_COLUMNS = ["created_utc", "created_stored", "host", "url", "body_bytes",
                   "cache_format", "cache_key", "record", "rel_path"]
_BLOCK_MAGIC = 0xC104CAC3
_BLOCK_HEADER = 8192
_ENTRY_KEY_AT = 96
# Chromium counts microseconds from 1601. These bound a plausible entry time.
_WEBKIT_EPOCH_MS = 11644473600000
_WEBKIT_MIN = (_WEBKIT_EPOCH_MS + 1195430400000) * 1000      # 19 November 2007
_WEBKIT_MAX = (_WEBKIT_EPOCH_MS + 4102444800000) * 1000      # 1 January 2100
_SIMPLE_MAGIC = 0xFCFB6D1BA7725C30


def _cache_row(key, row, created_utc, body_bytes, cache_format, record):
    url = key.split()[-1] if key.split() else key
    url = url[url.find("http"):] if "http" in url else url
    host = re.match(r"^[a-z][a-z0-9+.-]*://([^/:?#]+)", url, re.IGNORECASE)
    return {"created_utc": created_utc, "created_stored": row.get("created_stored", ""),
            "host": host.group(1).lower() if host else "", "url": url,
            "body_bytes": body_bytes, "cache_format": cache_format, "cache_key": key,
            "record": record, "rel_path": row["rel_path"]}


def browser_block_file(data, row):
    """Entries of a Chromium block-file cache (data_1 and the like).

    Each entry record holds the address requested and the time the entry
    was created, in UTC from the device clock. Returns [] for a block file
    that holds no entry records.
    """
    if len(data) < _BLOCK_HEADER or struct.unpack("<I", data[:4])[0] != _BLOCK_MAGIC:
        return []
    block = struct.unpack("<i", data[12:16])[0]
    if block != 256:
        return []
    rows = []
    for index in range((len(data) - _BLOCK_HEADER) // block):
        start = _BLOCK_HEADER + index * block
        state, created, key_length, long_key = struct.unpack("<iQiI", data[start + 20:start + 40])
        if not (0 <= state <= 2 and _WEBKIT_MIN <= created <= _WEBKIT_MAX and long_key == 0
                and 0 < key_length <= 4 * block - _ENTRY_KEY_AT - 1):
            continue
        key = data[start + _ENTRY_KEY_AT:start + _ENTRY_KEY_AT + key_length]
        if len(key) != key_length or b"\x00" in key or b"://" not in key:
            continue
        sizes = struct.unpack("<4i", data[start + 40:start + 56])
        rows.append(_cache_row(key.decode("utf-8", "replace"), row,
                               _utc(created // 1000 - _WEBKIT_EPOCH_MS),
                               sizes[1] if sizes[1] >= 0 else "", "block file",
                               "entry record at byte %d" % start))
    return rows


def browser_cache_entry(data, row):
    """One Chromium cache file. Returns a row, or None if it is not an entry."""
    if len(data) < 24 or struct.unpack("<Q", data[:8])[0] != _SIMPLE_MAGIC:
        return None
    key_length = struct.unpack("<I", data[12:16])[0]
    if key_length == 0:
        return None                      # an index file, which uses the same signature
    if key_length > 8192:
        raise ValueError("cache entry key of %d bytes" % key_length)
    for offset in (24, 20):
        key = data[offset:offset + key_length]
        if len(key) == key_length and b"\x00" not in key:
            break
    else:
        raise ValueError("cache entry key not found after the header")
    return _cache_row(key.decode("utf-8", "replace"), row, "", row["size"], "simple file",
                      "whole file")


# --- thumbnails ---------------------------------------------------------------------------

THUMBNAIL_COLUMNS = ["item_id", "title", "content_code", "document_present", "created_stored",
                     "modified_stored", "size", "rel_path"]
_THUMBNAIL = re.compile(r"^thumbnail_(.+?)_([A-Z]{4})(?:_portrait)?\.[A-Za-z]+$")


def thumbnails(files):
    rows = []
    for row in files:
        if not row["rel_path"].lower().startswith("system/thumbnails/"):
            continue
        match = _THUMBNAIL.match(os.path.basename(row["rel_path"]))
        if match:
            rows.append({"item_id": match.group(1), "title": "",
                         "content_code": match.group(2), "document_present": "",
                         "created_stored": row.get("created_stored", ""),
                         "modified_stored": row.get("modified_stored", ""),
                         "size": row["size"], "rel_path": row["rel_path"]})
    return rows


# --- collect --------------------------------------------------------------------------------

def collect(files, read, open_db, exported, sidecar_fields, errors):
    """Every table of this module. Returns {name: (columns, rows)}.

    read(rel, limit=None) returns a file's bytes. open_db(rel) returns a connection to a
    working copy of a database. exported holds the fmcache tables as SQLite
    returns them. Failures go to `errors` and leave a table empty.
    """
    sizes = dict((r["rel_path"], int(r["size"] or 0)) for r in files)
    by_low = dict((r["rel_path"].lower(), r["rel_path"]) for r in files)
    tables = {
        "activity_records": (RECORD_COLUMNS, []), "reading_sessions": (READING_COLUMNS, []),
        "app_sessions": (APP_COLUMNS, []), "connectivity": (CONNECTIVITY_COLUMNS, []),
        "power_events": (POWER_COLUMNS, []),
    }

    def fail(rel, parser, exc):
        errors.append({"file": rel, "parser": parser,
                       "error": "%s: %s" % (type(exc).__name__, exc)})

    rel = by_low.get("system/fmcache/fmcache.db")
    if rel:
        log = by_low.get("system/fmcache/fmcache.db-wal")
        try:
            if sizes[rel] + sizes.get(log, 0) > MAX_DATABASE_BYTES:
                raise ValueError("larger than %d bytes, not read" % MAX_DATABASE_BYTES)
            current = dict((name, exported.get("fmcache_" + name, ((), ()))[1])
                           for name in ("records", "app_sessions", "reading_sessions"))
            tables.update(activity_log(read(rel), read(log) if log else b"", rel, current))
        except (OSError, ValueError, wal.WalError) as exc:
            fail(rel, "activity log", exc)

    tables["page_history"] = (PAGE_HISTORY_COLUMNS, page_history(sidecar_fields))

    positions, links, store = [], [], []
    for row in files:
        low = row["rel_path"].lower()
        is_audio = low.startswith("audible/") and low.endswith(".db")
        is_store = low.endswith(".localstorage")
        if not (is_audio or is_store):
            continue
        try:
            con = open_db(row["rel_path"])
        except (OSError, sqlite3.Error) as exc:
            fail(row["rel_path"], "sqlite", exc)
            continue
        try:
            if is_audio:
                found = audiobooks(con, row["rel_path"])
                positions += found[0]
                links += found[1]
            else:
                store += store_storage(con, row["rel_path"])
        except sqlite3.Error as exc:
            fail(row["rel_path"], "audiobooks" if is_audio else "store pages", exc)
        finally:
            con.close()
    tables["audiobook_positions"] = (AUDIO_POSITION_COLUMNS, positions)
    tables["audiobook_links"] = (AUDIO_LINK_COLUMNS, links)
    tables["store_storage"] = (STORE_COLUMNS, store)

    cache = []
    for row in files:
        if not row["rel_path"].lower().startswith("system/browser/"):
            continue
        try:
            # The key and the response headers of a larger entry are not read past
            # this many bytes. The entry is still listed if its key is found.
            data = read(row["rel_path"], MAX_CACHE_ENTRY_BYTES)
            if re.match(r"^data_\d+$", os.path.basename(row["rel_path"])):
                cache += browser_block_file(data, row)
                continue
            entry = browser_cache_entry(data, row)
        except (OSError, ValueError, struct.error) as exc:
            fail(row["rel_path"], "browser cache", exc)
            continue
        if entry:
            cache.append(entry)
    cache.sort(key=lambda r: (r["created_utc"] or r["created_stored"], r["rel_path"],
                              r["record"]))
    tables["browser_cache"] = (BROWSER_COLUMNS, cache)
    tables["thumbnails"] = (THUMBNAIL_COLUMNS, thumbnails(files))
    return tables
