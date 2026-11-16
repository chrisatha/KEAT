"""Physical acquisition: raw image of a Kindle's user storage disk.

The source is opened read-only and read sector by sector into a raw (dd)
image. MD5, SHA-1 and SHA-256 are computed over the bytes read. The image
file is then read back and compared. Sectors that cannot be read are
zero-filled and listed in the log.

Reading \\\\.\\PhysicalDriveN needs an elevated (administrator) prompt.
"""

import ctypes
import hashlib
import os
import re
import shutil

from .util import (CHUNK, hash_file, host_facts, prepare_output,
                   progress_printer, refuse_output_on_device, utc_now, write_json)

IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
DEFAULT_SECTOR = 512


def _is_raw_device(path):
    return os.name == "nt" and path.startswith("\\\\.\\")


def source_size(fh, path):
    """Size in bytes of an open source (raw device or regular file)."""
    if _is_raw_device(path):
        import msvcrt
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.DeviceIoControl.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID]
        kernel32.DeviceIoControl.restype = wintypes.BOOL
        length = ctypes.c_longlong(0)
        returned = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(
            wintypes.HANDLE(msvcrt.get_osfhandle(fh.fileno())),
            IOCTL_DISK_GET_LENGTH_INFO, None, 0,
            ctypes.byref(length), ctypes.sizeof(length),
            ctypes.byref(returned), None)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return length.value
    return os.fstat(fh.fileno()).st_size


# A source that returns nothing for this many bytes in a row is treated as
# gone (unplugged or failed). Reading on would take one timeout per sector.
LOST_SOURCE_BYTES = 4 * 1024 * 1024


class SourceLost(OSError):
    """The source stopped returning data."""

    def __init__(self, offset):
        super().__init__("the source stopped responding at byte offset %d" % offset)
        self.offset = offset


def _read_by_sector(fh, offset, length, sector, bad):
    """Read a range one sector at a time. Unreadable sectors become zeros."""
    out = bytearray()
    pos = offset
    end = offset + length
    while pos < end:
        want = min(sector, end - pos)
        data = b""
        try:
            fh.seek(pos)
            data = fh.read(want)
        except OSError:
            data = b""
        if len(data) != want:
            data = b"\x00" * want
            if bad and bad[-1][0] + bad[-1][1] == pos:
                bad[-1][1] += want
            else:
                bad.append([pos, want])
        out += data
        pos += want
    return bytes(out)


def _read_source(fh, size, sector, bad, report):
    """Yield the source in chunks. Raises SourceLost when it goes silent."""
    offset = 0
    try:
        fh.seek(0)
    except OSError:
        raise SourceLost(0)
    while offset < size:
        want = min(CHUNK, size - offset)
        try:
            data = fh.read(want)
        except OSError:
            data = b""
        if len(data) != want:
            data = _read_by_sector(fh, offset, want, sector, bad)
            if bad and bad[-1][0] + bad[-1][1] == offset + want \
                    and bad[-1][1] >= min(LOST_SOURCE_BYTES, size):
                # Earlier chunks of this run are already in the image as
                # zeros. Keep that part listed and drop the chunk not written.
                lost_from = bad[-1][0]
                bad[-1][1] = offset - lost_from
                if bad[-1][1] <= 0:
                    bad.pop()
                raise SourceLost(lost_from)
            try:
                fh.seek(offset + want)
            except OSError:
                raise SourceLost(offset + want)
        yield data
        offset += want
        if report:
            report(offset)
    if report:
        report(offset, final=True)


def _safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "kindle"


def acquire_image(source, out_dir, case=None, device=None, sector=DEFAULT_SECTOR,
                  verify_source=False, name=None, quiet=False, opener=None):
    """Image `source` (raw device path or file) into `out_dir`.

    Returns the acquisition log (also written to acquisition_log.json).
    log["status"] is "ok", "review" (unreadable sectors or a hash that does
    not match) or "incomplete" (the source stopped responding).
    `opener` replaces open() in tests.
    """
    source = str(source)
    refuse_output_on_device(out_dir, device)
    # Open the source first: a refused open must not leave an output folder.
    try:
        fh = (opener or (lambda p: open(p, "rb", buffering=0)))(source)
    except PermissionError as exc:
        raise PermissionError(
            "cannot open %s: %s. Raw devices need an elevated prompt." % (source, exc))
    try:
        out = prepare_output(out_dir)
    except Exception:
        fh.close()
        raise
    image_name = _safe_name(name or "kindle") + ".dd"
    image_path = out / image_name

    log = {"kind": "image", "started_utc": utc_now()}
    log.update(host_facts())
    log["case"] = case or {}
    log["device"] = device
    log["source"] = source
    log["sector_size"] = sector
    log["image_file"] = image_name
    log["image_format"] = "raw (dd), single file"
    lost = None

    with fh:
        size = source_size(fh, source)
        if size <= 0:
            raise ValueError("source reports a size of %d bytes" % size)
        free = shutil.disk_usage(str(out)).free
        if free < size:
            raise OSError("destination has %d bytes free, image needs %d" % (free, size))
        log["source_size_bytes"] = size

        hashers = {n: hashlib.new(n) for n in ("md5", "sha1", "sha256")}
        bad = []
        written = 0
        report = None if quiet else progress_printer("image", size)
        with open(image_path, "wb") as img:
            try:
                for data in _read_source(fh, size, sector, bad, report):
                    for h in hashers.values():
                        h.update(data)
                    img.write(data)
                    written += len(data)
            except SourceLost as exc:
                lost = exc
            img.flush()
            os.fsync(img.fileno())
        log["read_finished_utc"] = utc_now()
        log["image_size_bytes"] = written
        log["source_hashes"] = {n: h.hexdigest() for n, h in hashers.items()}
        log["unreadable_ranges"] = [{"offset": o, "length": n} for o, n in bad]
        log["unreadable_bytes"] = sum(n for _, n in bad)
        if lost is not None:
            log["stopped_at_offset"] = lost.offset
            log["stopped_reason"] = str(lost)

        image_hash = hash_file(image_path)["sha256"]
        log["image_sha256"] = image_hash
        log["image_matches_source_read"] = image_hash == log["source_hashes"]["sha256"]

        if verify_source and lost is None:
            second = hashlib.sha256()
            second_bad = []
            report = None if quiet else progress_printer("re-read source", size)
            try:
                for data in _read_source(fh, size, sector, second_bad, report):
                    second.update(data)
                log["source_second_read_sha256"] = second.hexdigest()
                log["source_second_read_matches"] = (
                    second.hexdigest() == log["source_hashes"]["sha256"])
            except SourceLost as exc:
                log["source_second_read_matches"] = False
                log["second_read_stopped_at_offset"] = exc.offset
            log["second_read_unreadable_ranges"] = [
                {"offset": o, "length": n} for o, n in second_bad]
        elif verify_source:
            log["source_second_read_matches"] = False

    log["finished_utc"] = utc_now()
    ok = log["image_matches_source_read"] and not bad
    if verify_source:
        ok = ok and log["source_second_read_matches"]
    log["status"] = "incomplete" if lost is not None else ("ok" if ok else "review")
    write_json(out / "acquisition_log.json", log)
    return log
