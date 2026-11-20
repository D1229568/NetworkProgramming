"""A small, resolution-independent UI toolkit on top of pygame-ce.

Everything is laid out on a fixed *logical* canvas of 1280x720 and scaled to
the real window size at draw time, so text and shapes stay sharp on any
screen (including 150%/200% Windows display scaling).

    R(x, y, w, h)   logical rectangle -> pygame.Rect in window pixels
    pt(x, y)        logical point     -> window pixel
    to_logical(..)  window pixel      -> logical point (for the mouse)
"""
from __future__ import annotations

import time
from collections import OrderedDict

import pygame

W, H = 1280, 720

# --- palette -----------------------------------------------------------------
INK = (30, 41, 59)
MUTED = (100, 116, 139)
FAINT = (148, 163, 184)
WHITE = (255, 255, 255)
CARD = (255, 255, 255)
CARD_ALT = (241, 245, 249)
LINE = (226, 232, 240)
ACCENT = (59, 130, 246)
ACCENT_DARK = (29, 78, 216)
GREEN = (34, 197, 94)
GREEN_DARK = (21, 128, 61)
RED = (239, 68, 68)
RED_DARK = (185, 28, 28)
ORANGE = (249, 115, 22)
GOLD = (250, 204, 21)
BG_TOP = (30, 27, 75)
BG_BOTTOM = (37, 99, 235)
PLAYER_COLORS = [(239, 68, 68), (59, 130, 246), (34, 197, 94), (234, 179, 8)]
PLAYER_DARK = [(153, 27, 27), (30, 64, 175), (21, 101, 52), (161, 98, 7)]


class View:
    scale = 1.0
    ox = 0.0
    oy = 0.0
    version = 0      # bumps on every resize so caches know to rebuild


def configure(win_w: int, win_h: int):
    View.scale = min(win_w / W, win_h / H)
    View.ox = (win_w - W * View.scale) / 2
    View.oy = (win_h - H * View.scale) / 2
    View.version += 1
    _fonts.clear()
    _texts.clear()
    _alpha.clear()


def s(v: float) -> float:
    return v * View.scale


def pt(x: float, y: float) -> tuple[float, float]:
    return View.ox + x * View.scale, View.oy + y * View.scale


def R(x: float, y: float, w: float, h: float) -> pygame.Rect:
    sc = View.scale
    x0, y0 = round(View.ox + x * sc), round(View.oy + y * sc)
    x1, y1 = round(View.ox + (x + w) * sc), round(View.oy + (y + h) * sc)
    return pygame.Rect(x0, y0, max(1, x1 - x0), max(1, y1 - y0))


def to_logical(px: float, py: float) -> tuple[float, float]:
    return (px - View.ox) / View.scale, (py - View.oy) / View.scale


def mix(a, b, t: float):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


# --- shapes ------------------------------------------------------------------
def rect(surf, color, r, radius=0, width=0):
    pygame.draw.rect(surf, color, R(*r), width=max(1, round(s(width))) if width else 0,
                     border_radius=max(0, round(s(radius))))


_alpha: OrderedDict = OrderedDict()


def rect_a(surf, rgba, r, radius=0):
    """Rectangle with transparency (cached, because SRCALPHA surfaces are slow to make)."""
    pr = R(*r)
    key = (pr.size, rgba, radius)
    img = _alpha.get(key)
    if img is None:
        img = pygame.Surface(pr.size, pygame.SRCALPHA)
        pygame.draw.rect(img, rgba, img.get_rect(), border_radius=max(0, round(s(radius))))
        _alpha[key] = img
        if len(_alpha) > 300:
            _alpha.popitem(last=False)
    surf.blit(img, pr)


