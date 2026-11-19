# ─────────────────────────────────────────────────────────────────────
#  gfx/dims.py
#  Floating dimension labels and corner dimension-row boxes
# ─────────────────────────────────────────────────────────────────────

import math

from .primitives import (
    gfx_rect_fill, gfx_loop, gfx_text, gfx_lines,
    gfx_dashed_line, gfx_dashed_polyline, gfx_dashed_arc, gfx_text_dims, gfx_text_rotated,
)


# ════════════════════════════════════════════════════════════════════
#  FLOATING LABEL  (appears near the cursor / segment midpoint)
# ════════════════════════════════════════════════════════════════════

def draw_float_dim(cx, cy, text, active=True, locked=False):
    """
    Draw a small AutoCAD-style dynamic-input field centred on (cx, cy).

    Colour coding (matches AutoCAD's in-canvas dimension input):
      locked  → green border  (value is pinned)
      active  → solid flat blue, white text, no separate border ring -
                the classic AutoCAD "typing a value" field
      idle    → dark bg
    """
    char_w = 7.5
    bw     = max(int(len(text) * char_w) + 16, 58)
    bh     = 22
    x      = cx - bw / 2
    y      = cy - bh / 2

    if locked:
        bg = (0.04, 0.22, 0.08, 0.95)
        bc = (0.20, 0.85, 0.20, 1.00)
    elif active:
        bg = (0.10, 0.46, 0.93, 1.00)   # flat AutoCAD field-blue
        bc = bg                         # no visible ring - solid block
    else:
        bg = (0.10, 0.10, 0.10, 0.90)
        bc = (0.38, 0.38, 0.38, 0.90)

    gfx_rect_fill(x, y, bw, bh, bg)
    gfx_loop(
        [(x, y), (x + bw, y), (x + bw, y + bh), (x, y + bh)],
        bc,
        2.0 if locked else (1.0 if not active else 0.0001),
    )
    gfx_text(text, x + 8, y + 5, 12, (1.0, 1.0, 1.0, 1.0))


# ════════════════════════════════════════════════════════════════════
#  CORNER DIM-ROW BOX  (rows of label + value near cursor)
# ════════════════════════════════════════════════════════════════════

_BG_ACTIVE    = (0.97, 0.97, 0.92, 0.98)
_BG_INACTIVE  = (0.10, 0.10, 0.10, 0.90)
_BORDER_ACT   = (0.10, 0.42, 0.95, 1.00)
_BORDER_INACT = (0.38, 0.38, 0.38, 0.90)
_TXT_ACT      = (0.05, 0.05, 0.05, 1.00)
_TXT_INACT    = (0.82, 0.82, 0.82, 1.00)
_TXT_LABEL    = (0.50, 0.50, 0.50, 1.00)


def _draw_dim_row(bx, by, label, value_str, active, locked=False):
    """Draw a single labelled row inside the corner dim box."""
    bg  = _BG_ACTIVE   if active  else _BG_INACTIVE
    bc  = ((0.2, 0.8, 0.2, 1.0) if locked
           else (_BORDER_ACT if active else _BORDER_INACT))
    tc  = _TXT_ACT     if active  else _TXT_INACT
    lc  = (0.30, 0.30, 0.30, 1) if active else _TXT_LABEL
    bw, row_h, px, py, fs = 162, 23, 7, 5, 12

    gfx_rect_fill(bx, by, bw, row_h, bg)
    gfx_loop(
        [(bx, by), (bx + bw, by), (bx + bw, by + row_h), (bx, by + row_h)],
        bc,
        2.0 if (active or locked) else 1.0,
    )
    gfx_text(label,              bx + px,      by + py, fs, lc)
    gfx_text(value_str,          bx + px + 22, by + py, fs, tc)


