"""Parser for 'My Clippings.txt'.

Each entry is: a title line, a detail line starting with '- ', a blank
line, the text, then a line of ten '=' characters. The 'Added on' time is
device local time with no time zone. The detail line is kept verbatim
because its wording depends on the device language.
"""

import re
from datetime import datetime

SEPARATOR = "=========="

_KIND = re.compile(r"^-\s*Your\s+(\w+)", re.IGNORECASE)
_PAGE = re.compile(r"\bpage\s+([0-9ivxlcdm]+(?:-[0-9ivxlcdm]+)?)", re.IGNORECASE)
_LOCATION = re.compile(r"\blocation\s+(\d+(?:-\d+)?)", re.IGNORECASE)
_ADDED = re.compile(r"Added on\s+(.+?)\s*$", re.IGNORECASE)
_AUTHOR = re.compile(r"^(.*)\s\(([^()]*)\)\s*$")

_DATE_FORMATS = (
    "%A, %B %d, %Y %I:%M:%S %p",
    "%A, %B %d, %Y %H:%M:%S",
    "%A, %d %B %Y %H:%M:%S",
    "%A, %d %B %Y %I:%M:%S %p",
)


def _parse_added(text):
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).isoformat(timespec="seconds")
        except ValueError:
            continue
    return ""


def parse_clippings(text):
    """Return a list of entries in file order."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    entries = []
    block = []
    for line in text.split("\n"):
        if line.strip() == SEPARATOR:
            entry = _entry(block, len(entries) + 1)
            if entry:
                entries.append(entry)
            block = []
        else:
            block.append(line)
    entry = _entry(block, len(entries) + 1)
    if entry:
        entries.append(entry)
    return entries


def _entry(lines, index):
    lines = [ln.replace("﻿", "") for ln in lines]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return None
    title_line = lines[0].strip()
    detail = lines[1].strip() if len(lines) > 1 else ""
    body = "\n".join(lines[2:]).strip("\n").strip()
    title, author = title_line, ""
    match = _AUTHOR.match(title_line)
    if match:
        title, author = match.group(1).strip(), match.group(2).strip()
    kind = _KIND.search(detail)
    page = _PAGE.search(detail)
    location = _LOCATION.search(detail)
    added = _ADDED.search(detail)
    added_raw = added.group(1) if added else ""
    return {
        "entry": index,
        "title_line": title_line,
        "title": title,
        "author": author,
        "kind": kind.group(1).lower() if kind else "",
        "page": page.group(1) if page else "",
        "location": location.group(1) if location else "",
        "added_raw": added_raw,
        "added_device_local": _parse_added(added_raw),
        "detail_line": detail,
        "text": body,
    }
