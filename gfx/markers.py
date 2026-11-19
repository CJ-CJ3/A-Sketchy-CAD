# ─────────────────────────────────────────────────────────────────────
#  gfx/markers.py
#  Snap markers, crosshair, axis guide overlays
# ─────────────────────────────────────────────────────────────────────

import math
from .primitives import (
    gfx_lines, gfx_loop, gfx_circle, gfx_dot, gfx_rect_fill,
    gfx_dashed_line,
)

# ── AutoSnap marker colour ──────────────────────────────────────────
# Per the AutoCAD-style spec, the "AutoSnap Marker" is the green symbol
# shown when the cursor engages an Object Snap point. Its shape changes
# with the snap type (Endpoint -> square, Midpoint -> triangle, ...),
# but the colour stays a consistent green.
_AUTOSNAP_GREEN = (1.00, 0.85, 0.00, 1.0)  # kept the name for now to avoid touching every call site; colour is yellow per request

# Snap indicator red — used for the always-visible snap target highlight
# so it reads clearly against geometry without clashing with the green
# AutoSnap marker used on draw-tool snap points.
_SNAP_RED       = (1.00, 0.18, 0.18, 1.0)
_SNAP_YELLOW    = (1.00, 0.85, 0.00, 1.0)
_SNAP_RED_FILL  = (1.00, 0.18, 0.18, 0.18)

# ── snap colours keyed by snap type ─────────────────────────────────
_SNAP_COL = {
    'VERTEX':    _AUTOSNAP_GREEN,       # generic vertex -> green square
    'ENDPOINT':  _AUTOSNAP_GREEN,       # Endpoint -> green square (was missing - fell through to a white/grey dot)
    'MIDPOINT':  _AUTOSNAP_GREEN,       # Midpoint -> green triangle
    'CENTER':    _AUTOSNAP_GREEN,       # Center   -> green circle
    'GRID':      (0.30, 0.85, 1.00, 0.7),
    'FREE':      (0.55, 0.55, 0.55, 0.7),
    'TRACKING':  _AUTOSNAP_GREEN,       # OTRACK inferred point -> green square, same as Endpoint
}

def draw_vertex_square(pos_2d, half_size_px, fill_col=None, outline_col=None, outline_w=1.6):
    """Draw a CAD-style square vertex marker centered on pos_2d."""
    x, y = pos_2d
    h = max(1, int(half_size_px))
    if fill_col is not None:
        gfx_rect_fill(x - h, y - h, h * 2, h * 2, fill_col)
    gfx_loop(
        [(x - h, y - h), (x + h, y - h),
         (x + h, y + h), (x - h, y + h)],
        outline_col if outline_col is not None else _AUTOSNAP_GREEN,
        outline_w,
    )



def gfx_snap_marker(pos_2d, snap_type: str):
    """
    Draw the appropriate AutoSnap marker icon at *pos_2d*.

      VERTEX   (Endpoint) → green square    (per spec example)
      MIDPOINT            → green triangle  (per spec example)
      CENTER              → green circle + cross
      GRID                → small cyan dot  (not an Object Snap marker)
      FREE                → small grey dot  (not an Object Snap marker)
    """
    col  = _SNAP_COL.get(snap_type, (1, 1, 1, 1))
    x, y = pos_2d

    if snap_type in ('VERTEX', 'ENDPOINT', 'TRACKING'):
        # Endpoint, and OTRACK's inferred intersection, both get the
        # same filled-square marker - an inferred point should look
        # and behave exactly like a real endpoint snap once the
        # cursor is close enough to it.
        draw_vertex_square(
            pos_2d, 7,
            fill_col=(col[0], col[1], col[2], 0.18),
            outline_col=col,
            outline_w=1.8,
        )

    elif snap_type == 'MIDPOINT':
        # Midpoint -> upward-pointing triangle
        s = 8
        gfx_loop(
            [(x, y + s), (x + s, y - s * 0.6), (x - s, y - s * 0.6)],
            col, 1.8,
        )

    elif snap_type == 'CENTER':
        gfx_circle(pos_2d, 7, col, segs=20, w=1.8)
        t = 4
        gfx_lines([(x - t, y), (x + t, y)], col, 1.2)
        gfx_lines([(x, y - t), (x, y + t)], col, 1.2)

    elif snap_type == 'GRID':
        gfx_dot(pos_2d, col, 4.0)

    else:   # FREE
        gfx_dot(pos_2d, col, 3.5)