def draw_dim_rows(context, mx, my, rows):
    """
    Draw a stack of dimension rows near (mx, my), auto-repositioning
    to stay within the viewport bounds.

    *rows* is a list of (label, value_str, active, locked) tuples.
    """
    if not rows:
        return
    VW, VH  = context.region.width, context.region.height
    row_h   = 23
    row_g   = 2
    total_h = len(rows) * row_h + (len(rows) - 1) * row_g
    MARGIN  = 18
    bx      = mx + MARGIN
    by      = my + MARGIN

    if bx + 162 > VW - 4:
        bx = mx - 162 - MARGIN
    if by + total_h > VH - 4:
        by = my - total_h - MARGIN

    for i, (label, val_str, active, locked) in enumerate(reversed(rows)):
        _draw_dim_row(bx, by + i * (row_h + row_g), label, val_str, active, locked)


# ════════════════════════════════════════════════════════════════════
#  CAD-STYLE LINEAR DIMENSION  (AutoCAD/SolidWorks-style measurement)
#
#  Draws:
#    - two extension lines running out from the measured points
#    - a dotted/dashed dimension line, offset to the side, spanning
#      between the extension lines
#    - small endpoint tick/witness marks where the extension lines
#      meet the dimension line
#    - the distance text centred on the dimension line, rotated to
#      stay parallel with it, on a small readable background plate
# ════════════════════════════════════════════════════════════════════

_DIM_LINE_OFFSET = 34    # px - how far the dimension line sits from the
                         # measured segment
_EXT_GAP         = 6     # px - small gap between the measured point and
                         # where its extension line starts (AutoCAD-style)
_EXT_OVERSHOOT   = 6     # px - how far the extension line runs past the
                         # dimension line
_TICK_LEN        = 5     # px - half-length of the witness tick marks


_MIN_DIM_OFFSET = _EXT_GAP + 4   # px - how close the dimension line (and
                                 # with it, both extension lines) can be
                                 # dragged to the measured segment before
                                 # it'd start overlapping it


def min_dim_offset():
    """The smallest usable 'dim_offset' - both compute_len_dimension_line()
    and compute_len_extension_lines() already clamp up to this internally,
    but callers that compute a live offset from cursor position (e.g.
    while placing a new measurement) can use this to keep their own
    preview/state consistent with what will actually be drawn."""
    return _MIN_DIM_OFFSET


def default_dim_offset():
    """The 'dim_offset' every LEN measurement gets until it's been
    customized (dragged, or set explicitly via the CAD Dimension
    sidebar panel) - i.e. what m.get('dim_offset') falls back to."""
    return _DIM_LINE_OFFSET


def compute_len_dimension_line(s1, s2, side=1.0, offset=None):
    """
    Return (d1, d2): the on-screen endpoints of the *offset* dimension
    line for a LEN measurement between screen points *s1*/*s2*, on the
    given *side* (+1/-1) - i.e. the same line draw_measurement_dimension()
    actually renders, not the measured segment itself.

    *offset* is how far (px) the dimension line sits from the measured
    segment - None uses the default (_DIM_LINE_OFFSET); anything closer
    than _MIN_DIM_OFFSET is clamped up to it, matching
    compute_len_extension_lines() below so the dimension line and both
    extension lines it connects to always agree on the same effective
    offset. Passing a custom offset is how dragging a measurement's
    extension lines (or choosing one live at placement time) moves the
    dimension line along with them, since both are driven by this same
    value.

    Shared by drawing and hit-testing so the two can never drift apart:
    the operator's hover/selection logic needs the exact same offset
    line the eye sees, not the underlying edge being measured.

    Returns None if *s1*/*s2* coincide (degenerate segment).
    """
    x1, y1 = s1
    x2, y2 = s2
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-4:
        return None
    ux, uy = dx / length, dy / length
    px, py = -uy * side, ux * side
    off = _DIM_LINE_OFFSET if offset is None else max(_MIN_DIM_OFFSET, offset)
    d1 = (x1 + px * off, y1 + py * off)
    d2 = (x2 + px * off, y2 + py * off)
    return d1, d2


