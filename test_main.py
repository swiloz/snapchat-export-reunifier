import unittest

import process


class MainEntryPointTests(unittest.TestCase):
    def test_zero_zero_gps_is_treated_as_missing(self):
        self.assertEqual(
            process.parse_location("Latitude, Longitude: 0, 0"),
            (None, None),
        )

    def test_real_gps_is_preserved(self):
        self.assertEqual(
            process.parse_location("Latitude, Longitude: 45.799843, 4.946438"),
            (45.799843, 4.946438),
        )


if __name__ == "__main__":
    unittest.main()
