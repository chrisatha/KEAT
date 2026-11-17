"""Parse a logical acquisition into one SQLite file and one CSV per table.

Works on the copied files only. The device is not touched. Every file that
cannot be parsed is listed in parse_log.json with the reason.
"""

import csv
import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

from . import __version__, analysis, device_records
from .parsers import krds
from .parsers.clippings import parse_clippings
from .util import (csv_safe, harden_db, hash_file, inside, long_path, utc_now,
                   write_json)

_ASIN = re.compile(r"_(B[0-9A-Z]{9})(_sample)?$")
_CDE_KEY = re.compile(r"-cdeKey_([0-9A-Fa-f]{32})$")
_SQLITE_MAGIC = b"SQLite format 3\x00"
# Sidecar files are a few kilobytes. A larger file with the sidecar
# signature is listed in parse_errors and not loaded into memory.
MAX_SIDECAR_BYTES = 64 * 1024 * 1024


def _container(head):
    if head.startswith(b"\xeaDRMION\xee"):
        return "KFX (DRM-protected)"
    if head.startswith(b"CONT"):
        return "KFX"
    if head.startswith(b"%PDF"):
        return "PDF"
    if head[60:68] in (b"BOOKMOBI", b"TEXtREAd"):
        return "MOBI/AZW"
    if head.startswith(b"PK\x03\x04"):
        return "ZIP-based"
    if head.startswith(b"TPZ"):
        return "Topaz"
    return ""


class _Output:
    """Writes each table to SQLite and to a CSV file."""

    def __init__(self, out_dir):
        self.out = Path(out_dir)
        (self.out / "csv").mkdir(parents=True, exist_ok=True)
        self.db_path = self.out / "kindle_parsed.db"
        self.con = sqlite3.connect(str(self.db_path))
        self.counts = {}

    def table(self, name, columns, rows):
        rows = [[_cell(row.get(c)) for c in columns] for row in rows]
        cols_sql = ", ".join(_ident(c) for c in columns)
        self.con.execute("CREATE TABLE %s (%s)" % (_ident(name), cols_sql))
        self.con.executemany(
            "INSERT INTO %s VALUES (%s)" % (_ident(name), ",".join("?" * len(columns))), rows)
        self.con.commit()
        with open(self.out / "csv" / (name + ".csv"), "w", newline="",
                  encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow([_csv_cell(c) for c in columns])
            writer.writerows([_csv_cell(v) for v in row] for row in rows)
        self.counts[name] = len(rows)

    def close(self):
        self.con.close()


def _ident(name):
    """Quote a table or column name. Names come from device databases."""
    return '"%s"' % str(name).replace('"', '""')


def _csv_cell(value):
    """A CSV cannot hold a NUL character. Show it as the text \\x00.

    Text that a spreadsheet would read as a formula gets a leading
    apostrophe. The SQLite file keeps the value unchanged.
    """
    if isinstance(value, str) and "\x00" in value:
        value = value.replace("\x00", "\\x00")
    # Device text must not run as a formula when the file is opened in a
    # spreadsheet.
    return csv_safe(value)


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _open_working_copy(path, tmp_root):
    """Copy a database with its journal files and open the copy."""
    work = tempfile.mkdtemp(dir=tmp_root)
    for suffix in ("", "-wal", "-shm", "-journal"):
        src = long_path(str(path) + suffix)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(work, os.path.basename(str(path)) + suffix))
    con = sqlite3.connect(os.path.join(work, os.path.basename(str(path))))
    # The schema comes from the device: do not let it run functions.
    harden_db(con)
    return con


def _dump_table(con, table):
    cur = con.execute("SELECT * FROM %s" % _ident(table))
    columns = [d[0] for d in cur.description]
    return columns, [dict(zip(columns, row)) for row in cur]


def _book_key(rel_path):
    """Name shared by a content file and its .sdr sidecar folder."""
    parts = rel_path.split("/")
    for i, part in enumerate(parts):
        if part.lower().endswith(".sdr"):
            return "/".join(parts[:i] + [part[:-4]])
    return os.path.splitext(rel_path)[0]


def _name_ids(key):
    base = key.split("/")[-1]
    asin = _ASIN.search(base)
    cde = _CDE_KEY.search(base)
    return {
        "asin": asin.group(1) if asin else "",
        "is_sample": "yes" if asin and asin.group(2) else "",
        "cde_key": cde.group(1).upper() if cde else "",
    }


_HASH_TOKEN = re.compile(r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{64}|[0-9A-Fa-f]{32})(?![0-9A-Fa-f])")


