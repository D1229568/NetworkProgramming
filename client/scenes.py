"""The three screens of the client: Login/Register, Lobby and Room."""
from __future__ import annotations

import math
import time

import pygame

from common import protocol as P

from . import ui
from .game_view import ORIGIN, WORLD_H, WORLD_W, GameView

PHASE_TEXT = {"waiting": "Choosing level", "countdown": "Starting", "playing": "Playing",
              "paused": "Paused", "level_clear": "Level clear"}
PHASE_COLOR = {"waiting": ui.ACCENT, "countdown": ui.ORANGE, "playing": ui.GREEN,
               "paused": (194, 65, 12), "level_clear": ui.GREEN}


class Scene:
    def __init__(self, app):
        self.app = app
        self.widgets: list = []
        self.inputs: list = []          # Tab order

    def on_enter(self):
        pass

    def add(self, widget):
        self.widgets.append(widget)
        if isinstance(widget, ui.TextInput):
            self.inputs.append(widget)
        return widget

    def handle(self, ev, pos) -> bool:
        if ev.type == pygame.KEYDOWN and ev.key == pygame.K_TAB:
            shown = [w for w in self.inputs if w.visible]
            if shown:
                i = shown.index(ui.Focus.widget) if ui.Focus.widget in shown else -1
                step = -1 if ev.mod & pygame.KMOD_SHIFT else 1
                ui.set_focus(shown[(i + step) % len(shown)])
                return True
        for w in reversed(self.widgets):
            if w.visible and w.handle(ev, pos):
                return True
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            ui.set_focus(None)
        return False

    def update(self, dt, pos):
        for w in self.widgets:
            if w.visible:
                w.update(dt, pos)

    def draw(self, surf, dt):
        for w in self.widgets:
            if w.visible:
                w.draw(surf)


