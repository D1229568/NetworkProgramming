"""Co-op Block Adventure - TCP game server.

Threads:
  * accept thread      - accepts TCP connections, one ClientHandler each
  * per client: reader - TLS handshake, then decodes frames and dispatches them
                writer - sends queued messages (so a slow client never blocks the game)
  * tick thread        - advances every room 60 times per second, broadcasts state
  * monitor thread     - heartbeat: drops clients that have been silent too long
  * discovery thread   - answers UDP broadcast "is there a server?" requests on the LAN
  * console thread     - admin commands typed into the server window (status, kick, quit)

All shared state (sessions, rooms, invitations) is protected by one re-entrant lock.
"""
from __future__ import annotations

import argparse
import os
import secrets
import socket
import ssl
import sys
import threading
import time
import traceback
from collections import deque

from common import discovery as D
from common import protocol as P
from common.transport import PlainTransport, TlsTransport, fingerprint, server_context

from .certgen import ensure_certificate
from .levels import LEVELS
from .room import WAITING, Room
from .users import UserDB

MAX_SEND_QUEUE = 500
LOBBY_BROADCAST_INTERVAL = 0.2
INVITE_TTL = 30.0
INVITE_COOLDOWN = 5.0
DISCOVERY_REPLIES_PER_SECOND = 20


class ClientHandler:
    """One connected TCP client."""

    def __init__(self, server: "GameServer", sock: socket.socket, addr, transport):
        self.server = server
        self.sock = sock
        self.transport = transport
        self.addr = f"{addr[0]}:{addr[1]}"
        self.user: str | None = None
        self.last_recv = time.monotonic()
        self.close_reason = "connection closed"
        self._cond = threading.Condition()
        self._queue: deque[bytes] = deque()
        self._state: bytes | None = None
        self._ready = False         # True after the TLS handshake
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
                while not self._closed and (not self._ready or
                                            (not self._queue and self._state is None and not self._closing)):
                    self._cond.wait()
                if self._closed:
                    return
                batch = list(self._queue)
                self._queue.clear()
                state, self._state = self._state, None
                closing = self._closing
            try:
                for data in batch:
                    self.transport.sendall(data)
                if state is not None and not closing:
                    self.transport.sendall(state)
            except (OSError, ssl.SSLError):
                self.abort("send failed")
                return
            if closing:
                self._shutdown()
                return

    # -------------------------------------------------------------- receiving
    def _read_loop(self):
        decoder = P.FrameDecoder()
        try:
            try:
                self.transport.handshake()
            except (OSError, ssl.SSLError, ConnectionError) as exc:
                self.close_reason = f"TLS handshake failed ({exc.__class__.__name__}) - client without TLS?"
                return
            with self._cond:
                self._ready = True
                self._cond.notify()
            while True:
                data = self.transport.recv()
                if not data:
                    if not self._closed:
                        self.close_reason = "client closed the connection"
                    break
                self.last_recv = time.monotonic()
                for msg in decoder.feed(data):
                    self.server.dispatch(self, msg)
        except P.ProtocolError as exc:
            self.close_reason = f"protocol error: {exc}"
        except (OSError, ssl.SSLError) as exc:
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
        self.transport.close()


