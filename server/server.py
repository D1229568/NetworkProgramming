"""Co-op Block Adventure - TCP game server.

Threads:
  * accept thread      - accepts TCP connections, one ClientHandler each
  * per client: reader - decodes frames and dispatches them
                writer - sends queued messages (so a slow client never blocks the game)
  * tick thread        - advances every room 60 times per second, broadcasts state
  * monitor thread     - heartbeat: drops clients that have been silent too long
  * console thread     - admin commands typed into the server window (status, kick, quit)

All shared state (sessions, rooms) is protected by one re-entrant lock.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import traceback
from collections import deque

from common import protocol as P

from .levels import LEVELS
from .room import WAITING, Room
from .users import UserDB

MAX_SEND_QUEUE = 500
LOBBY_BROADCAST_INTERVAL = 0.2


class ClientHandler:
    """One connected TCP client."""

    def __init__(self, server: "GameServer", sock: socket.socket, addr):
        self.server = server
        self.sock = sock
        self.addr = f"{addr[0]}:{addr[1]}"
        self.user: str | None = None
        self.last_recv = time.monotonic()
        self.close_reason = "connection closed"
        self._cond = threading.Condition()
        self._queue: deque[bytes] = deque()
        self._state: bytes | None = None
        self._closing = False       # graceful: flush the queue, then close
        self._closed = False

    def start(self):
        threading.Thread(target=self._read_loop, name=f"read-{self.addr}", daemon=True).start()
        threading.Thread(target=self._write_loop, name=f"write-{self.addr}", daemon=True).start()

    # ---------------------------------------------------------------- sending
    def send(self, msg: dict):
        self.send_bytes(P.encode(msg))

    def send_bytes(self, data: bytes):
        with self._cond:
            if self._closing or self._closed:
                return
            if len(self._queue) < MAX_SEND_QUEUE:
                self._queue.append(data)
                self._cond.notify()
                return
        self.abort("too slow to receive data")

    def send_state_bytes(self, data: bytes):
        """Game snapshots replace each other: only the newest one is kept."""
        with self._cond:
            if not (self._closing or self._closed):
                self._state = data
                self._cond.notify()

    def _write_loop(self):
        while True:
            with self._cond:
                while not self._queue and self._state is None and not self._closing and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                batch = list(self._queue)
                self._queue.clear()
                state, self._state = self._state, None
                closing = self._closing
            try:
                for data in batch:
                    self.sock.sendall(data)
                if state is not None and not closing:
                    self.sock.sendall(state)
            except OSError:
                self.abort("send failed")
                return
            if closing:
                self._shutdown()
                return

    # -------------------------------------------------------------- receiving
    def _read_loop(self):
        decoder = P.FrameDecoder()
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    if not self._closed:
                        self.close_reason = "client closed the connection"
                    break
                self.last_recv = time.monotonic()
                for msg in decoder.feed(data):
                    self.server.dispatch(self, msg)
        except P.ProtocolError as exc:
            self.close_reason = f"protocol error: {exc}"
        except OSError as exc:
            if not self._closed:
                self.close_reason = f"connection error ({exc.__class__.__name__})"
        finally:
            self._shutdown()
            self.server.on_disconnect(self)

    # ---------------------------------------------------------------- closing
    def close_gracefully(self, reason: str):
        with self._cond:
            self.close_reason = reason
            self._closing = True
            self._cond.notify()

    def abort(self, reason: str):
        if not self._closed:
            self.close_reason = reason
        self._shutdown()

    def _shutdown(self):
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cond.notify_all()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class GameServer:
    def __init__(self, host="0.0.0.0", port=P.DEFAULT_PORT, data_dir=None,
                 heartbeat_timeout=P.HEARTBEAT_TIMEOUT, reconnect_timeout=P.RECONNECT_TIMEOUT,
                 countdown_seconds=3.0, level_clear_seconds=4.0, verbose=True):
        self.host, self.port = host, port
        data_dir = data_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        self.users = UserDB(os.path.join(data_dir, "users.json"))
        self.heartbeat_timeout = heartbeat_timeout
        self.reconnect_timeout = reconnect_timeout
        self.countdown_seconds = countdown_seconds
        self.level_clear_seconds = level_clear_seconds
        self.verbose = verbose

        self.lock = threading.RLock()
        self.handlers: set[ClientHandler] = set()
        self.sessions: dict[str, ClientHandler] = {}     # logged-in users
        self.rooms: dict[int, Room] = {}
        self.user_room: dict[str, int] = {}              # username -> room id
        self._next_room_id = 1
        self._lobby_dirty = True
        self._last_lobby = 0.0
        self.running = False
        self._listener: socket.socket | None = None

    # -------------------------------------------------------------- lifecycle
    def start(self):
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((self.host, self.port))
        self._listener.listen(32)
        self.port = self._listener.getsockname()[1]
        self.running = True
        for target, name in ((self._accept_loop, "accept"), (self._tick_loop, "tick"),
                             (self._monitor_loop, "heartbeat")):
            threading.Thread(target=target, name=name, daemon=True).start()
        self.log(f"Server listening on {self.host}:{self.port} (TCP)")

    def stop(self):
        self.running = False
        if self._listener:
            try:
                self._listener.close()
            except OSError:
                pass
        with self.lock:
            handlers = list(self.handlers)
        for h in handlers:
            h.abort("server shutting down")
        time.sleep(0.3)                 # let the client threads finish cleanly
        self.log("Server stopped")

    def log(self, text: str):
        if self.verbose:
            print(time.strftime("[%H:%M:%S] ") + text, flush=True)

    # ---------------------------------------------------------------- threads
    def _accept_loop(self):
        while self.running:
            try:
                sock, addr = self._listener.accept()
            except OSError:
                break
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # low latency
            handler = ClientHandler(self, sock, addr)
            with self.lock:
                self.handlers.add(handler)
            handler.start()
            handler.send({"type": "hello", "version": P.PROTOCOL_VERSION,
                          "server": "Co-op Block Adventure"})
            self.log(f"Connection from {handler.addr}")

    def _tick_loop(self):
        dt = 1.0 / P.TICK_RATE
        next_tick = time.perf_counter()
        while self.running:
            with self.lock:
                for room in list(self.rooms.values()):
                    try:
                        room.tick()
                    except Exception:
                        traceback.print_exc()
                now = time.monotonic()
                if self._lobby_dirty and now - self._last_lobby >= LOBBY_BROADCAST_INTERVAL:
                    self._lobby_dirty = False
                    self._last_lobby = now
                    self._broadcast_lobby()
            next_tick += dt
            delay = next_tick - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.25:
                next_tick = time.perf_counter()       # fell far behind: resynchronise

    def _monitor_loop(self):
        while self.running:
            time.sleep(0.5)
            now = time.monotonic()
            with self.lock:
                silent = [h for h in self.handlers if now - h.last_recv > self.heartbeat_timeout]
            for h in silent:
                self.log(f"Heartbeat timeout: {h.user or h.addr} silent for "
                         f"{now - h.last_recv:.1f} s")
                h.abort("heartbeat timeout")

    # --------------------------------------------------------------- dispatch
    def dispatch(self, h: ClientHandler, msg: dict):
        kind = msg.get("type")
        try:
            if kind == "ping":
                h.send({"type": "pong", "t": msg.get("t")})
            elif kind == "register":
                self._on_register(h, msg)
            elif kind == "login":
                self._on_login(h, msg)
            else:
                handler = getattr(self, f"_on_{kind}", None)
                if handler is None:
                    h.send({"type": "error", "msg": f"Unknown message type: {kind}"})
                    return
                with self.lock:
                    if h.user is None:
                        h.send({"type": "error", "msg": "Please log in first"})
                        return
                    handler(h, msg)
        except Exception:
            traceback.print_exc()
            h.send({"type": "error", "msg": "Internal server error"})

    # ---------------------------------------------------------- accounts
    def _on_register(self, h, msg):
        ok, text = self.users.register(msg.get("username"), msg.get("password"))
        h.send({"type": "register_result", "ok": ok, "msg": text})
        if ok:
            self.log(f"New member registered: {msg.get('username')}")

    def _on_login(self, h, msg):
        ok, text, name = self.users.verify(msg.get("username"), msg.get("password"))
        if not ok:
            h.send({"type": "login_result", "ok": False, "msg": text})
            return
        with self.lock:
            if h.user is not None:
                h.send({"type": "login_result", "ok": False, "msg": "Already logged in"})
                return
            old = self.sessions.get(name)
            if old is not None and old is not h:
                # same account logs in again (e.g. reconnecting before the old
                # connection timed out): the new connection takes over the seat
                old.user = None
                old.send({"type": "kicked", "reason": "Your account logged in from another window."})
                old.close_gracefully("replaced by a new login")
            h.user = name
            self.sessions[name] = h
            room = self.rooms.get(self.user_room.get(name))
            rejoin = room is not None and name in room.members
            progress = self.users.progress(name)
            h.send({"type": "login_result", "ok": True, "username": name, "rejoin": rejoin,
                    "progress": progress, "level_count": len(LEVELS),
                    "msg": f"Welcome, {name}! Levels unlocked: {progress['unlocked'] + 1}/{len(LEVELS)}, "
                           f"levels cleared: {progress['clears']}."})
            self.log(f"{name} logged in from {h.addr}" + (" (rejoining room %d)" % room.id if rejoin else ""))
            if rejoin:
                room.member_reconnected(name, h)
            else:
                self.user_room.pop(name, None)
                h.send(self.lobby_snapshot())
            self.mark_lobby_dirty()

    def _on_logout(self, h, msg):
        name = h.user
        self._leave_room(name, "left the game")
        if self.sessions.get(name) is h:
            del self.sessions[name]
        h.user = None
        h.send({"type": "logout_ok"})
        self.log(f"{name} logged out")
        self.mark_lobby_dirty()

    def on_disconnect(self, h: ClientHandler):
        """Called once when a connection ends.  If the user never logged out,
        this is an abnormal disconnection: keep their seat and pause the game."""
        with self.lock:
            self.handlers.discard(h)
            name = h.user
            if name is None or self.sessions.get(name) is not h:
                self.log(f"Connection {h.addr} closed ({h.close_reason})")
                return
            del self.sessions[name]
            h.user = None
            self.log(f"{name} disconnected abnormally: {h.close_reason}")
            room = self.rooms.get(self.user_room.get(name))
            if room is not None and name in room.members:
                room.member_dropped(name)
            self.mark_lobby_dirty()

    # ---------------------------------------------------------------- rooms
    def _room_of(self, name: str) -> Room | None:
        room = self.rooms.get(self.user_room.get(name))
        return room if room is not None and name in room.members else None

    def _on_list_rooms(self, h, msg):
        h.send(self.lobby_snapshot())

    def _on_create_room(self, h, msg):
        if self._room_of(h.user):
            return h.send({"type": "error", "msg": "You are already in a room"})
        error = P.check_room_name(msg.get("name"))
        size = msg.get("size")
        if error or size not in P.ROOM_SIZES:
            return h.send({"type": "error", "msg": error or "Room size must be 2, 3 or 4"})
        self._create_and_join(h, P.clean_text(msg.get("name"), P.ROOM_NAME_MAX_LEN), size)

    def _create_and_join(self, h, name, size):
        room = Room(self, self._next_room_id, name, size)
        self._next_room_id += 1
        self.rooms[room.id] = room
        self.log(f"{h.user} created room {room.id} '{name}' for {size} players")
        self._join(h, room)

    def _join(self, h, room: Room):
        self.user_room[h.user] = room.id
        room.add_member(h.user, h)
        self.mark_lobby_dirty()

    def _on_join_room(self, h, msg):
        if self._room_of(h.user):
            return h.send({"type": "error", "msg": "You are already in a room"})
        room = self.rooms.get(msg.get("room_id"))
        if room is None:
            return h.send({"type": "error", "msg": "That room no longer exists"})
        if room.phase != WAITING:
            return h.send({"type": "error", "msg": "That room is already playing"})
        if room.missing == 0:
            return h.send({"type": "error", "msg": "That room is full"})
        self._join(h, room)

    def _on_quick_match(self, h, msg):
        """Server-side matchmaking: put the player in the fullest open room."""
        if self._room_of(h.user):
            return h.send({"type": "error", "msg": "You are already in a room"})
        size = msg.get("size")
        if size is not None and size not in P.ROOM_SIZES:
            size = None
        open_rooms = [r for r in self.rooms.values()
                      if r.phase == WAITING and r.missing > 0 and (size is None or r.size == size)]
        if open_rooms:
            room = min(open_rooms, key=lambda r: (r.missing, r.id))
            self._join(h, room)
        else:
            self._create_and_join(h, f"Quick Match {self._next_room_id}", size or 2)

    def _on_leave_room(self, h, msg):
        self._leave_room(h.user, "left the room")
        h.send({"type": "room_left"})
        h.send(self.lobby_snapshot())

    def _leave_room(self, name, reason):
        room = self._room_of(name)
        if room is not None:
            room.remove_member(name, reason)
        self.user_room.pop(name, None)
        self.mark_lobby_dirty()

    def forget_member(self, name: str, room: Room):
        if self.user_room.get(name) == room.id:
            del self.user_room[name]

    def delete_room(self, room: Room):
        if self.rooms.pop(room.id, None) is not None:
            self.log(f"Room {room.id} '{room.name}' closed")
            self.mark_lobby_dirty()

    # ------------------------------------------------------- in-room actions
    def _require_room(self, h) -> Room | None:
        room = self._room_of(h.user)
        if room is None:
            h.send({"type": "error", "msg": "You are not in a room"})
        return room

    def _on_select_level(self, h, msg):
        room = self._require_room(h)
        if room:
            error = room.select_level(h.user, msg.get("level"))
            if error:
                h.send({"type": "error", "msg": error})

    def _on_ready(self, h, msg):
        room = self._require_room(h)
        if room:
            room.set_ready(h.user, msg.get("ready") is True)

    def _on_input(self, h, msg):
        room = self._room_of(h.user)
        if room:
            room.handle_input(h.user, msg)

    def _on_pause(self, h, msg):
        room = self._require_room(h)
        if room:
            room.toggle_pause(h.user)

    def _on_restart(self, h, msg):
        room = self._require_room(h)
        if room:
            room.vote_restart(h.user)

    def _on_chat(self, h, msg):
        text = P.clean_text(msg.get("text"), P.CHAT_MAX_LEN)
        if not text:
            return
        room = self._room_of(h.user)
        if room:
            room.chat(h.user, text)
            return
        data = P.encode({"type": "chat", "channel": "lobby", "from": h.user, "text": text})
        for name, other in self.sessions.items():
            if self._room_of(name) is None:
                other.send_bytes(data)

    # ---------------------------------------------------------------- lobby
    def mark_lobby_dirty(self):
        self._lobby_dirty = True

    def lobby_snapshot(self) -> dict:
        rooms = [r.summary() for r in sorted(self.rooms.values(), key=lambda r: r.id)]
        return {
            "type": "lobby",
            "rooms": rooms,
            "online": [{"name": n, "room": self.user_room.get(n)} for n in sorted(self.sessions)],
            "games_in_progress": sum(1 for r in self.rooms.values() if r.in_game),
        }

    def _broadcast_lobby(self):
        data = P.encode(self.lobby_snapshot())
        for h in self.sessions.values():
            h.send_bytes(data)

    # -------------------------------------------------------------- console
    def status_text(self) -> str:
        with self.lock:
            lines = [f"=== {time.strftime('%H:%M:%S')}  connections: {len(self.handlers)}, "
                     f"online: {len(self.sessions)}, rooms: {len(self.rooms)}, "
                     f"games in progress: {sum(1 for r in self.rooms.values() if r.in_game)} ==="]
            for name in sorted(self.sessions):
                rid = self.user_room.get(name)
                lines.append(f"  user {name:<16} {'room %d' % rid if rid else 'lobby'}")
            for room in sorted(self.rooms.values(), key=lambda r: r.id):
                level = (f"playing level {room.level_index + 1}" if room.world
                         else f"selected level {room.selected + 1}")
                lines.append(f"  room {room.id} '{room.name}' {len(room.members)}/{room.size} "
                             f"[{room.phase.upper()}, {level}] missing {room.missing}, host {room.host}")
                for m in room.members.values():
                    if room.phase == WAITING:
                        flags = ("ready" if m.ready else "not ready") + f", unlocked up to level {m.unlocked + 1}"
                    elif m.connected:
                        flags = "online"
                    else:
                        flags = f"OFFLINE, removed in {max(0, int(m.deadline - time.monotonic()))} s"
                    lines.append(f"      - {m.name} ({flags})")
            return "\n".join(lines)

    def kick(self, name: str) -> bool:
        with self.lock:
            h = self.sessions.get(name)
            if h is None:
                return False
            h.send({"type": "kicked", "reason": "You were kicked by the server administrator."})
            self._on_logout(h, {})
            h.close_gracefully("kicked")
            return True

    def console(self):
        print("Commands: status | kick <user> | quit", flush=True)
        for line in sys.stdin:
            cmd, _, arg = line.strip().partition(" ")
            if cmd in ("status", "s", "rooms", "users"):
                print(self.status_text(), flush=True)
            elif cmd == "kick":
                print("kicked" if self.kick(arg.strip()) else "no such user", flush=True)
            elif cmd in ("quit", "exit", "q"):
                self.running = False
                return
            elif cmd:
                print("Commands: status | kick <user> | quit", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Co-op Block Adventure server")
    parser.add_argument("--host", default="0.0.0.0", help="interface to listen on (default: all)")
    parser.add_argument("--port", type=int, default=P.DEFAULT_PORT)
    parser.add_argument("--data", default=None, help="folder for users.json")
    args = parser.parse_args(argv)

    server = GameServer(args.host, args.port, args.data)
    server.start()
    threading.Thread(target=server.console, name="console", daemon=True).start()
    try:
        while server.running:
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
