from __future__ import annotations

import os
import shutil
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

import process_core as core


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


class Progress:
    def __init__(self, label: str, total: int, every: int = 25, verbose: bool = False):
        self.label = label
        self.total = max(total, 1)
        self.every = max(every, 1)
        self.verbose = verbose
        self.started = time.perf_counter()

    def show(self, done: int, suffix: str = "") -> None:
        if not (self.verbose or done == self.total or done % self.every == 0):
            return
        elapsed = max(time.perf_counter() - self.started, 1e-6)
        rate = done / elapsed
        remaining = max(self.total - done, 0)
        eta = remaining / rate if rate > 0 else 0
        pct = 100.0 * done / self.total
        tail = f" | {suffix}" if suffix else ""
        print(
            f"  {self.label}: {done}/{self.total} ({pct:5.1f}%)"
            f" | {rate:5.1f}/s | ETA {_fmt_duration(eta)}{tail}",
            flush=True,
        )


def default_workers() -> int:
    # Conservative default: enough parallelism to hide ExifTool/process and disk
    # latency without launching too many concurrent FFmpeg encoders.
    return min(4, max(1, os.cpu_count() or 1))


def _hash_one(path: Path) -> Tuple[Path, Optional[str], Optional[str]]:
    try:
        return path, core.sha256_file(path), None
    except OSError as exc:
        return path, None, str(exc)


def deduplicate_parallel(
    files: Sequence[Path],
    mode: str,
    duplicate_log,
    workers: int,
    progress_every: int,
    verbose: bool,
):
    if mode == "none":
        return list(files), 0

    progress = Progress("Hashing", len(files), progress_every, verbose)
    results = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="snap-hash") as pool:
        # map() preserves file order, keeping dedupe deterministic.
        for index, result in enumerate(pool.map(_hash_one, files), start=1):
            results.append(result)
            progress.show(index)

    seen = {}
    unique = []
    duplicates = 0
    for path, digest, error in results:
        if error or digest is None:
            duplicate_log.write(f"HASH_FAIL: {path} ({error or 'unknown error'})\n")
            unique.append(path)
            continue

        if mode == "within-day":
            key = (core.filename_day(path) or "", core.media_type_for(path) or "", digest)
        else:
            key = (digest,)

        if key in seen:
            duplicates += 1
            duplicate_log.write(f"DUP: {path} (matches {seen[key]})\n")
        else:
            seen[key] = path
            unique.append(path)

    return unique, duplicates


def _output_dir(root: Path, local_dt, layout: str) -> Path:
    if layout == "flat":
        return root
    return root / local_dt.strftime("%Y") / local_dt.strftime("%m")


def _overlay_output_dir(root: Path, overlay: Path, layout: str) -> Path:
    if layout == "flat":
        return root
    day = core.filename_day(overlay)
    if not day:
        return root / "undated"
    return root / day[:4] / day[5:7]


@dataclass
class MediaOutcome:
    index: int
    source: Path
    destination: Optional[Path] = None
    processed: bool = False
    gps: bool = False
    overlay_merged: bool = False
    metadata_fail: Optional[str] = None
    render_fail: Optional[str] = None
    warning: Optional[str] = None
    oversized: bool = False


def _process_one(
    index,
    match,
    output_dir: Path,
    output_layout: str,
    overlay_map,
    target_tz,
    overlay_mode: str,
    ffmpeg: str,
    exiftool: str,
) -> MediaOutcome:
    source = match.file
    outcome = MediaOutcome(index=index, source=source)
    try:
        mtype = core.media_type_for(source)
        if mtype is None:
            outcome.render_fail = "unsupported media type"
            return outcome

        if match.entry:
            local_dt = match.entry.dt_utc.astimezone(target_tz)
        else:
            local_dt = core.default_datetime_for(source, target_tz)

        dest_dir = _output_dir(output_dir, local_dt, output_layout)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / core.destination_name(source, local_dt, index)
        outcome.destination = dest
        overlay = overlay_map.get(core.overlay_key(source))

        ok, render_message, merged = core.render_media(
            source, overlay, dest, overlay_mode, ffmpeg
        )
        if not ok:
            outcome.render_fail = render_message
            return outcome
        outcome.overlay_merged = merged
        if render_message:
            outcome.warning = render_message

        ok, message, _ = core.exiftool_write(
            dest, mtype, match.entry, target_tz, exiftool
        )
        if not ok:
            outcome.metadata_fail = message
        else:
            outcome.processed = True
            outcome.gps = bool(
                match.entry
                and match.entry.lat is not None
                and match.entry.lon is not None
            )

        try:
            size = dest.stat().st_size
            limit = core.MAX_PHOTO_BYTES if mtype == "Image" else core.MAX_VIDEO_BYTES
            outcome.oversized = size > limit
        except OSError as exc:
            extra = f"could not stat output: {exc}"
            outcome.warning = f"{outcome.warning}; {extra}" if outcome.warning else extra
        return outcome
    except Exception as exc:
        outcome.render_fail = f"unexpected processing error: {exc}"
        return outcome


