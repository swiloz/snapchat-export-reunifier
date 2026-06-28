#!/usr/bin/env python3
"""
Snapchat Export Reunifier

Reunifies stripped metadata (timestamps, GPS coordinates) with exported media
files from a Snapchat data export. Deduplicates, injects EXIF data, and
organizes into a YYYY/MM/ folder structure ready for Google Photos or any
photo library that reads EXIF.

Requirements:
  - Python 3.8+
  - exiftool (https://exiftool.org)
    macOS:  brew install exiftool
    Linux:  sudo apt install libimage-exiftool-perl

Usage:
  python3 process.py /path/to/extracted/snapchat-export [--output /path/to/output]
"""

import argparse
import json
import os
import sys
import hashlib
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path


# ============================================================
# Constants
# ============================================================
MEDIA_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.mp4', '.webp', '.gif'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
VIDEO_EXTENSIONS = {'.mp4'}

# Google Photos upload limits
MAX_PHOTO_BYTES = 200 * 1024 * 1024       # 200 MB
MAX_VIDEO_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB


def find_json_file(export_dir: Path) -> Path | None:
    """Locate the memories_history.json metadata file in common Snapchat export structures."""
    candidates = [
        export_dir / "json" / "memories_history.json",
        export_dir / "Snapchat Data" / "json" / "memories_history.json",
        export_dir / "Latest Offload" / "Snapchat Data" / "json" / "memories_history.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    # Fallback: recursive search
    for p in export_dir.rglob("memories_history.json"):
        return p
    return None


def build_json_lookup(json_file: Path) -> dict:
    """Parse memories_history.json into a lookup table keyed by date|type|index."""
    lookup = {}
    try:
        with open(json_file) as f:
            data = json.load(f)

        entries = data.get("Saved Media", [])
        groups = defaultdict(list)

        for e in entries:
            date_str = e.get("Date", "")
            if not date_str:
                continue
            day = date_str[:10]
            mtype = "Image" if e.get("Media Type") == "Image" else "Video"

            lat, lon = 0.0, 0.0
            loc = e.get("Location", "")
            if "Latitude, Longitude:" in loc:
                parts = loc.split(":", 1)[1].strip().split(",")
                if len(parts) == 2:
                    try:
                        lat = float(parts[0].strip())
                        lon = float(parts[1].strip())
                    except ValueError:
                        pass

            groups[(day, mtype)].append((date_str, lat, lon))

        for key in groups:
            groups[key].sort(key=lambda x: x[0])

        for (day, mtype), items in groups.items():
            for idx, (ts, lat, lon) in enumerate(items):
                lookup[f"{day}|{mtype}|{idx}"] = {
                    "ts": ts.replace(" UTC", ""),
                    "lat": lat,
                    "lon": lon,
                }

        print(f"  → Parsed {len(lookup)} JSON metadata entries")
    except FileNotFoundError:
        print("  → No memories_history.json found — continuing with filename dates only")
    except Exception as e:
        print(f"  → WARNING: JSON parse failed ({e}), continuing with filename dates only")

    return lookup


def collect_media_files(export_dir: Path) -> list[Path]:
    """Walk the export directory and collect all media files."""
    files = []
    for root, _, names in os.walk(export_dir):
        for name in names:
            if os.path.splitext(name)[1].lower() in MEDIA_EXTENSIONS:
                files.append(Path(root) / name)
    return files


def deduplicate(files: list[Path], duplicate_log) -> list[Path]:
    """Deduplicate files by MD5 content hash."""
    seen = {}
    unique = []
    dupes = 0

    for i, filepath in enumerate(files):
        if (i + 1) % 2000 == 0:
            print(f"  ... hashing {i + 1} / {len(files)}")
        try:
            h = hashlib.md5()
            with open(filepath, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    h.update(chunk)
            digest = h.hexdigest()

            if digest in seen:
                dupes += 1
                duplicate_log.write(f"DUP: {filepath} (matches {seen[digest]})\n")
            else:
                seen[digest] = str(filepath)
                unique.append(filepath)
        except Exception as e:
            duplicate_log.write(f"HASH_FAIL: {filepath} ({e})\n")

    return unique, dupes


def separate_overlays(files: list[Path]) -> tuple[list[Path], list[Path]]:
    """Separate Snapchat overlay PNGs from main media."""
    main, overlays = [], []
    for f in files:
        name = f.name
        if "-overlay" in name or "overlay~" in name:
            overlays.append(f)
        else:
            main.append(f)
    return main, overlays


def inject_exif_and_organize(
    files: list[Path],
    json_lookup: dict,
    output_dir: Path,
    log_dir: Path,
    tz_offset: str = "-04:00",
) -> dict:
    """Inject EXIF timestamps + GPS, organize into YYYY/MM/ structure."""
    stats = {
        'photos': 0, 'videos': 0, 'json_matched': 0,
        'gps_injected': 0, 'exif_failures': 0, 'no_date': 0, 'oversized': 0,
    }

    failed_log = open(log_dir / "failed_files.log", "w")
    unmatched_log = open(log_dir / "unmatched_files.log", "w")
    oversized_log = open(log_dir / "oversized_files.log", "w")

    day_counters = defaultdict(lambda: {"Image": 0, "Video": 0})
    file_counter = 0

    for i, filepath in enumerate(files):
        if (i + 1) % 500 == 0:
            print(f"  ... processed {i + 1} / {len(files)}")

        file_counter += 1
        name = filepath.name
        ext_lower = filepath.suffix.lower()

        # Extract date from filename (Snapchat format: YYYY-MM-DD_...)
        file_date = name[:10]
        valid_date = True
        try:
            year, month, day = file_date.split("-")
            int(year); int(month); int(day)
            if len(year) != 4 or len(month) != 2 or len(day) != 2:
                valid_date = False
        except (ValueError, AttributeError):
            valid_date = False

        if not valid_date:
            unmatched_log.write(f"NO_DATE: {filepath}\n")
            stats['no_date'] += 1
            dest_dir = output_dir / "undated"
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(filepath, dest_dir / name)
            continue

        media_type = "Image" if ext_lower in IMAGE_EXTENSIONS else "Video"
        idx = day_counters[file_date][media_type]
        day_counters[file_date][media_type] += 1

        # JSON lookup for precise timestamp + GPS
        lookup_key = f"{file_date}|{media_type}|{idx}"
        json_entry = json_lookup.get(lookup_key)

        if json_entry:
            full_ts = json_entry["ts"]
            lat, lon = json_entry["lat"], json_entry["lon"]
            parts = full_ts.split(" ")
            exif_date = (
                parts[0].replace("-", ":") + " " + parts[1]
                if len(parts) >= 2
                else f"{year}:{month}:{day} 12:00:00"
            )
            stats['json_matched'] += 1
        else:
            exif_date = f"{year}:{month}:{day} 12:00:00"
            lat, lon = 0.0, 0.0

        # Destination
        dest_dir = output_dir / year / month
        dest_dir.mkdir(parents=True, exist_ok=True)

        clean_time = exif_date.replace(":", "").replace(" ", "_")
        dest_name = f"{clean_time}_Snap_{file_counter:05d}{ext_lower}"
        dest_file = dest_dir / dest_name

        shutil.copy2(filepath, dest_file)

        # Build exiftool command
        exif_args = ["exiftool", "-overwrite_original", "-q"]

        if media_type == "Image":
            exif_args += [
                f"-DateTimeOriginal={exif_date}",
                f"-CreateDate={exif_date}",
                f"-OffsetTimeOriginal={tz_offset}",
                f"-FileModifyDate={exif_date}",
            ]
        else:
            exif_args += [
                f"-QuickTime:CreateDate={exif_date}",
                f"-QuickTime:TrackCreateDate={exif_date}",
                f"-FileModifyDate={exif_date}",
            ]

        # GPS injection
        if lat != 0.0 and lon != 0.0:
            lat_ref = "N" if lat >= 0 else "S"
            lon_ref = "E" if lon >= 0 else "W"
            exif_args += [
                f"-GPSLatitude={abs(lat)}",
                f"-GPSLatitudeRef={lat_ref}",
                f"-GPSLongitude={abs(lon)}",
                f"-GPSLongitudeRef={lon_ref}",
            ]
            stats['gps_injected'] += 1

        exif_args.append(str(dest_file))

        try:
            result = subprocess.run(exif_args, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                stats['photos' if media_type == "Image" else 'videos'] += 1
            else:
                failed_log.write(
                    f"EXIF_FAIL (rc={result.returncode}): {dest_file} "
                    f"stderr={result.stderr.strip()}\n"
                )
                stats['exif_failures'] += 1
        except Exception as e:
            failed_log.write(f"EXIF_EXCEPTION: {dest_file} ({e})\n")
            stats['exif_failures'] += 1

        # Oversized check
        file_size = dest_file.stat().st_size
        limit = MAX_PHOTO_BYTES if media_type == "Image" else MAX_VIDEO_BYTES
        if file_size > limit:
            oversized_log.write(
                f"OVERSIZED ({file_size // (1024 * 1024)}MB): {dest_file}\n"
            )
            stats['oversized'] += 1

    failed_log.close()
    unmatched_log.close()
    oversized_log.close()

    return stats


def copy_overlays(overlays: list[Path], overlay_dir: Path):
    """Copy overlay files into their own directory, organized by date."""
    for filepath in overlays:
        name = filepath.name
        file_date = name[:10]
        try:
            year, month, _ = file_date.split("-")
            dest = overlay_dir / year / month
        except ValueError:
            dest = overlay_dir / "undated"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(filepath, dest / name)


def main():
    parser = argparse.ArgumentParser(
        description="Reunify Snapchat export metadata and organize for Google Photos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage — extracts alongside your export
  python3 process.py ~/Downloads/my_snapchat_export

  # Custom output directory
  python3 process.py ~/Downloads/my_snapchat_export --output ~/Pictures/Snapchat

  # Different timezone offset for EXIF
  python3 process.py ~/Downloads/my_snapchat_export --tz-offset "+01:00"
""",
    )
    parser.add_argument(
        "export_dir",
        help="Path to extracted Snapchat export directory",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output directory (default: <export_dir>/../google-photos-ready)",
    )
    parser.add_argument(
        "--tz-offset",
        default="-04:00",
        help="Timezone offset for EXIF OffsetTimeOriginal (default: -04:00 / EDT)",
    )

    args = parser.parse_args()
    export_dir = Path(args.export_dir).resolve()

    if not export_dir.exists():
        print(f"Error: Export directory not found: {export_dir}")
        sys.exit(1)

    base_dir = export_dir.parent
    output_dir = Path(args.output).resolve() if args.output else base_dir / "google-photos-ready"
    overlay_dir = base_dir / "overlays-only"
    log_dir = base_dir / "logs"

    for d in [output_dir, overlay_dir, log_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # Check for exiftool
    if shutil.which("exiftool") is None:
        print("Error: exiftool not found. Install it first:")
        print("  macOS:  brew install exiftool")
        print("  Linux:  sudo apt install libimage-exiftool-perl")
        sys.exit(1)

    print("=" * 60)
    print("  Snapchat Export Reunifier")
    print("=" * 60)
    print(f"  Export:   {export_dir}")
    print(f"  Output:   {output_dir}")
    print(f"  Overlays: {overlay_dir}")
    print(f"  Logs:     {log_dir}")
    print("=" * 60)

    # Phase 1: JSON metadata
    print("\n[Phase 1] Building JSON timestamp + GPS lookup table...")
    json_file = find_json_file(export_dir)
    json_lookup = build_json_lookup(json_file) if json_file else {}
    if not json_file:
        print("  → No memories_history.json found — using filename dates only")

    # Phase 2: Collect media
    print(f"\n[Phase 2] Collecting media files from {export_dir}...")
    all_files = collect_media_files(export_dir)
    total = len(all_files)
    print(f"  → Found {total} total media files")

    if total == 0:
        print("\nNo media files found. Check your export directory path.")
        sys.exit(1)

    # Phase 3: Deduplicate
    print(f"\n[Phase 3] Deduplicating by content hash...")
    dup_log = open(log_dir / "duplicates.log", "w")
    unique_files, dupes = deduplicate(all_files, dup_log)
    dup_log.close()
    print(f"  → {len(unique_files)} unique files, {dupes} duplicates skipped")

    # Phase 4: Separate overlays
    print(f"\n[Phase 4] Separating overlay PNGs from main media...")
    main_files, overlay_files = separate_overlays(unique_files)
    print(f"  → {len(main_files)} main media files")
    print(f"  → {len(overlay_files)} overlay files separated")

    # Phase 5: EXIF injection + organize
    print(f"\n[Phase 5] Injecting EXIF metadata and organizing {len(main_files)} files...")
    stats = inject_exif_and_organize(
        main_files, json_lookup, output_dir, log_dir, args.tz_offset
    )

    # Phase 6: Overlays
    print(f"\n[Phase 6] Copying {len(overlay_files)} overlays to separate directory...")
    copy_overlays(overlay_files, overlay_dir)

    # Summary
    print("\n" + "=" * 60)
    print("              PROCESSING COMPLETE")
    print("=" * 60)
    print(f"  Total media files found:     {total}")
    print(f"  Duplicates removed:          {dupes}")
    print(f"  Unique files:                {len(unique_files)}")
    print(f"  Overlays separated:          {len(overlay_files)}")
    print(f"  Main files processed:        {len(main_files)}")
    print(f"    → Photos with EXIF:        {stats['photos']}")
    print(f"    → Videos with metadata:    {stats['videos']}")
    print(f"    → JSON timestamp matches:  {stats['json_matched']}")
    print(f"    → GPS coordinates added:   {stats['gps_injected']}")
    print(f"    → EXIF write failures:     {stats['exif_failures']}")
    print(f"    → No date in filename:     {stats['no_date']}")
    print(f"    → Oversized for GPhotos:   {stats['oversized']}")
    print(f"\n  Output:   {output_dir}")
    print(f"  Overlays: {overlay_dir}")
    print(f"  Logs:     {log_dir}")
    print("=" * 60)

    # Write summary file
    with open(log_dir / "summary.txt", "w") as f:
        f.write(f"Snapchat Export Reunifier — Processing Summary\n")
        f.write(f"{'=' * 50}\n")
        f.write(f"Total media files:    {total}\n")
        f.write(f"Duplicates removed:   {dupes}\n")
        f.write(f"Unique files:         {len(unique_files)}\n")
        f.write(f"Overlays separated:   {len(overlay_files)}\n")
        f.write(f"Photos with EXIF:     {stats['photos']}\n")
        f.write(f"Videos with metadata: {stats['videos']}\n")
        f.write(f"JSON matches:         {stats['json_matched']}\n")
        f.write(f"GPS injected:         {stats['gps_injected']}\n")
        f.write(f"EXIF failures:        {stats['exif_failures']}\n")
        f.write(f"No date in filename:  {stats['no_date']}\n")
        f.write(f"Oversized:            {stats['oversized']}\n")

    print("\nDone!")


if __name__ == "__main__":
    main()
