"""Byte transports used by both the server and the client.

* ``PlainTransport`` - the TCP socket as is (only used with ``--no-tls``).
* ``TlsTransport``   - TLS (Python's ``ssl`` module) on top of the TCP socket.

Why the MemoryBIO design?  Each connection has one thread that reads and
another that writes.  An OpenSSL connection object must not be used by two
threads at once, so instead of ``ssl.wrap_socket`` we drive an
``ssl.SSLObject`` through in-memory buffers ourselves:

    TCP socket --recv--> incoming BIO --SSLObject.read()--> plaintext frames
    plaintext  --SSLObject.write()--> outgoing BIO --sendall--> TCP socket

All SSLObject calls happen under ``_tls_lock``.  The encrypted bytes are sent
outside that lock, but ``_send_lock`` is taken *before* the TLS lock is
released, so TLS records always leave in the order they were produced.

Both transports count the bytes that really go over the wire
(``bytes_in`` / ``bytes_out``), which the network stats overlay shows.
"""
from __future__ import annotations

import hashlib
import socket
import ssl
import threading

HANDSHAKE_TIMEOUT = 5.0


def fingerprint(der: bytes) -> str:
    """SHA-256 fingerprint of a certificate, formatted like AB:CD:..."""
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def server_context(cert_file: str, key_file: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_file, key_file)
    return ctx


def client_context() -> ssl.SSLContext:
    """The server uses a self-signed certificate, so there is no certificate
    authority to check against.  Instead the client pins the certificate's
    fingerprint the first time it connects (trust on first use, like SSH)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class PlainTransport:
    secure = False

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.bytes_in = 0
        self.bytes_out = 0
        self._send_lock = threading.Lock()

    def handshake(self, timeout=HANDSHAKE_TIMEOUT):
        pass

    def recv(self, n: int = 65536) -> bytes:
        data = self.sock.recv(n)
        self.bytes_in += len(data)
        return data

    def sendall(self, data: bytes):
        with self._send_lock:
            self.sock.sendall(data)
            self.bytes_out += len(data)

    def description(self) -> str:
        return "TCP (plaintext, no TLS)"

    def peer_certificate(self) -> bytes | None:
        return None

    def close(self):
        _close_socket(self.sock)


class TlsTransport:
    secure = True

    def __init__(self, sock: socket.socket, context: ssl.SSLContext, server_side: bool):
        self.sock = sock
        self.bytes_in = 0
        self.bytes_out = 0
        self._incoming = ssl.MemoryBIO()
        self._outgoing = ssl.MemoryBIO()
        self._tls = context.wrap_bio(self._incoming, self._outgoing, server_side=server_side)
        self._tls_lock = threading.Lock()
        self._send_lock = threading.Lock()

    # Call with _tls_lock held.  Takes the outgoing TLS bytes and the send
    # lock, so the caller can send them after releasing the TLS lock.
    def _take_outgoing(self) -> bytes:
        data = self._outgoing.read()
        if data:
            self._send_lock.acquire()
        return data

    def _send_taken(self, data: bytes):
        if not data:
            return
        try:
            self.sock.sendall(data)
            self.bytes_out += len(data)
        finally:
            self._send_lock.release()

    def _feed(self) -> bool:
        raw = self.sock.recv(65536)
        if not raw:
            return False
        self.bytes_in += len(raw)
        with self._tls_lock:
            self._incoming.write(raw)
        return True

    def handshake(self, timeout=HANDSHAKE_TIMEOUT):
        self.sock.settimeout(timeout)
        try:
            while True:
                with self._tls_lock:
                    try:
                        self._tls.do_handshake()
                        done = True
                    except ssl.SSLWantReadError:
                        done = False
                    out = self._take_outgoing()
                self._send_taken(out)
                if done:
                    return
                if not self._feed():
                    raise ConnectionError("connection closed during the TLS handshake")
        finally:
            self.sock.settimeout(None)

    def recv(self, n: int = 65536) -> bytes:
        """Return decrypted bytes, or b'' when the connection has ended."""
        while True:
            with self._tls_lock:
                try:
                    data = self._tls.read(n)
                except ssl.SSLWantReadError:
                    data = None
                except ssl.SSLZeroReturnError:
                    data = b""           # the peer closed TLS cleanly
                out = self._take_outgoing()
            self._send_taken(out)
            if data is not None:
                return data
            if not self._feed():
                return b""

    def sendall(self, data: bytes):
        with self._tls_lock:
            self._tls.write(data)
            out = self._take_outgoing()
        self._send_taken(out)

    def description(self) -> str:
        cipher = self._tls.cipher()
        return f"TCP + {self._tls.version()} ({cipher[0] if cipher else '?'})"

    def peer_certificate(self) -> bytes | None:
        return self._tls.getpeercert(binary_form=True)

    def close(self):
        _close_socket(self.sock)


def _close_socket(sock: socket.socket):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass
