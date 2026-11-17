"""Ask a Kindle in mass-storage mode which SCSI commands it answers.

    python tools/scsi_probe.py --drive 1 --out <folder> [--hash] [--no-vendor]

FOR A TEST DEVICE ONLY. This sends commands to the device. Do not run it
on evidence. It needs an elevated prompt on Windows.

A Kindle in this mode is one USB interface of class 08 (mass storage,
SCSI, bulk-only). SCSI commands are the only channel it offers. This tool
records what the device does with each one:

1. Standard queries that only read: INQUIRY and its vital product data
   pages, READ CAPACITY, MODE SENSE, REPORT LUNS and others.
2. A sweep of operation codes 0x00 to 0xFF. Each is sent once with every
   other byte zero and a read-direction buffer. The answer says whether
   the device knows the code. Codes that write, erase, format, reserve,
   stop the unit or load firmware are on a skip list and are never sent.
   Codes 0xC0 to 0xFF are vendor-specific: their meaning is unknown, which
   is the reason to ask. --no-vendor leaves them out.

With --hash the whole device is read and hashed before and after, to show
whether its content changed.
"""

import argparse
import ctypes
import hashlib
import json
import os
import sys
import time
from ctypes import wintypes

IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x4D014
SCSI_IOCTL_DATA_IN = 1
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 1, 2
OPEN_EXISTING = 3
INVALID_HANDLE = ctypes.c_void_p(-1).value

# Never sent. Each changes the device or its medium, or can with zero arguments.
SKIP = {
    0x04: "FORMAT UNIT", 0x07: "REASSIGN BLOCKS", 0x08: "READ(6), asked in the queries",
    0x0A: "WRITE(6)", 0x15: "MODE SELECT(6)", 0x16: "RESERVE(6)", 0x17: "RELEASE(6)",
    0x18: "COPY", 0x19: "ERASE(6)", 0x1B: "START STOP UNIT", 0x1D: "SEND DIAGNOSTIC",
    0x2A: "WRITE(10)", 0x2C: "ERASE(10)", 0x2E: "WRITE AND VERIFY(10)", 0x31: "SEARCH DATA",
    0x33: "SET LIMITS", 0x38: "MEDIUM SCAN", 0x39: "COMPARE", 0x3A: "COPY AND VERIFY",
    0x3B: "WRITE BUFFER", 0x3F: "WRITE LONG(10)", 0x40: "CHANGE DEFINITION",
    0x41: "WRITE SAME(10)", 0x42: "UNMAP", 0x48: "SANITIZE", 0x4C: "LOG SELECT",
    0x50: "XDWRITE", 0x51: "XPWRITE", 0x53: "XDWRITEREAD", 0x55: "MODE SELECT(10)",
    0x56: "RESERVE(10)", 0x57: "RELEASE(10)", 0x5F: "PERSISTENT RESERVE OUT",
    0x7F: "variable-length command", 0x83: "EXTENDED COPY", 0x85: "ATA PASS-THROUGH(16)",
    0x89: "COMPARE AND WRITE", 0x8A: "WRITE(16)", 0x8E: "WRITE AND VERIFY(16)",
    0x93: "WRITE SAME(16)", 0x94: "ZBC OUT", 0x9F: "SERVICE ACTION OUT(16)",
    0xA1: "ATA PASS-THROUGH(12)", 0xA4: "MAINTENANCE OUT", 0xA5: "MOVE MEDIUM",
    0xA9: "SERVICE ACTION OUT(12)", 0xAA: "WRITE(12)", 0xAC: "ERASE(12)",
    0xAE: "WRITE AND VERIFY(12)", 0xB5: "SECURITY PROTOCOL OUT", 0xB6: "SEND VOLUME TAG",
    0xBF: "SEND DVD STRUCTURE",
}
SENSE_KEYS = ("NO SENSE", "RECOVERED ERROR", "NOT READY", "MEDIUM ERROR", "HARDWARE ERROR",
              "ILLEGAL REQUEST", "UNIT ATTENTION", "DATA PROTECT", "BLANK CHECK",
              "VENDOR SPECIFIC", "COPY ABORTED", "ABORTED COMMAND", "EQUAL",
              "VOLUME OVERFLOW", "MISCOMPARE", "COMPLETED")


class SPTD(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("ScsiStatus", ctypes.c_ubyte),
                ("PathId", ctypes.c_ubyte), ("TargetId", ctypes.c_ubyte),
                ("Lun", ctypes.c_ubyte), ("CdbLength", ctypes.c_ubyte),
                ("SenseInfoLength", ctypes.c_ubyte), ("DataIn", ctypes.c_ubyte),
                ("DataTransferLength", wintypes.ULONG), ("TimeOutValue", wintypes.ULONG),
                ("DataBuffer", ctypes.c_void_p), ("SenseInfoOffset", wintypes.ULONG),
                ("Cdb", ctypes.c_ubyte * 16)]


