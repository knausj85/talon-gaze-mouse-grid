"""Dwell-to-select toolbar of mouse actions, drawn above the gaze grid's zoom view.

The toolbar lives on the grid's own skia canvas, so it shares the grid's
lifecycle, position math and screenshot hiding, and works the same on macOS and
Windows. Hovering a button for `user.gaze_grid_action_dwell` seconds selects it;
while hovering, the button fills up to show progress.
"""

from dataclasses import dataclass
from typing import Optional

from talon import actions, app, ctrl
from talon.skia import Paint, Path, Rect

IS_MAC = app.platform == "mac"

# How long the cursor may leave a button before its dwell progress resets.
# Eye tracking jitters; without this, brief excursions restart the countdown.
DWELL_GRACE_SECONDS = 0.15

BUTTON_W = 74
BUTTON_H = 72
BUTTON_GAP = 8
PANEL_PAD = 8
PANEL_GAP = 14
LABEL_SIZE = 13
BADGE_SIZE = 15

# The default typeface lacks the macOS modifier glyphs (⌘ ⇧ ⌃); Lucida Grande has them.
TOOLBAR_TYPEFACE = "Lucida Grande" if IS_MAC else None

COLOR_PANEL = "18181ce6"
COLOR_BUTTON = "2c2c34ff"
COLOR_BUTTON_BORDER = "5a5a64ff"
COLOR_PROGRESS = "4696ff88"
COLOR_SELECTED = "4696ffff"
COLOR_SELECTED_BORDER = "c8e0ffff"
COLOR_ICON = "ebebebff"
COLOR_ICON_FILL = "ebebebff"
COLOR_ICON_ON_SELECTED = "ffffffff"
COLOR_STATUS = "f5be6eff"


@dataclass(frozen=True)
class MouseAction:
    key: str
    label: str
    button: int = 0  # 0 = left, 1 = right
    count: int = 1
    modifiers: tuple[str, ...] = ()
    badge: str = ""
    is_drag: bool = False
    is_zoom: bool = False  # narrow the grid instead of clicking


def _modifier_actions() -> list[MouseAction]:
    # cmd-click on macOS is ctrl-click on Windows/Linux, so there "cmd" is left out
    # rather than shown as a duplicate of ctrl.
    if not IS_MAC:
        return [
            MouseAction("shift", "Shift", modifiers=("shift",), badge="+"),
            MouseAction("ctrl", "Ctrl", modifiers=("ctrl",), badge="+"),
        ]
    return [
        MouseAction("cmd", "Cmd", modifiers=("cmd",), badge="⌘"),
        MouseAction("shift", "Shift", modifiers=("shift",), badge="⇧"),
        MouseAction("ctrl", "Ctrl", modifiers=("ctrl",), badge="⌃"),
    ]


MOUSE_ACTIONS: list[MouseAction] = [
    MouseAction("zoom", "Zoom", is_zoom=True),
    MouseAction("left", "Left"),
    MouseAction("right", "Right", button=1),
    MouseAction("double", "Double", count=2, badge="2×"),
    MouseAction("triple", "Triple", count=3, badge="3×"),
    *_modifier_actions(),
    MouseAction("drag", "Drag", is_drag=True),
]
ACTIONS_BY_KEY = {a.key: a for a in MOUSE_ACTIONS}
DEFAULT_ACTION_KEY = "left"


def perform_click(action: MouseAction):
    """Click at the current cursor position with the action's button, count and modifiers."""
    for mod in action.modifiers:
        actions.key(f"{mod}:down")
    try:
        for _ in range(action.count):
            actions.mouse_click(action.button)
    finally:
        for mod in reversed(action.modifiers):
            actions.key(f"{mod}:up")


