"""Tests for frame comparison PNG generation."""

import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import compare


class CompareTests(unittest.TestCase):
    def test_parses_seconds_and_clock_timestamps(self):
        self.assertEqual(compare.parse_timestamp("12.5"), 12.5)
        self.assertEqual(compare.parse_timestamp("01:02.5"), 62.5)
        self.assertEqual(compare.parse_timestamp("1:02:03"), 3723)

    def test_rejects_invalid_timestamps(self):
        for timestamp in ("-1", "nan", "one:two", "1:2:3:4"):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(argparse.ArgumentTypeError):
                    compare.parse_timestamp(timestamp)

    def test_filter_builds_three_columns_and_one_row_per_timestamp(self):
        video_filter = compare.build_comparison_filter(2)

        self.assertIn("hstack=inputs=3[row0]", video_filter)
        self.assertIn("hstack=inputs=3[row1]", video_filter)
        self.assertIn("[row0][row1]vstack=inputs=2[comparison]", video_filter)
        self.assertIn("flags=neighbor", video_filter)
        self.assertIn("lutrgb", video_filter)
        self.assertIn("geq=", video_filter)
        self.assertNotIn("fps=", video_filter)
        scaled = video_filter.split("[scaled0_in]", 1)[1].split(
            "[scaled0]",
            1,
        )[0]
        self.assertNotIn("lutrgb", scaled)
        self.assertNotIn("geq=", scaled)

    def test_filter_builds_one_column_per_named_variant(self):
        variants = [
            compare.ComparisonVariant("default"),
            compare.ComparisonVariant("no gamma", led_gamma=1),
        ]
        video_filter = compare.build_comparison_filter(
            1,
            variants=variants,
        )

        self.assertIn("split=4", video_filter)
        self.assertIn("hstack=inputs=4[row0]", video_filter)
        self.assertIn("[processed0_0]", video_filter)
        self.assertIn("[processed0_1]", video_filter)

    def test_loads_named_variants_from_toml(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comparison.toml"
            path.write_text(
                """
fit = "crop"

[[variant]]
name = "default"

[[variant]]
name = "no gamma"
led_gamma = 1
rgb5_quantizer = false
""".lstrip(),
                encoding="utf-8",
            )

            fit, variants = compare.load_config(path)

        self.assertEqual(fit, "crop")
        self.assertEqual([variant.name for variant in variants], ["default", "no gamma"])
        self.assertEqual(variants[1].led_gamma, 1)
        self.assertFalse(variants[1].rgb5_quantizer)

    def test_header_png_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "header.png"
            compare.write_header_png(path, ["source", "no gamma"])

            self.assertTrue(path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    @patch("compare.stream.DISPLAY_ROTATION", 180)
    def test_filter_presents_rotated_panel_frames_upright(self):
        video_filter = compare.build_comparison_filter(1)
        scaled = video_filter.split("[scaled0_in]", 1)[1].split(
            "[scaled0]",
            1,
        )[0]

        self.assertEqual(scaled.count("hflip,vflip"), 2)

    @patch("compare.shutil.which", return_value="/opt/bin/ffmpeg")
    def test_command_seeks_each_timestamp_and_writes_one_png(self, _which):
        command = compare.build_comparison_command(
            Path("/tmp/movie.mkv"),
            Path("/tmp/comparison.png"),
            [1.5, 62],
        )

        seeks = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "-ss"
        ]
        self.assertEqual(seeks, ["1.5", "62"])
        self.assertEqual(command.count("/tmp/movie.mkv"), 2)
        self.assertEqual(command[command.index("-frames:v") + 1], "1")
        self.assertEqual(command[command.index("-update") + 1], "1")
        self.assertEqual(command[-1], "/tmp/comparison.png")

    def test_comparison_requires_at_least_one_row(self):
        with self.assertRaisesRegex(ValueError, "timestamp"):
            compare.build_comparison_filter(0)

    def test_parser_supports_explicit_preprocessing_disables(self):
        args = compare.create_parser().parse_args(
            [
                "/tmp/movie.mkv",
                "/tmp/comparison.png",
                "10",
                "--no-led-gamma",
                "--no-led-dark-floor",
                "--no-rgb5-quantizer",
            ]
        )

        self.assertEqual(args.led_gamma, 1)
        self.assertEqual(args.led_dark_floor, 0)
        self.assertFalse(args.rgb5_quantizer)


if __name__ == "__main__":
    unittest.main()
