"""Re-hash an acquisition folder and compare it with its own records."""

import csv
import json
import os
from pathlib import Path

from .util import hash_file, long_path


def _unlisted(root, prefix, listed, record_name, result):
    """Report files under `root` that the record does not list."""
    base = long_path(root)

    def failed(exc):
        result["problems"].append("cannot list %s: %s" % (exc.filename, exc.strerror))

    for folder, _dirs, names in os.walk(base, onerror=failed):
        for name in names:
            rel = os.path.relpath(os.path.join(folder, name), base).replace("\\", "/")
            if prefix + rel not in listed:
                result["problems"].append(
                    "%s%s: file is not listed in %s" % (prefix, rel, record_name))


def verify(acq_dir):
    """Return a result dict. result['ok'] is True when everything matches."""
    acq = Path(acq_dir)
    log_path = acq / "acquisition_log.json"
    if not log_path.is_file():
        raise ValueError("no acquisition_log.json in %s" % acq)
    log = json.loads(log_path.read_text(encoding="utf-8"))
    result = {"kind": log.get("kind"), "problems": [], "checked": 0}

    sha_file = Path(str(log_path) + ".sha256")
    if sha_file.is_file():
        recorded = sha_file.read_text(encoding="utf-8").split()[0]
        if recorded != hash_file(log_path)["sha256"]:
            result["problems"].append("acquisition_log.json does not match its .sha256 file")
    else:
        result["problems"].append("acquisition_log.json.sha256 is missing")

    if log.get("kind") == "image":
        image = acq / log["image_file"]
        if not image.is_file():
            result["problems"].append("image file is missing: %s" % log["image_file"])
        else:
            result["checked"] = 1
            if hash_file(image)["sha256"] != log["source_hashes"]["sha256"]:
                result["problems"].append("image hash does not match the log")
        if log.get("status") == "incomplete":
            result["problems"].append(
                "the image is incomplete: %s" % log.get("stopped_reason", "the read stopped early"))
    elif log.get("kind") == "logical":
        manifest = acq / "manifest.csv"
        if hash_file(manifest)["sha256"] != log.get("manifest_sha256"):
            result["problems"].append("manifest.csv does not match the hash in the log")
        expected = set()
        with open(manifest, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row["type"] != "file":
                    continue
                # An error row may have left a partial file. It is listed,
                # so it is not an added file, and it has no hash to check.
                expected.add(row["rel_path"])
                if row["status"] != "ok":
                    continue
                path = acq / "files" / row["rel_path"]
                try:
                    digest = hash_file(path)["sha256"]
                except OSError as exc:
                    result["problems"].append("%s: %s" % (row["rel_path"], exc))
                    continue
                result["checked"] += 1
                if digest != row["sha256"]:
                    result["problems"].append("%s: hash differs from manifest" % row["rel_path"])
        _unlisted(acq / "files", "", expected, "the manifest", result)
        deleted = acq / "deleted_entries.csv"
        recovered = set()
        if deleted.is_file():
            if hash_file(deleted)["sha256"] != log.get("deleted_entries_sha256"):
                result["problems"].append(
                    "deleted_entries.csv does not match the hash in the log")
            with open(deleted, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    if not row["recovered_file"]:
                        continue
                    recovered.add(row["recovered_file"])
                    try:
                        digest = hash_file(acq / row["recovered_file"])["sha256"]
                    except OSError as exc:
                        result["problems"].append("%s: %s" % (row["recovered_file"], exc))
                        continue
                    result["checked"] += 1
                    if digest != row["sha256"]:
                        result["problems"].append(
                            "%s: hash differs from deleted_entries.csv" % row["recovered_file"])
        if (acq / "deleted").is_dir():
            _unlisted(acq / "deleted", "deleted/", recovered, "deleted_entries.csv", result)
    else:
        result["problems"].append("unknown acquisition kind: %r" % log.get("kind"))

    result["ok"] = not result["problems"]
    return result
