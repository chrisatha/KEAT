"""Support profile: the structure of a device, without its content.

When a Kindle model or firmware is new to the tool, this file says what
is on it in terms the parsers care about: folder layout of the system
area, file types, sidecar field names and their value shapes, database
schemas, and what the parsers could not read.

It is built to be shared in a bug report. It holds no file names from
the documents area, no titles, no text, no annotation content, no times
of use, no account identifiers and only the first six characters of the
serial number. Review it before sharing all the same: it is plain JSON.
"""

import csv
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from . import __version__
from .report import _container, parse_acquisition
from .util import inside, long_path, open_db_readonly, utc_now, write_json

# Top-level folders a Kindle creates. Any other top-level name is counted,
# not named: a user can create folders there.
KNOWN_TOP = {
    "documents", "system", "audible", "fonts", "voice", "screenshots", "music",
    ".active_content_sandbox", ".bcache", ".hidden", ".notebooks", ".kindle",
    "system volume information", "amazon-cover-bug", ".fseventsd", ".trashes",
}
_EXTENSION = re.compile(r"^\.[A-Za-z0-9_-]{1,12}$")
_ACCOUNT = re.compile(r"amzn1\.account\.[A-Za-z0-9]+")
_TOKEN = re.compile(r"(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{16,}")
_ASIN = re.compile(r"\bB0[0-9A-Z]{8}\b")
_FIRMWARE = re.compile(r"^[\w .()\-]{1,80}$")
_SIGNATURES = (
    (b"\x00\x00\x00\x00\x00\x1a\xb1\x26", "kindle sidecar"),
    (b"SQLite format 3\x00", "sqlite"),
    (b"\xff\xd8\xff", "jpeg"), (b"\x89PNG", "png"), (b"GIF8", "gif"),
    (b"<?xml", "xml"), (b"{", "json or text"), (b"<!DOCTYPE", "html"), (b"<html", "html"),
)


def _extension(path):
    ext = os.path.splitext(path)[1]
    return ext.lower() if _EXTENSION.match(ext) else ("<none>" if not ext else "<other>")


def _area(path):
    """A coarse, content-free location for a path."""
    parts = path.split("/")
    top = parts[0].lower()
    if top == "documents":
        inside_sidecar = any(p.lower().endswith(".sdr") for p in parts[1:-1])
        return "documents (sidecar folder)" if inside_sidecar else "documents"
    if top == "system" and len(parts) > 2:
        return "system/" + parts[1]
    if top in KNOWN_TOP:
        return parts[0]
    return "<other top-level folder>"


def _scrub(text):
    """Remove account ids, store ids and long tokens from a name."""
    text = _ACCOUNT.sub("amzn1.account.<id>", str(text))
    text = _ASIN.sub("<asin>", text)
    return _TOKEN.sub("<id>", text)[:120]


def _redact_path(path):
    """A database path with identifiers removed. Used outside documents only."""
    return "/".join(_scrub(part) for part in path.split("/"))


def _error_class(text):
    """The start of an error message, before any quoted name or path."""
    return re.sub(r"\d+", "N", re.split(r"['\"\\/]", str(text))[0])[:120].strip()


def _signature(head):
    kind = _container(head)
    if kind:
        return kind
    for magic, name in _SIGNATURES:
        if head.startswith(magic):
            return name
    return "<not recognised>" if head else "<empty>"


def _shape(value):
    if isinstance(value, dict):
        return "field:" + _scrub(value.get("name", ""))
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "str"


def _mask(text):
    """Letters to 'a', digits to '9'. Keeps the shape of a line, not its words."""
    return re.sub(r"[A-Za-z]", "a", re.sub(r"\d", "9", text))


def _count(mapping, key, amount=1):
    mapping[key] = mapping.get(key, 0) + amount


