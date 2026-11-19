# ─────────────────────────────────────────────────────────────────────
#  gfx/cursor.py
#  AutoCAD-style cursor components for Sketch Mode:
#    - Pick Box   : small square at the centre of the crosshair
#    - Aperture   : (normally invisible) Object Snap proximity box,
#                   drawable for debugging/tuning only
#    - Grips      : blue post-selection handle squares
#
#  These are intentionally separate, small, dependency-light helpers so
#  they can be combined freely by the Sketch Mode cursor overlay in
#  operators/sketch_mode.py without affecting the existing crosshair /
#  AutoSnap-marker drawing used by the active draw tools.
# ─────────────────────────────────────────────────────────────────────

from .primitives import gfx_loop, gfx_rect_fill
from ..utils.math_utils import world_to_screen


# ════════════════════════════════════════════════════════════════════
#  COLOURS
# ════════════════════════════════════════════════════════════════════

# Pick Box - the small white square shown at the crosshair centre most
# of the time while working (AutoCAD default PICKBOX = 3 px).
_PICKBOX_COL = (1.0, 1.0, 1.0, 0.95)

# Aperture - normally invisible; drawn only when the user enables the
# debug toggle so they can see/tune the Object Snap catch radius.
_APERTURE_COL = (1.0, 0.85, 0.0, 0.35)

# Grips - yellow idle, green selected, white dragging
_GRIP_FILL_COL        = (1.00, 0.85, 0.00, 0.90)   # yellow  — unselected
_GRIP_OUTLINE_COL     = (0.60, 0.40, 0.00, 0.80)   # amber outline
_GRIP_SEL_FILL_COL    = (0.15, 0.90, 0.25, 0.95)   # green   — selected
_GRIP_SEL_OUTLINE_COL = (0.05, 0.50, 0.10, 0.90)   # dark green outline
_GRIP_DRAG_FILL_COL   = (1.00, 1.00, 1.00, 0.95)   # white   — being dragged
_GRIP_DRAG_OUTLINE_COL= (0.70, 0.70, 0.70, 0.90)   # grey outline

# Hard safety cap on how many grips are drawn per frame, regardless of
# how many vertices the current selection actually has. This keeps the
# draw callback bounded even if the user selects a dense mesh.
_MAX_GRIPS = 200


# ════════════════════════════════════════════════════════════════════
#  PICK BOX
# ════════════════════════════════════════════════════════════════════

def draw_pickbox(mx, my, half_size_px, col=_PICKBOX_COL):
    """
    Draw the AutoCAD-style 'pick box': a small square outline centred
    on the cursor position (mx, my).

    *half_size_px* is the distance from the centre to each edge, so the
    box is (2 * half_size_px) pixels wide/tall. The AutoCAD default
    PICKBOX value of 3 corresponds to half_size_px = 3.
    """
    h = max(1, int(half_size_px))
    gfx_loop(
        [
            (mx - h, my - h),
            (mx + h, my - h),
            (mx + h, my + h),
            (mx - h, my + h),
        ],
        col, 1.2,
    )


# ════════════════════════════════════════════════════════════════════
#  APERTURE  (debug visualisation only - normally invisible)
# ════════════════════════════════════════════════════════════════════

def draw_aperture(mx, my, half_size_px, col=_APERTURE_COL):
    """
    Draw the Object Snap 'aperture' box: the (normally invisible) area
    around the cursor within which a snap point will engage the
    AutoSnap marker. Intended purely as a debug/tuning aid, toggled via
    CADSettings.cursor_show_aperture.

    *half_size_px* should match CADSettings.cursor_aperture_px so the
    drawn box reflects the actual snap-catch radius used by
    find_hover_point() in utils/math_utils.py.
    """
    h = max(1, int(half_size_px))
    gfx_loop(
        [
            (mx - h, my - h),
            (mx + h, my - h),
            (mx + h, my + h),
            (mx - h, my + h),
        ],
        col, 1.0,
    )


# ════════════════════════════════════════════════════════════════════
#  GRIPS
# ════════════════════════════════════════════════════════════════════

def draw_grip(pos_2d, half_size_px,
               fill_col=_GRIP_FILL_COL, outline_col=_GRIP_OUTLINE_COL):
    """Draw a single filled+outlined grip square centred at pos_2d."""
    x, y = pos_2d
    h = max(1, int(half_size_px))

    # Filled square (gfx_rect_fill takes the bottom-left corner + size).
    gfx_rect_fill(x - h, y - h, h * 2, h * 2, fill_col)

    # Thin outline so the grip stays visible over bright backgrounds.
    gfx_loop(
        [
            (x - h, y - h),
            (x + h, y - h),
            (x + h, y + h),
            (x - h, y + h),
        ],
        outline_col, 1.0,
    )


