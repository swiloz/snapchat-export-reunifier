from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}
UUID_RE = re.compile(
    r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


@dataclass
class MatchResult:
    file: Path
    entry: object | None
    method: str
    confidence: str
    delta_seconds: Optional[float] = None
    note: str = ""


def filename_uuid(path: Path) -> Optional[str]:
    match = UUID_RE.search(path.name)
    return match.group(1).lower() if match else None


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


def _filesystem_clocks(path: Path) -> List[Tuple[str, datetime]]:
    """Return plausible naive filesystem clocks for a Snapchat media file.

    Extraction/copy timestamps are rejected when their date is nowhere near the
    date encoded in the Snapchat filename.  We deliberately keep more than one
    clock: current Windows exports may preserve the useful timestamp in mtime,
    while older files can preserve it in creation time.
    """
    day = filename_day(path)
    if not day:
        return []
    expected = datetime.strptime(day, "%Y-%m-%d").date()
    try:
        stat = path.stat()
    except OSError:
        return []

    raw: List[Tuple[str, float]] = []
    birth = getattr(stat, "st_birthtime", None)
    if birth:
        raw.append(("birthtime", birth))
    if os.name == "nt":
        raw.append(("creation", stat.st_ctime))
    raw.append(("mtime", stat.st_mtime))

    clocks: List[Tuple[str, datetime]] = []
    seen: set[Tuple[str, int]] = set()
    for source, stamp in raw:
        key = (source, int(stamp))
        if key in seen:
            continue
        seen.add(key)
        clock = datetime.fromtimestamp(stamp).replace(tzinfo=None, microsecond=0)
        if abs((clock.date() - expected).days) <= 1:
            clocks.append((source, clock))
    return clocks


def _utc_interpretations(clock: datetime, target_tz) -> List[Tuple[str, datetime]]:
    """Interpret one naive filesystem clock in every safe way we have observed.

    Snapchat/ZIP exports are inconsistent across generations:
    - some Windows creation timestamps behave like a UTC wall clock;
    - many LastWriteTime values are local wall clock values.

    For local time we try both folds so the autumn DST overlap is handled.
    """
    values: List[Tuple[str, datetime]] = [
        ("as-utc", clock.replace(tzinfo=timezone.utc)),
    ]

    if target_tz is not None:
        for fold in (0, 1):
            try:
                local = clock.replace(tzinfo=target_tz, fold=fold)
                utc_value = local.astimezone(timezone.utc)
                values.append((f"as-local-fold{fold}", utc_value))
            except (ValueError, OverflowError):
                pass

    deduped: List[Tuple[str, datetime]] = []
    seen: set[datetime] = set()
    for mode, value in values:
        value = value.replace(microsecond=0)
        if value not in seen:
            seen.add(value)
            deduped.append((mode, value))
    return deduped


def _best_file_entry_delta(path: Path, entry, target_tz) -> Optional[Tuple[float, str, datetime]]:
    best: Optional[Tuple[float, str, datetime]] = None
    for source, clock in _filesystem_clocks(path):
        for mode, candidate_utc in _utc_interpretations(clock, target_tz):
            delta = abs((entry.dt_utc - candidate_utc).total_seconds())
            current = (delta, f"{source}/{mode}", candidate_utc)
            if best is None or current[0] < best[0]:
                best = current
    return best


def _adjacent_days(day: str) -> Tuple[str, str, str]:
    base = datetime.strptime(day, "%Y-%m-%d")
    return (
        (base - timedelta(days=1)).strftime("%Y-%m-%d"),
        day,
        (base + timedelta(days=1)).strftime("%Y-%m-%d"),
    )


def _candidate_entries(day: str, media_type: str, by_group, used_entries: set[int]):
    """Use same-day JSON first, plus adjacent UTC days for midnight crossings."""
    result = []
    seen = set()
    for candidate_day in _adjacent_days(day):
        for entry in by_group.get((candidate_day, media_type), []):
            if entry.source_index in used_entries or entry.source_index in seen:
                continue
            seen.add(entry.source_index)
            result.append(entry)
    return result


def _timezone_time_pairs(
    files: Sequence[Path],
    entries: Sequence[object],
    target_tz,
    tolerance_seconds: int,
) -> List[Tuple[Path, object, float, str]]:
    """Globally pair the strongest filesystem-time matches.

    An edge must be within tolerance.  We sort by delta and only let one file
    consume one JSON entry. Exact/near-exact matches therefore win before any
    weaker candidate can claim the same entry.
    """
    edges: List[Tuple[float, str, Path, object]] = []
    for path in files:
        for entry in entries:
            best = _best_file_entry_delta(path, entry, target_tz)
            if best is None:
                continue
            delta, interpretation, _ = best
            if delta <= tolerance_seconds:
                edges.append((delta, interpretation, path, entry))

    edges.sort(key=lambda item: (item[0], str(item[2]).lower(), item[3].source_index))
    used_files: set[Path] = set()
    used_entries: set[int] = set()
    pairs: List[Tuple[Path, object, float, str]] = []
    for delta, interpretation, path, entry in edges:
        if path in used_files or entry.source_index in used_entries:
            continue
        used_files.add(path)
        used_entries.add(entry.source_index)
        pairs.append((path, entry, delta, interpretation))
    return pairs


def match_media(
    files: Sequence[Path],
    by_uuid: Dict[str, List[object]],
    by_group: Dict[Tuple[str, str], List[object]],
    target_tz,
    tolerance_seconds: int = 300,
    unsafe_index_fallback: bool = False,
) -> List[MatchResult]:
    """Timezone-aware matcher for modern Snapchat My Data exports."""
    results: Dict[Path, MatchResult] = {}
    used_entries: set[int] = set()

    # 1) UUID remains authoritative when Snapchat provides one.
    for path in files:
        uid = filename_uuid(path)
        mtype = media_type_for(path)
        if not uid or uid not in by_uuid:
            continue
        candidates = [
            entry for entry in by_uuid[uid]
            if entry.source_index not in used_entries and entry.media_type == mtype
        ]
        if len(candidates) == 1:
            entry = candidates[0]
            results[path] = MatchResult(path, entry, "uuid", "high")
            used_entries.add(entry.source_index)

    # 2) One file + one same-day/type JSON row is safe without filesystem time.
    file_groups: Dict[Tuple[str, str], List[Path]] = {}
    for path in files:
        if path in results:
            continue
        day = filename_day(path)
        mtype = media_type_for(path)
        if not day or not mtype:
            results[path] = MatchResult(path, None, "none", "none", note="invalid filename date/type")
            continue
        file_groups.setdefault((day, mtype), []).append(path)

    deferred: Dict[Tuple[str, str], List[Path]] = {}
    for key, group_files in file_groups.items():
        same_day_entries = [
            entry for entry in by_group.get(key, [])
            if entry.source_index not in used_entries
        ]
        if len(group_files) == 1 and len(same_day_entries) == 1:
            path, entry = group_files[0], same_day_entries[0]
            results[path] = MatchResult(path, entry, "unique-day-type", "high")
            used_entries.add(entry.source_index)
        else:
            deferred[key] = sorted(group_files, key=lambda p: p.name.lower())

    # 3) Ambiguous groups: compare every plausible filesystem clock both as UTC
    #    and as a local wall clock in the requested IANA timezone.
    for (day, mtype), group_files in deferred.items():
        entries = _candidate_entries(day, mtype, by_group, used_entries)
        matched_files: set[Path] = set()
        matched_entries: set[int] = set()

        for path, entry, delta, interpretation in _timezone_time_pairs(
            group_files, entries, target_tz, tolerance_seconds
        ):
            confidence = "high" if delta <= 2 else "medium" if delta <= 10 else "low"
            results[path] = MatchResult(
                path,
                entry,
                "filesystem-time-tz",
                confidence,
                delta_seconds=delta,
                note=interpretation,
            )
            matched_files.add(path)
            matched_entries.add(entry.source_index)
            used_entries.add(entry.source_index)

        remaining_files = [p for p in group_files if p not in matched_files]
        remaining_entries = [
            entry for entry in by_group.get((day, mtype), [])
            if entry.source_index not in used_entries
        ]

        if unsafe_index_fallback and len(remaining_files) == len(remaining_entries):
            remaining_files.sort(key=lambda p: p.name.lower())
            remaining_entries.sort(key=lambda entry: entry.dt_utc)
            for path, entry in zip(remaining_files, remaining_entries):
                results[path] = MatchResult(
                    path, entry, "index-fallback", "low", note="unsafe ordered fallback"
                )
                used_entries.add(entry.source_index)
        else:
            for path in remaining_files:
                same_day_count = len(by_group.get((day, mtype), []))
                results[path] = MatchResult(
                    path,
                    None,
                    "ambiguous",
                    "none",
                    note=f"ambiguous group: {len(group_files)} files / {same_day_count} same-day JSON entries",
                )

    return [results[path] for path in files]
