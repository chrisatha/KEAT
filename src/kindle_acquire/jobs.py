"""The acquisition steps as the windows run them.

Shared by the Qt and Tkinter front ends. Each function runs on a worker
thread, reports through the `say` callback, and raises on failure.
"""

import ctypes
import os
import re
import sys
import time
from pathlib import Path


def asset(name):
    """Path of a file shipped in the package's assets folder."""
    return str(Path(__file__).resolve().parent / "assets" / name)


def set_app_id():
    """Give the window its own taskbar entry and icon instead of Python's."""
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("chrisatha.keat")
        except (AttributeError, OSError):
            pass


def is_admin():
    if os.name != "nt":
        return os.geteuid() == 0
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def relaunch_elevated():
    """Start a second copy of the window with administrator rights."""
    # No path goes on the command line: the folder that holds the package is
    # the working directory, where "-m" looks first.
    src = str(Path(__file__).resolve().parents[1])
    exe = Path(sys.executable)
    windowed = exe.with_name("pythonw.exe")
    if windowed.is_file():
        exe = windowed
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", str(exe), "-m kindle_acquire gui", src, 1)
    return result > 32


def safe_label(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")


def new_output(base, evidence, kind, device=None, fallback="kindle"):
    """A new output folder path: <label>_<kind>_<date>_<time> under base."""
    label = safe_label(evidence or "")
    if not label and device:
        label = "kindle_%s" % safe_label(device["usb_serial"])
    name = "%s_%s_%s" % (label or safe_label(fallback) or "kindle", kind,
                         time.strftime("%Y%m%d_%H%M%S"))
    return str(Path(base) / name)


def pick_device(found, mode="mass-storage"):
    """The single connected Kindle in `mode`. Returns (device, problem text)."""
    devices = [d for d in (found or {}).get("devices", []) if d["mode"] == mode]
    if len(devices) != 1:
        return None, ("Expected one connected Kindle in %s mode, found %d.\n"
                      "Connect one device and press Detect." % (mode, len(devices)))
    device = dict(devices[0])
    device["usb_write_protect_policy"] = found.get("usb_write_protect_policy")
    return device, ""


def is_write_protected(device):
    return bool(device.get("usb_write_protect_policy") == 1
                or (device.get("disk") or {}).get("is_read_only"))


def recheck(device):
    """Detect again. Returns (the same device as connected now, detect result).

    The list on screen can be stale: a different disk may have taken the
    disk number since Detect was pressed.
    """
    from .device import detect
    found = detect()
    for current in found["devices"]:
        if current["usb_serial"] == device["usb_serial"] and current["mode"] == device["mode"]:
            current = dict(current)
            current["usb_write_protect_policy"] = found.get("usb_write_protect_policy")
            return current, found
    raise RuntimeError("the Kindle with serial %s is no longer connected" % device["usb_serial"])


def _report_counts(log, say):
    c = log["counts"]
    say("Files written and verified: %d. Files with errors: %d. Folders: %d. Bytes: %d" % (
        c["files_ok"], c["files_error"], c["folders"], c["bytes_copied"]))
    say("Manifest SHA-256: %s" % log["manifest_sha256"])
    for error in log["errors"]:
        say("ERROR %s: %s" % (error["rel_path"], error["detail"]))
    if log["errors"]:
        raise RuntimeError("%d error(s), see manifest.csv" % len(log["errors"]))


def run_image(device, out, case, verify_source, say):
    from .image import acquire_image
    source = "\\\\.\\PhysicalDrive%s" % device["disk"]["number"]
    name = case.get("evidence_id") or "kindle_%s" % device["usb_serial"]
    say("Imaging %s into %s" % (source, out))
    log = acquire_image(source, out, case=case, device=device,
                        sector=device["disk"]["logical_sector_size"],
                        verify_source=verify_source, name=name)
    say("Image %s, %d bytes" % (log["image_file"], log["source_size_bytes"]))
    for algo in ("md5", "sha1", "sha256"):
        say("  %-6s %s" % (algo, log["source_hashes"][algo]))
    say("Image file matches the bytes read: %s" % log["image_matches_source_read"])
    if verify_source:
        say("Second read of the device matches: %s" % log["source_second_read_matches"])
    say("Unreadable bytes: %d" % log["unreadable_bytes"])
    if log["status"] == "incomplete":
        raise RuntimeError("%s. The image holds %d of %d bytes" % (
            log["stopped_reason"], log["image_size_bytes"], log["source_size_bytes"]))
    if log["status"] != "ok":
        raise RuntimeError("the image needs review, see acquisition_log.json")
    return log


def run_logical(device, out, case, say):
    from .logical import acquire_logical
    if not device["volumes"]:
        raise RuntimeError("the Kindle disk has no mounted volume")
    source = device["volumes"][0]["root"]
    say("Copying %s into %s" % (source, out))
    log = acquire_logical(source, out, case=case, device=device)
    _report_counts(log, say)
    return log


def run_extract(source, out, case, say):
    from .extract import extract_image
    say("Extracting %s into %s" % (source, out))
    log = extract_image(source, out, case=case)
    c = log["counts"]
    say("Deleted entries listed: %d. Deleted files read back: %d (%d bytes)" % (
        c["deleted_entries"], c["deleted_files_recovered"], c["deleted_bytes_recovered"]))
    _report_counts(log, say)
    return log


def run_parse(acquisition, known, say):
    """Returns the folder that holds kindle_parsed.db."""
    from .report import parse_acquisition
    summary = parse_acquisition(acquisition, known=known or None)
    parsed = summary.pop("output")
    report = summary.pop("report")
    width = max(len(k) for k in summary)
    for key, value in summary.items():
        say("  %-*s %s" % (width, key, value))
    say("Examiner report: %s" % report)
    return parsed


def open_report(folder):
    """Open the examiner report of a parsed acquisition in the default browser."""
    import webbrowser
    from .examiner import find_parsed
    path = find_parsed(folder) / "report.html"
    if not path.is_file():
        raise ValueError("no report.html in %s. Parse the acquisition again." % path.parent)
    webbrowser.open(path.resolve().as_uri())
    return str(path)


def run_compare(first, second, say):
    from .compare import compare, describe
    result = compare(first, second)
    for line in describe(result).splitlines():
        say(line)
    shown = 0
    for diff in result["differences"]:
        if shown == 40:
            say("  ... %d more. Use 'kindle-acquire compare --out' for the full list."
                % (len(result["differences"]) - shown))
            break
        say("  %s: %s (a: %s, b: %s)" % (diff["difference"], diff["rel_path"],
                                         diff["a"], diff["b"]))
        shown += 1
    if not result["same_content"]:
        raise RuntimeError("the two acquisitions do not hold the same content")
    return result


def run_survey(acquisition, say):
    """Returns the path of the support profile."""
    from .survey import survey
    profile, path = survey(acquisition)
    say("Support profile written to %s" % path)
    say("Files: %d. Folders: %d. Kinds of parse error: %d." % (
        profile["counts"]["files"], profile["counts"]["folders"], len(profile["parse_errors"])))
    say("It holds structure, not content. Read it before sharing.")
    return str(path)


def run_verify(acquisition, say):
    from .verify import verify
    result = verify(acquisition)
    for problem in result["problems"]:
        say("PROBLEM: %s" % problem)
    say("%s acquisition, %d item(s) re-hashed: %s" % (
        result["kind"], result["checked"], "OK" if result["ok"] else "FAILED"))
    if not result["ok"]:
        raise RuntimeError("verification found %d problem(s)" % len(result["problems"]))
    return result
