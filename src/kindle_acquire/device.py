"""Find connected Kindle devices on a Windows host.

Read-only. Queries Plug and Play and the storage stack through PowerShell.
A Kindle shows up in one of two ways:

  mass-storage  the user storage is a FAT32 disk (older models)
  mtp           the device is a Windows Portable Device with no disk
                (Scribe and the 2024 models)
"""

import base64
import ctypes
import json
import os
import subprocess

AMAZON_USB_VENDOR_ID = "1949"

_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$usb = @(Get-CimInstance Win32_PnPEntity |
    Where-Object { $_.PNPDeviceID -match '^USB\\VID_1949&PID_[0-9A-F]{4}\\' } |
    ForEach-Object {
        [ordered]@{
            instance_id = $_.PNPDeviceID
            name        = $_.Name
            pnp_class   = $_.PNPClass
            status      = $_.Status
        }
    })
$disks = @(Get-Disk | Where-Object { $_.BusType -eq 'USB' } | ForEach-Object {
    $d = $_
    $parts = @(Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue | ForEach-Object {
        $letter = $null
        if ($_.DriveLetter -and [int][char]$_.DriveLetter -ne 0) { $letter = [string]$_.DriveLetter }
        [ordered]@{
            number       = $_.PartitionNumber
            drive_letter = $letter
            offset       = [int64]$_.Offset
            size         = [int64]$_.Size
        }
    })
    [ordered]@{
        number               = $d.Number
        friendly_name        = $d.FriendlyName
        serial               = ([string]$d.SerialNumber).Trim()
        size                 = [int64]$d.Size
        logical_sector_size  = $d.LogicalSectorSize
        physical_sector_size = $d.PhysicalSectorSize
        is_read_only         = [bool]$d.IsReadOnly
        partitions           = $parts
    }
})
$wp = $null
try {
    $wp = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\StorageDevicePolicies' -ErrorAction Stop).WriteProtect
} catch {}
[ordered]@{ usb = $usb; disks = $disks; usb_write_protect_policy = $wp } | ConvertTo-Json -Depth 6 -Compress
"""


def _run_powershell(script):
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    # Full path: a bare name would also be searched for in the current folder.
    powershell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                              "WindowsPowerShell", "v1.0", "powershell.exe")
    proc = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
         "Bypass", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError("device query failed: %s" % proc.stderr.strip()[:2000])
    return proc.stdout


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def volume_info(root):
    """Label, serial number and file system of a mounted volume (e.g. 'D:\\')."""
    info = {"root": root, "label": "", "serial": "", "filesystem": ""}
    if os.name != "nt":
        return info
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    name = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint32(0)
    max_len = ctypes.c_uint32(0)
    flags = ctypes.c_uint32(0)
    ok = kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root), name, 261, ctypes.byref(serial),
        ctypes.byref(max_len), ctypes.byref(flags), fs, 261)
    if ok:
        info["label"] = name.value
        info["serial"] = "%04X-%04X" % (serial.value >> 16, serial.value & 0xFFFF)
        info["filesystem"] = fs.value
    else:
        info["error"] = ctypes.FormatError(ctypes.get_last_error())
    return info


def detect():
    """Return {'devices': [...], 'usb_write_protect_policy': ...}.

    Each device: usb_serial, usb_product_id, mode, names, and for
    mass-storage devices the disk number, size, partitions and volumes.
    """
    if os.name != "nt":
        raise RuntimeError("device detection is implemented for Windows only")
    raw = json.loads(_run_powershell(_PS_SCRIPT))
    disks = _as_list(raw.get("disks"))
    devices = []
    for entry in _as_list(raw.get("usb")):
        instance = entry["instance_id"]
        head, _, serial = instance.rpartition("\\")
        product_id = ""
        marker = "PID_"
        if marker in head:
            product_id = head.split(marker, 1)[1][:4]
        # Interface nodes of a composite device repeat the parent; skip them.
        if "&MI_" in head:
            continue
        device = {
            "usb_vendor_id": AMAZON_USB_VENDOR_ID,
            "usb_product_id": product_id,
            "usb_serial": serial,
            "usb_instance_id": instance,
            "usb_name": entry.get("name") or "",
            "usb_class": entry.get("pnp_class") or "",
            "mode": "unknown",
            "disk": None,
            "volumes": [],
        }
        for disk in disks:
            if disk.get("serial") and disk["serial"].upper() == serial.upper():
                disk["partitions"] = _as_list(disk.get("partitions"))
                device["disk"] = disk
                device["mode"] = "mass-storage"
                for part in disk["partitions"]:
                    letter = part.get("drive_letter")
                    if letter:
                        device["volumes"].append(volume_info(letter + ":\\"))
                break
        if device["disk"] is None and device["usb_class"].upper() == "WPD":
            device["mode"] = "mtp"
        devices.append(device)
    return {
        "devices": devices,
        "usb_write_protect_policy": raw.get("usb_write_protect_policy"),
    }


def describe(result):
    """Plain-text summary of detect() output."""
    lines = []
    if not result["devices"]:
        lines.append("No Kindle found (no USB device with vendor ID 1949).")
    for dev in result["devices"]:
        lines.append("Kindle  serial %s  USB %s:%s  mode %s" % (
            dev["usb_serial"], dev["usb_vendor_id"], dev["usb_product_id"], dev["mode"]))
        disk = dev["disk"]
        if disk:
            lines.append("  disk %s  %s  %d bytes  sector %s  read-only %s" % (
                disk["number"], disk["friendly_name"], disk["size"],
                disk["logical_sector_size"], disk["is_read_only"]))
            lines.append("  raw device path \\\\.\\PhysicalDrive%s" % disk["number"])
        for vol in dev["volumes"]:
            lines.append("  volume %s  label %r  %s  serial %s" % (
                vol["root"], vol["label"], vol["filesystem"], vol["serial"]))
        if dev["mode"] == "mtp":
            lines.append("  MTP device: no disk to image. MTP copy is not implemented yet.")
    policy = result.get("usb_write_protect_policy")
    lines.append("Host USB write-protect policy: %s" % (
        "on" if policy == 1 else "off (volumes mount read-write without a write blocker)"))
    return "\n".join(lines)