def perform_drag(start: tuple[float, float], end: tuple[float, float], steps: int = 15):
    """Press at `start`, move in steps to `end`, release.

    The button is only held during this call (never while the grid is open), so a
    cancelled drag can't leave it stuck down. Intermediate moves and short pauses let
    apps recognise the gesture as a drag rather than a click.
    """
    sx, sy = start
    ex, ey = end
    ctrl.mouse_move(sx, sy)
    actions.sleep("50ms")
    ctrl.mouse_click(button=0, down=True)
    try:
        actions.sleep("100ms")
        for i in range(1, steps + 1):
            t = i / steps
            ctrl.mouse_move(sx + (ex - sx) * t, sy + (ey - sy) * t)
            actions.sleep("15ms")
        actions.sleep("100ms")
    finally:
        ctrl.mouse_click(button=0, up=True)


class DwellTimer:
    """Fires when the same target stays under the cursor for a set time.

    `target` is any hashable (a button key, a grid cell). Leaving for less than
    DWELL_GRACE_SECONDS doesn't restart the count.
    """

    def __init__(self):
        self.target = None
        self.progress = 0.0
        self._start = 0.0
        self._last_inside = 0.0

    def clear(self):
        self.target = None
        self.progress = 0.0

    def state(self):
        """What a redraw depends on, for change detection."""
        return (self.target, round(self.progress, 3))

    def update(self, target, now: float, seconds: float) -> bool:
        """Track `target` (None: nothing under the cursor). True when the dwell completes."""
        if target is not None and target == self.target:
            self._last_inside = now
        elif target is not None:
            self.target = target
            self._start = now
            self._last_inside = now
        elif self.target is not None and now - self._last_inside > DWELL_GRACE_SECONDS:
            self.clear()

        if self.target is None:
            return False
        self.progress = min(1.0, (now - self._start) / seconds) if seconds > 0 else 1.0
        if self.progress >= 1.0:
            self.clear()
            return True
        return False


def _contains(rect: Rect, x: float, y: float) -> bool:
    return rect.x <= x <= rect.x + rect.width and rect.y <= y <= rect.y + rect.height


def draw_dwell_fill(c, rect: Rect, progress: float, radius: float = 0.0, color: str = COLOR_PROGRESS):
    """Fill `rect` from the bottom up to show how far a dwell has run."""
    if progress <= 0:
        return
    fill_h = rect.height * progress
    c.save()
    c.clip_rect(Rect(rect.x, rect.y + rect.height - fill_h, rect.width, fill_h))
    paint = c.paint
    paint.style = Paint.Style.FILL
    paint.color = color
    if radius:
        c.draw_round_rect(rect, radius, radius)
    else:
        c.draw_rect(rect)
    c.restore()


