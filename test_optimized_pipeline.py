import unittest
from datetime import datetime
from pathlib import Path

import optimized_pipeline
import process


class OptimizedPipelineTests(unittest.TestCase):
    def test_worker_count_is_valid(self):
        self.assertGreaterEqual(optimized_pipeline.default_workers(), 1)

    def test_output_layout_year_month(self):
        root = Path("photos-ready")
        dt = datetime(2026, 9, 15, 18, 30, 0)
        self.assertEqual(
            optimized_pipeline._output_dir(root, dt, "year-month"),
            root / "2026" / "09",
        )

    def test_output_layout_flat(self):
        root = Path("photos-ready")
        dt = datetime(2026, 9, 15, 18, 30, 0)
        self.assertEqual(optimized_pipeline._output_dir(root, dt, "flat"), root)

    def test_cli_performance_options(self):
        args = process.build_parser().parse_args([
            "export",
            "--workers", "2",
            "--output-layout", "flat",
            "--progress-every", "7",
            "--verbose",
        ])
        self.assertEqual(args.workers, 2)
        self.assertEqual(args.output_layout, "flat")
        self.assertEqual(args.progress_every, 7)
        self.assertTrue(args.verbose)


if __name__ == "__main__":
    unittest.main()
