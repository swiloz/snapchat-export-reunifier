import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import process


class ReunifierTests(unittest.TestCase):
    def test_uuid_parsing(self):
        uid = "ab8798d4-03f2-4410-7819-f91e8e0ee1f6"
        self.assertEqual(process.extract_uuid(f"https://x.invalid/?mid={uid}&foo=1"), uid)
        self.assertEqual(process.extract_uuid(f"https://x.invalid/?sid={uid}"), uid)
        self.assertEqual(
            process.filename_uuid(Path(f"2021-01-05_{uid}-main.jpg")), uid
        )

    def test_overlay_key_pairs_main_and_overlay(self):
        uid = "ab8798d4-03f2-4410-7819-f91e8e0ee1f6"
        main = Path(f"2021-01-05_{uid}-main.jpg")
        overlay = Path(f"2021-01-05_{uid}-overlay.png")
        self.assertEqual(process.overlay_key(main), process.overlay_key(overlay))

    def test_blank_links_are_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "memories_history.json"
            json_path.write_text(json.dumps({"Saved Media": [{
                "Date": "2021-01-05 08:06:42 UTC",
                "Media Type": "Image",
                "Location": "Latitude, Longitude: 44.701897, 4.795153",
                "Download Link": "",
                "Media Download Url": "",
            }]}), encoding="utf-8")
            entries, by_uuid, by_group = process.load_metadata(json_path)
            self.assertEqual(len(entries), 1)
            self.assertEqual(by_uuid, {})
            self.assertEqual(len(by_group[("2021-01-05", "Image")]), 1)

    def test_unique_day_type_match_is_safe_without_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "2021-01-05_ab8798d4-03f2-4410-7819-f91e8e0ee1f6-main.jpg"
            media.write_bytes(b"x")
            entry = process.MetadataEntry(
                0, datetime(2021, 1, 5, 8, 6, 42, tzinfo=timezone.utc),
                "Image", 44.7, 4.79, None,
            )
            matches = process.match_media(
                [media], {}, {("2021-01-05", "Image"): [entry]}
            )
            self.assertEqual(matches[0].method, "unique-day-type")
            self.assertEqual(matches[0].confidence, "high")
            self.assertIs(matches[0].entry, entry)

    def test_ambiguous_group_does_not_use_index_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = Path(tmp) / "2021-01-05_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa-main.jpg"
            p2 = Path(tmp) / "2021-01-05_bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb-main.jpg"
            p1.write_bytes(b"a")
            p2.write_bytes(b"b")
            now = datetime.now().timestamp()
            os.utime(p1, (now, now)); os.utime(p2, (now, now))
            entries = [
                process.MetadataEntry(0, datetime(2021, 1, 5, 8, tzinfo=timezone.utc), "Image", 1, 2, None),
                process.MetadataEntry(1, datetime(2021, 1, 5, 9, tzinfo=timezone.utc), "Image", 3, 4, None),
            ]
            matches = process.match_media(
                [p1, p2], {}, {("2021-01-05", "Image"): entries}
            )
            self.assertTrue(all(m.method == "ambiguous" for m in matches))
            self.assertTrue(all(m.entry is None for m in matches))

    def test_timestamp_matching_recovers_multi_item_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = Path(tmp) / "2021-01-05_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa-main.jpg"
            p2 = Path(tmp) / "2021-01-05_bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb-main.jpg"
            p1.write_bytes(b"a"); p2.write_bytes(b"b")
            t1 = datetime(2021, 1, 5, 7, 6, 42).timestamp()
            t2 = datetime(2021, 1, 5, 9, 10, 0).timestamp()
            os.utime(p1, (t1, t1)); os.utime(p2, (t2, t2))
            entries = [
                process.MetadataEntry(0, datetime(2021, 1, 5, 8, 6, 42, tzinfo=timezone.utc), "Image", 1, 2, None),
                process.MetadataEntry(1, datetime(2021, 1, 5, 10, 10, 0, tzinfo=timezone.utc), "Image", 3, 4, None),
            ]
            matches = process.match_media(
                [p1, p2], {}, {("2021-01-05", "Image"): entries}, tolerance_seconds=5
            )
            self.assertEqual([m.method for m in matches], ["filesystem-time", "filesystem-time"])
            self.assertEqual([m.delta_seconds for m in matches], [0.0, 0.0])


if __name__ == "__main__":
    unittest.main()
