"""Co-op Block Adventure - graphical client (pygame-ce).

Screens:  Login/Register  ->  Lobby (rooms, online players, chat)  ->  Room
(level select + seats, then the game, with the member list and room chat).

The main loop runs at 60 FPS: it reads mouse/keyboard events, processes the
messages that the network thread put in a queue (see network.py), and draws
the current screen.  A heartbeat ping goes out every second; if the server
goes silent or the TCP connection breaks, a banner appears and the client
keeps reconnecting and logging in again with the same account, which puts the
player back into the game that the server kept paused for them.
"""
from __future__ import annotations

import math
import os
import queue
import random
import time

os.environ.setdefault("SDL_WINDOWS_DPI_AWARENESS", "permonitorv2")   # sharp text on high-DPI screens
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame  # noqa: E402

from common import protocol as P  # noqa: E402

from . import ui  # noqa: E402
from .network import Connection  # noqa: E402
from .scenes import LobbyScene, LoginScene, RoomScene  # noqa: E402

MAX_RECONNECT_ATTEMPTS = 20
RECONNECT_DELAY = 2.0


def make_icon() -> pygame.Surface:
    icon = pygame.Surface((64, 64), pygame.SRCALPHA)
    pygame.draw.rect(icon, ui.PLAYER_DARK[1], (4, 4, 56, 56), border_radius=14)
    pygame.draw.rect(icon, ui.PLAYER_COLORS[1], (8, 6, 48, 48), border_radius=12)
    for x in (20, 36):
        pygame.draw.ellipse(icon, (255, 255, 255), (x, 18, 10, 16))
        pygame.draw.ellipse(icon, (15, 23, 42), (x + 3, 22, 6, 9))
    return icon


