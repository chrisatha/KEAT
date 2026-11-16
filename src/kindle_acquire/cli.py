"""Command line for kindle-acquire."""

import argparse
import json
import sys

from . import __version__


def _case(args):
    return {
        "case_id": args.case or "",
        "evidence_id": args.evidence or "",
        "examiner": args.examiner or "",
        "notes": args.notes or "",
    }


def _add_case_args(parser):
    parser.add_argument("--case", help="case or matter number")
    parser.add_argument("--evidence", help="evidence item number")
    parser.add_argument("--examiner", help="examiner name")
    parser.add_argument("--notes", help="free-text note for the log")


def _pick_device(mode_needed=None):
    from .device import detect
    found = detect()
    devices = found["devices"]
    if mode_needed:
        devices = [d for d in devices if d["mode"] == mode_needed]
    if len(devices) != 1:
        raise SystemExit(
            "expected one connected Kindle%s, found %d. Use --source."
            % (" in %s mode" % mode_needed if mode_needed else "", len(devices)))
    device = devices[0]
    device["usb_write_protect_policy"] = found["usb_write_protect_policy"]
    return device


def open_window(force_tk=False):
    """The Qt window when PySide6 or PyQt6 is installed, else the Tkinter one."""
    if not force_tk:
        try:
            from .gui_qt import main as qt_main
        except ImportError:
            pass
        else:
            return qt_main()
    from .gui import main as tk_main
    return tk_main()


def cmd_gui(args):
    return open_window(force_tk=args.tk)


def cmd_detect(args):
    from .device import describe, detect
    found = detect()
    if args.json:
        print(json.dumps(found, indent=2))
    else:
        print(describe(found))
    return 0 if found["devices"] else 1


def cmd_logical(args):
    from .logical import acquire_logical
    device = None
    source = args.source
    if not args.no_detect:
        try:
            device = _pick_device("mass-storage")
        except SystemExit:
            if not source:
                raise
    if not source:
        if not device["volumes"]:
            raise SystemExit("the Kindle disk has no mounted volume")
        source = device["volumes"][0]["root"]
    if device and device.get("usb_write_protect_policy") != 1 \
            and not device["disk"]["is_read_only"]:
        sys.stderr.write(
            "WARNING: the volume is mounted read-write. Windows changes a "
            "mounted volume. Use a write blocker for evidence.\n")
    log = acquire_logical(source, args.out, case=_case(args), device=device,
                          quiet=args.quiet)
    c = log["counts"]
    print("files copied and verified: %d" % c["files_ok"])
    print("files with errors: %d" % c["files_error"])
    print("folders: %d (errors: %d)" % (c["folders"], c["folders_error"]))
    print("bytes copied: %d" % c["bytes_copied"])
    print("manifest SHA-256: %s" % log["manifest_sha256"])
    return 0 if not log["errors"] else 2


def cmd_image(args):
    from .image import acquire_image
    device = None
    source = args.source
    if not source:
        device = _pick_device("mass-storage")
        source = "\\\\.\\PhysicalDrive%s" % device["disk"]["number"]
    name = args.name
    if not name:
        name = args.evidence or (
            "kindle_%s" % device["usb_serial"] if device else "kindle")
    sector = device["disk"]["logical_sector_size"] if device else 512
    log = acquire_image(source, args.out, case=_case(args), device=device,
                        sector=sector, verify_source=args.verify_source,
                        name=name, quiet=args.quiet)
    print("image: %s  (%d bytes)" % (log["image_file"], log["source_size_bytes"]))
    for algo in ("md5", "sha1", "sha256"):
        print("%-6s %s" % (algo, log["source_hashes"][algo]))
    print("image file matches the bytes read: %s" % log["image_matches_source_read"])
    if args.verify_source:
        print("second read of the source matches: %s" % log["source_second_read_matches"])
    print("unreadable bytes: %d" % log["unreadable_bytes"])
    if log["status"] == "incomplete":
        print("INCOMPLETE: %s. The image holds %d of %d bytes." % (
            log["stopped_reason"], log["image_size_bytes"], log["source_size_bytes"]))
    return 0 if log["status"] == "ok" else 2


