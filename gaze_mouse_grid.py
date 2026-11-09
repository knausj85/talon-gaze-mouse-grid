import sys
import time
from datetime import datetime
from pathlib import Path

from talon import Module, actions, canvas, cron, ctrl, scope, screen, settings, ui
from talon.skia import Paint, Rect

from .gaze_grid_action_toolbar import (
    ActionToolbar,
    DwellTimer,
    draw_dwell_fill,
    perform_click,
    perform_drag,
)


def clamp(value, lo, hi):
    """Clamp value between lo and hi."""
    return max(lo, min(hi, value))


def _rect_contains(rect, x, y):
    """Check whether the point (x, y) lies inside rect, edges included."""
    inside_x = rect.x <= x <= rect.x + rect.width
    inside_y = rect.y <= y <= rect.y + rect.height
    return inside_x and inside_y


def snap_to_cell_center(pct, grid_size):
    """Snap a 0-1 percentage to the center of the nearest grid cell."""
    cell = clamp(int(pct * grid_size), 0, grid_size - 1)
    return (cell + 0.5) / grid_size


mod = Module()
mod.mode(
    "gaze_mouse_grid",
    desc="Gaze mouse grid is active — parrot sounds control the grid",
)

mod.setting(
    "gaze_grid_size",
    type=int,
    default=6,
    desc="Grid subdivision count (NxN cells)",
)
mod.setting(
    "gaze_grid_initial_narrow_factor",
    type=int,
    default=2,
    desc="How much the initial gaze-based narrow zooms in (2 = half screen)",
)
mod.setting(
    "gaze_grid_narrow_padding",
    type=int,
    default=1,
    desc="Cells of padding around the selected cell when narrowing (1 = 3x3 region)",
)
mod.setting(
    "gaze_grid_settle_delay",
    type=str,
    default="200ms",
    desc="Delay for eye tracker to update cursor position before reading gaze",
)
mod.setting(
    "gaze_grid_line_color",
    type=str,
    default="ff0000ff",
    desc="Grid line color in RRGGBBAA hex format",
)
mod.setting(
    "gaze_grid_hint_color",
    type=str,
    default="ff000044",
    desc="Hint highlight color in RRGGBBAA hex format",
)
mod.setting(
    "gaze_grid_line_width",
    type=int,
    default=1,
    desc="Grid line stroke width in pixels",
)
mod.setting(
    "gaze_grid_display_ratio",
    type=float,
    default=0.5,
    desc="Fraction of screen used for the drawn grid (0.5 = half screen)",
)
mod.setting(
    "gaze_grid_debug_capture",
    type=bool,
    default=False,
    desc="Save each grid screenshot, a matching full-screen screenshot, and the capture rect to ~/.talon/gaze_grid_debug",
)

mod.setting(
    "gaze_grid_action_dwell",
    type=float,
    default=1.5,
    desc="Seconds to hover over a mouse action button above the grid to select it",
)
mod.setting(
    "gaze_grid_cell_dwell_enabled",
    type=bool,
    default=False,
    desc="Perform the selected mouse action by hovering over a grid cell",
)
mod.setting(
    "gaze_grid_cell_dwell",
    type=float,
    default=1.5,
    desc="Seconds to hover over a grid cell to perform the selected mouse action",
)

mod.setting(
    "gaze_grid_cell_dwell_radius",
    type=int,
    default=25,
    desc="Pixels the cursor may drift and still count as still for a grid cell dwell",
)

mod.setting(
    "gaze_grid_default_action",
    type=str,
    default="left",
    desc="Toolbar action selected when the grid opens and after each click: "
    "zoom, left, right, double, triple, cmd (macOS), shift, ctrl or drag",
)

# How often dwell selection checks the cursor.
DWELL_TICK = "30ms"
CELL_DWELL_FILL_COLOR = "4696ff66"
CELL_DWELL_BORDER_COLOR = "c8e0ffff"
DRAG_PENDING_TEXT = "Drag: pick the drop point, then click"
DRAG_START_COLOR = "00e0ffff"
# The marker left at a pending drag's start while the grid is closed.
DRAG_INDICATOR_RADIUS = 7
DRAG_INDICATOR_COLOR = "00e0ffcc"
DRAG_INDICATOR_OUTLINE = "000000aa"

