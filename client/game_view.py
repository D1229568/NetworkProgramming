"""The game screen (pygame-ce): draws the server's snapshots and reads the keyboard.

Rendering: the server sends ~30 snapshots per second and we draw ~60 frames
per second, interpolating between the last two snapshots so teammates move
smoothly.  Small effects (dust, sparkles, confetti) are purely cosmetic and
are derived on the client from changes between snapshots.

Input: key presses become (left, right, jump).  Before anything is sent we
check that acting is legal right now (phase "playing", still in the level,
online) and only send when the state changes.  The server checks again.
"""
from __future__ import annotations

import math
import random
import time

import pygame

from common.protocol import STATE_RATE

from . import ui

ORIGIN = (16, 72)                  # where the 960x512 world sits on the 1280x720 canvas
WORLD_W, WORLD_H = 960, 512

LETTER = {"a": ((249, 115, 22), (154, 52, 18)), "b": ((168, 85, 247), (88, 28, 135)),
          "c": ((20, 184, 166), (17, 94, 89))}
KEY_GOLD, KEY_DARK = (250, 204, 21), (161, 98, 7)
GAME_KEYS = {
    pygame.K_LEFT: "left", pygame.K_a: "left",
    pygame.K_RIGHT: "right", pygame.K_d: "right",
    pygame.K_UP: "jump", pygame.K_w: "jump", pygame.K_SPACE: "jump",
}


def WR(x, y, w, h) -> pygame.Rect:
    """World rectangle -> window pixels."""
    return ui.R(ORIGIN[0] + x, ORIGIN[1] + y, w, h)


def WP(x, y):
    return ui.pt(ORIGIN[0] + x, ORIGIN[1] + y)


