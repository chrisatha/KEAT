"""Logical acquisition: copy every file from a mounted Kindle volume.

Each file is hashed while it is read from the device. The copy is then read
back and its hash compared. Every file, folder and error is written to
manifest.csv. Nothing is skipped silently.
"""

import csv
import hashlib
import os
import shutil
import stat
from pathlib import Path

from .device import volume_info
from .util import (CHUNK, hash_file, host_facts, long_path, ns_to_utc,
                   prepare_output, progress_printer, refuse_output_on_device,
                   utc_now, write_json)

MANIFEST_COLUMNS = [
    "rel_path", "type", "size", "sha256", "md5",
    "modified_stored", "created_stored", "accessed_stored",
    "modified_utc", "created_utc", "accessed_utc", "attributes",
    "status", "detail",
]

# Set per run. On a FAT volume the directory entry holds a wall-clock time
# with no time zone. Windows reads it as host local time, using the host's
# current offset, and reports UTC. Adding the offset back gives the value
# as stored. None means the source is not FAT and the columns stay empty.
_STORED_OFFSET_NS = None


def _stored(ns):
    if _STORED_OFFSET_NS is None:
        return ""
    return ns_to_utc(ns + _STORED_OFFSET_NS)[:19]


def _attributes(st):
    value = getattr(st, "st_file_attributes", 0)
    names = []
    for flag, name in ((stat.FILE_ATTRIBUTE_READONLY, "R"),
                       (stat.FILE_ATTRIBUTE_HIDDEN, "H"),
                       (stat.FILE_ATTRIBUTE_SYSTEM, "S"),
                       (stat.FILE_ATTRIBUTE_ARCHIVE, "A")):
        if value & flag:
            names.append(name)
    return "".join(names)


def _row(rel_path, kind, st=None, **extra):
    row = dict.fromkeys(MANIFEST_COLUMNS, "")
    row["rel_path"] = rel_path
    row["type"] = kind
    if st is not None:
        row["size"] = st.st_size if kind == "file" else ""
        row["modified_stored"] = _stored(st.st_mtime_ns)
        row["created_stored"] = _stored(st.st_ctime_ns)
        row["accessed_stored"] = _stored(st.st_atime_ns)[:10]
        row["modified_utc"] = ns_to_utc(st.st_mtime_ns)
        row["created_utc"] = ns_to_utc(st.st_ctime_ns)
        row["accessed_utc"] = ns_to_utc(st.st_atime_ns)
        row["attributes"] = _attributes(st)
    row.update(extra)
    return row


def _copy_file(src, dst):
    """Copy src to dst. Returns (row fields, bytes copied)."""
    st_before = os.stat(src)
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()
    copied = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            block = fin.read(CHUNK)
            if not block:
                break
            sha256.update(block)
            md5.update(block)
            fout.write(block)
            copied += len(block)
        fout.flush()
        os.fsync(fout.fileno())
    st_after = os.stat(src)
    fields = {"sha256": sha256.hexdigest(), "md5": md5.hexdigest(),
              "status": "ok", "detail": ""}
    if copied != st_before.st_size:
        fields["status"] = "error"
        fields["detail"] = "read %d bytes, directory entry says %d" % (
            copied, st_before.st_size)
    elif (st_after.st_size != st_before.st_size
          or st_after.st_mtime_ns != st_before.st_mtime_ns):
        fields["status"] = "error"
        fields["detail"] = "source changed during the copy"
    else:
        check = hash_file(dst)["sha256"]
        if check != fields["sha256"]:
            fields["status"] = "error"
            fields["detail"] = "copy does not match the source hash (%s)" % check
    try:
        os.utime(dst, ns=(st_before.st_atime_ns, st_before.st_mtime_ns))
    except OSError:
        pass
    return st_before, fields, copied