DEBUG_DIR = Path.home() / ".talon" / "gaze_grid_debug"
_debug_capture_count = 0


def _is_debug_capture_enabled() -> bool:
    return settings.get("user.gaze_grid_debug_capture")


def _next_debug_prefix() -> Path:
    global _debug_capture_count
    _debug_capture_count += 1
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return DEBUG_DIR / f"{timestamp}-{_debug_capture_count:03d}"


def _append_debug_log(line: str):
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DEBUG_DIR / "log.txt"
    with log_path.open("a") as log_file:
        log_file.write(line + "\n")


def _save_debug_capture(zoomed_img, rect, screen_rect):
    """Save the grid screenshot, a full-screen screenshot, and the capture rect."""
    if not _is_debug_capture_enabled():
        return

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    prefix = _next_debug_prefix()
    full_img = screen.capture_rect(screen_rect)
    zoomed_img.write_file(f"{prefix}-zoomed.png")
    full_img.write_file(f"{prefix}-full.png")
    _append_debug_log(
        f"{prefix.name} capture rect={rect} zoomed_img={zoomed_img.width}x{zoomed_img.height} "
        f"screen_rect={screen_rect} full_img={full_img.width}x{full_img.height}"
    )


def _log_debug_click(cursor, draw_rect, rect, target):
    """Record where the cursor was on the grid and where the click was sent."""
    if not _is_debug_capture_enabled():
        return

    _append_debug_log(
        f"click cursor={cursor} draw_rect={draw_rect} rect={rect} target={target}"
    )


_previous_modes: set[str] = set()


def _save_current_modes():
    global _previous_modes
    modes = scope.get("mode", set())

    _previous_modes = set()
    if "command" in modes:
        _previous_modes.add("command")
    if "dictation" in modes:
        _previous_modes.add("dictation")


def _enter_grid_mode():
    actions.mode.disable("command")
    actions.mode.disable("dictation")
    actions.mode.enable("user.gaze_mouse_grid")


def _exit_grid_mode():
    actions.mode.disable("user.gaze_mouse_grid")


def _restore_previous_modes():
    for mode_name in _previous_modes:
        actions.mode.enable(mode_name)


