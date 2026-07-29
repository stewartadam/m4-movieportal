"""Tests for the device-side frame queue and scheduling."""

import unittest

import movieportal


class MoviePortalTests(unittest.TestCase):
    class FakeStream:
        def __init__(self, data):
            self.data = bytearray(data)

        def readinto(self, target):
            count = min(len(target), len(self.data))
            target[:count] = self.data[:count]
            del self.data[:count]
            return count

    def test_frame_queue_wraps_without_allocating_new_buffers(self):
        queue = movieportal.FrameQueue(capacity=2, frame_size=4)
        original_buffers = tuple(queue.buffers)

        queue.writable_view()[:] = b"abcd"
        queue.commit(10, 100)
        queue.writable_view()[:] = b"efgh"
        queue.commit(11, 200)

        self.assertTrue(queue.full())
        self.assertEqual(queue.head_buffer(), b"abcd")
        self.assertEqual(queue.pop(), (10, 100))

        queue.writable_view()[:] = b"ijkl"
        queue.commit(12, 300)

        self.assertEqual(queue.pop(), (11, 200))
        self.assertEqual(queue.head_buffer(), b"ijkl")
        self.assertEqual(tuple(queue.buffers), original_buffers)

    def test_session_counts_one_underrun_until_a_frame_arrives(self):
        session = movieportal.PlaybackSession()
        session.configure(24, 1)

        session.mark_underrun()
        session.mark_underrun()
        self.assertEqual(session.underruns, 1)

        session.mark_frame_available()
        session.mark_underrun()
        self.assertEqual(session.underruns, 2)

    def test_session_uses_absolute_presentation_timestamps(self):
        session = movieportal.PlaybackSession()
        session.configure(24000, 1001)
        session.start(10.0)

        self.assertAlmostEqual(
            session.due_at(1000),
            11.0 + movieportal.START_DELAY_SECONDS,
        )

    def test_packet_reader_resynchronizes_after_stale_bytes(self):
        packet = movieportal.protocol.pack_packet(movieportal.protocol.MSG_HELLO)
        stream = self.FakeStream(b"stale bytes" + packet)
        reader = movieportal.PacketReader()
        queue = movieportal.FrameQueue(capacity=1, frame_size=4)

        event = None
        for _ in range(64):
            event = reader.poll(stream, queue)
            if event is not None:
                break

        self.assertEqual(
            event,
            (movieportal.protocol.MSG_HELLO, 0, 0, b""),
        )


if __name__ == "__main__":
    unittest.main()