def cmd_extract(args):
    from .extract import extract_image
    log = extract_image(args.source, args.out, case=_case(args),
                        include_deleted=not args.no_deleted, quiet=args.quiet)
    c = log["counts"]
    print("files extracted and verified: %d" % c["files_ok"])
    print("files with errors: %d" % c["files_error"])
    print("folders: %d (errors: %d)" % (c["folders"], c["folders_error"]))
    print("bytes extracted: %d" % c["bytes_copied"])
    if not args.no_deleted:
        print("deleted entries listed: %d" % c["deleted_entries"])
        print("deleted files read back: %d (%d bytes)" % (
            c["deleted_files_recovered"], c["deleted_bytes_recovered"]))
    print("manifest SHA-256: %s" % log["manifest_sha256"])
    return 0 if not log["errors"] else 2


def cmd_verify(args):
    from .verify import verify
    result = verify(args.acquisition)
    for problem in result["problems"]:
        print("PROBLEM: %s" % problem)
    print("%s acquisition, %d item(s) re-hashed: %s" % (
        result["kind"], result["checked"], "OK" if result["ok"] else "FAILED"))
    return 0 if result["ok"] else 2


def cmd_compare(args):
    from .compare import compare, describe, write_differences
    result = compare(args.a, args.b)
    print(describe(result))
    if args.out:
        write_differences(result, args.out)
        print("differences written to %s" % args.out)
    return 0 if result["same_content"] else 2


def cmd_survey(args):
    from .survey import survey
    profile, path = survey(args.acquisition, args.out)
    print("support profile written to %s" % path)
    print("files: %d, folders: %d, parse errors: %d kinds" % (
        profile["counts"]["files"], profile["counts"]["folders"], len(profile["parse_errors"])))
    print("It holds structure, not content. Read it before sharing.")
    return 0


def cmd_parse(args):
    from .report import parse_acquisition
    summary = parse_acquisition(args.acquisition, args.out, known=args.known)
    for key, value in summary.items():
        print("%s: %s" % (key, value))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="kindle-acquire",
        description="Forensic acquisition of Amazon Kindle e-readers. "
                    "Copies data as stored. Does not remove DRM.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("gui", help="open the desktop window")
    p.add_argument("--tk", action="store_true",
                   help="use the Tkinter window even when Qt is installed")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("detect", help="list connected Kindle devices")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("logical", help="copy and hash every file on the mounted volume")
    p.add_argument("--out", required=True, help="new or empty output folder")
    p.add_argument("--source", help="volume root, e.g. D:\\ (default: the connected Kindle)")
    p.add_argument("--no-detect", action="store_true",
                   help="do not query the device (use with --source on a folder)")
    p.add_argument("--quiet", action="store_true")
    _add_case_args(p)
    p.set_defaults(func=cmd_logical)

    p = sub.add_parser("image", help="raw image of the Kindle disk (needs administrator)")
    p.add_argument("--out", required=True, help="new or empty output folder")
    p.add_argument("--source", help="raw device or file (default: the connected Kindle)")
    p.add_argument("--name", help="image file name without extension")
    p.add_argument("--verify-source", action="store_true",
                   help="read the source a second time and compare hashes")
    p.add_argument("--quiet", action="store_true")
    _add_case_args(p)
    p.set_defaults(func=cmd_image)

    p = sub.add_parser("extract", help="extract files and deleted entries from a raw image")
    p.add_argument("source", help="image acquisition folder, or a raw image file")
    p.add_argument("--out", required=True, help="new or empty output folder")
    p.add_argument("--no-deleted", action="store_true",
                   help="do not list or read back deleted entries")
    p.add_argument("--quiet", action="store_true")
    _add_case_args(p)
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("verify", help="re-hash an acquisition folder against its records")
    p.add_argument("acquisition", help="acquisition folder")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("parse", help="parse a logical acquisition into SQLite and CSV")
    p.add_argument("acquisition", help="logical acquisition folder")
    p.add_argument("--out", help="output folder (default: <acquisition>/parsed)")
    p.add_argument("--known", help="reference files to look for: a folder of files, "
                                   "or a text/CSV file of MD5 or SHA-256 values")
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("compare", help="compare the records of two acquisitions")
    p.add_argument("a", help="first acquisition folder")
    p.add_argument("b", help="second acquisition folder")
    p.add_argument("--out", help="write every difference to this CSV file")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("survey", help="write a content-free profile of a device's structure")
    p.add_argument("acquisition", help="logical or extracted acquisition folder")
    p.add_argument("--out", help="output file (default: <acquisition>/support_profile.json)")
    p.set_defaults(func=cmd_survey)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, OSError, RuntimeError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
