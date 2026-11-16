"""Extract files from a raw FAT32 image without mounting it.

Produces the same folder layout as a logical acquisition (files/,
manifest.csv, acquisition_log.json), so `verify` and `parse` work on it.
Deleted directory entries are listed in deleted_entries.csv. Deleted files
that can be read back are written under deleted/.

Names inside an image are untrusted. Each is reduced to a single safe
path component, and two names that would land on the same output path
are kept apart.
"""

import csv
import hashlib
import json
import os
import re

from .fat32 import Fat32Error, Fat32Volume
from .logical import MANIFEST_COLUMNS
from .util import (hash_file, host_facts, long_path, prepare_output,
                   progress_printer, status, utc_now, write_json)

DELETED_COLUMNS = [
    "index", "rel_path", "type", "size", "first_cluster", "entry_offset",
    "modified_stored", "created_stored", "accessed_stored", "attributes",
    "name_source", "recovery", "detail", "recovered_file", "sha256", "md5",
]

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]|CONIN\$|CONOUT\$)$", re.IGNORECASE)


def _safe_component(name):
    """One path component that Windows will store under exactly this name."""
    safe = _UNSAFE.sub("_", name)
    if safe.endswith((" ", ".")):
        safe = safe[:-1] + "_"
    if safe in ("", ".", ".."):
        safe = "_"
    if _RESERVED.match(safe.split(".")[0].strip()):
        safe = "_" + safe
    return safe


def _unique(parent, component, used):
    """Output path for a component, changed if the path is already taken."""
    candidate = parent + "/" + component if parent else component
    if candidate.casefold() in used:
        base, ext = os.path.splitext(component)
        number = 2
        while True:
            candidate = "%s%s~%d%s" % (parent + "/" if parent else "", base, number, ext)
            if candidate.casefold() not in used:
                break
            number += 1
    used.add(candidate.casefold())
    return candidate


def _attributes(attr):
    return "".join(name for bit, name in ((0x01, "R"), (0x02, "H"), (0x04, "S"), (0x20, "A"))
                   if attr & bit)


def _write_stream(chunks, dst):
    """Write chunks to dst. Returns (sha256, md5, bytes written)."""
    sha256, md5, written = hashlib.sha256(), hashlib.md5(), 0
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as out:
        for chunk in chunks:
            sha256.update(chunk)
            md5.update(chunk)
            out.write(chunk)
            written += len(chunk)
        out.flush()
        os.fsync(out.fileno())
    return sha256.hexdigest(), md5.hexdigest(), written


def resolve_source(source):
    """Accept an image acquisition folder or an image file or raw device.

    Returns (image path, source facts for the log).
    """
    source = str(source)
    facts = {"given": source}
    if os.path.isdir(source):
        log_path = os.path.join(source, "acquisition_log.json")
        if not os.path.isfile(log_path):
            raise ValueError("no acquisition_log.json in %s" % source)
        with open(log_path, encoding="utf-8") as fh:
            image_log = json.load(fh)
        if image_log.get("kind") != "image":
            raise ValueError("%s is not an image acquisition" % source)
        image = os.path.join(source, image_log["image_file"])
        facts["image_log_sha256"] = image_log["source_hashes"]["sha256"]
        facts["image_status"] = image_log.get("status")
        facts["device"] = image_log.get("device")
        facts["case"] = image_log.get("case")
    else:
        image = source
    facts["image"] = image
    return image, facts


