# Snapchat Export Reunifier

**Reunify your Snapchat data after export.** Snapchat strips EXIF metadata—timestamps, GPS coordinates, camera info—from your photos and videos when you download your data. This tool puts it back.

## The Problem

When you request your data from Snapchat (or most social platforms), you get your photos and videos back—but they've been **deliberately stripped of metadata**. The timestamps that tell you *when* a photo was taken? Gone. The GPS coordinates that tell you *where*? Gone. What you receive is a pile of unnamed files with no meaningful way to organize them.

This isn't a technical limitation. It's a design choice.

### Why platforms strip your metadata

Social media companies store your metadata separately from your media files. When you upload a photo, the platform extracts the EXIF data (date, time, location, camera model) and indexes it in their own databases for ad targeting, content ranking, and engagement optimization. The original embedded metadata is discarded or overwritten during their processing pipeline.

When you later request an export of "your data," they return the processed media files—not the originals. The metadata *does* exist in their systems (it's how they show you "On This Day" memories and location-based highlights), but they export it as a separate JSON or HTML file rather than re-embedding it into the media. The result: your photos lose their timeline, your videos lose their location, and importing them into any other photo library dumps everything into a single undated mess.

This creates **artificial switching costs**. Your 10 years of memories are technically "portable," but practically useless outside the platform. You can leave, but your photo library can't come with you intact. It's data portability in letter but not in spirit.

Snapchat is not unique here. Google Photos, Instagram, Facebook, and TikTok all do variations of the same thing. The [Data Transfer Initiative](https://dtinit.org/) exists partly because of how consistently platforms make this difficult.

### What this tool does

This tool **reunifies** the separated metadata with your actual media files:

1. **Parses** Snapchat's `memories_history.json` to extract original timestamps and GPS coordinates
2. **Matches** each media file to its metadata entry using date-based correlation
3. **Injects** the recovered timestamps and GPS coordinates back into the files as proper EXIF/QuickTime metadata using `exiftool`
4. **Deduplicates** by content hash (Snapchat exports often contain 40-50% duplicate files across chat backups and memories)
5. **Separates** Snapchat overlay PNGs from actual photos
6. **Organizes** everything into a clean `YYYY/MM/` folder structure

The output is a folder of media files with proper embedded metadata that any photo library—Google Photos, Apple Photos, Immich, whatever—will correctly sort by date and display on a map.

## Requirements

- **Python 3.8+**
- **[exiftool](https://exiftool.org)** — the industry-standard metadata tool
  ```bash
  # macOS
  brew install exiftool

  # Ubuntu / Debian
  sudo apt install libimage-exiftool-perl

  # Windows (Chocolatey)
  choco install exiftool
  ```

## Getting Your Snapchat Export

1. Open Snapchat → Settings → My Data (or go to [accounts.snapchat.com](https://accounts.snapchat.com))
2. Request a full data export
3. Wait for the email (can take hours to days depending on account size)
4. Download and extract all zip parts into a single folder

## Usage

```bash
# Basic — processes export and creates output alongside it
python3 process.py /path/to/extracted/snapchat-export

# Custom output directory
python3 process.py /path/to/extracted/snapchat-export --output ~/Pictures/Snapchat

# Different timezone (default is EDT / -04:00)
python3 process.py /path/to/extracted/snapchat-export --tz-offset "+01:00"
```

### Output Structure

```
google-photos-ready/
├── 2019/
│   ├── 01/
│   │   ├── 20190115_231731_Snap_00042.jpg   ← EXIF date: 2019:01:15 23:17:31
│   │   └── 20190118_140522_Snap_00043.mp4   ← QuickTime date injected
│   ├── 02/
│   └── ...
├── 2020/
├── ...
└── undated/          ← files with no parseable date in filename

overlays-only/        ← Snapchat filter/lens overlay PNGs (separated)
├── 2019/
└── ...

logs/
├── summary.txt       ← processing statistics
├── duplicates.log    ← all duplicate files found
├── failed_files.log  ← any EXIF injection failures
├── unmatched_files.log  ← files with no date in filename
└── oversized_files.log  ← files exceeding Google Photos limits
```

### Uploading to Google Photos

Once processed, upload the `google-photos-ready/` folder using [Google Photos](https://photos.google.com) (drag and drop or use the desktop uploader). Photos will appear in the correct position on your timeline because the dates are now embedded in the files themselves.

Works with any photo library that reads EXIF: Apple Photos, Immich, Synology Photos, Amazon Photos, etc.

## How It Works

### Metadata Recovery

Snapchat exports include a `memories_history.json` file containing timestamps and GPS coordinates for saved memories. The tool correlates these entries with media files by matching on date and media type (image vs. video), then injects the metadata using `exiftool`:

- **Photos:** `DateTimeOriginal`, `CreateDate`, `OffsetTimeOriginal`, `GPSLatitude/Longitude`
- **Videos:** `QuickTime:CreateDate`, `QuickTime:TrackCreateDate`
- **All files:** `FileModifyDate` set to match

For files without a JSON match (chat media, etc.), the date is extracted from the filename (Snapchat uses `YYYY-MM-DD_` prefixes) and set to noon on that day.

### Deduplication

Snapchat exports frequently contain the same file multiple times across different export sections (memories, chat media, camera roll backups). The tool hashes every file with MD5 and keeps only unique content. In testing, this typically removes **40-50%** of files.

### Overlay Separation

Snapchat saves filter/lens overlays as separate PNG files alongside the base photo. These are identified by `-overlay` or `overlay~` in the filename and moved to a separate directory so they don't clutter your photo library.

## Limitations

- **JSON coverage is partial.** The `memories_history.json` only contains metadata for "Saved Media" (Memories). Chat-sent photos, stories, and spotlight content have filenames with dates but no precise timestamps or GPS in the export.
- **Timezone is assumed.** Snapchat exports timestamps in UTC. The tool applies a fixed offset (default `-04:00` / EDT). Adjust with `--tz-offset` if you were primarily in a different timezone.
- **Overlay pairing is not attempted.** Overlays are separated but not matched to their base photos. The base photos stand alone fine; overlays are preserved separately if you want them.

## License

MIT
