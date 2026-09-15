#!/usr/bin/env python3
"""Validation runner for the hybrid Snapchat matcher.

The proven matcher from process.py runs first. Only files it cannot match are
retried with the timezone-aware matcher. This preserves all existing safe
matches while recovering CET/CEST, UTC-wall-clock and midnight-crossing cases.
"""

from collections import defaultdict

import process
import robust_matcher


BASE_MATCH_MEDIA = process.match_media


def hybrid_match_media(
    files,
    by_uuid,
    by_group,
    target_tz,
    tolerance_seconds=300,
    unsafe_index_fallback=False,
):
    """Keep baseline matches and retry only unresolved files timezone-aware."""
    # Never let the baseline unsafe fallback consume entries before the robust
    # retry. If explicitly requested, unsafe fallback is applied by the second
    # pass only after all safe timestamp matching has been attempted.
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
            # Make it explicit in the audit CSV that this came from the second
            # timezone-aware pass rather than the baseline matcher.
            if candidate.method == "filesystem-time-tz":
                candidate.note = "fallback; " + candidate.note
            combined.append(candidate)
        else:
            # Keep the baseline diagnostic when the fallback cannot improve it.
            combined.append(original)

    return combined


def main() -> None:
    args = process.build_parser().parse_args()

    try:
        target_tz = process.resolve_output_timezone(args.timezone, args.tz_offset)
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

    process.match_media = matcher
    raise SystemExit(process.process(args))


if __name__ == "__main__":
    main()