def compute_len_extension_lines(s1, s2, side=1.0, offset=None):
    """
    Return (e1_start, e1_end, e2_start, e2_end): the on-screen
    endpoints of both extension (witness) lines for a LEN measurement,
    given *side* (see compute_len_dimension_line()) and the shared
    *offset* (px, from the measured segment to the dimension line -
    None uses the default _DIM_LINE_OFFSET) that also positions the
    dimension line itself.

    Both extension lines always run out to the same distance past the
    dimension line (offset + _EXT_OVERSHOOT) - there's a single shared
    *offset*, not one per line - so they can't help but stay parallel,
    equal-length, and aligned with each other and with the dimension
    line: dragging one end naturally moves both, because they're both
    just this one offset applied at each measured point.

    Shared by drawing and hit-testing/dragging so the handles a user
    drags always match the extension lines actually on screen.

    Returns None if *s1*/*s2* coincide (degenerate segment).
    """
    x1, y1 = s1
    x2, y2 = s2
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-4:
        return None
    ux, uy = dx / length, dy / length
    px, py = -uy * side, ux * side

    off = _DIM_LINE_OFFSET if offset is None else max(_MIN_DIM_OFFSET, offset)
    ext_len = off + _EXT_OVERSHOOT

    e1_start = (x1 + px * _EXT_GAP,  y1 + py * _EXT_GAP)
    e1_end   = (x1 + px * ext_len,   y1 + py * ext_len)
    e2_start = (x2 + px * _EXT_GAP,  y2 + py * _EXT_GAP)
    e2_end   = (x2 + px * ext_len,   y2 + py * ext_len)
    return e1_start, e1_end, e2_start, e2_end


