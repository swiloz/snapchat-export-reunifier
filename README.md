# Snapchat Export Reunifier

A safer Snapchat **My Data** processor for restoring timestamps and GPS metadata, merging Snapchat overlays, and preparing media for Apple Photos, iCloud Photos, Google Photos, Immich, and other photo libraries.

This fork keeps the original project's goal but hardens the parts that are fragile with newer Snapchat exports, especially exports where `Download Link` and `Media Download Url` are blank.

## What changed in this fork

- **Supports newer My Data exports with blank download links.**
- Uses an exact **UUID match** when Snapchat still exports one.
- Uses safe **day + media type** matching when only one candidate exists.
- For days with multiple photos/videos, uses preserved filesystem timestamps to recover the correct JSON entry instead of blindly trusting file order.
- **Ambiguous matches are not assigned by default.** This avoids silently giving one Snap another Snap's time or GPS.
- Optional legacy `--unsafe-index-fallback` reproduces the old order-based behavior when explicitly requested.
- Produces `logs/matches.csv` with match method, confidence, timestamp, GPS, and time delta.
- **Merges `*-overlay.png` files** with their `*-main` photo or video when requested.
- Preserves the original overlays separately even when they are merged.
- Adds `--dry-run` so you can audit matching before generating media.
- Uses SHA-256 deduplication and defaults to deduplicating only within the same day/type.
- Supports IANA timezones such as `Europe/Paris`, including historical DST rules.
- Writes broader QuickTime metadata for videos and EXIF timestamps for photos.

## Important matching behavior

Newer Snapchat exports may look like this:

```text
2021-01-05_ab8798d4-03f2-4410-7819-f91e8e0ee1f6-main.jpg
2021-01-05_ab8798d4-03f2-4410-7819-f91e8e0ee1f6-overlay.png
```

while `memories_history.json` contains:

```json
{
  "Date": "2021-01-05 08:06:42 UTC",
  "Media Type": "Image",
  "Location": "Latitude, Longitude: 44.701897, 4.795153",
  "Download Link": "",
  "Media Download Url": ""
}
```

There is no longer an explicit UUID link between the two. The old implementation paired files and JSON entries by index within a day. That can be wrong because Snapchat's media HTML/files can be ordered by UUID rather than capture time.

This fork uses the following hierarchy:

1. Exact UUID from JSON URL when available — **high confidence**.
2. Only one file and one JSON entry for a day/type — **high confidence**.
3. Filesystem timestamp correlated with JSON time — **high/medium confidence**.
4. Otherwise, the file is marked **ambiguous** and gets date-only metadata instead of potentially incorrect GPS/time.
5. `--unsafe-index-fallback` is available only if you deliberately want the previous behavior.

## Requirements

- Python **3.9+**
- [ExifTool](https://exiftool.org/) — required for writing metadata
- [Pillow](https://pillow.readthedocs.io/) — required to merge photo overlays
- [FFmpeg](https://ffmpeg.org/) — required to merge video overlays

Install the Python dependency:

```bash
pip install -r requirements.txt
```

### Windows

If ExifTool is not on `PATH`, you can pass the executable directly:

```powershell
python process.py "C:\Snapchat\MyData" `
  --exiftool "C:\ExifTool\exiftool.exe" `
  --timezone Europe/Paris
```

The same applies to FFmpeg with `--ffmpeg`.

## Recommended workflow

### 1. Audit first

Do this before processing thousands of Memories:

```powershell
python process.py "C:\Snapchat\MyData" --dry-run --timezone Europe/Paris
```

Then inspect:

```text
logs/matches.csv
```

Pay particular attention to:

- `uuid` / `unique-day-type` / `filesystem-time` → expected
- `high` / `medium` confidence → generally safe
- `ambiguous` → no precise JSON entry was assigned
- `index-fallback` → only appears if you enabled the unsafe fallback

### 2. Process and merge overlays

```powershell
python process.py "C:\Snapchat\MyData" --timezone Europe/Paris --overlay-mode merge
```

### 3. Import the generated folder

By default the result is written beside your export:

```text
photos-ready/
├── 2019/
│   ├── 03/
│   └── ...
├── 2020/
└── ...

overlays-only/
└── ...                 # original overlay PNGs are preserved

logs/
├── matches.csv          # full matching audit
├── duplicates.log
├── failed_files.log
├── warnings.log
└── summary.txt
```

Import `photos-ready/` into your photo library.

## Overlay modes

```text
--overlay-mode merge      Merge overlay onto photo/video (default)
--overlay-mode separate   Keep the main media unchanged; preserve overlays separately
--overlay-mode ignore     Ignore overlay files entirely
```

Photo overlays are composited with Pillow. Video overlays are rendered with FFmpeg/H.264. If an overlay render fails, the tool falls back to copying the main media rather than dropping the Memory.

## Timezones

Snapchat JSON timestamps are UTC. For photos, EXIF generally needs the local wall-clock time plus its UTC offset.

Prefer an IANA timezone:

```powershell
--timezone Europe/Paris
```

This handles summer/winter time correctly for that zone. A fixed offset is also available:

```powershell
--tz-offset +02:00
```

A fixed offset does **not** adapt to DST and is therefore less robust for multi-year libraries.

## Deduplication

The default is:

```text
--dedupe within-day
```

Modes:

```text
none        Never deduplicate
global      Remove byte-identical media globally
within-day  Remove byte-identical media only within the same day/type (default)
```

`within-day` is safer than global deduplication because an identical file can legitimately appear as a separate saved occurrence on another date.

## Safety and limitations

- The tool **never modifies the original Snapchat export**.
- No algorithm can perfectly reconstruct a missing UUID relationship if Snapchat removed every linking identifier and the filesystem timestamps were also destroyed during extraction.
- Ambiguous files are deliberately left without precise JSON time/GPS by default rather than receiving another Memory's metadata.
- Files that have no precise JSON match use the date from the filename at `12:00:00`.
- `--unsafe-index-fallback` can maximize the number of matches but can associate the wrong GPS/time when several Memories share the same day/type.
- Video overlay merging requires re-encoding the video stream.

## Testing

```bash
python -m unittest -v
```

The tests cover UUID parsing, blank-link exports, safe single-item matching, ambiguous groups, timestamp-assisted matching, and overlay pairing.

## License

MIT. Based on the original `carusojude/snapchat-export-reunifier` project.
