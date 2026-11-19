"""Wire protocol shared by the server and the client.

Transport: TCP.  TCP is a *byte stream*, so one ``recv()`` can return half a
message or several messages glued together.  Every message is therefore framed
with a 4-byte length prefix:

    +---------------------+----------------------------------+
    | length (uint32, BE) | UTF-8 encoded JSON object        |
    +---------------------+----------------------------------+

Every JSON object has a ``"type"`` field (see README.md for the full list).
``FrameDecoder`` buffers incoming bytes and returns only complete messages.
"""
from __future__ import annotations

import json
import re
import struct

PROTOCOL_VERSION = 1

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 50555

# --- timing ---------------------------------------------------------------
TICK_RATE = 60                # server physics updates per second
STATE_RATE = 30               # game-state snapshots sent to clients per second
HEARTBEAT_INTERVAL = 1.0      # client sends "ping" every second
HEARTBEAT_TIMEOUT = 6.0       # server drops a client that has been silent this long
SERVER_SILENCE_TIMEOUT = 5.0  # client treats a silent server as disconnected
RECONNECT_TIMEOUT = 60.0      # how long a room waits for a dropped player

# --- limits ---------------------------------------------------------------
MAX_MESSAGE_SIZE = 64 * 1024
CHAT_MAX_LEN = 200
ROOM_NAME_MAX_LEN = 20
ROOM_SIZES = (2, 3, 4)

_HEADER = struct.Struct("!I")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class ProtocolError(Exception):
    """Raised when the peer sends bytes that are not a valid frame."""


def encode(msg: dict) -> bytes:
    """Serialize one message into a length-prefixed frame."""
    payload = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_MESSAGE_SIZE:
        raise ProtocolError(f"message too large ({len(payload)} bytes)")
    return _HEADER.pack(len(payload)) + payload


class FrameDecoder:
    """Reassembles length-prefixed JSON frames from a TCP byte stream."""

    def __init__(self, max_size: int = MAX_MESSAGE_SIZE):
        self._buf = bytearray()
        self._max = max_size

    def feed(self, data: bytes) -> list[dict]:
        self._buf.extend(data)
        messages = []
        while len(self._buf) >= _HEADER.size:
            (length,) = _HEADER.unpack_from(self._buf, 0)
            if length > self._max:
                raise ProtocolError(f"frame too large ({length} bytes)")
            end = _HEADER.size + length
            if len(self._buf) < end:
                break  # wait for the rest of the frame
            payload = bytes(self._buf[_HEADER.size:end])
            del self._buf[:end]
            try:
                msg = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProtocolError(f"invalid JSON: {exc}") from exc
            if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
                raise ProtocolError("a message must be a JSON object with a 'type'")
            messages.append(msg)
        return messages


# --- validation (used by both sides: the client checks before sending, the
# --- server checks again because it never trusts the client) --------------

def check_username(name) -> str | None:
    """Return an error message, or None when the username is acceptable."""
    if not isinstance(name, str) or not _USERNAME_RE.match(name):
        return "Username must be 3-16 characters: letters, digits or _"
    return None


def check_password(password) -> str | None:
    if not isinstance(password, str) or not 4 <= len(password) <= 64:
        return "Password must be 4-64 characters"
    return None


def clean_text(text, max_len: int) -> str:
    """Strip control characters and surrounding blanks, then truncate."""
    if not isinstance(text, str):
        return ""
    return _CONTROL_CHARS.sub("", text).strip()[:max_len]


def check_room_name(name) -> str | None:
    if not clean_text(name, ROOM_NAME_MAX_LEN):
        return f"Room name must be 1-{ROOM_NAME_MAX_LEN} characters"
    return None
