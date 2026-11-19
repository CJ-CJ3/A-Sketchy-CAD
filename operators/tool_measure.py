# ─────────────────────────────────────────────────────────────────────
#  operators/tool_measure.py
#  CAD_OT_Measure  –  AutoCAD-style DIST tool.
#
#  Click the first point, drag, click the second point. The distance
#  is shown live while dragging and stays on screen after the second
#  click until a new measurement is started (first click) or the tool
#  is exited (Enter/Esc/Right-click).
#
#  This tool creates NO geometry and touches no existing mesh data -
#  it only reads the same snap cache the draw tools already use, so it
#  can't interfere with Line/Circle/Trim/etc.
# ─────────────────────────────────────────────────────────────────────

import bpy
import bmesh
import json
import math
import time
from mathutils import Vector
from bpy_extras import view3d_utils

from ..utils.math_utils import build_snap_cache, apply_snaps, mouse_to_3d, world_to_screen, point_in_polygon, format_length as _shared_format_length
from ..utils.otrack import SnapAcquisitionManager, update_tracking
from .. import state as _st
from ..utils.cursor_util import (
    update_modal_cursor, restore_modal_cursor, cursor_fx_visible, init_modal_cursor,
    guarded_modal,
    handle_ui_handoff,
    reset_tpanel_to_select,
)
from ..gfx.primitives import gfx_lines, gfx_text, gfx_dot, gfx_rect_fill, gfx_rounded_rect_fill, gfx_rounded_rect_loop, gfx_circle
from ..gfx.markers    import gfx_snap_marker, draw_crosshair, draw_vertex_square, draw_selected_snap_points, draw_osnap_tracking
from ..gfx.dims       import (draw_dim_rows, draw_measurement_dimension, draw_area_measurement,
                               draw_radius_measurement, draw_diameter_measurement,
                               draw_arc_length_measurement, draw_angle_measurement,
                               compute_len_dimension_line, compute_angle_arc_geometry,
                               compute_len_extension_lines, min_dim_offset,
                               _point_at_fraction)
from ..gfx.primitives import gfx_text_dims
from ..ui.measurement_panel import reveal_dimension_panel, hide_dimension_panel

_EDGE_PICK_PX      = 14    # pixel tolerance for double-click "measure this line"
_DOUBLE_CLICK_SEC  = 0.35  # max time between clicks to count as a double-click
_DOUBLE_CLICK_PX   = 8     # max cursor movement between clicks to still count

_MODES        = ('LENGTH', 'AREA', 'RADIUS', 'DIAMETER', 'ARC', 'ANGLE')
_MODE_LABELS  = {'LENGTH': 'Length', 'AREA': 'Area', 'RADIUS': 'Radius', 'DIAMETER': 'Diameter',
                  'ARC': 'Circumference', 'ANGLE': 'Angle'}


def _closest_point_on_segment_2d(p, a, b):
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-9:
        return a
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return (ax + dx * t, ay + dy * t)


def _nearest_t_on_screen_path(path, mx, my):
    """
    Find the point on the polyline *path* (a list of 2-D screen
    points, in order) that's closest to the cursor (mx, my), and
    return that point's fraction *t* (0..1) of the way along the
    polyline's own total screen-space length.

    *t* is a curve-intrinsic parameter rather than a fixed screen
    coordinate, so re-deriving a point from it every frame (via
    _point_at_fraction in gfx/dims.py) keeps a picked label anchored
    to the same spot on the curve even as the view rotates or pans -
    unlike storing the raw click position.
    """
    seg_lens = []
    total = 0.0
    for a, b in zip(path, path[1:]):
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        seg_lens.append(d)
        total += d
    if total < 1e-6:
        return 0.0

    best_d = float('inf')
    best_walked = 0.0
    walked = 0.0
    for (a, b), seg_len in zip(zip(path, path[1:]), seg_lens):
        if seg_len >= 1e-6:
            cx, cy = _closest_point_on_segment_2d((mx, my), a, b)
            d = math.hypot(cx - mx, cy - my)
            if d < best_d:
                best_d = d
                best_walked = walked + math.hypot(cx - a[0], cy - a[1])
        walked += seg_len
    return best_walked / total


def _pick_edge_at_cursor(context, mx, my, snap_cache):
    """Find the nearest real mesh edge (v0, v1, world-space) whose
    on-screen projection passes within _EDGE_PICK_PX of (mx, my).

    Used by double-click "measure this edge": every mesh edge - a
    Line's single segment, one side of a Rectangle, one segment of a
    Polyline, a Circle's polygon-approximation edge, etc. - is a
    candidate, and the *whole* edge (its two endpoints) is what gets
    measured, regardless of where along its length the user clicked.
    """
    best_d, best = float(_EDGE_PICK_PX), None
    for v0, v1 in snap_cache.get('NEAREST_EDGES', []):
        s0 = world_to_screen(context, v0)
        s1 = world_to_screen(context, v1)
        if s0 is None or s1 is None:
            continue
        cx, cy = _closest_point_on_segment_2d((mx, my), s0, s1)
        d = math.hypot(cx - mx, cy - my)
        if d < best_d:
            best_d, best = d, (v0, v1)
    return best


# ════════════════════════════════════════════════════════════════════
#  AREA sub-mode helpers
# ════════════════════════════════════════════════════════════════════

def _object_closed_loops(obj):
    """Return world-space point lists for every closed loop of
    connected edges in *obj* - used to pick/measure the area of a
    face-less CAD sketch outline (Rectangle, Circle, closed Polyline,
    a hand-drawn closed shape, ...). Assumes simple loops (each vertex
    has at most two edge neighbours), which covers every shape this
    addon draws."""
    me = obj.data
    if me is None or len(me.edges) == 0:
        return []

    adj = {}
    for e in me.edges:
        a, b = e.vertices[0], e.vertices[1]
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)

    visited, loops = set(), []
    for e in me.edges:
        key = frozenset((e.vertices[0], e.vertices[1]))
        if key in visited:
            continue
        start = e.vertices[0]
        chain = [start, e.vertices[1]]
        visited.add(key)
        closed = False
        while True:
            last = chain[-1]
            candidates = [v for v in adj.get(last, ()) if frozenset((last, v)) not in visited]
            if not candidates:
                break
            nxt = candidates[0]
            visited.add(frozenset((last, nxt)))
            if nxt == start:
                closed = True
                break
            if nxt in chain:
                break
            chain.append(nxt)
        if closed and len(chain) >= 3:
            loops.append([obj.matrix_world @ me.vertices[i].co for i in chain])
    return loops


def _polygon_area_3d(pts):
    """Area of a planar polygon in 3-D, via Newell's method - works
    regardless of which way the polygon happens to be oriented."""
    n = len(pts)
    if n < 3:
        return 0.0
    nx = ny = nz = 0.0
    for i in range(n):
        v1, v2 = pts[i], pts[(i + 1) % n]
        nx += (v1.y - v2.y) * (v1.z + v2.z)
        ny += (v1.z - v2.z) * (v1.x + v2.x)
        nz += (v1.x - v2.x) * (v1.y + v2.y)
    return 0.5 * math.sqrt(nx * nx + ny * ny + nz * nz)


def _bbox_center(pts):
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    zs = [p.z for p in pts]
    return Vector(((min(xs) + max(xs)) / 2.0,
                   (min(ys) + max(ys)) / 2.0,
                   (min(zs) + max(zs)) / 2.0))


def _mesh_object_area_and_center(obj, depsgraph):
    """Total surface area + bounding-box centre (both world-space) of
    a real faced mesh object - used when the cursor raycasts directly
    onto a solid/filled object rather than a face-less sketch outline."""
    eval_obj = obj.evaluated_get(depsgraph)
    me = eval_obj.to_mesh()
    try:
        bm = bmesh.new()
        bm.from_mesh(me)
        bm.transform(eval_obj.matrix_world)
        area = sum(f.calc_area() for f in bm.faces)
        center = _bbox_center([v.co for v in bm.verts]) if bm.verts else eval_obj.matrix_world.translation.copy()
        bm.free()
    finally:
        eval_obj.to_mesh_clear()
    return area, center


def _pick_area_at_cursor(context, mx, my):
    """
    Find whichever object's area the cursor is over and return
    (area, world_center, outline_world_or_None), or None if nothing
    is under the cursor.

    Two paths, tried in order:
      1. A real raycast - hits solid/filled mesh objects (with faces)
         directly, giving an exact pick even for overlapping geometry.
      2. Face-less CAD sketch outlines (Rectangle/Circle/Polyline/...)
         - these have no faces to raycast against, so instead every
         visible object's closed edge-loop(s) are projected to screen
         space and tested against the cursor with a point-in-polygon
         test (the same test used by the Lasso Select tool). If more
         than one loop contains the cursor (nested/overlapping shapes)
         the smallest one on screen wins, matching how CAD area-pick
         tools usually resolve overlaps.
    """
    region, rv3d = context.region, context.region_data
    if region is None or rv3d is None:
        return None

    origin    = view3d_utils.region_2d_to_origin_3d(region, rv3d, (mx, my))
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, (mx, my))
    depsgraph = context.evaluated_depsgraph_get()

    try:
        success, _loc, _n, _fi, hit_obj, _mat = context.scene.ray_cast(depsgraph, origin, direction)
    except Exception:
        success, hit_obj = False, None

    if success and hit_obj is not None and hit_obj.type == 'MESH':
        area, center = _mesh_object_area_and_center(hit_obj, depsgraph)
        return area, center, None

    best, best_screen_area = None, float('inf')
    for obj in context.visible_objects:
        if obj.type != 'MESH' or obj.data is None or len(obj.data.polygons) > 0:
            continue
        for loop_world in _object_closed_loops(obj):
            screen_pts = [world_to_screen(context, p) for p in loop_world]
            if any(s is None for s in screen_pts):
                continue
            if not point_in_polygon((mx, my), screen_pts):
                continue
            xs = [s[0] for s in screen_pts]
            ys = [s[1] for s in screen_pts]
            screen_area = max(1.0, (max(xs) - min(xs)) * (max(ys) - min(ys)))
            if screen_area < best_screen_area:
                best_screen_area = screen_area
                best = (_polygon_area_3d(loop_world), _bbox_center(loop_world), loop_world)
    return best


# ════════════════════════════════════════════════════════════════════
#  RADIUS sub-mode helpers
# ════════════════════════════════════════════════════════════════════

def _pick_radius_at_cursor(context, mx, my):
    """
    Find the CAD Circle object under the cursor and return
    (center_world, radius_world, edge_point_world), or None if no
    circle is under the cursor.

    Circles made by this addon's Circle tool carry their exact centre
    and radius as custom properties (obj["cad_type"] == "circle",
    obj["cad_params"] = {"center": [...], "radius": ...}), so the
    value reported is exact rather than approximated from the
    polygon-approximation mesh. Picking (which circle, if several
    overlap) reuses the same closed-loop + point-in-polygon test as
    Area mode; the smallest one on screen wins for nested circles.

    *edge_point_world* is the point on the circle's actual
    circumference in the direction of the click, so the radius line
    drawn afterwards points naturally toward wherever the user clicked
    rather than always in a fixed direction.
    """
    best, best_screen_area = None, float('inf')
    for obj in context.visible_objects:
        if obj.type != 'MESH' or obj.get("cad_type") != "circle":
            continue
        try:
            params = json.loads(obj.get("cad_params", "{}"))
            center_local = Vector(params["center"])
            radius_local = float(params["radius"])
        except Exception:
            continue

        for loop_world in _object_closed_loops(obj):
            screen_pts = [world_to_screen(context, p) for p in loop_world]
            if any(s is None for s in screen_pts):
                continue
            if not point_in_polygon((mx, my), screen_pts):
                continue
            xs = [s[0] for s in screen_pts]
            ys = [s[1] for s in screen_pts]
            screen_area = max(1.0, (max(xs) - min(xs)) * (max(ys) - min(ys)))
            if screen_area < best_screen_area:
                center_world = obj.matrix_world @ center_local
                edge_world_ref = obj.matrix_world @ (center_local + Vector((radius_local, 0, 0)))
                radius_world = (edge_world_ref - center_world).length
                best_screen_area = screen_area
                best = (center_world, radius_world)

    if best is None:
        return None
    center_world, radius_world = best

    click_world = mouse_to_3d(context, mx, my)
    if click_world is not None and (click_world - center_world).length > 1e-9:
        direction = (click_world - center_world).normalized()
    else:
        direction = Vector((1.0, 0.0, 0.0))
    edge_point = center_world + direction * radius_world
    return center_world, radius_world, edge_point