def draw_highlight_marker(pos_2d, snap_type: str):
    """
    Wider-radius early-feedback marker: the same type-specific glyph
    set used by the real, close-range indicator (_draw_snap_shape,
    also used by draw_snap_indicator) - so every OSNAP type shows its
    own correctly-identifying icon (square/triangle/diamond/etc.) as
    the cursor approaches, not just the handful gfx_snap_marker knows
    about. This only ever affects what's drawn; it has no bearing on
    whether the cursor has actually engaged the snap.
    """
    x, y  = pos_2d
    col   = _AUTOSNAP_GREEN
    white = (1.00, 1.00, 1.00, 1.00)
    _draw_snap_shape(x, y, snap_type, col, white, scale=1.0)


def draw_snap_indicator(pos_2d, snap_mode: str, hover_t: float = 0.0, mx: float = 0.0, my: float = 0.0):
    """
    Render the CAD snap indicator stack at *pos_2d*.

    Signals (back → front):
      1. Geometry icon  – red outline icon whose shape encodes the snap type
      2. Cursor morph   – tiny satellite icon drawn beside the cursor (mx, my)
      3. Tooltip label  – snap-type name fades in after 0.3 s hover
    """
    import blf
    x, y  = pos_2d
    col   = _AUTOSNAP_GREEN
    white = (1.00, 1.00, 1.00, 1.00)

    # ── 1. Geometry icon (drawn directly on the snap point) ───────────
    _draw_snap_shape(x, y, snap_mode, col, white, scale=1.0)

    # ── 2. Cursor satellite (tiny icon next to the actual cursor) ─────
    if mx != 0.0 or my != 0.0:
        sx, sy = mx + 12, my + 12
        _draw_snap_shape(sx, sy, snap_mode, col, white, scale=0.55)

    # ── 4. Tooltip (fades in after 0.3 s) ────────────────────────────
    tt_alpha = min(max((hover_t - 0.30) / 0.15, 0.0), 1.0)
    if tt_alpha > 0.01:
        label = _SNAP_LABELS.get(snap_mode, snap_mode.title())
        fid   = 0
        try:
            blf.size(fid, 10, 72)
        except Exception:
            blf.size(fid, 10)
        # Small dark pill behind text
        tw, th = blf.dimensions(fid, label)
        px, py = x + 12, y + 12
        gfx_rect_fill(px - 3, py - 2, tw + 6, th + 4,
                      (0.0, 0.0, 0.0, 0.60 * tt_alpha))
        blf.color(fid, 1.0, 1.0, 1.0, tt_alpha)
        blf.position(fid, px, py, 0)
        blf.draw(fid, label)


# Human-readable labels for the tooltip
_SNAP_LABELS = {
    'ENDPOINT':      'Endpoint',
    'MIDPOINT':      'Midpoint',
    'VERTEX':        'Vertex',
    'CENTER':        'Center',
    'INTERSECTION':  'Intersection',
    'NEAREST':       'Nearest',
    'GRID':          'Grid',
    'INCREMENT':     'Increment',
    'QUADRANT':      'Quadrant',
    'TANGENT':       'Tangent',
    'PERPENDICULAR': 'Perpendicular',
    'PARALLEL':      'Parallel',
    # legacy
    'ANYTHING':      'Any',
    'VERTEX':        'Vertex',
    'EDGE':          'Edge',
    'FACE':          'Face',
    'POINT':         'Point',
}


def _draw_snap_square(x, y, col, scale=1.0):
    """Draw a simple filled+outlined yellow square centred on (x, y)."""
    S = 6 * scale
    gfx_rect_fill(x - S, y - S, S * 2, S * 2, (*col[:3], 0.35))
    gfx_loop([(x - S, y - S), (x + S, y - S),
              (x + S, y + S), (x - S, y + S)], col, max(1.2, 1.8 * scale))


