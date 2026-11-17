# Method notes

What the tool relies on, and how each point was established. Device
observations come from one Kindle (serial prefix G001PX, firmware 5.19.2,
USB ID 1949:0324) examined on Windows 11 in November 2026. Repeat the
checks on any other model or firmware before relying on them.

## Storage layout

The device presents one USB disk. Windows reports a single FAT32 volume at
offset 0 of that disk, with the label `Kindle`.

| Path | Content |
| --- | --- |
| `documents/My Clippings.txt` | Running text log of highlights, notes and bookmarks |
| `documents/Downloads/Items01/` | Downloaded books and personal documents, each with a `.sdr` sidecar folder |
| `system/version.txt` | Firmware version |
| `system/vocabulary/vocab.db` | Dictionary lookups (SQLite) |
| `system/freetime/freetime.db` | Reading statistics per book and per day (SQLite) |
| `system/fmcache/fmcache.db` | Queued device activity records with JSON bodies (SQLite) |
| `system/browser/` | Cache and storage of the built-in web browser |
| `audible/` | Audiobooks and their databases |

File names carry an identifier:

- `<title>_<ASIN>.kfx` for a store book, with `_sample` appended for a sample.
- `<name>-cdeKey_<32 hex characters>.pdf` for a personal document delivered
  through the Amazon account.

## Sidecar files

Files in a `.sdr` folder with the extensions `.yjr`, `.yjf`, `.pds`, `.pdt`
(and `.azw3r`, `.azw3f`, `.mbp1`, `.mbs` on other formats) share one binary
format. `parsers/krds.py` documents and decodes it. The tool recognises
the format by its 8-byte signature, not by file extension.

An annotation field holds, in order: start position, end position, created
time, last-modified time, a template string, and for notes the note text.
Times are milliseconds since 1970 in UTC.

## Times

### The Kindle writes FAT times in UTC

Evidence, from one acquisition:

1. A highlight's created time inside its sidecar file was 04:13:20 UTC.
2. `My Clippings.txt` recorded the same highlight at 11:13:20 PM the
   evening before. The device clock was five hours behind UTC on that date.
3. The FAT modified time of `My Clippings.txt`, as stored, was 04:13:20
   on the UTC date.
4. Of 41 sidecar files modified from May to October 2026, 34 had a stored
   FAT time within 5 seconds of the latest time recorded inside the same
   file. The median difference was 1 second.

### Windows shifts those times

Windows treats a FAT time as host local time and converts it to UTC with
the host's current offset. On a host at UTC-5, the file in point 3 is
reported as 09:13:20 UTC, five hours late. The manifest column `modified_utc` holds that
host value. The column `modified_stored` reverses the conversion and holds
the value on the volume. The log records the offset used
(`host_utc_offset_seconds`).

Windows applies the current offset to every date, including dates in the
other half of the daylight-saving year. Point 4 covers both halves.

### Files written by the host

`System Volume Information` is created by Windows when the volume mounts.
Its stored time is host local time. It marks when the device was connected
to a Windows computer without a write blocker.

## What a mounted volume changes

Two logical runs were made back to back with no write blocker.

- All 2,541 file hashes were identical between the runs.
- No modified time changed.
- The access date changed on 2,392 of 2,836 entries. Windows updated the
  access date on the device as the first run read each file.

A hardware write blocker prevents this. The `image` command reads the raw
disk and does not go through the file system.

## Raw image

The `image` command read the 6,702,472,704-byte disk twice. Both reads
gave the same SHA-256 and the image file matched. No sector was
unreadable. One read took about six minutes over USB.

## Extraction checked against the logical copy

`extract` was run on that image and compared with a logical copy of the
same device made through Windows.

- Both held 2,541 files and 295 folders with the same paths.
- All 2,541 files had the same SHA-256.
- The stored modified and created times matched for all 2,836 entries.
  The logical copy derives them by reversing the host's conversion. The
  extraction reads them from the directory entries. The two methods agree.
