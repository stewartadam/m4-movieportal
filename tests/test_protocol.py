"""Tests for the shared USB framing protocol."""

import struct
import unittest

import protocol


class ProtocolTests(unittest.TestCase):
    def test_packet_round_trip(self):
        packet = protocol.pack_packet(
            protocol.MSG_FRAME,
            b"pixels",
            sequence=123,
            pts_ms=456,
        )

        header = protocol.unpack_header(packet[: protocol.HEADER_SIZE])

        self.assertEqual(
            header,
            (protocol.MSG_FRAME, len(b"pixels"), 123, 456),
        )
        self.assertEqual(packet[protocol.HEADER_SIZE :], b"pixels")

    def test_rejects_wrong_magic(self):
        header = struct.pack(
            protocol.HEADER_FORMAT,
            b"NOPE",
            protocol.VERSION,
            protocol.MSG_HELLO,
            0,
            0,
            0,
        )

        with self.assertRaisesRegex(ValueError, "magic"):
            protocol.unpack_header(header)

    def test_rejects_oversized_payload(self):
        with self.assertRaisesRegex(ValueError, "too large"):
            protocol.pack_header(
                protocol.MSG_FRAME,
                protocol.MAX_PAYLOAD_SIZE + 1,
            )