def _pick_diameter_at_cursor(context, mx, my):
    """
    Find the CAD Circle object under the cursor and return
    (center_world, radius_world, edge1_world, edge2_world), or None
    if no circle is under the cursor.

    Reuses the same exact-value pick as _pick_radius_at_cursor()
    (circles store their real centre/radius as custom properties), but
    also returns the *opposite* edge point so the caller can draw a
    dashed line spanning the full diameter, straight through the
    centre, in the direction of wherever the user clicked.
    """
    hit = _pick_radius_at_cursor(context, mx, my)
    if hit is None:
        return None
    center_world, radius_world, edge_point = hit
    edge2_world = center_world - (edge_point - center_world)
    return center_world, radius_world, edge_point, edge2_world


# ════════════════════════════════════════════════════════════════════
#  ARC sub-mode helpers
# ════════════════════════════════════════════════════════════════════

def _closed_curve_length(obj, loop_world):
    """
    Circumference of a closed curve's outline.

    Circles get their *exact* analytic circumference (2*pi*r), read
    from the same exact centre/radius custom properties Radius and
    Diameter mode already use - rather than approximated from the
    polygon-approximation mesh. Every other closed shape (an ellipse/
    oval, a closed polyline, ...) has no such simple closed-form
    formula, so its length is the sum of its own tessellated edges -
    the same "trust the mesh that's actually on screen" convention
    already used for open arcs, and just as accurate given how finely
    this addon tessellates curves (64 segments for an ellipse).
    """
    if obj.get("cad_type") == "circle":
        try:
            params = json.loads(obj.get("cad_params", "{}"))
            center_local = Vector(params["center"])
            radius_local = float(params["radius"])
            mw = obj.matrix_world
            center_world = mw @ center_local
            edge_world_ref = mw @ (center_local + Vector((radius_local, 0, 0)))
            radius_world = (edge_world_ref - center_world).length
            return 2.0 * math.pi * radius_world
        except Exception:
            pass  # fall through to the generic mesh-based measurement below

    n = len(loop_world)
    return sum((loop_world[(i + 1) % n] - loop_world[i]).length for i in range(n))


def _pick_curve_at_cursor(context, mx, my):
    """
    Find whichever curved CAD object is under the cursor - a Circle,
    an Ellipse/Oval, an Arc, or any other closed sketch outline - and
    return (path_world, length_world, closed), or None if nothing
    curved is there.

    Closed curves (circles, ellipses, closed polylines, ...) are
    picked the same "anywhere inside counts" way Area/Radius/Diameter
    mode already pick shapes - via _object_closed_loops() + point-in-
    polygon, with the smallest one on screen winning for nested
    shapes - since a closed curve has an interior to click into.
    *closed* comes back True for these, and their length is computed
    by _closed_curve_length() above.

    Open curves (a single Arc) have no interior to click inside, so
    they fall back to the "closest point on any edge, within
    _EDGE_PICK_PX" proximity test used elsewhere in this tool for
    picking a specific line/edge. *closed* comes back False for
    these, and the length is the exact sum of the arc's own
    consecutive-vertex segments (see the old single-purpose version
    of this function, before it grew to cover every curve type, for
    why that beats reconstructing "radius * swept angle").
    """
    # 1) Closed shapes: click anywhere inside, smallest on screen wins.
    best, best_screen_area = None, float('inf')
    for obj in context.visible_objects:
        if obj.type != 'MESH' or obj.data is None or len(obj.data.polygons) > 0:
            continue
        for loop_world in _object_closed_loops(obj):
            screen_pts = [world_to_screen(context, p) for p in loop_world]
            if any(s is None for s in screen_pts):
                continue
            if not point_in_polygon((mx, my), screen_pts):
                continue
            xs = [s[0] for s in screen_pts]
            ys = [s[1] for s in screen_pts]
            screen_area = max(1.0, (max(xs) - min(xs)) * (max(ys) - min(ys)))
            if screen_area < best_screen_area:
                best_screen_area = screen_area
                best = (loop_world, _closed_curve_length(obj, loop_world), True)
    if best is not None:
        return best

    # 2) Open curves (arcs): nearest edge within pixel tolerance.
    best_d, best_obj = float(_EDGE_PICK_PX), None
    for obj in context.visible_objects:
        if obj.type != 'MESH' or obj.get("cad_type") != "arc" or obj.data is None:
            continue
        me = obj.data
        mw = obj.matrix_world
        for e in me.edges:
            v0 = mw @ me.vertices[e.vertices[0]].co
            v1 = mw @ me.vertices[e.vertices[1]].co
            s0 = world_to_screen(context, v0)
            s1 = world_to_screen(context, v1)
            if s0 is None or s1 is None:
                continue
            cx, cy = _closest_point_on_segment_2d((mx, my), s0, s1)
            d = math.hypot(cx - mx, cy - my)
            if d < best_d:
                best_d, best_obj = d, obj

    if best_obj is None:
        return None

    me = best_obj.data
    mw = best_obj.matrix_world
    path_world = [mw @ v.co for v in me.vertices]
    if len(path_world) < 2:
        return None

    arc_length_world = sum(
        (path_world[i + 1] - path_world[i]).length
        for i in range(len(path_world) - 1)
    )
    return path_world, arc_length_world, False


# ════════════════════════════════════════════════════════════════════
#  ANGLE sub-mode helpers
# ════════════════════════════════════════════════════════════════════

def _line_intersect_2d(a0, a1, b0, b1):
    """
    Intersection point of two *infinite* 2-D lines through (a0, a1)
    and (b0, b1), in the XY plane, or None if they're parallel.

    Unlike math_utils._edge_intersect_2d (which only returns a point
    if the two picked *segments* literally cross within their own
    bounds), angle measurement needs the lines' true apex even when
    the two picked segments only share an endpoint, or don't overlap
    in screen space at all - two walls meeting at a corner, for
    instance, where each wall is its own separate segment.
    """
    dx1, dy1 = a1.x - a0.x, a1.y - a0.y
    dx2, dy2 = b1.x - b0.x, b1.y - b0.y
    denom = dx1 * dy2 - dy1 * dx2
    if abs(denom) < 1e-9:
        return None
    t = ((b0.x - a0.x) * dy2 - (b0.y - a0.y) * dx2) / denom
    x = a0.x + t * dx1
    y = a0.y + t * dy1
    z = (a0.z + a1.z + b0.z + b1.z) * 0.25
    return Vector((x, y, z))


def _compute_angle_measurement(edge1, edge2):
    """
    Given two world-space edges (each a (v0, v1) tuple), work out the
    angle actually swept between them as drawn and return everything
    needed to draw + label it:
    (apex_world, ray1_end_world, ray2_end_world, angle_rad), or None
    if the two edges are parallel (no defined intersection/angle).

    The angle is measured between the two rays that radiate out from
    the apex through whichever endpoint of each edge sits farthest
    from it - i.e. the angle the two lines actually form as the user
    drew them, not just whichever of the two supplementary angles
    happens to be <= 90 degrees.
    """
    a0, a1 = edge1
    b0, b1 = edge2
    apex = _line_intersect_2d(a0, a1, b0, b1)
    if apex is None:
        return None

    ray1_end = a0 if (a0 - apex).length >= (a1 - apex).length else a1
    ray2_end = b0 if (b0 - apex).length >= (b1 - apex).length else b1

    d1 = ray1_end - apex
    d2 = ray2_end - apex
    if d1.length < 1e-9 or d2.length < 1e-9:
        return None

    cos_a = max(-1.0, min(1.0, d1.dot(d2) / (d1.length * d2.length)))
    angle_rad = math.acos(cos_a)

    return apex, ray1_end, ray2_end, angle_rad


def _format_length(context, value: float) -> str:
    """Format a distance using the scene's own unit settings (metric,
    imperial, or unitless 'Blender units'), matching how Blender's own
    N-panel / transform readouts display length.

    Delegates to utils.math_utils.format_length so Measure's labels
    use the exact same explicit-unit conversion (km/m/cm/mm/µm/miles/
    ft/in/thou) as every other tool, and update immediately whenever
    the user changes the selected unit in Scene > Units - rather than
    Blender's own adaptive best-fit, which silently ignores that
    dropdown."""
    return _shared_format_length(context, value)


def _format_area(context, value: float) -> str:
    """Format an area using the scene's own unit settings, matching
    _format_length()'s conventions (area scales as length-scale²) and
    always showing a fixed two decimal places."""
    scene = context.scene
    us = scene.unit_settings
    if us.system == 'NONE':
        return f"{value:.2f} u\u00b2"

    from ..utils.math_utils import LENGTH_UNIT_INFO, _fmt_num
    info = LENGTH_UNIT_INFO.get(us.length_unit)
    sq_meters = value * (us.scale_length ** 2)
    if info is None:
        # 'ADAPTIVE' - fall back to Blender's own best-fit formatting.
        try:
            return bpy.utils.units.to_string(
                us.system, 'AREA', sq_meters,
                precision=2, split_unit=us.use_separate,
            )
        except Exception:
            return f"{value:.2f} u\u00b2"

    factor, suffix = info
    return f"{_fmt_num(sq_meters / (factor ** 2))} {suffix}\u00b2"


def _format_angle(context, angle_rad: float) -> str:
    """Format an angle using the scene's own Rotation unit setting
    (Properties panel > Scene > Units), matching this addon's existing
    "NN.NN°" / "NN.NN rad" convention used by Rotate/Copy/Stretch/
    Move/Line/Arc/Array. Delegates to utils.math_utils.format_angle()
    for consistency across all angle readouts, and updates
    automatically whenever the user changes the selected Rotation
    unit."""
    from ..utils.math_utils import format_angle
    return format_angle(context, angle_rad)


