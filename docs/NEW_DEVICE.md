# Bringing a new Kindle into testing

A checklist for a model or firmware the tool has not seen. It produces a
baseline to test against later, and a content-free profile that shows
what the parsers do not yet understand.

Use a test device, or a device you are authorised to examine. Keep every
output folder out of this repository.

## 1. Before connecting

- Put the device in airplane mode. A sync can change or remove content.
- Note the model name and number from the back of the device or from
  Settings, Device Info. Note the firmware version shown there.
- Connect through a hardware write blocker if one is available. Record
  whether one was used: it decides which checks below are meaningful.

## 2. Detect

```
kindle-acquire detect --json > detect.json
```

- `mode` is `mass-storage`: continue with step 3.
- `mode` is `mtp`: the device has no disk to image and the copy path is
  not implemented yet. Save `detect.json` and stop here. Open an issue
  with that file. It holds the serial number, so shorten it to the first
  six characters before sharing.
- `mode` is `unknown`, or nothing is listed: save the output of
  `Get-PnpDevice -PresentOnly | Where-Object InstanceId -match 'VID_1949'`
  and open an issue.

## 3. Acquire

From an elevated prompt:

```
kindle-acquire image --out <runs>\<name>_image --verify-source
kindle-acquire extract <runs>\<name>_image --out <runs>\<name>_extract
kindle-acquire verify <runs>\<name>_image
kindle-acquire verify <runs>\<name>_extract
kindle-acquire parse <runs>\<name>_extract
```

Expected: the second read of the device matches the first, no unreadable
bytes, no extraction errors, both verifications pass, `parse_errors` is 0.
Any other result is a finding. Record it before going on.

## 4. Cross-check the two acquisition paths

Without a write blocker only, since this step mounts the volume:

```
kindle-acquire logical --out <runs>\<name>_logical
kindle-acquire compare <runs>\<name>_extract <runs>\<name>_logical
```

Expected: "same files, same hashes", and no differences in
`modified_stored` or `created_stored`. A difference in the stored times
means the device does not write FAT times the way the tested devices do.
See "Times" in [METHOD.md](METHOD.md).

## 5. Profile

```
kindle-acquire survey <runs>\<name>_extract
```

`support_profile.json` lists file types by area, sidecar field names with
the shape of their values, database schemas, and the kinds of parse
error. It holds no titles, text, times of use or account identifiers.
Read it before sharing it.

Look for:

- extensions or signatures marked `<not recognised>` in the documents area
- sidecar fields the tool does not turn into records
- `detail_lines_not_understood_masked` in `clippings`, which means the
  device writes `My Clippings.txt` in a wording the parser does not know
  (usually another interface language)
- databases with rows that no parsed table draws on
- an empty `activity_records` table on a device that holds
  `system/fmcache/fmcache.db`: the table layout has changed
- `browser_cache` rows with no `created_utc` under a `Cache` folder: the
  cache format has changed

## 6. Check the records against the device

Open `parsed/report.html`. Check the model line against the label on the
device: the model comes from a list of serial number codes that covers
four models. Check the device clock offset against the time zone set on
the device. Note the device clock and a reference clock side by side.

Pick three annotations and three clippings on the device screen and find
them in the parsed tables. Check the text, the kind and the time. Add a
typed note if the device has none, acquire again, and confirm the note
text appears in `annotations.note`. This check has not been made on any
device yet.

## 7. Optional: what the device answers

On a test device only, never on evidence, from an elevated prompt:

```
python tools\scsi_probe.py --drive <N> --out <runs>\<name>_probe --hash
```

It records which SCSI commands the device accepts and whether its content
changed. Compare the accepted list with "What the device answers over
USB" in [METHOD.md](METHOD.md). An accepted code outside that list is a
finding. Do not publish it before the vendor has been told.

## 8. Record the result

- Add a row to "Tested devices" in the README: model, serial prefix (six
  characters), firmware, USB ID, mode, and which commands passed.
- Add any new observation to [METHOD.md](METHOD.md) with the figures.
- Keep the image and its acquisition log. They are the baseline: after a
  change to the tool, extract the same image again and run `compare`
  against the earlier extraction. The result must be "same files, same
  hashes" unless the change was meant to alter the output.
- If a parser needed changing, add the smallest synthetic case that
  shows the new format to the tests. Do not add device data.