- File attributes matched for all entries.

The volume has 817,372 clusters of 8,192 bytes. 704,164 were unallocated.

## Deleted entries

The image held 1,271 deleted directory entries: 1,157 files and 114
folders.

| Result | Files |
| --- | --- |
| Read back as one unbroken run of unallocated clusters | 763 |
| Empty file, nothing to read | 286 |
| Not read back, a needed cluster is in use | 108 |

Of the 763 files read back, 277 had an extension with a known header
(PDF, JPEG, PNG, KFX, SQLite). All 277 began with the expected header.
All 37 sidecar files among them decoded without error. A matching header
shows the first cluster is right. It does not prove the rest of the file.

### Names of deleted entries

Deleting an entry overwrites the first byte of its short name and the
sequence numbers of its long-name parts. The `name_source` column says
what the listed name rests on.

| `name_source` | Meaning | Entries |
| --- | --- | --- |
| `long` | Long name with its end mark. The checksum in the long-name parts fits the short entry and the name's first character | 886 |
| `long, may be incomplete` | Long name with no end mark. Either the name is an exact multiple of 13 characters, or its last part was overwritten by a later entry | 223 |
| `long, not confirmed by checksum` | Long-name parts sit before the entry but the checksum does not fit. The name may belong to another file | 0 |
| `short, first character lost` | No long name. The first character is written as `_` | 162 |

### Limits of deleted-file recovery

- A file stored in more than one run of clusters is not recovered.
- Some FAT drivers clear the upper half of the first-cluster number when
  they delete a file. A file deleted by such a host, above cluster 65,535,
  would be read from the wrong place. Files deleted by the Kindle itself
  were not affected in testing. This has not been tested with a file
  deleted from a Windows computer.
- A deleted folder is followed into the next cluster only while that
  cluster still looks like directory entries.

## DRM

The tool reads the first bytes of each content file to name its container.
A KFX file that begins with the bytes `EA 44 52 4D 49 4F 4E EE` is reported
as DRM-protected. The tool does nothing further with it.

## Analysis tables

`parse` builds six tables from the rows it has already parsed. They read
no file and add no fact. Each row names the record it came from.

### Timeline and time bases

The timeline holds four kinds of time and names the kind in `time_basis`:

- UTC from the device clock: millisecond values in sidecar files and
  device databases.
- Device local time: `My Clippings.txt`.
- Device local date: the day rows of the reading statistics.
- FAT time as stored: created and modified times of live and deleted
  directory entries. The tested device writes these in UTC.

Rows are sorted by the time as written. A clipping made at 21:14 device
local time sorts before the sidecar annotation of the same event at 02:14
UTC the next day. The order across bases is true only after the offset is
applied.

### Device clock offset

A highlight, note or bookmark writes two records: a clipping with device
local time and a sidecar annotation with UTC. The tool pairs a clipping
with an annotation of the same kind when the two times differ by a
multiple of 15 minutes, to within two seconds, between UTC-12:00 and
UTC+14:00. It prefers a pair whose titles agree. It accepts a pair whose
titles differ only when one annotation fits.

On the test device all six clippings were paired on title, kind and time.
Five clippings gave one offset. The sixth, made after the autumn clock
change, gave an offset one hour lower.

The offset is the device's time zone setting at that moment. It does not
show that the device clock was correct.

### Documents and titles

A title comes from the device's reading statistics or vocabulary database
when the ASIN in the file name is listed there, else from the file name.
`title_source` says which. Deleted files under an `assets` folder are a
book's own resource files and get no row.

### Model from the serial number

Characters four to six of the serial number identify the model on recent
devices. The tool's list covers four models and comes from community
device lists. The test device's code is `1PX`, listed as the Paperwhite
11th generation. This has not been checked against the label on the
device.

## Activity log

