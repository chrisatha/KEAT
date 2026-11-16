"""Read-only FAT32 reader for raw images and raw devices.

Reads the boot sector, the file allocation table and directory clusters
directly. Nothing is mounted and nothing is written. Directory times are
returned exactly as stored: wall-clock values with no time zone.

Deleted entries are listed. A deleted file's data can be read back when
the clusters it would occupy as one unbroken run are all unallocated. That
is the standard assumption for FAT recovery and it is an assumption: the
content is not confirmed to be the original file.
"""

import struct
import sys
from array import array

SECTOR = 512
_MASK = 0x0FFFFFFF
_EOC = 0x0FFFFFF8
_BAD = 0x0FFFFFF7
_READ_MAX = 1024 * 1024
_FAT_PARTITION_TYPES = (0x0B, 0x0C)
# FAT32 entries are 28 bits wide and the top values are reserved.
MAX_CLUSTERS = 0x0FFFFFF5
# A FAT directory holds at most 65,536 entries of 32 bytes.
MAX_DIRECTORY_BYTES = 65536 * 32


class Fat32Error(ValueError):
    pass


def _checksum(short):
    total = 0
    for byte in short:
        total = (((total & 1) << 7) + (total >> 1) + byte) & 0xFF
    return total


def _date(value):
    day, month, year = value & 0x1F, (value >> 5) & 0x0F, 1980 + (value >> 9)
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return ""
    return "%04d-%02d-%02d" % (year, month, day)