def draw_measurement_dimension(
    s1, s2, text,
    line_col=(1.0, 0.75, 0.15, 1.0),
    point_fill=(1.0, 0.75, 0.15, 0.30),
    point_outline=(1.0, 0.75, 0.15, 1.0),
    text_col=(1.0, 1.0, 1.0, 1.0),
    text_bg=(0.08, 0.08, 0.08, 0.85),
    side=1.0,
    offset=None,
    text_size=None,
):
    """
    Draw one CAD-style linear-dimension overlay between screen points
    *s1* and *s2*, labeled with *text* (already formatted to the
    scene's length units).

    *side* flips which side of the measured segment the dimension
    line is drawn on (+1 / -1) - callers can keep this stable across
    frames so the dimension doesn't flip while dragging.

    *offset* overrides how far (px) the dimension line - and both
    extension lines together with it - sit from the measured segment;
    None uses the default. This only ever changes where the dimension
    line and extension lines are drawn; it never touches s1/s2
    themselves, so the measured distance is completely unaffected.

    *text_size* overrides the label's font size in px (None uses the
    default, 13) - e.g. from the CAD Dimension sidebar panel's "Text
    Size" field.

    Pure viewport drawing (POST_PIXEL) - never touches scene geometry.
    """
    x1, y1 = s1
    x2, y2 = s2
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-4:
        return

    ux, uy = dx / length, dy / length          # unit along the segment
    px, py = -uy * side, ux * side             # unit perpendicular

    # ── extension (witness) lines: point -> past the dimension line ──
    e1_start, e1_end, e2_start, e2_end = compute_len_extension_lines(
        s1, s2, side, offset
    )

    ext_col = (line_col[0], line_col[1], line_col[2], line_col[3] * 0.55)
    gfx_lines([e1_start, e1_end], ext_col, 1.2)
    gfx_lines([e2_start, e2_end], ext_col, 1.2)

    # ── dimension line itself, dotted/dashed, offset to one side ─────
    d1, d2 = compute_len_dimension_line(s1, s2, side, offset)
    gfx_dashed_line(d1, d2, line_col, dash_len=6, space_len=5, w=1.4)

    # ── witness ticks at both ends of the dimension line (45deg CAD
    #    tick style, small crossing dashes) ───────────────────────────
    tx, ty = ux * _TICK_LEN, uy * _TICK_LEN
    tpx, tpy = px * _TICK_LEN, py * _TICK_LEN
    gfx_lines([(d1[0] - tx - tpx, d1[1] - ty - tpy),
               (d1[0] + tx + tpx, d1[1] + ty + tpy)], line_col, 1.6)
    gfx_lines([(d2[0] - tx - tpx, d2[1] - ty - tpy),
               (d2[0] + tx + tpx, d2[1] + ty + tpy)], line_col, 1.6)

    # ── endpoint markers at the actual measured points ────────────────
    from .markers import draw_vertex_square
    draw_vertex_square(s1, 4, fill_col=point_fill, outline_col=point_outline, outline_w=1.6)
    draw_vertex_square(s2, 4, fill_col=point_fill, outline_col=point_outline, outline_w=1.6)

    # ── direction arrowhead, pointing from s1 -> s2 ────────────────────
    # Makes a measurement's direction visible at a glance (which
    # endpoint is "first" vs "second") instead of only inferable from
    # endpoint order - this is exactly what flipping a selected LEN
    # measurement's direction (Shift, see CAD_OT_Measure) visibly
    # reverses: swapping s1/s2 flips which way this chevron points.
    # Placed a quarter of the way along the line (not the midpoint) so
    # it never collides with the centred distance-text label below.
    _amx, _amy = d1[0] + (d2[0] - d1[0]) * 0.25, d1[1] + (d2[1] - d1[1]) * 0.25
    _alen = 5.0
    _atipx, _atipy = _amx + ux * _alen, _amy + uy * _alen
    _abackx, _abacky = _amx - ux * _alen, _amy - uy * _alen
    _aleftx, _alefty = _abackx + px * _alen, _abacky + py * _alen
    _arightx, _arighty = _abackx - px * _alen, _abacky - py * _alen
    gfx_lines([(_aleftx, _alefty), (_atipx, _atipy)], line_col, 1.8)
    gfx_lines([(_arightx, _arighty), (_atipx, _atipy)], line_col, 1.8)

    # ── distance text, centred on the dimension line, aligned with it ─
    angle = math.atan2(dy, dx)
    # Keep the label upright: never let it render upside-down.
    if angle > math.pi / 2 or angle < -math.pi / 2:
        angle += math.pi

    fs = 13 if text_size is None else max(6, int(text_size))
    tw, th = gfx_text_dims(text, fs)
    mx, my = (d1[0] + d2[0]) / 2, (d1[1] + d2[1]) / 2

    ca, sa = math.cos(angle), math.sin(angle)
    # Centre the text plate on the midpoint, in the line's local frame.
    lx, ly = -tw / 2, -th / 2
    plate_cx = mx + lx * ca - ly * sa
    plate_cy = my + lx * sa + ly * ca

    pad_x, pad_y = 5, 3
    if angle == 0:
        gfx_rect_fill(plate_cx - pad_x, plate_cy - pad_y, tw + pad_x * 2, th + pad_y * 2, text_bg)
    else:
        # Rotated background plate, built as a quad in the line's frame.
        corners_local = [(-pad_x, -pad_y), (tw + pad_x, -pad_y),
                          (tw + pad_x, th + pad_y), (-pad_x, th + pad_y)]
        quad = []
        for cx, cy in corners_local:
            wx = plate_cx + cx * ca - cy * sa
            wy = plate_cy + cx * sa + cy * ca
            quad.append((wx, wy))
        _fill_quad(quad, text_bg)

    gfx_text_rotated(text, plate_cx, plate_cy, fs, text_col, angle)


def _fill_quad(quad, col):
    """Fill an arbitrary (convex) screen-space quad - used for the
    rotated text background plate behind rotated dimension labels."""
    import gpu
    from gpu_extras.batch import batch_for_shader
    from .primitives import _sh
    gpu.state.blend_set('ALPHA')
    sh = _sh()
    sh.bind()
    sh.uniform_float("color", col)
    b = batch_for_shader(sh, 'TRIS', {"pos": [list(p) for p in quad]},
                          indices=[(0, 1, 2), (0, 2, 3)])
    b.draw(sh)
    gpu.state.blend_set('NONE')


# ════════════════════════════════════════════════════════════════════
#  CAD-STYLE AREA MEASUREMENT
#
#  Draws an (optional) dashed outline around the measured shape plus
#  a small centre marker and a background-plated area label centred
#  on the shape - used by the Measure tool's Area sub-mode (press M
#  to switch).
# ════════════════════════════════════════════════════════════════════