`system/fmcache/fmcache.db` holds three tables: `records`,
`app_sessions` and `reading_sessions`. The device queues activity records
there and removes them later. On the test device the database held 17
records at the time of imaging.

The database runs in write-ahead mode. Its `-wal` file was 4.1 MB and held
1,004 page frames from three generations. Each frame is a full copy of a
page as it was when written. The tool decodes every table leaf page in the
database file and in every frame, and keeps each distinct row once.

On the test device this gave 782 distinct records, 9 reading sessions and
9 app sessions. 765 of the records were no longer in the database. The
records ran from 03:29 to 19:16 UTC on the day of imaging. One reading
session dated from four days earlier.

A row is assigned to a table by its shape: ten columns with text in the
second and tenth for a record, four columns with a JSON session in the
third for a session. A row that continues on an overflow page is not
used. None did on the test device.

A session row is rewritten while the session runs. The tool keeps the
version with the latest end time. Page turns are counted from the
`ereader_book_page_turn` records that carry the session's number and fall
between its start and end.

The two long sessions on the test device ran 2,143 and 1,323 seconds with
70 and 22 page turns. Six sessions lasted 60 seconds with no page turn:
the device woke, reopened the book and slept again.

### What the activity log does not hold

Connection records name the kind of connection and its state. They do not
name the network. No file on the USB volume held a network name or
password.

## Browser cache

The browser is Chromium. Its main cache is in the block-file format:
`index`, `data_0` to `data_3` and `f_*` files. Each 256-byte entry record
in `data_1` holds the address and a creation time in microseconds since
1601, in UTC. The tool reads every block that has a plausible time, a key
length that fits and a key containing `://`. On the test device this gave
462 entries from 22 hosts. A key longer than one entry record's blocks is
stored elsewhere and is not read.

The code cache and service worker cache use the simple format, one file
per entry. These carry no creation time. They are dated by the stored
file time. The test device held 46.

## Audiobooks and store pages

`audible/default.audible.db` holds one `ANNOTATIONS` row per audiobook of
type `REMOTE_LPH`, the last position heard, with a time and the name of
the app that set it. The meaning of its `LTO` column is not confirmed. It
held 0 on one row and -240 on the other.

The store pages keep a WebKit local storage file. Keys starting `*index*`
hold the time a store request was saved. Keys starting `*cache*` hold the
response. On the test device the file held one book page and one order
response.

## What the device answers over USB

In mass-storage mode the test device presents one USB interface: class
08, subclass 06, protocol 50. That is mass storage with SCSI commands over
bulk-only transport, and one logical unit.

`tools/scsi_probe.py` sent it the standard read-only queries and then one
command per operation code from 0x00 to 0xFF, each with zero arguments.
52 codes that write, erase, format, reserve, stop the unit or load
firmware were not sent.

| Answer | Operation codes |
| --- | --- |
| Accepted, or recognised with the zero arguments rejected | 12 |
| Rejected as an invalid operation code | 191 |
| Not delivered by Windows | 1 |
| Not sent | 52 |

The 12 are TEST UNIT READY, REQUEST SENSE, INQUIRY, MODE SENSE (6 and
10), PREVENT ALLOW MEDIUM REMOVAL, READ FORMAT CAPACITIES, READ CAPACITY,
READ (10 and 12), VERIFY and SYNCHRONIZE CACHE. All 64 vendor-specific
codes from 0xC0 to 0xFF were rejected. The device refused the vital
product data pages of INQUIRY. The SHA-256 of the whole device was the
same before and after.

Not tested: the 52 codes that were not sent, commands with other
arguments, and USB control requests below SCSI.

## My Clippings.txt

The device appends to this file. In the test data it held two bookmark
entries with no matching annotation in any allocated sidecar file. Both bookmarks were found in deleted sidecar files read
back from the image, for books no longer on the device. The times agree
to the second once the device's UTC offset is applied.

The allocated sidecar files held four highlights with no entry in the
clippings file. The two sources do not record the same
set of events and both should be reported.