def process(args, matcher, target_tz) -> int:
    started_total = time.perf_counter()
    export_dir = Path(args.export_dir).resolve()
    if not export_dir.exists():
        print(f"Error: export directory not found: {export_dir}")
        return 1

    workers = max(1, int(args.workers))
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

    print("=" * 72)
    print("  Snapchat Export Reunifier — optimized robust mode")
    print("=" * 72)
    print(f"  Export:          {export_dir}")
    print(f"  Output:          {output_dir}")
    print(f"  Output layout:   {args.output_layout}")
    print(f"  Overlay mode:    {args.overlay_mode}")
    print(f"  Dedupe:          {args.dedupe}")
    print(f"  Timezone:        {args.timezone or args.tz_offset or 'UTC'}")
    print(f"  Workers:         {workers}")
    print(f"  Progress every:  {args.progress_every} file(s)")
    print(f"  Verbose:         {args.verbose}")
    print(f"  Dry run:         {args.dry_run}")

    stage = time.perf_counter()
    json_file = core.find_json_file(export_dir)
    if json_file:
        entries, by_uuid, by_group = core.load_metadata(json_file)
        gps_entries = sum(1 for e in entries if e.lat is not None and e.lon is not None)
        print(
            f"\n[1/6] Metadata: {len(entries)} JSON entries, "
            f"{gps_entries} with GPS, {sum(len(v) for v in by_uuid.values())} with UUID links "
            f"({_fmt_duration(time.perf_counter() - stage)})"
        )
        if args.verbose:
            print(f"      JSON: {json_file}")
    else:
        entries, by_uuid, by_group = [], {}, {}
        print("\n[1/6] No memories_history.json found — date-only fallback")

    stage = time.perf_counter()
    all_files = core.collect_media_files(export_dir, excluded=(output_dir, overlay_dir, log_dir))
    print(
        f"[2/6] Scan: {len(all_files)} media files found "
        f"({_fmt_duration(time.perf_counter() - stage)})"
    )
    if not all_files:
        return 1

    stage = time.perf_counter()
    with (log_dir / "duplicates.log").open("w", encoding="utf-8") as duplicate_log:
        unique_files, duplicate_count = deduplicate_parallel(
            all_files,
            args.dedupe,
            duplicate_log,
            workers,
            args.progress_every,
            args.verbose,
        )
    print(
        f"[3/6] Dedupe: {len(unique_files)} kept / {duplicate_count} skipped "
        f"({_fmt_duration(time.perf_counter() - stage)})"
    )

    overlays = [path for path in unique_files if core.is_overlay(path)]
    mains = [path for path in unique_files if not core.is_overlay(path)]
    overlay_map = {}
    for overlay in overlays:
        overlay_map.setdefault(core.overlay_key(overlay), overlay)
    print(f"[4/6] Media split: {len(mains)} main media / {len(overlays)} overlays")

    stage = time.perf_counter()
    matches = matcher(
        mains,
        by_uuid,
        by_group,
        tolerance_seconds=args.match_tolerance_seconds,
        unsafe_index_fallback=args.unsafe_index_fallback,
    )
    core.write_match_report(log_dir / "matches.csv", matches)
    method_counts = Counter(match.method for match in matches)
    unmatched = sum(1 for match in matches if match.entry is None)
    print(
        "[5/6] Matching: "
        + ", ".join(f"{key}={value}" for key, value in sorted(method_counts.items()))
        + f" | unmatched={unmatched} ({_fmt_duration(time.perf_counter() - stage)})"
    )

    if args.dry_run:
        print(f"\nDry run complete in {_fmt_duration(time.perf_counter() - started_total)}.")
        print(f"Match report: {log_dir / 'matches.csv'}")
        return 0

    stage = time.perf_counter()
    if args.overlay_mode != "ignore":
        for overlay in overlays:
            dest_dir = _overlay_output_dir(overlay_dir, overlay, args.output_layout)
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(overlay, dest_dir / overlay.name)
        if overlays:
            print(
                f"      Backed up {len(overlays)} overlay files "
                f"({_fmt_duration(time.perf_counter() - stage)})"
            )

    print(f"[6/6] Processing {len(matches)} media with {workers} worker(s)...")
    progress = Progress("Processing", len(matches), args.progress_every, args.verbose)
    stats = Counter()
    outcomes = []

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="snap-media") as pool:
        futures = {
            pool.submit(
                _process_one,
                index,
                match,
                output_dir,
                args.output_layout,
                overlay_map,
                target_tz,
                args.overlay_mode,
                args.ffmpeg,
                args.exiftool,
            ): index
            for index, match in enumerate(matches, start=1)
        }

        for done, future in enumerate(as_completed(futures), start=1):
            try:
                outcome = future.result()
            except Exception as exc:
                outcome = MediaOutcome(
                    index=futures[future],
                    source=Path("<unknown>"),
                    render_fail=f"worker crashed: {exc}",
                )
            outcomes.append(outcome)
            if outcome.processed:
                stats["processed"] += 1
            if outcome.gps:
                stats["gps"] += 1
            if outcome.overlay_merged:
                stats["overlay_merged"] += 1
            if outcome.metadata_fail:
                stats["metadata_fail"] += 1
            if outcome.render_fail:
                stats["render_fail"] += 1
            if outcome.oversized:
                stats["oversized"] += 1

            suffix = (
                f"ok={stats['processed']} overlay={stats['overlay_merged']} "
                f"gps={stats['gps']} errors={stats['metadata_fail'] + stats['render_fail']}"
            )
            progress.show(done, suffix)
            if args.verbose and (outcome.metadata_fail or outcome.render_fail or outcome.warning):
                problem = outcome.render_fail or outcome.metadata_fail or outcome.warning
                print(f"      ! {outcome.source.name}: {problem}", flush=True)

    outcomes.sort(key=lambda o: o.index)
    with (log_dir / "failed_files.log").open("w", encoding="utf-8") as failed_log, \
         (log_dir / "warnings.log").open("w", encoding="utf-8") as warnings_log:
        for outcome in outcomes:
            if outcome.render_fail:
                failed_log.write(f"RENDER_FAIL: {outcome.source}: {outcome.render_fail}\n")
            if outcome.metadata_fail:
                failed_log.write(
                    f"METADATA_FAIL: {outcome.destination or outcome.source}: {outcome.metadata_fail}\n"
                )
            if outcome.warning:
                warnings_log.write(f"{outcome.source}: {outcome.warning}\n")
            if outcome.oversized and outcome.destination:
                warnings_log.write(f"OVERSIZED: {outcome.destination}\n")

    total_elapsed = time.perf_counter() - started_total
    summary = [
        "Snapchat Export Reunifier — summary",
        "=" * 52,
        f"Media found:             {len(all_files)}",
        f"Duplicates skipped:      {duplicate_count}",
        f"Main media:              {len(mains)}",
        f"Overlays found:          {len(overlays)}",
        f"Overlays merged:         {stats['overlay_merged']}",
        f"Processed:               {stats['processed']}",
        f"GPS written:             {stats['gps']}",
        f"Metadata failures:       {stats['metadata_fail']}",
        f"Render failures:         {stats['render_fail']}",
        f"Ambiguous/unmatched:     {unmatched}",
        f"Unsafe index matches:    {sum(1 for m in matches if m.method == 'index-fallback')}",
        f"Workers:                 {workers}",
        f"Output layout:           {args.output_layout}",
        f"Elapsed:                 {_fmt_duration(total_elapsed)}",
        f"Report:                  {log_dir / 'matches.csv'}",
    ]
    (log_dir / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n" + "\n".join(summary))
    return 0
