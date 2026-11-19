"""Client side of the TCP connection.

Tkinter is not thread-safe, so the network thread never touches the GUI.  It
puts events into a queue that the GUI thread polls:

    ("connected", conn_id, None)
    ("connect_failed", conn_id, error_text)
    ("msg", conn_id, message_dict)
    ("closed", conn_id, reason)

``conn_id`` lets the GUI ignore late events from an old connection after it
has already reconnected with a new one.
"""
from __future__ import annotations

import itertools
import queue
import socket
import threading
import time

from common.protocol import FrameDecoder, ProtocolError, encode

_ids = itertools.count(1)


class Connection:
    def __init__(self, events: queue.Queue):
        self.events = events
        self.id = next(_ids)
        self.sock: socket.socket | None = None
        self.connected = False
        self.last_recv = time.monotonic()
        self.freeze_until = 0.0         # debug: pretend the network is frozen
        self._send_lock = threading.Lock()
        self._closed = False

    @property
    def alive(self) -> bool:
        return self.connected and not self._closed

    def connect_async(self, host: str, port: int, timeout: float = 4.0):
        threading.Thread(target=self._connect, args=(host, port, timeout), daemon=True).start()

    def _connect(self, host, port, timeout):
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            self._closed = True
            self.events.put(("connect_failed", self.id, str(exc) or exc.__class__.__name__))
            return
        if self._closed:                # cancelled while connecting
            sock.close()
            return
        self.sock = sock
        self.connected = True
        self.last_recv = time.monotonic()
        self.events.put(("connected", self.id, None))
        self._read_loop()

    def _read_loop(self):
        decoder = FrameDecoder()
        reason = "the server closed the connection"
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    break
                self.last_recv = time.monotonic()
                for msg in decoder.feed(data):
                    self.events.put(("msg", self.id, msg))
        except ProtocolError as exc:
            reason = f"protocol error: {exc}"
        except OSError as exc:
            reason = f"network error: {exc.__class__.__name__}"
        self.fail(reason)

    def send(self, msg: dict) -> bool:
        if not self.alive:
            return False
        if time.monotonic() < self.freeze_until:
            return True                 # simulated network freeze: silently drop
        data = encode(msg)
        try:
            with self._send_lock:
                self.sock.sendall(data)
            return True
        except OSError as exc:
            self.fail(f"send failed: {exc.__class__.__name__}")
            return False

    def fail(self, reason: str):
        """The connection broke (or we decided it is dead): report it once."""
        if self._closed:
            return
        self._closed = True
        self._close_socket()
        self.events.put(("closed", self.id, reason))

    def close(self):
        """Intentional close: no 'closed' event is reported."""
        self._closed = True
        self._close_socket()

    def _close_socket(self):
        sock = self.sock
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
