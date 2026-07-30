"""Build a PNG grid comparing source and MatrixPortal frame processing."""

import argparse
from dataclasses import dataclass
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import tomllib
import zlib

import stream


CELL_SCALE = 8
CELL_WIDTH = stream.WIDTH * CELL_SCALE
CELL_HEIGHT = stream.HEIGHT * CELL_SCALE
HEADER_HEIGHT = 32


@dataclass(frozen=True)
class ComparisonVariant:
    """One named post-processing configuration for the comparison grid."""

    name: str
    led_gamma: float = stream.DEFAULT_LED_GAMMA
    led_dark_floor: int = stream.DEFAULT_LED_DARK_FLOOR
    rgb5_quantizer: bool = True


FONT = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
    ".": ("00000", "00000", "00000", "00000", "00000", "00110", "00110"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
}


def parse_timestamp(value):
    """Parse seconds or an HH:MM:SS-style timestamp."""
    parts = value.split(":")
    if not 1 <= len(parts) <= 3:
        raise argparse.ArgumentTypeError(
            "timestamp must be seconds or [HH:]MM:SS"
        )
    try:
        components = [float(part) for part in parts]
    except ValueError as error:
        raise argparse.ArgumentTypeError("timestamp must be numeric") from error
    if any(not math.isfinite(part) or part < 0 for part in components):
        raise argparse.ArgumentTypeError(
            "timestamp must be non-negative and finite"
        )

    seconds = 0.0
    for component in components:
        seconds = seconds * 60 + component
    return seconds


def format_timestamp(timestamp):
    """Format a parsed timestamp for FFmpeg without redundant zeroes."""
    return "{:g}".format(timestamp)


def load_config(path):
    """Load and validate named post-processing variants from TOML."""
    try:
        with path.open("rb") as config_file:
            config = tomllib.load(config_file)
    except OSError as error:
        raise ValueError("could not read comparison config: {}".format(error)) from error
    if not isinstance(config, dict):
        raise ValueError("comparison config must contain a table")

    fit = config.get("fit", "letterbox")
    if fit not in ("letterbox", "crop"):
        raise ValueError("comparison config fit must be letterbox or crop")
    singular = config.get("variant")
    plural = config.get("variants")
    if singular is not None and plural is not None:
        raise ValueError("use either variant or variants, not both")
    variant_tables = singular if singular is not None else plural
    if not isinstance(variant_tables, list) or not variant_tables:
        raise ValueError("comparison config must define at least one variant")

    allowed = {"name", "led_gamma", "led_dark_floor", "rgb5_quantizer"}
    variants = []
    names = set()
    for table in variant_tables:
        if not isinstance(table, dict):
            raise ValueError("each comparison variant must be a table")
        unknown = set(table) - allowed
        if unknown:
            raise ValueError(
                "unknown comparison variant option(s): {}".format(
                    ", ".join(sorted(unknown))
                )
            )
        name = table.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("each comparison variant needs a non-empty name")
        if name in names:
            raise ValueError("comparison variant names must be unique")
        names.add(name)
        gamma = table.get("led_gamma", stream.DEFAULT_LED_GAMMA)
        try:
            gamma = stream.parse_led_gamma(str(gamma))
        except (TypeError, ValueError, argparse.ArgumentTypeError) as error:
            raise ValueError("invalid led_gamma for variant {}".format(name)) from error
        dark_floor = table.get(
            "led_dark_floor",
            stream.DEFAULT_LED_DARK_FLOOR,
        )
        if (
            isinstance(dark_floor, bool)
            or not isinstance(dark_floor, int)
            or not 0 <= dark_floor <= stream.RGB5_MAX
        ):
            raise ValueError("invalid led_dark_floor for variant {}".format(name))
        rgb5_quantizer = table.get("rgb5_quantizer", True)
        if not isinstance(rgb5_quantizer, bool):
            raise ValueError("invalid rgb5_quantizer for variant {}".format(name))
        variants.append(
            ComparisonVariant(
                name=name,
                led_gamma=gamma,
                led_dark_floor=dark_floor,
                rgb5_quantizer=rgb5_quantizer,
            )
        )
    return fit, variants


def _png_chunk(kind, payload):
    """Build one PNG chunk for the dependency-free header renderer."""
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(
        ">I",
        zlib.crc32(kind + payload) & 0xFFFFFFFF,
    )