def extract_image(source, out_dir, case=None, include_deleted=True, quiet=False):
    """Extract a FAT32 image into `out_dir`. Returns the log."""
    image, facts = resolve_source(source)
    is_device = image.startswith("\\\\.\\")
    if not is_device and not os.path.isfile(image):
        raise ValueError("image not found: %s" % image)
    out = prepare_output(out_dir)

    log = {"kind": "logical", "method": "FAT32 reader on a raw image",
           "started_utc": utc_now()}
    log.update(host_facts())
    log["case"] = case if case and any(case.values()) else (facts.get("case") or case or {})
    log["device"] = facts.get("device")
    log["source"] = image
    if not is_device:
        if not quiet:
            status("hashing the image before extraction")
        facts["image_sha256"] = hash_file(image)["sha256"]
        if "image_log_sha256" in facts:
            facts["image_matches_its_log"] = facts["image_sha256"] == facts["image_log_sha256"]
            if not facts["image_matches_its_log"]:
                raise ValueError("the image does not match the hash in its acquisition log")
    log["source_image"] = facts

    counts = {"files_ok": 0, "files_error": 0, "folders": 0, "folders_error": 0,
              "other": 0, "bytes_copied": 0, "deleted_entries": 0,
              "deleted_files_recovered": 0, "deleted_bytes_recovered": 0}
    errors = []
    files_root = out / "files"
    files_root.mkdir()
    deleted_rows = []

    with open(image, "rb", buffering=0) as fh:
        volume = Fat32Volume.open(fh)
        volume_bytes = volume.cluster_count * volume.cluster_size
        used_bytes = (volume.cluster_count - volume.free_clusters()) * volume.cluster_size
        report = None if quiet else progress_printer("extract", used_bytes)
        used_paths = set()      # output paths taken, casefolded
        out_dirs = {"": ""}     # folder path on the volume -> output path
        owners = {}             # first cluster -> first file that used it
        with open(out / "manifest.csv", "w", newline="", encoding="utf-8") as mf:
            writer = csv.DictWriter(mf, fieldnames=MANIFEST_COLUMNS)
            writer.writeheader()
            for path, entry in volume.walk(include_deleted=include_deleted):
                kind = "folder" if entry["is_dir"] else "file"
                if entry["deleted"]:
                    counts["deleted_entries"] += 1
                    row = dict.fromkeys(DELETED_COLUMNS, "")
                    row.update({
                        "index": counts["deleted_entries"], "rel_path": path, "type": kind,
                        "size": entry["size"] if kind == "file" else "",
                        "first_cluster": entry["first_cluster"],
                        "entry_offset": entry["entry_offset"],
                        "modified_stored": entry["modified_stored"],
                        "created_stored": entry["created_stored"],
                        "accessed_stored": entry["accessed_stored"],
                        "attributes": _attributes(entry["attr"]),
                        "name_source": entry["name_source"],
                        "recovery": "not attempted" if kind == "folder" else "not recovered",
                        "detail": entry["problem"],
                    })
                    if kind == "file":
                        clusters, reason = volume.deleted_file_clusters(entry)
                        row["detail"] = reason
                        if clusters:
                            rel = "deleted/%06d/%s" % (row["index"], _safe_component(entry["name"]))
                            dst = long_path(out / rel)
                            try:
                                sha, md5, written = _write_stream(
                                    volume.read_clusters(clusters, entry["size"]), dst)
                                if hash_file(dst)["sha256"] != sha:
                                    raise ValueError("recovered copy does not match the data read")
                            except (OSError, ValueError) as exc:
                                # Do not leave a partial file that no record lists.
                                try:
                                    os.remove(dst)
                                    os.rmdir(os.path.dirname(dst))
                                except OSError:
                                    pass
                                row["detail"] = "read failed: %s" % exc
                                errors.append({"rel_path": path, "type": "deleted file",
                                               "detail": row["detail"]})
                            else:
                                row.update({
                                    "recovery": "recovered", "recovered_file": rel,
                                    "sha256": sha, "md5": md5,
                                    "detail": "read as one unbroken run of unallocated "
                                              "clusters; content not confirmed"})
                                counts["deleted_files_recovered"] += 1
                                counts["deleted_bytes_recovered"] += written
                    deleted_rows.append(row)
                    continue

                parent = entry["parent"]
                component = _safe_component(entry["name"])
                safe = _unique(out_dirs.get(parent, _safe_component(parent)), component, used_paths)
                notes = []
                if safe.rpartition("/")[2] != entry["name"]:
                    notes.append("name on the volume: %r" % entry["name"])
                row = dict.fromkeys(MANIFEST_COLUMNS, "")
                row.update({
                    "rel_path": safe, "type": kind,
                    "modified_stored": entry["modified_stored"],
                    "created_stored": entry["created_stored"],
                    "accessed_stored": entry["accessed_stored"],
                    "attributes": _attributes(entry["attr"]),
                    "status": "ok",
                })
                problem = ""
                try:
                    if kind == "folder":
                        out_dirs[path] = safe
                        os.makedirs(long_path(files_root / safe), exist_ok=True)
                        problem = entry["problem"]
                    else:
                        row["size"] = entry["size"]
                        clusters, problem = volume.file_clusters(entry)
                        first = entry["first_cluster"]
                        if entry["size"] and first in owners:
                            problem = problem or ("shares its clusters with %r" % owners[first])
                        elif entry["size"]:
                            owners[first] = safe
                        if counts["bytes_copied"] + entry["size"] > volume_bytes:
                            raise ValueError("not extracted: the files listed hold more data "
                                             "than the volume can store")
                        dst = long_path(files_root / safe)
                        sha, md5, written = _write_stream(
                            volume.read_clusters(clusters, entry["size"]), dst)
                        row["sha256"], row["md5"] = sha, md5
                        counts["bytes_copied"] += written
                        if not problem and written != entry["size"]:
                            problem = "read %d bytes, entry says %d" % (written, entry["size"])
                        if not problem and hash_file(dst)["sha256"] != sha:
                            problem = "copy does not match the data read"
                        if report:
                            report(counts["bytes_copied"])
                except (OSError, ValueError) as exc:
                    problem = str(exc)
                if problem:
                    row["status"] = "error"
                    notes.insert(0, problem)
                    errors.append({"rel_path": safe, "type": kind, "detail": problem})
                row["detail"] = "; ".join(notes)
                if kind == "folder":
                    counts["folders_error" if problem else "folders"] += 1
                else:
                    counts["files_error" if problem else "files_ok"] += 1
                writer.writerow(row)
        if report:
            report(counts["bytes_copied"], final=True)
        for path, reason in volume.problems:
            errors.append({"rel_path": path, "type": "folder", "detail": reason})
        log["volume"] = {
            "offset_in_image": volume.offset,
            "label": volume.label or volume.boot_label,
            "serial": volume.serial,
            "bytes_per_sector": volume.bytes_per_sector,
            "cluster_size": volume.cluster_size,
            "clusters": volume.cluster_count,
            "free_clusters": volume.free_clusters(),
        }

    if include_deleted:
        with open(out / "deleted_entries.csv", "w", newline="", encoding="utf-8") as df:
            writer = csv.DictWriter(df, fieldnames=DELETED_COLUMNS)
            writer.writeheader()
            writer.writerows(deleted_rows)
        log["deleted_entries_sha256"] = hash_file(out / "deleted_entries.csv")["sha256"]
    log["finished_utc"] = utc_now()
    log["counts"] = counts
    log["errors"] = errors
    log["manifest_sha256"] = hash_file(out / "manifest.csv")["sha256"]
    log["timestamp_note"] = (
        "All times are FAT directory values as stored, with no time zone and "
        "no conversion by the host.")
    log["deleted_note"] = (
        "A recovered deleted file was read as one unbroken run of unallocated "
        "clusters starting at the cluster in its directory entry. The content "
        "is not confirmed to be the original file.")
    write_json(out / "acquisition_log.json", log)
    return log


__all__ = ["extract_image", "resolve_source", "Fat32Error"]