def _datetime(date_value, time_value, tenths=0):
    date = _date(date_value)
    if not date:
        return ""
    hour, minute = time_value >> 11, (time_value >> 5) & 0x3F
    second = (time_value & 0x1F) * 2 + (tenths // 100 if tenths < 200 else 0)
    if hour > 23 or minute > 59 or second > 59:
        return ""
    return "%sT%02d:%02d:%02d" % (date, hour, minute, second)


def _short_name(entry, deleted):
    base = bytearray(entry[0:8])
    ext = entry[8:11]
    if deleted:
        base[0] = ord("_")
    elif base[0] == 0x05:
        base[0] = 0xE5
    base_text = bytes(base).decode("cp437", errors="replace").rstrip(" ")
    ext_text = ext.decode("cp437", errors="replace").rstrip(" ")
    case = entry[12]
    if case & 0x08:
        base_text = base_text.lower()
    if case & 0x10:
        ext_text = ext_text.lower()
    return base_text + ("." + ext_text if ext_text else "")


def _first_alias_byte(name):
    """First byte a FAT driver gives the 8.3 alias of a long name."""
    for char in name:
        if char in " .":
            continue
        char = char.upper()
        try:
            raw = char.encode("cp437")
        except UnicodeEncodeError:
            return ord("_")
        if len(raw) != 1 or char in "+,;=[]":
            return ord("_")
        return raw[0]
    return None


def _lost_first_byte(short, target):
    """The first short-name byte that gives the long-name checksum.

    Deleting an entry overwrites that byte. The checksum stored in the
    long-name parts fixes it: exactly one value fits.
    """
    for value in range(256):
        if _checksum(bytes([value]) + short[1:]) == target:
            return value
    return None


def _join(parts):
    text = b"".join(p[2] for p in parts).decode("utf-16-le", errors="replace")
    end = text.find("\x00")
    return text if end < 0 else text[:end]


def _long_name(parts, short, deleted):
    """Join long-name entries that sit directly before a short entry.

    parts: [(first_byte, checksum, 26 name bytes, is_deleted)] in disk order.
    Returns (name, name source). The name is "" when there is none.
    """
    if deleted:
        # The sequence numbers are overwritten on deletion. Take the run of
        # deleted parts directly before the entry that share one checksum.
        run = []
        for part in reversed(parts):
            if not part[3] or (run and part[1] != run[0][1]):
                break
            run.append(part)
        name = _join(run) if run else ""
        if not name:
            return "", ""
        # The part furthest from the entry holds the end of the name. With
        # no terminator there, earlier parts may have been overwritten.
        top = run[-1][2]
        if not any(top[i:i + 2] == b"\x00\x00" for i in range(0, 26, 2)):
            return name, "long, may be incomplete"
        if _lost_first_byte(short, run[0][1]) != _first_alias_byte(name):
            return name, "long, not confirmed by checksum"
        return name, "long"
    # A live set starts at the part flagged as last (0x40). Parts before it
    # are leftovers from other entries.
    start = None
    for index, part in enumerate(parts):
        if not part[3] and part[0] & 0x40:
            start = index
    if start is None:
        return "", ""
    parts = parts[start:]
    expected = _checksum(short)
    count = parts[0][0] & 0x3F
    if count != len(parts):
        return "", ""
    for index, part in enumerate(parts):
        if part[3] or part[1] != expected or (part[0] & 0x3F) != count - index:
            return "", ""
    return _join(list(reversed(parts))), "long"


def _looks_like_directory(data):
    """True when every slot up to the end mark is a plausible entry."""
    for pos in range(0, len(data) - 31, 32):
        slot = data[pos:pos + 32]
        if slot[0] == 0x00:
            return True
        attr = slot[11]
        if attr & 0x3F == 0x0F:
            if slot[12] != 0 or slot[26:28] != b"\x00\x00":
                return False
            continue
        if attr & 0xC0 or any(byte < 0x20 for byte in slot[1:11]):
            return False
        if not _date(struct.unpack_from("<H", slot, 24)[0]):
            return False
    return True


class Fat32Volume:
    def __init__(self, fh, offset=0):
        self.fh = fh
        self.offset = offset
        boot = self._read(offset, SECTOR)
        if boot[510:512] != b"\x55\xaa":
            raise Fat32Error("no boot signature at offset %d" % offset)
        (self.bytes_per_sector, self.sectors_per_cluster, self.reserved_sectors,
         self.fat_count, root_entries, total16) = struct.unpack_from("<HBHBHH", boot, 11)
        fat16_size = struct.unpack_from("<H", boot, 22)[0]
        total32, fat32_size = struct.unpack_from("<II", boot, 32)
        self.root_cluster = struct.unpack_from("<I", boot, 44)[0]
        if (self.bytes_per_sector not in (512, 1024, 2048, 4096)
                or self.sectors_per_cluster == 0
                or self.sectors_per_cluster & (self.sectors_per_cluster - 1)
                or self.fat_count == 0):
            raise Fat32Error("not a FAT boot sector at offset %d" % offset)
        if root_entries != 0 or fat16_size != 0 or fat32_size == 0:
            raise Fat32Error("the volume is FAT12 or FAT16, not FAT32")
        self.total_sectors = total32 or total16
        self.fat_size_sectors = fat32_size
        self.cluster_size = self.bytes_per_sector * self.sectors_per_cluster
        self.first_data_sector = self.reserved_sectors + self.fat_count * fat32_size
        if self.first_data_sector >= self.total_sectors:
            raise Fat32Error("boot sector describes a volume with no data area")
        self.cluster_count = (self.total_sectors - self.first_data_sector) // self.sectors_per_cluster
        if self.cluster_count > MAX_CLUSTERS:
            raise Fat32Error("boot sector claims %d clusters, more than FAT32 allows"
                             % self.cluster_count)
        serial = struct.unpack_from("<I", boot, 67)[0]
        self.serial = "%04X-%04X" % (serial >> 16, serial & 0xFFFF)
        self.boot_label = boot[71:82].decode("cp437", errors="replace").rstrip(" ")
        self.label = ""
        # Read only the part of the table the volume needs, in whole sectors.
        # A damaged boot sector cannot make this larger than the FAT32 limit.
        needed = (self.cluster_count + 2) * 4
        needed = -(-needed // self.bytes_per_sector) * self.bytes_per_sector
        if needed > fat32_size * self.bytes_per_sector:
            raise Fat32Error("the allocation table is shorter than the volume")
        fat_bytes = self._read(offset + self.reserved_sectors * self.bytes_per_sector, needed)
        self.fat = array("I")
        self.fat.frombytes(fat_bytes[:len(fat_bytes) // 4 * 4])
        if sys.byteorder == "big":
            self.fat.byteswap()
        if len(self.fat) < self.cluster_count + 2:
            raise Fat32Error("the allocation table is shorter than the volume")

    @classmethod
    def open(cls, fh):
        """Find the FAT32 volume: at offset 0, or in an MBR partition."""
        try:
            return cls(fh, 0)
        except Fat32Error as first_error:
            fh.seek(0)
            sector = fh.read(SECTOR)
            if len(sector) == SECTOR and sector[510:512] == b"\x55\xaa":
                for index in range(4):
                    entry = sector[446 + 16 * index:462 + 16 * index]
                    start = struct.unpack_from("<I", entry, 8)[0]
                    if entry[4] in _FAT_PARTITION_TYPES and start:
                        return cls(fh, start * SECTOR)
            raise first_error

    # --- low level ---------------------------------------------------------
    def _read(self, position, length):
        self.fh.seek(position)
        data = self.fh.read(length)
        if len(data) != length:
            raise Fat32Error("short read at offset %d: wanted %d bytes, got %d"
                             % (position, length, len(data)))
        return data

    def is_valid(self, cluster):
        return 2 <= cluster < self.cluster_count + 2

    def is_free(self, cluster):
        return self.is_valid(cluster) and (self.fat[cluster] & _MASK) == 0

    def free_clusters(self):
        return sum(1 for c in range(2, self.cluster_count + 2)
                   if (self.fat[c] & _MASK) == 0)

    def cluster_offset(self, cluster):
        sector = self.first_data_sector + (cluster - 2) * self.sectors_per_cluster
        return self.offset + sector * self.bytes_per_sector

    def chain(self, first):
        """Follow an allocation chain. Returns (clusters, problem text)."""
        clusters, seen, cluster = [], set(), first
        while True:
            if not self.is_valid(cluster):
                return clusters, "chain points outside the volume (cluster %d)" % cluster
            if cluster in seen:
                return clusters, "chain loops at cluster %d" % cluster
            seen.add(cluster)
            clusters.append(cluster)
            nxt = self.fat[cluster] & _MASK
            if nxt >= _EOC:
                return clusters, ""
            if nxt == _BAD:
                return clusters, "chain reaches a bad-cluster mark after cluster %d" % cluster
            if nxt == 0:
                return clusters, "chain runs into an unallocated cluster after cluster %d" % cluster
            cluster = nxt

    def read_clusters(self, clusters, size=None):
        """Yield the data of a cluster list, trimmed to `size` bytes."""
        remaining = len(clusters) * self.cluster_size if size is None else size
        index = 0
        while index < len(clusters) and remaining > 0:
            run = 1
            while (index + run < len(clusters)
                   and clusters[index + run] == clusters[index] + run
                   and (run + 1) * self.cluster_size <= _READ_MAX):
                run += 1
            data = self._read(self.cluster_offset(clusters[index]), run * self.cluster_size)
            if len(data) > remaining:
                data = data[:remaining]
            remaining -= len(data)
            index += run
            yield data

    # --- directories -------------------------------------------------------
    def _parse_directory(self, clusters, all_deleted=False):
        raw = b"".join(self.read_clusters(clusters))
        entries, parts = [], []
        for pos in range(0, len(raw) - 31, 32):
            entry = raw[pos:pos + 32]
            first = entry[0]
            if first == 0x00:
                break
            attr = entry[11]
            deleted = first == 0xE5
            if attr & 0x3F == 0x0F:
                parts.append((first, entry[13], entry[1:11] + entry[14:26] + entry[28:32], deleted))
                continue
            if attr & 0x08:
                if not deleted and not self.label:
                    self.label = entry[0:11].decode("cp437", errors="replace").rstrip(" ")
                parts = []
                continue
            long_name, long_source = _long_name(parts, entry[0:11], deleted)
            parts = []
            name = long_name or _short_name(entry, deleted)
            if entry[0:11] in (b".          ", b"..         "):
                continue
            (tenths, ctime, cdate, adate, high, mtime, mdate, low,
             size) = struct.unpack_from("<BHHHHHHHI", entry, 13)
            entries.append({
                "name": name,
                "name_source": long_source if long_name else (
                    "short, first character lost" if deleted else "short"),
                "is_dir": bool(attr & 0x10),
                "deleted": deleted or all_deleted,
                "attr": attr,
                "first_cluster": (high << 16) | low,
                "size": size,
                "modified_stored": _datetime(mdate, mtime),
                "created_stored": _datetime(cdate, ctime, tenths),
                "accessed_stored": _date(adate),
                "entry_offset": (self.cluster_offset(clusters[pos // self.cluster_size])
                                 + pos % self.cluster_size),
                "problem": "",
            })
        return entries

    def _deleted_directory_clusters(self, first):
        """Clusters of a deleted folder, or [] if the space was reused."""
        if not self.is_valid(first):
            return [], "first cluster %d is outside the volume" % first
        if not self.is_free(first):
            return [], "first cluster is in use"
        # Whole clusters only: a raw device refuses partial-sector reads.
        data = self._read(self.cluster_offset(first), self.cluster_size)
        own = (struct.unpack_from("<H", data, 20)[0] << 16) | struct.unpack_from("<H", data, 26)[0]
        if data[0:11] != b".          " or own != first or not _looks_like_directory(data):
            return [], "first cluster no longer holds a folder"
        clusters = [first]
        while True:
            if any(data[i] == 0x00 for i in range(0, len(data), 32)):
                return clusters, ""
            nxt = clusters[-1] + 1
            if not self.is_free(nxt):
                return clusters, ""
            if len(clusters) * self.cluster_size >= MAX_DIRECTORY_BYTES:
                return clusters, "stopped after %d clusters" % len(clusters)
            data = self._read(self.cluster_offset(nxt), self.cluster_size)
            if not _looks_like_directory(data):
                # The next cluster holds other data: the folder ended here.
                return clusters, ""
            clusters.append(nxt)

    def walk(self, include_deleted=True):
        """Yield (path, entry) for every directory entry, depth-first."""
        root_clusters, root_problem = self.chain(self.root_cluster)
        # Folders that could not be read in full: [(path, reason)].
        self.problems = [(".", root_problem)] if root_problem else []
        stack = [("", root_clusters, False)]
        visited = set(root_clusters[:1])
        while stack:
            parent, clusters, in_deleted = stack.pop()
            if len(clusters) * self.cluster_size > MAX_DIRECTORY_BYTES:
                clusters = clusters[:MAX_DIRECTORY_BYTES // self.cluster_size]
                self.problems.append((parent or ".", "folder is larger than %d bytes, "
                                      "only that much was read" % MAX_DIRECTORY_BYTES))
            try:
                entries = self._parse_directory(clusters, all_deleted=in_deleted)
            except (Fat32Error, OSError) as exc:
                self.problems.append((parent or ".", "folder could not be read: %s" % exc))
                continue
            sub = []
            for entry in entries:
                path = parent + "/" + entry["name"] if parent else entry["name"]
                # A name can itself contain "/", so the parent is given apart.
                entry["parent"] = parent
                if entry["is_dir"]:
                    first = entry["first_cluster"]
                    if first in visited:
                        entry["problem"] = "folder cluster already listed, not entered again"
                    elif not entry["deleted"]:
                        child, entry["problem"] = self.chain(first)
                        if child:
                            visited.add(first)
                            sub.append((path, child, False))
                    elif include_deleted:
                        try:
                            child, entry["problem"] = self._deleted_directory_clusters(first)
                        except (Fat32Error, OSError) as exc:
                            child, entry["problem"] = [], "could not be read: %s" % exc
                        if child:
                            visited.add(first)
                            sub.append((path, child, True))
                if entry["deleted"] and not include_deleted:
                    continue
                yield path, entry
            stack.extend(reversed(sub))

    # --- file data ---------------------------------------------------------
    def file_clusters(self, entry):
        """Clusters holding an allocated file. Returns (clusters, problem)."""
        if entry["size"] == 0:
            return [], ""
        clusters, problem = self.chain(entry["first_cluster"])
        needed = -(-entry["size"] // self.cluster_size)
        if not problem and len(clusters) < needed:
            problem = "chain holds %d bytes, entry says %d" % (
                len(clusters) * self.cluster_size, entry["size"])
        return clusters[:needed], problem

    def deleted_file_clusters(self, entry):
        """Clusters a deleted file would occupy as one unbroken run.

        Returns (clusters, reason). clusters is [] when recovery is refused.
        """
        if entry["size"] == 0:
            return [], "empty file"
        first = entry["first_cluster"]
        needed = -(-entry["size"] // self.cluster_size)
        if not self.is_valid(first):
            return [], "first cluster %d is outside the volume" % first
        for index in range(needed):
            cluster = first + index
            if not self.is_valid(cluster):
                return [], "the run passes the end of the volume"
            if not self.is_free(cluster):
                return [], "cluster %d is in use (byte %d of the file)" % (
                    cluster, index * self.cluster_size)
        return list(range(first, first + needed)), ""
