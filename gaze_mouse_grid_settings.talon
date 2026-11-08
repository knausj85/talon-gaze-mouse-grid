# Default settings for gaze mouse grid.
# To override, create your own .talon file with a more specific context
# (e.g., matching your OS) and a settings(): block with your values.
settings():
    # Number of grid subdivisions (NxN). Higher = more precision per narrow,
    # but smaller cells are harder to target with gaze.
    user.gaze_grid_size = 6

    # How much the initial gaze-based narrow zooms in.
    # 2 = start with half the screen; 3 = start with a third, etc.
    user.gaze_grid_initial_narrow_factor = 5

    # Cells of padding around the selected cell when narrowing.
    # 0 = no padding, zoom exactly to the selected cell.
    # 1 = show a 3x3 region (selected cell + 1 neighbor on each side).
    # 2 = show a 5x5 region.
    user.gaze_grid_narrow_padding = 1

    # Delay after enabling control1 (head-snap) before reading cursor position.
    # Increase if your eye tracker needs more time to settle.
    user.gaze_grid_settle_delay = "200ms"

    # Grid line color in RRGGBBAA hex format.
    user.gaze_grid_line_color = "ff0000ff"

    # Hint highlight color in RRGGBBAA hex format.
    # The highlight shows which cell your gaze landed in.
    user.gaze_grid_hint_color = "ff000044"

    # Grid line stroke width in pixels.
    user.gaze_grid_line_width = 1

    # Fraction of screen used for the drawn grid.
    # 0.5 = grid occupies half the screen width/height.
    user.gaze_grid_display_ratio = 0.5

    # Save each grid screenshot, a matching full-screen screenshot, and the
    # capture rect to ~/.talon/gaze_grid_debug for diagnosing click offsets.
    user.gaze_grid_debug_capture = false

    # Toolbar action selected when the grid opens and after each click:
    # zoom, left, right, double, triple, cmd (macOS), shift, ctrl or drag.
    # "zoom" makes the click sound (or a cell dwell) narrow the grid instead.
    user.gaze_grid_default_action = "zoom"

    # Seconds to hover over a mouse action button (above the grid) to select it.
    user.gaze_grid_action_dwell = 1.5

    # Perform the selected mouse action by hovering over a grid cell, instead of
    # (or as well as) the click sound. The cell fills up while you hover.
    user.gaze_grid_cell_dwell_enabled = true 

    # Seconds to hover over a grid cell to perform the selected mouse action.
    user.gaze_grid_cell_dwell = 1.5

    # How far (pixels) the cursor may drift during a grid cell dwell and still
    # count as still. Moving further restarts the count from the new spot.
    # Raise it if eye tracking jitter keeps restarting the dwell.
    user.gaze_grid_cell_dwell_radius = 1
