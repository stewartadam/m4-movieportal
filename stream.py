# /// script
# requires-python = ">=3.11"
# dependencies = ["pyserial>=3.5"]
# ///
"""Stream synthetic or FFmpeg-decoded RGB565 frames over USB."""

import argparse
from collections import deque
from fractions import Fraction
import math
from pathlib import Path
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time

from mpv_ipc import MpvIpcClient, MpvIpcError
import protocol
from settings import DISPLAY_ROTATION


WIDTH = 64
HEIGHT = 32
FRAME_SIZE = WIDTH * HEIGHT * 2
RGB24_FRAME_SIZE = WIDTH * HEIGHT * 3
DEFAULT_FPS = Fraction(24, 1)
PREBUFFER_FRAMES = 3
DEFAULT_IINA_SOCKET = "/tmp/m4-movieportal-mpv.sock"
DEFAULT_LED_GAMMA = 2.2
RGB5_MAX = 31
RGB_WORK_MAX = 65535
DEFAULT_LED_DARK_FLOOR = 0
SHADOW_CHROMA_MAX_LEVEL = 4
SHADOW_CHROMA_DIVISOR = 2
DARK_FLOOR_SUBSTEPS = 64


def parse_frame_rate(value):
    """Parse an integer, decimal, or rational frame rate."""
    try:
        rate = Fraction(value)
    except (ValueError, ZeroDivisionError) as error:
        raise argparse.ArgumentTypeError("invalid frame rate") from error
    if rate <= 0 or rate.numerator > 65535 or rate.denominator > 65535:
        raise argparse.ArgumentTypeError("frame rate is out of range")
    return rate


def parse_led_gamma(value):
    """Parse a positive, finite LED gamma exponent."""
    try:
        gamma = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("LED gamma must be a number") from error
    if not math.isfinite(gamma) or gamma <= 0:
        raise argparse.ArgumentTypeError("LED gamma must be positive and finite")
    return gamma


def parse_led_dark_floor(value):
    """Parse a five-bit level used as the pixel black cutoff."""
    try:
        level = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "LED dark floor must be an integer"
        ) from error
    if not 0 <= level <= RGB5_MAX:
        raise argparse.ArgumentTypeError(
            "LED dark floor must be between 0 and {}".format(RGB5_MAX)
        )
    return level


def build_rgb5_levels(dark_floor=DEFAULT_LED_DARK_FLOOR):
    """Build stable five-bit levels with correlated shadow chroma."""
    rounded = {
        component: "floor({}(X,Y)*{}/{}+0.5)".format(
            component,
            RGB5_MAX,
            RGB_WORK_MAX,
        )
        for component in "rgb"
    }
    peak_level = "max(max({r},{g}),{b})".format(**rounded)
    luma = "(0.2126*r(X,Y)+0.7152*g(X,Y)+0.0722*b(X,Y))"
    rounded_luma = (
        "floor((0.2126*r(X,Y)+0.7152*g(X,Y)+0.0722*b(X,Y))"
        "*{}/{}+0.5)".format(RGB5_MAX, RGB_WORK_MAX)
    )
    luma_signal = "gte({}*{}*{},{}*{})".format(
        luma,
        RGB5_MAX,
        DARK_FLOOR_SUBSTEPS,
        RGB_WORK_MAX,
        dark_floor + 1,
    )
    luma_level = "if({},max({},1),{})".format(
        luma_signal,
        rounded_luma,
        rounded_luma,
    )
    levels = {}
    for component in "rgb":
        chroma = "(({}(X,Y)-{})*{}/{}/{})".format(
            component,
            luma,
            RGB5_MAX,
            RGB_WORK_MAX,
            SHADOW_CHROMA_DIVISOR,
        )
        chroma_level = (
            "if(gte({chroma},0),"
            "floor({chroma}+0.5),ceil({chroma}-0.5))"
        ).format(chroma=chroma)
        shadow_level = "clip({}+{},0,{})".format(
            luma_level,
            chroma_level,
            RGB5_MAX,
        )
        levels[component] = "if(lte({},{}),{},{})".format(
            peak_level,
            SHADOW_CHROMA_MAX_LEVEL,
            shadow_level,
            rounded[component],
        )

    return levels