def draw_measurements_list(context, measurements, selected_indices=None, hover_index=None):
    """
    Draw a list of finished measurement dicts (the same shape as
    CAD_OT_Measure._measurements entries, tagged by 'kind').

    Factored out of CAD_OT_Measure._draw() so the exact same rendering
    is shared between:
      - the Measure tool's own live overlay (this session's
        measurements, including any not yet "kept"), and
      - CAD_OT_SketchModeManager's always-on overlay, which draws
        state.kept_measurements so "kept" measurements stay visible
        even after the Measure tool itself has been closed.

    *selected_indices*, if given, is a set of indices into
    *measurements* that should be drawn highlighted in green instead
    of their normal per-kind color - used by the Measure tool to show
    which measurement(s) are currently selected (double-click a
    measurement's dimension line/text to select it; Ctrl+A selects
    all). *hover_index*, if given, is a single index that gets the
    same green highlight to signal it's selectable (the cursor is
    currently over its dimension line/arc or its text label) - it
    doesn't have to be selected for this. Measurements the user has
    flagged to "keep" (m['kept'] is True) get a small lock icon
    appended to their label so Keep/Not-Keep state stays visible at a
    glance regardless of selection/hover.

    Wrapped measurement-by-measurement in its own try/except so one
    degenerate entry (e.g. geometry that's since been deleted) can't
    take down the whole overlay.
    """
    selected_indices = selected_indices or ()

    LINE_COL   = (1.0, 0.75, 0.15, 1.0)
    POINT_FILL = (1.0, 0.75, 0.15, 0.30)
    POINT_OUT  = (1.0, 0.75, 0.15, 1.0)
    AREA_COL   = (0.35, 0.75, 1.0, 1.0)
    RADIUS_COL   = (0.65, 0.45, 1.0, 1.0)
    DIAMETER_COL = (1.0, 0.45, 0.60, 1.0)
    ARC_COL      = (0.45, 0.85, 1.0, 1.0)
    ANGLE_COL    = (1.0, 0.80, 0.25, 1.0)

    # Selection highlight - overrides the per-kind color above for
    # whichever measurement(s) the user currently has selected.
    SEL_LINE_COL  = (0.25, 0.95, 0.35, 1.0)
    SEL_FILL_COL  = (0.25, 0.95, 0.35, 0.35)
    SEL_TEXT_COL  = (0.65, 1.0, 0.70, 1.0)

    for i, m in enumerate(measurements):
        try:
            is_sel = (i in selected_indices) or (i == hover_index)
            keep_mark = " \U0001f512" if m.get('kept') else ""

            if m['kind'] == 'LEN':
                s1 = world_to_screen(context, m['p1'])
                s2 = world_to_screen(context, m['p2'])
                if s1 and s2:
                    _side_override = m.get('side_override')
                    _side = _side_override if _side_override is not None else _dim_side(s1, s2)
                    draw_measurement_dimension(
                        s1, s2, _format_length(context, m['dist']) + keep_mark,
                        line_col=SEL_LINE_COL if is_sel else LINE_COL,
                        point_fill=SEL_FILL_COL if is_sel else POINT_FILL,
                        point_outline=SEL_LINE_COL if is_sel else POINT_OUT,
                        text_col=SEL_TEXT_COL if is_sel else (1.0, 1.0, 1.0, 1.0),
                        side=_side,
                        offset=m.get('dim_offset'),
                        text_size=m.get('text_size'),
                    )
                    # Draggable handles at the far end of each extension
                    # line - only shown for a *selected* measurement, so
                    # editing the dimension's offset is an explicit,
                    # deliberate action (select it first, same as any
                    # other per-measurement edit) rather than something
                    # that could happen by accidentally clicking near an
                    # unselected measurement's extension line. Both
                    # handles move together since they're both driven by
                    # the same shared 'dim_offset' - dragging either one
                    # updates that one value (see _ext_handle_at_cursor()
                    # and the drag handling in modal()).
                    if i in selected_indices:
                        geom = compute_len_extension_lines(s1, s2, _side, m.get('dim_offset'))
                        if geom is not None:
                            _e1s, e1_end, _e2s, e2_end = geom
                            HANDLE_FILL = (1.0, 1.0, 1.0, 0.85)
                            HANDLE_OUT  = (0.25, 0.95, 0.35, 1.0)
                            from ..gfx.markers import draw_vertex_square
                            draw_vertex_square(e1_end, 4.5, fill_col=HANDLE_FILL, outline_col=HANDLE_OUT, outline_w=1.6)
                            draw_vertex_square(e2_end, 4.5, fill_col=HANDLE_FILL, outline_col=HANDLE_OUT, outline_w=1.6)
            elif m['kind'] == 'AREA':
                sc = world_to_screen(context, m['center'])
                if sc is None:
                    continue
                outline_screen = None
                if m['outline']:
                    outline_screen = [world_to_screen(context, p) for p in m['outline']]
                    if any(s is None for s in outline_screen):
                        outline_screen = None
                draw_area_measurement(
                    sc, _format_area(context, m['area']) + keep_mark,
                    outline_screen=outline_screen,
                    line_col=SEL_LINE_COL if is_sel else AREA_COL,
                    text_col=SEL_TEXT_COL if is_sel else (1.0, 1.0, 1.0, 1.0),
                )
            elif m['kind'] == 'RADIUS':
                sc = world_to_screen(context, m['center'])
                se = world_to_screen(context, m['edge'])
                if sc and se:
                    draw_radius_measurement(
                        sc, se, _format_length(context, m['radius']) + keep_mark,
                        line_col=SEL_LINE_COL if is_sel else RADIUS_COL,
                        text_col=SEL_TEXT_COL if is_sel else (1.0, 1.0, 1.0, 1.0),
                    )
            elif m['kind'] == 'DIAMETER':
                se1 = world_to_screen(context, m['edge1'])
                se2 = world_to_screen(context, m['edge2'])
                if se1 and se2:
                    draw_diameter_measurement(
                        se1, se2, _format_length(context, m['radius'] * 2.0) + keep_mark,
                        line_col=SEL_LINE_COL if is_sel else DIAMETER_COL,
                        text_col=SEL_TEXT_COL if is_sel else (1.0, 1.0, 1.0, 1.0),
                    )
            elif m['kind'] == 'ARC':
                path_screen = [world_to_screen(context, p) for p in m['path']]
                if all(s is not None for s in path_screen):
                    draw_arc_length_measurement(
                        path_screen, _format_length(context, m['length']) + keep_mark,
                        closed=m.get('closed', False), label_t=m.get('label_t'),
                        line_col=SEL_LINE_COL if is_sel else ARC_COL,
                        text_col=SEL_TEXT_COL if is_sel else (1.0, 1.0, 1.0, 1.0),
                    )
            else:  # 'ANGLE'
                sa = world_to_screen(context, m['apex'])
                s1 = world_to_screen(context, m['ray1_end'])
                s2 = world_to_screen(context, m['ray2_end'])
                if sa and s1 and s2:
                    draw_angle_measurement(
                        sa, s1, s2, _format_angle(context, m['angle_rad']) + keep_mark,
                        line_col=SEL_LINE_COL if is_sel else ANGLE_COL,
                        text_col=SEL_TEXT_COL if is_sel else (1.0, 1.0, 1.0, 1.0),
                    )
        except Exception:
            continue


def _dist_to_text_box(mx, my, cx, cy, text, fs=13, pad_x=5, pad_y=3):
    """
    Distance from (mx, my) to the axis-aligned bounding box of a
    measurement's text plate centred on (cx, cy) - 0.0 if the cursor
    is inside it. Used so hovering/double-clicking the *label* itself
    (not just the line/arc it sits on) counts as targeting the
    measurement, matching what the user actually sees on screen.

    Ignores label rotation (uses an upright box at the given size) -
    close enough for hit-testing purposes since dimension labels are
    always roughly text-sized regardless of the angle they're drawn
    at, and this keeps hit-testing simple and robust.
    """
    tw, th = gfx_text_dims(text, fs)
    hw, hh = tw / 2 + pad_x, th / 2 + pad_y
    dx = max(abs(mx - cx) - hw, 0.0)
    dy = max(abs(my - cy) - hh, 0.0)
    return math.hypot(dx, dy)


def _measurement_hit_dist(context, m, mx, my):
    """
    Return the on-screen pixel distance from (mx, my) to measurement
    dict *m*'s **visible** dimension line/arc or its measurement-text
    label, or None if it can't be projected to screen space right now
    (e.g. behind the camera).

    Deliberately tests against what's actually drawn (the offset
    dimension line for LEN, the dashed sweep arc for ANGLE, etc.) -
    not the underlying measured geometry - so hovering/selecting a
    measurement requires pointing at the dimension line or its label,
    the same as a real CAD app, rather than at the edge/circle/curve
    being measured.

    Used by _find_measurement_at_cursor() below, for both hover
    highlighting and double-click selection.
    """
    try:
        if m['kind'] == 'LEN':
            s1 = world_to_screen(context, m['p1'])
            s2 = world_to_screen(context, m['p2'])
            if not s1 or not s2:
                return None
            side_override = m.get('side_override')
            side = side_override if side_override is not None else _dim_side(s1, s2)
            geom = compute_len_dimension_line(s1, s2, side, m.get('dim_offset'))
            if geom is None:
                return None
            d1, d2 = geom
            cx, cy = _closest_point_on_segment_2d((mx, my), d1, d2)
            line_d = math.hypot(cx - mx, cy - my)
            mid = ((d1[0] + d2[0]) / 2, (d1[1] + d2[1]) / 2)
            text = _format_length(context, m['dist'])
            text_d = _dist_to_text_box(mx, my, mid[0], mid[1], text)
            return min(line_d, text_d)

        elif m['kind'] == 'AREA':
            sc = world_to_screen(context, m['center'])
            if sc is None:
                return None
            # Clicking anywhere inside the area's outline counts as a
            # direct hit, not just near its center label - the outline
            # (when present) *is* this measurement's "dimension line".
            if m.get('outline'):
                outline_screen = [world_to_screen(context, p) for p in m['outline']]
                if all(s is not None for s in outline_screen) and point_in_polygon(outline_screen, (mx, my)):
                    return 0.0
            text = _format_area(context, m['area'])
            r = 5
            text_cx = sc[0]
            text_cy = sc[1] + r + 6 + gfx_text_dims(text, 13)[1] / 2
            return _dist_to_text_box(mx, my, text_cx, text_cy, text)

        elif m['kind'] == 'RADIUS':
            sc = world_to_screen(context, m['center'])
            se = world_to_screen(context, m['edge'])
            if not sc or not se:
                return None
            cx, cy = _closest_point_on_segment_2d((mx, my), sc, se)
            line_d = math.hypot(cx - mx, cy - my)
            mid = ((sc[0] + se[0]) / 2, (sc[1] + se[1]) / 2)
            text = _format_length(context, m['radius'])
            text_d = _dist_to_text_box(mx, my, mid[0], mid[1], text)
            return min(line_d, text_d)

        elif m['kind'] == 'DIAMETER':
            se1 = world_to_screen(context, m['edge1'])
            se2 = world_to_screen(context, m['edge2'])
            if not se1 or not se2:
                return None
            cx, cy = _closest_point_on_segment_2d((mx, my), se1, se2)
            line_d = math.hypot(cx - mx, cy - my)
            mid = ((se1[0] + se2[0]) / 2, (se1[1] + se2[1]) / 2)
            text = _format_length(context, m['radius'] * 2.0)
            text_d = _dist_to_text_box(mx, my, mid[0], mid[1], text)
            return min(line_d, text_d)

        elif m['kind'] == 'ARC':
            path_screen = [world_to_screen(context, p) for p in m['path']]
            if len(path_screen) < 2 or any(s is None for s in path_screen):
                return None
            dash_path = list(path_screen)
            if m.get('closed') and dash_path[0] != dash_path[-1]:
                dash_path.append(dash_path[0])
            best = float('inf')
            for a, b in zip(dash_path, dash_path[1:]):
                cx, cy = _closest_point_on_segment_2d((mx, my), a, b)
                d = math.hypot(cx - mx, cy - my)
                if d < best:
                    best = d
            # The label sits directly on the path (at label_t, or the
            # topmost point by default) - same anchor draw_arc_length_
            # measurement() uses.
            label_t = m.get('label_t')
            if label_t is not None:
                anchor = _point_at_fraction(dash_path, label_t)
            else:
                anchor = dash_path[0]
                for x, y in dash_path[1:]:
                    if y > anchor[1]:
                        anchor = (x, y)
            text = _format_length(context, m['length'])
            text_d = _dist_to_text_box(mx, my, anchor[0], anchor[1], text)
            return min(best, text_d)

        else:  # 'ANGLE'
            sa = world_to_screen(context, m['apex'])
            s1 = world_to_screen(context, m['ray1_end'])
            s2 = world_to_screen(context, m['ray2_end'])
            if not sa or not s1 or not s2:
                return None
            geom = compute_angle_arc_geometry(sa, s1, s2)
            if geom is None:
                return None
            a1, ang_end, radius = geom
            delta = ang_end - a1
            ax, ay = sa
            # Sample the dashed arc itself (what's actually drawn),
            # not the straight rays underneath it.
            n = 20
            best = float('inf')
            prev = None
            for i in range(n + 1):
                t = a1 + delta * (i / n)
                p = (ax + math.cos(t) * radius, ay + math.sin(t) * radius)
                if prev is not None:
                    cx, cy = _closest_point_on_segment_2d((mx, my), prev, p)
                    d = math.hypot(cx - mx, cy - my)
                    if d < best:
                        best = d
                prev = p
            # Label sits just outside the arc, at its angular midpoint.
            mid_ang = a1 + delta / 2.0
            label_r = radius + 14.0
            lx = ax + math.cos(mid_ang) * label_r
            ly = ay + math.sin(mid_ang) * label_r
            text = _format_angle(context, m['angle_rad'])
            text_d = _dist_to_text_box(mx, my, lx, ly, text)
            return min(best, text_d)
    except Exception:
        return None


def _find_measurement_at_cursor(context, measurements, mx, my, tolerance=14.0):
    """
    Return the index of the measurement in *measurements* whose
    **visible** dimension line/arc or measurement-text label is
    closest to (mx, my), as long as it's within *tolerance* pixels -
    or None if nothing is close enough. Ties are broken in favor of
    the measurement placed most recently (later in the list), since
    that's usually the one the user is looking at.

    Used both for hover highlighting and for double-click selection,
    so the two always agree on what counts as "pointing at" a
    measurement.
    """
    best_i, best_d = None, tolerance
    for i, m in enumerate(measurements):
        d = _measurement_hit_dist(context, m, mx, my)
        if d is not None and d <= best_d:
            best_d = d
            best_i = i
    return best_i