class ActionToolbar:
    def __init__(self, default_key=lambda: DEFAULT_ACTION_KEY):
        """`default_key()` names the action selected when nothing else is (read on
        each use, so it follows a setting)."""
        self._default_key = default_key
        # None: the default action.
        self.selected_key: Optional[str] = None
        self.status_text: Optional[str] = None
        self._buttons: list[tuple[MouseAction, Rect]] = []
        self._panel: Optional[Rect] = None
        self._scale = 1.0
        self._hovered: Optional[str] = None
        self._dwell = DwellTimer()

    @property
    def selected(self) -> MouseAction:
        if self.selected_key in ACTIONS_BY_KEY:
            return ACTIONS_BY_KEY[self.selected_key]
        return ACTIONS_BY_KEY.get(self._default_key(), ACTIONS_BY_KEY[DEFAULT_ACTION_KEY])

    def select(self, key: str):
        if key not in ACTIONS_BY_KEY:
            raise ValueError(f"unknown mouse action {key!r}; one of {sorted(ACTIONS_BY_KEY)}")
        self.selected_key = key
        self._clear_hover()

    def reset(self):
        self.selected_key = None
        self.status_text = None
        self._clear_hover()

    def _clear_hover(self):
        self._hovered = None
        self._dwell.clear()

    @property
    def _hover_key(self) -> Optional[str]:
        """The button shown as hovered: under the cursor, or within its grace period."""
        return self._dwell.target or self._hovered

    # --- layout ---

    def layout(self, draw_rect: Rect, screen_rect: Rect):
        """Place the toolbar centered above the zoom view; below it if there's no room
        above; over the top of the screen if there's room for neither."""
        n = len(MOUSE_ACTIONS)
        natural_w = n * BUTTON_W + (n - 1) * BUTTON_GAP + 2 * PANEL_PAD
        natural_h = BUTTON_H + 2 * PANEL_PAD
        self._scale = min(1.0, (screen_rect.width - 2 * PANEL_GAP) / natural_w)
        s = self._scale
        panel_w, panel_h = natural_w * s, natural_h * s
        gap = PANEL_GAP * s

        x = draw_rect.x + (draw_rect.width - panel_w) / 2
        x = max(screen_rect.x + gap, min(x, screen_rect.x + screen_rect.width - gap - panel_w))

        above = draw_rect.y - gap - panel_h
        below = draw_rect.y + draw_rect.height + gap
        if above >= screen_rect.y:
            y = above
        elif below + panel_h <= screen_rect.y + screen_rect.height:
            y = below
        else:
            y = screen_rect.y + gap

        self._panel = Rect(x, y, panel_w, panel_h)
        self._buttons = []
        bx = x + PANEL_PAD * s
        by = y + PANEL_PAD * s
        for action in MOUSE_ACTIONS:
            self._buttons.append((action, Rect(bx, by, BUTTON_W * s, BUTTON_H * s)))
            bx += (BUTTON_W + BUTTON_GAP) * s

    def contains(self, x: float, y: float) -> bool:
        return self._panel is not None and _contains(self._panel, x, y)

    # --- dwell ---

    def update(self, x: float, y: float, now: float, dwell_seconds: float) -> bool:
        """Advance dwell selection for a cursor at (x, y). Returns True if anything
        visible changed (so the canvas needs a redraw)."""
        hovered = None
        for action, rect in self._buttons:
            if _contains(rect, x, y):
                hovered = action.key
                break

        before = (self._hovered, self._dwell.state(), self.selected.key)

        self._hovered = hovered
        # The selected button has nothing to dwell for.
        target = hovered if hovered != self.selected.key else None
        if self._dwell.update(target, now, dwell_seconds):
            self.selected_key = hovered

        return before != (self._hovered, self._dwell.state(), self.selected.key)

    # --- drawing ---

    def draw(self, c):
        if self._panel is None:
            return
        s = self._scale
        paint = c.paint
        paint.antialias = True
        if TOOLBAR_TYPEFACE:
            paint.typeface = TOOLBAR_TYPEFACE

        paint.style = Paint.Style.FILL
        paint.color = COLOR_PANEL
        c.draw_round_rect(self._panel, 10 * s, 10 * s)

        for action, rect in self._buttons:
            self._draw_button(c, action, rect)

        if self.status_text:
            paint.style = Paint.Style.FILL
            paint.textsize = 15 * s
            paint.text_align = Paint.TextAlign.CENTER
            paint.color = COLOR_STATUS
            panel = self._panel
            c.draw_text(self.status_text, panel.x + panel.width / 2, panel.y + panel.height + 20 * s)
            paint.text_align = Paint.TextAlign.LEFT

    def _draw_button(self, c, action: MouseAction, rect: Rect):
        s = self._scale
        paint = c.paint
        selected = action.key == self.selected.key
        radius = 7 * s

        paint.style = Paint.Style.FILL
        paint.color = COLOR_SELECTED if selected else COLOR_BUTTON
        c.draw_round_rect(rect, radius, radius)

        if action.key == self._dwell.target:
            draw_dwell_fill(c, rect, self._dwell.progress, radius)

        paint.style = Paint.Style.STROKE
        hovered = action.key == self._hover_key
        paint.stroke_width = (3 if selected or hovered else 1) * s
        paint.color = COLOR_SELECTED_BORDER if selected or hovered else COLOR_BUTTON_BORDER
        c.draw_round_rect(rect, radius, radius)

        fg = COLOR_ICON_ON_SELECTED if selected else COLOR_ICON
        icon_cx = rect.x + rect.width / 2
        icon_top = rect.y + 9 * s
        if action.is_drag:
            self._draw_drag_icon(c, icon_cx, icon_top, fg)
        elif action.is_zoom:
            self._draw_zoom_icon(c, icon_cx, icon_top, fg)
        else:
            self._draw_mouse_icon(c, icon_cx, icon_top, action.button, fg)
            if action.badge:
                paint.style = Paint.Style.FILL
                paint.color = fg
                paint.textsize = BADGE_SIZE * s
                paint.text_align = Paint.TextAlign.LEFT
                c.draw_text(action.badge, icon_cx + 12 * s, icon_top + 14 * s)

        paint.style = Paint.Style.FILL
        paint.color = fg
        paint.textsize = LABEL_SIZE * s
        paint.text_align = Paint.TextAlign.CENTER
        c.draw_text(action.label, icon_cx, rect.y + rect.height - 9 * s)
        paint.text_align = Paint.TextAlign.LEFT

    def _draw_mouse_icon(self, c, cx: float, top: float, button: int, color: str):
        """A mouse outline with the pressed button filled in."""
        s = self._scale
        w, h = 20 * s, 32 * s
        body = Rect(cx - w / 2, top, w, h)
        split_y = top + h * 0.42
        paint = c.paint

        # Pressed button: clip the body's top-left or top-right quadrant.
        c.save()
        half_x = body.x if button == 0 else cx
        c.clip_rect(Rect(half_x, top, w / 2, split_y - top))
        paint.style = Paint.Style.FILL
        paint.color = color
        c.draw_round_rect(body, w / 2, w / 2)
        c.restore()

        paint.style = Paint.Style.STROKE
        paint.stroke_width = 2 * s
        paint.color = color
        c.draw_round_rect(body, w / 2, w / 2)
        c.draw_line(body.x, split_y, body.x + w, split_y)
        c.draw_line(cx, top, cx, split_y)

    def _draw_zoom_icon(self, c, cx: float, top: float, color: str):
        """A magnifying glass with a plus."""
        s = self._scale
        paint = c.paint
        paint.antialias = True
        paint.style = Paint.Style.STROKE
        paint.color = color
        lens_x, lens_y, r = cx - 4 * s, top + 12 * s, 10 * s
        paint.stroke_width = 2.5 * s
        c.draw_circle(lens_x, lens_y, r)
        paint.stroke_width = 4 * s
        c.draw_line(lens_x + r * 0.72, lens_y + r * 0.72, cx + 13 * s, top + 30 * s)
        paint.stroke_width = 2 * s
        arm = 5 * s
        c.draw_line(lens_x - arm, lens_y, lens_x + arm, lens_y)
        c.draw_line(lens_x, lens_y - arm, lens_x, lens_y + arm)

    def _draw_drag_icon(self, c, cx: float, top: float, color: str):
        """A dotted path from a start dot to an arrowhead."""
        s = self._scale
        paint = c.paint
        x0, y0 = cx - 15 * s, top + 26 * s
        x1, y1 = cx + 14 * s, top + 5 * s

        paint.style = Paint.Style.FILL
        paint.color = color
        c.draw_circle(x0, y0, 4 * s)

        paint.style = Paint.Style.STROKE
        paint.stroke_width = 2.5 * s
        c.draw_line(x0, y0, x1, y1)

        # Arrowhead pointing along (x0, y0) -> (x1, y1).
        dx, dy = x1 - x0, y1 - y0
        length = (dx * dx + dy * dy) ** 0.5
        ux, uy = dx / length, dy / length
        size = 9 * s
        bx, by = x1 - ux * size, y1 - uy * size
        px, py = -uy * size * 0.55, ux * size * 0.55
        path = Path.from_svg(f"M {x1} {y1} L {bx + px} {by + py} L {bx - px} {by - py} Z")
        paint.style = Paint.Style.FILL
        c.draw_path(path)
