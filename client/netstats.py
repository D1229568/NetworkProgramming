"""Network statistics overlay (press F3).

Shows what the network layer is doing right now:
  * transport: TCP + TLS version and cipher (or plaintext), server certificate fingerprint
  * ping (round-trip time of the heartbeat) with a small graph
  * messages and bytes per second in each direction - both the JSON payload and
    the bytes really on the wire, so you can see the TLS overhead
  * game snapshots per second and their jitter (how unevenly they arrive)
"""
from __future__ import annotations

import statistics
import time
from collections import deque

from . import ui


def _bytes(n: float) -> str:
    if n < 1024:
        return f"{n:.0f} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1048576:.2f} MB"


class NetStats:
    def __init__(self):
        self.visible = False
        self.ping: deque = deque(maxlen=60)
        self.snapshots: deque = deque(maxlen=150)
        self.rates = dict.fromkeys(("msgs_in", "msgs_out", "payload_in", "payload_out", "bytes_in", "bytes_out"), 0.0)
        self.reconnects = 0
        self._last = None

    def on_ping(self, ms: float):
        self.ping.append(ms)

    def on_snapshot(self):
        self.snapshots.append(time.monotonic())

    def sample(self, conn):
        """Called once per second: turn the connection's counters into rates."""
        now = time.monotonic()
        if conn is None or not conn.alive:
            self._last = None
            self.rates = dict.fromkeys(self.rates, 0.0)
            return
        counters = {k: getattr(conn, k) for k in self.rates}
        if self._last and self._last[1] == conn.id:
            dt = max(1e-6, now - self._last[0])
            self.rates = {k: (counters[k] - self._last[2][k]) / dt for k in counters}
        self._last = (now, conn.id, counters)

    def snapshot_stats(self):
        now = time.monotonic()
        times = [t for t in self.snapshots if now - t < 3.0]
        if len(times) < 3:
            return 0.0, 0.0
        gaps = [b - a for a, b in zip(times, times[1:])]
        return len(gaps) / sum(gaps), statistics.pstdev(gaps) * 1000

    def draw(self, surf, app):
        if not self.visible:
            return
        x, y, w, h = ui.W - 446, 58, 430, 362
        ui.shadow(surf, (x, y, w, h), 16, 8)
        ui.rect_a(surf, (15, 23, 42, 250), (x, y, w, h), 16)
        ui.text(surf, "Network stats", x + 18, y + 14, 18, ui.WHITE, bold=True)
        ui.text(surf, "F3 to hide", x + w - 18, y + 18, 12, ui.FAINT, anchor="topright")
        conn = app.conn
        rows = []
        if conn and conn.alive:
            rows.append(("Server", f"{app.server_addr[0]}:{app.server_addr[1]}"))
            rows.append(("Transport", conn.description()))
            if conn.fingerprint:
                rows.append(("Certificate", conn.fingerprint[:29] + "..."))
        else:
            rows.append(("Server", "not connected"))
        ping = list(self.ping)
        if ping:
            rows.append(("Ping", f"{ping[-1]:.0f} ms   (avg {sum(ping) / len(ping):.0f}, max {max(ping):.0f})"))
        r = self.rates
        rows.append(("Sent", f"{r['msgs_out']:.1f} msg/s   {_bytes(r['payload_out'])}/s   "
                             f"(wire {_bytes(r['bytes_out'])}/s)"))
        rows.append(("Received", f"{r['msgs_in']:.1f} msg/s   {_bytes(r['payload_in'])}/s   "
                                 f"(wire {_bytes(r['bytes_in'])}/s)"))
        rate, jitter = self.snapshot_stats()
        rows.append(("Snapshots", f"{rate:.1f} /s   jitter {jitter:.1f} ms" if rate else "- (not in a game)"))
        if conn and conn.alive:
            rows.append(("Totals", f"sent {_bytes(conn.bytes_out)}   received {_bytes(conn.bytes_in)}"))
        rows.append(("Reconnects", str(self.reconnects)))
        rows.append(("Framing", "4-byte length + JSON, TCP_NODELAY on"))
        ry = y + 48
        for label, value in rows:
            ui.text(surf, label, x + 18, ry, 13, ui.FAINT, bold=True)
            ui.text(surf, value, x + 112, ry, 13, (226, 232, 240))
            ry += 24
        # ping graph
        gx, gy, gw, gh = x + 18, y + h - 62, w - 36, 46
        ui.rect_a(surf, (255, 255, 255, 18), (gx, gy, gw, gh), 6)
        if len(ping) >= 2:
            top = max(20.0, max(ping) * 1.2)
            step = gw / (self.ping.maxlen - 1)
            start = self.ping.maxlen - len(ping)
            pts = [(gx + (start + i) * step, gy + gh - min(gh, gh * v / top)) for i, v in enumerate(ping)]
            for a, b in zip(pts, pts[1:]):
                ui.line(surf, (134, 239, 172), a, b, 2)
            ui.text(surf, f"{top:.0f} ms", gx + 6, gy + 4, 11, ui.FAINT)
        ui.text(surf, "ping, last 60 s", gx + gw - 6, gy + gh - 4, 11, ui.FAINT, anchor="bottomright")