def acquire_logical(source, out_dir, case=None, device=None, quiet=False):
    """Copy the tree under `source` into `out_dir`/files.

    Returns the acquisition log (also written to acquisition_log.json).
    """
    source = os.path.abspath(str(source))
    if not os.path.isdir(source):
        raise ValueError("source is not a folder: %s" % source)
    refuse_output_on_device(out_dir, device)
    out = prepare_output(out_dir, source)
    files_root = out / "files"
    files_root.mkdir()

    log = {"kind": "logical", "started_utc": utc_now()}
    log.update(host_facts())
    log["case"] = case or {}
    log["device"] = device
    log["source"] = source
    global _STORED_OFFSET_NS
    _STORED_OFFSET_NS = None
    drive = os.path.splitdrive(source)[0]
    if drive:
        log["source_volume"] = volume_info(drive + "\\")
        if log["source_volume"].get("filesystem", "").upper().startswith("FAT"):
            _STORED_OFFSET_NS = log["host_utc_offset_seconds"] * 1000000000
    try:
        usage = shutil.disk_usage(source)
        log["source_volume_bytes"] = {"total": usage.total, "used": usage.used,
                                      "free": usage.free}
    except OSError as exc:
        log["source_volume_bytes"] = {"error": str(exc)}
    total_hint = log["source_volume_bytes"].get("used", 0)
    report = None if quiet else progress_printer("copy", total_hint)

    counts = {"files_ok": 0, "files_error": 0, "folders": 0,
              "folders_error": 0, "other": 0, "bytes_copied": 0}
    errors = []

    manifest_path = out / "manifest.csv"
    with open(manifest_path, "w", newline="", encoding="utf-8") as mf:
        writer = csv.DictWriter(mf, fieldnames=MANIFEST_COLUMNS)
        writer.writeheader()

        def emit(row):
            writer.writerow(row)
            if row["status"] == "error":
                errors.append({"rel_path": row["rel_path"], "type": row["type"],
                               "detail": row["detail"]})

        stack = [""]
        while stack:
            rel_dir = stack.pop()
            abs_dir = os.path.join(source, rel_dir) if rel_dir else source
            try:
                with os.scandir(long_path(abs_dir)) as it:
                    entries = sorted(it, key=lambda e: e.name.casefold())
            except OSError as exc:
                counts["folders_error"] += 1
                emit(_row(rel_dir or ".", "folder", status="error",
                          detail="cannot list folder: %s" % exc))
                continue
            sub_dirs = []
            for entry in entries:
                rel = os.path.join(rel_dir, entry.name) if rel_dir else entry.name
                rel_posix = rel.replace("\\", "/")
                dst = long_path(files_root / rel)
                try:
                    if entry.is_symlink():
                        counts["other"] += 1
                        emit(_row(rel_posix, "link", status="skipped",
                                  detail="link, not followed"))
                    elif entry.is_dir(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                        os.makedirs(dst, exist_ok=True)
                        counts["folders"] += 1
                        emit(_row(rel_posix, "folder", st, status="ok"))
                        sub_dirs.append(rel)
                    elif entry.is_file(follow_symlinks=False):
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        st, fields, copied = _copy_file(long_path(entry.path), dst)
                        counts["bytes_copied"] += copied
                        counts["files_ok" if fields["status"] == "ok"
                               else "files_error"] += 1
                        emit(_row(rel_posix, "file", st, **fields))
                        if report:
                            report(counts["bytes_copied"])
                    else:
                        counts["other"] += 1
                        emit(_row(rel_posix, "other", status="skipped",
                                  detail="not a regular file or folder"))
                except OSError as exc:
                    counts["files_error"] += 1
                    emit(_row(rel_posix, "file", status="error", detail=str(exc)))
            # Depth-first, in name order.
            stack.extend(reversed(sub_dirs))
    if report:
        report(counts["bytes_copied"], final=True)

    log["finished_utc"] = utc_now()
    log["counts"] = counts
    log["errors"] = errors
    log["manifest_sha256"] = hash_file(manifest_path)["sha256"]
    log["timestamp_note"] = (
        "FAT timestamps carry no time zone. The *_utc columns are the host's "
        "conversion, which assumes host local time. The *_stored columns are "
        "the wall-clock values as stored on the volume (the host conversion "
        "reversed with host_utc_offset_seconds). They are filled for FAT "
        "sources only. FAT access times are dates only.")
    log["log_sha256_file"] = "acquisition_log.json.sha256"
    write_json(out / "acquisition_log.json", log)
    return log
