"""USB RGB565 frame receiver for the MatrixPortal M4."""

import gc
import struct
import time

import protocol
from settings import DISPLAY_BIT_DEPTH


WIDTH = 64
HEIGHT = 32
FRAME_SIZE = WIDTH * HEIGHT * 2
QUEUE_CAPACITY = 4
START_DELAY_SECONDS = protocol.PREROLL_MS / 1000.0
CONTROL_PAYLOAD_SIZE = protocol.STATUS_SIZE


class FrameQueue:
    """A fixed-size, allocation-free queue of RGB565 frames."""

    def __init__(self, capacity=QUEUE_CAPACITY, frame_size=FRAME_SIZE):
        self.capacity = capacity
        self.frame_size = frame_size
        self.buffers = [bytearray(frame_size) for _ in range(capacity)]
        self.views = [memoryview(item) for item in self.buffers]
        self.sequences = [0] * capacity
        self.timestamps = [0] * capacity
        self.head = 0
        self.count = 0

    def clear(self):
        self.head = 0
        self.count = 0

    def full(self):
        return self.count == self.capacity

    def empty(self):
        return self.count == 0

    def writable_view(self):
        if self.full():
            return None
        return self.views[(self.head + self.count) % self.capacity]

    def commit(self, sequence, pts_ms):
        if self.full():
            raise RuntimeError("frame queue is full")
        index = (self.head + self.count) % self.capacity
        self.sequences[index] = sequence
        self.timestamps[index] = pts_ms
        self.count += 1

    def head_buffer(self):
        if self.empty():
            return None
        return self.buffers[self.head]

    def head_timestamp(self):
        if self.empty():
            return None
        return self.timestamps[self.head]

    def next_timestamp(self):
        if self.count < 2:
            return None
        return self.timestamps[(self.head + 1) % self.capacity]

    def pop(self):
        if self.empty():
            raise RuntimeError("frame queue is empty")
        sequence = self.sequences[self.head]
        pts_ms = self.timestamps[self.head]
        self.head = (self.head + 1) % self.capacity
        self.count -= 1
        return sequence, pts_ms


class PacketReader:
    """Incrementally read framed packets without blocking display timing."""

    def __init__(self):
        self.header = bytearray(protocol.HEADER_SIZE)
        self.header_view = memoryview(self.header)
        self.control = bytearray(CONTROL_PAYLOAD_SIZE)
        self.control_view = memoryview(self.control)
        self.header_used = 0
        self.payload_used = 0
        self.message_type = 0
        self.payload_length = 0
        self.sequence = 0
        self.pts_ms = 0
        self.target = None

    def reset(self):
        """Discard any partial packet after a transport reconnect."""
        self.header_used = 0
        self.payload_used = 0
        self.message_type = 0
        self.payload_length = 0
        self.sequence = 0
        self.pts_ms = 0
        self.target = None

    @staticmethod
    def _read(stream, target, offset):
        view = target[offset:]
        count = stream.readinto(view)
        return count or 0

    def poll(self, stream, queue):
        """Return one complete event, or None when more bytes are needed."""
        if self.header_used < len(protocol.MAGIC):
            self.header_used += self._read(
                stream,
                self.header_view[: len(protocol.MAGIC)],
                self.header_used,
            )
            if self.header_used < len(protocol.MAGIC):
                return None
            if (
                self.header[0] != protocol.MAGIC[0]
                or self.header[1] != protocol.MAGIC[1]
                or self.header[2] != protocol.MAGIC[2]
                or self.header[3] != protocol.MAGIC[3]
            ):
                self.header[0] = self.header[1]
                self.header[1] = self.header[2]
                self.header[2] = self.header[3]
                self.header_used = len(protocol.MAGIC) - 1
                return None

        if self.header_used < protocol.HEADER_SIZE:
            self.header_used += self._read(
                stream, self.header_view, self.header_used
            )
            if self.header_used < protocol.HEADER_SIZE:
                return None
            (
                self.message_type,
                self.payload_length,
                self.sequence,
                self.pts_ms,
            ) = protocol.unpack_header(self.header)
            if self.message_type == protocol.MSG_FRAME:
                if self.payload_length != queue.frame_size:
                    self.reset()
                    raise ValueError("frame has incorrect size")
            elif self.payload_length > CONTROL_PAYLOAD_SIZE:
                self.reset()
                raise ValueError("control payload is too large")

        if self.payload_length == 0:
            event = (self.message_type, self.sequence, self.pts_ms, b"")
            self.reset()
            return event

        if self.target is None:
            if self.message_type == protocol.MSG_FRAME:
                self.target = queue.writable_view()
                if self.target is None:
                    return None
            else:
                self.target = self.control_view

        self.payload_used += self._read(stream, self.target, self.payload_used)
        if self.payload_used < self.payload_length:
            return None

        if self.message_type == protocol.MSG_FRAME:
            queue.commit(self.sequence, self.pts_ms)
            event = (self.message_type, self.sequence, self.pts_ms, b"")
        else:
            payload = bytes(self.control[: self.payload_length])
            event = (self.message_type, self.sequence, self.pts_ms, payload)
        self.reset()
        return event