class GameServer:
    def __init__(self, host="0.0.0.0", port=P.DEFAULT_PORT, data_dir=None,
                 heartbeat_timeout=P.HEARTBEAT_TIMEOUT, reconnect_timeout=P.RECONNECT_TIMEOUT,
                 countdown_seconds=3.0, level_clear_seconds=4.0, verbose=True,
                 tls=True, cert_file=None, key_file=None,
                 discovery=True, discovery_port=D.DISCOVERY_PORT, name=None):
        self.host, self.port = host, port
        self.data_dir = data_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        self.users = UserDB(os.path.join(self.data_dir, "users.json"))
        self.heartbeat_timeout = heartbeat_timeout
        self.reconnect_timeout = reconnect_timeout
        self.countdown_seconds = countdown_seconds
        self.level_clear_seconds = level_clear_seconds
        self.verbose = verbose
        self.tls = tls
        self.cert_file, self.key_file = cert_file, key_file
        self.fingerprint: str | None = None
        self._ssl_context = None
        self.discovery = discovery
        self.discovery_port = discovery_port
        self.name = name or f"{socket.gethostname()}'s server"
        self.server_id = secrets.token_hex(4)

        self.lock = threading.RLock()
        self.handlers: set[ClientHandler] = set()
        self.sessions: dict[str, ClientHandler] = {}     # logged-in users
        self.rooms: dict[int, Room] = {}
        self.user_room: dict[str, int] = {}              # username -> room id
        self.invites: dict[tuple, dict] = {}             # (invitee, room id) -> invitation
        self._next_room_id = 1
        self._lobby_dirty = True
        self._last_lobby = 0.0
        self.running = False
        self._listener: socket.socket | None = None
        self._udp: socket.socket | None = None

    # -------------------------------------------------------------- lifecycle
    def start(self):
        if self.tls:
            if not (self.cert_file and self.key_file):
                self.cert_file, self.key_file, created = ensure_certificate(self.data_dir)
                if created:
                    self.log("Created a new self-signed TLS certificate in " + self.data_dir)
            self._ssl_context = server_context(self.cert_file, self.key_file)
            with open(self.cert_file, encoding="ascii") as f:
                self.fingerprint = fingerprint(ssl.PEM_cert_to_DER_cert(f.read()))

        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((self.host, self.port))
        self._listener.listen(32)
        self.port = self._listener.getsockname()[1]
        self.running = True
        threads = [(self._accept_loop, "accept"), (self._tick_loop, "tick"), (self._monitor_loop, "heartbeat")]
        if self.discovery and self._open_discovery_socket():
            threads.append((self._discovery_loop, "discovery"))
        for target, name in threads:
            threading.Thread(target=target, name=name, daemon=True).start()

        self.log(f"Server '{self.name}' listening on {self.host}:{self.port} (TCP)")
        if self.tls:
            self.log("TLS is ON. Certificate SHA-256 fingerprint:")
            self.log("   " + self.fingerprint)
        else:
            self.log("TLS is OFF (--no-tls): passwords and messages travel as plain text!")
        if self._udp is not None:
            self.log(f"LAN discovery answering on UDP port {self.discovery_port}")

    def _open_discovery_socket(self) -> bool:
        try:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.bind(("" if self.host == "0.0.0.0" else self.host, self.discovery_port))
        except OSError as exc:
            self.log(f"LAN discovery disabled: cannot use UDP port {self.discovery_port} ({exc})")
            return False
        self._udp = udp
        self.discovery_port = udp.getsockname()[1]
        return True

    def stop(self):
        self.running = False
        for sock in (self._listener, self._udp):
            if sock is not None:
                try:
                    sock.close()
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
            if self.tls:
                transport = TlsTransport(sock, self._ssl_context, server_side=True)
            else:
                transport = PlainTransport(sock)
            handler = ClientHandler(self, sock, addr, transport)
            with self.lock:
                self.handlers.add(handler)
            handler.start()
            handler.send({"type": "hello", "version": P.PROTOCOL_VERSION, "server": "Co-op Block Adventure",
                          "name": self.name, "tls": self.tls})
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
                for key in [k for k, inv in self.invites.items() if now > inv["expires"]]:
                    del self.invites[key]
            for h in silent:
                self.log(f"Heartbeat timeout: {h.user or h.addr} silent for "
                         f"{now - h.last_recv:.1f} s")
                h.abort("heartbeat timeout")

    def discovery_info(self) -> dict:
        with self.lock:
            open_rooms = sum(1 for r in self.rooms.values() if r.phase == WAITING and r.missing > 0)
            return {"id": self.server_id, "name": self.name, "port": self.port, "tls": self.tls,
                    "fingerprint": self.fingerprint, "version": P.PROTOCOL_VERSION,
                    "players": len(self.sessions), "rooms": len(self.rooms), "open_rooms": open_rooms}

    def _discovery_loop(self):
        """Answer 'is there a server?' broadcasts (UDP), with a small per-sender rate limit."""
        budget: dict[str, list] = {}
        while self.running:
            try:
                data, addr = self._udp.recvfrom(D.MAX_DATAGRAM + 1)
            except OSError:
                if not self.running:
                    return
                continue            # Windows reports ICMP errors from earlier replies here
            if not D.is_request(data):
                continue
            now = time.monotonic()
            window = budget.setdefault(addr[0], [now, 0])
            if now - window[0] > 1.0:
                window[0], window[1] = now, 0
            window[1] += 1
            if window[1] > DISCOVERY_REPLIES_PER_SECOND:
                continue
            try:
                self._udp.sendto(D.make_reply(self.discovery_info()), addr)
            except OSError:
                pass

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

    @staticmethod
    def notice(h: ClientHandler, text: str, kind: str = "info"):
        h.send({"type": "notice", "text": text, "kind": kind})

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

    def _join_error(self, room: Room | None) -> str | None:
        if room is None:
            return "That room no longer exists"
        if room.phase != WAITING:
            return "That room is already playing"
        if room.missing == 0:
            return "That room is full"
        return None

    def _on_join_room(self, h, msg):
        if self._room_of(h.user):
            return h.send({"type": "error", "msg": "You are already in a room"})
        room = self.rooms.get(msg.get("room_id"))
        error = self._join_error(room)
        if error:
            return h.send({"type": "error", "msg": error})
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

    # ------------------------------------------------------------ invitations
    def _on_invite(self, h, msg):
        """Invite an online player who is in the lobby to your (waiting) room."""
        room = self._require_room(h)
        if room is None:
            return
        to = msg.get("to")
        target = self.sessions.get(to) if isinstance(to, str) else None
        if to == h.user:
            return h.send({"type": "error", "msg": "You cannot invite yourself"})
        if room.phase != WAITING or room.missing == 0:
            return h.send({"type": "error", "msg": "Your room is full or already playing"})
        if target is None:
            return h.send({"type": "error", "msg": f"{to} is not online"})
        if self._room_of(to):
            return h.send({"type": "error", "msg": f"{to} is already in a room"})
        now = time.monotonic()
        key = (to, room.id)
        previous = self.invites.get(key)
        if previous and previous["from"] == h.user and now - previous["sent"] < INVITE_COOLDOWN:
            return h.send({"type": "error", "msg": f"You just invited {to} - give them a moment"})
        self.invites[key] = {"from": h.user, "sent": now, "expires": now + INVITE_TTL}
        target.send({"type": "invite", "from": h.user, "room_id": room.id, "room_name": room.name,
                     "size": room.size, "count": len(room.members), "level": room.selected + 1,
                     "expires_in": INVITE_TTL})
        self.notice(h, f"Invitation sent to {to}.", "ok")
        self.log(f"{h.user} invited {to} to room {room.id}")

    def _on_invite_reply(self, h, msg):
        key = (h.user, msg.get("room_id"))
        invite = self.invites.pop(key, None)
        if invite is None or time.monotonic() > invite["expires"]:
            return h.send({"type": "error", "msg": "That invitation has expired"})
        inviter = self.sessions.get(invite["from"])
        if msg.get("accept") is not True:
            if inviter is not None:
                self.notice(inviter, f"{h.user} declined your invitation.", "warn")
            return
        if self._room_of(h.user):
            return h.send({"type": "error", "msg": "You are already in a room"})
        room = self.rooms.get(key[1])
        error = self._join_error(room)
        if error:
            return h.send({"type": "error", "msg": error})
        self._join(h, room)

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
        def kb(n):
            return f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1048576:.2f} MB"

        with self.lock:
            lines = [f"=== {time.strftime('%H:%M:%S')}  connections: {len(self.handlers)}, "
                     f"online: {len(self.sessions)}, rooms: {len(self.rooms)}, "
                     f"games in progress: {sum(1 for r in self.rooms.values() if r.in_game)} ===",
                     f"  TLS: {'on, fingerprint ' + self.fingerprint[:23] + '...' if self.tls else 'OFF'}"
                     f"   discovery: {'UDP ' + str(self.discovery_port) if self._udp else 'off'}"]
            for h in sorted(self.handlers, key=lambda x: x.user or "~"):
                rid = self.user_room.get(h.user) if h.user else None
                who = h.user or "(not logged in)"
                where = f"room {rid}" if rid else "lobby" if h.user else ""
                lines.append(f"  user {who:<16} {where:<8} from {h.addr:<21} "
                             f"in {kb(h.transport.bytes_in):>9}  out {kb(h.transport.bytes_out):>9}")
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
    parser.add_argument("--data", default=None, help="folder for users.json and the TLS certificate")
    parser.add_argument("--name", default=None, help="server name shown to players on the LAN")
    parser.add_argument("--no-tls", action="store_true", help="plain TCP without encryption (for demos)")
    parser.add_argument("--no-discovery", action="store_true", help="do not answer LAN discovery (UDP)")
    args = parser.parse_args(argv)

    server = GameServer(args.host, args.port, args.data, tls=not args.no_tls,
                        discovery=not args.no_discovery, name=args.name)
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
