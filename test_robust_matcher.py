import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import robust_matcher


class Entry:
    def __init__(self, index, dt_utc, media_type="Video"):
        self.source_index = index
        self.dt_utc = dt_utc
        self.media_type = media_type
        self.lat = None
        self.lon = None
        self.uuid = None


class RobustMatcherTests(unittest.TestCase):
    def test_summer_local_clock_matches_utc_json(self):
        path = Path("2022-03-27_147e0447-2258-4764-49dd-33ac6c6ba2ac-main.mp4")
        entry = Entry(0, datetime(2022, 3, 27, 8, 14, 33, tzinfo=timezone.utc))
        clocks = [("mtime", datetime(2022, 3, 27, 10, 14, 33))]
        with patch("robust_matcher._filesystem_clocks", return_value=clocks):
            matches = robust_matcher.match_media(
                [path], {}, {("2022-03-27", "Video"): [entry]}, ZoneInfo("Europe/Paris")
            )
        self.assertEqual(matches[0].method, "unique-day-type")

    def test_multiple_summer_files_use_cest_conversion(self):
        p1 = Path("2022-03-27_11111111-1111-1111-1111-111111111111-main.mp4")
        p2 = Path("2022-03-27_22222222-2222-2222-2222-222222222222-main.mp4")
        entries = [
            Entry(0, datetime(2022, 3, 27, 8, 14, 33, tzinfo=timezone.utc)),
            Entry(1, datetime(2022, 3, 27, 10, 50, 35, tzinfo=timezone.utc)),
        ]
        clocks = {
            p1: [("mtime", datetime(2022, 3, 27, 10, 14, 33))],
            p2: [("mtime", datetime(2022, 3, 27, 12, 50, 35))],
        }
        with patch("robust_matcher._filesystem_clocks", side_effect=lambda p: clocks[p]):
            matches = robust_matcher.match_media(
                [p1, p2], {}, {("2022-03-27", "Video"): entries},
                ZoneInfo("Europe/Paris"), tolerance_seconds=2,
            )
        self.assertEqual([m.method for m in matches], ["filesystem-time-tz", "filesystem-time-tz"])
        self.assertEqual([m.delta_seconds for m in matches], [0.0, 0.0])
        self.assertTrue(all("as-local" in m.note for m in matches))

    def test_winter_local_clock_uses_cet_conversion(self):
        p1 = Path("2022-10-31_11111111-1111-1111-1111-111111111111-main.mp4")
        p2 = Path("2022-10-31_22222222-2222-2222-2222-222222222222-main.mp4")
        entries = [
            Entry(0, datetime(2022, 10, 31, 18, 1, 1, tzinfo=timezone.utc)),
            Entry(1, datetime(2022, 10, 31, 18, 7, 17, tzinfo=timezone.utc)),
        ]
        clocks = {
            p1: [("mtime", datetime(2022, 10, 31, 19, 1, 1))],
            p2: [("mtime", datetime(2022, 10, 31, 19, 7, 17))],
        }
        with patch("robust_matcher._filesystem_clocks", side_effect=lambda p: clocks[p]):
            matches = robust_matcher.match_media(
                [p1, p2], {}, {("2022-10-31", "Video"): entries},
                ZoneInfo("Europe/Paris"), tolerance_seconds=2,
            )
        self.assertEqual([m.delta_seconds for m in matches], [0.0, 0.0])
        self.assertTrue(all("as-local" in m.note for m in matches))

    def test_raw_utc_creation_clock_is_also_supported(self):
        p1 = Path("2020-06-14_11111111-1111-1111-1111-111111111111-main.jpg")
        p2 = Path("2020-06-14_22222222-2222-2222-2222-222222222222-main.jpg")
        entries = [
            Entry(0, datetime(2020, 6, 14, 12, 55, 5, tzinfo=timezone.utc), "Image"),
            Entry(1, datetime(2020, 6, 14, 15, 38, 27, tzinfo=timezone.utc), "Image"),
        ]
        clocks = {
            p1: [("creation", datetime(2020, 6, 14, 12, 55, 4))],
            p2: [("creation", datetime(2020, 6, 14, 15, 38, 26))],
        }
        with patch("robust_matcher._filesystem_clocks", side_effect=lambda p: clocks[p]):
            matches = robust_matcher.match_media(
                [p1, p2], {}, {("2020-06-14", "Image"): entries},
                ZoneInfo("Europe/Paris"), tolerance_seconds=2,
            )
        self.assertEqual([m.delta_seconds for m in matches], [1.0, 1.0])
        self.assertTrue(all(m.note == "creation/as-utc" for m in matches))

    def test_local_midnight_crossing_can_match_previous_utc_day(self):
        path = Path("2025-10-31_38716316-BD1D-4386-8EBA-560B29757AC2-main.mp4")
        other = Path("2025-10-31_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa-main.mp4")
        previous_day_entry = Entry(0, datetime(2025, 10, 31, 23, 6, 48, tzinfo=timezone.utc))
        other_entry = Entry(1, datetime(2025, 10, 31, 7, 58, 19, tzinfo=timezone.utc))
        clocks = {
            path: [("mtime", datetime(2025, 11, 1, 0, 6, 48))],
            other: [("mtime", datetime(2025, 10, 31, 8, 58, 19))],
        }
        with patch("robust_matcher._filesystem_clocks", side_effect=lambda p: clocks[p]):
            matches = robust_matcher.match_media(
                [path, other], {}, {("2025-10-31", "Video"): [previous_day_entry, other_entry]},
                ZoneInfo("Europe/Paris"), tolerance_seconds=2,
            )
        self.assertEqual([m.delta_seconds for m in matches], [0.0, 0.0])


if __name__ == "__main__":
    unittest.main()