class SPTDWithSense(ctypes.Structure):
    _fields_ = [("sptd", SPTD), ("filler", wintypes.ULONG), ("sense", ctypes.c_ubyte * 32)]


class Device:
    def __init__(self, drive):
        self.path = "\\\\.\\PhysicalDrive%d" % drive
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p]
        k32.DeviceIoControl.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        self.k32 = k32
        self.handle = k32.CreateFileW(self.path, GENERIC_READ | GENERIC_WRITE,
                                      FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                      OPEN_EXISTING, 0, None)
        if self.handle in (None, INVALID_HANDLE):
            raise OSError("cannot open %s: Windows error %d (run elevated)" % (
                self.path, ctypes.get_last_error()))

    def close(self):
        self.k32.CloseHandle(self.handle)

    def send(self, cdb, length=512, timeout=10):
        """Send one command with a read-direction buffer. Returns a result dict."""
        buffer = ctypes.create_string_buffer(max(length, 1))
        block = SPTDWithSense()
        block.sptd.Length = ctypes.sizeof(SPTD)
        block.sptd.CdbLength = len(cdb)
        block.sptd.SenseInfoLength = 32
        block.sptd.DataIn = SCSI_IOCTL_DATA_IN
        block.sptd.DataTransferLength = length
        block.sptd.TimeOutValue = timeout
        block.sptd.DataBuffer = ctypes.cast(buffer, ctypes.c_void_p)
        block.sptd.SenseInfoOffset = SPTDWithSense.sense.offset
        for index, byte in enumerate(cdb):
            block.sptd.Cdb[index] = byte
        returned = wintypes.DWORD(0)
        started = time.time()
        ok = self.k32.DeviceIoControl(self.handle, IOCTL_SCSI_PASS_THROUGH_DIRECT,
                                      ctypes.byref(block), ctypes.sizeof(block),
                                      ctypes.byref(block), ctypes.sizeof(block),
                                      ctypes.byref(returned), None)
        result = {"cdb": bytes(cdb).hex(), "seconds": round(time.time() - started, 3)}
        if not ok:
            result["windows_error"] = ctypes.get_last_error()
            result["answer"] = "Windows did not deliver the command (error %d)" % (
                result["windows_error"])
            return result
        got = block.sptd.DataTransferLength
        sense = bytes(block.sense[:block.sptd.SenseInfoLength])
        result.update(status=block.sptd.ScsiStatus, data_bytes=got,
                      data=buffer.raw[:got].hex() if got else "")
        if block.sptd.ScsiStatus == 0:
            result["answer"] = "accepted"
        elif block.sptd.ScsiStatus == 2 and len(sense) >= 14:
            key, asc, ascq = sense[2] & 0x0F, sense[12], sense[13]
            result.update(sense_key=SENSE_KEYS[key], asc="%02x" % asc, ascq="%02x" % ascq,
                          sense=sense.hex())
            if key == 5 and asc == 0x20:
                result["answer"] = "not supported (invalid operation code)"
            elif key == 5 and asc in (0x24, 0x26, 0x1A):
                result["answer"] = "recognised, rejected the zero arguments"
            elif key == 5 and asc == 0x21:
                result["answer"] = "recognised, block address out of range"
            else:
                result["answer"] = "check condition: %s %02x/%02x" % (SENSE_KEYS[key], asc, ascq)
        else:
            result["answer"] = "status 0x%02x" % block.sptd.ScsiStatus
        return result

    def alive(self):
        for _ in range(3):
            if self.send([0, 0, 0, 0, 0, 0], 0).get("status") == 0:
                return True
            time.sleep(1)
        return False


def cdb_length(opcode):
    group = opcode >> 5
    return {0: 6, 1: 10, 2: 10, 3: 10, 4: 16, 5: 12}.get(group, 10)


