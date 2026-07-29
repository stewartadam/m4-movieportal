"""Small synchronous client for mpv's newline-delimited JSON IPC protocol."""

import itertools
import json
import queue
import socket
import threading


class MpvIpcError(RuntimeError):
    """Report an mpv IPC transport or command failure."""


class MpvIpcClient:
    """Send commands to mpv while collecting asynchronous player events."""

    def __init__(self, socket_path, timeout=3):
        self.timeout = timeout
        self.events = queue.Queue()
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._socket.connect(str(socket_path))
        except OSError:
            self._socket.close()
            raise
        self._reader = self._socket.makefile("rb")
        self._request_ids = itertools.count(1)
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._read_messages,
            name="mpv-ipc-reader",
            daemon=True,
        )
        self._thread.start()

    def _read_messages(self):
        try:
            for line in self._reader:
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    self.events.put(
                        {
                            "event": "ipc-error",
                            "message": "invalid JSON from mpv: {}".format(error),
                        }
                    )
                    continue

                request_id = message.get("request_id")
                if request_id is not None:
                    with self._pending_lock:
                        response_queue = self._pending.get(request_id)
                    if response_queue is not None:
                        response_queue.put(message)
                        continue
                self.events.put(message)
        except (OSError, ValueError) as error:
            self.events.put({"event": "ipc-error", "message": str(error)})
        finally:
            self.events.put({"event": "shutdown"})
            with self._pending_lock:
                pending = tuple(self._pending.values())
            for response_queue in pending:
                try:
                    response_queue.put_nowait(
                        {
                            "error": "IPC connection closed",
                        }
                    )
                except queue.Full:
                    pass

    def command(self, name, *arguments):
        """Run one mpv command and return its data field."""
        request_id = next(self._request_ids)
        response_queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = response_queue
        request = {
            "command": [name, *arguments],
            "request_id": request_id,
        }
        encoded = (json.dumps(request, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        try:
            with self._write_lock:
                self._socket.sendall(encoded)
            try:
                response = response_queue.get(timeout=self.timeout)
            except queue.Empty as error:
                raise MpvIpcError(
                    "mpv did not answer the {!r} command".format(name)
                ) from error
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

        if response.get("error") != "success":
            raise MpvIpcError(
                "mpv {!r} failed: {}".format(name, response.get("error"))
            )
        return response.get("data")

    def observe_property(self, observer_id, name):
        self.command("observe_property", observer_id, name)

    def get_property(self, name):
        return self.command("get_property", name)

    def set_property(self, name, value):
        return self.command("set_property", name, value)

    def next_event(self, timeout=0):
        """Return the next player event, or None without blocking by default."""
        try:
            if timeout == 0:
                return self.events.get_nowait()
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._socket.close()
        try:
            self._reader.close()
        except OSError:
            pass
        self._thread.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, _exception_type, _exception, _traceback):
        self.close()