# =============================================================== login screen
class LoginScene(Scene):
    def __init__(self, app, host, port):
        super().__init__(app)
        x, w = 724, 452
        self.mode = self.add(ui.Segmented(x + 32, 168, w - 64, 44, [("login", "Log in"), ("register", "Register")],
                                          "login", self._on_mode, size=16))
        self.host = self.add(ui.TextInput(x + 32, 252, 268, 44, "Server address", value=host, max_len=60,
                                          on_submit=self.submit))
        self.port = self.add(ui.TextInput(x + 312, 252, w - 344, 44, "Port", value=str(port), max_len=5,
                                          on_submit=self.submit))
        self.user = self.add(ui.TextInput(x + 32, 330, w - 64, 44, "Username", max_len=16, on_submit=self.submit))
        self.password = self.add(ui.TextInput(x + 32, 408, w - 64, 44, "Password", secret=True, max_len=64,
                                              on_submit=self.submit))
        self.confirm = self.add(ui.TextInput(x + 32, 486, w - 64, 44, "Repeat password", secret=True,
                                             max_len=64, on_submit=self.submit))
        self.button = self.add(ui.Button(x + 32, 500, w - 64, 56, "Log in", self.submit, size=19))
        self.message = ("", ui.MUTED)
        self._on_mode("login")

    def on_enter(self):
        self.message = ("", ui.MUTED)
        ui.set_focus(self.user if not self.user.text else self.password)

    def _on_mode(self, mode):
        register = mode == "register"
        self.confirm.visible = register
        self.button.y = 564 if register else 486
        self.button.label = "Create account" if register else "Log in"
        self.button.style = "success" if register else "primary"
        self.message = ("", ui.MUTED)

    def show_error(self, msg):
        self.message = (msg, ui.RED)

    def submit(self):
        host = self.host.text.strip()
        try:
            port = int(self.port.text)
            if not 0 < port < 65536:
                raise ValueError
        except ValueError:
            return self.show_error("Port must be a number between 1 and 65535")
        if not host:
            return self.show_error("Please enter the server address")
        user, pw = self.user.text.strip(), self.password.text
        error = P.check_username(user) or P.check_password(pw)       # client-side checks
        if not error and self.mode.value == "register" and pw != self.confirm.text:
            error = "The two passwords do not match"
        if error:
            return self.show_error(error)
        if self.mode.value == "register":
            self.message = ("Creating account...", ui.MUTED)
            self.app.register(host, port, user, pw)
        else:
            self.message = ("Logging in...", ui.MUTED)
            self.app.login(host, port, user, pw)

    def on_register_result(self, msg):
        if msg["ok"]:
            self.mode.value = "login"
            self._on_mode("login")
            self.password.set("")
            self.confirm.set("")
            self.message = (msg["msg"], ui.GREEN_DARK)
            ui.set_focus(self.password)
        else:
            self.show_error(msg["msg"])

    def draw(self, surf, dt):
        now = time.monotonic()
        # left side: title and a little animated team
        ui.text(surf, "Co-op Block", 96, 150, 64, ui.WHITE, bold=True)
        ui.text(surf, "Adventure", 96, 222, 64, ui.GOLD, bold=True)
        ui.text(surf, "協力方塊大冒險", 100, 310, 26, (199, 210, 254), bold=True)
        ui.text(surf, "A 2-4 player online co-op puzzle platformer.", 100, 356, 18, (224, 231, 255))
        ui.text(surf, "Stack up, push together, hold the gates - and get EVERYONE out.", 100, 384, 18,
                (224, 231, 255))
        base_y = 600
        ui.rect(surf, (74, 160, 70), (96, base_y, 470, 16), 6)
        ui.rect(surf, (55, 65, 81), (96, base_y + 12, 470, 26), 6)
        for i, (cx, color) in enumerate(((150, 0), (240, 1), (240, 2), (360, 3))):
            stack = 1 if i == 2 else 0
            hop = abs(math.sin(now * 2.2 + i)) * (16 if i in (0, 3) else 0)
            size = 56
            y = base_y - size * (stack + 1) - hop
            ui.block_character(surf, cx, y, size, color, facing=1 if i < 2 else -1,
                               look_up=1.0 if stack else 0.0, blink=(now + i) % 3.5 < 0.13,
                               squash=-0.05 if hop > 8 else 0.0)
        kx, ky = 470, 520 + 6 * math.sin(now * 3)
        ui.circle(surf, (161, 98, 7), (kx, ky), 14)
        ui.circle(surf, ui.GOLD, (kx, ky), 11)
        ui.circle(surf, (161, 98, 7), (kx, ky), 4)
        ui.rect(surf, ui.GOLD, (kx + 10, ky - 3, 26, 7))
        ui.rect(surf, ui.GOLD, (kx + 28, ky + 2, 6, 11))

        # right side: the form card
        ui.card(surf, (724, 96, 452, 560 if self.mode.value == "register" else 482), radius=24)
        ui.text(surf, "Welcome!", 756, 116, 26, ui.INK, bold=True)
        ui.text(surf, "Server", 756, 228, 13, ui.MUTED, bold=True)
        ui.text(surf, "Account", 756, 306, 13, ui.MUTED, bold=True)
        msg, color = self.message
        if msg:
            for i, part in enumerate(ui.wrap(msg, 15, 388)[:2]):
                ui.text(surf, part, 950, self.button.y + 72 + i * 20, 15, color, anchor="midtop")
        super().draw(surf, dt)