def load_known(path):
    """Reference hashes to look for. Returns {hash: label}.

    `path` is a folder of reference files (each is hashed) or a text or CSV
    file with one MD5 or SHA-256 per line. The rest of the line is the label.
    """
    known = {}
    path = str(path)
    if os.path.isdir(path):
        for folder, _dirs, names in os.walk(path):
            for name in sorted(names):
                full = os.path.join(folder, name)
                label = os.path.relpath(full, path).replace("\\", "/")
                for digest in hash_file(full, ("sha256", "md5")).values():
                    known.setdefault(digest, label)
    elif os.path.isfile(path):
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                for token in _HASH_TOKEN.findall(line):
                    known.setdefault(token.lower(), line.strip())
    else:
        raise ValueError("known-hash source not found: %s" % path)
    return known


def parse_acquisition(acq_dir, out_dir=None, known=None):
    known_hashes = load_known(known) if known else {}
    acq = Path(os.path.abspath(str(acq_dir)))
    files_root = acq / "files"
    manifest_path = acq / "manifest.csv"
    if not files_root.is_dir() or not manifest_path.is_file():
        raise ValueError("not a logical acquisition folder: %s" % acq)
    out_dir = Path(out_dir) if out_dir else acq / "parsed"
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError("output folder is not empty: %s" % out_dir)
    out = _Output(out_dir)
    errors = []
    started = utc_now()

    with open(manifest_path, newline="", encoding="utf-8") as fh:
        manifest = [r for r in csv.DictReader(fh)]
    # A manifest may come from someone else. A row that names a path outside
    # the acquisition folder is listed as an error and not read.
    files = []
    for r in manifest:
        if r["type"] != "file" or r["status"] != "ok":
            continue
        try:
            inside(files_root, r["rel_path"])
        except ValueError as exc:
            errors.append({"file": r["rel_path"], "parser": "manifest", "error": str(exc)})
            continue
        files.append(r)

    def read(rel, limit=None):
        with open(long_path(inside(files_root, rel)), "rb") as fh:
            return fh.read() if limit is None else fh.read(limit)

    # --- device facts -----------------------------------------------------
    facts = []
    log = {}
    log_path = acq / "acquisition_log.json"
    if log_path.is_file():
        log = json.loads(log_path.read_text(encoding="utf-8"))
        device = log.get("device") or {}
        for key in ("usb_serial", "usb_product_id", "mode"):
            if device.get(key):
                facts.append({"fact": key, "value": device[key], "source": "acquisition_log.json"})
    for row in files:
        if row["rel_path"].lower() == "system/version.txt":
            facts.append({"fact": "firmware", "source": row["rel_path"],
                          "value": read(row["rel_path"]).decode("utf-8", "replace").strip()})
    out.table("device_facts", ["fact", "value", "source"], facts)

    # --- sidecar files (any file with the sidecar signature) --------------
    annotations, positions, fields, sidecar_files = [], [], [], []
    # Allocated files, then deleted files that were read back from an image.
    candidates = [(r["rel_path"], files_root / r["rel_path"], r["rel_path"], r, "allocated")
                  for r in files]
    deleted_rows = []
    deleted_path = acq / "deleted_entries.csv"
    if deleted_path.is_file():
        with open(deleted_path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            deleted_columns = list(reader.fieldnames)
            deleted_rows = list(reader)
        for r in deleted_rows:
            if r["recovered_file"]:
                try:
                    full = inside(acq, r["recovered_file"])
                except ValueError as exc:
                    errors.append({"file": r["recovered_file"], "parser": "deleted entries",
                                   "error": str(exc)})
                    continue
                candidates.append((r["recovered_file"], full,
                                   r["rel_path"], r, "deleted, read back"))
    for rel, full, original, row, state in candidates:
        try:
            with open(long_path(full), "rb") as fh:
                data = fh.read(8)
                if not krds.is_krds(data):
                    continue
                data += fh.read(MAX_SIDECAR_BYTES)
                if len(data) > MAX_SIDECAR_BYTES:
                    raise krds.KrdsError("larger than %d bytes, not decoded" % MAX_SIDECAR_BYTES)
            decoded = krds.decode(data)
        except (OSError, krds.KrdsError, RecursionError) as exc:
            errors.append({"file": rel, "parser": "sidecar",
                           "error": "%s: %s" % (type(exc).__name__, exc)})
            continue
        key = _book_key(original)
        base = {"book_key": key, "sidecar_file": rel, "file_state": state}
        sidecar_files.append(dict(base, size=row["size"], sha256=row["sha256"],
                                  modified_stored=row.get("modified_stored", ""),
                                  field_count=len(decoded["fields"])))
        for item in krds.annotations(decoded):
            annotations.append(dict(base, **item))
        for item in krds.positions(decoded):
            positions.append(dict(base, **item))
        for node in decoded["fields"]:
            fields.append(dict(base, field=node["name"], values=node["values"]))
    out.table("sidecar_files", ["book_key", "file_state", "sidecar_file", "size", "sha256",
                                "modified_stored", "field_count"], sidecar_files)
    out.table("annotations", ["book_key", "file_state", "kind", "start_position",
                              "end_position", "created_utc", "modified_utc", "note",
                              "template", "raw_values", "sidecar_file"], annotations)
    out.table("reading_positions", ["book_key", "file_state", "field", "position",
                                    "time_utc", "raw_values", "sidecar_file"], positions)
    out.table("sidecar_fields", ["book_key", "file_state", "sidecar_file", "field",
                                 "values"], fields)

    # --- content files ------------------------------------------------------
    sidecar_keys = {}
    for row in manifest:
        if row["type"] == "folder" and row["rel_path"].lower().endswith(".sdr"):
            sidecar_keys[row["rel_path"][:-4]] = row["rel_path"]
    annotation_counts = {}
    for item in annotations:
        if item["file_state"] != "allocated":
            continue
        annotation_counts[item["book_key"]] = annotation_counts.get(item["book_key"], 0) + 1
    content = []
    for row in files:
        rel = row["rel_path"]
        low = rel.lower()
        in_documents = low.startswith("documents/")
        in_audible = low.startswith("audible/") and low.endswith((".aax", ".aa"))
        if not (in_documents or in_audible):
            continue
        if ".sdr/" in low or low.startswith("documents/.cache/"):
            continue
        key = os.path.splitext(rel)[0]
        try:
            head = read(rel, 68)
        except OSError as exc:
            errors.append({"file": rel, "parser": "content", "error": str(exc)})
            head = b""
        entry = {
            "rel_path": rel,
            "extension": os.path.splitext(rel)[1].lower(),
            "size": row["size"],
            "sha256": row["sha256"],
            "md5": row["md5"],
            "modified_stored": row.get("modified_stored", ""),
            "created_stored": row.get("created_stored", ""),
            "container": _container(head),
            "sidecar_folder": sidecar_keys.get(key, ""),
            "annotation_count": annotation_counts.get(key, 0),
            "known_match": known_hashes.get(row["sha256"]) or known_hashes.get(row["md5"]) or "",
        }
        entry.update(_name_ids(key))
        content.append(entry)
    out.table("content_files", ["rel_path", "extension", "container", "size", "sha256",
                                "md5", "modified_stored", "created_stored", "asin",
                                "is_sample", "cde_key", "sidecar_folder",
                                "annotation_count", "known_match"], content)
    if known:
        # Every file in the acquisition is checked, not only content files.
        matches = [{"rel_path": r["rel_path"], "sha256": r["sha256"], "md5": r["md5"],
                    "size": r["size"],
                    "known_match": known_hashes.get(r["sha256"]) or known_hashes.get(r["md5"])}
                   for r in files
                   if r["sha256"] in known_hashes or r["md5"] in known_hashes]
        out.table("known_matches", ["rel_path", "known_match", "size", "sha256", "md5"], matches)

    # --- My Clippings.txt -----------------------------------------------------
    clippings = []
    for row in files:
        if os.path.basename(row["rel_path"]).lower() == "my clippings.txt":
            text = read(row["rel_path"]).decode("utf-8-sig", errors="replace")
            for entry in parse_clippings(text):
                clippings.append(dict(entry, source_file=row["rel_path"]))
    out.table("clippings", ["entry", "title", "author", "kind", "page", "location",
                            "added_device_local", "added_raw", "text", "title_line",
                            "detail_line", "source_file"], clippings)

    # --- SQLite databases -------------------------------------------------------
    tmp_root = tempfile.mkdtemp(prefix="kindle_parse_")
    inventory = []
    lookups, exported = [], {}
    wanted = {
        "system/vocabulary/vocab.db": None,
        "system/freetime/freetime.db": ("bookinfo", "bookmetadata", "dayinfo", "badgesearned"),
        "system/fmcache/fmcache.db": ("records", "app_sessions", "reading_sessions"),
    }
    try:
        for row in files:
            rel = row["rel_path"]
            if rel.lower().endswith(("-wal", "-shm", "-journal")):
                continue
            try:
                if read(rel, 16) != _SQLITE_MAGIC:
                    continue
                con = _open_working_copy(files_root / rel, tmp_root)
            except (OSError, sqlite3.Error) as exc:
                errors.append({"file": rel, "parser": "sqlite", "error": str(exc)})
                continue
            try:
                tables = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                for table in tables:
                    try:
                        count = con.execute(
                            "SELECT COUNT(*) FROM %s" % _ident(table)).fetchone()[0]
                        columns = [c[1] for c in con.execute(
                            "PRAGMA table_info(%s)" % _ident(table))]
                        inventory.append({"database": rel, "table": table, "rows": count,
                                          "columns": ", ".join(columns)})
                    except sqlite3.Error as exc:
                        errors.append({"file": rel, "parser": "sqlite",
                                       "error": "table %s: %s" % (table, exc)})
                low = rel.lower()
                if low == "system/vocabulary/vocab.db" and "LOOKUPS" in tables:
                    cur = con.execute(
                        "SELECT l.timestamp, w.word, w.stem, w.lang, l.usage, l.pos, "
                        "b.title, b.authors, b.asin, l.dict_key "
                        "FROM LOOKUPS l LEFT JOIN WORDS w ON w.id = l.word_key "
                        "LEFT JOIN BOOK_INFO b ON b.id = l.book_key ORDER BY l.timestamp")
                    for ts, word, stem, lang, usage, pos, title, authors, asin, dict_key in cur:
                        lookups.append({
                            "lookup_utc": krds.ms_to_utc(ts), "word": word, "stem": stem,
                            "language": lang, "usage": usage, "position": pos,
                            "book_title": title, "book_authors": authors,
                            "book_asin": asin, "dictionary_asin": dict_key,
                            "timestamp_ms": ts, "source_file": rel})
                for table in wanted.get(low) or ():
                    if table in tables:
                        name = "%s_%s" % (os.path.splitext(os.path.basename(low))[0], table)
                        exported[name] = _dump_table(con, table)
            except sqlite3.Error as exc:
                errors.append({"file": rel, "parser": "sqlite", "error": str(exc)})
            finally:
                con.close()
        # --- activity log, audiobooks, store pages, browser cache, thumbnails ---
        extra = device_records.collect(
            files, read, lambda rel: _open_working_copy(files_root / rel, tmp_root),
            exported, fields, errors)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    out.table("vocabulary_lookups", ["lookup_utc", "word", "stem", "language", "usage",
                                     "book_title", "book_authors", "book_asin", "position",
                                     "dictionary_asin", "timestamp_ms", "source_file"], lookups)
    out.table("database_inventory", ["database", "table", "rows", "columns"], inventory)
    for name, (columns, rows) in sorted(exported.items()):
        try:
            out.table(name, columns, rows)
        except sqlite3.Error as exc:
            errors.append({"file": name, "parser": "sqlite",
                           "error": "table not exported: %s" % exc})
    if deleted_path.is_file():
        for row in deleted_rows:
            row["known_match"] = (known_hashes.get(row["sha256"])
                                  or known_hashes.get(row["md5"]) or "")
        out.table("deleted_entries", deleted_columns + ["known_match"], deleted_rows)
    # --- analysis tables, derived from the rows above ---------------------------
    derived = analysis.build(
        log, facts, manifest, files, content, annotations, positions, clippings,
        lookups, deleted_rows, sidecar_files, exported, errors, __version__, extra)
    for name, (columns, rows) in extra.items():
        out.table(name, columns, rows)
    for name, columns, rows in derived:
        out.table(name, columns, rows)
    out.table("parse_errors", ["file", "parser", "error"], errors)
    out.close()

    summary = {"output": str(out.out)}
    summary.update(out.counts)
    write_json(out.out / "parse_log.json", {
        "tool": "kindle-acquire", "tool_version": __version__,
        "started_utc": started, "finished_utc": utc_now(),
        "acquisition": str(acq), "tables": out.counts, "errors": errors,
        "known_hash_source": str(known) if known else "",
        "known_hashes_loaded": len(known_hashes),
        "time_notes": {
            "*_utc": "decoded from millisecond epoch values written by the device",
            "*_stored": "FAT directory time as stored, no time zone",
            "added_device_local": "clock time printed by the device, no time zone",
            "timeline.time": "in the basis named in time_basis. Events in different "
                             "bases are not in true order without the device clock "
                             "offset (clock_offsets table)",
            "activity_by_day.day": "the date part of timeline.time, each event in "
                                   "its own basis",
        },
    })
    from .examiner import write_report
    summary["report"] = write_report(out.out)
    return summary