def build_rgb5_quantizer(component, levels):
    """Build a stable five-bit quantizer for one RGB component."""
    return "{}*{}/{}".format(
        levels[component],
        RGB_WORK_MAX,
        RGB5_MAX,
    )


def build_video_filter(
    fit,
    led_gamma=DEFAULT_LED_GAMMA,
    led_dark_floor=DEFAULT_LED_DARK_FLOOR,
    rgb5_quantizer=True,
    scaled_source=False,
):
    """Build the 64x32 scale/crop and LED transfer filter graph."""
    if not math.isfinite(led_gamma) or led_gamma <= 0:
        raise ValueError("LED gamma must be positive and finite")
    if (
        not isinstance(led_dark_floor, int)
        or not 0 <= led_dark_floor <= RGB5_MAX
    ):
        raise ValueError(
            "LED dark floor must be an integer between 0 and {}".format(
                RGB5_MAX
            )
        )
    if scaled_source:
        led_gamma = 1
        rgb5_quantizer = False
    filters = "fps={fps},format=gbrp16le"
    if fit == "crop":
        filters += (
            ","
            "scale=64:32:force_original_aspect_ratio=increase:flags=area,"
            "crop=64:32"
        )
    else:
        filters += (
            ","
            "scale=64:32:force_original_aspect_ratio=decrease:flags=area,"
            "pad=64:32:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    if led_gamma != 1:
        gamma = "{:g}".format(led_gamma)
        expression = "pow(val/maxval,{})*maxval".format(gamma)
        filters += (
            ",lutrgb=r='{expression}':g='{expression}':b='{expression}'"
        ).format(expression=expression)
    if rgb5_quantizer:
        levels = build_rgb5_levels(led_dark_floor)
        expressions = {
            component: build_rgb5_quantizer(component, levels)
            for component in "rgb"
        }
        filters += ",geq=r='{r}':g='{g}':b='{b}':i=nearest".format(
            **expressions
        )
    filters += ",format=rgb24"
    if DISPLAY_ROTATION == 180:
        filters += ",hflip,vflip"
    elif DISPLAY_ROTATION != 0:
        raise ValueError("DISPLAY_ROTATION must be 0 or 180")
    return filters


def build_ffmpeg_command(
    source,
    frame_rate,
    fit="letterbox",
    duration=None,
    hwaccel="auto",
    start_time=0,
    max_frames=None,
    led_gamma=DEFAULT_LED_GAMMA,
    led_dark_floor=DEFAULT_LED_DARK_FLOOR,
    rgb5_quantizer=True,
    scaled_source=False,
):
    """Build an FFmpeg RGB24 command for deterministic host-side packing."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg was not found in PATH")

    command = [ffmpeg, "-hide_banner", "-loglevel", "warning"]
    if hwaccel != "none":
        command.extend(["-hwaccel", hwaccel])
    if start_time < 0:
        raise ValueError("start time cannot be negative")
    if start_time:
        command.extend(["-ss", str(start_time)])
    if duration is not None:
        command.extend(["-t", str(duration)])
    if max_frames is not None and max_frames <= 0:
        raise ValueError("maximum frame count must be positive")
    command.extend(["-i", str(source), "-map", "0:v:0", "-an", "-sn", "-dn"])
    fps_text = "{}/{}".format(frame_rate.numerator, frame_rate.denominator)
    command.extend(
        [
            "-vf",
            build_video_filter(
                fit,
                led_gamma,
                led_dark_floor,
                rgb5_quantizer,
                scaled_source,
            ).format(fps=fps_text),
            "-pix_fmt",
            "rgb24",
        ]
    )
    if max_frames is not None:
        command.extend(["-frames:v", str(max_frames)])
    command.extend(
        [
            "-f",
            "rawvideo",
            "pipe:1",
        ]
    )
    return command


def read_exact(stream, size):
    """Read exactly size bytes, returning b'' on clean EOF."""
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if remaining == size:
                return b""
            raise EOFError("stream ended in the middle of a frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def pack_rgb565(rgb24):
    """Pack RGB24 without the color-channel dithering used by swscale."""
    if len(rgb24) != RGB24_FRAME_SIZE:
        raise ValueError(
            "RGB24 frame must contain {} bytes".format(RGB24_FRAME_SIZE)
        )

    packed = bytearray(FRAME_SIZE)
    output_offset = 0
    for input_offset in range(0, RGB24_FRAME_SIZE, 3):
        red = (rgb24[input_offset] * 31 + 127) // 255
        green = (rgb24[input_offset + 1] * 63 + 127) // 255
        blue = (rgb24[input_offset + 2] * 31 + 127) // 255
        pixel = (red << 11) | (green << 5) | blue
        packed[output_offset] = pixel & 0xFF
        packed[output_offset + 1] = pixel >> 8
        output_offset += 2
    return bytes(packed)


def ffmpeg_frames(
    source,
    frame_rate,
    fit,
    duration,
    hwaccel,
    start_time=0,
    max_frames=None,
    led_gamma=DEFAULT_LED_GAMMA,
    led_dark_floor=DEFAULT_LED_DARK_FLOOR,
    rgb5_quantizer=True,
    scaled_source=False,
):
    """Yield decoded frames from FFmpeg."""
    command = build_ffmpeg_command(
        source,
        frame_rate,
        fit,
        duration,
        hwaccel,
        start_time,
        max_frames,
        led_gamma,
        led_dark_floor,
        rgb5_quantizer,
        scaled_source,
    )
    print("Decoder:", " ".join(command), file=sys.stderr)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    completed = False
    try:
        while True:
            rgb24 = read_exact(process.stdout, RGB24_FRAME_SIZE)
            if not rgb24:
                break
            yield pack_rgb565(rgb24)
        completed = True
    finally:
        stopping = not completed and process.poll() is None
        if stopping:
            process.terminate()
        if process.stdout is not None:
            process.stdout.close()
        if stopping:
            try:
                return_code = process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                return_code = process.wait()
        else:
            return_code = process.wait()
        if completed and return_code:
            raise RuntimeError("ffmpeg exited with status {}".format(return_code))


def build_ffplay_command(
    source,
    duration=None,
    delay_ms=protocol.PREROLL_MS,
    volume=100,
    start_time=0,
):
    """Build a headless host-audio playback command."""
    ffplay = shutil.which("ffplay")
    if ffplay is None:
        raise RuntimeError(
            "ffplay was not found in PATH; use --no-audio or install ffplay"
        )
    if delay_ms < 0:
        raise ValueError("audio delay cannot be negative")
    if not 0 <= volume <= 100:
        raise ValueError("audio volume must be between 0 and 100")
    if start_time < 0:
        raise ValueError("start time cannot be negative")

    command = [
        ffplay,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nodisp",
        "-autoexit",
        "-sync",
        "audio",
        "-vn",
        "-volume",
        str(volume),
    ]
    if duration is not None:
        command.extend(["-t", str(duration)])
    if start_time:
        command.extend(["-ss", str(start_time)])
    if delay_ms:
        command.extend(["-af", "adelay={}:all=1".format(delay_ms)])
    command.append(str(source))
    return command


class AudioPlayer:
    """Manage an ffplay process tied to one video stream."""

    def __init__(
        self,
        source,
        duration=None,
        delay_ms=protocol.PREROLL_MS,
        volume=100,
        start_time=0,
    ):
        self.command = build_ffplay_command(
            source,
            duration=duration,
            delay_ms=delay_ms,
            volume=volume,
            start_time=start_time,
        )
        self.process = None

    def start(self):
        if self.process is not None:
            raise RuntimeError("audio player is already started")
        print("Audio:", " ".join(self.command), file=sys.stderr)
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.DEVNULL,
        )

    def finish(self, timeout=2):
        if self.process is None:
            return
        try:
            return_code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop()
            return
        if return_code:
            raise RuntimeError(
                "ffplay exited with status {}".format(return_code)
            )

    def stop(self):
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


class PlaybackInterrupted(Exception):
    """Restart or suspend the panel stream after an IINA state change."""


class PausedSeekRequested(PlaybackInterrupted):
    """Present a new still frame while IINA remains paused."""


class PlayerEnded(Exception):
    """Stop following IINA because its current file ended."""


class IinaPlaybackControl:
    """Coordinate IINA's mpv clock with restartable panel sessions."""

    PAUSE_OBSERVER_ID = 1

    def __init__(self, ipc, preroll_ms=protocol.PREROLL_MS):
        self.ipc = ipc
        self.preroll_seconds = preroll_ms / 1000.0
        self._expected_pause = deque()
        self._release_timer = None
        self._errors = queue.Queue()
        self._initial_pause_event = True
        self._paused_previewed = False
        self.sync_held = False
        self.resume_after_sync = False
        self.ipc.observe_property(self.PAUSE_OBSERVER_ID, "pause")

    def _set_pause(self, paused):
        self._expected_pause.append(paused)
        self.ipc.set_property("pause", paused)

    def _cancel_release(self):
        if self._release_timer is not None:
            self._release_timer.cancel()
            self._release_timer = None

    def _hold_for_resync(self):
        self._cancel_release()
        was_sync_held = self.sync_held
        paused = bool(self.ipc.get_property("pause"))
        if paused and not was_sync_held:
            self.resume_after_sync = False
            self.sync_held = False
            return False
        self.resume_after_sync = not paused or (
            was_sync_held and self.resume_after_sync
        )
        self.sync_held = True
        if not paused:
            self._set_pause(True)
        return True

    def prepare(self):
        """Pause IINA and return the source and stable seek position."""
        if not self.sync_held:
            if self.ipc.get_property("pause"):
                return None
            self.resume_after_sync = True
            self.sync_held = True
            self._set_pause(True)

        source = self.ipc.get_property("path")
        position = self.ipc.get_property("time-pos") or 0
        if not source:
            raise RuntimeError("IINA has no open movie")
        return source, max(0, position)

    def _release(self):
        self._release_timer = None
        if not self.sync_held or not self.resume_after_sync:
            return
        try:
            self._set_pause(False)
            self.sync_held = False
        except Exception as error:
            self._errors.put(error)

    def playback_started(self):
        """Resume IINA when the board's fixed preroll has elapsed."""
        self._cancel_release()
        if self.resume_after_sync:
            self._release_timer = threading.Timer(
                self.preroll_seconds,
                self._release,
            )
            self._release_timer.daemon = True
            self._release_timer.start()

    def _handle_pause_change(self, paused):
        if self._initial_pause_event:
            self._initial_pause_event = False
            return
        if self._expected_pause and paused == self._expected_pause[0]:
            self._expected_pause.popleft()
            return

        self._cancel_release()
        self.sync_held = False
        self.resume_after_sync = not paused
        raise PlaybackInterrupted("IINA pause state changed")

    @staticmethod
    def _raise_connection_event(event):
        event_name = event.get("event")
        if event_name == "ipc-error":
            raise MpvIpcError(event.get("message", "IINA IPC failed"))
        if event_name == "shutdown":
            raise MpvIpcError("IINA closed its mpv IPC connection")

    def poll(self):
        """Raise when the active panel session must stop or restart."""
        try:
            error = self._errors.get_nowait()
        except queue.Empty:
            error = None
        if error is not None:
            raise error

        pause_interruption = None
        seek_interruption = None
        position_changed = False
        player_ended = False
        while True:
            while True:
                event = self.ipc.next_event()
                if event is None:
                    break
                self._raise_connection_event(event)
                event_name = event.get("event")
                if (
                    event_name == "property-change"
                    and event.get("name") == "pause"
                ):
                    try:
                        self._handle_pause_change(bool(event.get("data")))
                    except PlaybackInterrupted as error:
                        pause_interruption = error
                elif event_name in ("seek", "start-file"):
                    position_changed = True
                elif event_name == "end-file":
                    self._cancel_release()
                    player_ended = True

            if player_ended:
                break
            if position_changed and seek_interruption is None:
                if not self._hold_for_resync():
                    seek_interruption = PausedSeekRequested()
                else:
                    seek_interruption = PlaybackInterrupted(
                        "IINA playback position changed"
                    )
                # The synchronous property commands above form an IPC barrier.
                # Drain events queued before their responses before restarting.
                continue
            break

        if player_ended:
            raise PlayerEnded()
        if seek_interruption is not None:
            raise seek_interruption
        if pause_interruption is not None:
            raise pause_interruption

    def wait_until_playing(self):
        """Wait without busy-spinning until IINA has a playing movie."""
        announced = False
        pending_event = None
        seek_pending = False
        while True:
            while True:
                if pending_event is None:
                    event = self.ipc.next_event()
                else:
                    event = pending_event
                    pending_event = None
                if event is None:
                    break
                self._raise_connection_event(event)
                if event.get("event") == "end-file":
                    raise PlayerEnded()
                if event.get("event") in ("seek", "start-file"):
                    seek_pending = True
                if (
                    event.get("event") == "property-change"
                    and event.get("name") == "pause"
                ):
                    paused = bool(event.get("data"))
                    if self._initial_pause_event:
                        self._initial_pause_event = False
                    elif (
                        self._expected_pause
                        and paused == self._expected_pause[0]
                    ):
                        self._expected_pause.popleft()

            try:
                source = self.ipc.get_property("path")
            except MpvIpcError:
                source = None
            paused = bool(self.ipc.get_property("pause"))

            # Property commands form an IPC barrier: by the time their
            # responses arrive, the reader has queued every earlier seek
            # event. Drain those events before acting on the batch.
            pending_event = self.ipc.next_event()
            if pending_event is not None:
                continue
            if source and paused and (
                seek_pending or not self._paused_previewed
            ):
                self._paused_previewed = True
                raise PausedSeekRequested()
            if source and not paused:
                self._paused_previewed = False
                return
            if not source:
                self._paused_previewed = False
            if not announced:
                if source:
                    message = (
                        "IINA is paused; displayed the current frame and "
                        "waiting for playback."
                    )
                else:
                    message = "IINA has no open movie; waiting for playback."
                print(message, file=sys.stderr)
                announced = True

            pending_event = self.ipc.next_event(timeout=0.25)

    def restore_player(self):
        """Undo an internal synchronization pause before exiting."""
        self._cancel_release()
        if self.sync_held and self.resume_after_sync:
            try:
                self._set_pause(False)
            except Exception:
                pass
        self.sync_held = False


def test_pattern_frames(frame_rate, seconds):
    """Yield moving RGB565 color bars."""
    frame_count = round(float(frame_rate) * seconds)
    for frame_number in range(frame_count):
        frame = bytearray(FRAME_SIZE)
        offset = 0
        for y in range(HEIGHT):
            for x in range(WIDTH):
                source_x = WIDTH - 1 - x if DISPLAY_ROTATION == 180 else x
                source_y = HEIGHT - 1 - y if DISPLAY_ROTATION == 180 else y
                red = ((source_x + frame_number) >> 1) & 0x1F
                green = ((source_y * 2 + frame_number) & 0x3F)
                blue = (
                    (source_x + source_y + frame_number * 2) >> 1
                ) & 0x1F
                color = (red << 11) | (green << 5) | blue
                frame[offset] = color & 0xFF
                frame[offset + 1] = color >> 8
                offset += 2
        yield frame


def _serial_module():
    try:
        import serial
        from serial.tools import list_ports
    except ImportError as error:
        raise RuntimeError(
            "pyserial is required; run this file with 'uv run --script stream.py'"
        ) from error
    return serial, list_ports


def discover_port():
    """Select the likely second CircuitPython CDC interface."""
    _serial, list_ports = _serial_module()
    candidates = [
        port
        for port in list_ports.comports()
        if "usbmodem" in port.device.lower()
        or "circuitpython" in (port.description or "").lower()
    ]
    if not candidates:
        raise RuntimeError("no CircuitPython USB serial ports were found")
    candidates.sort(key=lambda port: port.device)
    if len(candidates) == 1:
        raise RuntimeError(
            "only one CircuitPython serial port was found; install boot.py "
            "and reset the board to enable the data port"
        )
    return candidates[-1].device


def open_port(port=None):
    """Open the dedicated binary CDC port."""
    serial, _list_ports = _serial_module()
    selected = port or discover_port()
    print("USB data port:", selected, file=sys.stderr)
    connection = serial.Serial(
        selected,
        baudrate=115200,
        timeout=3,
        write_timeout=10,
    )
    # Give any packet queued before the previous host disconnected time to
    # arrive, then begin the new handshake from an empty host-side buffer.
    time.sleep(0.25)
    connection.reset_input_buffer()
    return connection


def write_all(stream, data):
    """Write a complete packet or fail."""
    offset = 0
    while offset < len(data):
        written = stream.write(data[offset:])
        if not written:
            raise RuntimeError("USB write timed out")
        offset += written


def send_packet(stream, message_type, payload=b"", sequence=0, pts_ms=0):
    write_all(
        stream,
        protocol.pack_packet(message_type, payload, sequence, pts_ms),
    )


def receive_packet(stream):
    header = read_exact(stream, protocol.HEADER_SIZE)
    if not header:
        raise RuntimeError("device did not respond")
    message_type, payload_length, sequence, pts_ms = protocol.unpack_header(header)
    payload = read_exact(stream, payload_length)
    return message_type, sequence, pts_ms, payload


def expect_packet(stream, expected_type):
    event = receive_packet(stream)
    if event[0] == protocol.MSG_ERROR:
        raise RuntimeError("device error: " + event[3].decode("utf-8", "replace"))
    if event[0] != expected_type:
        raise RuntimeError(
            "expected device message {}, received {}".format(
                expected_type, event[0]
            )
        )
    return event


def stream_frames(
    stream,
    frames,
    frame_rate,
    start_callback=None,
    poll_callback=None,
    started_callback=None,
):
    """Configure the device, prebuffer frames, and stream until EOF."""
    send_packet(stream, protocol.MSG_HELLO)
    caps_event = expect_packet(stream, protocol.MSG_CAPS)
    width, height, frame_size, capacity, free_heap = struct.unpack(
        protocol.CAPS_FORMAT, caps_event[3]
    )
    if (width, height, frame_size) != (WIDTH, HEIGHT, FRAME_SIZE):
        raise RuntimeError("device capabilities do not match the streamer")
    print(
        "Device: {}x{}, {} frame slots, {} bytes free".format(
            width, height, capacity, free_heap
        ),
        file=sys.stderr,
    )

    config = struct.pack(
        protocol.CONFIG_FORMAT,
        WIDTH,
        HEIGHT,
        frame_rate.numerator,
        frame_rate.denominator,
    )
    send_packet(stream, protocol.MSG_CONFIG, config)
    expect_packet(stream, protocol.MSG_READY)

    iterator = iter(frames)
    sequence = 0
    started = False
    start_time = time.monotonic()
    try:
        for frame in iterator:
            if poll_callback is not None:
                poll_callback()
            if len(frame) != FRAME_SIZE:
                raise ValueError("producer returned an incorrectly sized frame")
            pts_ms = round(
                sequence * 1000 * frame_rate.denominator / frame_rate.numerator
            )
            send_packet(
                stream,
                protocol.MSG_FRAME,
                frame,
                sequence=sequence,
                pts_ms=pts_ms,
            )
            sequence += 1
            if not started and sequence >= min(PREBUFFER_FRAMES, capacity - 1):
                if start_callback is not None:
                    start_callback()
                send_packet(stream, protocol.MSG_START)
                started = True
                if started_callback is not None:
                    started_callback()
        if not started:
            if start_callback is not None:
                start_callback()
            send_packet(stream, protocol.MSG_START)
            if started_callback is not None:
                started_callback()
        send_packet(stream, protocol.MSG_END)
        status_event = expect_packet(stream, protocol.MSG_STATUS)
    except BaseException:
        close_iterator = getattr(iterator, "close", None)
        if close_iterator is not None:
            try:
                close_iterator()
            except Exception:
                pass
        try:
            send_packet(stream, protocol.MSG_STOP)
            expect_packet(stream, protocol.MSG_STATUS)
        except Exception:
            pass
        raise

    received, displayed, dropped, underruns, free_heap, free_slots = struct.unpack(
        protocol.STATUS_FORMAT, status_event[3]
    )
    elapsed = time.monotonic() - start_time
    print(
        (
            "Complete: sent={}, received={}, displayed={}, dropped={}, "
            "underruns={}, elapsed={:.2f}s, free_heap={}, free_slots={}"
        ).format(
            sequence,
            received,
            displayed,
            dropped,
            underruns,
            elapsed,
            free_heap,
            free_slots,
        ),
        file=sys.stderr,
    )
    return {
        "sent": sequence,
        "received": received,
        "displayed": displayed,
        "dropped": dropped,
        "underruns": underruns,
    }


def add_video_preprocessing_arguments(parser):
    """Add the LED transfer controls shared by movie playback commands."""
    gamma = parser.add_mutually_exclusive_group()
    gamma.add_argument(
        "--led-gamma",
        type=parse_led_gamma,
        default=DEFAULT_LED_GAMMA,
        help="LED gamma exponent (default: %(default)s)",
    )
    gamma.add_argument(
        "--no-led-gamma",
        dest="led_gamma",
        action="store_const",
        const=1,
        help="disable LED gamma correction",
    )
    parser.add_argument(
        "--rgb5-quantizer",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="apply the custom five-bit shadow quantizer (default: enabled)",
    )
    dark_floor = parser.add_mutually_exclusive_group()
    dark_floor.add_argument(
        "--led-dark-floor",
        type=parse_led_dark_floor,
        default=DEFAULT_LED_DARK_FLOOR,
        metavar="LEVEL",
        help="pixel black cutoff, 0-31 (default: %(default)s)",
    )
    dark_floor.add_argument(
        "--no-led-dark-floor",
        dest="led_dark_floor",
        action="store_const",
        const=0,
        help="select the weakest LED dark cutoff",
    )
    parser.add_argument(
        "--scaled-source",
        action="store_true",
        help=(
            "send only the scaled source image, bypassing LED gamma and "
            "the custom five-bit quantizer"
        ),
    )


def create_parser():
    parser = argparse.ArgumentParser(
        description="Stream RGB565 video to an M4 MoviePortal"
    )
    parser.add_argument("--port", help="dedicated CircuitPython data CDC port")
    parser.add_argument(
        "--fps",
        type=parse_frame_rate,
        default=DEFAULT_FPS,
        help="output frame rate (default: 24)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    test_parser = subparsers.add_parser("test", help="show a generated test pattern")
    test_parser.add_argument("--seconds", type=float, default=10)

    play_parser = subparsers.add_parser("play", help="decode and stream a movie")
    play_parser.add_argument("source", type=Path)
    play_parser.add_argument(
        "--fit", choices=("letterbox", "crop"), default="letterbox"
    )
    play_parser.add_argument(
        "--duration",
        type=float,
        help="stop after this many seconds (useful for testing)",
    )
    play_parser.add_argument(
        "--start",
        type=float,
        default=0,
        help="start at this many seconds into the movie",
    )
    play_parser.add_argument(
        "--hwaccel",
        default="auto",
        help="FFmpeg hardware decoder: auto, none, or a platform name",
    )
    add_video_preprocessing_arguments(play_parser)
    play_parser.add_argument(
        "--no-audio",
        action="store_true",
        help="disable host audio playback",
    )
    play_parser.add_argument(
        "--audio-delay-ms",
        type=int,
        default=protocol.PREROLL_MS,
        help="delay host audio to match video preroll (default: 250)",
    )
    play_parser.add_argument(
        "--volume",
        type=int,
        default=100,
        help="host audio volume from 0 to 100 (default: 100)",
    )

    iina_parser = subparsers.add_parser(
        "iina",
        help="follow the movie, seek, and pause controls in IINA",
    )
    iina_parser.add_argument(
        "--socket",
        default=DEFAULT_IINA_SOCKET,
        help="IINA mpv IPC socket (default: %(default)s)",
    )
    iina_parser.add_argument(
        "--fit", choices=("letterbox", "crop"), default="letterbox"
    )
    iina_parser.add_argument(
        "--hwaccel",
        default="auto",
        help="FFmpeg hardware decoder: auto, none, or a platform name",
    )
    add_video_preprocessing_arguments(iina_parser)
    return parser


def stream_from_iina(args):
    """Follow IINA's current file and restart FFmpeg after timeline changes."""
    try:
        ipc_client = MpvIpcClient(args.socket)
    except OSError as error:
        raise RuntimeError(
            "could not connect to IINA at {}: {}".format(args.socket, error)
        ) from error

    with ipc_client as ipc:
        control = IinaPlaybackControl(ipc)
        try:
            with open_port(args.port) as device:
                preview_pending = False
                while True:
                    preview = preview_pending
                    preview_pending = False
                    if not preview and not control.sync_held:
                        try:
                            control.wait_until_playing()
                        except PausedSeekRequested:
                            preview = True
                        except PlayerEnded:
                            return

                    if preview:
                        source = control.ipc.get_property("path")
                        start_time = max(
                            0,
                            control.ipc.get_property("time-pos") or 0,
                        )
                    else:
                        prepared = control.prepare()
                        if prepared is None:
                            continue
                        source, start_time = prepared
                    print(
                        "IINA{}: {} at {:.3f}s".format(
                            " preview" if preview else "",
                            source,
                            start_time,
                        ),
                        file=sys.stderr,
                    )
                    frames = ffmpeg_frames(
                        source,
                        args.fps,
                        args.fit,
                        None,
                        args.hwaccel,
                        start_time=start_time,
                        max_frames=1 if preview else None,
                        led_gamma=args.led_gamma,
                        led_dark_floor=args.led_dark_floor,
                        rgb5_quantizer=args.rgb5_quantizer,
                        scaled_source=args.scaled_source,
                    )
                    try:
                        stream_frames(
                            device,
                            frames,
                            args.fps,
                            poll_callback=control.poll,
                            started_callback=(
                                None
                                if preview
                                else control.playback_started
                            ),
                        )
                    except PausedSeekRequested:
                        preview_pending = True
                        continue
                    except PlaybackInterrupted:
                        continue
                    except PlayerEnded:
                        return
                    finally:
                        frames.close()
                    if preview:
                        continue
                    return
        finally:
            control.restore_player()


def main(argv=None):
    args = create_parser().parse_args(argv)
    if args.command == "iina":
        stream_from_iina(args)
        return

    audio = None
    if args.command == "test":
        if args.seconds <= 0:
            raise SystemExit("--seconds must be positive")
        frames = test_pattern_frames(args.fps, args.seconds)
        start_callback = None
    else:
        if not args.source.is_file():
            raise SystemExit("movie does not exist: {}".format(args.source))
        if args.duration is not None and args.duration <= 0:
            raise SystemExit("--duration must be positive")
        if args.start < 0:
            raise SystemExit("--start cannot be negative")
        if args.audio_delay_ms < 0:
            raise SystemExit("--audio-delay-ms cannot be negative")
        if not 0 <= args.volume <= 100:
            raise SystemExit("--volume must be between 0 and 100")
        frames = ffmpeg_frames(
            args.source,
            args.fps,
            args.fit,
            args.duration,
            args.hwaccel,
            start_time=args.start,
            led_gamma=args.led_gamma,
            led_dark_floor=args.led_dark_floor,
            rgb5_quantizer=args.rgb5_quantizer,
            scaled_source=args.scaled_source,
        )
        if args.no_audio:
            start_callback = None
        else:
            audio = AudioPlayer(
                args.source,
                duration=args.duration,
                delay_ms=args.audio_delay_ms,
                volume=args.volume,
                start_time=args.start,
            )
            start_callback = audio.start

    succeeded = False
    try:
        with open_port(args.port) as stream:
            stream_frames(
                stream,
                frames,
                args.fps,
                start_callback=start_callback,
            )
        succeeded = True
    finally:
        close_frames = getattr(frames, "close", None)
        if close_frames is not None:
            close_frames()
        if audio is not None:
            if succeeded:
                audio.finish()
            else:
                audio.stop()


def run(argv=None):
    """Run the command-line interface and handle user cancellation quietly."""
    try:
        main(argv)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