def _draw_snap_shape(x, y, snap_mode, col, white, scale=1.0):
    """
    Draw the snap-type icon centred on (x, y).

    Design system — all icons share:
      • Thin-stroke base geometry in col (red or icon-idle colour)
      • Filled white target dot/shape at the snap point itself
      • Consistent S=8*scale characteristic radius

    Icon → snap_mode mapping:
      ENDPOINT      □     hollow square (standard AutoCAD marker)
      MIDPOINT      △     hollow triangle (standard AutoCAD marker)
      VERTEX        □●    hollow square + centre dot
      CENTER        ○     hollow circle (standard AutoCAD marker)
      INTERSECTION  ╳●    diagonal cross + centre dot
      NEAREST       —⊙—   line with hollow centre circle
      GRID          #     hash / grid lines
      INCREMENT     ⋮     three evenly-spaced dots
      QUADRANT      ◇     hollow diamond (standard AutoCAD marker)
      TANGENT       ◯—    arc + tangent bar
      PERPENDICULAR ∟     right-angle corner
      PARALLEL      ═     two parallel lines
    """
    S = 8 * scale
    H = S * 0.5    # half of S
    lw = max(1.2, 1.8 * scale)

    if snap_mode == 'ENDPOINT':
        # Hollow square outline only - matches the standard AutoCAD
        # Endpoint marker (no fill, no stub line, no centre dot).
        d = S * 0.7
        gfx_loop([(x - d, y - d), (x + d, y - d),
                  (x + d, y + d), (x - d, y + d)], col, lw)

    elif snap_mode == 'VERTEX':
        # VERTEX: hollow square + centre dot
        gfx_loop([(x - S, y - S), (x + S, y - S),
                   (x + S, y + S), (x - S, y + S)], col, lw)
        # filled white dot
        gfx_dot((x, y), white, S * 1.25)
        gfx_dot((x, y), col,   S * 0.65)

    elif snap_mode == 'MIDPOINT':
        # Hollow triangle outline only - matches the standard AutoCAD
        # Midpoint marker (no stub line, no fill).
        d = S * 0.8
        gfx_loop([(x, y - d), (x + d, y + d * 0.75), (x - d, y + d * 0.75)], col, lw)

    elif snap_mode == 'CENTER':
        # Hollow circle outline only - matches the standard AutoCAD
        # Center marker (no fill, no centre dot).
        gfx_circle((x, y), S * 0.85, col, segs=24, w=lw)

    elif snap_mode == 'INTERSECTION':
        # ╳●  diagonal cross + centre dot
        gfx_lines([(x - S, y - S), (x + S, y + S)], col, lw)
        gfx_lines([(x + S, y - S), (x - S, y + S)], col, lw)
        gfx_dot((x, y), white, S * 1.1)
        gfx_dot((x, y), col,   S * 0.50)

    elif snap_mode == 'NEAREST':
        # —⊙—  line + open circle at nearest point
        gfx_lines([(x - S * 1.1, y), (x + S * 1.1, y)], col, lw)
        gfx_circle((x, y), S * 0.45, col, segs=16, w=lw)
        gfx_dot((x, y), white, S * 0.55)

    elif snap_mode == 'GRID':
        # #  3×3 grid hash
        step = S * 0.6
        for i in (-1, 0, 1):
            gfx_lines([(x + i * step, y - S), (x + i * step, y + S)], col, lw)
            gfx_lines([(x - S, y + i * step), (x + S, y + i * step)], col, lw)
        gfx_dot((x, y), white, S * 0.6)

    elif snap_mode == 'INCREMENT':
        # ⋮  three vertically-spaced dots
        for dy in (-S * 0.7, 0, S * 0.7):
            gfx_dot((x, y + dy), col,   S * 0.8)
            gfx_dot((x, y + dy), white, S * 0.4)

    elif snap_mode == 'QUADRANT':
        # Hollow diamond outline only - matches the standard AutoCAD
        # Quadrant marker (no circle, no tick, no fill).
        d = S * 0.85
        gfx_loop([(x, y - d), (x + d, y), (x, y + d), (x - d, y)], col, lw)

    elif snap_mode == 'TANGENT':
        # ◯—  arc (upper half) + horizontal tangent bar below
        segs = 14
        r    = S * 0.85
        arc_pts = []
        for i in range(segs + 1):
            a = math.radians(i * 180 / segs)
            arc_pts.append((x + r * math.cos(a), y + r * math.sin(a)))
        for i in range(len(arc_pts) - 1):
            gfx_lines([arc_pts[i], arc_pts[i + 1]], col, lw)
        bar_y = y - r * 0.15
        gfx_lines([(x - S * 1.1, bar_y), (x + S * 1.1, bar_y)], col, lw)
        # tangent touch point
        gfx_dot((x, bar_y), white, S * 0.85)
        gfx_dot((x, bar_y), col,   S * 0.40)

    elif snap_mode == 'PERPENDICULAR':
        # ∟  right-angle corner with tick marks
        gfx_lines([(x, y + S), (x, y - H), (x + S, y - H)], col, lw)
        # small square in corner
        sq = H * 0.45
        gfx_loop([(x, y - H), (x + sq, y - H),
                   (x + sq, y - H + sq), (x, y - H + sq)], col, lw * 0.9)
        gfx_dot((x, y + S), white, S * 0.85)

    elif snap_mode == 'PARALLEL':
        # ═  two parallel horizontal lines
        off = S * 0.38
        gfx_lines([(x - S, y + off), (x + S, y + off)], col, lw)
        gfx_lines([(x - S, y - off), (x + S, y - off)], col, lw)
        # arrow indicating direction
        gfx_lines([(x + S * 0.6, y + off + H * 0.5),
                   (x + S * 0.6, y - off - H * 0.5)], col, lw * 0.8)

    else:
        # Fallback: simple cross
        gfx_lines([(x - S, y), (x + S, y)], col, lw)
        gfx_lines([(x, y - S), (x, y + S)], col, lw)
        gfx_dot((x, y), white, S * 0.9)