class PlaybackSession:
    """Own frame scheduling and playback counters."""

    def __init__(self):
        self.fps_num = 0
        self.fps_den = 1
        self.started_at = None
        self.ending = False
        self.received = 0
        self.displayed = 0
        self.dropped = 0
        self.underruns = 0
        self._underrun_active = False

    def configure(self, fps_num, fps_den):
        if fps_num <= 0 or fps_den <= 0:
            raise ValueError("frame rate must be positive")
        self.fps_num = fps_num
        self.fps_den = fps_den
        self.started_at = None
        self.ending = False
        self.received = 0
        self.displayed = 0
        self.dropped = 0
        self.underruns = 0
        self._underrun_active = False

    def start(self, now):
        self.started_at = now + START_DELAY_SECONDS

    def due_at(self, pts_ms):
        return self.started_at + (pts_ms / 1000.0)

    def mark_underrun(self):
        if not self._underrun_active:
            self.underruns += 1
            self._underrun_active = True

    def mark_frame_available(self):
        self._underrun_active = False


def _write_all(stream, data):
    offset = 0
    while offset < len(data):
        offset += stream.write(data[offset:])


def _send_packet(stream, message_type, payload=b""):
    _write_all(stream, protocol.pack_packet(message_type, payload))


def _status_payload(session, queue):
    return struct.pack(
        protocol.STATUS_FORMAT,
        session.received,
        session.displayed,
        session.dropped,
        session.underruns,
        gc.mem_free(),
        queue.capacity - queue.count,
    )


def _create_matrix():
    import board
    import displayio
    import rgbmatrix

    displayio.release_displays()
    return rgbmatrix.RGBMatrix(
        width=WIDTH,
        height=HEIGHT,
        bit_depth=DISPLAY_BIT_DEPTH,
        addr_pins=board.MTX_ADDRESS[:4],
        doublebuffer=True,
        **board.MTX_COMMON,
    )


def _handle_control(event, stream, queue, session, now):
    message_type, _sequence, _pts_ms, payload = event
    if message_type == protocol.MSG_HELLO:
        caps = struct.pack(
            protocol.CAPS_FORMAT,
            WIDTH,
            HEIGHT,
            FRAME_SIZE,
            queue.capacity,
            gc.mem_free(),
        )
        _send_packet(stream, protocol.MSG_CAPS, caps)
    elif message_type == protocol.MSG_CONFIG:
        width, height, fps_num, fps_den = struct.unpack(
            protocol.CONFIG_FORMAT, payload
        )
        if width != WIDTH or height != HEIGHT:
            raise ValueError("host dimensions do not match the panel")
        queue.clear()
        session.configure(fps_num, fps_den)
        _send_packet(stream, protocol.MSG_READY)
    elif message_type == protocol.MSG_START:
        session.start(now)
    elif message_type == protocol.MSG_END:
        session.ending = True
    elif message_type == protocol.MSG_STOP:
        queue.clear()
        session.started_at = None
        session.ending = False
        _send_packet(stream, protocol.MSG_STATUS, _status_payload(session, queue))
    elif message_type != protocol.MSG_FRAME:
        raise ValueError("unexpected message type")


def _display_due_frame(matrix, framebuffer, queue, session, now):
    if session.started_at is None or now < session.started_at:
        return

    while queue.count > 1 and session.due_at(queue.next_timestamp()) <= now:
        queue.pop()
        session.dropped += 1

    if queue.empty():
        if not session.ending:
            session.mark_underrun()
        return

    if session.due_at(queue.head_timestamp()) > now:
        return

    framebuffer[:] = queue.head_buffer()
    matrix.refresh()
    queue.pop()
    session.displayed += 1
    session.mark_frame_available()


def main():
    """Receive RGB565 frames from the dedicated USB CDC data port."""
    import usb_cdc

    stream = usb_cdc.data
    if stream is None:
        raise RuntimeError("USB data CDC is disabled; install boot.py and reset")
    stream.timeout = 0
    stream.write_timeout = None

    matrix = _create_matrix()
    framebuffer = memoryview(matrix)
    if len(framebuffer) != FRAME_SIZE:
        framebuffer = framebuffer.cast("B")
    if len(framebuffer) != FRAME_SIZE:
        raise RuntimeError("unexpected matrix framebuffer size")

    queue = FrameQueue()
    reader = PacketReader()
    session = PlaybackSession()
    print(
        "MoviePortal ready: {} bytes free, {} frame buffers".format(
            gc.mem_free(), queue.capacity
        )
    )

    was_connected = False
    while True:
        if not stream.connected:
            if was_connected:
                reader.reset()
                queue.clear()
                session.started_at = None
                session.ending = False
            was_connected = False
            time.sleep(0.01)
            continue
        if not was_connected:
            reader.reset()
            queue.clear()
            session.started_at = None
            session.ending = False
            was_connected = True

        now = time.monotonic()
        try:
            event = reader.poll(stream, queue)
            if event is not None:
                if event[0] == protocol.MSG_FRAME:
                    session.received += 1
                    session.mark_frame_available()
                else:
                    _handle_control(event, stream, queue, session, now)
        except (RuntimeError, ValueError) as error:
            print("Protocol error:", error)
            queue.clear()
            session.started_at = None
            session.ending = False
            _send_packet(stream, protocol.MSG_ERROR, str(error).encode("utf-8"))

        _display_due_frame(matrix, framebuffer, queue, session, now)

        if session.ending and queue.empty():
            _send_packet(
                stream, protocol.MSG_STATUS, _status_payload(session, queue)
            )
            session.started_at = None
            session.ending = False
