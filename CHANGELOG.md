# Changelog

## 0.4.1

### Security

- `parse`, `verify`, `survey` and `extract` refuse a path in a manifest,
  a deleted-entry list or an image log that points outside the
  acquisition folder. Before, a folder received from someone else could
  make `parse` read files on the examination computer into its output.
- Text in the parsed CSV files and in the `compare` difference list that
  a spreadsheet would run as a formula gets a leading apostrophe. The
  SQLite file keeps the text unchanged.
- The examiner report carries a content security policy: no scripts, no
  outside requests.
- The window shows paths, device names and error messages as plain text.
  Before, markup in such text could make the window load an image from a
  network path.
- A JSON record nested deeper than Python allows no longer stops a parse.
- `extract` stops reading deleted files back once they hold as much data
  as the volume can store. Many deleted entries can name the same free
  space.
- `logical` does not follow a junction or mount point in the source.
- Databases are opened with an untrusted schema, and the parsed database
  is opened read-only by `survey`.

## 0.4.0

### Added

- A reader for SQLite write-ahead logs. It returns the rows of every page
  version, including rows the database no longer holds.
- `activity_records`, `reading_sessions`, `app_sessions`, `connectivity`
  and `power_events`, from the device's activity log and its write-ahead
  log.
- `audiobook_positions` and `audiobook_links`, from the databases under
  `audible/`.
- `store_storage`, from the store pages' local storage.
- `browser_cache`, from both Chromium cache formats. Block-file entries
  carry their creation time in UTC.
- `thumbnails` and `page_history`.
- All of these feed the timeline, the device summary and the examiner
  report. The summary gains the time zone setting, the account ID and the
  library counts.
- `tools/scsi_probe.py`: asks a test device which SCSI commands it
  answers. It is not part of the acquisition and is not for evidence.
- Tests for the log reader on hand-built pages and on a real SQLite log,
  and fuzz tests for the log reader, the activity log and the browser
  cache.

### Changed

- The timeline source `queued activity record` is now `activity log` and
  includes records from earlier page versions.
- `activity_by_day` has columns for the activity log, audiobooks, store
  pages and the browser cache.

## 0.3.0

### Added

- Analysis tables, built by `parse` from the parsed rows: `device_summary`,
  `documents`, `timeline`, `activity_by_day`, `clock_offsets` and
  `observations`.
- The examiner report, `parsed/report.html`: one self-contained HTML page
  with its SHA-256 beside it. `report` writes it again from an existing
  parse.
- The Qt results viewer has an Open Report button and opens on the device
  summary.
- A model name indicated by the serial number, for four models.
- Screenshots of the timeline and the report, from synthetic data.

### Changed

- `parse` returns and prints the path of the report.

## 0.2.0

### Added

- `compare`: compares the records of two acquisitions and lists files
  added, removed or changed and times that differ.
- `survey`: writes a content-free profile of a device's structure, for
  reporting a model or firmware the tool does not know.
- Both are in the Qt window under File Operations.
- Regression tests: the full output of extract and parse on an invented
  Kindle is checked against a stored copy.
- Fuzz tests: damaged images, sidecar files and clippings text must be
  reported, not crash the tool.
- Tests for unreadable sectors and for a source that stops responding.
- Tests that both windows start.
- Continuous integration on Windows and Linux.
- `docs/NEW_DEVICE.md`: a checklist for testing a new model.
- A logo, and screenshots built from synthetic data.

### Changed

- `image` stops when the source returns nothing for 4 MiB in a row and
  writes a log with status `incomplete`. Before, it would read on one
  sector at a time to the end of the disk. `verify` fails an incomplete
  image.
- `image` records unreadable ranges of the second read.
- The FAT32 reader rejects a boot sector that describes more clusters
  than FAT32 allows, or no data area, before reading the table.
- `parse` does not load a sidecar file larger than 64 MiB. It is listed
  in `parse_errors`.
- The package version comes from one place.

### Fixed

- `parse` failed while writing a CSV when a value held a NUL character.
  The CSV now shows it as `\x00`. The SQLite file keeps the value.
- The Tkinter window did not start (0.1.0 after the Qt window was added).
- The logical copy kept its time-zone offset in a module variable.

## 0.1.0

First version: detect, image, extract, logical, verify, parse, and the
Tkinter and Qt windows.
