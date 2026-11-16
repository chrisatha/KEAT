# KEAT

Kindle E-Reader Acquisition Tool. Forensic acquisition of Amazon Kindle
e-readers. The command and the Python package are named `kindle-acquire`
and `kindle_acquire`. `keat` runs the same command.

The tool copies a Kindle's user storage with a hash for every file, or
images the storage disk sector by sector. It then parses the records an
examiner usually needs: highlights, notes, bookmarks, reading positions,
dictionary lookups, loaded documents and reading statistics.

It copies data as stored. It does not remove, bypass or decrypt DRM.
Protected books are copied and identified as protected. They are not opened.

Status: early. Tested on one device (see "Tested devices"). Python 3.8 or
later, Windows, standard library only.

## Commands

```
kindle-acquire detect
kindle-acquire logical --out <folder> [--case C] [--evidence E] [--examiner N]
kindle-acquire image   --out <folder> [--verify-source]        (administrator)
kindle-acquire extract <image folder or image file> --out <folder>
kindle-acquire verify  <acquisition folder>
kindle-acquire parse   <logical or extracted folder> [--known <folder or hash list>]
```

The recommended sequence for evidence is write blocker, `image`, `extract`,
`parse`. After `image` the device is not needed again.

Run from a checkout without installing:

```
set PYTHONPATH=src
python -m kindle_acquire detect
```

## Window

Double-click `kindle-acquire-gui.pyw`, or run `kindle-acquire gui`.

There are two windows over the same functions:

- The **Qt window** opens when PySide6 or PyQt6 is installed
  (`pip install PySide6`, or `pip install .[gui]`). Device details sit in
  a panel on the left. The right side is a menu of actions with a
  description beside each, a second menu for work on existing
  acquisitions, and a progress screen while a step runs. The layout
  follows the UFADE and ALEX extraction tools by Christian Peter. No code
  or assets from those projects are used. **View Results** opens
  `kindle_parsed.db`: pick a table, filter rows, sort by a column, and
  select a row to read every column in full.
- The **Tkinter window** opens otherwise, or with `kindle-acquire gui --tk`.
  It needs nothing beyond Python.

Qt is used by the window only. The acquisition code stays on the
standard library.

The window shows the connected device and its write-protection state,
takes the case fields and an output folder, and names each output folder
from the evidence item, the action and the time.

- **Full acquisition** images the disk, extracts from the image and parses
  the result, in that order, and stops at the first failure.
- **Image the disk** needs administrator rights. The window offers to
  restart itself elevated. The device is detected again when the action
  starts, and the action stops if that Kindle is no longer connected.
- **Copy files from the mounted volume** warns first when the volume is
  mounted read-write.
- **Extract**, **Parse** and **Verify** ask for an existing acquisition
  folder. **Known hashes** is passed to Parse.
- **Cancel** stops a copy, image or extraction. The output folder is left
  incomplete and has no acquisition log.

After **Parse** or **Full acquisition** the Qt window's progress screen
offers **View results**.

### detect

Lists USB devices with Amazon's vendor ID (1949). Reports the serial
number, the product ID, the disk number and the mounted volume. Reports
whether the device is a mass-storage disk or an MTP device.

### logical

Copies every file from the mounted volume into `<out>/files`.

- Each file is hashed (SHA-256 and MD5) while it is read from the device.
- The copy is read back and compared with that hash.
- `manifest.csv` has one row per file and folder: size, hashes, times,
  attributes, status. Errors are rows. Nothing is skipped silently.
- `acquisition_log.json` records the tool version, host, device, case
  fields, counts, errors and the hash of the manifest.

### image

Reads the Kindle's disk (`\\.\PhysicalDriveN`) into a raw image and
computes MD5, SHA-1 and SHA-256. The image file is read back and compared.
`--verify-source` reads the device a second time and compares again.
Unreadable sectors are zero-filled and listed in the log. Needs an
elevated prompt. The image holds unallocated space, which a logical copy
does not.

### extract

Reads the FAT32 file system inside a raw image with the tool's own reader.
Nothing is mounted. The output has the same layout as a logical
acquisition, and `verify` and `parse` work on it.

- Every allocated file is written to `files/`, hashed and read back.
- Times are the FAT values as stored, with no conversion by the host.
- Deleted directory entries are listed in `deleted_entries.csv` with their
  names, sizes, times, first cluster and byte offset in the image.