def draw_area_measurement(
    center_screen, text, outline_screen=None,
    line_col=(0.35, 0.75, 1.0, 1.0),
    text_col=(1.0, 1.0, 1.0, 1.0),
    text_bg=(0.08, 0.08, 0.08, 0.85),
    dashed=True,
):
    """
    Draw one CAD-style area-measurement overlay.

    *center_screen* is the (x, y) screen point the label is centred
    on (the measured shape's geometric centre). *outline_screen* is an
    optional list of screen-space points describing the shape's
    boundary loop - drawn as a dashed highlight when given (real 3-D
    meshes picked via raycast don't have a simple 2-D outline, so this
    is None for those and only the centre marker + label are shown).

    Pure viewport drawing (POST_PIXEL) - never touches scene geometry.
    """
    if outline_screen and len(outline_screen) >= 2:
        pts = list(outline_screen) + [outline_screen[0]]
        if dashed:
            for a, b in zip(pts, pts[1:]):
                gfx_dashed_line(a, b, line_col, dash_len=6, space_len=5, w=1.4)
        else:
            gfx_loop(pts, line_col, 1.4)

    cx, cy = center_screen

    # small crosshair-style centre marker
    r = 5
    gfx_lines([(cx - r, cy), (cx + r, cy)], line_col, 1.6)
    gfx_lines([(cx, cy - r), (cx, cy + r)], line_col, 1.6)

    fs = 13
    tw, th = gfx_text_dims(text, fs)
    pad_x, pad_y = 6, 4
    plate_x = cx - tw / 2
    plate_y = cy + r + 6   # sit just below the centre marker

    gfx_rect_fill(plate_x - pad_x, plate_y - pad_y, tw + pad_x * 2, th + pad_y * 2, text_bg)
    gfx_text(text, plate_x, plate_y, fs, text_col)


# ════════════════════════════════════════════════════════════════════
#  CAD-STYLE RADIUS MEASUREMENT
#
#  Draws a dashed line from a circle's centre out to a point on its
#  circumference, small markers at both ends, and the radius value
#  centred on the line and rotated to stay parallel with it - used by
#  the Measure tool's Radius sub-mode (press M to cycle to it).
# ════════════════════════════════════════════════════════════════════

def draw_radius_measurement(
    center_screen, edge_screen, text,
    line_col=(0.65, 0.45, 1.0, 1.0),
    point_fill=(0.65, 0.45, 1.0, 0.30),
    point_outline=(0.65, 0.45, 1.0, 1.0),
    text_col=(1.0, 1.0, 1.0, 1.0),
    text_bg=(0.08, 0.08, 0.08, 0.85),
):
    """
    Draw one CAD-style radius-measurement overlay between a circle's
    centre (*center_screen*) and a point on its circumference
    (*edge_screen*), labeled with *text* (already formatted to the
    scene's length units).

    Pure viewport drawing (POST_PIXEL) - never touches scene geometry.
    """
    x1, y1 = center_screen
    x2, y2 = edge_screen
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-4:
        return

    gfx_dashed_line(center_screen, edge_screen, line_col, dash_len=6, space_len=5, w=1.6)

    from .markers import draw_vertex_square
    r = 3.5
    gfx_rect_fill(x1 - r, y1 - r, r * 2, r * 2, point_fill)
    draw_vertex_square(edge_screen, 4, fill_col=point_fill, outline_col=point_outline, outline_w=1.6)

    angle = math.atan2(dy, dx)
    # Keep the label upright: never let it render upside-down.
    if angle > math.pi / 2 or angle < -math.pi / 2:
        angle += math.pi

    fs = 13
    tw, th = gfx_text_dims(text, fs)
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2

    ca, sa = math.cos(angle), math.sin(angle)
    lx, ly = -tw / 2, -th / 2
    plate_cx = mx + lx * ca - ly * sa
    plate_cy = my + lx * sa + ly * ca

    pad_x, pad_y = 5, 3
    if angle == 0:
        gfx_rect_fill(plate_cx - pad_x, plate_cy - pad_y, tw + pad_x * 2, th + pad_y * 2, text_bg)
    else:
        corners_local = [(-pad_x, -pad_y), (tw + pad_x, -pad_y),
                          (tw + pad_x, th + pad_y), (-pad_x, th + pad_y)]
        quad = []
        for cx, cy in corners_local:
            wx = plate_cx + cx * ca - cy * sa
            wy = plate_cy + cx * sa + cy * ca
            quad.append((wx, wy))
        _fill_quad(quad, text_bg)

    gfx_text_rotated(text, plate_cx, plate_cy, fs, text_col, angle)

