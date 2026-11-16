"""Decoder for Kindle reader sidecar files (.yjr, .yjf, .azw3r, .azw3f,
.pds, .pdt, .mbp1, .mbs and others that share the format).

Layout: an 8-byte signature, then a stream of typed values. A field is
0xFE, a name, any number of values or nested fields, then 0xFF.

  type  0  boolean   1 byte
        1  int32     4 bytes, big-endian
        2  int64     8 bytes
        3  string    1 byte empty-flag, then uint16 length and UTF-8 bytes
        4  double    8 bytes
        5  int16     2 bytes
        6  float     4 bytes
        7  byte      1 byte
        9  char      1 byte

The decoder is generic. It does not need to know a field's name to read it.
"""

import struct
from datetime import datetime, timezone

SIGNATURE = b"\x00\x00\x00\x00\x00\x1a\xb1\x26"

MAX_DEPTH = 64
_FIELD_BEGIN = 0xFE
_FIELD_END = 0xFF
_FIXED = {
    0: ("?", 1), 1: (">i", 4), 2: (">q", 8), 4: (">d", 8),
    5: (">h", 2), 6: (">f", 4), 7: (">b", 1), 9: (">B", 1),
}


class KrdsError(ValueError):
    pass


def is_krds(data):
    return data[:8] == SIGNATURE


class _Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def take(self, n):
        if self.pos + n > len(self.data):
            raise KrdsError("unexpected end of data at offset %d" % self.pos)
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def string(self):
        if self.take(1)[0]:
            return ""
        (length,) = struct.unpack(">H", self.take(2))
        return self.take(length).decode("utf-8", errors="replace")

    def value(self, kind):
        if kind == 3:
            return self.string()
        if kind not in _FIXED:
            raise KrdsError("unknown value type %d at offset %d" % (kind, self.pos - 1))
        fmt, size = _FIXED[kind]
        return struct.unpack(fmt, self.take(size))[0]

    def field(self, depth=0):
        if depth > MAX_DEPTH:
            raise KrdsError("fields nested deeper than %d at offset %d" % (MAX_DEPTH, self.pos))
        node = {"name": self.string(), "values": []}
        while True:
            kind = self.take(1)[0]
            if kind == _FIELD_END:
                return node
            if kind == _FIELD_BEGIN:
                node["values"].append(self.field(depth + 1))
            else:
                node["values"].append(self.value(kind))


def decode(data):
    """Decode a sidecar file. Returns {'header': [...], 'fields': [...]}.

    Each field is {'name': str, 'values': [value or nested field, ...]}.
    """
    if not is_krds(data):
        raise KrdsError("signature not found")
    reader = _Reader(data)
    reader.pos = len(SIGNATURE)
    header, fields = [], []
    while reader.pos < len(data):
        kind = reader.take(1)[0]
        if kind == _FIELD_BEGIN:
            fields.append(reader.field())
        elif kind == _FIELD_END:
            raise KrdsError("field end without a field at offset %d" % (reader.pos - 1))
        else:
            header.append(reader.value(kind))
    return {"header": header, "fields": fields}


def walk(fields):
    """Yield every field, depth-first."""
    for node in fields:
        yield node
        nested = [v for v in node["values"] if isinstance(v, dict)]
        for inner in walk(nested):
            yield inner


def ms_to_utc(value):
    """Milliseconds since 1970 to ISO 8601 UTC, or '' if not a plausible time."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return ""
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat(
            timespec="milliseconds")
    except (OverflowError, OSError, ValueError):
        return ""


_ANNOTATION_PREFIX = "annotation.personal."
_POSITION_FIELDS = ("lpr", "fpr", "updated_lpr", "erl")


def annotations(decoded):
    """Annotation records: highlights, bookmarks, notes and other kinds.

    Value order inside an annotation field: start position, end position,
    created (ms), last modified (ms), template, then the note text if any.
    """
    out = []
    for node in walk(decoded["fields"]):
        if not node["name"].startswith(_ANNOTATION_PREFIX):
            continue
        values = [v for v in node["values"] if not isinstance(v, dict)]
        strings = [v for v in values if isinstance(v, str)]
        times = [v for v in values if isinstance(v, int) and not isinstance(v, bool)]
        out.append({
            "kind": node["name"][len(_ANNOTATION_PREFIX):],
            "start_position": strings[0] if len(strings) > 0 else "",
            "end_position": strings[1] if len(strings) > 1 else "",
            "created_utc": ms_to_utc(times[0]) if len(times) > 0 else "",
            "modified_utc": ms_to_utc(times[1]) if len(times) > 1 else "",
            "template": strings[2] if len(strings) > 2 else "",
            "note": strings[3] if len(strings) > 3 else "",
            "raw_values": values,
        })
    return out


def positions(decoded):
    """Reading positions (last page read and similar) with their times."""
    out = []
    for node in decoded["fields"]:
        if node["name"] not in _POSITION_FIELDS:
            continue
        values = [v for v in node["values"] if not isinstance(v, dict)]
        strings = [v for v in values if isinstance(v, str) and v]
        times = [v for v in values
                 if isinstance(v, int) and not isinstance(v, bool) and v > 10 ** 11]
        out.append({
            "field": node["name"],
            "position": strings[0] if strings else "",
            "time_utc": ms_to_utc(times[0]) if times else "",
            "raw_values": values,
        })
    return out
