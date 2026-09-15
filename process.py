#!/usr/bin/env python3
"""Snapchat Export Reunifier — main entry point.

Runs the proven baseline matcher first, retries only unresolved media with the
timezone-aware matcher, and treats Snapchat's 0,0 GPS placeholder as missing
location data.
"""

from collections import defaultdict

import process_core as core
import robust_matcher
from process_core import *  # re-export the public API used by tests/tools


BASE_MATCH_MEDIA = core.match_media
ORIGINAL_PARSE_LOCATION = core.parse_location


def parse_location(value):
    """Parse Snapchat GPS, treating the 0,0 placeholder as no location."""
    lat, lon = ORIGINAL_PARSE_LOCATION(value)
    if lat == 0.0 and lon == 0.0:
        return None, None
    return lat, lon


# load_metadata resolves parse_location in process_core's module globals, so
# patch it before processing. This also makes GPS counters/reports ignore 0,0.
core.parse_location = parse_location


def hybrid_match_media(
    files,
    by_uuid,
    by_group,
    target_tz,
    tolerance_seconds=300,
    unsafe_index_fallback=False,
):
    """Keep baseline matches and retry only unresolved files timezone-aware."""
    baseline = BASE_MATCH_MEDIA(
        files,
        by_uuid,
        by_group,
        tolerance_seconds=tolerance_seconds,
        unsafe_index_fallback=False,
    )

    used_entries = {
        match.entry.source_index
        for match in baseline
        if match.entry is not None
    }
    unresolved = [match.file for match in baseline if match.entry is None]
    if not unresolved:
        return baseline

    remaining_by_uuid = defaultdict(list)
    for uid, entries in by_uuid.items():
        remaining_by_uuid[uid] = [
            entry for entry in entries if entry.source_index not in used_entries
        ]

    remaining_by_group = defaultdict(list)
    for key, entries in by_group.items():
        remaining_by_group[key] = [
            entry for entry in entries if entry.source_index not in used_entries
        ]

    retry = robust_matcher.match_media(
        unresolved,
        remaining_by_uuid,
        remaining_by_group,
        target_tz=target_tz,
        tolerance_seconds=tolerance_seconds,
        unsafe_index_fallback=unsafe_index_fallback,
    )
    retry_by_file = {match.file: match for match in retry}

    combined = []
    for original in baseline:
        if original.entry is not None:
            combined.append(original)
            continue

        candidate = retry_by_file.get(original.file)
        if candidate is not None and candidate.entry is not None:
            if candidate.method == "filesystem-time-tz":
                candidate.note = "fallback; " + candidate.note
            combined.append(candidate)
        else:
            combined.append(original)

    return combined


def main() -> None:
    args = core.build_parser().parse_args()

    try:
        target_tz = core.resolve_output_timezone(args.timezone, args.tz_offset)
    except Exception as exc:
        raise SystemExit(f"Error: {exc}")

    def matcher(
        files,
        by_uuid,
        by_group,
        tolerance_seconds=300,
        unsafe_index_fallback=False,
    ):
        return hybrid_match_media(
            files,
            by_uuid,
            by_group,
            target_tz=target_tz,
            tolerance_seconds=tolerance_seconds,
            unsafe_index_fallback=unsafe_index_fallback,
        )

    core.match_media = matcher
    raise SystemExit(core.process(args))


if __name__ == "__main__":
    main()
