"""Client side of the TCP (+TLS) connection, and LAN discovery.

The GUI is single-threaded, so the network threads never touch it.  They put
events into a queue that the main loop polls:

    ("connected", conn_id, None)
    ("connect_failed", conn_id, error_text | {"kind": "fingerprint", ...})
    ("msg", conn_id, message_dict)
    ("closed", conn_id, reason)
    ("discovered", None, [server_info, ...])

``conn_id`` lets the GUI ignore late events from an old connection after it
has already reconnected with a new one.

TLS trust: the server's certificate is self-signed, so the client remembers
its SHA-256 fingerprint the first time it connects to a host:port ("trust on
first use", like SSH).  If the fingerprint is different later, the client
refuses to continue unless the player explicitly trusts the new certificate.
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import socket
import ssl
import threading
import time

from common import discovery
from common.protocol import FrameDecoder, ProtocolError, encode
from common.transport import PlainTransport, TlsTransport, client_context, fingerprint

_ids = itertools.count(1)
KNOWN_SERVERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "known_servers.json")


class KnownServers:
    """host:port -> certificate fingerprint, stored in client/data/known_servers.json."""

    def __init__(self, path: str = KNOWN_SERVERS_FILE):
        self.path = path
        self._lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                self._data = json.load(f)
        except (OSError, ValueError):
            self._data = {}

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, fp: str):
        with self._lock:
            self._data[key] = fp
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2)
            except OSError:
                pass


class Connection:
    def __init__(self, events: queue.Queue, tls: bool = True, known: KnownServers | None = None):
        self.events = events
        self.id = next(_ids)
        self.tls = tls
        self.known = known
        self.transport = None
        self.connected = False
        self.last_recv = time.monotonic()
        self.freeze_until = 0.0         # debug: pretend the network is frozen
        self.fingerprint: str | None = None
        self.first_trust = False        # True when this server's certificate was just pinned
        self.msgs_in = self.msgs_out = 0
        self.payload_in = self.payload_out = 0
        self._send_lock = threading.Lock()
        self._closed = False

    @property
    def alive(self) -> bool:
        return self.connected and not self._closed

    @property
    def bytes_in(self) -> int:
        return self.transport.bytes_in if self.transport else 0

    @property
    def bytes_out(self) -> int:
        return self.transport.bytes_out if self.transport else 0

    def description(self) -> str:
        return self.transport.description() if self.transport else "-"

    def connect_async(self, host: str, port: int, timeout: float = 4.0):
        threading.Thread(target=self._connect, args=(host, port, timeout), daemon=True).start()

    def _connect(self, host, port, timeout):
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            return self._failed(str(exc) or exc.__class__.__name__)
        if self.tls:
            transport = TlsTransport(sock, client_context(), server_side=False)
            try:
                transport.handshake(timeout)
            except (OSError, ssl.SSLError, ConnectionError) as exc:
                transport.close()
                return self._failed(f"TLS handshake failed ({exc.__class__.__name__}). "
                                    "The server may be running without TLS - try turning TLS off.")
            fp = fingerprint(transport.peer_certificate())
            key = f"{host}:{port}"
            known = self.known.get(key) if self.known else None
            if known is not None and known != fp:
                transport.close()
                return self._failed({"kind": "fingerprint", "server": key, "expected": known, "got": fp})
            if known is None and self.known is not None:
                self.known.set(key, fp)
                self.first_trust = True
            self.fingerprint = fp
        else:
            transport = PlainTransport(sock)
        if self._closed:                # cancelled while connecting
            transport.close()
            return
        self.transport = transport
        self.connected = True
        self.last_recv = time.monotonic()
        self.events.put(("connected", self.id, None))
        self._read_loop()

    def _failed(self, error):
        self._closed = True
        self.events.put(("connect_failed", self.id, error))

    def _read_loop(self):
        decoder = FrameDecoder()
        reason = "the server closed the connection"
        try:
            while True:
                data = self.transport.recv()
                if not data:
                    break
                self.last_recv = time.monotonic()
                self.payload_in += len(data)
                for msg in decoder.feed(data):
                    self.msgs_in += 1
                    self.events.put(("msg", self.id, msg))
        except ProtocolError as exc:
            reason = f"protocol error: {exc}"
        except (OSError, ssl.SSLError) as exc:
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
                self.transport.sendall(data)
                self.msgs_out += 1
                self.payload_out += len(data)
            return True
        except (OSError, ssl.SSLError) as exc:
            self.fail(f"send failed: {exc.__class__.__name__}")
            return False

    def fail(self, reason: str):
        """The connection broke (or we decided it is dead): report it once."""
        if self._closed:
            return
        self._closed = True
        if self.transport:
            self.transport.close()
        self.events.put(("closed", self.id, reason))

    def close(self):
        """Intentional close: no 'closed' event is reported."""
        self._closed = True
        if self.transport:
            self.transport.close()


def discover_async(events: queue.Queue, timeout: float = 1.2):
    """Scan the LAN for servers (UDP broadcast) in the background."""
    def run():
        try:
            found = discovery.discover(timeout)
        except OSError:
            found = []
        events.put(("discovered", None, found))
    threading.Thread(target=run, daemon=True).start()