def _ext_handle_at_cursor(context, measurements, selected_indices, mx, my, tolerance=10.0):
    """
    Return (measurement_index, which_end) for whichever selected LEN
    measurement's extension-line drag handle - which_end 0 for the one
    at p1, 1 for the one at p2 - is within *tolerance* px of (mx, my),
    or None if none is close enough. Both handles always sit at the
    same shared 'dim_offset' distance from their respective measured
    point, so which_end only identifies which handle the user grabbed
    for hit-testing purposes - dragging either one updates that same
    shared value, moving both extension lines (and the dimension line
    between them) together.

    Only selected measurements are checked (matching which ones
    actually have their handles drawn - see draw_measurements_list()),
    so dragging a measurement's extension lines is always a deliberate
    "select it, then drag" action.
    """
    best, best_d = None, tolerance
    for i in selected_indices:
        if i < 0 or i >= len(measurements):
            continue
        m = measurements[i]
        if m['kind'] != 'LEN':
            continue
        s1 = world_to_screen(context, m['p1'])
        s2 = world_to_screen(context, m['p2'])
        if not s1 or not s2:
            continue
        side_override = m.get('side_override')
        side = side_override if side_override is not None else _dim_side(s1, s2)
        geom = compute_len_extension_lines(s1, s2, side, m.get('dim_offset'))
        if geom is None:
            continue
        _e1s, e1_end, _e2s, e2_end = geom
        d1 = math.hypot(e1_end[0] - mx, e1_end[1] - my)
        d2 = math.hypot(e2_end[0] - mx, e2_end[1] - my)
        if d1 < best_d:
            best_d, best = d1, (i, 0)
        if d2 < best_d:
            best_d, best = d2, (i, 1)
    return best


def _dim_side(s1, s2):
    """Pick which side of the measured segment the dimension line/text
    should sit on, so it reads above (or to the right of) the segment
    rather than flipping unpredictably as the mouse moves."""
    dx, dy = s2[0] - s1[0], s2[1] - s1[1]
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return 1.0
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    return 1.0 if py >= 0 else -1.0


def _side_from_cursor(s1, s2, mx, my):
    """
    Return +1.0 / -1.0 for whichever side of the on-screen segment
    s1->s2 the point (mx, my) currently sits on, using the exact same
    perpendicular convention draw_measurement_dimension()/_dim_side()
    use (px, py = -uy, ux at side=+1) - so passing this straight back
    in as 'side_override' puts the dimension line on the side the
    cursor is actually pointing at.

    This is what lets LENGTH's 3rd click choose the dimension line's
    side directly with the mouse: for a horizontal segment the
    perpendicular is vertical, so above/below the cursor maps to
    above/below the segment; for a vertical segment the perpendicular
    is horizontal, so left/right of the cursor maps to left/right of
    the segment - and everything in between follows the same rule
    continuously, so it also works for segments at any other angle.
    """
    x1, y1 = s1
    x2, y2 = s2
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return 1.0
    ux, uy = dx / length, dy / length
    px, py = -uy, ux                    # perpendicular unit at side=+1
    mux, muy = (x1 + x2) / 2.0, (y1 + y2) / 2.0   # segment midpoint
    dot = (mx - mux) * px + (my - muy) * py
    return 1.0 if dot >= 0 else -1.0


def _offset_from_cursor(s1, s2, mx, my):
    """
    Return how far (px) the point (mx, my) sits from the on-screen
    segment s1->s2 along its perpendicular axis - the magnitude, not
    the sign (see _side_from_cursor() for that) - clamped to
    min_dim_offset() so it's always a usable 'dim_offset'.

    This is what lets the cursor set the dimension line's *distance*
    from the measured edge (not just which side it's on) while
    choosing the side during LENGTH's 3rd-click "choose side" phase:
    move further from the edge for a bigger gap, closer for a smaller
    one, exactly matching where the dimension line/extension lines end
    up once confirmed. Works the same regardless of the segment's
    angle, since it's just the perpendicular distance to the line.
    """
    x1, y1 = s1
    x2, y2 = s2
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return min_dim_offset()
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    mux, muy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    dot = (mx - mux) * px + (my - muy) * py
    return max(min_dim_offset(), abs(dot))