def survey(acq_dir, out_path=None):
    """Write the support profile for a logical or extracted acquisition."""
    acq = Path(os.path.abspath(str(acq_dir)))
    log = json.loads((acq / "acquisition_log.json").read_text(encoding="utf-8"))
    if log.get("kind") != "logical":
        raise ValueError("survey needs a logical or extracted acquisition")
    with open(acq / "manifest.csv", newline="", encoding="utf-8") as fh:
        manifest = list(csv.DictReader(fh))

    device = log.get("device") or (log.get("source_image") or {}).get("device") or {}
    disk = device.get("disk") or {}
    profile = {
        "profile_format": 1,
        "tool_version": __version__,
        "created_utc": utc_now(),
        "acquisition_method": log.get("method", "copy from a mounted volume"),
        "device": {
            "usb_product_id": device.get("usb_product_id", ""),
            "mode": device.get("mode", ""),
            "serial_prefix": (device.get("usb_serial") or "")[:6],
            "disk_size_bytes": disk.get("size", ""),
            "sector_size": disk.get("logical_sector_size", ""),
        },
        # The label and the volume serial identify the device, so they stay out.
        "volume": {k: v for k, v in (log.get("volume") or {}).items()
                   if k not in ("serial", "label")},
    }
    if "label" in (log.get("volume") or {}):
        profile["volume"]["label_is_kindle"] = log["volume"]["label"] == "Kindle"

    # --- files -------------------------------------------------------------
    files = [r for r in manifest if r["type"] == "file"]
    counts = {"files": len(files), "folders": sum(1 for r in manifest if r["type"] == "folder"),
              "rows_with_errors": sum(1 for r in manifest if r["status"] == "error"),
              "bytes": sum(int(r["size"] or 0) for r in files)}
    by_area, signatures, unknown_top = {}, {}, set()
    for row in manifest:
        top = row["rel_path"].split("/")[0]
        if top.lower() not in KNOWN_TOP:
            unknown_top.add(top)
    for row in files:
        area = _area(row["rel_path"])
        ext = _extension(row["rel_path"])
        _count(by_area.setdefault(area, {}), ext)
        try:
            with open(long_path(inside(acq / "files", row["rel_path"])), "rb") as fh:
                head = fh.read(68)
        except (OSError, ValueError):
            head = None
        _count(signatures.setdefault(ext, {}),
               "<unreadable>" if head is None else _signature(head))
    counts["top_level_entries_not_named"] = len(unknown_top)
    profile["counts"] = counts
    profile["file_types_by_area"] = {a: dict(sorted(e.items())) for a, e in sorted(by_area.items())}
    profile["signatures_by_extension"] = {e: dict(sorted(s.items()))
                                          for e, s in sorted(signatures.items())}
    for row in files:
        if row["rel_path"].lower() == "system/version.txt":
            try:
                text = inside(acq / "files", row["rel_path"]).read_bytes()[:200]
                text = text.decode("utf-8", "replace").strip()
                profile["device"]["firmware"] = (
                    text if _FIRMWARE.match(text) else "<unexpected content>")
            except (OSError, ValueError):
                pass

    # --- parsed records ------------------------------------------------------
    parsed = acq / "parsed" / "kindle_parsed.db"
    temp = None
    if not parsed.is_file():
        temp = tempfile.mkdtemp(prefix="keat_survey_")
        parse_acquisition(acq, Path(temp) / "parsed")
        parsed = Path(temp) / "parsed" / "kindle_parsed.db"
    try:
        con = open_db_readonly(parsed)
        tables = {name: con.execute('SELECT COUNT(*) FROM "%s"' % name.replace('"', '""'))
                  .fetchone()[0]
                  for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        profile["parsed_table_rows"] = dict(sorted(tables.items()))

        sidecars = {}
        for path, field, values in con.execute(
                "SELECT sidecar_file, field, \"values\" FROM sidecar_fields"):
            entry = sidecars.setdefault(_extension(path), {}).setdefault(
                _scrub(field), {"count": 0, "value_shapes": []})
            entry["count"] += 1
            try:
                shape = [_shape(v) for v in json.loads(values)]
            except (ValueError, RecursionError):
                shape = ["<not decoded>"]
            # Long repeated runs say nothing new: keep the first few values.
            shape = shape[:12] + (["..."] if len(shape) > 12 else [])
            if shape not in entry["value_shapes"] and len(entry["value_shapes"]) < 6:
                entry["value_shapes"].append(shape)
        profile["sidecar_fields_by_extension"] = {
            ext: dict(sorted(fields.items())) for ext, fields in sorted(sidecars.items())}
        kinds = {}
        for kind, state, number in con.execute(
                "SELECT kind, file_state, COUNT(*) FROM annotations GROUP BY 1, 2"):
            _count(kinds, "%s (%s)" % (_scrub(kind)[:40], state), number)
        profile["annotation_kinds"] = kinds

        clipping_kinds, unparsed = {}, []
        for kind, added, detail in con.execute(
                "SELECT kind, added_device_local, detail_line FROM clippings"):
            _count(clipping_kinds, _scrub(kind)[:24] if kind else "<not recognised>")
            if (not kind or not added) and len(unparsed) < 10:
                unparsed.append(_mask(detail))
        profile["clippings"] = {"kinds": clipping_kinds,
                                "detail_lines_not_understood_masked": unparsed}

        content = {"name_has_asin": 0, "name_has_cde_key": 0, "sample": 0, "neither": 0}
        containers = {}
        for ext, container, asin, cde, sample in con.execute(
                "SELECT extension, container, asin, cde_key, is_sample FROM content_files"):
            _count(containers, "%s %s" % (ext if _EXTENSION.match(ext or "") else "<other>",
                                          container or "<not recognised>"))
            _count(content, "name_has_asin" if asin else
                   ("name_has_cde_key" if cde else "neither"))
            if sample:
                content["sample"] += 1
        profile["content_files"] = {"name_patterns": content, "containers": containers}

        databases = {}
        for database, table, rows, columns in con.execute(
                'SELECT "database", "table", rows, columns FROM database_inventory'):
            area = _area(database)
            key = (_redact_path(database) if not area.startswith(("documents", "<other"))
                   else "%s/*%s" % (area, _extension(database)))
            databases.setdefault(key, {})[_scrub(table)] = {
                "rows": rows, "columns": ", ".join(_scrub(c) for c in columns.split(", "))}
        profile["databases"] = dict(sorted(databases.items()))

        errors = {}
        for parser, error in con.execute("SELECT parser, error FROM parse_errors"):
            # Keep the kind of error. Drop names and paths the message may quote.
            _count(errors, "%s: %s" % (parser, _error_class(error)))
        profile["parse_errors"] = errors

        if "deleted_entries" in tables:
            profile["deleted_entries"] = {
                "by_recovery": dict(con.execute(
                    "SELECT type || ', ' || recovery, COUNT(*) FROM deleted_entries GROUP BY 1")),
                "by_name_source": dict(con.execute(
                    "SELECT name_source, COUNT(*) FROM deleted_entries GROUP BY 1")),
            }
        con.close()
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)

    out_path = Path(out_path) if out_path else acq / "support_profile.json"
    write_json(out_path, profile)
    return profile, out_path
