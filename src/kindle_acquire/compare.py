"""Compare two acquisitions of the same device.

Uses: checking a tool change against an earlier run (regression), checking
an extraction from an image against a copy through Windows, and showing
what changed on a device between two acquisitions.

Only the records are read: manifest.csv for logical and extracted
acquisitions, the log for images. No file content is re-read. Run
`verify` first if the folders may have been altered.
"""

import csv
import json
from pathlib import Path

# Manifest columns compared when both sides have a value.
FIELDS = ("size", "sha256", "modified_stored", "created_stored", "accessed_stored",
          "attributes", "status")
DIFFERENCE_COLUMNS = ["rel_path", "difference", "a", "b"]


def _load(folder):
    folder = Path(folder)
    log_path = folder / "acquisition_log.json"
    if not log_path.is_file():
        raise ValueError("no acquisition_log.json in %s" % folder)
    log = json.loads(log_path.read_text(encoding="utf-8"))
    rows = {}
    if log.get("kind") == "logical":
        with open(folder / "manifest.csv", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                rows[row["rel_path"]] = row
    return log, rows


def compare(a_dir, b_dir):
    """Return a result dict. result['same_content'] is the headline."""
    log_a, rows_a = _load(a_dir)
    log_b, rows_b = _load(b_dir)
    result = {"a": str(a_dir), "b": str(b_dir),
              "kind_a": log_a.get("kind"), "kind_b": log_b.get("kind"),
              "differences": [], "counts": {}}
    diffs = result["differences"]

    if log_a.get("kind") != log_b.get("kind"):
        raise ValueError("cannot compare a %s acquisition with a %s acquisition"
                         % (log_a.get("kind"), log_b.get("kind")))

    if log_a.get("kind") == "image":
        for algo in ("sha256", "md5", "sha1"):
            a, b = log_a["source_hashes"].get(algo, ""), log_b["source_hashes"].get(algo, "")
            if a != b:
                diffs.append({"rel_path": log_a["image_file"], "difference": algo, "a": a, "b": b})
        if log_a.get("source_size_bytes") != log_b.get("source_size_bytes"):
            diffs.append({"rel_path": log_a["image_file"], "difference": "size",
                          "a": log_a.get("source_size_bytes"), "b": log_b.get("source_size_bytes")})
        result["same_content"] = not diffs
        result["counts"] = {"images_compared": 1, "differences": len(diffs)}
        return result
    if log_a.get("kind") != "logical":
        raise ValueError("unknown acquisition kind: %r" % log_a.get("kind"))

    counts = {"entries_a": len(rows_a), "entries_b": len(rows_b),
              "files_in_both": 0, "files_same_sha256": 0}
    for path in sorted(set(rows_a) - set(rows_b)):
        diffs.append({"rel_path": path, "difference": "only in a",
                      "a": rows_a[path]["type"], "b": ""})
    for path in sorted(set(rows_b) - set(rows_a)):
        diffs.append({"rel_path": path, "difference": "only in b",
                      "a": "", "b": rows_b[path]["type"]})
    for path in sorted(set(rows_a) & set(rows_b)):
        a, b = rows_a[path], rows_b[path]
        if a["type"] != b["type"]:
            diffs.append({"rel_path": path, "difference": "type", "a": a["type"], "b": b["type"]})
            continue
        if a["type"] == "file":
            counts["files_in_both"] += 1
            if a["sha256"] and a["sha256"] == b["sha256"]:
                counts["files_same_sha256"] += 1
        for field in FIELDS:
            left, right = a.get(field, ""), b.get(field, "")
            # A column one side does not record (an older manifest, or a
            # source that is not FAT) is not a difference.
            if left != right and left != "" and right != "":
                diffs.append({"rel_path": path, "difference": field, "a": left, "b": right})

    by_kind = {}
    for diff in diffs:
        by_kind[diff["difference"]] = by_kind.get(diff["difference"], 0) + 1
    counts["differences"] = len(diffs)
    counts["by_difference"] = by_kind
    result["counts"] = counts
    content_kinds = {"only in a", "only in b", "type", "size", "sha256", "status"}
    result["same_content"] = not any(d["difference"] in content_kinds for d in diffs)
    result["same_records"] = not diffs
    return result


def write_differences(result, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=DIFFERENCE_COLUMNS)
        writer.writeheader()
        writer.writerows(result["differences"])


def describe(result):
    lines = ["a: %s" % result["a"], "b: %s" % result["b"]]
    counts = result["counts"]
    if result["kind_a"] == "image":
        lines.append("images are %s" % ("identical" if result["same_content"] else "DIFFERENT"))
    else:
        lines.append("entries: a %d, b %d" % (counts["entries_a"], counts["entries_b"]))
        lines.append("files in both: %d, with the same SHA-256: %d" % (
            counts["files_in_both"], counts["files_same_sha256"]))
        for kind, number in sorted(counts["by_difference"].items()):
            lines.append("  %-18s %d" % (kind, number))
        lines.append("content: %s" % ("same files, same hashes" if result["same_content"]
                                      else "DIFFERENT"))
        if result["same_content"] and not result["same_records"]:
            lines.append("records: times or attributes differ, see the difference list")
    return "\n".join(lines)