class GameView:
    def __init__(self, app):
        self.app = app
        self.reset()

    def reset(self):
        self.level: dict | None = None
        self.serial = None
        self.prev: dict | None = None
        self.cur: dict | None = None
        self.cur_time = 0.0
        self.phase = None
        self.phase_since = 0.0
        self.countdown_since = (None, 0.0)
        self.pressed: set[str] = set()
        self.sent = (False, False, False)
        self.connection_lost = False
        self.particles: list[list] = []
        self.track: dict[str, dict] = {}
        self._layers = None
        self._layers_key = None
        self._clouds = [(random.uniform(0, WORLD_W), random.uniform(50, 200), random.uniform(0.6, 1.2),
                         random.uniform(4, 10)) for _ in range(4)]

    # =============================================================== messages
    def load_level(self, msg: dict):
        lost = self.connection_lost
        self.reset()
        self.connection_lost = lost
        self.level = msg
        self.serial = msg["serial"]

    def on_state(self, msg: dict):
        if self.level is None or msg.get("serial") != self.serial:
            return                  # a late snapshot of another level
        self._effects_from(msg)
        self.prev, self.cur = self.cur, msg
        self.cur_time = time.monotonic()
        if msg["phase"] != self.phase:
            self.phase = msg["phase"]
            self.phase_since = self.cur_time
            self.sent = (False, False, False)     # the server cleared our inputs
            if self.phase == "playing":
                self._sync_input()                # keys already held now count
            if self.phase == "level_clear":
                self._confetti()
        if msg.get("countdown") != self.countdown_since[0]:
            self.countdown_since = (msg.get("countdown"), self.cur_time)

    # ================================================================== input
    def me(self):
        if not self.cur:
            return None
        return next((p for p in self.cur["players"] if p["n"] == self.app.username), None)

    def can_act(self) -> bool:
        me = self.me()
        return (self.cur is not None and self.cur["phase"] == "playing" and me is not None
                and not me["e"] and self.app.is_online() and not self.connection_lost)

    def _sync_input(self):
        if not self.can_act():
            return
        state = ("left" in self.pressed, "right" in self.pressed, "jump" in self.pressed)
        if state != self.sent:
            self.sent = state
            self.app.send({"type": "input", "left": state[0], "right": state[1], "jump": state[2]})

    def release_all(self):
        self.pressed.clear()
        self._sync_input()

    def handle(self, ev) -> bool:
        """Keyboard handling while the game has focus."""
        if self.level is None:
            return False
        if ev.type == pygame.KEYDOWN:
            if ev.key == pygame.K_p:
                if self.cur and self.cur["phase"] in ("countdown", "playing", "paused"):
                    self.app.send({"type": "pause"})
                return True
            if ev.key == pygame.K_r:
                if self.cur and self.cur["phase"] == "playing":
                    self.app.send({"type": "restart"})
                return True
            if ev.key == pygame.K_F9:
                self.app.debug_drop_connection()
                return True
            if ev.key == pygame.K_F10:
                self.app.debug_freeze_network()
                return True
            action = GAME_KEYS.get(ev.key)
            if action:
                self.pressed.add(action)
                self._sync_input()
                return True
        elif ev.type == pygame.KEYUP:
            action = GAME_KEYS.get(ev.key)
            if action:
                keys = pygame.key.get_pressed()
                still = any(keys[k] for k, a in GAME_KEYS.items() if a == action and k != ev.key)
                if not still:
                    self.pressed.discard(action)
                    self._sync_input()
                return True
        return False

    # ================================================================ effects
    def _burst(self, x, y, color, n=10, speed=2.2, up=1.5, life=0.7, size=4.0, gravity=0.12):
        for _ in range(n):
            a = random.uniform(0, math.tau)
            v = random.uniform(0.3, 1.0) * speed
            self.particles.append([x, y, math.cos(a) * v, math.sin(a) * v - up, life, life, color, size, gravity])

    def _confetti(self):
        for _ in range(140):
            self.particles.append([random.uniform(0, WORLD_W), random.uniform(-80, 0),
                                   random.uniform(-1.2, 1.2), random.uniform(0.5, 2.5),
                                   3.5, 3.5, random.choice(ui.PLAYER_COLORS + [ui.GOLD]),
                                   random.uniform(4, 7), 0.03])

    def _effects_from(self, new):
        """Cosmetic effects deduced from what changed since the last snapshot."""
        old = self.cur
        if old is None:
            for p in new["players"]:
                self.track[p["n"]] = {"vy": 0.0}
            return
        old_players = {p["n"]: p for p in old["players"]}
        for p in new["players"]:
            o = old_players.get(p["n"])
            t = self.track.setdefault(p["n"], {"vy": 0.0})
            if o is None:
                continue
            vy = p["y"] - o["y"]
            feet = (p["x"] + 14, p["y"] + 28)
            if t["vy"] > 4 and abs(vy) < 0.5 and not p["e"]:
                self._burst(*feet, (226, 232, 240), n=7, speed=1.6, up=0.6, life=0.45, size=3.5)
            if vy < -5 and t["vy"] >= -0.5:
                self._burst(*feet, (241, 245, 249), n=4, speed=1.0, up=0.2, life=0.35, size=3)
            if p["e"] and not o["e"]:
                self._burst(p["x"] + 14, p["y"] + 14, ui.GOLD, n=18, speed=3, life=0.9)
            if p["fl"] and not o["fl"]:
                self._burst(p["x"] + 14, p["y"] + 14, (148, 163, 184), n=14, speed=2.5, life=0.6, size=5)
            t["vy"] = vy
        ok, nk = old.get("key"), new.get("key")
        if ok and nk and nk["holder"] and not ok["holder"]:
            self._burst(nk["x"] + 10, nk["y"] + 8, ui.GOLD, n=16, speed=2.6, life=0.8)
        if new.get("door") and not old.get("door") and self.level.get("door"):
            dx, dy, dw, dh = self.level["door"]
            self._burst(dx + dw / 2, dy + dh / 2, (253, 224, 71), n=24, speed=3.2, life=1.0)

    # ================================================================ drawing
    def _build_layers(self):
        """Pre-render the parts of the level that never move (sky and tiles)."""
        T = self.level["tile"]
        sc = ui.View.scale
        size = WR(0, 0, WORLD_W, WORLD_H).size

        def lr(x, y, w, h):
            x0, y0 = round(x * sc), round(y * sc)
            return pygame.Rect(x0, y0, max(1, round((x + w) * sc) - x0), max(1, round((y + h) * sc) - y0))

        sky = ui.gradient(size, (125, 196, 255), (219, 239, 255))
        hills = pygame.Surface(size, pygame.SRCALPHA)
        for base, amp, col, k in ((360, 40, (180, 220, 250, 255), 0.006), (400, 30, (160, 210, 240, 255), 0.011)):
            pts = [(0, size[1])]
            for i in range(0, WORLD_W + 20, 20):
                pts.append((i * sc, (base - amp * math.sin(i * k + base)) * sc))
            pts.append((size[0], size[1]))
            pygame.draw.polygon(hills, col, pts)
        sky.blit(hills, (0, 0))

        tiles = pygame.Surface(size, pygame.SRCALPHA)
        grid = self.level["grid"]
        rng = random.Random(self.serial)
        line_w = max(1, round(sc))
        for r, row in enumerate(grid):
            for c, ch in enumerate(row):
                x, y = c * T, r * T
                if ch == "#":
                    pygame.draw.rect(tiles, (71, 85, 105), lr(x, y, T, T))
                    pygame.draw.rect(tiles, (88, 104, 128), lr(x, y, T, 3))
                    pygame.draw.rect(tiles, (51, 65, 85), lr(x, y + T - 3, T, 3))
                    for _ in range(2):
                        px, py = rng.uniform(5, T - 9), rng.uniform(7, T - 9)
                        pygame.draw.rect(tiles, (61, 74, 95), lr(x + px, y + py, 4, 3))
                    if r > 0 and grid[r - 1][c] != "#":
                        pygame.draw.rect(tiles, (74, 160, 70), lr(x, y, T, 8))
                        pygame.draw.rect(tiles, (120, 205, 95), lr(x, y, T, 5))
                        for k in range(3):
                            bx = x + 4 + k * 10 + rng.uniform(-2, 2)
                            pygame.draw.rect(tiles, (120, 205, 95), lr(bx, y - 3, 2, 4))
                elif ch == "^":
                    for k in range(2):
                        bx = x + k * 16
                        pts = [((bx + 1) * sc, (y + T) * sc), ((bx + 8) * sc, (y + T - 15) * sc),
                               ((bx + 15) * sc, (y + T) * sc)]
                        pygame.draw.polygon(tiles, (203, 213, 225), pts)
                        pygame.draw.polygon(tiles, (100, 116, 139), pts, line_w)
                        pygame.draw.line(tiles, (255, 255, 255), ((bx + 5) * sc, (y + T - 4) * sc),
                                         ((bx + 8) * sc, (y + T - 12) * sc), line_w)
        if self.level.get("door"):
            dx, dy, dw, dh = self.level["door"]
            pygame.draw.rect(tiles, (92, 51, 23), lr(dx - 5, dy - 20, dw + 10, dh + 20),
                             border_top_left_radius=round(14 * sc), border_top_right_radius=round(14 * sc))
            sign = lr(dx - 4, dy - 42, dw + 8, 16)
            pygame.draw.rect(tiles, (22, 163, 74), sign, border_radius=round(4 * sc))
            img = ui.font(11, True).render("EXIT", True, (255, 255, 255))
            tiles.blit(img, img.get_rect(center=sign.center))
        self._layers = (sky.convert(), tiles.convert_alpha())
        self._layers_key = (self.serial, ui.View.version)

    @staticmethod
    def _lerp(a, b, alpha):
        if a is None or abs(b[0] - a[0]) > 48 or abs(b[1] - a[1]) > 48:
            return b                # teleport (respawn): don't slide across the map
        return a[0] + (b[0] - a[0]) * alpha, a[1] + (b[1] - a[1]) * alpha

    def draw(self, surf, dt):
        area = WR(0, 0, WORLD_W, WORLD_H)
        if self.level is None or self.cur is None:
            ui.rect(surf, (15, 23, 42), (*ORIGIN, WORLD_W, WORLD_H), 16)
            ui.text(surf, "Loading level...", ORIGIN[0] + WORLD_W / 2, ORIGIN[1] + WORLD_H / 2, 26,
                    ui.WHITE, bold=True, anchor="center")
            return
        if self._layers_key != (self.serial, ui.View.version):
            self._build_layers()
        now = time.monotonic()
        cur = self.cur
        prev = self.prev or cur
        alpha = min(1.0, (now - self.cur_time) * STATE_RATE)

        ui.shadow(surf, (*ORIGIN, WORLD_W, WORLD_H), 14, 8)
        old_clip = surf.get_clip()
        surf.set_clip(area)
        surf.blit(self._layers[0], area)
        self._draw_clouds(surf, now)
        surf.blit(self._layers[1], area)
        self._draw_door(surf, cur, now)
        self._draw_buttons(surf, cur)
        self._draw_gates(surf, cur)
        self._draw_boxes(surf, cur, prev, alpha)
        positions = self._draw_players(surf, cur, prev, alpha, now)
        self._draw_key(surf, cur, now)
        self._draw_particles(surf, dt)
        self._draw_hud(surf, cur)
        self._draw_overlay(surf, cur, now)
        surf.set_clip(old_clip)
        del positions

    def _draw_clouds(self, surf, now):
        for x0, y, k, speed in self._clouds:
            x = (x0 + now * speed) % (WORLD_W + 200) - 100
            for dx, dy, r in ((0, 0, 20), (22, -10, 26), (48, 0, 20), (24, 6, 18)):
                ui.rect_a(surf, (255, 255, 255, 200), (ORIGIN[0] + x + dx * k - r * k, ORIGIN[1] + y + dy * k - r * k,
                                                       2 * r * k, 2 * r * k), r * k)

    def _draw_door(self, surf, cur, now):
        if not self.level.get("door"):
            return
        dx, dy, dw, dh = self.level["door"]
        top_r = round(ui.s(11))
        if cur["door"]:
            pygame.draw.rect(surf, (30, 20, 45), WR(dx + 1, dy - 15, dw - 2, dh + 15),
                             border_top_left_radius=top_r, border_top_right_radius=top_r)
            glow = 90 + 50 * math.sin(now * 4)
            ui.rect_a(surf, (253, 224, 71, int(glow)), (ORIGIN[0] + dx + 4, ORIGIN[1] + dy - 10, dw - 8, dh + 8), 9)
        else:
            pygame.draw.rect(surf, (180, 110, 60), WR(dx + 1, dy - 15, dw - 2, dh + 15),
                             border_top_left_radius=top_r, border_top_right_radius=top_r)
            for k in range(3):
                pygame.draw.line(surf, (140, 82, 40), WP(dx + 6 + k * 10, dy - 10), WP(dx + 6 + k * 10, dy + dh - 2),
                                 max(1, round(ui.s(1.5))))
            pygame.draw.circle(surf, KEY_DARK, WP(dx + 16, dy + 4), ui.s(5), max(1, round(ui.s(2))))
            pygame.draw.rect(surf, KEY_DARK, WR(dx + 9, dy + 4, 14, 11), border_radius=round(ui.s(2)))
            pygame.draw.rect(surf, KEY_GOLD, WR(dx + 10, dy + 5, 12, 9), border_radius=round(ui.s(2)))

    def _draw_buttons(self, surf, cur):
        for b, count in zip(self.level["buttons"], cur["buttons"]):
            x, y, w, h = b["rect"]
            col, dark = LETTER.get(b["letter"], LETTER["a"])
            pressed = count >= b["need"]
            pygame.draw.rect(surf, dark, WR(x, y + h - 3, w, 3))
            sink = 4 if pressed else 0
            pygame.draw.rect(surf, ui.mix(col, (255, 255, 255), 0.35) if pressed else col,
                             WR(x + 3, y + sink, w - 6, h - 2 - sink), border_radius=round(ui.s(3)))
            label = f"{count}/{b['need']}"
            bw = ui.text_width(label, 12, True) + 12
            bx, by = ORIGIN[0] + x + w / 2 - bw / 2, ORIGIN[1] + y - 22
            ui.rect(surf, ui.GREEN if pressed else dark, (bx, by, bw, 17), 8)
            ui.text(surf, label, bx + bw / 2, by + 8.5, 12, ui.WHITE, bold=True, anchor="center")

    def _draw_gates(self, surf, cur):
        for g in self.level["gates"]:
            col, dark = LETTER.get(g["letter"].lower(), LETTER["a"])
            opened = cur["gates"].get(g["letter"])
            for gx, gy, gw, gh in g["rects"]:
                if opened:
                    ui.rect_a(surf, (*col, 55), (ORIGIN[0] + gx + 4, ORIGIN[1] + gy, gw - 8, gh), 4)
                    continue
                r = WR(gx + 4, gy, gw - 8, gh)
                pygame.draw.rect(surf, col, r)
                old = surf.get_clip()
                surf.set_clip(r.clip(old))
                for k in range(-1, 3):
                    pygame.draw.line(surf, dark, WP(gx + 4, gy + k * 16 + 4), WP(gx + gw - 4, gy + k * 16 + 20),
                                     max(1, round(ui.s(3))))
                surf.set_clip(old)
                pygame.draw.rect(surf, dark, r, max(1, round(ui.s(2))))

    def _draw_boxes(self, surf, cur, prev, alpha):
        prev_boxes = prev.get("boxes", [])
        for i, (bx, by, pushers) in enumerate(cur["boxes"]):
            info = self.level["boxes"][i]
            size, weight = info["size"], info["weight"]
            pb = prev_boxes[i] if i < len(prev_boxes) else None
            x, y = self._lerp((pb[0], pb[1]) if pb else None, (bx, by), alpha)
            pygame.draw.rect(surf, (130, 82, 38), WR(x, y, size, size), border_radius=round(ui.s(4)))
            inner = WR(x + 3, y + 3, size - 6, size - 6)
            pygame.draw.rect(surf, (214, 158, 92), inner, border_radius=round(ui.s(3)))
            pygame.draw.rect(surf, (184, 125, 64), inner, max(1, round(ui.s(3))), border_radius=round(ui.s(3)))
            pygame.draw.line(surf, (150, 96, 46), WP(x + 6, y + size - 6), WP(x + size - 6, y + 6),
                             max(1, round(ui.s(4))))
            if weight > 1:
                for cx, cy in ((x + 2, y + 2), (x + size - 9, y + 2), (x + 2, y + size - 9), (x + size - 9, y + size - 9)):
                    pygame.draw.rect(surf, (100, 116, 139), WR(cx, cy, 7, 7), border_radius=round(ui.s(2)))
                label = f"PUSH ×{weight}"
                bw = ui.text_width(label, 12, True) + 14
                lx, ly = ORIGIN[0] + x + size / 2 - bw / 2, ORIGIN[1] + y - 34
                ui.rect(surf, (15, 23, 42), (lx, ly, bw, 18), 9)
                ui.text(surf, label, lx + bw / 2, ly + 9, 12, ui.WHITE, bold=True, anchor="center")
                for k in range(weight):
                    ui.circle(surf, ui.GREEN if k < pushers else (203, 213, 225),
                              (ORIGIN[0] + x + size / 2 + (k - (weight - 1) / 2) * 12, ORIGIN[1] + y - 9), 4)

    def _draw_players(self, surf, cur, prev, alpha, now):
        online = {m["name"]: m["connected"] for m in (self.app.room or {}).get("members", [])}
        colors = {p["name"]: p["color"] for p in self.level["players"]}
        prev_players = {p["n"]: p for p in prev["players"]}
        positions = {}
        for p in cur["players"]:
            pp = prev_players.get(p["n"])
            x, y = self._lerp((pp["x"], pp["y"]) if pp else None, (p["x"], p["y"]), alpha)
            positions[p["n"]] = (x, y)
            if p["e"] or (p["fl"] and int(now * 12) % 2 == 0):
                continue
            vy = self.track.get(p["n"], {"vy": 0.0})["vy"]
            squash = max(-0.16, min(0.12, -vy * 0.025)) if abs(vy) > 0.6 else 0.0
            gray = not online.get(p["n"], True)
            blink = (now + (sum(map(ord, p["n"])) % 7)) % 4.0 < 0.12
            ui.block_character(surf, ORIGIN[0] + x, ORIGIN[1] + y, 28, colors.get(p["n"], 0), p["f"],
                               look_up=1.0 if vy < -1 else 0.0, blink=blink, squash=squash, gray=gray)
            me = p["n"] == self.app.username
            label = p["n"] + (" (offline)" if gray else "")
            tw = ui.text_width(label, 12, True) + 12
            lx, ly = ORIGIN[0] + x + 14 - tw / 2, ORIGIN[1] + y - 22
            if me:
                ui.rect(surf, ui.ACCENT, (lx, ly, tw, 17), 8)
                ui.polygon(surf, ui.ACCENT, [(lx + tw / 2 - 4, ly + 16), (lx + tw / 2 + 4, ly + 16),
                                             (lx + tw / 2, ly + 20)])
            else:
                ui.rect_a(surf, (15, 23, 42, 150), (lx, ly, tw, 17), 8)
            ui.text(surf, label, lx + tw / 2, ly + 8.5, 12, ui.WHITE, bold=True, anchor="center")
            if p["k"]:
                self._key_icon(surf, x + 4, y - 44 + 2 * math.sin(now * 5), 0.9)
        return positions

    def _key_icon(self, surf, x, y, k=1.0):
        ox, oy = ORIGIN
        c = ui.pt(ox + x + 6 * k, oy + y + 8 * k)
        pygame.draw.circle(surf, KEY_DARK, c, ui.s(6.5 * k))
        pygame.draw.circle(surf, KEY_GOLD, c, ui.s(5 * k))
        pygame.draw.circle(surf, KEY_DARK, c, ui.s(2 * k))
        pygame.draw.rect(surf, KEY_GOLD, ui.R(ox + x + 11 * k, oy + y + 6.5 * k, 10 * k, 3.5 * k))
        pygame.draw.rect(surf, KEY_GOLD, ui.R(ox + x + 17 * k, oy + y + 9 * k, 3 * k, 5 * k))
        pygame.draw.rect(surf, KEY_GOLD, ui.R(ox + x + 13 * k, oy + y + 9 * k, 2.5 * k, 3.5 * k))

    def _draw_key(self, surf, cur, now):
        k = cur.get("key")
        if not k or k["used"] or k["holder"]:
            return
        y = k["y"] + 2 * math.sin(now * 3)
        ui.rect_a(surf, (253, 224, 71, int(70 + 40 * math.sin(now * 5))),
                  (ORIGIN[0] + k["x"] - 6, ORIGIN[1] + y - 4, 32, 24), 12)
        self._key_icon(surf, k["x"], y)
        if random.random() < 0.08:
            self._burst(k["x"] + random.uniform(0, 20), y + random.uniform(0, 14), (255, 255, 255),
                        n=1, speed=0.4, up=0.4, life=0.6, size=2.5, gravity=0)

    def _draw_particles(self, surf, dt):
        alive = []
        steps = dt * 60
        for p in self.particles:
            p[0] += p[2] * steps
            p[1] += p[3] * steps
            p[3] += p[8] * steps
            p[4] -= dt
            if p[4] <= 0 or p[1] > WORLD_H + 20:
                continue
            alive.append(p)
            size = max(1.0, p[7] * (0.4 + 0.6 * p[4] / p[5]))
            pygame.draw.rect(surf, p[6], WR(p[0] - size / 2, p[1] - size / 2, size, size))
        self.particles = alive[-600:]

    @staticmethod
    def _pill(surf, label, x, y, anchor="left", color=(15, 23, 42), alpha=190, size=14):
        w = ui.text_width(label, size, True) + 24
        if anchor == "right":
            x -= w
        elif anchor == "center":
            x -= w / 2
        ui.rect_a(surf, (*color, alpha), (x, y, w, 30), 15)
        ui.text(surf, label, x + w / 2, y + 15, size, ui.WHITE, bold=True, anchor="center")
        return w

    def _draw_hud(self, surf, cur):
        lv = self.level
        ox, oy = ORIGIN
        self._pill(surf, f"LEVEL {lv['index'] + 1}  ·  {lv['name']}", ox + 12, oy + 8)
        self._pill(surf, f"TIME  {cur['time']:.1f} s", ox + WORLD_W - 12, oy + 8, anchor="right")
        if cur.get("votes"):
            self._pill(surf, f"Restart votes {cur['votes']}/{cur['votes_needed']} - press R to agree",
                       ox + WORLD_W / 2, oy + 8, anchor="center", color=(194, 65, 12), alpha=225)
        me = self.me()
        if me and me["e"] and cur["phase"] == "playing":
            msg = "You made it!  Waiting for your teammates..."
            w = ui.text_width(msg, 16, True) + 40
            ui.rect_a(surf, (22, 163, 74, 220), (ox + WORLD_W / 2 - w / 2, oy + 52, w, 38), 19)
            ui.text(surf, msg, ox + WORLD_W / 2, oy + 71, 16, ui.WHITE, bold=True, anchor="center")

    @staticmethod
    def _banner(surf, big, sub="", color=(15, 23, 42), big_size=60, pop=1.0):
        ox, oy = ORIGIN
        lines = sub.split("\n") if sub else []
        h = 120 + 26 * len(lines)
        y = oy + WORLD_H / 2 - h / 2
        ui.rect_a(surf, (*color, 220), (ox, y, WORLD_W, h))
        size = big_size * pop
        ui.text(surf, big, ox + WORLD_W / 2 + 3, y + 63, size, (0, 0, 0), bold=True, anchor="center", alpha=90)
        ui.text(surf, big, ox + WORLD_W / 2, y + 60, size, ui.WHITE, bold=True, anchor="center")
        for i, part in enumerate(lines):
            ui.text(surf, part, ox + WORLD_W / 2, y + 112 + i * 26, 17, (241, 245, 249), anchor="center")

    def _draw_overlay(self, surf, cur, now):
        phase = cur["phase"]
        if self.connection_lost:
            self._banner(surf, "CONNECTION LOST", "Trying to reconnect to the server...", color=(127, 29, 29))
        elif phase == "countdown":
            n, since = self.countdown_since
            pop = 1.0 + max(0.0, 0.35 - (now - since)) * 1.3
            self._banner(surf, str(n or ""), "Get ready!", big_size=72, pop=pop)
        elif phase == "playing" and now - self.phase_since < 0.8:
            self._banner(surf, "GO!", big_size=72, pop=1.0 + max(0.0, 0.3 - (now - self.phase_since)),
                         color=(21, 128, 61))
        elif phase == "paused":
            pause = cur.get("pause") or {}
            if pause.get("waiting"):
                sub = "\n".join(f"Waiting for {w['name']} to reconnect...  {w['remaining']} s"
                                for w in pause["waiting"])
            else:
                sub = f"Paused by {pause.get('by')}   -   press P to resume"
            self._banner(surf, "PAUSED", sub, color=(154, 52, 18))
        elif phase == "level_clear":
            res = self.app.last_result
            lines = [f"Time {cur['time']:.1f} s"]
            if res and res.get("index") == self.level["index"]:
                lines[0] += "   -   NEW BEST!" if res["record"] else f"   (your best {res['best']:.1f} s)"
                if res["last"]:
                    lines.append("You finished the whole adventure!")
                elif res["newly_unlocked"]:
                    lines.append(f"Level {self.level['index'] + 2} unlocked!  Back to the level select...")
                else:
                    lines.append("Back to the level select...")
            self._banner(surf, "LEVEL CLEAR!", "\n".join(lines), color=(21, 128, 61))
