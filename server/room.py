"""A room = one group of players and the game they play together.

Game-flow state machine (the server tells every client the current phase, so
players always know whether they may act):

    waiting   - the host picks a level, everybody presses Ready
    waiting   --(room full and everyone ready)--> countdown --(3 s)--> playing
    playing   --(everyone through the door)--> level_clear --(4 s)--> waiting
    countdown/playing --(a player drops or presses P)--> paused --(all back)--> countdown

Level unlocking: every account remembers the highest level it has unlocked
(clearing level N unlocks level N+1).  In a room only levels that EVERY member
has unlocked can be picked, so nobody skips a level they have not earned.

All methods run while the server's global lock is held.
"""
from __future__ import annotations

import math
import time
from collections import deque

from common.protocol import STATE_RATE, TICK_RATE, encode

from .game import World
from .levels import LEVELS

WAITING = "waiting"
COUNTDOWN = "countdown"
PLAYING = "playing"
PAUSED = "paused"
LEVEL_CLEAR = "level_clear"
IN_GAME = {COUNTDOWN, PLAYING, PAUSED, LEVEL_CLEAR}

MIN_PLAYERS = 2
CHAT_HISTORY = 40
LAST_LEVEL = len(LEVELS) - 1


class Member:
    def __init__(self, name: str, color: int, conn, unlocked: int):
        self.name = name
        self.color = color
        self.conn = conn            # ClientHandler, or None while disconnected
        self.unlocked = unlocked    # highest level index this member may play
        self.ready = False
        self.connected = True
        self.deadline = None        # monotonic time at which a dropped player is removed


