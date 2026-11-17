"""Rows from every page version in a SQLite database and its write-ahead log.

SQLite appends changed pages to the -wal file. Old versions of a page stay
in that file until it is overwritten, and frames from before the last
checkpoint stay after it. This reader does not ask SQLite for the current
state. It walks every table leaf page in the database file and in every
frame of the log, and returns each cell it can decode.

A row from an earlier frame was in the database at some time. It may have
been changed or deleted since. Nothing is said about which table a row
belongs to: the caller decides from the shape of the values.
"""

import struct

WAL_MAGICS = (0x377F0682, 0x377F0683)
_LEAF_TABLE = 0x0D
MAX_PAGE = 65536
MAX_CELLS = 5000


class WalError(ValueError):
    pass


def _varint(data, pos):
    """SQLite variable-length integer. Returns (value, next position)."""
    value = 0
    for index in range(9):
        if pos >= len(data):
            raise IndexError("varint runs past the page")
        byte = data[pos]
        pos += 1
        if index == 8:
            return (value << 8) | byte, pos
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos
    return value, pos


_INT_SIZES = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8}


def _decode_record(payload, partial=False):
    """A record's column values. Raises IndexError or ValueError on damage.

    With `partial`, the payload is cut short: the columns that fit are
    returned and the first cut column ends the list.
    """
    header_size, pos = _varint(payload, 0)
    if header_size < 1 or header_size > len(payload):
        raise ValueError("record header larger than the payload")
    types = []
    while pos < header_size:
        serial, pos = _varint(payload, pos)
        types.append(serial)
    if pos != header_size:
        raise ValueError("record header does not end on a column")
    values = []
    pos = header_size
    try:
        _read_columns(payload, pos, types, values)
    except IndexError:
        if not partial:
            raise
    return values


def _read_columns(payload, pos, types, values):
    for serial in types:
        if serial == 0:
            values.append(None)
        elif serial in _INT_SIZES:
            size = _INT_SIZES[serial]
            if pos + size > len(payload):
                raise IndexError("integer runs past the payload")
            values.append(int.from_bytes(payload[pos:pos + size], "big", signed=True))
            pos += size
        elif serial == 7:
            if pos + 8 > len(payload):
                raise IndexError("float runs past the payload")
            values.append(struct.unpack(">d", payload[pos:pos + 8])[0])
            pos += 8
        elif serial in (8, 9):
            values.append(serial - 8)
        elif serial >= 12:
            size = (serial - 12) // 2
            if pos + size > len(payload):
                raise IndexError("value runs past the payload")
            chunk = bytes(payload[pos:pos + size])
            values.append(chunk if serial % 2 == 0 else chunk.decode("utf-8", "replace"))
            pos += size
        else:
            raise ValueError("reserved serial type %d" % serial)


def leaf_rows(page, usable, is_first_page=False):
    """Rows of one table leaf page: [(rowid, values, complete)].

    `complete` is False when the row continues on an overflow page. Only the
    part stored on this page is decoded then, and a cut value is dropped.
    """
    base = 100 if is_first_page else 0
    if len(page) < base + 8 or page[base] != _LEAF_TABLE:
        return []
    count = struct.unpack(">H", page[base + 3:base + 5])[0]
    if count > MAX_CELLS:
        return []
    max_local = usable - 35
    min_local = (usable - 12) * 32 // 255 - 23
    rows = []
    for index in range(count):
        at = base + 8 + 2 * index
        if at + 2 > len(page):
            break
        cell = struct.unpack(">H", page[at:at + 2])[0]
        try:
            size, pos = _varint(page, cell)
            rowid, pos = _varint(page, pos)
            if rowid >= 1 << 63:
                rowid -= 1 << 64
            complete = size <= max_local
            local = size
            if not complete:
                local = min_local + (size - min_local) % (usable - 4)
                if local > max_local:
                    local = min_local
            if size < 1 or pos + local > len(page):
                continue
            payload = page[pos:pos + local]
            values = _decode_record(payload, partial=not complete)
        except (IndexError, ValueError):
            continue
        rows.append((rowid, values, complete))
    return rows


def page_versions(db_bytes, wal_bytes):
    """Every version of every page. Yields (source, page number, page, usable).

    source is "database file" or "write-ahead log frame N, generation G".
    Generations count the distinct salt values in the log, newest first in
    the file because SQLite restarts the log from the top.
    """
    page_size = 0
    reserved = 0
    if len(db_bytes) >= 100 and db_bytes[:16] == b"SQLite format 3\x00":
        page_size = struct.unpack(">H", db_bytes[16:18])[0]
        page_size = 65536 if page_size == 1 else page_size
        reserved = db_bytes[20]
    if len(wal_bytes) >= 32:
        magic, _version, wal_page = struct.unpack(">3I", wal_bytes[:12])
        if magic in WAL_MAGICS:
            page_size = page_size or wal_page
            if wal_page != page_size:
                raise WalError("the log and the database disagree on the page size")
        else:
            wal_bytes = b""
    else:
        wal_bytes = b""
    if page_size < 512 or page_size > MAX_PAGE or page_size & (page_size - 1):
        raise WalError("not a SQLite database: page size %d" % page_size)
    usable = page_size - reserved
    if usable < 480:
        raise WalError("reserved space leaves no room for rows")
    for number in range(len(db_bytes) // page_size):
        yield ("database file", number + 1,
               db_bytes[number * page_size:(number + 1) * page_size], usable)
    generations = {}
    frame_size = page_size + 24
    for index in range((len(wal_bytes) - 32) // frame_size):
        start = 32 + index * frame_size
        number, _commit, salt1, salt2 = struct.unpack(">4I", wal_bytes[start:start + 16])
        if number == 0:
            continue
        generation = generations.setdefault((salt1, salt2), len(generations) + 1)
        yield ("write-ahead log frame %d, generation %d" % (index + 1, generation), number,
               wal_bytes[start + 24:start + 24 + page_size], usable)


def all_rows(db_bytes, wal_bytes=b""):
    """Every decodable table row in every page version.

    Returns [(source, page number, rowid, values, complete)], in file order.
    The same row appears once per page version that holds it.
    """
    out = []
    for source, number, page, usable in page_versions(db_bytes, wal_bytes):
        for rowid, values, complete in leaf_rows(page, usable, is_first_page=(number == 1)):
            out.append((source, number, rowid, values, complete))
    return out