class GazeMouseGrid:
    def __init__(self):
        self.screen = None
        self.rect = None
        self.img = None
        self.mcanvas = None
        self.active = False
        self._draw_rect_cache = None
        self._hint_pos = None
        # After a zoom: the real-screen area of the cell zoomed into. Drawn instead
        # of _hint_pos's cell, since that cell spans several cells of the new grid.
        self._hint_rect = None
        self.was_zoom_mouse_active = False
        self.was_control_mouse_active = False
        self.was_control1_mouse_active = False
        self.toolbar = ActionToolbar(lambda: settings.get("user.gaze_grid_default_action"))
        self.cell_dwell = DwellTimer()
        # Where the cursor came to rest; the cell dwell's target. Moving further
        # than the dwell radius from it starts a new dwell at the new spot.
        self._dwell_anchor = None
        self.drag_start = None
        self._drag_indicator = None
        self._opening = False
        self._dwell_job = None
        self._capturing = False

    def setup(self):
        """Initialize canvas and set target rect to full screen."""
        self.screen = ui.screens()[0]
        self.rect = self.screen.rect.copy()
        self._draw_rect_cache = None
        self.img = None

        if self.mcanvas is not None:
            self.mcanvas.close()

        self.mcanvas = canvas.Canvas.from_screen(self.screen)

        if self.active:
            self.mcanvas.register("draw", self.draw)
            self.mcanvas.freeze()

    def show(self):
        """Save eye tracking state, snap cursor via control1, then show grid."""
        if self.active:
            return

        self._opening = True
        self.sync_drag_indicator()
        self.save_eye_tracking_state()
        actions.tracking.control1_toggle(True)
        cron.after(settings.get("user.gaze_grid_settle_delay"), self.show_after_gaze_settle)

    def show_after_gaze_settle(self):
        """Read gaze position, switch to control mouse, auto-narrow and display."""
        gaze_x, gaze_y = ctrl.mouse_pos()
        self.auto_narrow_to_cursor(gaze_x, gaze_y)
        self._hint_pos = (gaze_x, gaze_y)
        self._hint_rect = None
        actions.tracking.control1_toggle(False)
        self.enable_control_mouse()

        self.mcanvas.register("draw", self.draw)
        self.active = True
        self._opening = False
        self.update_screenshot()
        self.start_dwell_tracking()

    def auto_narrow_to_cursor(self, mx, my):
        """Narrow self.rect centered on (mx, my) by the initial narrow factor."""
        self.narrow_centered_on(mx, my, factor=settings.get("user.gaze_grid_initial_narrow_factor"))

    def close(self):
        """Map cursor position to target region, restore eye tracking, hide grid."""
        if not self.active:
            return

        target = self.cursor_to_target()
        self.stop_dwell_tracking()

        self.mcanvas.unregister("draw", self.draw)
        self.mcanvas.close()
        self.mcanvas = None
        self.img = None
        self.active = False

        self.disable_control_mouse()
        self.restore_eye_tracking_state()
        ctrl.mouse_move(*target)
        return target

    def reset_action(self):
        """Back to the default action, cancelling any pending drag."""
        self.set_drag_start(None)
        self.toolbar.reset()

    def set_drag_start(self, pos):
        """Cache where a drag starts (None: no drag pending)."""
        self.drag_start = pos
        self.toolbar.drag_pending = pos is not None
        self.sync_drag_indicator()

    def sync_drag_indicator(self):
        """Show the pending-drag marker at the drag's start while the grid is closed."""
        want = self.drag_start is not None and not self.active and not self._opening
        if want and self._drag_indicator is None:
            x, y = self.drag_start
            size = DRAG_INDICATOR_RADIUS * 2 + 6
            self._drag_indicator = canvas.Canvas.from_rect(Rect(x - size / 2, y - size / 2, size, size))
            self._drag_indicator.register("draw", self.draw_drag_indicator)
            self._drag_indicator.freeze()
        elif not want and self._drag_indicator is not None:
            self._drag_indicator.close()
            self._drag_indicator = None

    def draw_drag_indicator(self, c):
        cx, cy = c.rect.x + c.rect.width / 2, c.rect.y + c.rect.height / 2
        paint = c.paint
        paint.antialias = True
        paint.style = Paint.Style.STROKE
        paint.stroke_width = 4
        paint.color = DRAG_INDICATOR_OUTLINE
        c.draw_circle(cx, cy, DRAG_INDICATOR_RADIUS)
        paint.stroke_width = 2
        paint.color = DRAG_INDICATOR_COLOR
        c.draw_circle(cx, cy, DRAG_INDICATOR_RADIUS)

    def start_dwell_tracking(self):
        self.stop_dwell_tracking()
        self.reset_cell_dwell()
        self._dwell_job = cron.interval(DWELL_TICK, self.update_dwell)

    def stop_dwell_tracking(self):
        self.reset_cell_dwell()
        if self._dwell_job is not None:
            cron.cancel(self._dwell_job)
            self._dwell_job = None

    def update_dwell(self):
        """Advance toolbar and grid cell dwells; redraw when their look changes."""
        if not self.active or self.mcanvas is None or self._capturing:
            return
        mx, my = ctrl.mouse_pos()
        now = time.perf_counter()
        changed = self.toolbar.update(mx, my, now, settings.get("user.gaze_grid_action_dwell"))

        fired = False
        if settings.get("user.gaze_grid_cell_dwell_enabled"):
            before = self.cell_dwell.state()
            fired = self.cell_dwell.update(
                self.update_dwell_anchor(mx, my), now, settings.get("user.gaze_grid_cell_dwell")
            )
            changed = changed or before != self.cell_dwell.state()
        elif self.cell_dwell.target is not None:
            self.reset_cell_dwell()
            changed = True

        if fired:
            # Outside this cron callback, which the click's close() cancels. Tracking
            # keeps running in case the click is refused (cursor moved off the grid).
            cron.after("0ms", actions.user.gaze_grid_click)
        if changed or fired:
            self.mcanvas.freeze()

    def reset_cell_dwell(self):
        self.cell_dwell.clear()
        self._dwell_anchor = None

    def update_dwell_anchor(self, x, y):
        """The point the cursor is resting at, or None off the grid.

        The anchor only moves when the cursor leaves the dwell radius around it, so
        a new anchor (a new dwell target) means the cursor moved and the count restarts.
        """
        if self.cell_at(x, y) is None:
            self._dwell_anchor = None
            return None
        radius = settings.get("user.gaze_grid_cell_dwell_radius")
        anchor = self._dwell_anchor
        if anchor is None or (x - anchor[0]) ** 2 + (y - anchor[1]) ** 2 > radius**2:
            self._dwell_anchor = (x, y)
        return self._dwell_anchor

    def cell_at(self, x, y):
        """(col, row) of the drawn grid cell at (x, y), or None off the grid."""
        draw_rect = self.calc_draw_rect()
        if not _rect_contains(draw_rect, x, y):
            return None
        grid_size = settings.get("user.gaze_grid_size")
        col = clamp(int((x - draw_rect.x) / draw_rect.width * grid_size), 0, grid_size - 1)
        row = clamp(int((y - draw_rect.y) / draw_rect.height * grid_size), 0, grid_size - 1)
        return (col, row)

    def is_cursor_on_toolbar(self):
        if not self.active:
            return False
        return self.toolbar.contains(*ctrl.mouse_pos())

    def cursor_to_target(self):
        """Map the cursor's exact position on the drawn grid to the real target region."""
        draw_rect = self.calc_draw_rect()
        mx, my = ctrl.mouse_pos()

        pct_x = clamp((mx - draw_rect.x) / draw_rect.width, 0, 1)
        pct_y = clamp((my - draw_rect.y) / draw_rect.height, 0, 1)

        target = (
            self.rect.x + pct_x * self.rect.width,
            self.rect.y + pct_y * self.rect.height,
        )
        _log_debug_click((mx, my), draw_rect, self.rect, target)
        return target

    def is_cursor_off_grid(self):
        """Report whether the cursor is outside the drawn grid while the grid is open."""
        if not self.active:
            return False

        mx, my = ctrl.mouse_pos()
        return not _rect_contains(self.calc_draw_rect(), mx, my)

    def draw(self, c):
        """Draw the centered grid with screenshot background and hint highlight."""
        draw_rect = self.calc_draw_rect()

        self.draw_screenshot(c, draw_rect)
        self.draw_hint_highlight(c, draw_rect)
        self.draw_grid_lines(c, draw_rect)
        self.draw_cell_dwell(c, draw_rect)
        self.draw_drag_start(c, draw_rect)

        self.toolbar.status_text = DRAG_PENDING_TEXT if self.drag_start else None
        self.toolbar.layout(draw_rect, self.screen.rect)
        self.toolbar.draw(c)

    def draw_cell_dwell(self, c, draw_rect):
        """Fill the cell under the cursor from the bottom up as its dwell runs."""
        anchor = self.cell_dwell.target
        if anchor is None:
            return
        cell = self.cell_at(*anchor)
        if cell is None:
            return
        col, row = cell
        grid_size = settings.get("user.gaze_grid_size")
        cell_w = draw_rect.width / grid_size
        cell_h = draw_rect.height / grid_size
        cell_rect = Rect(draw_rect.x + col * cell_w, draw_rect.y + row * cell_h, cell_w, cell_h)

        draw_dwell_fill(c, cell_rect, self.cell_dwell.progress, color=CELL_DWELL_FILL_COLOR)
        paint = c.paint
        paint.style = Paint.Style.STROKE
        paint.stroke_width = 3
        paint.color = CELL_DWELL_BORDER_COLOR
        c.draw_rect(cell_rect)

    def draw_drag_start(self, c, draw_rect):
        """Mark where a pending drag starts, if it's inside the zoomed region."""
        if self.drag_start is None:
            return
        sx, sy = self.drag_start
        if not _rect_contains(self.rect, sx, sy):
            return
        x = draw_rect.x + (sx - self.rect.x) / self.rect.width * draw_rect.width
        y = draw_rect.y + (sy - self.rect.y) / self.rect.height * draw_rect.height
        paint = c.paint
        paint.antialias = True
        paint.color = DRAG_START_COLOR
        paint.style = Paint.Style.STROKE
        paint.stroke_width = 3
        c.draw_circle(x, y, 10)
        paint.style = Paint.Style.FILL
        c.draw_circle(x, y, 3)

    def calc_draw_rect(self):
        """Compute centered draw rect scaled by the display ratio setting."""
        if self._draw_rect_cache is not None:
            return self._draw_rect_cache

        sw = self.screen.width
        sh = self.screen.height
        ratio = settings.get("user.gaze_grid_display_ratio")

        aspect = self.rect.width / self.rect.height

        if aspect >= 1:
            w = sw * ratio
            h = w / aspect
        else:
            h = sh * ratio
            w = h * aspect

        x = self.screen.x + (sw - w) / 2
        y = self.screen.y + (sh - h) / 2

        self._draw_rect_cache = Rect(x, y, w, h)
        return self._draw_rect_cache

    def draw_screenshot(self, c, draw_rect):
        """Draw captured screenshot as grid background."""
        if not self.img:
            return

        src = Rect(0, 0, self.img.width, self.img.height)
        c.draw_image_rect(self.img, src, draw_rect)

    def draw_hint_highlight(self, c, draw_rect):
        """Highlight the area last zoomed into, or else the grid cell holding the
        gaze position the grid opened at."""
        if self._hint_rect is not None:
            self.draw_hint_rect(c, draw_rect)
            return
        if self._hint_pos is None:
            return

        grid_size = settings.get("user.gaze_grid_size")

        real_x, real_y = self._hint_pos
        pct_x = clamp((real_x - self.rect.x) / self.rect.width, 0, 0.999)
        pct_y = clamp((real_y - self.rect.y) / self.rect.height, 0, 0.999)

        col = int(pct_x * grid_size)
        row = int(pct_y * grid_size)

        cell_w = draw_rect.width / grid_size
        cell_h = draw_rect.height / grid_size
        cell_rect = Rect(
            draw_rect.x + col * cell_w,
            draw_rect.y + row * cell_h,
            cell_w,
            cell_h,
        )

        paint = c.paint
        paint.color = settings.get("user.gaze_grid_hint_color")
        paint.style = Paint.Style.FILL
        c.draw_rect(cell_rect)

    def draw_grid_lines(self, c, draw_rect):
        """Draw grid lines over the draw rect."""
        grid_size = settings.get("user.gaze_grid_size")

        paint = c.paint
        paint.color = settings.get("user.gaze_grid_line_color")
        paint.stroke_width = settings.get("user.gaze_grid_line_width")
        paint.style = Paint.Style.STROKE

        x, y, w, h = draw_rect.x, draw_rect.y, draw_rect.width, draw_rect.height

        for i in range(1, grid_size):
            c.draw_line(x + i * w / grid_size, y, x + i * w / grid_size, y + h)
            c.draw_line(x, y + i * h / grid_size, x + w, y + i * h / grid_size)

    def draw_hint_rect(self, c, draw_rect):
        """Fill the drawn area showing _hint_rect (real-screen coordinates)."""
        hint = self._hint_rect
        scale_x = draw_rect.width / self.rect.width
        scale_y = draw_rect.height / self.rect.height
        left = max(hint.x, self.rect.x)
        top = max(hint.y, self.rect.y)
        right = min(hint.x + hint.width, self.rect.x + self.rect.width)
        bottom = min(hint.y + hint.height, self.rect.y + self.rect.height)
        if right <= left or bottom <= top:
            return

        paint = c.paint
        paint.color = settings.get("user.gaze_grid_hint_color")
        paint.style = Paint.Style.FILL
        c.draw_rect(Rect(
            draw_rect.x + (left - self.rect.x) * scale_x,
            draw_rect.y + (top - self.rect.y) * scale_y,
            (right - left) * scale_x,
            (bottom - top) * scale_y,
        ))

    def narrow_at_cursor(self):
        """Narrow to the selected cell plus surrounding padding cells."""
        if not self.active:
            return

        draw_rect = self.calc_draw_rect()
        mx, my = ctrl.mouse_pos()
        grid_size = settings.get("user.gaze_grid_size")
        padding = settings.get("user.gaze_grid_narrow_padding")

        pct_x = clamp((mx - draw_rect.x) / draw_rect.width, 0, 1)
        pct_y = clamp((my - draw_rect.y) / draw_rect.height, 0, 1)

        col = clamp(int(pct_x * grid_size), 0, grid_size - 1)
        row = clamp(int(pct_y * grid_size), 0, grid_size - 1)

        cell_w = self.rect.width / grid_size
        cell_h = self.rect.height / grid_size

        new_w = (1 + 2 * padding) * cell_w
        new_h = (1 + 2 * padding) * cell_h

        # The cell being zoomed into, to highlight in the new view.
        self._hint_rect = Rect(self.rect.x + col * cell_w, self.rect.y + row * cell_h, cell_w, cell_h)

        if new_w < 1 or new_h < 1:
            target_x = self.rect.x + (col + 0.5) * cell_w
            target_y = self.rect.y + (row + 0.5) * cell_h
            self._recenter_current_zoom(target_x, target_y)
            self._hint_pos = (target_x, target_y)
            self.update_screenshot()
            return

        left = clamp(col - padding, 0, grid_size - (1 + 2 * padding))
        top = clamp(row - padding, 0, grid_size - (1 + 2 * padding))

        new_x = self.rect.x + left * cell_w
        new_y = self.rect.y + top * cell_h

        self._draw_rect_cache = None
        self.rect = Rect(new_x, new_y, new_w, new_h)

        self._hint_pos = (self.rect.x + (col - left + 0.5) * cell_w,
                          self.rect.y + (row - top + 0.5) * cell_h)
        self.update_screenshot()

    def narrow_centered_on(self, x, y, factor):
        """Narrow self.rect by the given factor, centered on (x, y).

        If narrowing would shrink cells below one pixel,
        re-center on (x, y) at the current zoom level instead.
        """
        new_w = self.rect.width / factor
        new_h = self.rect.height / factor

        if new_w < 1 or new_h < 1:
            self._recenter_current_zoom(x, y)
            return

        new_x = clamp(x - new_w / 2, self.rect.x, self.rect.x + self.rect.width - new_w)
        new_y = clamp(y - new_h / 2, self.rect.y, self.rect.y + self.rect.height - new_h)

        self._draw_rect_cache = None
        self.rect = Rect(new_x, new_y, new_w, new_h)

    def _recenter_current_zoom(self, x, y):
        """Re-center the grid on (x, y) at the current zoom level."""
        screen_rect = self.screen.rect
        w = self.rect.width
        h = self.rect.height

        new_x = clamp(x - w / 2, screen_rect.x, screen_rect.x + screen_rect.width - w)
        new_y = clamp(y - h / 2, screen_rect.y, screen_rect.y + screen_rect.height - h)

        self._draw_rect_cache = None
        self.rect = Rect(new_x, new_y, w, h)

    def click_target(self):
        """Close grid (moves mouse to target) and perform the selected mouse action.

        A drag takes two: the first caches the start (the grid closes, leaving a
        marker there); the next, after the user reopens the grid, drops.
        """
        if not self.active:
            return

        action = self.toolbar.selected
        target = self.close()

        if action.is_drag:
            if self.drag_start is None:
                self.toolbar.reset()
                self.set_drag_start(target)
                return
            start = self.drag_start
            self.reset_action()
            perform_drag(start, target)
            return

        self.reset_action()
        perform_click(action)

    def update_screenshot(self):
        """Capture screenshot of target rect for background display."""

        def finish_capture():
            try:
                if self.mcanvas is None:
                    return
                self.img = screen.capture_rect(self.rect)
                _save_debug_capture(self.img, self.rect, self.screen.rect)
                self.mcanvas.freeze()
            finally:
                self._capturing = False

        # The toolbar's redraws would show the canvas again mid-capture.
        self._capturing = True
        self.reset_cell_dwell()
        self.mcanvas.hide()
        cron.after("16ms", finish_capture)

    def save_eye_tracking_state(self):
        """Record which eye tracking modes are currently active."""
        self.was_zoom_mouse_active = actions.tracking.control_zoom_enabled()
        self.was_control_mouse_active = actions.tracking.control_enabled()
        self.was_control1_mouse_active = actions.tracking.control1_enabled()

    def enable_control_mouse(self):
        """Disable zoom mouse if active, then enable control mouse."""
        if self.was_zoom_mouse_active:
            actions.tracking.control_zoom_toggle(False)

        if not actions.tracking.control_enabled():
            actions.tracking.control_toggle(True)

    def disable_control_mouse(self):
        """Turn off control mouse (gaze tracking)."""
        if actions.tracking.control_enabled():
            actions.tracking.control_toggle(False)

    def restore_eye_tracking_state(self):
        """Restore eye tracking modes to what they were before show()."""
        if self.was_control_mouse_active and not actions.tracking.control_enabled():
            actions.tracking.control_toggle(True)

        if self.was_control1_mouse_active and not actions.tracking.control1_enabled():
            actions.tracking.control1_toggle(True)

        if self.was_zoom_mouse_active and not actions.tracking.control_zoom_enabled():
            actions.tracking.control_zoom_toggle(True)

        self.was_zoom_mouse_active = False
        self.was_control_mouse_active = False
        self.was_control1_mouse_active = False


    def shutdown(self):
        """Release canvases and timers (a newer load of this module replaces us)."""
        self.stop_dwell_tracking()
        self.drag_start = None
        self.active = False
        self._opening = False
        self.sync_drag_indicator()
        if self.mcanvas is not None:
            self.mcanvas.close()
            self.mcanvas = None