# ════════════════════════════════════════════════════════════════════
#  CAD-STYLE DIAMETER MEASUREMENT
#
#  Draws a dashed line spanning a circle's full diameter (from one
#  point on its circumference, straight through the centre, to the
#  opposite point), a small marker at the centre and at both ends, and
#  a value centred on the line and rotated to stay parallel with it -
#  used by the Measure tool's Diameter sub-mode (press M to cycle to
#  it). The label shows the diameter measurement (the caller passes
#  in an already-formatted diameter string, i.e. 2 * radius).
# ════════════════════════════════════════════════════════════════════

def draw_diameter_measurement(
    edge1_screen, edge2_screen, text,
    line_col=(1.0, 0.45, 0.60, 1.0),
    point_fill=(1.0, 0.45, 0.60, 0.30),
    point_outline=(1.0, 0.45, 0.60, 1.0),
    text_col=(1.0, 1.0, 1.0, 1.0),
    text_bg=(0.08, 0.08, 0.08, 0.85),
):
    """
    Draw one CAD-style diameter-measurement overlay: a dashed line
    running the full width of a circle, between two points on its
    circumference (*edge1_screen*, *edge2_screen*), passing through
    its centre. *text* (already formatted to the scene's length
    units) is centred on the line.

    Pure viewport drawing (POST_PIXEL) - never touches scene geometry.
    """
    x1, y1 = edge1_screen
    x2, y2 = edge2_screen
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-4:
        return

    gfx_dashed_line(edge1_screen, edge2_screen, line_col, dash_len=6, space_len=5, w=1.6)

    from .markers import draw_vertex_square
    draw_vertex_square(edge1_screen, 4, fill_col=point_fill, outline_col=point_outline, outline_w=1.6)
    draw_vertex_square(edge2_screen, 4, fill_col=point_fill, outline_col=point_outline, outline_w=1.6)

    # small marker at the circle's centre, midway along the line
    mid_x, mid_y = (x1 + x2) / 2, (y1 + y2) / 2
    r = 3.5
    gfx_rect_fill(mid_x - r, mid_y - r, r * 2, r * 2, point_fill)

    angle = math.atan2(dy, dx)
    # Keep the label upright: never let it render upside-down.
    if angle > math.pi / 2 or angle < -math.pi / 2:
        angle += math.pi

    fs = 13
    tw, th = gfx_text_dims(text, fs)

    ca, sa = math.cos(angle), math.sin(angle)
    lx, ly = -tw / 2, -th / 2
    plate_cx = mid_x + lx * ca - ly * sa
    plate_cy = mid_y + lx * sa + ly * ca

    pad_x, pad_y = 5, 3
    if angle == 0:
        gfx_rect_fill(plate_cx - pad_x, plate_cy - pad_y, tw + pad_x * 2, th + pad_y * 2, text_bg)
    else:
        corners_local = [(-pad_x, -pad_y), (tw + pad_x, -pad_y),
                          (tw + pad_x, th + pad_y), (-pad_x, th + pad_y)]
        quad = []
        for cx, cy in corners_local:
            wx = plate_cx + cx * ca - cy * sa
            wy = plate_cy + cx * sa + cy * ca
            quad.append((wx, wy))
        _fill_quad(quad, text_bg)

    gfx_text_rotated(text, plate_cx, plate_cy, fs, text_col, angle)


# ════════════════════════════════════════════════════════════════════
#  CAD-STYLE ARC LENGTH MEASUREMENT
#
#  Draws a dashed line that follows an arc's curve (its tessellated
#  path, projected to screen space), a small marker at each endpoint,
#  and the arc-length value centred along the arc and rotated to stay
#  tangent to it - used by the Measure tool's Arc sub-mode (press M
#  to cycle to it).
# ════════════════════════════════════════════════════════════════════