# =============================================================== lobby screen
class LobbyScene(Scene):
    def __init__(self, app):
        super().__init__(app)
        self.logout_btn = self.add(ui.Button(1124, 22, 132, 46, "Log out", app.logout, "light", 15))
        self.rooms = self.add(ui.ListView(44, 168, 816, 312, 64, self._draw_room, key=lambda r: r["id"],
                                          on_select=lambda r: None, on_activate=self._join,
                                          empty_text="No rooms yet - create one or press Quick match!"))
        self.refresh_btn = self.add(ui.Button(740, 112, 120, 40, "Refresh",
                                              lambda: app.send({"type": "list_rooms"}), "light", 14))
        self.join_btn = self.add(ui.Button(44, 500, 200, 52, "Join room", self._join_selected, size=16))
        self.quick_size = self.add(ui.Segmented(500, 504, 210, 40, [(None, "Any"), (2, "2"), (3, "3"), (4, "4")],
                                                None, size=14))
        self.quick_btn = self.add(ui.Button(720, 500, 140, 52, "Quick match", self._quick, "success", 16))
        self.room_name = self.add(ui.TextInput(44, 612, 360, 46, "Room name (optional)", max_len=P.ROOM_NAME_MAX_LEN,
                                               on_submit=self._create))
        self.room_size = self.add(ui.Segmented(420, 615, 190, 40, [(2, "2P"), (3, "3P"), (4, "4P")], 2, size=14))
        self.create_btn = self.add(ui.Button(626, 608, 234, 52, "Create room", self._create, "warning", 16))
        self.online = self.add(ui.ListView(904, 150, 336, 176, 34, self._draw_online, key=lambda u: u["name"]))
        self.chat = self.add(ui.ChatBox(888, 364, 368, 316, "Lobby chat",
                                        lambda text: app.send({"type": "chat", "text": text})))
        self.lobby = {"rooms": [], "online": [], "games_in_progress": 0}

    def on_enter(self):
        self.app.send({"type": "list_rooms"})

    def update_lobby(self, msg):
        self.lobby = msg
        self.rooms.set_items(msg["rooms"])
        self.online.set_items(msg["online"])

    def _joinable(self, room):
        return room is not None and room["phase"] == "waiting" and room["missing"] > 0

    def _join(self, room):
        if not self._joinable(room):                                      # client-side check
            self.app.toast("That room is full or already playing.", "warn")
            return
        self.app.send({"type": "join_room", "room_id": room["id"]})

    def _join_selected(self):
        room = self.rooms.selected_item()
        if room is None:
            self.app.toast("Pick a room from the list first.", "warn")
        else:
            self._join(room)

    def _quick(self):
        self.app.send({"type": "quick_match", "size": self.quick_size.value})

    def _create(self):
        name = P.clean_text(self.room_name.text, P.ROOM_NAME_MAX_LEN) or f"{self.app.username}'s room"
        self.app.send({"type": "create_room", "name": name, "size": self.room_size.value})
        self.room_name.set("")

    def update(self, dt, pos):
        self.join_btn.enabled = self._joinable(self.rooms.selected_item())
        super().update(dt, pos)

    def _draw_room(self, surf, room, r, selected, hovered):
        x, y, w, h = r
        if selected:
            ui.rect(surf, (219, 234, 254), (x, y + 3, w - 10, h - 6), 12)
            ui.rect(surf, ui.ACCENT, (x, y + 3, w - 10, h - 6), 12, 2)
        elif hovered:
            ui.rect(surf, ui.CARD_ALT, (x, y + 3, w - 10, h - 6), 12)
        ui.text(surf, room["name"], x + 18, y + 12, 17, ui.INK, bold=True)
        ui.text(surf, f"#{room['id']}  ·  host {room['host']}  ·  {', '.join(room['members'])}",
                x + 18, y + 37, 13, ui.MUTED)
        for i in range(room["size"]):
            sx = x + 430 + i * 30
            if i < room["count"]:
                ui.rect(surf, ui.PLAYER_COLORS[i % 4], (sx, y + 20, 22, 22), 6)
            else:
                ui.rect(surf, ui.FAINT, (sx, y + 20, 22, 22), 6, 2)
        if room["phase"] == "waiting":
            label = f"Need {room['missing']} more" if room["missing"] else "Full"
            color = ui.GREEN if room["missing"] else ui.FAINT
            sub = f"Level {room['selected']} selected"
        else:
            label = PHASE_TEXT.get(room["phase"], room["phase"])
            color = PHASE_COLOR.get(room["phase"], ui.ACCENT)
            sub = f"Level {room['level']}/{room['level_count']}"
        cw = ui.text_width(label, 13, True) + 22
        ui.rect(surf, color, (x + w - 30 - cw, y + 12, cw, 24), 12)
        ui.text(surf, label, x + w - 30 - cw / 2, y + 24, 13, ui.WHITE, bold=True, anchor="center")
        ui.text(surf, sub, x + w - 30, y + 41, 12, ui.MUTED, anchor="topright")

    def _draw_online(self, surf, user, r, selected, hovered):
        x, y, w, h = r
        me = user["name"] == self.app.username
        ui.circle(surf, ui.GREEN, (x + 12, y + h / 2), 5)
        ui.text(surf, user["name"] + ("  (you)" if me else ""), x + 26, y + h / 2, 15, ui.INK, bold=me,
                anchor="midleft")
        where = f"room #{user['room']}" if user["room"] else "lobby"
        ui.text(surf, where, x + w - 14, y + h / 2, 13, ui.MUTED, anchor="midright")

    def draw(self, surf, dt):
        app = self.app
        ui.text(surf, "Lobby", 32, 18, 40, ui.WHITE, bold=True)
        prog = app.progress or {}
        chips = [f"★ {app.username}", f"Levels unlocked {prog.get('unlocked', 0) + 1}/{app.level_count}",
                 f"Levels cleared {prog.get('clears', 0)}"]
        cx = 196
        for c in chips:
            cw = ui.text_width(c, 14, True) + 26
            ui.rect_a(surf, (255, 255, 255, 40), (cx, 30, cw, 32), 16)
            ui.text(surf, c, cx + cw / 2, 46, 14, ui.WHITE, bold=True, anchor="center")
            cx += cw + 10
        ui.card(surf, (24, 96, 856, 584), radius=22)
        ui.text(surf, "Rooms", 48, 112, 24, ui.INK, bold=True)
        rooms = self.lobby["rooms"]
        waiting = sum(1 for r in rooms if r["phase"] == "waiting")
        ui.text(surf, f"{waiting} open  ·  {self.lobby['games_in_progress']} game(s) in progress  ·  "
                      f"double-click a room to join", 140, 122, 14, ui.MUTED)
        ui.line(surf, ui.LINE, (44, 160), (860, 160), 1)
        ui.line(surf, ui.LINE, (44, 486), (860, 486), 1)
        ui.text(surf, "Size", 456, 524, 14, ui.MUTED, bold=True, anchor="midleft")
        ui.line(surf, ui.LINE, (44, 572), (860, 572), 1)
        ui.text(surf, "Create a new room", 44, 584, 14, ui.MUTED, bold=True)
        ui.card(surf, (888, 96, 368, 248), radius=22)
        ui.text(surf, f"Online players ({len(self.lobby['online'])})", 904, 112, 16, ui.INK, bold=True)
        super().draw(surf, dt)


