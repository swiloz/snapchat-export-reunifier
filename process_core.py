#!/usr/bin/env python3
"""Snapchat Export Reunifier — robust My Data processor.

Features:
- Restores timestamp + GPS metadata from memories_history.json.
- Uses UUID links when Snapchat still exports them.
- For newer exports with blank links, uses safe date/type matching and filesystem
  timestamps to avoid silently assigning another Snap's time/GPS.
- Optionally merges Snapchat overlays into photos/videos.
- Produces an auditable CSV report with match method/confidence.

The input export is never modified.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None


MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".mp4", ".mov", ".webp", ".gif"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}
UUID_RE = re.compile(
    r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
MAX_PHOTO_BYTES = 200 * 1024 * 1024
MAX_VIDEO_BYTES = 10 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class MetadataEntry:
    source_index: int
    dt_utc: datetime
    media_type: str
    lat: Optional[float]
    lon: Optional[float]
    uuid: Optional[str]

    @property
    def day(self) -> str:
        return self.dt_utc.strftime("%Y-%m-%d")


@dataclass
class MatchResult:
    file: Path
    entry: Optional[MetadataEntry]
    method: str
    confidence: str
    delta_seconds: Optional[float] = None
    note: str = ""


def find_json_file(export_dir: Path) -> Optional[Path]:
    candidates = [
        export_dir / "json" / "memories_history.json",
        export_dir / "Snapchat Data" / "json" / "memories_history.json",
        export_dir / "Latest Offload" / "Snapchat Data" / "json" / "memories_history.json",
        export_dir / "memories_history.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return next(export_dir.rglob("memories_history.json"), None)


def parse_json_date(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def parse_location(value: str) -> Tuple[Optional[float], Optional[float]]:
    if not value or "Latitude, Longitude:" not in value:
        return None, None
    try:
        body = value.split(":", 1)[1]
        lat_s, lon_s = body.split(",", 1)
        return float(lat_s.strip()), float(lon_s.strip())
    except (ValueError, IndexError):
        return None, None


def extract_uuid(value: str) -> Optional[str]:
    if not value:
        return None
    match = UUID_RE.search(unquote(value))
    return match.group(1).lower() if match else None


def filename_uuid(path: Path) -> Optional[str]:
    return extract_uuid(path.name)


def filename_day(path: Path) -> Optional[str]:
    match = DATE_RE.match(path.name)
    if not match:
        return None
    value = "-".join(match.groups())
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def media_type_for(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "Image"
    if ext in VIDEO_EXTENSIONS:
        return "Video"
    return None


def load_metadata(json_file: Path) -> Tuple[List[MetadataEntry], Dict[str, List[MetadataEntry]], Dict[Tuple[str, str], List[MetadataEntry]]]:
    with json_file.open("r", encoding="utf-8-sig") as handle:
        data = json.load(handle)

    entries: List[MetadataEntry] = []
    by_uuid: Dict[str, List[MetadataEntry]] = defaultdict(list)
    by_group: Dict[Tuple[str, str], List[MetadataEntry]] = defaultdict(list)

    for index, raw in enumerate(data.get("Saved Media", [])):
        dt = parse_json_date(raw.get("Date", ""))
        if dt is None:
            continue
        mtype = "Image" if raw.get("Media Type") == "Image" else "Video"
        lat, lon = parse_location(raw.get("Location", ""))
        uid = extract_uuid(raw.get("Download Link", "")) or extract_uuid(raw.get("Media Download Url", ""))
        entry = MetadataEntry(index, dt, mtype, lat, lon, uid)
        entries.append(entry)
        by_group[(entry.day, entry.media_type)].append(entry)
        if uid:
            by_uuid[uid].append(entry)

    for group_entries in by_group.values():
        group_entries.sort(key=lambda item: item.dt_utc)

    return entries, by_uuid, by_group


def is_overlay(path: Path) -> bool:
    stem = path.stem.lower()
    return "-overlay" in stem or "overlay~" in stem or stem.endswith("_overlay")


def overlay_key(path: Path) -> str:
    stem = path.stem.lower()
    replacements = (
        ("-overlay", ""), ("-main", ""), ("overlay~", ""), ("main~", ""),
        ("_overlay", ""), ("_main", ""),
    )
    for old, new in replacements:
        stem = stem.replace(old, new)
    return stem.rstrip("-_~")


def collect_media_files(export_dir: Path, excluded: Sequence[Path] = ()) -> List[Path]:
    excluded_resolved = [p.resolve() for p in excluded]
    result: List[Path] = []
    for root, dirs, names in os.walk(export_dir):
        root_path = Path(root).resolve()
        dirs[:] = [
            name for name in dirs
            if not any((root_path / name).resolve() == ex or ex in (root_path / name).resolve().parents for ex in excluded_resolved)
        ]
        for name in names:
            path = Path(root) / name
            if path.suffix.lower() in MEDIA_EXTENSIONS:
                result.append(path)
    return sorted(result, key=lambda p: str(p).lower())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deduplicate(files: Sequence[Path], mode: str, duplicate_log) -> Tuple[List[Path], int]:
    if mode == "none":
        return list(files), 0

    seen: Dict[Tuple[str, ...], Path] = {}
    unique: List[Path] = []
    duplicates = 0
    for index, path in enumerate(files, start=1):
        if index % 1000 == 0:
            print(f"  ... hashing {index} / {len(files)}")
        try:
            digest = sha256_file(path)
            if mode == "within-day":
                key = (filename_day(path) or "", media_type_for(path) or "", digest)
            else:
                key = (digest,)
            if key in seen:
                duplicates += 1
                duplicate_log.write(f"DUP: {path} (matches {seen[key]})\n")
            else:
                seen[key] = path
                unique.append(path)
        except OSError as exc:
            duplicate_log.write(f"HASH_FAIL: {path} ({exc})\n")
            unique.append(path)
    return unique, duplicates


def _usable_file_clock(path: Path) -> Optional[datetime]:
    """Return a likely original local wall-clock timestamp, or None.

    Windows creation time is useful for current Snapchat ZIP exports. On Unix,
    birthtime is preferred when available; mtime is used as a secondary signal.
    A timestamp is accepted only when its calendar date is near the date encoded
    in the Snapchat filename, which rejects ordinary extraction-time timestamps.
    """
    day = filename_day(path)
    if not day:
        return None
    expected = datetime.strptime(day, "%Y-%m-%d").date()
    try:
        stat = path.stat()
    except OSError:
        return None

    candidates: List[float] = []
    birth = getattr(stat, "st_birthtime", None)
    if birth:
        candidates.append(birth)
    if os.name == "nt":
        candidates.append(stat.st_ctime)
    candidates.append(stat.st_mtime)

    seen: set[int] = set()
    for stamp in candidates:
        rounded = int(stamp)
        if rounded in seen:
            continue
        seen.add(rounded)
        dt = datetime.fromtimestamp(stamp)
        if abs((dt.date() - expected).days) <= 1:
            return dt.replace(tzinfo=None, microsecond=0)
    return None


def _delta_minutes(entry: MetadataEntry, file_clock: datetime) -> Optional[int]:
    naive_utc = entry.dt_utc.replace(tzinfo=None)
    delta = naive_utc - file_clock
    minutes = int(round(delta.total_seconds() / 60.0))
    if abs(minutes) > 14 * 60:
        return None
    return minutes


def _best_offset_from_samples(samples: Sequence[int]) -> Optional[int]:
    if not samples:
        return None
    quantized = [int(round(value / 15.0) * 15) for value in samples]
    counts = Counter(quantized)
    value, count = counts.most_common(1)[0]
    if count >= 2 or len(samples) == 1:
        return value
    return int(round(statistics.median(quantized)))


def _greedy_time_pairs(
    files: Sequence[Path],
    entries: Sequence[MetadataEntry],
    offset_minutes: int,
) -> List[Tuple[Path, MetadataEntry, float]]:
    edges: List[Tuple[float, Path, MetadataEntry]] = []
    for path in files:
        clock = _usable_file_clock(path)
        if clock is None:
            continue
        adjusted = clock + timedelta(minutes=offset_minutes)
        for entry in entries:
            delta = abs((entry.dt_utc.replace(tzinfo=None) - adjusted).total_seconds())
            edges.append((delta, path, entry))
    edges.sort(key=lambda item: item[0])

    used_files: set[Path] = set()
    used_entries: set[int] = set()
    pairs: List[Tuple[Path, MetadataEntry, float]] = []
    for delta, path, entry in edges:
        if path in used_files or entry.source_index in used_entries:
            continue
        used_files.add(path)
        used_entries.add(entry.source_index)
        pairs.append((path, entry, delta))
    return pairs


def _infer_group_offset(files: Sequence[Path], entries: Sequence[MetadataEntry]) -> Optional[int]:
    clocks = [(path, _usable_file_clock(path)) for path in files]
    clocks = [(path, clock) for path, clock in clocks if clock is not None]
    if not clocks or not entries:
        return None

    candidates: set[int] = set()
    for _, clock in clocks:
        for entry in entries:
            delta = _delta_minutes(entry, clock)
            if delta is not None:
                candidates.add(int(round(delta / 15.0) * 15))

    best: Optional[Tuple[int, float, int]] = None
    for offset in candidates:
        pairs = _greedy_time_pairs([p for p, _ in clocks], entries, offset)
        good = sum(1 for _, _, delta in pairs if delta <= 300)
        total = sum(min(delta, 86400.0) for _, _, delta in pairs)
        score = (-good, total, abs(offset))
        if best is None or score < (best[0], best[1], abs(best[2])):
            best = (-good, total, offset)
    if best is None or -best[0] == 0:
        return None
    return best[2]


def match_media(
    files: Sequence[Path],
    by_uuid: Dict[str, List[MetadataEntry]],
    by_group: Dict[Tuple[str, str], List[MetadataEntry]],
    tolerance_seconds: int = 300,
    unsafe_index_fallback: bool = False,
) -> List[MatchResult]:
    """Match files to JSON entries without silently trusting directory order."""
    results: Dict[Path, MatchResult] = {}
    used_entries: set[int] = set()

    for path in files:
        uid = filename_uuid(path)
        mtype = media_type_for(path)
        if not uid or uid not in by_uuid:
            continue
        candidates = [e for e in by_uuid[uid] if e.source_index not in used_entries and e.media_type == mtype]
        if len(candidates) == 1:
            entry = candidates[0]
            results[path] = MatchResult(path, entry, "uuid", "high")
            used_entries.add(entry.source_index)

    file_groups: Dict[Tuple[str, str], List[Path]] = defaultdict(list)
    for path in files:
        if path in results:
            continue
        day = filename_day(path)
        mtype = media_type_for(path)
        if day and mtype:
            file_groups[(day, mtype)].append(path)
        else:
            results[path] = MatchResult(path, None, "none", "none", note="invalid filename date/type")

    offset_samples_by_month: Dict[str, List[int]] = defaultdict(list)
    global_samples: List[int] = []
    deferred: Dict[Tuple[str, str], Tuple[List[Path], List[MetadataEntry]]] = {}

    for key, group_files in file_groups.items():
        available_entries = [e for e in by_group.get(key, []) if e.source_index not in used_entries]
        group_files = sorted(group_files, key=lambda p: p.name.lower())
        if len(group_files) == 1 and len(available_entries) == 1:
            path, entry = group_files[0], available_entries[0]
            results[path] = MatchResult(path, entry, "unique-day-type", "high")
            used_entries.add(entry.source_index)
            clock = _usable_file_clock(path)
            if clock is not None:
                sample = _delta_minutes(entry, clock)
                if sample is not None:
                    month = key[0][:7]
                    offset_samples_by_month[month].append(sample)
                    global_samples.append(sample)
        else:
            deferred[key] = (group_files, available_entries)

    month_offsets = {month: _best_offset_from_samples(samples) for month, samples in offset_samples_by_month.items()}
    global_offset = _best_offset_from_samples(global_samples)

    for key, (group_files, available_entries) in deferred.items():
        if not available_entries:
            for path in group_files:
                results[path] = MatchResult(path, None, "none", "none", note="no JSON entries for day/type")
            continue

        month = key[0][:7]
        offset = month_offsets.get(month)
        if offset is None:
            offset = global_offset
        if offset is None:
            offset = _infer_group_offset(group_files, available_entries)

        matched_files: set[Path] = set()
        matched_entries: set[int] = set()
        if offset is not None:
            for path, entry, delta in _greedy_time_pairs(group_files, available_entries, offset):
                if delta <= tolerance_seconds:
                    confidence = "high" if delta <= 5 else "medium"
                    results[path] = MatchResult(
                        path, entry, "filesystem-time", confidence, delta_seconds=delta,
                        note=f"clock offset {offset:+d} min",
                    )
                    matched_files.add(path)
                    matched_entries.add(entry.source_index)
                    used_entries.add(entry.source_index)

        remaining_files = [p for p in group_files if p not in matched_files]
        remaining_entries = [e for e in available_entries if e.source_index not in matched_entries]

        if unsafe_index_fallback and len(remaining_files) == len(remaining_entries):
            remaining_files.sort(key=lambda p: p.name.lower())
            remaining_entries.sort(key=lambda e: e.dt_utc)
            for path, entry in zip(remaining_files, remaining_entries):
                results[path] = MatchResult(path, entry, "index-fallback", "low", note="unsafe ordered fallback")
                used_entries.add(entry.source_index)
        else:
            for path in remaining_files:
                note = f"ambiguous group: {len(group_files)} files / {len(available_entries)} JSON entries"
                results[path] = MatchResult(path, None, "ambiguous", "none", note=note)

    return [results[path] for path in files]


def parse_fixed_offset(value: str) -> timezone:
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", value)
    if not match:
        raise ValueError("offset must look like +02:00 or -04:00")
    sign = 1 if match.group(1) == "+" else -1
    delta = timedelta(hours=int(match.group(2)), minutes=int(match.group(3))) * sign
    return timezone(delta)


def resolve_output_timezone(timezone_name: Optional[str], tz_offset: Optional[str]):
    if timezone_name:
        if ZoneInfo is None:
            raise RuntimeError("--timezone requires Python 3.9+")
        try:
            return ZoneInfo(timezone_name)
        except Exception as exc:
            raise RuntimeError(f"unknown timezone '{timezone_name}': {exc}")
    if tz_offset:
        return parse_fixed_offset(tz_offset)
    return timezone.utc


def exif_datetime(dt: datetime) -> str:
    return dt.strftime("%Y:%m:%d %H:%M:%S")


def offset_string(dt: datetime) -> str:
    raw = dt.strftime("%z")
    return f"{raw[:3]}:{raw[3:]}" if raw else "+00:00"


def default_datetime_for(path: Path, target_tz) -> datetime:
    day = filename_day(path)
    if day:
        local = datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=target_tz)
        return local
    return datetime.now(target_tz).replace(microsecond=0)


def exiftool_write(
    path: Path,
    media_type: str,
    entry: Optional[MetadataEntry],
    target_tz,
    exiftool: str,
) -> Tuple[bool, str, datetime]:
    if entry:
        utc_dt = entry.dt_utc
        local_dt = utc_dt.astimezone(target_tz)
        lat, lon = entry.lat, entry.lon
    else:
        local_dt = default_datetime_for(path, target_tz)
        utc_dt = local_dt.astimezone(timezone.utc)
        lat = lon = None

    args = [exiftool, "-overwrite_original", "-q"]
    if media_type == "Image":
        local_value = exif_datetime(local_dt)
        args += [
            f"-DateTimeOriginal={local_value}",
            f"-CreateDate={local_value}",
            f"-ModifyDate={local_value}",
            f"-OffsetTimeOriginal={offset_string(local_dt)}",
            f"-OffsetTimeDigitized={offset_string(local_dt)}",
            f"-FileModifyDate={local_value}{offset_string(local_dt)}",
        ]
        if lat is not None and lon is not None:
            args += [
                f"-GPSLatitude={abs(lat)}",
                f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
                f"-GPSLongitude={abs(lon)}",
                f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}",
            ]
    else:
        utc_value = exif_datetime(utc_dt)
        local_iso = local_dt.isoformat(timespec="seconds")
        args += [
            f"-QuickTime:CreateDate={utc_value}",
            f"-QuickTime:ModifyDate={utc_value}",
            f"-TrackCreateDate={utc_value}",
            f"-TrackModifyDate={utc_value}",
            f"-MediaCreateDate={utc_value}",
            f"-MediaModifyDate={utc_value}",
            f"-Keys:CreationDate={local_iso}",
            f"-FileModifyDate={exif_datetime(local_dt)}{offset_string(local_dt)}",
        ]
        if lat is not None and lon is not None:
            args.append(f"-Keys:GPSCoordinates={lat:.8f} {lon:.8f}")

    args.append(str(path))
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return False, str(exc), local_dt
    if completed.returncode != 0:
        return False, completed.stderr.strip() or completed.stdout.strip(), local_dt
    return True, completed.stderr.strip(), local_dt


def merge_image_overlay(main: Path, overlay: Path, destination: Path) -> Tuple[bool, str]:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return False, "Pillow is not installed (pip install Pillow)"
    try:
        with Image.open(main) as base_image, Image.open(overlay) as overlay_image:
            base = ImageOps.exif_transpose(base_image).convert("RGBA")
            ov = overlay_image.convert("RGBA")
            if ov.size != base.size:
                ov = ov.resize(base.size, Image.Resampling.LANCZOS)
            merged = Image.alpha_composite(base, ov)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.suffix.lower() in {".jpg", ".jpeg"}:
                merged.convert("RGB").save(destination, quality=95, subsampling=0, optimize=True)
            else:
                merged.save(destination)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def merge_video_overlay(main: Path, overlay: Path, destination: Path, ffmpeg: str) -> Tuple[bool, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    filter_graph = "[1:v][0:v]scale2ref[ov][base];[base][ov]overlay=0:0:format=auto[v]"
    command = [
        ffmpeg, "-y", "-loglevel", "error", "-i", str(main), "-i", str(overlay),
        "-filter_complex", filter_graph,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-c:a", "copy", "-movflags", "+faststart", str(destination),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            return True, ""
        command[command.index("copy")] = "aac"
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            return True, ""
        return False, result.stderr.strip()
    except Exception as exc:
        return False, str(exc)


def render_media(
    source: Path,
    overlay: Optional[Path],
    destination: Path,
    overlay_mode: str,
    ffmpeg: str,
) -> Tuple[bool, str, bool]:
    if overlay_mode == "merge" and overlay is not None:
        if media_type_for(source) == "Image":
            ok, msg = merge_image_overlay(source, overlay, destination)
        else:
            ok, msg = merge_video_overlay(source, overlay, destination, ffmpeg)
        if ok:
            return True, "", True
        shutil.copy2(source, destination)
        return True, f"overlay merge failed; main copied instead: {msg}", False

    shutil.copy2(source, destination)
    return True, "", False


def destination_name(path: Path, local_dt: datetime, counter: int) -> str:
    uid = filename_uuid(path)
    token = uid if uid else f"{counter:05d}"
    return f"{local_dt.strftime('%Y%m%d_%H%M%S')}_Snap_{token}{path.suffix.lower()}"


def write_match_report(path: Path, matches: Sequence[MatchResult]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "file", "method", "confidence", "delta_seconds", "json_timestamp_utc",
            "latitude", "longitude", "json_uuid", "note",
        ])
        for match in matches:
            entry = match.entry
            writer.writerow([
                str(match.file), match.method, match.confidence,
                "" if match.delta_seconds is None else f"{match.delta_seconds:.3f}",
                "" if entry is None else entry.dt_utc.isoformat(),
                "" if entry is None or entry.lat is None else entry.lat,
                "" if entry is None or entry.lon is None else entry.lon,
                "" if entry is None or entry.uuid is None else entry.uuid,
                match.note,
            ])


def process(args) -> int:
    export_dir = Path(args.export_dir).resolve()
    if not export_dir.exists():
        print(f"Error: export directory not found: {export_dir}")
        return 1

    base_dir = export_dir.parent
    output_dir = Path(args.output).resolve() if args.output else base_dir / "photos-ready"
    overlay_dir = base_dir / "overlays-only"
    log_dir = base_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        if args.overlay_mode != "ignore":
            overlay_dir.mkdir(parents=True, exist_ok=True)

    if not args.dry_run and shutil.which(args.exiftool) is None and not Path(args.exiftool).exists():
        print(f"Error: exiftool not found: {args.exiftool}")
        return 1

    try:
        target_tz = resolve_output_timezone(args.timezone, args.tz_offset)
    except Exception as exc:
        print(f"Error: {exc}")
        return 1

    print("=" * 68)
    print("  Snapchat Export Reunifier — robust My Data mode")
    print("=" * 68)
    print(f"  Export:          {export_dir}")
    print(f"  Output:          {output_dir}")
    print(f"  Overlay mode:    {args.overlay_mode}")
    print(f"  Dedupe:          {args.dedupe}")
    print(f"  Timezone:        {args.timezone or args.tz_offset or 'UTC'}")
    print(f"  Dry run:         {args.dry_run}")

    json_file = find_json_file(export_dir)
    if json_file:
        entries, by_uuid, by_group = load_metadata(json_file)
        print(f"\n[1/6] Metadata: {len(entries)} JSON entries ({sum(len(v) for v in by_uuid.values())} with UUID links)")
    else:
        entries, by_uuid, by_group = [], {}, {}
        print("\n[1/6] No memories_history.json found — date-only fallback")

    all_files = collect_media_files(export_dir, excluded=(output_dir, overlay_dir, log_dir))
    print(f"[2/6] Found {len(all_files)} media files")
    if not all_files:
        return 1

    with (log_dir / "duplicates.log").open("w", encoding="utf-8") as duplicate_log:
        unique_files, duplicate_count = deduplicate(all_files, args.dedupe, duplicate_log)
    print(f"[3/6] {len(unique_files)} files after dedupe ({duplicate_count} skipped)")

    overlays = [path for path in unique_files if is_overlay(path)]
    mains = [path for path in unique_files if not is_overlay(path)]
    overlay_map: Dict[str, Path] = {}
    for overlay in overlays:
        overlay_map.setdefault(overlay_key(overlay), overlay)
    print(f"[4/6] {len(mains)} main media / {len(overlays)} overlays")

    matches = match_media(
        mains, by_uuid, by_group,
        tolerance_seconds=args.match_tolerance_seconds,
        unsafe_index_fallback=args.unsafe_index_fallback,
    )
    write_match_report(log_dir / "matches.csv", matches)
    method_counts = Counter(match.method for match in matches)
    print("[5/6] Matching: " + ", ".join(f"{key}={value}" for key, value in sorted(method_counts.items())))

    if args.dry_run:
        print(f"\nDry run complete. Review: {log_dir / 'matches.csv'}")
        return 0

    failed_log = (log_dir / "failed_files.log").open("w", encoding="utf-8")
    warnings_log = (log_dir / "warnings.log").open("w", encoding="utf-8")
    stats = Counter()

    if args.overlay_mode != "ignore":
        for overlay in overlays:
            day = filename_day(overlay)
            dest = overlay_dir / (day[:4] if day else "undated") / (day[5:7] if day else "")
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(overlay, dest / overlay.name)

    print(f"[6/6] Rendering and writing metadata for {len(matches)} files...")
    for counter, match in enumerate(matches, start=1):
        source = match.file
        mtype = media_type_for(source)
        if mtype is None:
            continue
        if match.entry:
            local_dt = match.entry.dt_utc.astimezone(target_tz)
        else:
            local_dt = default_datetime_for(source, target_tz)

        year, month = local_dt.strftime("%Y"), local_dt.strftime("%m")
        dest_dir = output_dir / year / month
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / destination_name(source, local_dt, counter)
        overlay = overlay_map.get(overlay_key(source))

        ok, render_message, merged = render_media(source, overlay, dest, args.overlay_mode, args.ffmpeg)
        if not ok:
            failed_log.write(f"RENDER_FAIL: {source}: {render_message}\n")
            stats["render_fail"] += 1
            continue
        if render_message:
            warnings_log.write(f"{source}: {render_message}\n")
        if merged:
            stats["overlay_merged"] += 1

        ok, message, _ = exiftool_write(dest, mtype, match.entry, target_tz, args.exiftool)
        if not ok:
            failed_log.write(f"METADATA_FAIL: {dest}: {message}\n")
            stats["metadata_fail"] += 1
        else:
            stats["processed"] += 1
            if match.entry and match.entry.lat is not None and match.entry.lon is not None:
                stats["gps"] += 1

        size = dest.stat().st_size
        limit = MAX_PHOTO_BYTES if mtype == "Image" else MAX_VIDEO_BYTES
        if size > limit:
            warnings_log.write(f"OVERSIZED: {dest} ({size} bytes)\n")
            stats["oversized"] += 1

        if counter % 250 == 0:
            print(f"  ... {counter} / {len(matches)}")

    failed_log.close()
    warnings_log.close()

    summary = [
        "Snapchat Export Reunifier — summary",
        "=" * 48,
        f"Media found:             {len(all_files)}",
        f"Duplicates skipped:      {duplicate_count}",
        f"Main media:              {len(mains)}",
        f"Overlays found:          {len(overlays)}",
        f"Overlays merged:         {stats['overlay_merged']}",
        f"Processed:               {stats['processed']}",
        f"GPS written:             {stats['gps']}",
        f"Metadata failures:       {stats['metadata_fail']}",
        f"Render failures:         {stats['render_fail']}",
        f"Ambiguous/unmatched:     {sum(1 for m in matches if m.entry is None)}",
        f"Unsafe index matches:    {sum(1 for m in matches if m.method == 'index-fallback')}",
        f"Report:                  {log_dir / 'matches.csv'}",
    ]
    (log_dir / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n" + "\n".join(summary))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Restore Snapchat My Data media metadata safely.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Inspect matches first (recommended)
  python process.py C:\\Snapchat\\MyData --dry-run --timezone Europe/Paris

  # Process and merge overlays
  python process.py C:\\Snapchat\\MyData --timezone Europe/Paris --overlay-mode merge

  # Reproduce the old risky index behavior only when you explicitly want it
  python process.py C:\\Snapchat\\MyData --unsafe-index-fallback
""",
    )
    parser.add_argument("export_dir", help="Extracted Snapchat My Data directory")
    parser.add_argument("--output", "-o", help="Output directory (default: sibling photos-ready)")
    parser.add_argument("--timezone", help="IANA timezone for photo wall-clock time, e.g. Europe/Paris")
    parser.add_argument("--tz-offset", help="Fixed offset fallback, e.g. +02:00")
    parser.add_argument("--overlay-mode", choices=("merge", "separate", "ignore"), default="merge")
    parser.add_argument("--dedupe", choices=("none", "within-day", "global"), default="within-day")
    parser.add_argument("--dry-run", action="store_true", help="Only analyze matches and write reports")
    parser.add_argument("--match-tolerance-seconds", type=int, default=300)
    parser.add_argument(
        "--unsafe-index-fallback", action="store_true",
        help="Pair remaining same-day files by index (old behavior; may assign wrong time/GPS)",
    )
    parser.add_argument("--exiftool", default="exiftool", help="ExifTool executable/path")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable/path")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(process(args))


if __name__ == "__main__":
    main()