def _point_at_fraction(path, t):
    """The point on polyline *path* that sits *t* (0..1) of the way
    along its own total length - used to re-derive a label's anchor
    point fresh every frame from a curve-intrinsic fraction, so it
    stays locked to the same spot on the curve as the view changes."""
    seg_lens = []
    total = 0.0
    for a, b in zip(path, path[1:]):
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        seg_lens.append(d)
        total += d
    if total < 1e-6:
        return path[0]

    target = max(0.0, min(1.0, t)) * total
    walked = 0.0
    for (a, b), seg_len in zip(zip(path, path[1:]), seg_lens):
        if walked + seg_len >= target or (a, b) == (path[-2], path[-1]):
            local_t = 0.0 if seg_len < 1e-9 else (target - walked) / seg_len
            return (a[0] + (b[0] - a[0]) * local_t, a[1] + (b[1] - a[1]) * local_t)
        walked += seg_len
    return path[-1]


def draw_arc_length_measurement(
    path_screen, text, closed=False, label_t=None,
    line_col=(0.45, 0.85, 1.0, 1.0),
    point_fill=(0.45, 0.85, 1.0, 0.30),
    point_outline=(0.45, 0.85, 1.0, 1.0),
    text_col=(1.0, 1.0, 1.0, 1.0),
    text_bg=(0.08, 0.08, 0.08, 0.85),
):
    """
    Draw one CAD-style curve-length-measurement overlay: a dashed
    line tracing *path_screen* (a curve's tessellated vertices,
    already projected to screen space, in order along the curve), and
    *text* (already formatted to the scene's length units) drawn
    horizontally, anchored right on the curve itself at *label_t*
    (0..1, the fraction of the way along the curve the user picked
    for it) if given, otherwise defaulting to the curve's topmost
    point.

    *closed* distinguishes an open curve (an Arc - two real
    endpoints, each gets a small square marker) from a closed one (a
    Circle, Ellipse/Oval, or any other closed outline - the dashed
    line wraps all the way around back to its own start, and there
    are no endpoints to mark).

    Pure viewport drawing (POST_PIXEL) - never touches scene geometry.
    """
    if len(path_screen) < 2:
        return

    # For a closed curve, dash (and measure the midpoint along) the
    # full loop back to its own start point - not just the open chain
    # of vertices as stored.
    dash_path = list(path_screen)
    if closed and dash_path[0] != dash_path[-1]:
        dash_path.append(dash_path[0])

    gfx_dashed_polyline(dash_path, line_col, dash_len=6, space_len=5, w=1.6)

    if not closed:
        from .markers import draw_vertex_square
        draw_vertex_square(dash_path[0], 4, fill_col=point_fill, outline_col=point_outline, outline_w=1.6)
        draw_vertex_square(dash_path[-1], 4, fill_col=point_fill, outline_col=point_outline, outline_w=1.6)

    # Label sits right on the curve, at whichever point the user
    # picked for it (label_t); failing that (nothing picked yet, e.g.
    # an un-clicked hover preview), default to the curve's topmost
    # point (screen space) - always the most legible option, and
    # sidesteps needing a length-wise "midpoint" that a closed curve
    # like a circle or ellipse doesn't really have a natural one of.
    if label_t is not None:
        top_x, top_y = _point_at_fraction(dash_path, label_t)
    else:
        top_x, top_y = dash_path[0]
        for x, y in dash_path[1:]:
            if y > top_y:
                top_x, top_y = x, y

    fs = 13
    tw, th = gfx_text_dims(text, fs)
    plate_cx = top_x - tw / 2
    plate_cy = top_y - th / 2

    pad_x, pad_y = 5, 3
    gfx_rect_fill(plate_cx - pad_x, plate_cy - pad_y, tw + pad_x * 2, th + pad_y * 2, text_bg)
    gfx_text_rotated(text, plate_cx, plate_cy, fs, text_col, 0.0)