# ════════════════════════════════════════════════════════════════════
#  CROSSHAIR
# ════════════════════════════════════════════════════════════════════

_AXIS_COL  = {'X': (1.0, 0.25, 0.25, 1),   'Y': (0.25, 1.0, 0.25, 1)}
_AXIS_GLOW = {'X': (1.0, 0.25, 0.25, 0.22), 'Y': (0.25, 1.0, 0.25, 0.22)}
_XHAIR_SQ      = 10
_XHAIR_GAP     = 10

# Fallbacks used if CADSettings.cursor_crosshair_pct is unavailable.
_DEFAULT_CROSSHAIR_PCT = 5     # AutoCAD's modern CURSORSIZE default
_MIN_CROSSHAIR_ARM_PX  = 24    # keep the crosshair visible in tiny viewports


def draw_angle_tooltip(cx, cy, text, active=False, locked=False):
    """Small dark tooltip showing the current polar angle, positioned
    just under the crosshair - AutoCAD's compact "20°" style readout,
    distinct from the blue distance field (draw_float_dim).

    Deliberately does NOT call blf.dimensions() to measure the text -
    draw_float_dim (proven to render reliably) never does either; it
    just estimates the box width from the character count. Calling
    blf.dimensions() here turned out to be exactly what was leaving
    blf's font state broken for the blf.draw() call right after it -
    the box/border would still render, but no glyphs ever appeared,
    even with the call wrapped in a try/except (the exception itself
    wasn't the problem - the call succeeding or failing either way
    left the font in a bad state for what came next).
    """
    char_w = 7.5
    bw     = max(int(len(text) * char_w) + 16, 44)
    bh     = 22
    x, y   = cx - bw / 2, cy - bh - 6

    if locked:
        bg, bc = (0.04, 0.22, 0.08, 0.95), (0.20, 0.85, 0.20, 1.00)
    elif active:
        bg, bc = (0.10, 0.46, 0.93, 1.00), (0.10, 0.46, 0.93, 1.00)
    else:
        bg, bc = (0.16, 0.16, 0.16, 0.92), (0.42, 0.42, 0.42, 0.9)

    gfx_rect_fill(x, y, bw, bh, bg)
    gfx_loop([(x, y), (x + bw, y), (x + bw, y + bh), (x, y + bh)], bc, 1.2)
    gfx_text(text, x + 8, y + 5, 12, (1.0, 1.0, 1.0, 1.0))