class Room:
    _level_serial = 0

    def __init__(self, server, room_id: int, name: str, size: int):
        self.server = server
        self.id = room_id
        self.name = name
        self.size = size
        self.members: dict[str, Member] = {}
        self.host: str | None = None
        self.selected = 0           # level index chosen by the host
        self.phase = WAITING
        self.world: World | None = None
        self.level_index = 0
        self.level_serial = 0
        self.timer = 0
        self.manual_pause: str | None = None
        self.restart_votes: set[str] = set()
        self.chat_log: deque = deque(maxlen=CHAT_HISTORY)
        self._ticks = 0

    # ------------------------------------------------------------ properties
    @property
    def missing(self) -> int:
        return max(0, self.size - len(self.members))

    @property
    def in_game(self) -> bool:
        return self.phase in IN_GAME

    @property
    def max_level(self) -> int:
        """Highest level index that every member has unlocked."""
        return min((m.unlocked for m in self.members.values()), default=0)

    def _disconnected(self) -> list[Member]:
        return [m for m in self.members.values() if not m.connected]

    def _connected(self) -> list[Member]:
        return [m for m in self.members.values() if m.connected]

    def info(self) -> dict:
        now = time.monotonic()
        return {
            "type": "room",
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "phase": self.phase,
            "missing": self.missing,
            "host": self.host,
            "selected": self.selected,
            "max_level": self.max_level,
            "level": self.level_index + 1 if self.world else 0,
            "level_count": len(LEVELS),
            "levels": [{"name": lv["name"], "hint": lv.get("hint", "")} for lv in LEVELS],
            "manual_pause": self.manual_pause,
            "members": [{
                "name": m.name, "color": m.color, "ready": m.ready, "connected": m.connected,
                "unlocked": m.unlocked,
                "reconnect_in": max(0, int(m.deadline - now)) if m.deadline else None,
            } for m in self.members.values()],
        }

    def summary(self) -> dict:
        return {"id": self.id, "name": self.name, "size": self.size, "count": len(self.members),
                "missing": self.missing, "phase": self.phase, "host": self.host,
                "selected": self.selected + 1,
                "level": self.level_index + 1 if self.world else 0, "level_count": len(LEVELS),
                "members": list(self.members)}

    # ------------------------------------------------------------- messaging
    def broadcast(self, msg: dict):
        data = encode(msg)
        for m in self.members.values():
            if m.connected and m.conn is not None:
                m.conn.send_bytes(data)

    def broadcast_room(self):
        self.broadcast(self.info())
        self.server.mark_lobby_dirty()

    def chat(self, sender: str, text: str):
        msg = {"type": "chat", "channel": "room", "from": sender, "text": text}
        self.chat_log.append(msg)
        self.broadcast(msg)

    def notice(self, text: str):
        msg = {"type": "chat", "channel": "room", "from": None, "text": text, "system": True}
        self.chat_log.append(msg)
        self.broadcast(msg)
        self.server.log(f"[room {self.id}] {text}")

    def _state_msg(self) -> dict:
        msg = {"type": "state", "serial": self.level_serial, "phase": self.phase,
               "votes": len(self.restart_votes), "votes_needed": self._votes_needed()}
        msg.update(self.world.snapshot())
        if self.phase == COUNTDOWN:
            msg["countdown"] = math.ceil(self.timer / TICK_RATE)
        elif self.phase == PAUSED:
            now = time.monotonic()
            msg["pause"] = {
                "by": self.manual_pause,
                "waiting": [{"name": m.name, "remaining": max(0, int(m.deadline - now))}
                            for m in self._disconnected() if m.deadline],
            }
        return msg

    def broadcast_state(self):
        if self.world is None:
            return
        data = encode(self._state_msg())
        for m in self.members.values():
            if m.connected and m.conn is not None:
                m.conn.send_state_bytes(data)     # latest-only: never queues up stale frames

    def _send_full_state(self, conn):
        """Everything a (re)joining client needs to draw the room."""
        conn.send(self.info())
        for msg in self.chat_log:
            conn.send(msg)
        if self.world is not None:
            conn.send(self._level_msg())
            conn.send(self._state_msg())

    # ------------------------------------------------------------ membership
    def add_member(self, name: str, conn):
        used = {m.color for m in self.members.values()}
        color = next(c for c in range(4) if c not in used)
        unlocked = self.server.users.progress(name)["unlocked"]
        self.members[name] = Member(name, color, conn, unlocked)
        if self.host is None:
            self.host = name
        self._send_full_state(conn)
        self.notice(f"{name} joined the room." + (f" Waiting for {self.missing} more player(s)."
                                                  if self.missing else " The room is full!"))
        self._clamp_selection()
        self.broadcast_room()

    def remove_member(self, name: str, reason: str):
        """Remove a member for good (left, logged out, or never came back)."""
        if self.members.pop(name, None) is None:
            return
        self.restart_votes.discard(name)
        if self.manual_pause == name:
            self.manual_pause = None
        self.server.forget_member(name, self)
        if not self.members:
            self.server.delete_room(self)
            return
        self.notice(f"{name} {reason}.")
        if self.host == name:
            self.host = next(iter(self.members))
            self.notice(f"{self.host} is now the host and picks the level.")
        if self.in_game:
            if len(self.members) < MIN_PLAYERS:
                self.notice("Not enough players to continue - back to the level select.")
                self._back_to_waiting()
            elif self.phase in (COUNTDOWN, PLAYING, PAUSED):
                self.notice("Restarting the level with the remaining players.")
                self._load_level(self.level_index)
        elif self.missing:
            self.notice(f"Waiting for {self.missing} more player(s).")
        self.broadcast_room()

    def member_dropped(self, name: str):
        """The member's connection broke without a proper logout."""
        m = self.members.get(name)
        if m is None:
            return
        if not self.in_game:
            self.remove_member(name, "lost connection")
            return
        m.connected = False
        m.conn = None
        m.deadline = time.monotonic() + self.server.reconnect_timeout
        self.restart_votes.discard(name)
        self.notice(f"{name} lost connection! Waiting up to "
                    f"{int(self.server.reconnect_timeout)} s for them to come back...")
        if self.phase in (COUNTDOWN, PLAYING):
            self._enter_pause()
        self.broadcast_room()

    def member_reconnected(self, name: str, conn):
        m = self.members[name]
        was_dropped = not m.connected
        m.conn = conn
        m.connected = True
        m.deadline = None
        self._send_full_state(conn)
        if was_dropped:
            self.notice(f"{name} reconnected!")
            if self.phase == PAUSED and not self._disconnected() and self.manual_pause is None:
                self._start_countdown()
        self.broadcast_room()

    def _expire_dropped(self):
        now = time.monotonic()
        for m in list(self.members.values()):
            if not m.connected and m.deadline is not None and now >= m.deadline:
                self.remove_member(m.name, "did not reconnect in time and was removed")
                if self.server.rooms.get(self.id) is not self:
                    return          # room was deleted

    # ------------------------------------------------------- level selection
    def _clamp_selection(self):
        """Keep the selected level playable for everyone currently in the room."""
        if self.selected > self.max_level:
            locked_for = [m.name for m in self.members.values() if m.unlocked < self.selected]
            old = self.selected
            self.selected = self.max_level
            self.notice(f"Level {old + 1} is still locked for {', '.join(locked_for)} - "
                        f"switched to level {self.selected + 1}.")

    def select_level(self, name: str, index) -> str | None:
        """Host picks the level.  Returns an error message, or None on success."""
        if self.phase != WAITING:
            return "You can only pick a level before the game starts"
        if name != self.host:
            return f"Only the host ({self.host}) can pick the level"
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(LEVELS):
            return "There is no such level"
        if index > self.max_level:
            locked_for = [m.name for m in self.members.values() if m.unlocked < index]
            return (f"Level {index + 1} is locked for {', '.join(locked_for)}. "
                    f"Clear level {index} first.")
        if index != self.selected:
            self.selected = index
            for m in self.members.values():
                m.ready = False         # everybody confirms the new choice
            self.notice(f"{name} picked level {index + 1}: {LEVELS[index]['name']}. Press Ready!")
            self.broadcast_room()
        return None

    # ------------------------------------------------------------ game flow
    def set_ready(self, name: str, ready: bool):
        if self.phase != WAITING:
            return
        self.members[name].ready = ready
        self.broadcast_room()
        if len(self.members) == self.size and all(m.ready for m in self.members.values()):
            self.notice(f"Everyone is ready - starting level {self.selected + 1}!")
            self._load_level(self.selected)

    def _level_msg(self) -> dict:
        level = LEVELS[self.level_index]
        msg = {"type": "level", "serial": self.level_serial, "index": self.level_index,
               "count": len(LEVELS), "name": level["name"], "hint": level.get("hint", "")}
        msg.update(self.world.static_info())
        return msg

    def _load_level(self, index: int):
        Room._level_serial += 1
        self.level_serial = Room._level_serial
        self.level_index = index
        self.world = World(LEVELS[index], [(m.name, m.color) for m in self.members.values()])
        self.restart_votes.clear()
        self.broadcast(self._level_msg())
        self._start_countdown()

    def _start_countdown(self):
        if self._disconnected():
            self._enter_pause()
            return
        self.phase = COUNTDOWN
        self.timer = int(self.server.countdown_seconds * TICK_RATE)
        self.world.clear_inputs()
        self.broadcast_room()
        self.broadcast_state()

    def _enter_pause(self):
        self.phase = PAUSED
        self.world.clear_inputs()
        self.broadcast_room()
        self.broadcast_state()

    def _back_to_waiting(self):
        self.phase = WAITING
        self.world = None
        self.manual_pause = None
        self.restart_votes.clear()
        for m in self.members.values():
            m.ready = False
        for m in self._disconnected():          # no seat reservations outside a game
            self.members.pop(m.name, None)
            self.server.forget_member(m.name, self)
            if self.host == m.name:
                self.host = None
        if not self.members:
            self.server.delete_room(self)
            return
        if self.host is None:
            self.host = next(iter(self.members))
        self._clamp_selection()
        self.broadcast_room()

    def toggle_pause(self, name: str):
        if self.phase in (COUNTDOWN, PLAYING):
            self.manual_pause = name
            self.notice(f"{name} paused the game (press P to resume).")
            self._enter_pause()
        elif self.phase == PAUSED and self.manual_pause is not None:
            self.manual_pause = None
            self.notice(f"{name} resumed the game.")
            if not self._disconnected():
                self._start_countdown()
            else:
                self.broadcast_room()

    def _votes_needed(self) -> int:
        return len(self._connected()) // 2 + 1

    def vote_restart(self, name: str):
        if self.phase != PLAYING:
            return
        if name in self.restart_votes:
            self.restart_votes.discard(name)
        else:
            self.restart_votes.add(name)
        needed = self._votes_needed()
        if len(self.restart_votes) >= needed:
            self.notice("Restart vote passed - restarting the level.")
            self._load_level(self.level_index)
        else:
            self.notice(f"{name} wants to restart the level ({len(self.restart_votes)}/{needed} votes, "
                        f"press R to agree).")

    def handle_input(self, name: str, msg: dict):
        if self.phase != PLAYING or self.world is None:
            return              # actions are only legal while playing
        self.world.set_input(name, msg.get("left") is True, msg.get("right") is True,
                             msg.get("jump") is True)

    def _on_world_event(self, event: tuple):
        kind = event[0]
        if kind == "key":
            self.notice(f"{event[1]} picked up the key!")
        elif kind == "key_lost":
            self.notice("The key flew back to where it was.")
        elif kind == "door":
            self.notice(f"{event[1]} unlocked the door! Everybody in!")

    def _finish_level(self):
        index, seconds = self.level_index, self.world.time
        self.phase = LEVEL_CLEAR
        self.timer = int(self.server.level_clear_seconds * TICK_RATE)
        results = self.server.users.record_clear(list(self.members), index, seconds, LAST_LEVEL)
        for m in self.members.values():
            res = results.get(m.name)
            if res is None:
                continue
            m.unlocked = res["unlocked"]
            if m.connected and m.conn is not None:
                m.conn.send({"type": "level_result", "index": index, "time": round(seconds, 2),
                             "best": res["best"], "record": res["record"],
                             "unlocked": res["unlocked"], "newly_unlocked": res["newly_unlocked"],
                             "last": index == LAST_LEVEL})
        self.notice(f"Level {index + 1} cleared in {seconds:.1f} s!")
        if index == LAST_LEVEL:
            self.notice("That was the final level - you finished the whole adventure!")
        self.selected = min(index + 1, LAST_LEVEL)     # suggest the next level
        self.broadcast_room()

    def tick(self):
        if self.phase == WAITING or self.world is None:
            return
        self._expire_dropped()
        if self.world is None or self.server.rooms.get(self.id) is not self:
            return

        if self.phase == COUNTDOWN:
            self.timer -= 1
            if self.timer <= 0:
                self.phase = PLAYING
                self.broadcast_room()
        elif self.phase == PLAYING:
            self.world.step()
            for event in self.world.pop_events():
                self._on_world_event(event)
            if self.world.cleared:
                self._finish_level()
        elif self.phase == PAUSED:
            if not self._disconnected() and self.manual_pause is None:
                self._start_countdown()
        elif self.phase == LEVEL_CLEAR:
            self.timer -= 1
            if self.timer <= 0:
                self.broadcast_state()
                self._back_to_waiting()
                return

        self._ticks += 1
        fast = self.phase in (COUNTDOWN, PLAYING)
        every = TICK_RATE // STATE_RATE if fast else TICK_RATE // 4
        if self._ticks % every == 0:
            self.broadcast_state()