class App:
    def __init__(self, host=P.DEFAULT_HOST, port=P.DEFAULT_PORT, user=None, password=None, size=None):
        pygame.init()
        pygame.key.set_repeat(400, 35)
        if size is None:
            dw, dh = pygame.display.get_desktop_sizes()[0]
            k = min(dw * 0.88 / ui.W, dh * 0.82 / ui.H)
            size = (round(ui.W * k), round(ui.H * k))
        self.screen = pygame.display.set_mode(size, pygame.RESIZABLE)
        pygame.display.set_caption("Co-op Block Adventure  協力方塊大冒險")
        pygame.display.set_icon(make_icon())
        ui.configure(*self.screen.get_size())
        pygame.key.stop_text_input()

        # connection state
        self.events: queue.Queue = queue.Queue()
        self.conn: Connection | None = None
        self.server_addr = (host, port)
        self.username: str | None = None
        self._password: str | None = None
        self._pending_password: str | None = None
        self._after_connect = None
        self.logged_in = False
        self.reconnecting = False
        self._reconnect_attempt = 0
        self._reconnect_reason = ""
        self._reconnect_timer = None
        self.ping_ms: float | None = None
        self.status_note = ""

        # game state shown by the screens
        self.room: dict | None = None
        self.progress: dict | None = None
        self.level_count = 5
        self.last_result: dict | None = None

        self.toasts = ui.Toasts()
        self.modal: ui.Modal | None = None
        self._giveup_btn = ui.Button(1130, 4, 130, 38, "Give up", self.cancel_reconnect, "light", 14)
        self._timers: list[list] = []
        self._next_heartbeat = time.monotonic() + P.HEARTBEAT_INTERVAL
        self._bg = None
        self._bg_key = None
        self._floaters = [[random.uniform(0, ui.W), random.uniform(0, ui.H), random.uniform(14, 46),
                           random.uniform(6, 18), random.uniform(0, math.tau)] for _ in range(18)]

        self.login_scene = LoginScene(self, host, port)
        self.lobby_scene = LobbyScene(self)
        self.room_scene = RoomScene(self)
        self.scene = None
        self.show(self.login_scene)
        self.running = True
        if user and password:
            self.after(0.2, lambda: self.login(host, port, user, password))

    # ================================================================ helpers
    def show(self, scene):
        ui.set_focus(None)
        self.scene = scene
        scene.on_enter()

    def after(self, delay, fn):
        timer = [time.monotonic() + delay, fn]
        self._timers.append(timer)
        return timer

    def cancel(self, timer):
        if timer in self._timers:
            self._timers.remove(timer)

    def toast(self, text, kind="info"):
        self.toasts.push(text, kind)

    def confirm(self, title, message, ok_label, on_ok):
        self.modal = ui.Modal(title, message, [("Cancel", "light", None), (ok_label, "danger", on_ok)])

    def alert(self, title, message):
        self.modal = ui.Modal(title, message, [("OK", "primary", None)])

    def is_online(self) -> bool:
        return bool(self.conn and self.conn.alive and self.logged_in)

    def send(self, msg: dict) -> bool:
        return bool(self.conn and self.conn.send(msg))

    # ============================================================== main loop
    def run(self):
        clock = pygame.time.Clock()
        while self.running:
            dt = min(clock.tick(60) / 1000.0, 0.05)
            pos = ui.to_logical(*pygame.mouse.get_pos())
            for ev in pygame.event.get():
                self._event(ev, pos)
            self._poll_network()
            self._run_timers()
            if self.modal:
                self.modal.update(dt, pos)
            self.scene.update(dt, pos)
            self._draw(dt)
            pygame.display.flip()
        pygame.quit()

    def _event(self, ev, pos):
        if ev.type == pygame.QUIT:
            self.on_close()
            return
        if ev.type == pygame.WINDOWSIZECHANGED:
            self.screen = pygame.display.get_surface()
            ui.configure(*self.screen.get_size())
            return
        if ev.type == pygame.WINDOWFOCUSLOST:
            self.room_scene.game.release_all()
            return
        if hasattr(ev, "pos") and ev.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP, pygame.MOUSEMOTION):
            pos = ui.to_logical(*ev.pos)
        if self.modal:
            self.modal.handle(ev, pos)
            if self.modal.closed:
                self.modal = None
            return
        if self.reconnecting and self._giveup_btn.handle(ev, pos):
            return
        self.scene.handle(ev, pos)

    def _run_timers(self):
        now = time.monotonic()
        due = [t for t in self._timers if t[0] <= now]
        for t in due:
            self._timers.remove(t)
            t[1]()
        if now >= self._next_heartbeat:
            self._next_heartbeat = now + P.HEARTBEAT_INTERVAL
            self._heartbeat()

    def _draw_background(self):
        key = (self.screen.get_size(), ui.View.version)
        if self._bg_key != key:
            self._bg = ui.gradient(self.screen.get_size(), ui.BG_TOP, ui.BG_BOTTOM).convert()
            self._bg_key = key
        self.screen.blit(self._bg, (0, 0))
        now = time.monotonic()
        for f in self._floaters:
            x, y0, size, speed, phase = f
            y = (y0 - now * speed) % (ui.H + 120) - 60
            xx = x + 14 * math.sin(now * 0.6 + phase)
            ui.rect_a(self.screen, (255, 255, 255, 16), (xx, y, size, size), size * 0.26)

    def _draw(self, dt):
        self._draw_background()
        self.scene.draw(self.screen, dt)
        if self.reconnecting:
            ui.rect(self.screen, (185, 28, 28), (0, 0, ui.W, 46))
            ui.polygon(self.screen, ui.GOLD, [(30, 10), (42, 34), (18, 34)])
            ui.text(self.screen, "!", 30, 25, 15, (127, 29, 29), bold=True, anchor="center")
            ui.text(self.screen, f"Connection lost ({self._reconnect_reason}).  Reconnecting...  "
                                 f"attempt {self._reconnect_attempt}/{MAX_RECONNECT_ATTEMPTS}",
                    54, 23, 16, ui.WHITE, bold=True, anchor="midleft")
            self._giveup_btn.draw(self.screen)
        self._draw_status_bar()
        self.toasts.draw(self.screen)
        if self.modal:
            self.modal.draw(self.screen)

    def _draw_status_bar(self):
        y = ui.H - 24
        ui.rect_a(self.screen, (15, 23, 42, 170), (0, y, ui.W, 24))
        if self.conn and self.conn.alive:
            parts = [f"Connected to {self.server_addr[0]}:{self.server_addr[1]} (TCP)"]
            if self.ping_ms is not None:
                parts.append(f"ping {self.ping_ms:.0f} ms")
            if self.logged_in:
                parts.append(f"logged in as {self.username}")
            color = (134, 239, 172)
        else:
            parts = ["Not connected"]
            color = (252, 165, 165)
        if self.status_note:
            parts.append(self.status_note)
        ui.circle(self.screen, color, (14, y + 12), 4)
        ui.text(self.screen, "   ·   ".join(parts), 26, y + 12, 13, (226, 232, 240), anchor="midleft")

    # ============================================================ networking
    def _connect(self, host, port, then):
        if self.conn is not None:
            self.conn.close()
        self.server_addr = (host, port)
        self.conn = Connection(self.events)
        self._after_connect = then
        self.status_note = f"connecting to {host}:{port}..."
        self.conn.connect_async(host, port)

    def _with_connection(self, host, port, then):
        if self.conn and self.conn.alive and self.server_addr == (host, port):
            then()
        else:
            self._connect(host, port, then)

    def login(self, host, port, user, password):
        def go():
            self._pending_password = password
            self.send({"type": "login", "username": user, "password": password})
        self._with_connection(host, port, go)

    def register(self, host, port, user, password):
        self._with_connection(host, port, lambda: self.send(
            {"type": "register", "username": user, "password": password}))

    def logout(self):
        self.send({"type": "logout"})

    def _poll_network(self):
        for _ in range(300):
            try:
                kind, conn_id, payload = self.events.get_nowait()
            except queue.Empty:
                return
            if self.conn is None or conn_id != self.conn.id:
                continue                        # event from an old connection
            if kind == "msg":
                handler = getattr(self, "on_" + payload.get("type", ""), None)
                if handler:
                    handler(payload)
            elif kind == "connected":
                self.status_note = ""
                then, self._after_connect = self._after_connect, None
                if then:
                    then()
            elif kind == "connect_failed":
                self.conn = None
                self._on_connect_failed(payload)
            elif kind == "closed":
                self.conn = None
                self._on_connection_lost(payload)

    def _heartbeat(self):
        conn = self.conn
        if conn and conn.alive:
            conn.send({"type": "ping", "t": time.time()})
            silent = time.monotonic() - conn.last_recv
            if silent > P.SERVER_SILENCE_TIMEOUT:
                conn.fail(f"no reply from the server for {silent:.0f} s")

    def _on_connect_failed(self, error):
        self.status_note = ""
        if self.reconnecting:
            self._schedule_reconnect()
        else:
            self.login_scene.show_error(f"Cannot connect to {self.server_addr[0]}:{self.server_addr[1]} - {error}")

    def _on_connection_lost(self, reason):
        if self.reconnecting:
            self._schedule_reconnect()
        elif self.logged_in:
            self._start_reconnect(reason)
        else:
            self.status_note = f"disconnected ({reason})"

    # ---------------------------------------------------------- reconnecting
    def _start_reconnect(self, reason):
        self.reconnecting = True
        self._reconnect_attempt = 0
        self._reconnect_reason = reason
        self.room_scene.game.connection_lost = True
        self.room_scene.game.release_all()
        self._try_reconnect()

    def _schedule_reconnect(self):
        self._reconnect_timer = self.after(RECONNECT_DELAY, self._try_reconnect)

    def _try_reconnect(self):
        self._reconnect_timer = None
        if not self.reconnecting:
            return
        self._reconnect_attempt += 1
        if self._reconnect_attempt > MAX_RECONNECT_ATTEMPTS:
            self._end_reconnect()
            self._to_login()
            self.alert("Connection lost", "Could not reconnect to the server. Please log in again.")
            return
        host, port = self.server_addr
        self.login(host, port, self.username, self._password)

    def _end_reconnect(self):
        self.reconnecting = False
        if self._reconnect_timer:
            self.cancel(self._reconnect_timer)
            self._reconnect_timer = None
        self.room_scene.game.connection_lost = False

    def cancel_reconnect(self):
        self._end_reconnect()
        if self.conn:
            self.conn.close()
            self.conn = None
        self._to_login()

    def _to_login(self):
        self.logged_in = False
        self.username = None
        self._password = None
        self.room = None
        self.room_scene.game.reset()
        self.show(self.login_scene)

    # debug helpers (documented in the README) to demonstrate failure handling
    def debug_drop_connection(self):
        if self.conn and self.conn.alive:
            self.toast("[debug] F9: dropping the TCP connection without logging out", "warn")
            self.conn.fail("simulated network failure (F9)")

    def debug_freeze_network(self):
        if self.conn and self.conn.alive:
            self.toast("[debug] F10: outgoing traffic frozen for 10 s - watch the server's heartbeat check", "warn")
            self.conn.freeze_until = time.monotonic() + 10

    # ============================================================== messages
    def on_hello(self, msg):
        if msg.get("version") != P.PROTOCOL_VERSION:
            self.toast("The server uses a different protocol version", "warn")

    def on_pong(self, msg):
        if isinstance(msg.get("t"), (int, float)):
            self.ping_ms = (time.time() - msg["t"]) * 1000

    def on_register_result(self, msg):
        self.login_scene.on_register_result(msg)

    def on_login_result(self, msg):
        if not msg["ok"]:
            if self.reconnecting:
                self._end_reconnect()
                self._to_login()
                self.alert("Reconnect failed", msg["msg"])
            else:
                self.login_scene.show_error(msg["msg"])
            return
        was_reconnecting = self.reconnecting
        self._end_reconnect()
        self.logged_in = True
        self.username = msg["username"]
        self._password = self._pending_password
        self.progress = msg.get("progress")
        self.level_count = msg.get("level_count", self.level_count)
        if not msg.get("rejoin"):
            self.room = None
            self.room_scene.game.reset()
            self.show(self.lobby_scene)
            if was_reconnecting:
                self.toast("Reconnected.", "ok")
            else:
                self.lobby_scene.chat.add(None, msg.get("msg", ""), system=True)
        else:
            self.room_scene.chat.clear()          # the server re-sends the room's chat history
            self.toast("Reconnected - you are back in your game!", "ok")

    def on_logout_ok(self, _msg):
        self._to_login()

    def on_kicked(self, msg):
        self.logged_in = False                  # do not auto-reconnect
        if self.conn:
            self.conn.close()
            self.conn = None
        self._to_login()
        self.alert("Disconnected", msg.get("reason", "Kicked by the server"))

    def on_lobby(self, msg):
        self.lobby_scene.update_lobby(msg)

    def on_room(self, msg):
        self.room = msg
        self.room_scene.on_room(msg)
        if self.scene is not self.room_scene:
            self.room_scene.chat.clear()
            self.show(self.room_scene)

    def on_room_left(self, _msg):
        self.room = None
        self.room_scene.game.reset()
        self.show(self.lobby_scene)

    def on_level(self, msg):
        self.room_scene.game.load_level(msg)

    def on_state(self, msg):
        self.room_scene.game.on_state(msg)

    def on_level_result(self, msg):
        self.last_result = msg
        if self.progress is not None:
            self.progress["unlocked"] = msg["unlocked"]
            self.progress.setdefault("best", {})[str(msg["index"])] = msg["best"]
            self.progress["clears"] = self.progress.get("clears", 0) + 1
        if msg["newly_unlocked"]:
            self.toast(f"Level {msg['unlocked'] + 1} unlocked!", "ok")

    def on_chat(self, msg):
        if msg.get("channel") == "room":
            self.room_scene.chat.add(msg.get("from"), msg["text"], system=bool(msg.get("system")))
            if msg.get("system") and self.scene is self.room_scene and self.room_scene.game.cur:
                self.toast(msg["text"])           # pop up game events while playing
        else:
            self.lobby_scene.chat.add(msg.get("from"), msg["text"], system=bool(msg.get("system")))

    def on_error(self, msg):
        self.toast(msg.get("msg", "Error"), "error")

    def on_close(self):
        if self.conn and self.conn.alive:
            if self.logged_in:
                self.send({"type": "logout"})   # a normal, announced departure
            self.conn.close()
        self.running = False


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Co-op Block Adventure client")
    parser.add_argument("--host", default=P.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=P.DEFAULT_PORT)
    parser.add_argument("--user", help="log in automatically (for quick testing)")
    parser.add_argument("--password")
    args = parser.parse_args(argv)
    App(args.host, args.port, args.user, args.password).run()


if __name__ == "__main__":
    main()