def draw_command_tooltip(cx, cy, text):
    """AutoCAD-style command-line tooltip that trails the cursor:
    dark grey pill with the current prompt text, plus a small square
    "options" icon at its right edge (the little dropdown glyph
    AutoCAD shows next to dynamic-input prompts)."""
    import blf
    fid = 0
    try:
        blf.size(fid, 11, 72)
    except Exception:
        blf.size(fid, 11)
    try:
        tw, th = blf.dimensions(fid, text)
    except Exception:
        tw, th = len(text) * 6.5, 11   # fallback estimate if dimensions() fails
    px, py = cx + 14, cy + 6
    bh     = 22
    bw     = tw + 16
    icon_w = 20

    gfx_rect_fill(px, py, bw, bh, (0.20, 0.20, 0.20, 0.92))

    # Text draw isolated from the rest of the tooltip: if it fails for
    # any reason, the box/icon below should still render instead of
    # the whole tooltip silently stopping mid-way (which is what
    # produces an empty-looking box with no visible text).
    try:
        gfx_text(text, px + 8, py + 5, 11, (1.0, 1.0, 1.0, 1.0))
    except Exception as e:
        print(f"[CAD] draw_command_tooltip text draw failed: {e}")

    ix = px + bw
    gfx_rect_fill(ix, py, icon_w, bh, (0.30, 0.30, 0.30, 0.92))
    # tiny down-pointing chevron inside the icon box
    icx, icy = ix + icon_w / 2, py + bh / 2
    gfx_lines(
        [(icx - 4, icy + 2), (icx, icy - 2), (icx + 4, icy + 2)],
        (0.85, 0.85, 0.85, 1.0), 1.4,
    )


def draw_crosshair(context, mx, my, axis_lock=None, show_box=True, gap=True):
    """
    Draw a short, AutoCAD-style crosshair centred on the mouse.

    Each arm's length is CADSettings.cursor_crosshair_pct percent of the
    viewport's largest dimension (mirroring AutoCAD's CURSORSIZE system
    variable), with a minimum of _MIN_CROSSHAIR_ARM_PX so it stays
    visible in small viewports. Colour shifts to red/green when
    *axis_lock* is 'X' or 'Y'.

    *gap*: when False, the arms form one continuous "+" through the
    centre point with no break - matches AutoCAD's dynamic-input
    crosshair (as opposed to Blender's own gapped reticle, which is
    what *gap=True* reproduces for other overlay contexts).
    """
    region = context.region
    W   = region.width
    H   = region.height
    col = _AXIS_COL.get(axis_lock, (1.0, 1.0, 1.0, 0.90))
    G   = _XHAIR_GAP if gap else 0

    settings = getattr(context.scene, 'cad_settings', None)
    pct = getattr(settings, 'cursor_crosshair_pct', _DEFAULT_CROSSHAIR_PCT) \
        if settings is not None else _DEFAULT_CROSSHAIR_PCT

    arm = max(_MIN_CROSSHAIR_ARM_PX, (max(W, H) * pct) / 100.0)

    if G:
        gfx_lines([(mx - arm, my), (mx - G,  my)], col, 1.0)
        gfx_lines([(mx + G,   my), (mx + arm, my)], col, 1.0)
        gfx_lines([(mx, my - arm), (mx, my - G)],  col, 1.0)
        gfx_lines([(mx, my + G),  (mx, my + arm)], col, 1.0)
    else:
        gfx_lines([(mx - arm, my), (mx + arm, my)], col, 1.0)
        gfx_lines([(mx, my - arm), (mx, my + arm)], col, 1.0)

    if show_box:
        S = _XHAIR_SQ
        gfx_loop(
            [(mx - S, my - S), (mx + S, my - S),
             (mx + S, my + S), (mx - S, my + S)],
            col, 1.2,
        )



def draw_axis_guide(context, anchor_3d, axis: str):
    """
    Draw a full-viewport axis line through *anchor_3d* in screen space.
    Used to visualise axis-lock constraints.
    """
    from ..utils.math_utils import world_to_screen
    a2 = world_to_screen(context, anchor_3d)
    if not a2:
        return

    W, H  = context.region.width, context.region.height
    col   = _AXIS_GLOW[axis]
    solid = (*_AXIS_COL[axis][:3], 0.55)

    if axis == 'X':
        gfx_lines([(0, a2[1]), (W, a2[1])], col,   1.0)
        gfx_lines([(0, a2[1]), (W, a2[1])], solid, 1.0)
    else:
        gfx_lines([(a2[0], 0), (a2[0], H)], col,   1.0)
        gfx_lines([(a2[0], 0), (a2[0], H)], solid, 1.0)