def shadow(surf, r, radius=18, dy=8, alpha=50):
    x, y, w, h = r
    rect_a(surf, (8, 12, 40, alpha // 2), (x - 2, y + dy - 2, w + 4, h + 4), radius + 2)
    rect_a(surf, (8, 12, 40, alpha), (x, y + dy, w, h), radius)


def card(surf, r, color=CARD, radius=18, dy=8):
    shadow(surf, r, radius, dy)
    rect(surf, color, r, radius)


def line(surf, color, a, b, width=1):
    pygame.draw.line(surf, color, pt(*a), pt(*b), max(1, round(s(width))))


def circle(surf, color, c, radius, width=0):
    pygame.draw.circle(surf, color, pt(*c), max(1.0, s(radius)), max(1, round(s(width))) if width else 0)


def polygon(surf, color, pts, width=0):
    pygame.draw.polygon(surf, color, [pt(*p) for p in pts], max(1, round(s(width))) if width else 0)


def gradient(size, top, bottom) -> pygame.Surface:
    """Vertical gradient surface of the given pixel size."""
    tiny = pygame.Surface((1, 2))
    tiny.set_at((0, 0), top)
    tiny.set_at((0, 1), bottom)
    return pygame.transform.smoothscale(tiny, size)


# --- text --------------------------------------------------------------------
FONT_NAMES = ("microsoftjhengheiui,microsoftjhenghei,microsoftyaheiui,pingfangtc,"
              "notosanscjktc,notosanscjk,arialunicodems,segoeui,arial")
_paths: dict = {}
_fonts: dict = {}
_texts: OrderedDict = OrderedDict()


def font(size: float, bold=False) -> pygame.font.Font:
    px = max(6, round(size * View.scale))
    key = (px, bold)
    f = _fonts.get(key)
    if f is None:
        if bold not in _paths:
            _paths[bold] = pygame.font.match_font(FONT_NAMES, bold=bold)
        f = pygame.font.Font(_paths[bold], px)
        _fonts[key] = f
    return f


def text_surface(txt: str, size: float, color, bold=False) -> pygame.Surface:
    key = (txt, size, color, bold)
    img = _texts.get(key)
    if img is None:
        img = font(size, bold).render(txt, True, color)
        _texts[key] = img
        if len(_texts) > 900:
            _texts.popitem(last=False)
    else:
        _texts.move_to_end(key)
    return img


def text(surf, txt, x, y, size=16, color=INK, bold=False, anchor="topleft", alpha=255) -> pygame.Rect:
    img = text_surface(str(txt), size, color, bold)
    if alpha < 255:
        img = img.copy()
        img.set_alpha(alpha)
    r = img.get_rect(**{anchor: pt(x, y)})
    surf.blit(img, r)
    return r


def text_width(txt: str, size: float, bold=False) -> float:
    return font(size, bold).size(txt)[0] / View.scale


def wrap(txt: str, size: float, max_w: float, bold=False, first_indent: float = 0.0) -> list[str]:
    """Word-wrap (falls back to per-character breaks for Chinese text)."""
    lines, cur = [], ""
    limit = max_w - first_indent
    for word in txt.split(" "):
        candidate = word if not cur else cur + " " + word
        if text_width(candidate, size, bold) <= limit:
            cur = candidate
            continue
        if cur:
            lines.append(cur)
            limit = max_w
        cur = ""
        for ch in word:                     # very long word / CJK sentence
            if text_width(cur + ch, size, bold) > limit and cur:
                lines.append(cur)
                limit = max_w
                cur = ""
            cur += ch
    lines.append(cur)
    return lines


# --- focus ---------------------------------------------------------------------
class Focus:
    widget = None


def set_focus(widget):
    if Focus.widget is widget:
        return
    if Focus.widget is not None:
        Focus.widget.focused = False
    Focus.widget = widget
    if widget is not None:
        widget.focused = True
        widget.blink = 0.0
        pygame.key.start_text_input()
        pygame.key.set_text_input_rect(R(*widget.rect))
    else:
        pygame.key.stop_text_input()


# --- widgets -----------------------------------------------------------------
class Widget:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.visible = True
        self.enabled = True

    @property
    def rect(self):
        return self.x, self.y, self.w, self.h

    def hit(self, pos) -> bool:
        return self.x <= pos[0] < self.x + self.w and self.y <= pos[1] < self.y + self.h

    def handle(self, ev, pos) -> bool:
        return False

    def update(self, dt, pos):
        pass

    def draw(self, surf):
        pass


class Button(Widget):
    STYLES = {
        "primary": (ACCENT, ACCENT_DARK, WHITE),
        "success": (GREEN, GREEN_DARK, WHITE),
        "danger": (RED, RED_DARK, WHITE),
        "warning": (ORANGE, (194, 65, 12), WHITE),
        "light": ((241, 245, 249), (203, 213, 225), INK),
    }
    DEPTH = 5

    def __init__(self, x, y, w, h, label, on_click, style="primary", size=17):
        super().__init__(x, y, w, h)
        self.label = label
        self.on_click = on_click
        self.style = style
        self.size = size
        self.hover = 0.0
        self.down = False

    def handle(self, ev, pos):
        if not self.visible or not self.enabled:
            return False
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1 and self.hit(pos):
            self.down = True
            return True
        if ev.type == pygame.MOUSEBUTTONUP and ev.button == 1 and self.down:
            self.down = False
            if self.hit(pos) and self.on_click:
                self.on_click()
            return True
        return False

    def update(self, dt, pos):
        target = 1.0 if self.enabled and self.hit(pos) else 0.0
        self.hover += (target - self.hover) * min(1.0, dt * 14)

    def draw(self, surf):
        if not self.visible:
            return
        face, edge, fg = self.STYLES[self.style]
        if not self.enabled:
            face, edge, fg = (226, 232, 240), (203, 213, 225), FAINT
        else:
            face = mix(face, WHITE, 0.12 * self.hover)
        d = self.DEPTH
        press = 3 if self.down else 0
        rect(surf, edge, (self.x, self.y + d, self.w, self.h - d), 12)
        rect(surf, face, (self.x, self.y + press, self.w, self.h - d), 12)
        text(surf, self.label, self.x + self.w / 2, self.y + press + (self.h - d) / 2, self.size, fg,
             bold=True, anchor="center")


class TextInput(Widget):
    def __init__(self, x, y, w, h, placeholder="", secret=False, max_len=64, on_submit=None,
                 on_escape=None, size=17, value=""):
        super().__init__(x, y, w, h)
        self.placeholder = placeholder
        self.secret = secret
        self.max_len = max_len
        self.on_submit = on_submit
        self.on_escape = on_escape
        self.size = size
        self.text = value
        self.cursor = len(value)
        self.focused = False
        self.blink = 0.0

    def set(self, value: str):
        self.text = value[: self.max_len]
        self.cursor = len(self.text)

    def _shown(self):
        return "•" * len(self.text) if self.secret else self.text

    def _insert(self, s_):
        s_ = "".join(ch for ch in s_ if ch.isprintable())
        room = self.max_len - len(self.text)
        if room <= 0 or not s_:
            return
        s_ = s_[:room]
        self.text = self.text[: self.cursor] + s_ + self.text[self.cursor:]
        self.cursor += len(s_)

    def handle(self, ev, pos):
        if not self.visible or not self.enabled:
            return False
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            if self.hit(pos):
                set_focus(self)
                shown = self._shown()
                rel = pos[0] - self.x - 12 + self._scroll
                self.cursor = len(shown)
                for i in range(len(shown) + 1):
                    if text_width(shown[:i], self.size) >= rel:
                        self.cursor = i
                        break
                return True
            return False
        if not self.focused:
            return False
        if ev.type == pygame.TEXTINPUT:
            self._insert(ev.text)
            self.blink = 0.0
            return True
        if ev.type == pygame.KEYDOWN:
            ctrl = ev.mod & pygame.KMOD_CTRL
            self.blink = 0.0
            if ev.key == pygame.K_BACKSPACE and self.cursor > 0:
                start = self.cursor - 1
                if ctrl:
                    while start > 0 and self.text[start - 1] != " ":
                        start -= 1
                self.text = self.text[:start] + self.text[self.cursor:]
                self.cursor = start
            elif ev.key == pygame.K_DELETE:
                self.text = self.text[: self.cursor] + self.text[self.cursor + 1:]
            elif ev.key == pygame.K_LEFT:
                self.cursor = max(0, self.cursor - 1)
            elif ev.key == pygame.K_RIGHT:
                self.cursor = min(len(self.text), self.cursor + 1)
            elif ev.key == pygame.K_HOME:
                self.cursor = 0
            elif ev.key == pygame.K_END:
                self.cursor = len(self.text)
            elif ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                if self.on_submit:
                    self.on_submit()
            elif ev.key == pygame.K_ESCAPE:
                if self.on_escape:
                    self.on_escape()
                else:
                    set_focus(None)
            elif ev.key == pygame.K_v and ctrl:
                try:
                    self._insert(pygame.scrap.get_text() or "")
                except Exception:
                    pass
            elif ev.key == pygame.K_TAB:
                return False            # let the scene move focus
            return True
        return False

    _scroll = 0.0

    def update(self, dt, pos):
        self.blink += dt

    def draw(self, surf):
        if not self.visible:
            return
        if self.focused:
            rect_a(surf, (59, 130, 246, 60), (self.x - 3, self.y - 3, self.w + 6, self.h + 6), 13)
        rect(surf, (248, 250, 252) if self.enabled else CARD_ALT, self.rect, 10)
        rect(surf, ACCENT if self.focused else LINE, self.rect, 10, 2 if self.focused else 1.5)
        inner = R(self.x + 12, self.y, self.w - 24, self.h)
        old_clip = surf.get_clip()
        surf.set_clip(inner.clip(old_clip) if old_clip else inner)
        shown = self._shown()
        cy = self.y + self.h / 2
        if not shown:
            text(surf, self.placeholder, self.x + 12, cy, self.size, FAINT, anchor="midleft")
            self._scroll = 0.0
        else:
            before = text_width(shown[: self.cursor], self.size)
            visible_w = self.w - 26
            if before - self._scroll > visible_w:
                self._scroll = before - visible_w
            elif before < self._scroll:
                self._scroll = before
            text(surf, shown, self.x + 12 - self._scroll, cy, self.size, INK, anchor="midleft")
        if self.focused and (self.blink % 1.0) < 0.55:
            cx = self.x + 12 + text_width(shown[: self.cursor], self.size) - self._scroll
            line(surf, ACCENT, (cx, cy - self.size * 0.62), (cx, cy + self.size * 0.62), 2)
        surf.set_clip(old_clip)


class Segmented(Widget):
    def __init__(self, x, y, w, h, options, value, on_change=None, size=15):
        super().__init__(x, y, w, h)
        self.options = options          # [(value, label)]
        self.value = value
        self.on_change = on_change
        self.size = size
        self._pos = self._index()

    def _index(self):
        return next((i for i, (v, _) in enumerate(self.options) if v == self.value), 0)

    def handle(self, ev, pos):
        if not self.visible or not self.enabled:
            return False
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1 and self.hit(pos):
            i = min(len(self.options) - 1, int((pos[0] - self.x) / (self.w / len(self.options))))
            value = self.options[i][0]
            if value != self.value:
                self.value = value
                if self.on_change:
                    self.on_change(value)
            return True
        return False

    def update(self, dt, pos):
        self._pos += (self._index() - self._pos) * min(1.0, dt * 16)

    def draw(self, surf):
        if not self.visible:
            return
        n = len(self.options)
        seg = self.w / n
        rect(surf, CARD_ALT, self.rect, self.h / 2)
        rect(surf, ACCENT, (self.x + 3 + self._pos * seg, self.y + 3, seg - 6, self.h - 6), (self.h - 6) / 2)
        for i, (value, label) in enumerate(self.options):
            selected = value == self.value
            text(surf, label, self.x + seg * i + seg / 2, self.y + self.h / 2, self.size,
                 WHITE if selected else MUTED, bold=selected, anchor="center")


class Toggle(Widget):
    """An on/off switch with a label on its left."""

    def __init__(self, x, y, w, h, label, value=True, on_change=None, size=13):
        super().__init__(x, y, w, h)
        self.label = label
        self.value = value
        self.on_change = on_change
        self.size = size
        self._pos = 1.0 if value else 0.0

    def handle(self, ev, pos):
        if not self.visible or not self.enabled:
            return False
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1 and self.hit(pos):
            self.value = not self.value
            if self.on_change:
                self.on_change(self.value)
            return True
        return False

    def update(self, dt, pos):
        self._pos += ((1.0 if self.value else 0.0) - self._pos) * min(1.0, dt * 16)

    def draw(self, surf):
        if not self.visible:
            return
        sw, sh = 42, 22
        sx, sy = self.x + self.w - sw, self.y + (self.h - sh) / 2
        text(surf, self.label, sx - 8, self.y + self.h / 2, self.size, GREEN_DARK if self.value else MUTED,
             bold=True, anchor="midright")
        rect(surf, mix((203, 213, 225), GREEN, self._pos), (sx, sy, sw, sh), sh / 2)
        circle(surf, WHITE, (sx + sh / 2 + self._pos * (sw - sh), sy + sh / 2), sh / 2 - 3)


class ListView(Widget):
    def __init__(self, x, y, w, h, row_h, draw_row, key=lambda item: item, on_select=None,
                 on_activate=None, empty_text=""):
        super().__init__(x, y, w, h)
        self.row_h = row_h
        self.draw_row = draw_row
        self.key = key
        self.on_select = on_select
        self.on_activate = on_activate
        self.empty_text = empty_text
        self.items: list = []
        self.selected = None
        self.scroll = 0.0
        self.hover_index = -1
        self._last_click = (0.0, -1)

    def set_items(self, items):
        self.items = list(items)
        if self.selected is not None and not any(self.key(i) == self.selected for i in self.items):
            self.selected = None
        self._clamp()

    def selected_item(self):
        return next((i for i in self.items if self.key(i) == self.selected), None)

    def _clamp(self):
        self.scroll = max(0.0, min(self.scroll, len(self.items) * self.row_h - self.h))

    def handle(self, ev, pos):
        if not self.visible:
            return False
        if ev.type == pygame.MOUSEWHEEL and self.hit(pos):
            self.scroll -= ev.y * self.row_h * 0.8
            self._clamp()
            return True
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1 and self.hit(pos):
            idx = int((pos[1] - self.y + self.scroll) // self.row_h)
            if 0 <= idx < len(self.items):
                item = self.items[idx]
                self.selected = self.key(item)
                now = time.monotonic()
                if self._last_click[1] == idx and now - self._last_click[0] < 0.4 and self.on_activate:
                    self.on_activate(item)
                elif self.on_select:
                    self.on_select(item)
                self._last_click = (now, idx)
            return True
        return False

    def update(self, dt, pos):
        if self.hit(pos):
            self.hover_index = int((pos[1] - self.y + self.scroll) // self.row_h)
        else:
            self.hover_index = -1

    def draw(self, surf):
        if not self.visible:
            return
        clip = R(*self.rect)
        old = surf.get_clip()
        surf.set_clip(clip)
        if not self.items and self.empty_text:
            text(surf, self.empty_text, self.x + self.w / 2, self.y + 40, 15, FAINT, anchor="center")
        first = int(self.scroll // self.row_h)
        for i in range(first, min(len(self.items), first + int(self.h // self.row_h) + 2)):
            ry = self.y + i * self.row_h - self.scroll
            item = self.items[i]
            self.draw_row(surf, item, (self.x, ry, self.w, self.row_h),
                          self.key(item) == self.selected, i == self.hover_index)
        surf.set_clip(old)
        total = len(self.items) * self.row_h
        if total > self.h:
            bar_h = max(30, self.h * self.h / total)
            bar_y = self.y + (self.h - bar_h) * (self.scroll / (total - self.h))
            rect(surf, LINE, (self.x + self.w - 5, bar_y, 4, bar_h), 2)


class ChatBox(Widget):
    """Chat log with word wrap and scrolling, plus an input line."""

    def __init__(self, x, y, w, h, title, on_send, color_of=None, on_escape=None):
        super().__init__(x, y, w, h)
        self.title = title
        self.on_send = on_send
        self.color_of = color_of or (lambda name: ACCENT)
        self.entries: list[tuple] = []
        self.scroll = 0                 # rows scrolled up from the bottom
        self.input = TextInput(x + 14, y + h - 52, w - 98, 38, "Type a message...", max_len=200,
                               on_submit=self._send, on_escape=on_escape, size=15)
        self.button = Button(x + w - 78, y + h - 54, 64, 42, "Send", self._send, size=14)
        self._rows_cache = (None, [])

    def _send(self):
        msg = self.input.text.strip()
        if msg:
            self.on_send(msg)
            self.input.set("")
        if self.input.on_escape:
            self.input.on_escape()

    def add(self, sender, txt, system=False):
        self.entries.append((time.strftime("%H:%M"), sender, txt, system))
        self.entries = self.entries[-200:]
        self.scroll = 0
        self._rows_cache = (None, [])

    def clear(self):
        self.entries = []
        self._rows_cache = (None, [])

    def _rows(self, width):
        key = (View.version, len(self.entries), width)
        if self._rows_cache[0] == key:
            return self._rows_cache[1]
        rows = []
        for stamp, sender, txt, system in self.entries:
            if system:
                for i, part in enumerate(wrap(txt, 14, width - 16)):
                    rows.append(("system", None, part if i else "• " + part))
            else:
                head = f"{sender}: "
                indent = text_width(head, 14, bold=True)
                for i, part in enumerate(wrap(txt, 14, width, first_indent=indent)):
                    rows.append(("first" if i == 0 else "cont", sender, part))
        self._rows_cache = (key, rows)
        return rows

    def handle(self, ev, pos):
        if self.input.handle(ev, pos) or self.button.handle(ev, pos):
            return True
        if ev.type == pygame.MOUSEWHEEL and self.hit(pos):
            self.scroll = max(0, self.scroll + ev.y * 2)
            return True
        return False

    def update(self, dt, pos):
        self.input.update(dt, pos)
        self.button.update(dt, pos)

    def draw(self, surf):
        card(surf, self.rect)
        text(surf, self.title, self.x + 16, self.y + 14, 16, INK, bold=True)
        log_x, log_y = self.x + 16, self.y + 44
        log_w, log_h = self.w - 32, self.h - 44 - 64
        rows = self._rows(log_w)
        row_h = 21
        fit = int(log_h // row_h)
        self.scroll = min(self.scroll, max(0, len(rows) - fit))
        visible = rows[max(0, len(rows) - fit - self.scroll): len(rows) - self.scroll]
        old = surf.get_clip()
        surf.set_clip(R(log_x, log_y, log_w, log_h))
        y = log_y + log_h - len(visible) * row_h
        for kind, sender, part in visible:
            if kind == "system":
                text(surf, part, log_x, y, 14, MUTED)
            elif kind == "first":
                r = text(surf, f"{sender}: ", log_x, y, 14, self.color_of(sender), bold=True)
                text(surf, part, log_x + (r.width / View.scale), y, 14, INK)
            else:
                text(surf, part, log_x, y, 14, INK)
            y += row_h
        surf.set_clip(old)
        if self.scroll:
            text(surf, "↓ newer messages", self.x + self.w - 16, self.y + 16, 12, ACCENT, anchor="topright")
        self.input.draw(surf)
        self.button.draw(surf)


class Toasts:
    COLORS = {"info": ACCENT, "ok": GREEN, "warn": ORANGE, "error": RED}

    def __init__(self):
        self.items: list[list] = []

    def push(self, txt, kind="info", ttl=3.5):
        self.items.append([txt, kind, time.monotonic(), ttl])
        self.items = self.items[-3:]

    def draw(self, surf):
        now = time.monotonic()
        self.items = [t for t in self.items if now - t[2] < t[3]]
        y = 70
        for txt, kind, born, ttl in self.items:
            age = now - born
            k = min(1.0, age * 6, (ttl - age) * 4)
            w = min(640, text_width(txt, 15, True) + 54)
            x = W / 2 - w / 2
            yy = y - (1 - k) * 16
            shadow(surf, (x, yy, w, 40), 20, 6, int(60 * k))
            rect(surf, (15, 23, 42), (x, yy, w, 40), 20)
            circle(surf, self.COLORS.get(kind, ACCENT), (x + 22, yy + 20), 6)
            text(surf, txt, x + 38, yy + 20, 15, WHITE, bold=True, anchor="midleft")
            y += 48


class Modal:
    """A dialog that blocks the rest of the UI until a button is pressed."""

    def __init__(self, title, message, buttons):
        self.title = title
        self.message = message
        self.w = 560
        self.lines = wrap(message, 16, self.w - 56)[:9]
        self.h = 160 + 24 * len(self.lines)
        x, y = W / 2 - self.w / 2, H / 2 - self.h / 2
        self.box = (x, y, self.w, self.h)
        self.buttons = []
        bx = x + self.w - 24
        for label, style, callback in reversed(buttons):
            bw = max(110, text_width(label, 16, True) + 40)
            bx -= bw
            self.buttons.append(Button(bx, y + self.h - 70, bw, 48, label, self._wrap(callback), style, 16))
            bx -= 12
        self.closed = False

    def _wrap(self, callback):
        def run():
            self.closed = True
            if callback:
                callback()
        return run

    def handle(self, ev, pos):
        for b in self.buttons:
            b.handle(ev, pos)
        if ev.type == pygame.KEYDOWN and ev.key in (pygame.K_ESCAPE, pygame.K_RETURN):
            # buttons are stored right-to-left: [0] = main action, [-1] = cancel
            self.buttons[-1 if ev.key == pygame.K_ESCAPE else 0].on_click()
        return True

    def update(self, dt, pos):
        for b in self.buttons:
            b.update(dt, pos)

    def draw(self, surf):
        rect_a(surf, (15, 23, 42, 150), (-200, -200, W + 400, H + 400))
        x, y, w, h = self.box
        card(surf, self.box, radius=20)
        text(surf, self.title, x + 28, y + 26, 22, INK, bold=True)
        for i, part in enumerate(self.lines):
            text(surf, part, x + 28, y + 70 + i * 24, 16, MUTED)
        for b in self.buttons:
            b.draw(surf)


# --- the little block characters (players) ------------------------------------
def block_character(surf, x, y, size, color, facing=1, look_up=0.0, blink=False, squash=0.0,
                    gray=False, to_screen=None):
    """Draw a player block with eyes; (x, y) is the top-left corner in logical px.

    ``to_screen`` maps logical rects; by default the global UI mapping is used.
    """
    mapper = to_screen or R
    body = PLAYER_COLORS[color % 4] if not gray else (148, 163, 184)
    dark = PLAYER_DARK[color % 4] if not gray else (100, 116, 139)
    w = size * (1 + squash)
    h = size * (1 - squash)
    bx = x + (size - w) / 2
    by = y + (size - h)
    rad = max(1, round(s(size * 0.24)))
    pygame.draw.rect(surf, dark, mapper(bx, by, w, h), border_radius=rad)
    inset = size * 0.08
    pygame.draw.rect(surf, body, mapper(bx + inset, by + inset * 0.6, w - 2 * inset, h - inset * 1.8),
                     border_radius=max(1, rad - 1))
    pygame.draw.rect(surf, (255, 255, 255), mapper(bx + w * 0.18, by + h * 0.14, w * 0.18, h * 0.08),
                     border_radius=max(1, round(s(size * 0.04))))
    ex = bx + w * 0.5 + facing * w * 0.08
    ey = by + h * 0.36 - look_up * h * 0.06
    eye_w, eye_h = w * 0.2, h * 0.3
    for dx in (-0.17, 0.17):
        cx = ex + dx * w
        if blink:
            pygame.draw.rect(surf, (15, 23, 42), mapper(cx - eye_w / 2, ey + eye_h * 0.45, eye_w, max(1.5, h * 0.05)))
            continue
        pygame.draw.ellipse(surf, (255, 255, 255), mapper(cx - eye_w / 2, ey, eye_w, eye_h))
        pygame.draw.ellipse(surf, (15, 23, 42), mapper(cx - eye_w * 0.22 + facing * eye_w * 0.18,
                                                       ey + eye_h * (0.28 - look_up * 0.15),
                                                       eye_w * 0.44, eye_h * 0.55))


def keycap(surf, label, x, y, size=13) -> float:
    """Draw a keyboard key; returns its width (logical)."""
    w = max(26, text_width(label, size, True) + 16)
    rect(surf, (203, 213, 225), (x, y + 3, w, 26), 7)
    rect(surf, (248, 250, 252), (x, y, w, 26), 7)
    text(surf, label, x + w / 2, y + 13, size, INK, bold=True, anchor="center")
    return w