def queries(device):
    """Standard read-only queries. Returns [(name, result)]."""
    out = []

    def ask(name, cdb, length):
        out.append((name, device.send(cdb, length)))
        return out[-1][1]

    ask("TEST UNIT READY", [0x00, 0, 0, 0, 0, 0], 0)
    ask("INQUIRY", [0x12, 0, 0, 0, 96, 0], 96)
    pages = ask("INQUIRY, list of vital product data pages", [0x12, 1, 0x00, 0, 255, 0], 255)
    listed = [0x80, 0x83]
    if pages.get("status") == 0 and pages.get("data"):
        raw = bytes.fromhex(pages["data"])
        listed = sorted(set(listed) | set(raw[4:4 + raw[3]]) - {0})
    for page in listed:
        ask("INQUIRY, vital product data page 0x%02x" % page, [0x12, 1, page, 0, 255, 0], 255)
    ask("REQUEST SENSE", [0x03, 0, 0, 0, 18, 0], 18)
    ask("READ CAPACITY(10)", [0x25] + [0] * 9, 8)
    ask("READ CAPACITY(16)", [0x9E, 0x10] + [0] * 11 + [32, 0, 0], 32)
    ask("READ FORMAT CAPACITIES", [0x23, 0, 0, 0, 0, 0, 0, 0, 252, 0], 252)
    ask("MODE SENSE(6), all pages", [0x1A, 0, 0x3F, 0, 255, 0], 255)
    ask("MODE SENSE(10), all pages", [0x5A, 0, 0x3F, 0, 0, 0, 0, 2, 0, 0], 512)
    ask("REPORT LUNS", [0xA0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 0, 0], 512)
    ask("REPORT SUPPORTED OPERATION CODES", [0xA3, 0x0C, 0, 0, 0, 0, 0, 0, 16, 0, 0, 0], 4096)
    ask("LOG SENSE, list of pages", [0x4D, 0, 0x40, 0, 0, 0, 0, 2, 0, 0], 512)
    ask("READ BUFFER, descriptor", [0x3C, 3, 0, 0, 0, 0, 0, 0, 4, 0], 4)
    ask("RECEIVE DIAGNOSTIC RESULTS, list of pages", [0x1C, 1, 0, 2, 0, 0], 512)
    ask("GET CONFIGURATION", [0x46, 0, 0, 0, 0, 0, 0, 2, 0, 0], 512)
    ask("READ TOC", [0x43, 0, 0, 0, 0, 0, 0, 2, 0, 0], 512)
    ask("READ(6), one block at 0", [0x08, 0, 0, 0, 1, 0], 512)
    ask("READ(10), one block at 0", [0x28, 0, 0, 0, 0, 0, 0, 0, 1, 0], 512)
    return out


def sweep(device, vendor=True, say=print):
    """One zero-argument command per operation code. Returns [result]."""
    results = []
    for opcode in range(256):
        entry = {"opcode": "%02x" % opcode}
        if opcode in SKIP:
            entry["answer"] = "not sent: %s" % SKIP[opcode]
        elif opcode >= 0xC0 and not vendor:
            entry["answer"] = "not sent: vendor-specific code, left out by --no-vendor"
        else:
            entry.update(device.send([opcode] + [0] * (cdb_length(opcode) - 1), 512))
            if not entry.get("data_bytes"):
                entry.pop("data", None)
            if not device.alive():
                entry["device_stopped_answering"] = True
                results.append(entry)
                say("the device stopped answering after operation code 0x%02x" % opcode)
                break
        results.append(entry)
    return results


def hash_device(path, size, say=print):
    digest = hashlib.sha256()
    done = 0
    with open(path, "rb", buffering=0) as fh:
        while done < size:
            chunk = fh.read(min(4 * 1024 * 1024, size - done))
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            if done % (512 * 1024 * 1024) < len(chunk):
                say("  hashed %d of %d MiB" % (done >> 20, size >> 20))
    return {"sha256": digest.hexdigest(), "bytes": done}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--drive", type=int, required=True, help="physical drive number")
    parser.add_argument("--out", required=True, help="folder for scsi_probe.json")
    parser.add_argument("--hash", action="store_true",
                        help="hash the whole device before and after")
    parser.add_argument("--no-vendor", action="store_true",
                        help="do not send operation codes 0xC0 to 0xFF")
    args = parser.parse_args(argv)
    if os.name != "nt":
        raise SystemExit("this tool uses the Windows SCSI pass-through interface")
    os.makedirs(args.out, exist_ok=True)
    log = {"tool": "KEAT scsi_probe", "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "drive": args.drive, "vendor_codes_sent": not args.no_vendor}
    target = os.path.join(args.out, "scsi_probe.json")

    def save():
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(log, fh, indent=1)

    device = Device(args.drive)
    try:
        log["queries"] = [dict(result, name=name) for name, result in queries(device)]
        capacity = next((q for q in log["queries"] if q["name"] == "READ CAPACITY(10)"), {})
        size = 0
        if capacity.get("status") == 0 and len(capacity.get("data", "")) == 16:
            raw = bytes.fromhex(capacity["data"])
            size = (int.from_bytes(raw[:4], "big") + 1) * int.from_bytes(raw[4:], "big")
        log["device_bytes"] = size
        save()
        if args.hash and size:
            print("hashing the device before the sweep")
            log["hash_before"] = hash_device(device.path, size)
            save()
        print("sweeping operation codes")
        log["sweep"] = sweep(device, vendor=not args.no_vendor)
        save()
        if args.hash and size:
            print("hashing the device after the sweep")
            log["hash_after"] = hash_device(device.path, size)
            log["content_unchanged"] = log["hash_after"] == log["hash_before"]
    finally:
        device.close()
        log["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        save()
    counts = {}
    for entry in log.get("sweep", []):
        answer = entry["answer"].split(":")[0] if entry["answer"].startswith("not sent") \
            else entry["answer"]
        counts[answer] = counts.get(answer, 0) + 1
    for answer, count in sorted(counts.items(), key=lambda item: -item[1]):
        print("%4d  %s" % (count, answer))
    if "content_unchanged" in log:
        print("device content unchanged: %s" % log["content_unchanged"])
    print("written to %s" % target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