def write_header_png(path, labels):
    """Render column labels into a small RGB PNG without extra dependencies."""
    width = CELL_WIDTH * len(labels)
    background = (24, 24, 24)
    foreground = (235, 235, 235)
    separator = (72, 72, 72)
    pixels = bytearray(background * (width * HEADER_HEIGHT))
    scale = 2
    advance = 12
    max_chars = max(1, (CELL_WIDTH - 16) // advance)

    def set_pixel(x, y, color):
        if 0 <= x < width and 0 <= y < HEADER_HEIGHT:
            offset = (y * width + x) * 3
            pixels[offset : offset + 3] = bytes(color)

    for column in range(len(labels)):
        left = column * CELL_WIDTH
        for x in (left, left + CELL_WIDTH - 1):
            for y in range(HEADER_HEIGHT):
                set_pixel(x, y, separator)
        label = str(labels[column]).upper()[:max_chars]
        text_width = max(0, len(label) * advance - 2)
        origin_x = left + max(0, (CELL_WIDTH - text_width) // 2)
        origin_y = (HEADER_HEIGHT - 7 * scale) // 2
        for char_index, char in enumerate(label):
            glyph = FONT.get(char, FONT[" "])
            glyph_x = origin_x + char_index * advance
            for glyph_y, glyph_row in enumerate(glyph):
                for glyph_x_offset, value in enumerate(glyph_row):
                    if value == "1":
                        for y_offset in range(scale):
                            for x_offset in range(scale):
                                set_pixel(
                                    glyph_x + glyph_x_offset * scale + x_offset,
                                    origin_y + glyph_y * scale + y_offset,
                                    foreground,
                                )

    raw = b"".join(
        b"\x00" + bytes(pixels[row * width * 3 : (row + 1) * width * 3])
        for row in range(HEADER_HEIGHT)
    )
    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, HEADER_HEIGHT, 8, 2, 0, 0, 0))
    png += _png_chunk(b"IDAT", zlib.compress(raw, 9))
    png += _png_chunk(b"IEND", b"")
    path.write_bytes(png)


def build_comparison_filter(
    row_count,
    fit="letterbox",
    led_gamma=stream.DEFAULT_LED_GAMMA,
    led_dark_floor=stream.DEFAULT_LED_DARK_FLOOR,
    rgb5_quantizer=True,
    variants=None,
    header_input_index=None,
):
    """Build a source/scaled/variant filter graph with one row per timestamp."""
    if row_count <= 0:
        raise ValueError("at least one timestamp is required")
    if variants is None:
        variants = [
            ComparisonVariant(
                "post-processed",
                led_gamma,
                led_dark_floor,
                rgb5_quantizer,
            )
        ]
    if not variants:
        raise ValueError("at least one post-processing variant is required")

    scaled_source = stream.build_video_filter(
        fit,
        led_gamma=led_gamma,
        led_dark_floor=led_dark_floor,
        rgb5_quantizer=rgb5_quantizer,
        scaled_source=True,
    ).removeprefix("fps={fps},")
    processed_filters = [
        stream.build_video_filter(
            fit,
            led_gamma=variant.led_gamma,
            led_dark_floor=variant.led_dark_floor,
            rgb5_quantizer=variant.rgb5_quantizer,
        ).removeprefix("fps={fps},")
        for variant in variants
    ]
    cell_scale = "scale={}:{}:flags=neighbor".format(
        CELL_WIDTH,
        CELL_HEIGHT,
    )
    if stream.DISPLAY_ROTATION == 180:
        cell_scale += ",hflip,vflip"
    cell_scale += ",setsar=1"
    source_scale = (
        "scale={width}:{height}:force_original_aspect_ratio=decrease:"
        "flags=lanczos,"
        "pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        "format=rgb24,setsar=1"
    ).format(width=CELL_WIDTH, height=CELL_HEIGHT)

    filters = []
    rows = []
    for index in range(row_count):
        filters.append(
            "[{index}:v]split={split_count}[{labels}]".format(
                index=index,
                split_count=2 + len(variants),
                labels="][".join(
                    [
                        "source{}_in".format(index),
                        "scaled{}_in".format(index),
                    ]
                    + [
                        "processed{}_{}_in".format(index, variant_index)
                        for variant_index in range(len(variants))
                    ]
                ),
            )
        )
        filters.append(
            "[source{index}_in]trim=end_frame=1,setpts=PTS-STARTPTS,"
            "{source_scale}[source{index}]".format(
                index=index,
                source_scale=source_scale,
            )
        )
        filters.append(
            "[scaled{index}_in]trim=end_frame=1,setpts=PTS-STARTPTS,"
            "{scaled_source},{cell_scale}[scaled{index}]".format(
                index=index,
                scaled_source=scaled_source,
                cell_scale=cell_scale,
            )
        )
        output_labels = ["source{}".format(index), "scaled{}".format(index)]
        for variant_index, processed in enumerate(processed_filters):
            output_label = "processed{}_{}".format(index, variant_index)
            filters.append(
                "[processed{index}_{variant}_in]trim=end_frame=1,"
                "setpts=PTS-STARTPTS,{processed},{cell_scale}[{output_label}]".format(
                    index=index,
                    variant=variant_index,
                    processed=processed,
                    cell_scale=cell_scale,
                    output_label=output_label,
                )
            )
            output_labels.append(output_label)
        row = "row{}".format(index)
        filters.append(
            "{}hstack=inputs={}[{}]".format(
                "".join("[{}]".format(label) for label in output_labels),
                len(output_labels),
                row,
            )
        )
        rows.append("[{}]".format(row))

    grid_label = "comparison" if header_input_index is None else "comparison_grid"
    if row_count == 1:
        filters.append("{}null[{}]".format(rows[0], grid_label))
    else:
        filters.append(
            "{}vstack=inputs={}[{}]".format(
                "".join(rows),
                row_count,
                grid_label,
            )
        )
    if header_input_index is not None:
        filters.append(
            "[{index}:v]format=rgb24[comparison_header];"
            "[comparison_header][comparison_grid]vstack=inputs=2[comparison]".format(
                index=header_input_index
            )
        )
    return ";".join(filters)