# ================================================================ room screen
class LevelCard(ui.Widget):
    def __init__(self, scene, index, x, y, w, h):
        super().__init__(x, y, w, h)
        self.scene = scene
        self.index = index
        self.hover = 0.0

    def _state(self):
        app = self.scene.app
        room = app.room or {}
        locked = self.index > room.get("max_level", 0)
        mine = (app.progress or {}).get("unlocked", 0)
        if not locked:
            reason = ""
        elif self.index > mine:
            reason = f"Clear level {self.index} first"
        else:
            names = [m["name"] for m in room.get("members", []) if m["unlocked"] < self.index]
            reason = "Locked for " + ", ".join(names)
        return room, locked, reason

    def handle(self, ev, pos):
        if ev.type != pygame.MOUSEBUTTONDOWN or ev.button != 1 or not self.hit(pos):
            return False
        app = self.scene.app
        room, locked, reason = self._state()
        if room.get("phase") != "waiting":
            return True
        if room.get("host") != app.username:                      # client-side legality checks
            app.toast(f"Only the host ({room.get('host')}) can choose the level.", "warn")
        elif locked:
            app.toast(f"Level {self.index + 1} is locked - {reason.lower()}.", "warn")
        elif room.get("selected") != self.index:
            app.send({"type": "select_level", "level": self.index})
        return True

    def update(self, dt, pos):
        room, locked, _ = self._state()
        can = room.get("host") == self.scene.app.username and not locked
        self.hover += ((1.0 if can and self.hit(pos) else 0.0) - self.hover) * min(1.0, dt * 14)

    def draw(self, surf):
        app = self.scene.app
        room, locked, reason = self._state()
        levels = room.get("levels") or []
        name = levels[self.index]["name"] if self.index < len(levels) else f"Level {self.index + 1}"
        selected = room.get("selected") == self.index
        lift = 4 * self.hover
        x, y, w, h = self.x, self.y - lift, self.w, self.h
        ui.shadow(surf, (x, y, w, h), 16, 6 + lift, 40)
        ui.rect(surf, (241, 245, 249) if locked else ui.WHITE, (x, y, w, h), 16)
        if selected:
            ui.rect(surf, ui.ACCENT, (x - 2, y - 2, w + 4, h + 4), 18, 4)
            ui.rect(surf, ui.ACCENT, (x + w / 2 - 44, y - 13, 88, 24), 12)
            ui.text(surf, "SELECTED", x + w / 2, y - 1, 12, ui.WHITE, bold=True, anchor="center")
        color = ui.FAINT if locked else ui.PLAYER_COLORS[self.index % 4]
        ui.rect(surf, color, (x + w / 2 - 34, y + 22, 68, 62), 16)
        if locked:
            cx, cy = x + w / 2, y + 53
            ui.circle(surf, ui.WHITE, (cx, cy - 8), 11, 4)
            ui.rect(surf, ui.WHITE, (cx - 15, cy - 6, 30, 24), 5)
            ui.circle(surf, ui.FAINT, (cx, cy + 4), 3)
        else:
            ui.text(surf, str(self.index + 1), x + w / 2, y + 53, 36, ui.WHITE, bold=True, anchor="center")
        lines = ui.wrap(name, 16, w - 20, bold=True)[:2]
        for i, part in enumerate(lines):
            ui.text(surf, part, x + w / 2, y + 100 + i * 20, 16, ui.MUTED if locked else ui.INK, bold=True,
                    anchor="midtop")
        best = (app.progress or {}).get("best", {}).get(str(self.index))
        if locked:
            for i, part in enumerate(ui.wrap(reason, 12, w - 18)[:2]):
                ui.text(surf, part, x + w / 2, y + h - 34 + i * 15, 12, ui.RED, anchor="midtop")
        elif best is not None:
            label = f"best {best:.1f} s"
            lw = ui.text_width(label, 13, True)
            lx = x + w / 2 - lw / 2 + 9
            cy = y + h - 17
            ui.line(surf, ui.GREEN_DARK, (lx - 17, cy), (lx - 12, cy + 4), 2.5)      # check mark
            ui.line(surf, ui.GREEN_DARK, (lx - 12, cy + 4), (lx - 5, cy - 5), 2.5)
            ui.text(surf, label, lx, y + h - 26, 13, ui.GREEN_DARK, bold=True)
        else:
            ui.text(surf, "not cleared yet", x + w / 2, y + h - 26, 13, ui.FAINT, anchor="midtop")


