"""Server-authoritative game simulation (physics + rules).

The server is the only place where the game is simulated.  Clients send their
key states ("input" messages); every tick the server moves everybody, resolves
collisions, decides who holds the key, which buttons are pressed and whether
the level is cleared, and then broadcasts a snapshot.  Because there is a
single source of truth, clients can never disagree about the game state.

Coordinates are pixels, y grows downwards, one tile is 32x32.
"""
from __future__ import annotations

import math

from common.protocol import TICK_RATE

TILE = 32
GRAVITY = 0.6
JUMP_SPEED = 10.2          # rises ~81 px (2.5 tiles): 2-tile step OK, 3-tile wall needs help
JUMP_CUT_SPEED = 4.0       # releasing jump early gives a shorter hop
MOVE_SPEED = 3.2
PUSH_SPEED = 1.8
MAX_FALL_SPEED = 14.0
PLAYER_SIZE = 28
JUMP_BUFFER_TICKS = 6      # a jump pressed slightly before landing still counts
COYOTE_TICKS = 5           # ... and so does one pressed just after walking off a ledge
RESPAWN_FLASH_TICKS = 45
KEY_W, KEY_H = 20, 16
BUTTON_MIN_OVERLAP = 10    # px of a body's width that must be on a button plate
CORNER_NUDGE = 8           # px a jumping player may be slid around a ledge corner
EPS = 0.01

BUTTON_LETTERS = "abc"
GATE_LETTERS = "ABC"


class LevelError(ValueError):
    pass


def overlaps(ax, ay, aw, ah, bx, by, bw, bh) -> bool:
    """Strict rectangle overlap; rectangles that only touch do not overlap."""
    return (ax < bx + bw - EPS and ax + aw > bx + EPS
            and ay < by + bh - EPS and ay + ah > by + EPS)


def resolve_count(value, players: int) -> int:
    """'all' -> number of players; numbers are capped at the number of players."""
    if value == "all":
        return max(1, players)
    return max(1, min(int(value), players))


class Body:
    def __init__(self, x, y, w, h):
        self.x, self.y = float(x), float(y)
        self.w, self.h = w, h
        self.vy = 0.0
        self.dx = 0.0               # horizontal distance moved this tick (for carrying riders)
        self.on_ground = False
        self.standing_on = None     # Body under us (None = ground or nothing)

    @property
    def rect(self):
        return self.x, self.y, self.w, self.h


class Player(Body):
    def __init__(self, name: str, color: int, spawn):
        super().__init__(spawn[0], spawn[1], PLAYER_SIZE, PLAYER_SIZE)
        self.name = name
        self.color = color
        self.spawn = spawn
        self.left = self.right = self.jump_held = False
        self.jump_buffer = 0
        self.coyote = 0
        self.jumping = False
        self.facing = 1
        self.has_key = False
        self.entered = False        # reached the open exit door
        self.flash = 0              # blinking after a respawn

    @property
    def direction(self) -> int:
        return int(self.right) - int(self.left)

    def set_input(self, left: bool, right: bool, jump: bool):
        if jump and not self.jump_held:
            self.jump_buffer = JUMP_BUFFER_TICKS   # remember the press (edge), not just the level
        self.left, self.right, self.jump_held = bool(left), bool(right), bool(jump)

    def clear_input(self):
        self.left = self.right = self.jump_held = False
        self.jump_buffer = 0


class Box(Body):
    def __init__(self, idx: int, x, y, size: int, weight: int):
        super().__init__(x, y, size, size)
        self.idx = idx
        self.home = (float(x), float(y))
        self.weight = weight        # number of players that must push together
        self.pushers = 0


class Button:
    PLATE_H = 8

    def __init__(self, idx: int, letter: str, cell_x: int, cell_y: int, width: int, need: int):
        self.idx = idx
        self.letter = letter
        self.x, self.y = cell_x, cell_y + TILE - self.PLATE_H
        self.w, self.h = width, self.PLATE_H
        self.need = need
        self.count = 0

    @property
    def pressed(self) -> bool:
        return self.count >= self.need


