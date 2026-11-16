# Changelog

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
