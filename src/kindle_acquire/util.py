"""Shared helpers: time formatting, long paths, hashing, host facts."""

import getpass
import hashlib
import json
import os
import platform
import re
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

CHUNK = 1024 * 1024


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ns_to_utc(ns):
    """Nanoseconds since the epoch to an ISO 8601 UTC string."""
    try:
        return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def long_path(path):
    """Absolute path that is safe beyond 260 characters on Windows."""
    s = os.path.abspath(str(path))
    if os.name != "nt" or s.startswith("\\\\?\\") or s.startswith("\\\\.\\"):
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def inside(root, rel):
    """The path of `rel` under `root`, as a Path.

    `rel` comes from a record (a manifest, a log) that may have been written
    by someone else. A path that would leave `root` raises ValueError.
    """
    text = str(rel).replace("\\", "/")
    parts = text.split("/")
    if (not text or text.startswith("/") or os.path.isabs(text)
            or os.path.splitdrive(text)[0]
            or (os.name == "nt" and ":" in text)
            or any(part in ("", ".", "..") for part in parts)):
        raise ValueError("the record names a path outside its folder: %r" % (rel,))
    return Path(root).joinpath(*parts)


_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")
_PLAIN_SIGNED = re.compile(r"^[+-][0-9][0-9.,:]*$")


def csv_safe(value):
    """A CSV cell that a spreadsheet will not run as a formula.

    Text from the device that starts with =, +, -, @, tab or carriage return
    gets a leading apostrophe. A signed number or offset such as -05:00 is
    left as it is. Other values are returned unchanged.
    """
    if (isinstance(value, str) and value.startswith(_FORMULA_LEAD)
            and not _PLAIN_SIGNED.match(value)):
        return "'" + value
    return value


def open_db_readonly(path):
    """Open a SQLite file read-only. Its schema is not trusted."""
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    harden_db(con)
    return con


def harden_db(con):
    """Settings for a database that the tool did not write itself."""
    # Unknown pragmas are ignored by older SQLite versions.
    con.execute("PRAGMA trusted_schema=OFF")
    con.execute("PRAGMA cell_size_check=ON")


def hash_file(path, algorithms=("sha256",)):
    """Hash a file. Returns {algorithm: hexdigest}."""
    hashers = {name: hashlib.new(name) for name in algorithms}
    with open(long_path(path), "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            for h in hashers.values():
                h.update(block)
    return {name: h.hexdigest() for name, h in hashers.items()}


def host_facts():
    """Facts about the examination computer, for the acquisition log."""
    offset = -time.timezone
    if time.daylight and time.localtime().tm_isdst > 0:
        offset = -time.altzone
    return {
        "tool": "kindle-acquire",
        "tool_version": __version__,
        "python": sys.version.split()[0],
        "host": socket.gethostname(),
        "os": platform.platform(),
        "user": getpass.getuser(),
        "host_utc_offset_seconds": offset,
        "host_timezone": time.tzname[0],
        "command_line": sys.argv[1:],
    }


def write_json(path, data):
    """Write JSON and a .sha256 file beside it. Returns the SHA-256."""
    path = Path(path)
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False)
    path.write_text(text + "\n", encoding="utf-8")
    digest = hash_file(path)["sha256"]
    Path(str(path) + ".sha256").write_text(
        "%s  %s\n" % (digest, path.name), encoding="utf-8"
    )
    return digest


def refuse_output_on_device(out_dir, device):
    """Raise if the output folder is on one of the device's own volumes."""
    drive = os.path.splitdrive(os.path.abspath(str(out_dir)))[0].upper()
    for volume in (device or {}).get("volumes") or []:
        if drive and volume.get("root", "")[:2].upper() == drive:
            raise ValueError(
                "the output folder is on the Kindle itself (%s). Choose a folder "
                "on the examination computer." % drive)


def prepare_output(out_dir, source=None):
    """Create the output folder. It must be empty and outside the source."""
    out = Path(os.path.abspath(str(out_dir)))
    if source is not None:
        src = Path(os.path.abspath(str(source)))
        if src.is_dir() and (out == src or src in out.parents):
            raise ValueError("output folder is inside the source: %s" % out)
    if out.exists():
        if not out.is_dir():
            raise ValueError("output path is not a folder: %s" % out)
        if any(out.iterdir()):
            raise ValueError("output folder is not empty: %s" % out)
    else:
        out.mkdir(parents=True)
    return out


# A front end (the GUI) sets this to receive progress instead of stderr:
# hook(label, done_bytes, total_bytes, final). total_bytes is None for a
# plain status line. The hook may raise to cancel the running operation.
PROGRESS_HOOK = None


def status(text):
    """One status line, to the front end's hook or to stderr."""
    if PROGRESS_HOOK is not None:
        PROGRESS_HOOK(text, 0, None, False)
    elif sys.stderr is not None:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()


def progress_printer(label, total):
    """Return a callback(done_bytes) that reports progress."""
    state = {"last": 0.0}

    def report(done, final=False):
        if PROGRESS_HOOK is not None:
            PROGRESS_HOOK(label, done, total, final)
            return
        if sys.stderr is None:
            return
        now = time.monotonic()
        if not final and now - state["last"] < 1.0:
            return
        state["last"] = now
        if total:
            pct = 100.0 * done / total
            msg = "\r%s: %5.1f%%  %.1f / %.1f MiB" % (
                label, pct, done / 1048576.0, total / 1048576.0)
        else:
            msg = "\r%s: %.1f MiB" % (label, done / 1048576.0)
        sys.stderr.write(msg + ("\n" if final else ""))
        sys.stderr.flush()

    return report