def build_comparison_command(
    source,
    output,
    timestamps,
    fit="letterbox",
    led_gamma=stream.DEFAULT_LED_GAMMA,
    led_dark_floor=stream.DEFAULT_LED_DARK_FLOOR,
    rgb5_quantizer=True,
    variants=None,
    header=None,
):
    """Build the FFmpeg command that renders a comparison PNG."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg was not found in PATH")
    if not timestamps:
        raise ValueError("at least one timestamp is required")

    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for timestamp in timestamps:
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("timestamps must be non-negative and finite")
        command.extend(
            [
                "-ss",
                format_timestamp(timestamp),
                "-i",
                str(source),
            ]
        )
    header_input_index = None
    if header is not None:
        header_input_index = len(timestamps)
        command.extend(["-i", str(header)])
    command.extend(
        [
            "-filter_complex",
            build_comparison_filter(
                len(timestamps),
                fit=fit,
                led_gamma=led_gamma,
                led_dark_floor=led_dark_floor,
                rgb5_quantizer=rgb5_quantizer,
                variants=variants,
                header_input_index=header_input_index,
            ),
            "-map",
            "[comparison]",
            "-frames:v",
            "1",
            "-update",
            "1",
            "-threads",
            "1",
            str(output),
        ]
    )
    return command


def create_parser():
    parser = argparse.ArgumentParser(
        description=(
            "create a PNG with source, scaled-source, and post-processed "
            "columns"
        )
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--config",
        type=Path,
        help="TOML file defining named post-processing variants",
    )
    parser.add_argument(
        "timestamps",
        type=parse_timestamp,
        nargs="+",
        metavar="TIMESTAMP",
        help="seconds or [HH:]MM:SS; each timestamp becomes one row",
    )
    parser.add_argument(
        "--fit",
        choices=("letterbox", "crop"),
        default="letterbox",
    )
    gamma = parser.add_mutually_exclusive_group()
    gamma.add_argument(
        "--led-gamma",
        type=stream.parse_led_gamma,
        default=stream.DEFAULT_LED_GAMMA,
    )
    gamma.add_argument(
        "--no-led-gamma",
        dest="led_gamma",
        action="store_const",
        const=1,
    )
    dark_floor = parser.add_mutually_exclusive_group()
    dark_floor.add_argument(
        "--led-dark-floor",
        type=stream.parse_led_dark_floor,
        default=stream.DEFAULT_LED_DARK_FLOOR,
        metavar="LEVEL",
    )
    dark_floor.add_argument(
        "--no-led-dark-floor",
        dest="led_dark_floor",
        action="store_const",
        const=0,
    )
    parser.add_argument(
        "--rgb5-quantizer",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser


def main(argv=None):
    args = create_parser().parse_args(argv)
    if not args.source.is_file():
        raise SystemExit("movie does not exist: {}".format(args.source))
    if args.output.suffix.lower() != ".png":
        raise SystemExit("output must use a .png extension")
    if args.source.resolve() == args.output.resolve():
        raise SystemExit("output must not overwrite the source")

    variants = None
    fit = args.fit
    if args.config is not None:
        fit, variants = load_config(args.config)
    if variants is None:
        variants = [
            ComparisonVariant(
                "post-processed",
                args.led_gamma,
                args.led_dark_floor,
                args.rgb5_quantizer,
            )
        ]

    labels = ["source", "scaled source"] + [variant.name for variant in variants]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="movieportal-compare-") as temp_dir:
        header = Path(temp_dir) / "header.png"
        write_header_png(header, labels)
        command = build_comparison_command(
            args.source,
            args.output,
            args.timestamps,
            fit=fit,
            led_gamma=args.led_gamma,
            led_dark_floor=args.led_dark_floor,
            rgb5_quantizer=args.rgb5_quantizer,
            variants=variants,
            header=header,
        )
        subprocess.run(command, check=True)
    if not args.output.is_file():
        raise RuntimeError("FFmpeg produced no comparison image")
    print(
        "Wrote {} (columns: {})".format(
            args.output,
            ", ".join(labels),
        )
    )


if __name__ == "__main__":
    main()