class RoomScene(Scene):
    def __init__(self, app):
        super().__init__(app)
        self.game = GameView(app)
        self.leave_btn = self.add(ui.Button(1124, 14, 132, 46, "Leave", self._leave, "light", 15))
        self.cards: list[LevelCard] = []
        self.ready_btn = self.add(ui.Button(ORIGIN[0] + WORLD_W / 2 - 130, ORIGIN[1] + 432, 260, 58, "I'm ready!",
                                            self._toggle_ready, "success", 19))
        self.chat = self.add(ui.ChatBox(992, 296, 264, 392, "Room chat",
                                        lambda text: app.send({"type": "chat", "text": text}),
                                        color_of=self._color_of, on_escape=lambda: ui.set_focus(None)))

    def _build_cards(self, n):
        """One card per level; the level list comes from the server."""
        for card in self.cards:
            self.widgets.remove(card)
        card_w, gap = min(168, (WORLD_W - 48 - (n - 1) * 16) / max(1, n)), 16
        start = ORIGIN[0] + (WORLD_W - (n * card_w + (n - 1) * gap)) / 2
        self.cards = [LevelCard(self, i, start + i * (card_w + gap), ORIGIN[1] + 92, card_w, 182) for i in range(n)]
        for card in self.cards:
            self.widgets.insert(0, card)

    def _color_of(self, name):
        for m in (self.app.room or {}).get("members", []):
            if m["name"] == name:
                return ui.PLAYER_DARK[m["color"] % 4]
        return ui.ACCENT

    def _in_game(self):
        room = self.app.room or {}
        return room.get("phase", "waiting") != "waiting" and self.game.level is not None

    def _me(self):
        return next((m for m in (self.app.room or {}).get("members", []) if m["name"] == self.app.username), None)

    def _toggle_ready(self):
        room, me = self.app.room, self._me()
        if room and me and room["phase"] == "waiting":                 # only legal while waiting
            self.app.send({"type": "ready", "ready": not me["ready"]})

    def _leave(self):
        room = self.app.room
        if room and room["phase"] != "waiting":
            self.app.confirm("Leave the game?",
                             "The game is running. If you leave, your teammates have to restart the level "
                             "without you.", "Leave", lambda: self.app.send({"type": "leave_room"}))
        else:
            self.app.send({"type": "leave_room"})

    def on_room(self, room):
        if len(room.get("levels", [])) != len(self.cards):
            self._build_cards(len(room["levels"]))
        if room["phase"] == "waiting" and self.game.level is not None:
            self.game.reset()

    def handle(self, ev, pos):
        in_game = self._in_game()
        for card in self.cards:
            card.visible = not in_game
        self.ready_btn.visible = not in_game
        if in_game and ui.Focus.widget is None and ev.type in (pygame.KEYDOWN, pygame.KEYUP):
            if ev.type == pygame.KEYDOWN and ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self.game.release_all()
                ui.set_focus(self.chat.input)
                return True
            if self.game.handle(ev):
                return True
        if ui.Focus.widget is not None and ev.type == pygame.KEYDOWN:
            self.game.release_all()
        return super().handle(ev, pos)

    def update(self, dt, pos):
        in_game = self._in_game()
        for card in self.cards:
            card.visible = not in_game
        self.ready_btn.visible = not in_game
        me = self._me()
        if me:
            self.ready_btn.label = "Cancel ready" if me["ready"] else "I'm ready!"
            self.ready_btn.style = "light" if me["ready"] else "success"
        super().update(dt, pos)

    def draw(self, surf, dt):
        app = self.app
        room = app.room or {}
        phase = room.get("phase", "waiting")
        ui.text(surf, f"#{room.get('id', '')}  {room.get('name', '')}", 24, 16, 30, ui.WHITE, bold=True)
        tx = 24 + ui.text_width(f"#{room.get('id', '')}  {room.get('name', '')}", 30, True) + 18
        label = PHASE_TEXT.get(phase, phase)
        if phase != "waiting" and room.get("level"):
            label += f"  ·  level {room['level']}"
        cw = ui.text_width(label, 14, True) + 26
        ui.rect(surf, PHASE_COLOR.get(phase, ui.ACCENT), (tx, 24, cw, 30), 15)
        ui.text(surf, label, tx + cw / 2, 39, 14, ui.WHITE, bold=True, anchor="center")
        host = f"Host: {room.get('host', '')}"
        hw = ui.text_width(host, 14, True) + 26
        ui.rect_a(surf, (255, 255, 255, 40), (tx + cw + 10, 24, hw, 30), 15)
        ui.text(surf, host, tx + cw + 10 + hw / 2, 39, 14, ui.WHITE, bold=True, anchor="center")

        if self._in_game():
            self.game.draw(surf, dt)
        else:
            self._draw_level_select(surf, room)
        self._draw_info_bar(surf, room)
        self._draw_players(surf, room)
        super().draw(surf, dt)

    def _draw_level_select(self, surf, room):
        ox, oy = ORIGIN
        ui.card(surf, (ox, oy, WORLD_W, WORLD_H), radius=22)
        host = room.get("host")
        me_host = host == self.app.username
        ui.text(surf, "Choose a level", ox + 32, oy + 22, 28, ui.INK, bold=True)
        sub = ("You are the host: click a level to choose it." if me_host else
               f"{host} (host) chooses the level.") + "  Clear a level to unlock the next one."
        ui.text(surf, sub, ox + 32, oy + 58, 15, ui.MUTED)
        # seats
        members = room.get("members", [])
        size = room.get("size", 2)
        seat_w = 120
        sx0 = ox + WORLD_W / 2 - size * seat_w / 2
        sy = oy + 300
        for i in range(size):
            cx = sx0 + i * seat_w + seat_w / 2
            if i < len(members):
                m = members[i]
                ui.block_character(surf, cx - 22, sy, 44, m["color"], gray=not m["connected"],
                                   blink=(time.monotonic() + i) % 3.7 < 0.12)
                if m["name"] == host:
                    ui.polygon(surf, ui.GOLD, [(cx - 12, sy - 4), (cx - 14, sy - 18), (cx - 6, sy - 10), (cx, sy - 20),
                                               (cx + 6, sy - 10), (cx + 14, sy - 18), (cx + 12, sy - 4)])
                ui.text(surf, m["name"], cx, sy + 52, 15, ui.INK, bold=True, anchor="midtop")
                tag, col = ("READY", ui.GREEN) if m["ready"] else ("not ready", ui.FAINT)
                tw = ui.text_width(tag, 12, True) + 18
                ui.rect(surf, col, (cx - tw / 2, sy + 76, tw, 20), 10)
                ui.text(surf, tag, cx, sy + 86, 12, ui.WHITE, bold=True, anchor="center")
            else:
                ui.rect(surf, ui.FAINT, (cx - 22, sy, 44, 44), 11, 2)
                ui.text(surf, "?", cx, sy + 22, 20, ui.FAINT, bold=True, anchor="center")
                ui.text(surf, "empty seat", cx, sy + 52, 14, ui.FAINT, anchor="midtop")
        missing = room.get("missing", 0)
        if missing:
            status = f"Waiting for {missing} more player(s)  -  the game starts when the room is full and everyone is ready"
        else:
            waiting = [m["name"] for m in members if not m["ready"]]
            status = ("Waiting for " + ", ".join(waiting) + " to press Ready") if waiting else "Starting..."
        ui.text(surf, status, ox + WORLD_W / 2, oy + 410, 15, ui.MUTED, anchor="center")

    def _draw_info_bar(self, surf, room):
        ox = ORIGIN[0]
        y = ORIGIN[1] + WORLD_H + 16
        ui.card(surf, (ox, y, WORLD_W, 88), radius=18)
        if self._in_game():
            hint = self.game.level.get("hint", "")
        else:
            levels = room.get("levels") or []
            sel = room.get("selected", 0)
            hint = (f"Level {sel + 1}: " + levels[sel]["hint"]) if sel < len(levels) else ""
        for i, part in enumerate(ui.wrap(hint, 14, WORLD_W - 40)[:2]):
            ui.text(surf, part, ox + 20, y + 12 + i * 19, 14, ui.INK)
        kx = ox + 20
        ky = y + 52
        for keys, label in ((("←", "→"), "move"), (("↑", "Space"), "jump"), (("P",), "pause"),
                            (("R",), "restart vote"), (("Enter",), "chat")):
            for k in keys:
                kx += ui.keycap(surf, k, kx, ky) + 4
            kx += 4
            ui.text(surf, label, kx, ky + 13, 13, ui.MUTED, anchor="midleft")
            kx += ui.text_width(label, 13) + 22

    def _draw_players(self, surf, room):
        x, y, w = 992, 72, 264
        members = room.get("members", [])
        h = 210
        ui.card(surf, (x, y, w, h), radius=20)
        ui.text(surf, f"Players {len(members)}/{room.get('size', '?')}", x + 16, y + 14, 16, ui.INK, bold=True)
        phase = room.get("phase")
        for i, m in enumerate(members):
            ry = y + 50 + i * 38
            ui.block_character(surf, x + 16, ry, 26, m["color"], gray=not m["connected"])
            name = m["name"] + ("  (you)" if m["name"] == self.app.username else "")
            ui.text(surf, name, x + 52, ry + 13, 15, ui.INK, bold=m["name"] == self.app.username, anchor="midleft")
            if not m["connected"]:
                state, col = f"offline {m['reconnect_in']}s", ui.RED
            elif phase == "waiting":
                state, col = ("ready", ui.GREEN_DARK) if m["ready"] else ("not ready", ui.FAINT)
            else:
                state, col = "online", ui.GREEN_DARK
            ui.text(surf, state, x + w - 16, ry + 13, 13, col, bold=True, anchor="midright")
        for i in range(room.get("missing", 0)):
            ry = y + 50 + (len(members) + i) * 38
            ui.rect(surf, ui.LINE, (x + 16, ry, 26, 26), 7, 2)
            ui.text(surf, "empty seat", x + 52, ry + 13, 14, ui.FAINT, anchor="midleft")
