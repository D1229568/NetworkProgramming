"""LAN server discovery over UDP broadcast.

TCP cannot broadcast: it needs the address of the other side before it can
connect.  To *find* that address we use UDP, which can send one datagram to
every device on the local network at once:

    client  --UDP broadcast "discover"-->  255.255.255.255:50556  (everyone on the WiFi)
    server  --UDP unicast  "server_info"-->  client  (name, TCP port, TLS, players)

The reply tells the client where the server's TCP port is; the game itself
then runs over TCP as usual.  A lost datagram does not matter - the client
just scans again.  Datagrams that are not ours are ignored.
"""
from __future__ import annotations

import json
import socket
import time

DISCOVERY_PORT = 50556
MAGIC = "coop-block-adventure"
MAX_DATAGRAM = 1024


def make_request() -> bytes:
    return json.dumps({"magic": MAGIC, "type": "discover", "v": 1}).encode("utf-8")


def is_request(data: bytes) -> bool:
    if len(data) > MAX_DATAGRAM:
        return False
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(msg, dict) and msg.get("magic") == MAGIC and msg.get("type") == "discover"


def make_reply(info: dict) -> bytes:
    return json.dumps({"magic": MAGIC, "type": "server_info", **info}).encode("utf-8")


def parse_reply(data: bytes) -> dict | None:
    if len(data) > MAX_DATAGRAM:
        return None
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(msg, dict) or msg.get("magic") != MAGIC or msg.get("type") != "server_info":
        return None
    if not isinstance(msg.get("port"), int):
        return None
    return msg


def local_ipv4_addresses() -> list[str]:
    addresses = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    try:                       # the address of the interface used for the default route
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))       # no packet is sent by connect() on UDP
        addresses.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    return sorted(a for a in addresses if not a.startswith(("127.", "169.254.")))


def broadcast_targets() -> list[str]:
    """255.255.255.255 plus the /24 broadcast address of every local network,
    and localhost (so a server on this same computer is found too)."""
    targets = ["255.255.255.255", "127.0.0.1"]
    for ip in local_ipv4_addresses():
        a, b, c, _ = ip.split(".")
        targets.append(f"{a}.{b}.{c}.255")
    return list(dict.fromkeys(targets))


def discover(timeout: float = 1.2, port: int = DISCOVERY_PORT, targets: list[str] | None = None) -> list[dict]:
    """Broadcast a discovery request and collect the replies for ``timeout`` seconds."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    found: dict[str, dict] = {}
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        request = make_request()
        for target in targets or broadcast_targets():
            try:
                sock.sendto(request, (target, port))
            except OSError:
                pass                # e.g. no route on this interface
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(left)
            try:
                data, addr = sock.recvfrom(MAX_DATAGRAM + 1)
            except socket.timeout:
                break
            except OSError:
                continue            # Windows reports ICMP "port unreachable" as an error here
            info = parse_reply(data)
            if info is None:
                continue
            info["host"] = addr[0]
            key = str(info.get("id")) or f"{addr[0]}:{info['port']}"
            old = found.get(key)
            # the same server can answer via localhost and via the LAN: show the LAN address
            if old is None or (old["host"].startswith("127.") and not addr[0].startswith("127.")):
                found[key] = info
    finally:
        sock.close()
    return sorted(found.values(), key=lambda s: (s["host"].startswith("127."), s.get("name", "")))
