"""Tests for the mpv JSON IPC client used by IINA mode."""

import json
from pathlib import Path
import queue
import socket
import tempfile
import threading
import unittest

from mpv_ipc import MpvIpcClient


class MpvIpcTests(unittest.TestCase):
    def test_event_poll_is_non_blocking_by_default(self):
        client = object.__new__(MpvIpcClient)
        client.events = queue.Queue()

        self.assertIsNone(client.next_event())

    def test_routes_command_responses_and_asynchronous_events(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            socket_path = Path(temporary_directory) / "mpv.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(socket_path))
            server.listen(1)
            requests = []

            def serve():
                connection, _address = server.accept()
                with connection, connection.makefile("rb") as reader:
                    request = json.loads(reader.readline())
                    requests.append(request)
                    connection.sendall(
                        b'{"event":"property-change","name":"pause",'
                        b'"data":false}\n'
                    )
                    response = {
                        "request_id": request["request_id"],
                        "error": "success",
                        "data": "/tmp/movie.mkv",
                    }
                    connection.sendall((json.dumps(response) + "\n").encode())

            server_thread = threading.Thread(target=serve)
            server_thread.start()
            try:
                with MpvIpcClient(socket_path) as client:
                    self.assertEqual(
                        client.get_property("path"),
                        "/tmp/movie.mkv",
                    )
                    self.assertEqual(
                        client.next_event(timeout=1)["event"],
                        "property-change",
                    )
            finally:
                server.close()
                server_thread.join(timeout=1)

            self.assertEqual(
                requests[0]["command"],
                ["get_property", "path"],
            )


if __name__ == "__main__":
    unittest.main()
