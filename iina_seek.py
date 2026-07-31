"""Cycle IINA through timestamps loaded from a TOML configuration.

IINA must be configured with the mpv IPC socket documented in README.md.
"""

import argparse
import math
import sys
import time
from pathlib import Path
import tomllib

from compare import parse_timestamp
from mpv_ipc import MpvIpcClient, MpvIpcError


DEFAULT_SOCKET = "/tmp/m4-movieportal-mpv.sock"
DEFAULT_DWELL_SECONDS = 3.0
DEFAULT_CONFIG = Path("comparison.big-lebowski.toml")


def load_timestamps(path):
    """Load and validate the timestamps array from a TOML config."""
    try:
        with path.open("rb") as config_file:
            config = tomllib.load(config_file)
    except OSError as error:
        raise ValueError("could not read timestamp config: {}".format(error)) from error
    except tomllib.TOMLDecodeError as error:
        raise ValueError("invalid timestamp config: {}".format(error)) from error

    timestamps = config.get("timestamps") if isinstance(config, dict) else None
    if not isinstance(timestamps, list) or not timestamps:
        raise ValueError("timestamp config must contain a non-empty timestamps array")

    try:
        return tuple(parse_timestamp(str(value)) for value in timestamps)
    except argparse.ArgumentTypeError as error:
        raise ValueError("timestamp config contains an invalid timestamp") from error


def create_parser():
    parser = argparse.ArgumentParser(
        description="Seek IINA through timestamps from a TOML config."
    )
    parser.add_argument(
        "--socket",
        default=DEFAULT_SOCKET,
        help="IINA's mpv IPC socket (default: %(default)s)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="TOML config containing timestamps (default: %(default)s)",
    )
    parser.add_argument(
        "--dwell",
        type=float,
        default=DEFAULT_DWELL_SECONDS,
        metavar="SECONDS",
        help="seconds to remain at each checkpoint (default: %(default)s)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="restart at the first checkpoint after the last one",
    )
    return parser


def run(ipc, dwell, timestamps, loop=False, sleep=time.sleep):
    """Seek through *timestamps* until complete or interrupted."""
    if not math.isfinite(dwell) or dwell < 0:
        raise ValueError("dwell must be a finite, non-negative number")
    if not timestamps:
        raise ValueError("at least one timestamp is required")

    while True:
        for index, timestamp in enumerate(timestamps, 1):
            ipc.command("seek", timestamp, "absolute", "exact")
            print(
                "[{}/{}] {:.3f}s".format(
                    index, len(timestamps), timestamp
                ),
                flush=True,
            )
            sleep(dwell)
        if not loop:
            return


def main(argv=None):
    args = create_parser().parse_args(argv)
    try:
        timestamps = load_timestamps(args.config)
        with MpvIpcClient(args.socket) as ipc:
            run(ipc, args.dwell, timestamps, loop=args.loop)
    except (OSError, MpvIpcError, ValueError) as error:
        print("iina-seek: {}".format(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\niina-seek: stopped", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
