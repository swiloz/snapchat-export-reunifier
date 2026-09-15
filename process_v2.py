#!/usr/bin/env python3
"""Temporary validation runner for the timezone-aware matcher.

Uses the existing processing/rendering pipeline from process.py and replaces
only the matching function. This keeps EXIF/overlay behavior unchanged while
we validate the new matcher on real Snapchat exports.
"""

import process
import robust_matcher


def main() -> None:
    args = process.build_parser().parse_args()

    try:
        target_tz = process.resolve_output_timezone(args.timezone, args.tz_offset)
    except Exception as exc:
        raise SystemExit(f"Error: {exc}")

    def timezone_aware_match_media(
        files,
        by_uuid,
        by_group,
        tolerance_seconds=300,
        unsafe_index_fallback=False,
    ):
        return robust_matcher.match_media(
            files,
            by_uuid,
            by_group,
            target_tz=target_tz,
            tolerance_seconds=tolerance_seconds,
            unsafe_index_fallback=unsafe_index_fallback,
        )

    process.match_media = timezone_aware_match_media
    raise SystemExit(process.process(args))


if __name__ == "__main__":
    main()