def draw_selected_snap_points(context):
    """
    Draw a green square on every currently-selected snap point
    (_st.selection.selected_snap_points), regardless of which tool is
    active. Called from the SELECT-tool idle overlay as well as every
    draw tool's own _draw() callback, so a selection made before
    switching to Line/Circle/Rect/etc. stays visible as a reference
    while drawing new geometry - selecting points is not tied to (and
    is never cleared by) switching tools.
    """
    from .. import state as _st
    from ..utils.math_utils import world_to_screen
    from mathutils import Vector

    pts = _st.selection.selected_snap_points
    if not pts:
        return

    col = (0.15, 0.90, 0.25, 1.0)   # green
    h   = 4
    for key in pts:
        s2 = world_to_screen(context, Vector(key))
        if s2 is None:
            continue
        sx, sy = s2
        gfx_rect_fill(sx - h, sy - h, h * 2, h * 2, (*col[:3], 0.35))
        gfx_loop([(sx - h, sy - h), (sx + h, sy - h),
                   (sx + h, sy + h), (sx - h, sy + h)], col, 2.0)


# OTRACK colour: AutoCAD's tracking guides/markers are green, matching
# the AutoSnap marker colour (_AUTOSNAP_GREEN) rather than the cyan
# used previously - the whole point is that it reads as "the same snap
# system, extended", not a separate cyan overlay.
_OTRACK_GREEN      = (0.20, 1.00, 0.20, 1.0)
_OTRACK_GREEN_DIM  = (0.20, 1.00, 0.20, 0.55)
_OTRACK_ORANGE     = (1.00, 0.10, 0.10, 0.95)  # ray colour while the cursor is actually snapped to it (red, high-contrast)

# How far (in px) a finite guide segment extends past whichever point
# is farthest along it (the acquired point itself, the mouse cursor,
# or a computed intersection) - AutoCAD's dashed tracking line always
# overshoots slightly past what it's aligning with, it never spans the
# full viewport.
_GUIDE_OVERSHOOT_PX = 40.0


def _draw_x_marker(pos_2d, col, half=5, w=1.6):
    """Small diagonal X - AutoCAD's marker for an acquired OTRACK
    point, as opposed to the square/triangle/circle AutoSnap glyphs
    used for live OSNAP hits."""
    x, y = pos_2d
    gfx_lines([(x - half, y - half), (x + half, y + half)], col, w)
    gfx_lines([(x - half, y + half), (x + half, y - half)], col, w)


def _draw_tooltip(text, x, y, alpha=1.0):
    """Small dark pill + white text, offset up-right of (x, y) - same
    visual language as draw_snap_indicator's tooltip, reused here so
    OTRACK feedback matches the rest of the snap HUD."""
    import blf
    fid = 0
    try:
        blf.size(fid, 11, 72)
    except Exception:
        blf.size(fid, 11)
    tw, th = blf.dimensions(fid, text)
    px, py = x + 14, y + 14
    gfx_rect_fill(px - 4, py - 3, tw + 8, th + 6, (0.0, 0.0, 0.0, 0.70 * alpha))
    blf.color(fid, 1.0, 1.0, 1.0, alpha)
    blf.position(fid, px, py, 0)
    blf.draw(fid, text)