class CAD_OT_Measure(bpy.types.Operator):
    """AutoCAD-style distance measurement: click, drag, click."""
    bl_idname  = "cad.measure"
    bl_label   = "CAD Measure"
    bl_options = {'REGISTER'}   # no UNDO - this tool never modifies scene data

    # ── measurement-type dropdown menu / indicator (same style as
    #    the Array tool's method switcher) ──────────────────────────
    _MENU_W        = 170
    _MENU_ROW_H    = 32
    _MENU_HEADER_H = 28
    _MENU_OFFSET_X = 18
    _MENU_OFFSET_Y = 18
    _MENU_RADIUS   = 6
    _MENU_BG          = (0.110, 0.110, 0.110, 0.97)
    _MENU_HEADER_BG   = (0.145, 0.145, 0.145, 0.97)
    _MENU_BG_ACTIVE   = (0.239, 0.435, 0.769, 1.00)
    _MENU_BG_HOVER    = (0.200, 0.200, 0.200, 0.90)
    _MENU_BORDER      = (0.172, 0.172, 0.172, 1.00)
    _MENU_SEP         = (0.220, 0.220, 0.220, 1.00)
    _MENU_HEADER_COL  = (0.620, 0.620, 0.620, 1.00)
    _MENU_TEXT_IDLE   = (0.850, 0.850, 0.850, 1.00)
    _MENU_TEXT_ACT    = (1.000, 1.000, 1.000, 1.00)

    _INDICATOR_SIZE    = 28
    _INDICATOR_OFFSET  = 18
    _INDICATOR_RADIUS  = 6
    _INDICATOR_BG      = (0.110, 0.110, 0.110, 0.97)
    _INDICATOR_BORDER  = (0.239, 0.435, 0.769, 1.00)
    _INDICATOR_GLYPH   = (1.000, 1.000, 1.000, 1.00)

    def _indicator_rect(self, context):
        s = self._INDICATOR_SIZE
        o = self._INDICATOR_OFFSET
        bx = self._mx - o - s
        by = self._my - o - s
        region = context.region
        if region:
            if bx < 4:
                bx = self._mx + o
            if by < 4:
                by = self._my + o
        return bx, by, s, s

    def _draw_method_indicator(self, context):
        x0, y0, s, _ = self._indicator_rect(context)
        r = self._INDICATOR_RADIUS
        gfx_rounded_rect_fill(x0, y0, s, s, r, self._INDICATOR_BG)
        gfx_rounded_rect_loop(x0, y0, s, s, r, self._INDICATOR_BORDER, 1.6)

        cx, cy = x0 + s / 2, y0 + s / 2
        col = self._INDICATOR_GLYPH
        if self._mode == 'LENGTH':
            # little dimension-line glyph: two endpoint dots + a line
            gfx_lines([(cx - 8, cy), (cx + 8, cy)], col, 1.4)
            gfx_dot((cx - 8, cy), col, 3.0)
            gfx_dot((cx + 8, cy), col, 3.0)
        elif self._mode == 'AREA':
            # little dashed-square glyph, for Area
            r2 = 7
            corners = [(cx - r2, cy - r2), (cx + r2, cy - r2),
                       (cx + r2, cy + r2), (cx - r2, cy + r2)]
            for a, b in zip(corners, corners[1:] + corners[:1]):
                gfx_lines([a, b], col, 1.4)
            gfx_dot((cx, cy), col, 2.2)
        elif self._mode == 'RADIUS':
            # little circle-with-radius-line glyph, for Radius
            gfx_circle((cx, cy), 7, col, segs=24, w=1.4)
            gfx_lines([(cx, cy), (cx + 7, cy)], col, 1.6)
            gfx_dot((cx, cy), col, 2.2)
        elif self._mode == 'DIAMETER':
            # little circle-with-full-diameter-line glyph, for Diameter
            gfx_circle((cx, cy), 7, col, segs=24, w=1.4)
            gfx_lines([(cx - 7, cy), (cx + 7, cy)], col, 1.6)
            gfx_dot((cx, cy), col, 2.2)
        elif self._mode == 'ARC':
            # little curved-arc glyph, for Arc
            segs = 10
            a0, a1 = math.radians(200), math.radians(340)
            arc_pts = [
                (cx + math.cos(a0 + (a1 - a0) * i / segs) * 7,
                 cy + math.sin(a0 + (a1 - a0) * i / segs) * 7)
                for i in range(segs + 1)
            ]
            for a, b in zip(arc_pts, arc_pts[1:]):
                gfx_lines([a, b], col, 1.6)
            gfx_dot(arc_pts[0], col, 2.2)
            gfx_dot(arc_pts[-1], col, 2.2)
        else:
            # little two-rays-and-an-arc glyph, for Angle
            r1 = (cx + 7, cy)
            r2 = (cx + 7 * math.cos(math.radians(55)), cy - 7 * math.sin(math.radians(55)))
            gfx_lines([(cx, cy), r1], col, 1.6)
            gfx_lines([(cx, cy), r2], col, 1.6)
            segs = 8
            a0, a1 = 0.0, math.radians(55)
            arc_pts = [
                (cx + math.cos(a0 + (a1 - a0) * i / segs) * 3.5,
                 cy - math.sin(a0 + (a1 - a0) * i / segs) * 3.5)
                for i in range(segs + 1)
            ]
            for a, b in zip(arc_pts, arc_pts[1:]):
                gfx_lines([a, b], col, 1.2)
            gfx_dot((cx, cy), col, 2.2)

    def _menu_rect(self, context):
        box_w = self._MENU_W
        box_h = self._MENU_HEADER_H + len(_MODES) * self._MENU_ROW_H
        bx = self._mx + self._MENU_OFFSET_X
        by = self._my + self._MENU_OFFSET_Y
        region = context.region
        if region:
            if bx + box_w > region.width - 4:
                bx = self._mx - self._MENU_OFFSET_X - box_w
            if by + box_h > region.height - 4:
                by = self._my - self._MENU_OFFSET_Y - box_h
        return bx, by, box_w, box_h

    def _menu_hit(self, context, mx, my):
        bx, by, box_w, box_h = self._menu_rect(context)
        header_y = by + box_h - self._MENU_HEADER_H
        if header_y <= my < by + box_h:
            return None
        if not (bx <= mx < bx + box_w):
            return None
        for ri, m in enumerate(_MODES):
            ry = by + (len(_MODES) - 1 - ri) * self._MENU_ROW_H
            if ry <= my < ry + self._MENU_ROW_H:
                return m
        return None

    def _draw_method_menu(self, context):
        bx, by, box_w, box_h = self._menu_rect(context)
        gfx_rounded_rect_fill(bx, by, box_w, box_h, self._MENU_RADIUS, self._MENU_BG)
        gfx_rounded_rect_loop(bx, by, box_w, box_h, self._MENU_RADIUS, self._MENU_BORDER, 1.0)

        header_y = by + box_h - self._MENU_HEADER_H
        gfx_rect_fill(bx + 1, header_y, box_w - 2, self._MENU_HEADER_H - 1, self._MENU_HEADER_BG)
        gfx_text("Measurement Type", bx + 10, header_y + (self._MENU_HEADER_H - 10) // 2, 10, self._MENU_HEADER_COL)
        sep_y = header_y - 1
        gfx_lines([(bx + 1, sep_y), (bx + box_w - 1, sep_y)], self._MENU_SEP, 1.0)

        for ri, m in enumerate(_MODES):
            ry = by + (len(_MODES) - 1 - ri) * self._MENU_ROW_H
            is_active = (m == self._mode)
            is_hover  = (self._menu_hit(context, self._mx, self._my) == m) and not is_active
            if is_active:
                gfx_rect_fill(bx + 1, ry + 1, box_w - 2, self._MENU_ROW_H - 2, self._MENU_BG_ACTIVE)
            elif is_hover:
                gfx_rect_fill(bx + 1, ry + 1, box_w - 2, self._MENU_ROW_H - 2, self._MENU_BG_HOVER)
            text_col = self._MENU_TEXT_ACT if is_active else self._MENU_TEXT_IDLE
            label = ("✓ " if is_active else "  ") + _MODE_LABELS[m]
            label_y = ry + (self._MENU_ROW_H - 12) // 2
            gfx_text(label, bx + 12, label_y, 12, text_col)

    def _set_mode(self, context, new_mode):
        if new_mode == self._mode:
            return
        self._mode = new_mode
        # Toggling mid-drag would leave a dangling first point behind.
        self._p1 = None
        self._pending_p2 = None
        self._awaiting_side = False
        self._dragging_ext = None
        self._area_hover = None
        self._radius_hover = None
        self._diameter_hover = None
        self._arc_hover = None
        self._arc_pending = None
        self._arc_label_t = None
        self._angle_edge1 = None
        self._angle_hover = None
        self._otrack.reset(clear_points=True)
        self._update_header(context)

    def _cycle_mode(self, context, step=1):
        idx = _MODES.index(self._mode)
        self._set_mode(context, _MODES[(idx + step) % len(_MODES)])

    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._raw       = Vector((0, 0, 0))
        self._cur       = Vector((0, 0, 0))
        self._snap_type = 'FREE'

        self._p1 = None   # first click point (world), None = waiting for it

        # 'LENGTH' (default) = click-click / double-click-a-line, as
        # before. 'AREA' = single click on any object shows its area
        # at its centre. Press M any time to toggle between them.
        self._mode = 'LENGTH'
        self._area_hover = None     # live (area, center, outline) under the cursor in AREA mode
        self._radius_hover = None   # live (center, radius, edge_point) under the cursor in RADIUS mode
        self._diameter_hover = None # live (center, radius, edge1, edge2) under the cursor in DIAMETER mode
        self._arc_hover = None      # live (path_world, length_world, closed) under the cursor in ARC mode
        self._arc_pending = None    # (path_world, length_world, closed) once a curve is picked, awaiting label placement
        self._arc_label_t = None    # live fraction (0..1) along the curve, nearest the cursor, while placing the label
        self._angle_edge1 = None    # first edge (v0, v1) picked in ANGLE mode, None = waiting for it
        self._angle_hover = None    # live (apex, ray1_end, ray2_end, angle_rad) under the cursor in ANGLE mode
        self._alt_held = False      # Alt held -> show the mode dropdown menu instead of the indicator

        # All completed measurements this session, kept on screen
        # simultaneously - each is a dict tagged by 'kind' ('LEN',
        # 'AREA', 'RADIUS', 'DIAMETER', 'ARC', or 'ANGLE'), plus a
        # 'kept' flag. Starting or finishing a new measurement never
        # clears the earlier ones.
        #
        # Seeded from state.kept_measurements (previously-kept results
        # left over from an earlier Measure session) so those can also
        # be selected/deleted/un-kept right alongside anything measured
        # in this session, rather than only being visible-but-untouchable
        # once the tool has been closed. CAD_OT_SketchModeManager's
        # overlay skips drawing state.kept_measurements while this tool
        # is the active one (see its _draw_cursor_overlay) so these
        # don't get drawn twice.
        #
        # A measurement's 'kept' flag decides whether it survives after
        # this tool exits (applied in cancel(), below) - toggle it with
        # Tab on whichever measurement(s) are currently selected, or
        # immediately while still drawing a new one.
        self._measurements = [dict(m) for m in _st.kept_measurements]

        # Indices into self._measurements that are currently selected
        # (double-click to select/replace selection, Shift+Click to
        # add/remove one at a time, Ctrl+A to select all - this
        # includes previously-kept measurements loaded above, so they
        # can be deleted or un-kept too). Selected measurements are
        # drawn highlighted in green; X deletes them, Tab toggles
        # their individual 'kept' flag.
        self._selected = set()

        # Tracks whether the "CAD Dimension" N-panel sidebar was open
        # the last time _sync_dimension_panel() checked - see that
        # method (near modal()) for why visibility is synced every
        # tick instead of only at the specific spots that change
        # _selected (that approach missed cases like an ordinary,
        # non-double click starting a new measurement while one was
        # still selected).
        self._dim_panel_shown = False

        # Index into self._measurements that the cursor is currently
        # hovering over (its dimension line/arc or its text label,
        # per _find_measurement_at_cursor()) - None if nothing's under
        # the cursor. Kept continuously up to date on MOUSEMOVE and
        # drawn with the same green highlight as a selected
        # measurement, so hovering previews what a double-click would
        # select without changing the actual selection.
        self._hover_i = None

        # Whether the measurement currently being drawn (mid-drag/
        # mid-pick, not yet finalized) will be marked 'kept' the
        # instant it's completed - set by pressing Tab while it's
        # still in progress (see modal()). Reset to False after each
        # measurement is appended, so it only ever applies to the one
        # in-progress measurement it was set during.
        self._pending_kept = False

        # Which side of the segment the LENGTH mode's live dimension
        # line/extension lines/label are drawn on while a measurement
        # is actively being placed - None means auto (whichever side
        # _dim_side() picks); +1.0 / -1.0 is an explicit choice.
        # Normally set live from cursor position during the 3rd-click
        # "choose side" phase below (self._awaiting_side), continuously
        # updated as the mouse moves to whichever side of the segment
        # it's currently on - Up/Down also still set it explicitly as
        # a keyboard alternative. Carried over onto the finished
        # measurement (as 'side_override' in its dict) so it sticks
        # after confirming, then reset to None once that measurement
        # is appended, ready for the next one.
        self._live_side_override = None

        # How far (px) the LENGTH mode's live dimension line/extension
        # lines sit from the measured segment while a measurement is
        # actively being placed - None means the default offset.
        # Updated alongside self._live_side_override during the
        # 3rd-click "choose side" phase (self._awaiting_side): moving
        # the cursor further from/closer to the measured edge sets
        # this, while which side of the edge it's on sets
        # self._live_side_override, so a single cursor position (and
        # the click that confirms it) picks both the side and the
        # gap in one motion, exactly matching how the finished
        # measurement will look. Carried over onto the finished
        # measurement (as 'dim_offset' in its dict), then reset to
        # None once appended, ready for the next one.
        self._live_dim_offset = None

        # LENGTH mode is a 3-click flow: 1st click sets self._p1, 2nd
        # click locks in the distance as self._pending_p2 and flips
        # self._awaiting_side on, 3rd click confirms whichever side
        # and offset the cursor is currently over (see
        # self._live_side_override / self._live_dim_offset above) and
        # appends the finished measurement. While self._awaiting_side
        # is True the distance is frozen - mouse movement only steers
        # which side of the segment, and how far from it, the
        # dimension line/extension lines/label sit, previewed live
        # exactly like the finished result will look.
        self._pending_p2 = None
        self._awaiting_side = False

        # (measurement_index, which_end) of the extension-line handle
        # currently being dragged (0 = the one at p1, 1 = the one at
        # p2) - None when nothing's being dragged. Set/cleared by
        # LEFTMOUSE press/release on a selected LEN measurement's
        # handle (see _ext_handle_at_cursor() and modal()); while set,
        # MOUSEMOVE updates only that measurement's shared
        # 'dim_offset' - never its p1/p2/dist - so dragging moves both
        # extension lines and the dimension line between them
        # together, changing only the dimension's offset from the
        # measured geometry; the measured distance itself is never
        # touched by this.
        self._dragging_ext = None
        # (add/remove from selection) during its current hold - reset
        # on Shift PRESS, checked on Shift RELEASE. If Shift was just
        # tapped alone (never combined with a click), releasing it
        # flips the selected LEN measurement(s)' direction instead.
        self._shift_used_for_click = False

        # Manual double-click detection (see modal()) - event.value ==
        # 'DOUBLE_CLICK' isn't reliably delivered to a running modal
        # operator across all Blender versions/platforms, so we also
        # track click timing/position ourselves as a fallback.
        # Tracked separately for the "double-click to select" gesture
        # and the "double-click an edge to measure its full length"
        # gesture, so the two never interfere with each other.
        self._last_click_time = None
        self._last_click_pos  = (0, 0)
        self._last_select_click_time = None
        self._last_select_click_pos  = (0, 0)

        self._mx, self._my = event.mouse_region_x, event.mouse_region_y
        self._snap_cache = build_snap_cache(context)
        self._otrack     = SnapAcquisitionManager()
        self._otrack.reset(clear_points=True)

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "MEASURE"
        context.window_manager.modal_handler_add(self)
        self._update_header(context)

        # Let the "CAD Dimension" N-panel (ui/measurement_panel.py) reach
        # this running session's live measurements/selection - see
        # state.measure_operator_ref for why that's needed.
        _st.measure_operator_ref = self
        return {'RUNNING_MODAL'}

    def _update_header(self, context):
        keep_flag = (
            "  |  Double-click=select, Shift+Click=add to selection, "
            "Ctrl+A=select all, X/Del=delete, Tab=keep, Shift=flip direction"
        )
        if self._mode == 'LENGTH':
            context.area.header_text_set(
                "MEASURE (Length)  |  Click first point  |  drag & click second point  |  "
                "double-click a line to measure its full length  |  "
                "↑/↓=flip dimension line side while placing  |  "
                "M/Alt+Scroll=switch, Alt+Click=menu" + keep_flag + "  |  Enter/Esc to exit"
            )
        elif self._mode == 'AREA':
            context.area.header_text_set(
                "MEASURE (Area)  |  Click any object to measure its area  |  "
                "M/Alt+Scroll=switch, Alt+Click=menu" + keep_flag + "  |  Enter/Esc to exit"
            )
        elif self._mode == 'RADIUS':
            context.area.header_text_set(
                "MEASURE (Radius)  |  Click a circle to measure its radius  |  "
                "M/Alt+Scroll=switch, Alt+Click=menu" + keep_flag + "  |  Enter/Esc to exit"
            )
        elif self._mode == 'DIAMETER':
            context.area.header_text_set(
                "MEASURE (Diameter)  |  Click a circle to measure its diameter  |  "
                "M/Alt+Scroll=switch, Alt+Click=menu" + keep_flag + "  |  Enter/Esc to exit"
            )
        elif self._mode == 'ARC':
            context.area.header_text_set(
                "MEASURE (Circumference)  |  Click a circle, oval, or arc, then click again to place the label  |  "
                "M/Alt+Scroll=switch, Alt+Click=menu" + keep_flag + "  |  Enter/Esc to exit"
            )
        else:
            context.area.header_text_set(
                "MEASURE (Angle)  |  Click first line, then click a second line  |  "
                "M/Alt+Scroll=switch, Alt+Click=menu" + keep_flag + "  |  Enter/Esc to exit"
            )

    def cancel(self, context):
        # Rebuild state.kept_measurements from scratch out of whichever
        # of this session's measurements are still flagged 'kept' -
        # this session started with a copy of the previous
        # state.kept_measurements (see invoke()), so anything the user
        # deleted (X) or un-kept (Tab) here is correctly dropped now
        # too, instead of only ever accumulating. CAD_OT_SketchModeManager's
        # always-on overlay then keeps drawing just this final list for
        # the rest of the Sketch Mode session.
        _st.kept_measurements[:] = [m for m in self._measurements if m.get('kept')]

        if _st.measure_operator_ref is self:
            _st.measure_operator_ref = None

        # No measurement stays "live-selected" once the tool itself has
        # exited - close the sidebar rather than leave it open with
        # nothing (or, via the Kept Measurements list fallback, some
        # stale prior selection) still showing.
        if not self._selected:
            hide_dimension_panel(context)

        try:
            bpy.types.SpaceView3D.draw_handler_remove(self._handler, 'WINDOW')
        except ValueError:
            pass
        restore_modal_cursor(self, context)
        context.area.header_text_set(None)
        context.scene.cad_settings.active_tool = "SELECT"
        self._otrack.reset(clear_points=True)
        reset_tpanel_to_select(self, context)
        context.area.tag_redraw()

    def _measure_edge_at_cursor(self, context):
        """Double-click support: pick the line/edge under
        (self._mx, self._my) and immediately drop into the same
        "choose side" phase the normal 3-click flow uses after its
        2nd click - full length, endpoint to endpoint, with no manual
        endpoint picking, but still letting the user pick which side
        the dimension line/extension lines/label land on with the
        cursor (or Up/Down) before a further click confirms it.

        Rebuilds the snap cache fresh here (rather than reusing the
        one captured back in invoke()) so this also finds any line
        drawn *after* Measure was activated.
        """
        try:
            cache = build_snap_cache(context)
        except Exception:
            cache = self._snap_cache
        hit = _pick_edge_at_cursor(context, self._mx, self._my, cache)
        if hit is None:
            return
        v0, v1 = hit
        self._p1 = v0
        self._pending_p2 = v1
        self._awaiting_side = True
        s1 = world_to_screen(context, v0)
        s2 = world_to_screen(context, v1)
        self._live_side_override = (
            _side_from_cursor(s1, s2, self._mx, self._my) if (s1 and s2) else None
        )
        self._live_dim_offset = (
            _offset_from_cursor(s1, s2, self._mx, self._my) if (s1 and s2) else None
        )
        self._otrack.reset(clear_points=True)
        context.area.tag_redraw()

    def modal(self, context, event):
        # Any unhandled exception inside _modal() must still pop this
        # tool's own cursor push before the operator ends, or the OS
        # cursor is left invisible outside the viewport with no visible
        # cause - see guarded_modal()'s docstring.
        result = guarded_modal(self, context, event, self._modal)
        self._sync_dimension_panel(context)
        return result

    def _sync_dimension_panel(self, context):
        """
        Keep the "CAD Dimension" N-panel sidebar's open/closed state in
        lockstep with whether a dimension is currently selected -
        called once every modal tick (from modal(), above) rather than
        only from the handful of places that change self._selected, so
        it can't be missed by some click/key combination none of those
        call sites anticipated (e.g. an ordinary, non-double click
        starting a new measurement while a previous one was still
        selected - that changes nothing about _selected itself, but the
        user's intent to move on from the old selection is clear from
        having started a new one - see below).

        reveal_dimension_panel()/hide_dimension_panel() themselves are
        idempotent-ish (they just set a bool + tag_redraw), but they're
        still only called on an actual show/hidden transition
        (self._dim_panel_shown) rather than every tick, so a user who's
        deliberately closed the sidebar themselves via N while a
        dimension is still selected doesn't have it forced back open
        every frame.
        """
        # Starting to place a brand-new measurement implicitly
        # abandons whatever was selected before, even though none of
        # those in-progress fields are part of self._selected itself.
        mid_new_measurement = (
            self._p1 is not None or self._pending_p2 is not None or
            self._arc_pending is not None or self._angle_edge1 is not None
        )
        show = bool(self._selected) and not mid_new_measurement
        if show != self._dim_panel_shown:
            if show:
                reveal_dimension_panel(context)
            else:
                hide_dimension_panel(context)
            self._dim_panel_shown = show

    def _modal(self, context, event):
        if handle_ui_handoff(self, context, event):
            return {'PASS_THROUGH'}
        update_modal_cursor(self, context, event)
        if not context.scene.cad_settings.sketch_mode:
            self.cancel(context)
            return {'CANCELLED'}

        try:
            context.area.tag_redraw()
            self._mx, self._my = event.mouse_region_x, event.mouse_region_y

            # Menu/dropdown is only meaningful while not mid-drag on a
            # length measurement (same restriction the Array tool uses).
            menu_ok = self._p1 is None
            self._alt_held = bool(event.alt) and menu_ok

            if event.type in {'LEFT_ALT', 'RIGHT_ALT'}:
                self._alt_held = (event.value == 'PRESS') and menu_ok
                return {'RUNNING_MODAL'}

            if event.type == 'MIDDLEMOUSE':
                return {'PASS_THROUGH'}
            if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN'} and not (event.type != 'TRACKPADPAN' and menu_ok and event.alt):
                return {'PASS_THROUGH'}

            if menu_ok and event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} and event.value == 'PRESS' and event.alt:
                self._cycle_mode(context, -1 if event.type == 'WHEELUPMOUSE' else 1)
                return {'RUNNING_MODAL'}

            if menu_ok and event.type == 'LEFTMOUSE' and event.value == 'PRESS' and self._alt_held:
                hit = self._menu_hit(context, self._mx, self._my)
                if hit is not None:
                    self._set_mode(context, hit)
                self._alt_held = False
                return {'RUNNING_MODAL'}

            # ── Drag an extension-line handle to move both extension
            #    lines (and the dimension line between them) together ─
            # Press-and-drag on either end of a *selected* LEN
            # measurement's extension lines (the small white/green
            # squares draw_measurements_list() draws at their far
            # ends) to move the dimension line - and both extension
            # lines along with it - closer to or further from the
            # measured segment. Both handles are driven by the same
            # shared 'dim_offset', so dragging either one moves both
            # at once, keeping them parallel, equal-length, and
            # aligned with each other and with the dimension line.
            # Purely visual - only ever writes to that measurement's
            # 'dim_offset', never its p1/p2/dist - so the measured
            # distance itself can't change this way. Checked before
            # any other click handling (and only while idle - not
            # mid-placement of a new measurement) so it always takes
            # priority over starting a new measurement or a
            # double-click when the cursor is actually on a handle.
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS' \
                    and menu_ok and self._dragging_ext is None and not self._alt_held:
                hit = _ext_handle_at_cursor(context, self._measurements, self._selected, self._mx, self._my)
                if hit is not None:
                    self._dragging_ext = hit
                    return {'RUNNING_MODAL'}

            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and self._dragging_ext is not None:
                self._dragging_ext = None
                return {'RUNNING_MODAL'}

            if event.type == 'MOUSEMOVE' and self._dragging_ext is not None:
                i, which_end = self._dragging_ext
                if 0 <= i < len(self._measurements):
                    m = self._measurements[i]
                    s1 = world_to_screen(context, m['p1'])
                    s2 = world_to_screen(context, m['p2'])
                    if s1 and s2:
                        side_override = m.get('side_override')
                        side = side_override if side_override is not None else _dim_side(s1, s2)
                        # Project the cursor onto the perpendicular
                        # axis (the axis both extension lines run
                        # along) to get the new shared offset - this
                        # is what makes dragging work the same way
                        # regardless of whether the measurement is
                        # horizontal, vertical, or at any other angle:
                        # "how far perpendicular to the measured edge
                        # is the cursor" is exactly the offset, however
                        # that edge happens to be oriented on screen.
                        # The perpendicular component of (cursor -
                        # anchor) is the same value whether anchor is
                        # s1 or s2 (their difference is purely along
                        # the segment, which is orthogonal to this
                        # axis), so either handle drives the one
                        # shared offset both extension lines - and the
                        # dimension line - use, which is exactly what
                        # keeps them moving together.
                        anchor = s1 if which_end == 0 else s2
                        dx, dy = s2[0] - s1[0], s2[1] - s1[1]
                        seg_len = math.hypot(dx, dy)
                        if seg_len >= 1e-4:
                            ux, uy = dx / seg_len, dy / seg_len
                            px, py = -uy * side, ux * side
                            new_offset = ((self._mx - anchor[0]) * px +
                                          (self._my - anchor[1]) * py)
                            m['dim_offset'] = max(new_offset, 1.0)
                            context.area.tag_redraw()
                return {'RUNNING_MODAL'}

            if event.type == 'MOUSEMOVE':
                # Keep the hover highlight (green, same as selection)
                # up to date on every mouse move: only the dimension
                # line/arc or the text label counts, per
                # _find_measurement_at_cursor(), and only while idle
                # (not mid-drag placing a new LENGTH measurement) so
                # it doesn't fight with that measurement's own
                # in-progress preview.
                self._hover_i = (
                    _find_measurement_at_cursor(context, self._measurements, self._mx, self._my)
                    if menu_ok else None
                )
                if self._mode == 'LENGTH' and self._awaiting_side:
                    # 3rd-click "choose side" phase: distance is
                    # frozen (self._pending_p2 already locked in) -
                    # mouse movement now picks both which side of the
                    # segment the dimension line/extension lines/label
                    # sit on, AND how far from it (the offset/gap),
                    # live, straight from cursor position - a single
                    # cursor position sets both at once, matching
                    # exactly how the finished measurement will look.
                    s1 = world_to_screen(context, self._p1)
                    s2 = world_to_screen(context, self._pending_p2)
                    if s1 and s2:
                        self._live_side_override = _side_from_cursor(s1, s2, self._mx, self._my)
                        self._live_dim_offset = _offset_from_cursor(s1, s2, self._mx, self._my)
                elif self._mode == 'LENGTH':
                    raw = mouse_to_3d(context, self._mx, self._my)
                    if raw:
                        # Anchor = current first point, so anchor-relative snaps
                        # (tangent/parallel/perpendicular) behave the same way
                        # they do while drawing a Line from that point.
                        self._raw, self._snap_type = apply_snaps(
                            context, raw, self._snap_cache,
                            snap_filter=_st.selection.get_active_stypes(),
                            anchor=self._p1,
                        )
                        self._cur = self._raw.copy()
                        update_tracking(self._otrack, self._raw, self._snap_type)
                elif self._mode == 'AREA':
                    # AREA mode: no point-snapping needed - just live-preview
                    # whichever object's area the cursor is currently over.
                    self._area_hover = _pick_area_at_cursor(context, self._mx, self._my)
                elif self._mode == 'RADIUS':
                    # RADIUS mode: live-preview whichever circle is under
                    # the cursor right now.
                    self._radius_hover = _pick_radius_at_cursor(context, self._mx, self._my)
                elif self._mode == 'DIAMETER':
                    # DIAMETER mode: live-preview whichever circle is
                    # under the cursor right now.
                    self._diameter_hover = _pick_diameter_at_cursor(context, self._mx, self._my)
                elif self._mode == 'ARC':
                    if self._arc_pending is None:
                        # ARC mode: live-preview whichever curve (circle,
                        # ellipse, arc, or other closed outline) is under
                        # the cursor right now.
                        self._arc_hover = _pick_curve_at_cursor(context, self._mx, self._my)
                    else:
                        # A curve is already picked - now placing its
                        # label, which snaps to whichever point on that
                        # same curve is nearest the cursor, until the
                        # next click drops it there.
                        path_world, _length, _closed = self._arc_pending
                        path_screen = [world_to_screen(context, p) for p in path_world]
                        if all(s is not None for s in path_screen):
                            self._arc_label_t = _nearest_t_on_screen_path(path_screen, self._mx, self._my)
                else:
                    # ANGLE mode: once the first line is picked,
                    # live-preview the angle it would form with whichever
                    # second line is currently under the cursor.
                    if self._angle_edge1 is not None:
                        hit = _pick_edge_at_cursor(context, self._mx, self._my, self._snap_cache)
                        self._angle_hover = (
                            _compute_angle_measurement(self._angle_edge1, hit) if hit is not None else None
                        )
                return {'RUNNING_MODAL'}

            if event.type == 'M' and event.value == 'PRESS' and menu_ok:
                self._cycle_mode(context, 1)
                return {'RUNNING_MODAL'}

            if event.type in {'X', 'DEL'} and event.value == 'PRESS' and menu_ok:
                # X or Delete: remove the currently selected measurement(s).
                if self._selected:
                    for i in sorted(self._selected, reverse=True):
                        del self._measurements[i]
                    self._selected = set()
                    context.area.tag_redraw()
                return {'RUNNING_MODAL'}

            if event.type == 'A' and event.value == 'PRESS' and event.ctrl and menu_ok:
                # Ctrl+A: select every measurement placed this session.
                self._selected = set(range(len(self._measurements)))
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}

            if event.type == 'TAB' and event.value == 'PRESS':
                # While a measurement is actively being drawn/targeted
                # (LENGTH's 2nd point pending, ARC's label placement,
                # ANGLE's 2nd line pending, or AREA/RADIUS/DIAMETER
                # currently hovering a valid pick), Tab immediately
                # marks it to be kept the instant it's completed,
                # instead of needing to select it and press Tab again
                # afterward.
                _mid_draw = (
                    (self._mode == 'LENGTH' and self._p1 is not None) or
                    (self._mode == 'ARC' and (self._arc_pending is not None or self._arc_hover is not None)) or
                    (self._mode == 'ANGLE' and self._angle_edge1 is not None) or
                    (self._mode == 'AREA' and self._area_hover is not None) or
                    (self._mode == 'RADIUS' and self._radius_hover is not None) or
                    (self._mode == 'DIAMETER' and self._diameter_hover is not None)
                )
                if _mid_draw:
                    self._pending_kept = True
                    context.area.tag_redraw()
                elif menu_ok and self._selected:
                    # Otherwise (idle, not drawing/targeting anything):
                    # toggle Keep/Not-Keep for the selected measurement(s).
                    for i in self._selected:
                        self._measurements[i]['kept'] = not self._measurements[i].get('kept', False)
                    context.area.tag_redraw()
                return {'RUNNING_MODAL'}

            # ── Up / Down while placing a LENGTH measurement: an
            #    explicit keyboard alternative to the cursor-driven 3rd
            #    click below for choosing which side of the segment the
            #    dimension line, its extension lines, and the
            #    (still-centered) value label are drawn on. Works during
            #    either phase - while still dragging out the distance,
            #    or during the dedicated "choose side" phase after the
            #    2nd click - but note that once the "choose side" phase
            #    has started, any further mouse movement re-derives the
            #    side from cursor position (see MOUSEMOVE above), so
            #    this is only "sticky" as long as the mouse doesn't move
            #    afterward.
            if event.type in {'UP_ARROW', 'DOWN_ARROW'} and event.value == 'PRESS' \
                    and self._mode == 'LENGTH' and self._p1 is not None:
                self._live_side_override = 1.0 if event.type == 'UP_ARROW' else -1.0
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}

            # ── Shift (tap alone) to flip a selected LEN measurement's
            #    dimension-line side - toggles which side of the
            #    measured segment the dimension line, its extension
            #    lines, and the (still-centered) value label are drawn
            #    on: above/below the segment for a horizontal
            #    measurement, left/right of it for a vertical one.
            # Only fires on *releasing* Shift, and only if Shift wasn't
            # actually used to Shift+Click something in the meantime -
            # that way holding Shift to build up a multi-selection (see
            # the Shift+Click handler below) never also flips anything.
            #
            # NOTE: this toggles the stored 'side_override' directly
            # rather than swapping the measurement's p1/p2 endpoints.
            # Swapping endpoints only reliably flips the rendered side
            # for segments where dy != 0 - for a purely horizontal
            # segment, _dim_side() re-derives its sign from the (now
            # reversed) direction vector and ends up flipping right
            # back to the same side, cancelling the swap out. Toggling
            # the override explicitly works the same way regardless of
            # the measurement's orientation.
            if event.type in {'LEFT_SHIFT', 'RIGHT_SHIFT'}:
                if event.value == 'PRESS':
                    self._shift_used_for_click = False
                elif event.value == 'RELEASE':
                    if not self._shift_used_for_click and self._selected:
                        flipped_any = False
                        for i in self._selected:
                            m = self._measurements[i]
                            if m['kind'] == 'LEN':
                                cur = m.get('side_override')
                                if cur is None:
                                    s1 = world_to_screen(context, m['p1'])
                                    s2 = world_to_screen(context, m['p2'])
                                    cur = _dim_side(s1, s2) if (s1 and s2) else 1.0
                                m['side_override'] = -cur
                                flipped_any = True
                        if flipped_any:
                            context.area.tag_redraw()
                    self._shift_used_for_click = False
                return {'RUNNING_MODAL'}

            if event.value != 'PRESS' and event.value != 'DOUBLE_CLICK':
                return {'RUNNING_MODAL'}

            # ── Double-click a measurement's dimension line/arc or its
            #    text label to select it ────────────────────────────
            # Selection ONLY happens via double-click - a single click
            # never selects a measurement, even one landing right on
            # its dimension line, so an ordinary first click is always
            # free to start the active mode's normal placement flow
            # (or land on empty space) without accidentally selecting
            # something. event.value == 'DOUBLE_CLICK' isn't reliably
            # delivered to a running modal operator on every Blender
            # version/platform, so click timing/position is also
            # tracked manually as a fallback - same approach already
            # used below for LENGTH's "double-click an edge" shortcut,
            # but tracked separately so the two double-click gestures
            # don't interfere with each other.
            #
            # Holding Shift while double-clicking toggles that one
            # measurement's membership in the current selection (so
            # several can be built up one at a time - Ctrl+A still
            # grabs all of them in one go); a plain double-click
            # replaces the whole selection with just the one clicked.
            if event.type == 'LEFTMOUSE':
                now = time.time()
                is_double_select = event.value == 'DOUBLE_CLICK' or (
                    self._last_select_click_time is not None
                    and (now - self._last_select_click_time) <= _DOUBLE_CLICK_SEC
                    and math.hypot(self._mx - self._last_select_click_pos[0],
                                    self._my - self._last_select_click_pos[1]) <= _DOUBLE_CLICK_PX
                )
                self._last_select_click_time = None if is_double_select else now
                self._last_select_click_pos  = (self._mx, self._my)

                if is_double_select:
                    hit_i = _find_measurement_at_cursor(context, self._measurements, self._mx, self._my)
                    if hit_i is not None:
                        if event.shift:
                            self._shift_used_for_click = True
                            if hit_i in self._selected:
                                self._selected.discard(hit_i)
                            else:
                                self._selected.add(hit_i)
                        else:
                            # Undo whatever this pair's first click just
                            # started (e.g. LENGTH's first point) so
                            # selecting a measurement never leaves a
                            # half-started new one behind.
                            self._p1 = None
                            self._pending_p2 = None
                            self._awaiting_side = False
                            self._arc_pending = None
                            self._arc_label_t = None
                            self._angle_edge1 = None
                            self._angle_hover = None
                            self._selected = {hit_i}

                        # Double-clicking a dimension's line/arc or its
                        # text label is also how the "CAD Dimension"
                        # N-panel sidebar gets opened/focused, and a
                        # Shift+double-click emptying the selection
                        # (toggling off the last-selected item) is how
                        # it gets closed again - both handled uniformly
                        # by _sync_dimension_panel(), called every tick
                        # from modal(), so nothing needs doing here.
                        context.area.tag_redraw()
                        return {'RUNNING_MODAL'}
                    # Double-click missed every measurement - deselect
                    # (clicking empty space, like most CAD apps), then
                    # fall through and let the mode's own handlers treat
                    # this click normally. _sync_dimension_panel() picks
                    # up the now-empty selection on the next tick.
                    elif self._selected:
                        self._selected = set()
                        context.area.tag_redraw()

            if event.type == 'LEFTMOUSE' and self._mode == 'AREA':
                hit = _pick_area_at_cursor(context, self._mx, self._my)
                if hit is not None:
                    area, center, outline = hit
                    self._measurements.append(
                        {'kind': 'AREA', 'center': center, 'area': area, 'outline': outline,
                         'kept': self._pending_kept}
                    )
                    self._pending_kept = False
                return {'RUNNING_MODAL'}

            if event.type == 'LEFTMOUSE' and self._mode == 'RADIUS':
                hit = _pick_radius_at_cursor(context, self._mx, self._my)
                if hit is not None:
                    center, radius, edge_point = hit
                    self._measurements.append(
                        {'kind': 'RADIUS', 'center': center, 'radius': radius, 'edge': edge_point,
                         'kept': self._pending_kept}
                    )
                    self._pending_kept = False
                return {'RUNNING_MODAL'}

            if event.type == 'LEFTMOUSE' and self._mode == 'DIAMETER':
                hit = _pick_diameter_at_cursor(context, self._mx, self._my)
                if hit is not None:
                    center, radius, edge1, edge2 = hit
                    self._measurements.append(
                        {'kind': 'DIAMETER', 'center': center, 'radius': radius,
                         'edge1': edge1, 'edge2': edge2, 'kept': self._pending_kept}
                    )
                    self._pending_kept = False
                return {'RUNNING_MODAL'}

            if event.type == 'LEFTMOUSE' and self._mode == 'ARC':
                if self._arc_pending is None:
                    # First click: lock in the curve, then wait for a
                    # second click to say where along it the label goes.
                    hit = _pick_curve_at_cursor(context, self._mx, self._my)
                    if hit is not None:
                        self._arc_pending = hit
                        path_world, _length, _closed = hit
                        path_screen = [world_to_screen(context, p) for p in path_world]
                        if all(s is not None for s in path_screen):
                            self._arc_label_t = _nearest_t_on_screen_path(path_screen, self._mx, self._my)
                else:
                    # Second click: drop the label at whichever point on
                    # the curve is nearest the cursor right now - the
                    # label can go anywhere around the curve, but always
                    # sits anchored right on it.
                    path_world, length_world, closed = self._arc_pending
                    self._measurements.append({
                        'kind': 'ARC', 'path': path_world, 'length': length_world,
                        'closed': closed, 'label_t': self._arc_label_t,
                        'kept': self._pending_kept,
                    })
                    self._pending_kept = False
                    self._arc_pending = None
                    self._arc_label_t = None
                return {'RUNNING_MODAL'}

            if event.type == 'LEFTMOUSE' and self._mode == 'ANGLE':
                hit = _pick_edge_at_cursor(context, self._mx, self._my, self._snap_cache)
                if self._angle_edge1 is None:
                    # First click: lock in line 1, wait for line 2.
                    if hit is not None:
                        self._angle_edge1 = hit
                else:
                    # Second click: compute + finalize the angle between
                    # line 1 and whichever line is under the cursor now
                    # (as long as it isn't the very same edge again).
                    if hit is not None and hit != self._angle_edge1:
                        measurement = _compute_angle_measurement(self._angle_edge1, hit)
                        if measurement is not None:
                            apex, ray1_end, ray2_end, angle_rad = measurement
                            self._measurements.append({
                                'kind': 'ANGLE', 'apex': apex,
                                'ray1_end': ray1_end, 'ray2_end': ray2_end,
                                'angle_rad': angle_rad, 'kept': self._pending_kept,
                            })
                            self._pending_kept = False
                    self._angle_edge1 = None
                    self._angle_hover = None
                return {'RUNNING_MODAL'}

            if event.type == 'LEFTMOUSE':
                # Double-click anywhere along an edge: measure that edge's
                # full length end-to-end, instead of the normal two-click
                # flow. We check the OS-reported 'DOUBLE_CLICK' value first,
                # but that isn't reliably delivered to an already-running
                # modal operator on every Blender version/platform, so we
                # also detect it ourselves from click timing + position as
                # a fallback - whichever fires, the effect is the same.
                now = time.time()
                is_double = event.value == 'DOUBLE_CLICK' or (
                    self._last_click_time is not None
                    and (now - self._last_click_time) <= _DOUBLE_CLICK_SEC
                    and math.hypot(self._mx - self._last_click_pos[0],
                                    self._my - self._last_click_pos[1]) <= _DOUBLE_CLICK_PX
                )
                self._last_click_time = None if is_double else now
                self._last_click_pos  = (self._mx, self._my)

                if is_double and not self._awaiting_side:
                    # Undo whatever the first click of this pair just did
                    # (it always looks like an ordinary single click first
                    # - it lands as a normal 1st click, setting self._p1,
                    # before the 2nd click of the pair arrives fast enough
                    # to read as a double-click) and measure the whole
                    # edge under the cursor instead - full length,
                    # endpoint to endpoint, no manual endpoint picking.
                    # Guarded only against the dedicated "choose side"
                    # phase (self._awaiting_side) below, where a rapid
                    # pair of clicks is instead the normal 2nd+3rd clicks
                    # of that distance/side flow, not this shortcut.
                    self._p1 = None
                    self._pending_p2 = None
                    self._awaiting_side = False
                    self._measure_edge_at_cursor(context)
                    return {'RUNNING_MODAL'}

                if self._p1 is None:
                    # 1st click: start a new measurement. Earlier
                    # finished measurements stay visible in
                    # self._measurements - only the in-progress
                    # drag line/label is per-attempt.
                    self._p1 = self._cur.copy()
                    self._pending_p2 = None
                    self._awaiting_side = False
                    self._live_side_override = None
                    self._live_dim_offset = None
                    self._otrack.reset(clear_points=True)
                elif not self._awaiting_side:
                    # 2nd click: lock in the distance and switch to the
                    # "choose side" phase - the mouse no longer moves
                    # the 2nd point, it now only picks which side of
                    # the segment (and how far from it) the dimension
                    # line/extension lines/label are drawn on (see
                    # MOUSEMOVE above), so the user can look at the
                    # result and adjust it before committing. Seed both
                    # from wherever the cursor already is (the same
                    # spot as this very click) so nothing visibly jumps
                    # the instant this phase starts.
                    self._pending_p2 = self._cur.copy()
                    self._awaiting_side = True
                    s1 = world_to_screen(context, self._p1)
                    s2 = world_to_screen(context, self._pending_p2)
                    self._live_side_override = (
                        _side_from_cursor(s1, s2, self._mx, self._my) if (s1 and s2) else None
                    )
                    self._live_dim_offset = (
                        _offset_from_cursor(s1, s2, self._mx, self._my) if (s1 and s2) else None
                    )
                    self._otrack.reset(clear_points=True)
                else:
                    # 3rd click: confirm whichever side and offset are
                    # currently previewed and finish the measurement.
                    dist = (self._pending_p2 - self._p1).length
                    self._measurements.append({'kind': 'LEN', 'p1': self._p1, 'p2': self._pending_p2,
                                                'dist': dist, 'kept': self._pending_kept,
                                                'side_override': self._live_side_override,
                                                'dim_offset': self._live_dim_offset})
                    self._pending_kept = False
                    self._live_side_override = None
                    self._live_dim_offset = None
                    self._p1 = None
                    self._pending_p2 = None
                    self._awaiting_side = False
                    self._otrack.reset(clear_points=True)
                return {'RUNNING_MODAL'}

            if event.type in {'RET', 'NUMPAD_ENTER', 'SPACE'}:
                self.cancel(context)
                return {'FINISHED'}

            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

            return {'RUNNING_MODAL'}
        except Exception:
            # Anything unexpected here must not leave the modal
            # operator dead without restoring the OS cursor - a bare
            # exception would otherwise abort this handler entirely
            # while cursor_modal_set('NONE') (see invoke()) is still
            # in effect, leaving no cursor visible at all.
            self.cancel(context)
            return {'CANCELLED'}


    def _draw(self, context):
        # Everything below that stands in for "the cursor" (OTRACK
        # marker, crosshair, snap marker, coordinate/typed-value
        # tooltip) must only be drawn while the real OS cursor is
        # actually hidden, i.e. the mouse is over the usable viewport -
        # see cursor_fx_visible(). Persisted/committed measurements
        # (draw_measurements_list, etc.) are unaffected and keep
        # drawing regardless.
        show_cursor_fx = cursor_fx_visible(self)

        draw_selected_snap_points(context)
        if show_cursor_fx:
            draw_osnap_tracking(context, self._mx, self._my)

        mx, my = self._mx, self._my
        c2 = world_to_screen(context, self._cur)

        # Draw the crosshair (our stand-in for the OS cursor, which is
        # hidden via cursor_modal_set('NONE') while this tool is
        # active) unconditionally, before any mode-specific overlay
        # drawing below - so a failure in one of those overlays (a bad
        # measurement, an edge case in a curve's geometry, etc.) can
        # never leave the viewport with no visible cursor at all.
        if show_cursor_fx:
            draw_crosshair(context, mx, my, show_box=False)

        LINE_COL   = (1.0, 0.75, 0.15, 1.0)
        POINT_FILL = (1.0, 0.75, 0.15, 0.30)
        POINT_OUT  = (1.0, 0.75, 0.15, 1.0)
        AREA_COL   = (0.35, 0.75, 1.0, 1.0)
        AREA_FILL_COL = (0.35, 0.75, 1.0, 0.45)   # dimmer, in-progress hover preview
        RADIUS_COL      = (0.65, 0.45, 1.0, 1.0)
        RADIUS_FILL_COL = (0.65, 0.45, 1.0, 0.45)   # dimmer, in-progress hover preview
        DIAMETER_COL      = (1.0, 0.45, 0.60, 1.0)
        DIAMETER_FILL_COL = (1.0, 0.45, 0.60, 0.45)   # dimmer, in-progress hover preview
        ARC_COL      = (0.45, 0.85, 1.0, 1.0)
        ARC_FILL_COL = (0.45, 0.85, 1.0, 0.45)   # dimmer, in-progress hover preview
        ANGLE_COL      = (1.0, 0.80, 0.25, 1.0)
        ANGLE_FILL_COL = (1.0, 0.80, 0.25, 0.45)   # dimmer, in-progress hover preview
        ANGLE_SELECTED_COL = (0.25, 0.95, 0.35, 1.0)   # picked line 1, waiting on line 2

        # All previously completed measurements this session - drawn
        # regardless of whether a new one is currently being
        # dragged/hovered, so the user can build up several
        # measurements on screen at once. Shared with
        # CAD_OT_SketchModeManager's overlay via draw_measurements_list()
        # so "kept" measurements render identically once this tool exits.
        try:
            draw_measurements_list(context, self._measurements, self._selected, self._hover_i)

            if self._mode == 'LENGTH':
                # Live in-progress drag line from the first click to the
                # cursor (or, once the 2nd click has locked in the
                # distance, from the first click to that fixed 2nd
                # point instead - see self._awaiting_side). A trailing
                # lock icon shows Tab has marked this one to be kept
                # the instant it's finished (see modal()'s Tab handler).
                _keep_mark = " \U0001f512" if self._pending_kept else ""
                s2_world = self._pending_p2 if self._awaiting_side else self._cur
                s2 = world_to_screen(context, s2_world) if self._awaiting_side else c2
                if self._p1 is not None and s2:
                    s1 = world_to_screen(context, self._p1)
                    if s1:
                        live_dist = (s2_world - self._p1).length
                        _side = (
                            self._live_side_override
                            if self._live_side_override is not None
                            else _dim_side(s1, s2)
                        )
                        draw_measurement_dimension(
                            s1, s2, _format_length(context, live_dist) + _keep_mark,
                            line_col=LINE_COL, point_fill=POINT_FILL, point_outline=POINT_OUT,
                            text_col=(1.0, 1.0, 0.75, 1.0),
                            side=_side,
                            offset=self._live_dim_offset if self._awaiting_side else None,
                        )

                if self._awaiting_side:
                    # 2nd point is locked in - show it as a fixed
                    # square (no live snap marker, since the cursor no
                    # longer targets a new point here; it only steers
                    # which side of - and how far from - the measured
                    # edge the dimension line lands).
                    if s2:
                        draw_vertex_square(s2, 5, fill_col=(1, 0.6, 0.1, 0.25), outline_col=(1, 0.6, 0.1, 1), outline_w=1.8)
                elif c2:
                    if show_cursor_fx:
                        gfx_snap_marker(c2, self._snap_type)
                    draw_vertex_square(c2, 5, fill_col=(1, 0.6, 0.1, 0.25), outline_col=(1, 0.6, 0.1, 1), outline_w=1.8)

                if self._awaiting_side:
                    live_dist = (self._pending_p2 - self._p1).length
                    if show_cursor_fx:
                        draw_dim_rows(
                            context, mx, my,
                            [("Dist:", _format_length(context, live_dist) + _keep_mark, True, False),
                             ("Side/Offset:", "click to set", False, False)],
                        )
                elif self._p1 is not None:
                    live_dist = (self._cur - self._p1).length
                    if show_cursor_fx:
                        draw_dim_rows(
                            context, mx, my,
                            [("Dist:", _format_length(context, live_dist) + _keep_mark, True, False)],
                        )
                elif show_cursor_fx:
                    draw_dim_rows(
                        context, mx, my,
                        [("X:", _format_length(context, self._cur.x), True, False),
                         ("Y:", _format_length(context, self._cur.y), False, False)],
                    )

                if show_cursor_fx:
                    cx, cy = c2 if c2 else (mx, my)
                    draw_crosshair(context, cx, cy, show_box=False)
                gfx_text("MEASURE", 10, 55, 12, (1.0, 0.85, 0.2, 1.0))

            elif self._mode == 'AREA':
                # Live hover preview of whichever object is under the
                # cursor right now - dimmer than a finalized measurement,
                # confirmed with a click. A trailing lock icon shows Tab
                # has marked it to be kept the instant it's clicked.
                if self._area_hover is not None:
                    h_area, h_center, h_outline = self._area_hover
                    sc = world_to_screen(context, h_center)
                    if sc is not None:
                        outline_screen = None
                        if h_outline:
                            outline_screen = [world_to_screen(context, p) for p in h_outline]
                            if any(s is None for s in outline_screen):
                                outline_screen = None
                        _keep_mark = " \U0001f512" if self._pending_kept else ""
                        draw_area_measurement(
                            sc, _format_area(context, h_area) + _keep_mark,
                            outline_screen=outline_screen, line_col=AREA_FILL_COL,
                            text_col=(1.0, 1.0, 0.85, 1.0),
                        )

                gfx_text("MEASURE (Area)", 10, 55, 12, (0.55, 0.85, 1.0, 1.0))

            elif self._mode == 'RADIUS':
                # Live hover preview of whichever circle is under the
                # cursor right now - dimmer than a finalized measurement,
                # confirmed with a click. Line runs from the circle's
                # center out to its circumference, matching a finalized
                # radius measurement's own indicator.
                if self._radius_hover is not None:
                    h_center, h_radius, h_edge = self._radius_hover
                    sc = world_to_screen(context, h_center)
                    se = world_to_screen(context, h_edge)
                    if sc and se:
                        _keep_mark = " \U0001f512" if self._pending_kept else ""
                        draw_radius_measurement(
                            sc, se, _format_length(context, h_radius) + _keep_mark,
                            line_col=RADIUS_FILL_COL, text_col=(1.0, 1.0, 0.85, 1.0),
                        )

                gfx_text("MEASURE (Radius)", 10, 55, 12, (0.75, 0.55, 1.0, 1.0))

            elif self._mode == 'DIAMETER':
                # Live hover preview of whichever circle is under the
                # cursor right now - dimmer than a finalized measurement,
                # confirmed with a click. The dashed line spans the full
                # diameter (edge-to-edge, through the centre); the label
                # on the line shows the diameter value (2 * radius).
                if self._diameter_hover is not None:
                    h_center, h_radius, h_edge1, h_edge2 = self._diameter_hover
                    se1 = world_to_screen(context, h_edge1)
                    se2 = world_to_screen(context, h_edge2)
                    if se1 and se2:
                        _keep_mark = " \U0001f512" if self._pending_kept else ""
                        draw_diameter_measurement(
                            se1, se2, _format_length(context, h_radius * 2.0) + _keep_mark,
                            line_col=DIAMETER_FILL_COL, text_col=(1.0, 1.0, 0.85, 1.0),
                        )

                gfx_text("MEASURE (Diameter)", 10, 55, 12, (1.0, 0.55, 0.70, 1.0))

            elif self._mode == 'ARC':
                if self._arc_pending is not None:
                    # A curve is already picked; now placing its label -
                    # it snaps to the nearest point along that same curve
                    # to the cursor, until the next click drops it there.
                    _keep_mark = " \U0001f512" if self._pending_kept else ""
                    path_world, length_world, closed = self._arc_pending
                    path_screen = [world_to_screen(context, p) for p in path_world]
                    if all(s is not None for s in path_screen):
                        draw_arc_length_measurement(
                            path_screen, _format_length(context, length_world) + _keep_mark,
                            closed=closed, label_t=self._arc_label_t,
                            line_col=ARC_COL, text_col=(1.0, 1.0, 1.0, 1.0),
                        )
                    gfx_text("MEASURE (Circumference)  -  click to place the label", 10, 55, 12, (0.55, 0.85, 1.0, 1.0))
                else:
                    # Live hover preview of whichever curve is under the
                    # cursor right now - dimmer than a finalized
                    # measurement. Click it to lock it in, then click
                    # again anywhere to place its label.
                    if self._arc_hover is not None:
                        h_path, h_length, h_closed = self._arc_hover
                        path_screen = [world_to_screen(context, p) for p in h_path]
                        if all(s is not None for s in path_screen):
                            _keep_mark = " \U0001f512" if self._pending_kept else ""
                            draw_arc_length_measurement(
                                path_screen, _format_length(context, h_length) + _keep_mark,
                                closed=h_closed,
                                line_col=ARC_FILL_COL, text_col=(1.0, 1.0, 0.85, 1.0),
                            )

                    gfx_text("MEASURE (Circumference)", 10, 55, 12, (0.55, 0.85, 1.0, 1.0))

            else:  # ANGLE mode
                # Highlight the first line green once it's picked, so
                # it's clear it registered and stays visible while
                # hunting for the second one.
                if self._angle_edge1 is not None:
                    e1s = world_to_screen(context, self._angle_edge1[0])
                    e1e = world_to_screen(context, self._angle_edge1[1])
                    if e1s and e1e:
                        gfx_lines([e1s, e1e], ANGLE_SELECTED_COL, 2.4)

                # Live hover preview of the angle line 1 would form with
                # whichever second line is under the cursor right now -
                # dimmer than a finalized measurement, confirmed with a
                # click. The dashed arc sweeps between the two lines; the
                # label in the gap between them shows the angle.
                if self._angle_hover is not None:
                    h_apex, h_ray1, h_ray2, h_angle = self._angle_hover
                    sa = world_to_screen(context, h_apex)
                    s1 = world_to_screen(context, h_ray1)
                    s2 = world_to_screen(context, h_ray2)
                    if sa and s1 and s2:
                        _keep_mark = " \U0001f512" if self._pending_kept else ""
                        draw_angle_measurement(
                            sa, s1, s2, _format_angle(context, h_angle) + _keep_mark,
                            line_col=ANGLE_FILL_COL, text_col=(1.0, 1.0, 0.85, 1.0),
                        )

                gfx_text("MEASURE (Angle)", 10, 55, 12, (1.0, 0.80, 0.25, 1.0))

        except Exception:
            pass

        # Measurement-type indicator (or the dropdown menu, while Alt
        # is held) - drawn last so it stays on top of everything else.
        if self._alt_held:
            self._draw_method_menu(context)
        else:
            self._draw_method_indicator(context)
