"""Binary USB protocol shared by CircuitPython and the host streamer."""

import struct


MAGIC = b"M4MP"
VERSION = 1

MSG_HELLO = 1
MSG_CAPS = 2
MSG_CONFIG = 3
MSG_READY = 4
MSG_FRAME = 5
MSG_START = 6
MSG_END = 7
MSG_STOP = 8
MSG_STATUS = 9
MSG_ERROR = 10

HEADER_FORMAT = "<4sBBHII"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
CONFIG_FORMAT = "<HHHH"
CONFIG_SIZE = struct.calcsize(CONFIG_FORMAT)
CAPS_FORMAT = "<HHHHI"
CAPS_SIZE = struct.calcsize(CAPS_FORMAT)
STATUS_FORMAT = "<IIIIII"
STATUS_SIZE = struct.calcsize(STATUS_FORMAT)

MAX_PAYLOAD_SIZE = 4096
PREROLL_MS = 250


def pack_header(message_type, payload_length=0, sequence=0, pts_ms=0):
    """Pack and validate a protocol header."""
    if not 0 <= message_type <= 255:
        raise ValueError("message type must fit in one byte")
    if not 0 <= payload_length <= MAX_PAYLOAD_SIZE:
        raise ValueError("payload is too large")
    if not 0 <= sequence <= 0xFFFFFFFF:
        raise ValueError("sequence must fit in four bytes")
    if not 0 <= pts_ms <= 0xFFFFFFFF:
        raise ValueError("timestamp must fit in four bytes")
    return struct.pack(
        HEADER_FORMAT,
        MAGIC,
        VERSION,
        message_type,
        payload_length,
        sequence,
        pts_ms,
    )


def pack_packet(message_type, payload=b"", sequence=0, pts_ms=0):
    """Pack a complete protocol packet."""
    return pack_header(message_type, len(payload), sequence, pts_ms) + payload


def unpack_header(buffer):
    """Unpack a header and reject incompatible or corrupt input."""
    if len(buffer) != HEADER_SIZE:
        raise ValueError("incorrect header size")
    magic, version, message_type, payload_length, sequence, pts_ms = struct.unpack(
        HEADER_FORMAT, buffer
    )
    if magic != MAGIC:
        raise ValueError("incorrect protocol magic")
    if version != VERSION:
        raise ValueError("unsupported protocol version")
    if payload_length > MAX_PAYLOAD_SIZE:
        raise ValueError("payload is too large")
    return message_type, payload_length, sequence, pts_ms