# Talon reloads this module on edit; the previous instance's canvases would linger.
_previous_grid = getattr(sys, "_gaze_mouse_grid_instance", None)
if _previous_grid is not None:
    try:
        _previous_grid.shutdown()
    except Exception as e:
        print(f"gaze_mouse_grid: shutting down the previous instance failed: {e!r}")

gaze_grid = GazeMouseGrid()
sys._gaze_mouse_grid_instance = gaze_grid


@mod.action_class
class GazeGridActions:
    def gaze_grid_activate():
        """Show the gaze mouse grid"""
        if not gaze_grid.mcanvas:
            gaze_grid.setup()

        _save_current_modes()
        gaze_grid.show()
        _enter_grid_mode()

    def gaze_grid_close():
        """Close the gaze mouse grid"""
        _exit_grid_mode()
        gaze_grid.close()
        gaze_grid.reset_action()
        _restore_previous_modes()

    def gaze_grid_toggle():
        """Toggle the gaze mouse grid open/closed"""
        if gaze_grid.active:
            actions.user.gaze_grid_close()
        else:
            actions.user.gaze_grid_activate()

    def gaze_grid_narrow_at_cursor():
        """Select the grid cell under the cursor"""
        gaze_grid.narrow_at_cursor()

    def gaze_grid_click():
        """Perform the toolbar's selected action at the cursor: zoom into the cell, or
        click at the target location and close the grid"""
        if gaze_grid.is_cursor_on_toolbar():
            return
        if gaze_grid.is_cursor_off_grid():
            actions.app.notify("Cursor is outside the grid")
            return

        if gaze_grid.toolbar.selected.is_zoom:
            gaze_grid.narrow_at_cursor()
            return

        _exit_grid_mode()
        gaze_grid.click_target()
        _restore_previous_modes()

    def gaze_grid_select_action(name: str):
        """Select the action used by the next grid click: zoom, left, right, double,
        triple, cmd (macOS only), shift, ctrl or drag"""
        gaze_grid.toolbar.select(name)
        if gaze_grid.active and gaze_grid.mcanvas is not None:
            gaze_grid.mcanvas.freeze()