- A deleted file is read back into `deleted/` when the clusters it would
  occupy as one unbroken run are all unallocated. This is the usual
  assumption for FAT recovery. The content is not confirmed to be the
  original file, and the row says so.
- Broken allocation chains, unreadable folders and two names that share
  one set of clusters are reported as errors. The run continues and the
  log is still written.
- Names in an image are treated as untrusted. Characters Windows cannot
  store are replaced with `_`, reserved device names get a `_` prefix, and
  a second name that would land on the same output path gets `~2`. The
  manifest row records the name as it is on the volume.

### verify

Re-hashes an acquisition folder against its own manifest or log. Reports
changed and missing files, and files added under `files/` or `deleted/`.
Recovered deleted files are included.

### parse

Reads a logical acquisition (the copies, never the device) and writes
`parsed/kindle_parsed.db` plus one CSV per table:

| Table | Content |
| --- | --- |
| `content_files` | Books, PDFs and audio files: hashes, container type, ASIN or cdeKey from the file name, sidecar folder, annotation count |
| `annotations` | Highlights, notes and bookmarks from sidecar files, with created and modified times. `file_state` says whether the sidecar file was allocated or deleted and read back |
| `reading_positions` | Last page read and similar positions, with times |
| `clippings` | Entries from `My Clippings.txt` |
| `vocabulary_lookups` | Dictionary lookups with the sentence and the book |
| `sidecar_fields` | Every field of every sidecar file, decoded |
| `database_inventory` | Every SQLite database found, with tables and row counts |
| `freetime_*`, `fmcache_records` | Reading statistics and queued device activity records, as stored |
| `deleted_entries` | Deleted directory entries and recovery results (extracted images only) |
| `known_matches` | Files whose hash is in the reference set given with `--known` |
| `parse_errors` | Every file a parser could not read, with the reason |

`--known` takes a folder of reference files, or a text or CSV file with
one MD5 or SHA-256 per line. Every file in the acquisition and every
recovered deleted file is compared. A match means the content is
identical. No match does not show the document was never on the device:
a converted or edited copy has a different hash.

## Write protection

A Kindle in mass-storage mode is a FAT32 disk. Windows writes to a FAT32
volume when it mounts it and updates access dates when files are read. In
testing, one logical run changed the access date of 2,392 of 2,836
entries on the device.

For evidence, connect the Kindle through a hardware USB write blocker and
use `image`. Use `logical` on a mounted volume only when a write blocker
cannot be used, and say so in the report. `detect` and `logical` report
whether the host's USB write-protect policy is on.

Keep the device off Wi-Fi. A sync can remove content that was deleted
from the Amazon account.

## Times

Three kinds of time appear in the output. They are not interchangeable.

- `*_utc` in parsed tables: millisecond values the device wrote inside its
  own files. These are UTC.
- `*_stored` in the manifest: the FAT directory time as stored. FAT has no
  time zone. On the tested device the Kindle wrote UTC.
- `*_utc` in the manifest: the host's conversion of the FAT time. Windows
  assumes host local time, which shifts a Kindle's times by the host's UTC
  offset. Prefer `*_stored`.
- `added_device_local` in `clippings`: the clock time the device printed,
  in the device's time zone setting.

See `docs/METHOD.md` for how these were established.

## Limits

- MTP devices (Kindle Scribe, 2024 models) are detected but not yet copied.
- Scribe notebooks are not exposed over USB and are out of reach of this
  version.
- The system partition (content catalog, system logs) is not reachable
  over USB.
- Raw images are single-file `dd`. No E01, no split files.
- `extract` reads FAT32 only. Deleted files stored in more than one run of
  clusters are not recovered. Unallocated space is not carved. See
  `docs/METHOD.md` for the limits on deleted names and recovery.
- CSV output holds device text unchanged. A cell that starts with `=` is
  a formula to a spreadsheet program. Open the CSV files as text or use
  the SQLite file.
- The window's image and copy buttons have been run against synthetic
  data, not yet against a device. The functions behind them have.
- Note text in sidecar files is decoded by position. It is covered by a
  synthetic test and has not been checked against a device that holds notes.

## Tested devices

| Model | Serial prefix | Firmware | USB ID | Mode | Result |
| --- | --- | --- | --- | --- | --- |
| Kindle (model to confirm) | G001PX | 5.19.2 | 1949:0324 | mass storage | logical, image, extract, verify, parse |

## Tests

```
python -m unittest discover -s tests
```

Tests use synthetic data. Do not add device data to this repository.
