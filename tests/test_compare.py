"""Tests for frame comparison PNG generation."""

import argparse
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
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
        self.assertNotIn("geq=", video_filter)
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
led_shadow_red_bias = 0.25
rgb5_quantizer = false
""".lstrip(),
                encoding="utf-8",
            )

            fit, variants, timestamps = compare.load_config(path)

        self.assertEqual(fit, "crop")
        self.assertEqual([variant.name for variant in variants], ["default", "no gamma"])
        self.assertEqual(variants[1].led_gamma, 1)
        self.assertEqual(variants[1].led_shadow_red_bias, 0.25)
        self.assertFalse(variants[1].rgb5_quantizer)
        self.assertIsNone(timestamps)

    def test_loads_timestamps_from_toml(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comparison.toml"
            path.write_text(
                "timestamps = [887.261, \"01:20:53\"]\n\n"
                "[[variant]]\nname = \"default\"\n",
                encoding="utf-8",
            )

            _fit, _variants, timestamps = compare.load_config(path)

        self.assertEqual(timestamps, [887.261, 4853.0])

    def test_header_png_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "header.png"
            compare.write_header_png(path, ["source", "no gamma"])

            self.assertTrue(path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_rgb565_preview_applies_inverse_led_gamma(self):
        pixel = (1 << 11) | (2 << 5) | 1
        frame = struct.pack("<H", pixel) * (
            compare.stream.WIDTH * compare.stream.HEIGHT
        )

        linear = compare.rgb565_to_cell(frame, preview_gamma=1)
        perceived = compare.rgb565_to_cell(frame, preview_gamma=2.2)

        self.assertEqual(linear[:3], bytes((8, 8, 8)))
        self.assertGreater(perceived[0], linear[0])
        self.assertEqual(perceived[0], perceived[2])

    def test_rgb565_preview_discards_green_lsb_like_five_bit_panel(self):
        even_pixel = (1 << 11) | (2 << 5) | 1
        odd_pixel = (1 << 11) | (3 << 5) | 1
        pixel_count = compare.stream.WIDTH * compare.stream.HEIGHT

        even = compare.rgb565_to_cell(
            struct.pack("<H", even_pixel) * pixel_count
        )
        odd = compare.rgb565_to_cell(
            struct.pack("<H", odd_pixel) * pixel_count
        )

        self.assertEqual(even, odd)

    def test_exact_comparison_uses_live_frame_pipeline_for_panel_cells(self):
        frame = bytes(compare.stream.FRAME_SIZE)
        source_rgb = bytes(compare.CELL_WIDTH * compare.CELL_HEIGHT * 3)

        def one_frame():
            yield frame

        variant = compare.ComparisonVariant(
            "test",
            led_gamma=1.4,
            led_dark_floor=3,
            led_shadow_red_bias=0.25,
            rgb5_quantizer=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch(
                    "compare.stream.ffmpeg_frames",
                    side_effect=[one_frame(), one_frame()],
                ) as ffmpeg_frames,
                patch(
                    "compare.subprocess.run",
                    return_value=SimpleNamespace(stdout=source_rgb),
                ),
                patch("compare.write_header_png"),
                patch("compare.write_rgb24_png"),
                patch("compare.shutil.which", return_value="/opt/bin/ffmpeg"),
            ):
                compare.render_exact_comparison(
                    Path("/tmp/movie.mkv"),
                    Path("/tmp/comparison.png"),
                    [5043.33],
                    "letterbox",
                    [variant],
                    Path(directory),
                )

        self.assertEqual(ffmpeg_frames.call_count, 2)
        scaled_call, variant_call = ffmpeg_frames.call_args_list
        self.assertTrue(scaled_call.kwargs["scaled_source"])
        self.assertFalse(variant_call.kwargs["scaled_source"])
        self.assertEqual(variant_call.kwargs["led_gamma"], 1.4)
        self.assertEqual(variant_call.kwargs["led_dark_floor"], 3)
        self.assertEqual(variant_call.kwargs["led_shadow_red_bias"], 0.25)
        self.assertFalse(variant_call.kwargs["rgb5_quantizer"])

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
                "--led-shadow-red-bias",
                "0.25",
                "--no-rgb5-quantizer",
            ]
        )

        self.assertEqual(args.led_gamma, 1)
        self.assertEqual(args.led_dark_floor, 0)
        self.assertEqual(args.led_shadow_red_bias, 0.25)
        self.assertFalse(args.rgb5_quantizer)


if __name__ == "__main__":
    unittest.main()