class World:
    """One running level."""

    def __init__(self, level: dict, players: list[tuple[str, int]]):
        rows = level["map"]
        self.rows = len(rows)
        self.cols = len(rows[0]) if rows else 0
        if not rows or any(len(r) != self.cols for r in rows):
            raise LevelError("all map rows must have the same length")
        n = len(players)
        self.name = level.get("name", "")

        self.solid = [[False] * self.cols for _ in range(self.rows)]
        self.grid: list[str] = []          # static picture for clients: '#', '^', '.'
        self.spikes: list[tuple] = []
        self.gates: dict[str, list[tuple]] = {}
        self.boxes: list[Box] = []
        self.key_home = None
        self.door = None
        spawns: dict[int, tuple] = {}
        button_cells = []
        heavy_weight = level.get("heavy_weight", "all")

        for r, row in enumerate(rows):
            line = []
            for c, ch in enumerate(row):
                x, y = c * TILE, r * TILE
                cell = "."
                if ch == "#":
                    self.solid[r][c] = True
                    cell = "#"
                elif ch == "^":
                    self.spikes.append((x + 3, y + TILE - 12, TILE - 6, 12))
                    cell = "^"
                elif ch in "1234":
                    spawns[int(ch)] = (x + (TILE - PLAYER_SIZE) / 2, y + TILE - PLAYER_SIZE)
                elif ch == "K":
                    self.key_home = (x + (TILE - KEY_W) / 2, y + 8)
                elif ch == "D":
                    self.door = (x, y, TILE, TILE)
                elif ch == "o":
                    self.boxes.append(Box(len(self.boxes), x, y, TILE, 1))
                elif ch == "O":
                    self.boxes.append(Box(len(self.boxes), x, y, 2 * TILE,
                                          resolve_count(heavy_weight, n)))
                elif ch in BUTTON_LETTERS:
                    button_cells.append((r, c, ch))
                elif ch in GATE_LETTERS:
                    self.gates.setdefault(ch, []).append((x, y, TILE, TILE))
                elif ch != ".":
                    raise LevelError(f"unknown map character {ch!r}")
                line.append(cell)
            self.grid.append("".join(line))

        if not spawns:
            raise LevelError("level has no spawn point")

        # merge horizontally adjacent button cells with the same letter
        need_cfg = level.get("need", {})
        self.buttons: list[Button] = []
        button_cells.sort()
        i = 0
        while i < len(button_cells):
            r, c, ch = button_cells[i]
            j = i
            while j + 1 < len(button_cells) and button_cells[j + 1] == (r, button_cells[j][1] + 1, ch):
                j += 1
            self.buttons.append(Button(len(self.buttons), ch, c * TILE, r * TILE,
                                       (j - i + 1) * TILE, resolve_count(need_cfg.get(ch, 1), n)))
            i = j + 1

        links = level.get("links", {})
        self.links = {g: set(links.get(g, [g.lower()])) for g in self.gates}
        self.gate_open = {g: False for g in self.gates}

        self.players: list[Player] = []
        for i, (name, color) in enumerate(players):
            spawn = spawns.get(i + 1) or spawns[min(spawns)]
            self.players.append(Player(name, color, spawn))
        self.by_name = {p.name: p for p in self.players}

        self.key_holder: Player | None = None
        self.key_used = False
        self.door_open = self.key_home is None
        self.ticks = 0
        self.cleared = False
        self.events: list[tuple] = []
        self._bodies: list[Body] = list(self.players) + list(self.boxes)

    # ------------------------------------------------------------------ input
    def set_input(self, name: str, left: bool, right: bool, jump: bool):
        p = self.by_name.get(name)
        if p is not None and not p.entered:
            p.set_input(left, right, jump)

    def clear_inputs(self):
        for p in self.players:
            p.clear_input()

    def pop_events(self) -> list[tuple]:
        events, self.events = self.events, []
        return events

    @property
    def time(self) -> float:
        return self.ticks / TICK_RATE

    # -------------------------------------------------------------- collision
    def _solid_at(self, c: int, r: int) -> bool:
        if c < 0 or c >= self.cols or r < 0:
            return True
        if r >= self.rows:
            return False            # below the map: a bottomless pit
        return self.solid[r][c]

    def _obstacles(self, body, x, y, w, h, include_bodies=True):
        """Yield (x, y, w, h, obj) of everything solid near the given area."""
        c0, c1 = int(math.floor(x / TILE)), int(math.floor((x + w - EPS) / TILE))
        r0, r1 = int(math.floor(y / TILE)), int(math.floor((y + h - EPS) / TILE))
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                if self._solid_at(c, r):
                    yield c * TILE, r * TILE, TILE, TILE, None
        boxes_only = isinstance(body, Box)
        for letter, rects in self.gates.items():
            if boxes_only or not self.gate_open[letter]:   # gates never let boxes through
                for rect in rects:
                    yield (*rect, None)
        if include_bodies:
            for other in self._bodies:
                if other is not body:
                    yield other.x, other.y, other.w, other.h, other

    def _blocked(self, body, x, y, w, h, include_bodies=False) -> bool:
        return any(overlaps(x, y, w, h, ox, oy, ow, oh)
                   for ox, oy, ow, oh, _ in self._obstacles(body, x, y, w, h, include_bodies))

    def _corner_nudge(self, p: Player, dy: float):
        """Ledge forgiveness: if a rising player's head would clip the corner of
        a ledge by a few pixels, slide them sideways instead of stopping them."""
        if not self._blocked(p, p.x, p.y + dy, p.w, p.h):
            return
        for k in range(1, CORNER_NUDGE + 1):
            for off in (-k, k):
                nx = p.x + off
                if (not self._blocked(p, nx, p.y + dy, p.w, p.h)
                        and not self._blocked(p, nx, p.y, p.w, p.h, include_bodies=True)):
                    p.x = nx
                    return

    def _move(self, b: Body, dx: float, dy: float):
        """Move along ONE axis, stopping at the first obstacle.

        Returns (distance moved, object hit or None, whether something was hit).
        Things that already overlap ``b`` are ignored so bodies can never get
        stuck inside each other.
        """
        if dx == 0 and dy == 0:
            return 0.0, None, False
        ox, oy = b.x, b.y
        sx, sy = min(ox, ox + dx), min(oy, oy + dy)        # swept area
        sw, sh = b.w + abs(dx), b.h + abs(dy)
        wanted = dx if dx else dy
        best, hit, hit_obj = wanted, False, None
        for rx, ry, rw, rh, obj in self._obstacles(b, sx, sy, sw, sh):
            if not overlaps(sx, sy, sw, sh, rx, ry, rw, rh):
                continue
            if overlaps(ox, oy, b.w, b.h, rx, ry, rw, rh):
                continue
            if dx > 0:
                allowed = rx - (ox + b.w)
            elif dx < 0:
                allowed = (rx + rw) - ox
            elif dy > 0:
                allowed = ry - (oy + b.h)
            else:
                allowed = (ry + rh) - oy
            closer = allowed < best if wanted > 0 else allowed > best
            if closer:
                best, hit, hit_obj = allowed, True, obj
            elif hit and abs(allowed - best) < EPS and hit_obj is None and obj is not None:
                hit_obj = obj           # standing on a body and the ground: ride the body
        best = max(0.0, best) if wanted > 0 else min(0.0, best)
        if dx:
            b.x = ox + best
        else:
            b.y = oy + best
        return best, hit_obj, hit

    def _riders(self, base: Body) -> list[Body]:
        """Everything stacked on top of ``base`` (directly or indirectly)."""
        result, frontier = [], [base]
        while frontier:
            below = frontier.pop()
            for b in self._bodies:
                if b.standing_on is below and b not in result:
                    result.append(b)
                    frontier.append(b)
        return result

    # ------------------------------------------------------------------- tick
    def step(self):
        if self.cleared:
            return
        self.ticks += 1
        active = [p for p in self.players if not p.entered]
        self._bodies = active + self.boxes
        for b in self._bodies:
            b.dx = 0.0
            if b.standing_on is not None and b.standing_on not in self._bodies:
                b.standing_on = None

        self._update_buttons_and_gates()

        # 1) boxes pushed by enough players
        for box in self.boxes:
            self._push_box(box, active)

        # 2) players walk (leaders first so a line of players moves together)
        for p in sorted(active, key=lambda p: -p.x * p.direction):
            d = p.direction
            if d:
                p.facing = d
                moved, _, _ = self._move(p, d * MOVE_SPEED, 0.0)
                p.dx += moved

        # 3) whatever stands on a moving body moves with it (bottom first)
        for b in sorted(self._bodies, key=lambda b: -(b.y + b.h)):
            below = b.standing_on
            if below is not None and below.dx:
                moved, _, _ = self._move(b, below.dx, 0.0)
                b.dx += moved

        # 4) jumping (top first); a jump also lifts the teammates standing on you
        for p in sorted(active, key=lambda p: p.y):
            if p.jump_buffer > 0 and (p.on_ground or p.coyote > 0):
                riders = self._riders(p)
                p.vy = -JUMP_SPEED
                p.jump_buffer = p.coyote = 0
                p.jumping = True
                p.on_ground, p.standing_on = False, None
                for r in riders:
                    r.vy = min(r.vy, p.vy)
                    r.on_ground, r.standing_on = False, None
            elif p.jump_buffer > 0:
                p.jump_buffer -= 1
            if p.jumping and not p.jump_held and p.vy < -JUMP_CUT_SPEED:
                p.vy = -JUMP_CUT_SPEED

        # 5) gravity: rising bodies top-first, falling bodies bottom-first
        for b in self._bodies:
            b.vy = min(b.vy + GRAVITY, MAX_FALL_SPEED)
        rising = sorted((b for b in self._bodies if b.vy < 0), key=lambda b: b.y)
        falling = sorted((b for b in self._bodies if b.vy >= 0), key=lambda b: -(b.y + b.h))
        for b in rising + falling:
            going_down = b.vy > 0
            if not going_down and isinstance(b, Player):
                self._corner_nudge(b, b.vy)
            _, obj, hit = self._move(b, 0.0, b.vy)
            if hit and going_down:
                b.on_ground, b.standing_on = True, obj
                b.vy = 0.0
            else:
                b.on_ground, b.standing_on = False, None
                if hit:
                    b.vy = 0.0          # bumped a ceiling
            if isinstance(b, Player):
                if b.on_ground:
                    b.coyote = COYOTE_TICKS
                elif b.coyote > 0:
                    b.coyote -= 1
                if b.vy >= 0:
                    b.jumping = False

        self._interactions(active)
        if all(p.entered for p in self.players):
            self.cleared = True
            self.events.append(("clear",))

    # ---------------------------------------------------------------- helpers
    def _push_chain(self, box: Box, d: int, active: list[Player]) -> list[Player]:
        """Players pushing ``box`` in direction ``d``, directly or through each other."""
        chain, frontier = [], [box]
        while frontier:
            front = frontier.pop()
            for p in active:
                if p in chain or p.direction != d or not p.on_ground:
                    continue        # you need your feet on something to push
                gap = front.x - (p.x + p.w) if d > 0 else p.x - (front.x + front.w)
                if abs(gap) <= 0.5 and p.y < front.y + front.h - EPS and p.y + p.h > front.y + EPS:
                    chain.append(p)
                    frontier.append(p)
        return chain

    def _push_box(self, box: Box, active: list[Player]):
        from_left = self._push_chain(box, 1, active)
        from_right = self._push_chain(box, -1, active)
        box.pushers = max(len(from_left), len(from_right))
        d = 0
        if from_left and not from_right and len(from_left) >= box.weight:
            d = 1
        elif from_right and not from_left and len(from_right) >= box.weight:
            d = -1
        if d:
            moved, _, _ = self._move(box, d * PUSH_SPEED, 0.0)
            box.dx += moved

    def _update_buttons_and_gates(self):
        for btn in self.buttons:
            btn.count = sum(1 for b in self._bodies if self._presses(b, btn))
        for letter, rects in self.gates.items():
            want_open = any(btn.pressed for btn in self.buttons if btn.letter in self.links[letter])
            was_open = self.gate_open[letter]
            self.gate_open[letter] = want_open
            if was_open and not want_open:
                self._eject_from_gate(rects)

    @staticmethod
    def _presses(b: Body, btn: Button) -> bool:
        """A body presses a button when it rests on the plate with enough of its width."""
        shared = min(b.x + b.w, btn.x + btn.w) - max(b.x, btn.x)
        return shared >= BUTTON_MIN_OVERLAP and overlaps(*b.rect, btn.x, btn.y, btn.w, btn.h)

    def _eject_from_gate(self, rects):
        """A closing gate pushes anyone inside it out to the nearer side."""
        for b in self._bodies:
            for gx, gy, gw, gh in rects:
                if not overlaps(*b.rect, gx, gy, gw, gh):
                    continue
                nx = gx - b.w if b.x + b.w / 2 < gx + gw / 2 else gx + gw
                if self._blocked(b, nx, b.y, b.w, b.h):
                    if isinstance(b, Player):
                        self._respawn(b)
                    else:
                        b.x, b.y = b.home
                        b.vy = 0.0
                else:
                    b.x = nx
                break

    def _respawn(self, p: Player):
        if self.key_holder is p:
            self.key_holder = None
            p.has_key = False
            self.events.append(("key_lost", p.name))
        p.x, p.y = p.spawn
        p.vy = 0.0
        p.on_ground, p.standing_on = False, None
        p.jump_buffer = 0
        p.jumping = False
        p.flash = RESPAWN_FLASH_TICKS
        self.events.append(("respawn", p.name))

    def _interactions(self, active: list[Player]):
        fall_limit = (self.rows + 2) * TILE
        for p in active:
            if p.flash:
                p.flash -= 1
            if p.y > fall_limit or any(overlaps(*p.rect, *s) for s in self.spikes):
                self._respawn(p)
        for box in self.boxes:
            if box.y > fall_limit:
                box.x, box.y = box.home
                box.vy = 0.0

        # pick up the key
        if self.key_home and not self.key_used and self.key_holder is None:
            for p in active:
                if overlaps(*p.rect, *self.key_home, KEY_W, KEY_H):
                    self.key_holder = p
                    p.has_key = True
                    self.events.append(("key", p.name))
                    break

        # unlock / enter the door
        holder = self.key_holder
        if self.door is None:
            return
        if not self.door_open:
            if holder is not None and overlaps(*holder.rect, *self.door):
                self.door_open = True
                self.key_used = True
                holder.has_key = False
                self.key_holder = None
                self.events.append(("door", holder.name))
        else:
            for p in active:
                if overlaps(*p.rect, *self.door):
                    p.entered = True
                    p.standing_on = None
                    p.clear_input()
                    self.events.append(("enter", p.name))

    # -------------------------------------------------------------- snapshots
    def static_info(self) -> dict:
        """Everything that never changes during the level (sent once)."""
        return {
            "cols": self.cols,
            "rows": self.rows,
            "tile": TILE,
            "grid": self.grid,
            "door": list(self.door) if self.door else None,
            "has_key": self.key_home is not None,
            "buttons": [{"letter": b.letter, "rect": [b.x, b.y, b.w, b.h], "need": b.need}
                        for b in self.buttons],
            "gates": [{"letter": g, "rects": [list(r) for r in rects]}
                      for g, rects in sorted(self.gates.items())],
            "boxes": [{"size": b.w, "weight": b.weight} for b in self.boxes],
            "players": [{"name": p.name, "color": p.color} for p in self.players],
        }

    def snapshot(self) -> dict:
        """The dynamic part of the world (sent ~30 times per second)."""
        key = None
        if self.key_home:
            key = {"x": self.key_home[0], "y": self.key_home[1],
                   "holder": self.key_holder.name if self.key_holder else None,
                   "used": int(self.key_used)}
        return {
            "players": [{"n": p.name, "x": round(p.x, 1), "y": round(p.y, 1), "f": p.facing,
                         "k": int(p.has_key), "e": int(p.entered), "fl": int(p.flash > 0)}
                        for p in self.players],
            "boxes": [[round(b.x, 1), round(b.y, 1), b.pushers] for b in self.boxes],
            "gates": {g: int(v) for g, v in self.gate_open.items()},
            "buttons": [b.count for b in self.buttons],
            "key": key,
            "door": int(self.door_open),
            "time": round(self.time, 2),
        }
