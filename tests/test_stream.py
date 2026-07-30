"""Tests for the host-side movie streamer."""

from fractions import Fraction
from io import StringIO
from pathlib import Path
import queue
import struct
import subprocess
import unittest
from unittest.mock import MagicMock, call, patch

import stream


class StreamTests(unittest.TestCase):
    class FakeDeviceStream:
        def __init__(self, responses):
            self.responses = bytearray(responses)
            self.written = bytearray()

        def read(self, size):
            result = self.responses[:size]
            del self.responses[:size]
            return bytes(result)

        def write(self, data):
            self.written.extend(data)
            return len(data)

    class FakeIpc:
        def __init__(self):
            self.properties = {
                "path": "/tmp/movie.mkv",
                "pause": False,
                "time-pos": 12.5,
            }
            self.events = queue.Queue()

        def observe_property(self, _observer_id, name):
            self.events.put(
                {
                    "event": "property-change",
                    "name": name,
                    "data": self.properties[name],
                }
            )

        def get_property(self, name):
            return self.properties[name]

        def set_property(self, name, value):
            self.properties[name] = value
            self.events.put(
                {
                    "event": "property-change",
                    "name": name,
                    "data": value,
                }
            )

        def next_event(self, timeout=None):
            try:
                if timeout is None:
                    return self.events.get_nowait()
                return self.events.get(timeout=timeout)
            except queue.Empty:
                return None

    def test_parses_common_frame_rates(self):
        self.assertEqual(stream.parse_frame_rate("24"), Fraction(24, 1))
        self.assertEqual(
            stream.parse_frame_rate("30000/1001"),
            Fraction(30000, 1001),
        )

    @patch("stream.shutil.which", return_value="/opt/bin/ffmpeg")
    def test_ffmpeg_uses_auto_hardware_decode_and_rgb565(self, _which):
        command = stream.build_ffmpeg_command(
            Path("/tmp/movie.mkv"),
            Fraction(24, 1),
            fit="letterbox",
            duration=5,
            hwaccel="auto",
        )

        self.assertIn("-hwaccel", command)
        self.assertEqual(command[command.index("-hwaccel") + 1], "auto")
        self.assertEqual(command[command.index("-pix_fmt") + 1], "rgb565le")
        self.assertIn(
            "force_original_aspect_ratio=decrease",
            command[command.index("-vf") + 1],
        )
        self.assertIn(
            "pow(val/255,2.2)*255",
            command[command.index("-vf") + 1],
        )
        self.assertTrue(command[command.index("-vf") + 1].endswith(",hflip,vflip"))
        self.assertEqual(command[-1], "pipe:1")

    @patch("stream.shutil.which", return_value="/opt/bin/ffmpeg")
    def test_ffmpeg_can_disable_hardware_decode(self, _which):
        command = stream.build_ffmpeg_command(
            Path("/tmp/movie.mkv"),
            Fraction(15, 1),
            hwaccel="none",
        )

        self.assertNotIn("-hwaccel", command)

    @patch("stream.shutil.which", return_value="/opt/bin/ffmpeg")
    def test_ffmpeg_seeks_before_opening_the_input(self, _which):
        command = stream.build_ffmpeg_command(
            Path("/tmp/movie.mkv"),
            Fraction(24, 1),
            start_time=83.5,
        )

        self.assertEqual(command[command.index("-ss") + 1], "83.5")
        self.assertLess(command.index("-ss"), command.index("-i"))

    @patch("stream.shutil.which", return_value="/opt/bin/ffmpeg")
    def test_ffmpeg_can_decode_one_seek_preview_frame(self, _which):
        command = stream.build_ffmpeg_command(
            Path("/tmp/movie.mkv"),
            Fraction(24, 1),
            start_time=83.5,
            max_frames=1,
        )

        self.assertEqual(command[command.index("-frames:v") + 1], "1")

    @patch("stream.DISPLAY_ROTATION", 0)
    def test_filter_can_leave_frames_unrotated(self):
        self.assertNotIn("hflip", stream.build_video_filter("letterbox"))

    def test_filter_can_disable_led_gamma_correction(self):
        video_filter = stream.build_video_filter("letterbox", led_gamma=1)

        self.assertNotIn("lutrgb", video_filter)

    def test_filter_rejects_invalid_led_gamma(self):
        for gamma in (0, -1, float("nan"), float("inf")):
            with self.subTest(gamma=gamma):
                with self.assertRaisesRegex(ValueError, "LED gamma"):
                    stream.build_video_filter("letterbox", led_gamma=gamma)

    @patch("stream.DISPLAY_ROTATION", 90)
    def test_filter_rejects_unsupported_rotation(self):
        with self.assertRaisesRegex(ValueError, "DISPLAY_ROTATION"):
            stream.build_video_filter("letterbox")

    @patch("stream.shutil.which", return_value="/opt/bin/ffplay")
    def test_ffplay_uses_headless_audio_and_matching_preroll(self, _which):
        command = stream.build_ffplay_command(
            Path("/tmp/movie.mkv"),
            duration=5,
            delay_ms=250,
            volume=75,
        )

        self.assertIn("-nodisp", command)
        self.assertIn("-autoexit", command)
        self.assertIn("-vn", command)
        self.assertEqual(command[command.index("-volume") + 1], "75")
        self.assertEqual(
            command[command.index("-af") + 1],
            "adelay=250:all=1",
        )
        self.assertEqual(command[-1], "/tmp/movie.mkv")

    @patch("stream.shutil.which", return_value=None)
    def test_ffplay_reports_when_unavailable(self, _which):
        with self.assertRaisesRegex(RuntimeError, "ffplay"):
            stream.build_ffplay_command(Path("/tmp/movie.mkv"))

    @patch("stream.shutil.which", return_value="/opt/bin/ffplay")
    def test_ffplay_starts_audio_at_the_same_seek_position(self, _which):
        command = stream.build_ffplay_command(
            Path("/tmp/movie.mkv"),
            start_time=83.5,
        )

        self.assertEqual(command[command.index("-ss") + 1], "83.5")
        self.assertEqual(command[-1], "/tmp/movie.mkv")

    @patch("stream.subprocess.Popen")
    @patch("stream.shutil.which", return_value="/opt/bin/ffplay")
    def test_ffplay_shares_the_terminal_signal_group(self, _which, popen):
        player = stream.AudioPlayer(Path("/tmp/movie.mkv"))

        player.start()

        self.assertFalse(
            popen.call_args.kwargs.get("start_new_session", False)
        )

    def test_pattern_produces_complete_rgb565_frames(self):
        frames = list(stream.test_pattern_frames(Fraction(2, 1), 1))

        self.assertEqual(len(frames), 2)
        self.assertTrue(all(len(frame) == stream.FRAME_SIZE for frame in frames))

    def test_interrupted_stream_consumes_the_stop_response(self):
        class CloseableFrames:
            def __init__(self):
                self.closed = False

            def __iter__(self):
                return self

            def __next__(self):
                return bytes(stream.FRAME_SIZE)

            def close(self):
                self.closed = True

        capabilities = struct.pack(
            stream.protocol.CAPS_FORMAT,
            stream.WIDTH,
            stream.HEIGHT,
            stream.FRAME_SIZE,
            4,
            1000,
        )
        status = struct.pack(stream.protocol.STATUS_FORMAT, 0, 0, 0, 0, 900, 4)
        responses = b"".join(
            (
                stream.protocol.pack_packet(
                    stream.protocol.MSG_CAPS,
                    capabilities,
                ),
                stream.protocol.pack_packet(stream.protocol.MSG_READY),
                stream.protocol.pack_packet(
                    stream.protocol.MSG_STATUS,
                    status,
                ),
            )
        )
        device = self.FakeDeviceStream(responses)
        frames = CloseableFrames()

        def interrupt():
            raise stream.PlaybackInterrupted()

        with self.assertRaises(stream.PlaybackInterrupted):
            stream.stream_frames(
                device,
                frames,
                Fraction(24, 1),
                poll_callback=interrupt,
            )

        self.assertTrue(frames.closed)
        self.assertFalse(device.responses)
        last_header = device.written[-stream.protocol.HEADER_SIZE :]
        self.assertEqual(
            stream.protocol.unpack_header(last_header)[0],
            stream.protocol.MSG_STOP,
        )

    def test_iina_command_uses_the_default_socket(self):
        args = stream.create_parser().parse_args(["iina"])

        self.assertEqual(args.socket, stream.DEFAULT_IINA_SOCKET)
        self.assertEqual(args.led_gamma, stream.DEFAULT_LED_GAMMA)

    def test_iina_waits_for_a_movie_to_open(self):
        class OpeningIpc(self.FakeIpc):
            def __init__(self):
                super().__init__()
                self.properties["path"] = None

            def next_event(self, timeout=None):
                event = super().next_event(timeout=None)
                if event is None and timeout is not None:
                    self.properties["path"] = "/tmp/new-movie.mkv"
                    return {"event": "start-file"}
                return event

        ipc = OpeningIpc()
        control = stream.IinaPlaybackControl(ipc)
        stderr = StringIO()

        with patch("stream.sys.stderr", stderr):
            control.wait_until_playing()

        self.assertEqual(ipc.properties["path"], "/tmp/new-movie.mkv")
        self.assertEqual(
            stderr.getvalue(),
            "IINA has no open movie; waiting for playback.\n",
        )

    def test_iina_seek_holds_the_player_for_a_restart(self):
        ipc = self.FakeIpc()
        control = stream.IinaPlaybackControl(ipc)
        control.wait_until_playing()

        self.assertEqual(
            control.prepare(),
            ("/tmp/movie.mkv", 12.5),
        )
        control.poll()
        control._release()
        control.poll()
        ipc.events.put({"event": "seek"})

        with self.assertRaises(stream.PlaybackInterrupted):
            control.poll()

        self.assertTrue(ipc.properties["pause"])
        self.assertTrue(control.sync_held)
        self.assertTrue(control.resume_after_sync)

    def test_iina_coalesces_queued_seeks_before_restarting(self):
        ipc = self.FakeIpc()
        control = stream.IinaPlaybackControl(ipc)
        control.wait_until_playing()
        ipc.get_property = MagicMock(side_effect=ipc.get_property)
        for _ in range(3):
            ipc.events.put({"event": "seek"})

        with self.assertRaises(stream.PlaybackInterrupted):
            control.poll()

        control.poll()
        self.assertTrue(ipc.events.empty())
        ipc.get_property.assert_called_once_with("pause")

    def test_iina_seek_while_paused_requests_a_still_preview(self):
        ipc = self.FakeIpc()
        ipc.properties["pause"] = True
        control = stream.IinaPlaybackControl(ipc)
        for _ in range(3):
            ipc.events.put({"event": "seek"})

        with self.assertRaises(stream.PausedSeekRequested):
            control.wait_until_playing()

        self.assertFalse(control.sync_held)
        self.assertTrue(ipc.properties["pause"])
        self.assertTrue(ipc.events.empty())

    def test_iina_initial_pause_requests_one_still_preview(self):
        class ResumingIpc(self.FakeIpc):
            def __init__(self):
                super().__init__()
                self.properties["pause"] = True

            def next_event(self, timeout=None):
                event = super().next_event(timeout=None)
                if event is None and timeout is not None:
                    self.properties["pause"] = False
                    return {
                        "event": "property-change",
                        "name": "pause",
                        "data": False,
                    }
                return event

        ipc = ResumingIpc()
        control = stream.IinaPlaybackControl(ipc)

        with self.assertRaises(stream.PausedSeekRequested):
            control.wait_until_playing()

        control.wait_until_playing()
        self.assertFalse(ipc.properties["pause"])

    @patch("stream.read_exact", side_effect=KeyboardInterrupt)
    @patch("stream.subprocess.Popen")
    def test_ffmpeg_is_stopped_before_its_output_pipe_is_closed(
        self, popen, _read_exact
    ):
        process = popen.return_value
        process.poll.return_value = None
        frames = stream.ffmpeg_frames(
            Path("/tmp/movie.mkv"),
            Fraction(24, 1),
            fit="letterbox",
            duration=None,
            hwaccel="auto",
        )

        with self.assertRaises(KeyboardInterrupt):
            next(frames)

        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertLess(
            process.method_calls.index(call.terminate()),
            process.method_calls.index(call.stdout.close()),
        )

    @patch("stream.read_exact", side_effect=KeyboardInterrupt)
    @patch("stream.subprocess.Popen")
    def test_ffmpeg_is_killed_if_it_does_not_stop_after_interrupt(
        self, popen, _read_exact
    ):
        process = popen.return_value
        process.poll.return_value = None
        process.wait.side_effect = (
            subprocess.TimeoutExpired(process.args, 1),
            -9,
        )
        frames = stream.ffmpeg_frames(
            Path("/tmp/movie.mkv"),
            Fraction(24, 1),
            fit="letterbox",
            duration=None,
            hwaccel="auto",
        )

        with self.assertRaises(KeyboardInterrupt):
            next(frames)

        process.kill.assert_called_once_with()

    @patch("stream.main", side_effect=KeyboardInterrupt)
    @patch("stream.sys.stderr", new_callable=MagicMock)
    def test_run_handles_keyboard_interrupt_without_a_traceback(
        self, stderr, _main
    ):
        self.assertEqual(stream.run([]), 130)
        self.assertTrue(stderr.write.called)


if __name__ == "__main__":
    unittest.main()