def draw_grips_for_selection(context, settings,
                             sel_verts=None, drag_obj=None, drag_verts=None,
                             mx=None, my=None, snap_cache=None, snap_filter=None):
    """
    Draw grip squares that advertise the currently-active Object Snap
    type(s), on top of every selected mesh object.

    Colour coding (vertex grips only):
      white  — vertex is part of an active drag
      green  — vertex is selected (in sel_verts)
      yellow — vertex is unselected (default grip)

    Midpoint/Center candidate squares (drawn from *snap_cache*) are
    always plain yellow, since edges/faces have no "selected" grip
    state of their own.

    Parameters
    ----------
    sel_verts   : dict {obj.name: set(vi)} — currently selected verts
    drag_obj    : the object whose verts are being dragged (or None)
    drag_verts  : set of vertex indices being dragged (or empty set)
    mx, my      : current crosshair position in region space. Only used
                  when CADSettings.cursor_grip_proximity_only is enabled,
                  in which case a grip is only drawn once the crosshair
                  is within cursor_grip_reveal_px of it - vertices that
                  are part of an active drag are always shown regardless
                  of distance, so a grip never disappears mid-drag.
    snap_cache  : the dict built by utils.math_utils.build_snap_cache(),
                  used to source MIDPOINT / CENTER candidate points.
                  Pass None to skip those buckets entirely.
    snap_filter : tuple of snap-type strings (e.g. ('MIDPOINT',)) that
                  restricts which candidate squares are shown, mirroring
                  the active Alt-menu Object Snap selection. None means
                  unrestricted, which falls back to the original
                  vertex-only behaviour.
    """
    half_px      = max(1, int(getattr(settings, 'cursor_grip_px', 4)))
    sel_verts    = sel_verts  or {}
    drag_verts   = drag_verts or set()
    drawn        = 0

    proximity_only = bool(getattr(settings, 'cursor_grip_proximity_only', True))
    reveal_px      = max(1, int(getattr(settings, 'cursor_grip_reveal_px', 40)))
    reveal_sq      = reveal_px * reveal_px
    have_cursor    = (mx is not None and my is not None)

    # Which candidate buckets are currently allowed. None = show every
    # bucket this function knows about (legacy / unrestricted behaviour).
    show_vertex   = snap_filter is None or ('VERTEX' in snap_filter or 'ENDPOINT' in snap_filter)
    show_midpoint = snap_filter is not None and 'MIDPOINT' in snap_filter
    show_center   = snap_filter is not None and 'CENTER'   in snap_filter

    def _in_reveal_range(p2, force_show):
        if force_show or not (proximity_only and have_cursor):
            return True
        dx = p2[0] - mx
        dy = p2[1] - my
        return (dx * dx + dy * dy) <= reveal_sq

    # ── VERTEX / ENDPOINT grips permanently disabled ──────────────────
    if False:
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            mw      = obj.matrix_world
            obj_sel = sel_verts.get(obj.name, set())
            is_drag_obj = (drag_obj is not None and obj.name == drag_obj.name)

            for vi, v in enumerate(obj.data.vertices):
                if drawn >= _MAX_GRIPS:
                    return
                p2 = world_to_screen(context, mw @ v.co)
                if p2 is None:
                    continue

                is_dragging = is_drag_obj and vi in drag_verts

                # Proximity gating: skip grips that are far from the
                # crosshair, unless this vertex is currently being
                # dragged (it should stay visible through the drag) or
                # already grip-selected (an active editing handle, not
                # merely an Object Snap candidate).
                if not _in_reveal_range(p2, force_show=is_dragging or vi in obj_sel):
                    continue

                if is_dragging:
                    draw_grip(p2, half_px + 2,
                              _GRIP_DRAG_FILL_COL, _GRIP_DRAG_OUTLINE_COL)
                elif vi in obj_sel:
                    draw_grip(p2, half_px + 1,
                              _GRIP_SEL_FILL_COL, _GRIP_SEL_OUTLINE_COL)
                else:
                    draw_grip(p2, half_px)

                drawn += 1

    # ── MIDPOINT / CENTER candidate squares, sourced from snap_cache ──
    if snap_cache and (show_midpoint or show_center):
        buckets = []
        if show_midpoint:
            buckets.append(snap_cache.get('MIDPOINT') or [])
        if show_center:
            buckets.append(snap_cache.get('CENTER') or [])

        for bucket in buckets:
            for world_pt in bucket:
                if drawn >= _MAX_GRIPS:
                    return
                p2 = world_to_screen(context, world_pt)
                if p2 is None:
                    continue
                if not _in_reveal_range(p2, force_show=False):
                    continue
                draw_grip(p2, half_px)
                drawn += 1