# ════════════════════════════════════════════════════════════════════
#  CAD-STYLE ANGLE MEASUREMENT
#
#  Draws a small dashed arc sweeping from one line to the other at
#  their apex - the "dashed line" that represents the angle being
#  measured, exactly the same way a straight dashed line represents a
#  radius/diameter and a dashed curve represents an arc's
#  circumference - with the angle value centred in the gap between
#  the two lines. Used by the Measure tool's Angle sub-mode (press M
#  to cycle to it).
# ════════════════════════════════════════════════════════════════════

def _wrap_pi(a):
    while a <= -math.pi:
        a += 2 * math.pi
    while a > math.pi:
        a -= 2 * math.pi
    return a


def compute_angle_arc_geometry(apex_screen, ray1_screen, ray2_screen):
    """
    Return (a1, ang_end, radius) describing the dashed arc that
    draw_angle_measurement() renders at *apex_screen*, sweeping from
    the direction of *ray1_screen* to *ray2_screen* (shorter way
    round) - or None if the rays are degenerate.

    Shared by drawing and hit-testing so hovering/selecting an angle
    measurement tests against the actual visible arc rather than the
    straight rays underneath it.
    """
    ax, ay = apex_screen
    d1x, d1y = ray1_screen[0] - ax, ray1_screen[1] - ay
    d2x, d2y = ray2_screen[0] - ax, ray2_screen[1] - ay
    len1 = math.hypot(d1x, d1y)
    len2 = math.hypot(d2x, d2y)
    if len1 < 1e-4 or len2 < 1e-4:
        return None

    a1 = math.atan2(d1y, d1x)
    a2 = math.atan2(d2y, d2x)
    delta = _wrap_pi(a2 - a1)   # signed, shorter-way-round sweep from a1 to a2
    ang_end = a1 + delta

    # Keep the arc comfortably inside both rays, but not so large it
    # swamps short lines nor so small it's unreadable on long ones.
    radius = max(24.0, min(60.0, min(len1, len2) * 0.4))
    return a1, ang_end, radius


def draw_angle_measurement(
    apex_screen, ray1_screen, ray2_screen, text,
    line_col=(1.0, 0.80, 0.25, 1.0),
    point_fill=(1.0, 0.80, 0.25, 0.30),
    point_outline=(1.0, 0.80, 0.25, 1.0),
    text_col=(1.0, 1.0, 1.0, 1.0),
    text_bg=(0.08, 0.08, 0.08, 0.85),
):
    """
    Draw one CAD-style angle-measurement overlay: a small dashed arc
    at *apex_screen*, sweeping from the direction of *ray1_screen* to
    the direction of *ray2_screen* (whichever way is the shorter,
    already-resolved sweep - the caller passes in the two ray
    endpoints exactly as measured), with *text* (already formatted as
    a "NN.NN°" string) centred in the gap between the two lines.

    Pure viewport drawing (POST_PIXEL) - never touches scene geometry.
    """
    ax, ay = apex_screen
    geom = compute_angle_arc_geometry(apex_screen, ray1_screen, ray2_screen)
    if geom is None:
        return
    a1, ang_end, radius = geom
    delta = ang_end - a1

    gfx_dashed_arc(apex_screen, radius, a1, ang_end, line_col, dash_len=5, space_len=4, w=1.6)

    from .markers import draw_vertex_square
    draw_vertex_square(apex_screen, 3.5, fill_col=point_fill, outline_col=point_outline, outline_w=1.4)

    # Label sits just outside the arc, at its angular midpoint - kept
    # upright (not rotated) since angle labels read most clearly flat.
    mid_ang = a1 + delta / 2.0
    label_r = radius + 14.0
    lx = ax + math.cos(mid_ang) * label_r
    ly = ay + math.sin(mid_ang) * label_r

    fs = 13
    tw, th = gfx_text_dims(text, fs)
    plate_x = lx - tw / 2.0
    plate_y = ly - th / 2.0
    pad_x, pad_y = 5, 3
    gfx_rect_fill(plate_x - pad_x, plate_y - pad_y, tw + pad_x * 2, th + pad_y * 2, text_bg)
    gfx_text_rotated(text, plate_x, plate_y, fs, text_col, 0.0)