def draw_osnap_tracking(context, mx, my):
    """
    Object Snap Tracking (OTRACK) overlay, AutoCAD-styled:

      * each acquired point gets a small green X marker (not a live
        OSNAP glyph - that's drawn separately by gfx_snap_marker/
        draw_snap_indicator whenever the cursor is actually over one)
      * guides are dashed green lines, drawn only as long as needed to
        reach from the acquired point to just past the cursor/
        intersection - never the full viewport
      * a text tooltip appears near the cursor naming what it's
        aligned to ("Extension" for a plain H/V/polar guide,
        "Endpoint: <75°, Extension" when a polar guide and an OSNAP
        point line up at the same spot)
    """
    from .. import state as _st
    from ..utils.math_utils import world_to_screen
    from ..utils.otrack import TrackingManager, TrackingPoint, Guide, IntersectionSolver
    from mathutils import Vector

    tracked = [Vector(key) for key in _st.selection.tracked_points]
    if not tracked:
        return

    settings = getattr(context.scene, 'cad_settings', None)
    mgr = TrackingManager(
        enable_h=getattr(settings, 'otrack_enable_h', True) if settings else True,
        enable_v=getattr(settings, 'otrack_enable_v', True) if settings else True,
        enable_polar=getattr(settings, 'otrack_enable_polar', False) if settings else False,
    )
    mgr.points = [TrackingPoint.from_key(tuple(p)) for p in tracked]

    # Acquired-point X markers.
    screen_pts = []
    for pt in mgr.points:
        s2 = world_to_screen(context, pt.position)
        if s2 is None:
            continue
        screen_pts.append(s2)
        _draw_x_marker(s2, _OTRACK_GREEN)

    if not screen_pts:
        return

    from ..utils.math_utils import mouse_to_3d
    mouse_world = mouse_to_3d(context, mx, my)

    active = mgr.active_guides(mouse_world)

    tol = getattr(settings, 'cursor_aperture_px', 7) if settings else 7
    tol = max(3.0, float(tol))

    # Determine, per active guide, whether the cursor is currently
    # actually snapped to it (its own alignment point is within the
    # snap radius) - computed before drawing so the ray itself can be
    # coloured yellow-orange while engaged, green while just showing
    # the available direction. Matches the same green/orange
    # convention used by the Polar Tracking reference line.
    engaged = []
    for g in active:
        origin_w, dir_w = g.as_ray()
        t = (mouse_world - origin_w).dot(dir_w)
        hit_w = origin_w + dir_w * t
        s2 = world_to_screen(context, hit_w)
        is_engaged = False
        if s2 is not None:
            hx, hy = s2
            is_engaged = math.hypot(hx - mx, hy - my) <= tol
        engaged.append(is_engaged)

    # One dashed ray per active guide: from the acquired point,
    # through the cursor, overshooting slightly - never both axes from
    # the same point, and never a direct point-to-cursor line at an
    # arbitrary angle (that's the AutoCAD behaviour being matched).
    W = context.region.width
    for g, is_engaged in zip(active, engaged):
        s2 = world_to_screen(context, g.origin)
        if s2 is None:
            continue
        sx, sy = s2
        ray_col = _OTRACK_ORANGE if is_engaged else _OTRACK_GREEN_DIM
        if g.kind == Guide.KIND_H:
            # Infinite construction line: spans the full viewport
            # width in both directions, not just point-to-cursor.
            gfx_dashed_line((0, sy), (W, sy), ray_col, dash_len=6, space_len=5, w=1.2)
        elif g.kind == Guide.KIND_V:
            # One-directional ray: starts at the point, extends only
            # toward (and slightly past) the cursor.
            y1 = my + math.copysign(_GUIDE_OVERSHOOT_PX, my - sy) if my != sy else my
            gfx_dashed_line((sx, sy), (sx, y1), ray_col, dash_len=6, space_len=5, w=1.2)

    aligned_labels = []

    # Marker + label at the cursor's own position along whichever
    # single active ray is closest (covers the "only one acquired
    # point, no intersection yet" case).
    for g, is_engaged in zip(active, engaged):
        if not is_engaged:
            continue
        origin_w, dir_w = g.as_ray()
        t = (mouse_world - origin_w).dot(dir_w)
        hit_w = origin_w + dir_w * t
        s2 = world_to_screen(context, hit_w)
        if s2 is None:
            continue
        _draw_x_marker(s2, _OTRACK_ORANGE, half=4, w=1.4)
        aligned_labels.append('Extension')

    # Intersections between active rays (e.g. horizontal ray from
    # point A meeting vertical ray from point B) - the actual "OTRACK
    # inferred point" AutoCAD shows once two tracking rays cross.
    for ipt in IntersectionSolver.all_intersections(active):
        s2 = world_to_screen(context, ipt)
        if s2 is None:
            continue
        ix, iy = s2
        if math.hypot(ix - mx, iy - my) <= tol:
            draw_vertex_square(
                (ix, iy), 7,
                fill_col=(_OTRACK_ORANGE[0], _OTRACK_ORANGE[1], _OTRACK_ORANGE[2], 0.18),
                outline_col=_OTRACK_ORANGE,
                outline_w=1.8,
            )
            aligned_labels.append('Intersection')

    if aligned_labels:
        # Live OSNAP already showing a glyph at the cursor (e.g. an
        # Endpoint circle) combines with the tracking label, matching
        # AutoCAD's "Endpoint: <75°, Extension" combined tooltip.
        osnap_label = None
        hovered_stype = getattr(_st.selection, 'snap_mode', None)
        if getattr(_st.selection, 'hovered_entity', None) or hovered_stype:
            osnap_label = _SNAP_LABELS.get(hovered_stype)

        seen = []
        for l in aligned_labels:
            if l not in seen:
                seen.append(l)
        text = ', '.join(seen)
        if osnap_label:
            text = f'{osnap_label}: {text}'
        _draw_tooltip(text, mx, my)
