# ─────────────────────────────────────────────────────────────────────
#  operators/tool_fillet.py
#  CAD_OT_DrawFillet – AutoCAD-style FILLET command.
#
#  Workflow
#  ────────
#    1. Click the first line/edge.
#    2. Click the second line/edge.
#    3. A tangent circular arc of the current radius is previewed live
#       between them; type a value (or scroll / +/-) to change the
#       radius while watching the preview update.
#    4. Enter commits: both edges are trimmed/extended so the arc
#       connects tangentially to each, the remaining portions of the
#       original edges are preserved, and the arc is added as its own
#       CAD_Fillet object.
#    5. After committing, the tool stays active so more fillets can be
#       created without re-selecting it - Esc/right-click backs out one
#       stage at a time, then exits.
#
#  Two corner cases are handled:
#    • Intersecting (non-parallel) edges  -> classic corner fillet:
#      trim both lines back to the tangent points and insert the arc
#      that replaces the sharp corner.
#    • Parallel edges (e.g. two long edges facing each other) -> a
#      tangent semicircle ("U-turn") capping the two facing ends. The
#      radius for this case is geometrically fixed at half the gap
#      between the lines, exactly like AutoCAD's FILLET on parallel
#      lines - a typed radius is ignored and the forced value is shown
#      instead.
# ─────────────────────────────────────────────────────────────────────

import math
import bmesh
import bpy
from mathutils import Vector

from ..utils.math_utils import world_to_screen, mouse_to_3d, build_snap_cache, apply_snaps, parse_typed_length, format_length
from ..utils.otrack import SnapAcquisitionManager, update_tracking, resolve_typed_distance_ray
from ..utils.cad_objects import create_cad_object, pick_reference_object
from .. import state as _st
from ..utils.cursor_util import (
    update_modal_cursor, restore_modal_cursor, cursor_fx_visible, init_modal_cursor,
    guarded_modal,
    handle_ui_handoff,
    reset_tpanel_to_select,
)
from ..gfx.primitives import gfx_dot, gfx_text, gfx_lines, gfx_dashed_line, gfx_loop
from ..gfx.markers    import draw_crosshair, gfx_snap_marker, draw_osnap_tracking, draw_selected_snap_points
from ..gfx.dims       import draw_float_dim, draw_dim_rows

_EDGE_PICK_RADIUS_PX = 10
_MIN_RADIUS          = 1e-4
_PARALLEL_EPS        = 1e-4   # |cross of unit directions| below this = parallel
_DEGENERATE_EPS      = 1e-3   # radians - corner angle too close to 0/pi


# ── small 2D helpers ────────────────────────────────────────────────

def _closest_point_on_segment_2d(p, a, b):
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 < 1e-9:
        return a, 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / length2
    t = max(0.0, min(1.0, t))
    return (ax + dx * t, ay + dy * t), t


def _line_isect_2d(A: Vector, B: Vector, C: Vector, D: Vector):
    """Infinite-line (not segment-clipped) X/Y intersection of line
    A-B and line C-D. Returns the intersection point, or None if the
    lines are parallel."""
    d1x, d1y = B.x - A.x, B.y - A.y
    d2x, d2y = D.x - C.x, D.y - C.y
    denom = d1x * d2y - d1y * d2x
    if abs(denom) < 1e-9:
        return None
    t = ((C.x - A.x) * d2y - (C.y - A.y) * d2x) / denom
    return Vector((A.x + d1x * t, A.y + d1y * t, 0.0))


def _arc_points(center, radius, a_start, sweep, segs=32):
    pts = []
    for i in range(segs + 1):
        t = a_start + sweep * (i / segs)
        pts.append(Vector((center.x + radius * math.cos(t),
                            center.y + radius * math.sin(t), 0.0)))
    return pts


# ── fillet geometry ──────────────────────────────────────────────────

class _FilletResult:
    __slots__ = ('valid', 'reason', 'radius', 'center', 'a_start', 'sweep',
                 'tangent1', 'tangent2', 'far1', 'far2', 'kind',
                 'changed1', 'changed2')

    def __init__(self, **kw):
        self.valid = False
        self.reason = ''
        self.changed1 = True
        self.changed2 = True
        for k, v in kw.items():
            setattr(self, k, v)


def _select_far_near(A: Vector, B: Vector, P: Vector, t_click: float):
    """Decide which endpoint is the far/kept side and which is the
    near/discarded side.

    If P genuinely sits between A and B (the edge straddles the
    intersection on both sides - an X-crossing), the click position
    picks which ray is kept, resolving the ambiguity - matching
    Trim/Extend's click-side convention.

    Otherwise (the ordinary case: the edge only reaches toward the
    intersection from one direction, e.g. a normal corner where one
    endpoint already sits at/near P), there is only one sensible
    choice - the farther endpoint is always kept, regardless of click
    position. A naive 0..1 split of the edge's own parametric length
    would otherwise flip this incorrectly: clicking anywhere in the
    "corner half" of an ordinary long edge (very easy to do, since
    that's naturally where the user is looking) would discard the
    long, correct side and keep a near-zero-length stub instead."""
    d = B - A
    dn = d.normalized()
    sA = (A - P).dot(dn)
    sB = (B - P).dot(dn)
    if (sA >= 0) == (sB >= 0):
        return (A, B) if (A - P).length >= (B - P).length else (B, A)
    click_pt = A + d * t_click
    sC = (click_pt - P).dot(dn)
    return (A, B) if (sC >= 0) == (sA >= 0) else (B, A)


def _compute_corner_fillet(A1, B1, A2, B2, radius, t1_click=0.0, t2_click=0.0):
    """Two intersecting (non-parallel) edges -> classic tangent-arc
    corner fillet. A1/B1 and A2/B2 are the full world-space endpoints
    of each picked edge.

    t1_click/t2_click are the parametric click positions (0..1) along
    each edge, from the same cursor/hover position used to pick it -
    used only for radius > 0 (see below).

    radius == 0 is a special case: there is only one sensible meeting
    point (the intersection itself), so click position is ignored and
    each edge's near/far side is picked purely by distance to the
    intersection - whichever endpoint is naturally closer gets moved
    to meet the other edge exactly (extending or trimming the minimum
    amount needed); the farther endpoint is never touched. This avoids
    accidentally trimming the "wrong" (longer) side of an edge just
    because of where it happened to be clicked, especially when both
    edges are close to the same length and either side could
    plausibly look like "the one you clicked".

    radius > 0 keeps click-side selection: whichever endpoint the
    click is nearer to is trimmed back to the fillet arc (the "corner"
    side); the other endpoint is the kept, anchor side. This lets the
    user choose which of an edge's two ends the fillet applies to, the
    same click-side convention Trim/Extend already use, instead of
    always picking whichever end happens to sit closer to the
    intersection point."""
    P = _line_isect_2d(A1, B1, A2, B2)
    if P is None:
        return None  # caller routes to the parallel branch instead

    if radius < 0.0:
        return _FilletResult(reason="Radius must be zero or greater")

    if radius == 0.0:
        # sharp corner: both edges' near endpoints are adjusted so
        # they meet at exactly the same point P - extending a short
        # edge out to P, or pulling an overshooting edge back to P.
        # Nothing else about either edge changes: only that one
        # endpoint moves, the rest of the line is untouched, and the
        # two edges remain separate entities that simply share a
        # coincident point.
        #
        # Which endpoint of each edge is "near" (adjusted) vs "far"
        # (kept, untouched) is decided the same click-based way as a
        # positive-radius fillet - the click determines the corner
        # side whenever an edge genuinely straddles the intersection
        # on both sides (an ambiguous X-crossing); an ordinary edge
        # that only reaches the intersection from one direction always
        # keeps its one sensible far side regardless of click position.
        far1, near1 = _select_far_near(A1, B1, P, t1_click)
        far2, near2 = _select_far_near(A2, B2, P, t2_click)

        if (far1 - P).length < 1e-9 or (far2 - P).length < 1e-9:
            return _FilletResult(reason="Edge endpoint coincides with the intersection point")

        ray1 = (far1 - P).normalized()
        ray2 = (far2 - P).normalized()
        cosang = max(-1.0, min(1.0, ray1.dot(ray2)))
        theta = math.acos(cosang)
        if theta < _DEGENERATE_EPS or theta > math.pi - _DEGENERATE_EPS:
            return _FilletResult(reason="Edges are collinear - no corner to fillet")

        changed1 = (near1 - P).length > 1e-9
        changed2 = (near2 - P).length > 1e-9

        return _FilletResult(
            valid=True, kind='SHARP', radius=0.0, center=None,
            a_start=0.0, sweep=0.0, tangent1=P, tangent2=P,
            far1=far1, far2=far2, changed1=changed1, changed2=changed2,
        )

    far1, near1 = _select_far_near(A1, B1, P, t1_click)
    far2, near2 = _select_far_near(A2, B2, P, t2_click)

    d_far1 = (far1 - P).length
    d_far2 = (far2 - P).length
    if d_far1 < 1e-9 or d_far2 < 1e-9:
        return _FilletResult(reason="Edge endpoint coincides with the intersection point")

    ray1 = (far1 - P).normalized()
    ray2 = (far2 - P).normalized()
    cosang = max(-1.0, min(1.0, ray1.dot(ray2)))
    theta = math.acos(cosang)

    if theta < _DEGENERATE_EPS or theta > math.pi - _DEGENERATE_EPS:
        return _FilletResult(reason="Edges are collinear - no corner to fillet")

    tan_half = math.tan(theta / 2)
    if tan_half < 1e-9:
        return _FilletResult(reason="Edges are collinear - no corner to fillet")

    d_tangent = radius / tan_half

    tangent1 = P + ray1 * d_tangent
    tangent2 = P + ray2 * d_tangent

    bisector = (ray1 + ray2)
    if bisector.length < 1e-9:
        return _FilletResult(reason="Edges are collinear - no corner to fillet")
    bisector = bisector.normalized()
    dist_center = radius / math.sin(theta / 2)
    center = P + bisector * dist_center

    a1 = math.atan2(tangent1.y - center.y, tangent1.x - center.x)
    a2 = math.atan2(tangent2.y - center.y, tangent2.x - center.x)
    diff = (a2 - a1) % (2 * math.pi)
    if diff > math.pi:
        diff -= 2 * math.pi

    return _FilletResult(
        valid=True, kind='CORNER', radius=radius, center=center,
        a_start=a1, sweep=diff, tangent1=tangent1, tangent2=tangent2,
        far1=far1, far2=far2,
    )


def _compute_parallel_fillet(A1, B1, A2, B2):
    """Two parallel edges -> tangent semicircle ("U-turn") joining
    their facing ends. Radius is geometrically forced to half the gap
    between the lines - it cannot be chosen freely."""
    d1 = (B1 - A1)
    if d1.length < 1e-9:
        return _FilletResult(reason="Edge has zero length")
    d1n = d1.normalized()

    # facing ends = the endpoint pair (one per edge) that are closest
    pairs = [(A1, A2), (A1, B2), (B1, A2), (B1, B2)]
    near1, near2 = min(pairs, key=lambda pr: (pr[0] - pr[1]).length)
    far1 = B1 if near1 is A1 else A1
    far2 = B2 if near2 is A2 else A2

    d2 = (B2 - A2)
    d2n = d2.normalized() if d2.length > 1e-9 else d1n
    if d1n.dot(d2n) < 0:
        d2n = -d2n

    normal = Vector((-d1n.y, d1n.x, 0.0)).normalized()
    dist = abs((A2 - A1).dot(normal))
    if dist < 1e-6:
        return _FilletResult(reason="Edges are coincident - nothing to fillet")
    radius = dist / 2.0

    # align both facing ends to the same axial coordinate so the
    # connecting segment is purely perpendicular (a true tangent cap)
    t_cap = max(near1.dot(d1n), near2.dot(d1n))
    tangent1 = near1 + d1n * (t_cap - near1.dot(d1n))
    tangent2 = near2 + d2n * (t_cap - near2.dot(d1n))

    center = (tangent1 + tangent2) / 2.0
    a1 = math.atan2(tangent1.y - center.y, tangent1.x - center.x)
    a2 = math.atan2(tangent2.y - center.y, tangent2.x - center.x)

    # bulge the cap outward, away from the kept segments (the kept
    # portions sit on the +d1n side of the tangent points, at t_cap)
    diff = (a2 - a1) % (2 * math.pi)
    mid_t = a1 + diff / 2
    mid_pt = Vector((center.x + radius * math.cos(mid_t),
                      center.y + radius * math.sin(mid_t), 0.0))
    if (mid_pt - center).dot(d1n) > 0:
        diff -= 2 * math.pi

    return _FilletResult(
        valid=True, kind='PARALLEL', radius=radius, center=center,
        a_start=a1, sweep=diff, tangent1=tangent1, tangent2=tangent2,
        far1=far1, far2=far2,
    )


def _compute_fillet(edge1, edge2, radius):
    """edge1/edge2: (obj, edge_index, A, B, t) world-space endpoints
    plus the parametric click/hover position (0..1) along the edge,
    used to pick which end/corner the fillet applies to.
    Returns a _FilletResult (check .valid)."""
    _o1, _e1, A1, B1, t1 = edge1
    _o2, _e2, A2, B2, t2 = edge2

    d1 = (B1 - A1)
    d2 = (B2 - A2)
    if d1.length < 1e-9 or d2.length < 1e-9:
        return _FilletResult(reason="Edge has zero length")
    d1n, d2n = d1.normalized(), d2.normalized()
    cross = d1n.x * d2n.y - d1n.y * d2n.x

    if abs(cross) < _PARALLEL_EPS:
        return _compute_parallel_fillet(A1, B1, A2, B2)

    result = _compute_corner_fillet(A1, B1, A2, B2, radius, t1, t2)
    if result is None:
        return _compute_parallel_fillet(A1, B1, A2, B2)
    return result


# ── operator ─────────────────────────────────────────────────────────

class CAD_OT_DrawFillet(bpy.types.Operator):
    """Round a corner between two edges (or cap two parallel edges)
    with a tangent circular arc, trimming/extending both as needed."""
    bl_idname  = "cad.draw_fillet"
    bl_label   = "CAD Fillet"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._stage  = 'RADIUS'  # 'RADIUS' -> 'PICK1' -> 'PICK2'
        self._radius = max(0.0, _st.fillet_radius)
        self._inp    = ''
        self._error  = ''

        self._edge1 = None   # (obj, edge_index, A, B, t) | None
        self._edge2 = None
        self._hover = None   # (obj, edge_index) | None
        self._result = None  # _FilletResult | None

        # pick-radius by clicking 2 points (empty space or an existing
        # point), only available while self._stage == 'RADIUS'
        self._radius_pt1 = None
        self._radius_inp = ''
        self._radius_typed_ray = None
        self._raw = Vector((0.0, 0.0, 0.0))
        self._cur = Vector((0.0, 0.0, 0.0))
        self._snap_type = 'FREE'
        self._snap_cache = build_snap_cache(context)
        self._otrack = SnapAcquisitionManager()
        self._otrack.reset(clear_points=True)

        self._mx, self._my = event.mouse_region_x, event.mouse_region_y
        self._ref_obj = _st.sketch_ref_obj or pick_reference_object(context)

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "FILLET"
        context.window_manager.modal_handler_add(self)
        self._update_header(context)
        return {'RUNNING_MODAL'}

    # ── cleanup ──────────────────────────────────────────────────────
    def _cleanup(self, context):
        try:
            bpy.types.SpaceView3D.draw_handler_remove(self._handler, 'WINDOW')
        except ValueError:
            pass
        restore_modal_cursor(self, context)
        context.area.header_text_set(None)
        context.scene.cad_settings.active_tool = "SELECT"
        self._otrack.reset(clear_points=True)
        _st.snap_cache_dirty = True
        reset_tpanel_to_select(self, context)
        if context.area:
            context.area.tag_redraw()

    def cancel(self, context):
        self._cleanup(context)

    def _update_radius_cur(self):
        """Resolve self._cur for the pick-radius sub-mode: a typed
        distance (if any) overrides the raw snapped cursor position,
        same convention as the other draw tools' dynamic input."""
        if self._radius_inp and self._radius_typed_ray is not None:
            try:
                val = float(self._radius_inp)
                origin, direction = self._radius_typed_ray
                self._cur = origin + direction * val
                return
            except ValueError:
                pass
        self._cur = self._raw.copy()

    # ── edge picking ─────────────────────────────────────────────────
    def _pick_edge_at_cursor(self, context):
        """Nearest edge to the cursor, screen-space, within
        _EDGE_PICK_RADIUS_PX. Returns (obj, edge_index, A, B, t) or
        None. t is the parametric position (0..1) of the closest point
        along the edge to the cursor - used to decide which end/corner
        the fillet applies to. This t is computed in world space (via
        a projection of the cursor's 3D position onto the edge's own
        line), not screen space - a purely screen-space parameter is
        skewed by perspective foreshortening on long edges, making one
        end effectively unreachable/hard to click precisely, especially
        if the edge recedes into the distance."""
        mx, my = self._mx, self._my
        best_d = float(_EDGE_PICK_RADIUS_PX)
        best   = None
        for obj in context.view_layer.objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            mw = obj.matrix_world
            mesh = obj.data
            for ei, e in enumerate(mesh.edges):
                a3 = mw @ mesh.vertices[e.vertices[0]].co
                b3 = mw @ mesh.vertices[e.vertices[1]].co
                a2 = world_to_screen(context, a3)
                b2 = world_to_screen(context, b3)
                if a2 is None or b2 is None:
                    continue
                (cx, cy), _t = _closest_point_on_segment_2d((mx, my), a2, b2)
                d = math.hypot(cx - mx, cy - my)
                if d < best_d:
                    best_d = d
                    best   = (obj, ei, a3, b3)
        if best is None:
            return None

        obj, ei, a3, b3 = best
        t = self._world_click_t(context, a3, b3)
        return (obj, ei, a3, b3, t)

    def _world_click_t(self, context, a3, b3):
        """Parametric position (0..1) of the cursor along world-space
        segment a3->b3, projected via the cursor's actual 3D position
        on the sketch plane rather than screen pixels."""
        world_click = mouse_to_3d(context, self._mx, self._my)
        if world_click is None:
            return 0.5
        seg = b3 - a3
        seg_len2 = seg.length_squared
        if seg_len2 < 1e-12:
            return 0.5
        t = (world_click - a3).dot(seg) / seg_len2
        return max(0.0, min(1.0, t))

    # ── modal ────────────────────────────────────────────────────────
    def modal(self, context, event):
        # Any unhandled exception inside _modal() must still pop this
        # tool's own cursor push before the operator ends, or the OS
        # cursor is left invisible outside the viewport with no visible
        # cause - see guarded_modal()'s docstring.
        return guarded_modal(self, context, event, self._modal)

    def _modal(self, context, event):
        if handle_ui_handoff(self, context, event):
            return {'PASS_THROUGH'}
        update_modal_cursor(self, context, event)
        if not context.scene.cad_settings.sketch_mode:
            self.cancel(context)
            return {'CANCELLED'}

        context.area.tag_redraw()
        self._mx = event.mouse_region_x
        self._my = event.mouse_region_y

        if event.type == 'MIDDLEMOUSE':
            return {'PASS_THROUGH'}

        # Let mouse wheel reach Blender (viewport zoom, Toolbar scroll)
        # instead of being swallowed by this modal's catch-all return.
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN'}:
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            if self._stage == 'RADIUS':
                raw = mouse_to_3d(context, self._mx, self._my)
                if raw:
                    self._raw, self._snap_type = apply_snaps(
                        context, raw, self._snap_cache,
                        snap_filter=_st.selection.get_active_stypes(),
                    )
                    update_tracking(self._otrack, self._raw, self._snap_type)
                    self._update_radius_cur()
                    if self._radius_pt1 is not None:
                        self._radius = max(_MIN_RADIUS, (self._cur - self._radius_pt1).length)
                        self._update_header(context)
                return {'RUNNING_MODAL'}
            if self._stage in {'PICK1', 'PICK2'}:
                hit = self._pick_edge_at_cursor(context)
                if hit is not None and self._edge1 is not None:
                    o1, e1, _a, _b, _t1 = self._edge1
                    if hit[0] is o1 and hit[1] == e1:
                        hit = None
                self._hover = hit
            return {'RUNNING_MODAL'}

        # ── pick the radius by clicking 2 points (empty space or an
        #    existing point/snap) instead of typing it. The distance
        #    between the clicks becomes the radius - clicking never
        #    creates or modifies geometry, it's purely a measurement ──
        if self._stage == 'RADIUS' and event.value == 'PRESS':
            ch = event.ascii
            if ch and ch in '0123456789.' and self._radius_pt1 is not None:
                if not self._radius_inp:
                    direction = self._raw - self._radius_pt1
                    if direction.length < 1e-6:
                        direction = Vector((1.0, 0.0, 0.0))
                    self._radius_typed_ray = (self._radius_pt1, direction.normalized())
                self._radius_inp += ch
                self._update_radius_cur()
                self._radius = max(_MIN_RADIUS, (self._cur - self._radius_pt1).length)
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if event.type == 'BACK_SPACE' and self._radius_inp:
                self._radius_inp = self._radius_inp[:-1]
                if not self._radius_inp:
                    self._radius_typed_ray = None
                self._update_radius_cur()
                if self._radius_pt1 is not None:
                    self._radius = max(_MIN_RADIUS, (self._cur - self._radius_pt1).length)
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if event.type == 'LEFTMOUSE':
                if self._radius_pt1 is None:
                    self._radius_pt1 = self._cur.copy()
                    self._radius_inp = ''
                    self._radius_typed_ray = None
                    self._inp = ''
                else:
                    self._radius = max(_MIN_RADIUS, (self._cur - self._radius_pt1).length)
                    _st.fillet_radius = self._radius
                    self._radius_pt1       = None
                    self._radius_inp       = ''
                    self._radius_typed_ray = None
                    self._otrack.reset(clear_points=True)
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if event.type in {'RET', 'NUMPAD_ENTER'} and self._radius_pt1 is not None:
                self._radius = max(_MIN_RADIUS, (self._cur - self._radius_pt1).length)
                _st.fillet_radius = self._radius
                self._radius_pt1       = None
                self._radius_inp       = ''
                self._radius_typed_ray = None
                self._otrack.reset(clear_points=True)
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if self._radius_pt1 is not None and event.type in {'RIGHTMOUSE', 'ESC'}:
                self._radius_pt1       = None
                self._radius_inp       = ''
                self._radius_typed_ray = None
                self._update_header(context)
                return {'RUNNING_MODAL'}

        # ── typed radius entry - editable any time before the second
        #    edge is actually clicked (RADIUS prompt, or while picking
        #    edge 1 / edge 2) ────────────────────────────────────────
        if self._stage in {'RADIUS', 'PICK1', 'PICK2'} and event.value == 'PRESS':
            ch = event.ascii
            if ch and ch in '0123456789.':
                self._inp += ch
                try:
                    self._radius = max(0.0, parse_typed_length(context, self._inp))
                except ValueError:
                    pass
                self._error = ''
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if event.type == 'BACK_SPACE' and self._inp:
                self._inp = self._inp[:-1]
                if self._inp:
                    try:
                        self._radius = max(0.0, parse_typed_length(context, self._inp))
                    except ValueError:
                        pass
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if self._stage == 'RADIUS' and event.type in {'RET', 'NUMPAD_ENTER'}:
                _st.fillet_radius = self._radius
                self._inp = ''
                self._stage = 'PICK1'
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if self._stage == 'RADIUS' and event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if self._stage == 'PICK1':
                hit = self._pick_edge_at_cursor(context)
                if hit is not None:
                    self._edge1 = hit
                    self._hover = None
                    self._error = ''
                    self._stage = 'PICK2'
                    self._update_header(context)
                return {'RUNNING_MODAL'}
            if self._stage == 'PICK2':
                hit = self._pick_edge_at_cursor(context)
                if hit is not None and not (hit[0] is self._edge1[0] and hit[1] == self._edge1[1]):
                    self._edge2 = hit
                    self._hover = None
                    self._result = _compute_fillet(self._edge1, self._edge2, self._radius)
                    if self._result.valid:
                        self._commit(context)
                        # stay active - ready for another fillet, with
                        # nothing selected/picked
                        self._edge1 = None
                        self._edge2 = None
                        self._result = None
                        self._error = ''
                        self._stage = 'PICK1'
                        self._update_header(context)
                        return {'RUNNING_MODAL'}
                    self._error = self._result.reason
                    self._edge2 = None
                    self._result = None
                    self._update_header(context)
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        if event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS' and self._stage in {'PICK1', 'PICK2'}:
            self.cancel(context)
            return {'CANCELLED'}

        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self.cancel(context)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ── commit: trim both edges, insert the arc ─────────────────────
    def _commit(self, context):
        result = self._result
        edge1, edge2 = self._edge1, self._edge2

        _st.fillet_radius = result.radius

        by_obj = {}
        for (obj, ei, _A, _B, _t), far_pt, tangent_pt, changed in (
            (edge1, result.far1, result.tangent1, result.changed1),
            (edge2, result.far2, result.tangent2, result.changed2),
        ):
            if result.kind == 'SHARP' and not changed:
                continue  # already reaches/crosses the intersection - leave it alone
            by_obj.setdefault(obj, []).append((ei, far_pt, tangent_pt))

        for obj, items in by_obj.items():
            self._trim_edges_on_object(obj, items)

        if result.kind != 'SHARP':
            pts = _arc_points(result.center, result.radius, result.a_start,
                               result.sweep, segs=32)
            verts = [(p.x, p.y, 0.0) for p in pts]
            edges = [(i, i + 1) for i in range(len(verts) - 1)]
            create_cad_object(
                context, "CAD_Fillet", verts, edges, "fillet",
                {"radius": result.radius, "kind": result.kind},
                reference_obj=edge1[0],
            )

        for o in context.selected_objects:
            o.select_set(False)
        context.view_layer.objects.active = None

    def _trim_edges_on_object(self, obj, items):
        """items: [(edge_index, far_point_world, tangent_point_world), ...]
        for edges that live on this single object. Each picked edge is
        replaced by a new edge running from its kept ('far') endpoint
        to a new vertex at the tangent point - the original edge is
        removed, its vertices left in place (matching the Trim tool's
        convention)."""
        mesh = obj.data
        mw = obj.matrix_world
        mw_inv = mw.inverted()

        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()

        # resolve every target edge/far-vertex first, before any new
        # geometry is added - adding verts/edges invalidates the
        # index-based lookup tables, so indices must not be re-read
        # from bm.edges[] after the first mutation
        resolved = []
        for edge_index, far_pt, tangent_pt in items:
            if edge_index >= len(bm.edges):
                continue
            bm_edge = bm.edges[edge_index]
            v0, v1 = bm_edge.verts
            w0 = mw @ v0.co
            w1 = mw @ v1.co
            far_vert = v0 if (w0 - far_pt).length <= (w1 - far_pt).length else v1
            resolved.append((bm_edge, far_vert, tangent_pt))

        to_delete = []
        for bm_edge, far_vert, tangent_pt in resolved:
            new_v = bm.verts.new(mw_inv @ tangent_pt)
            if not bm.edges.get((far_vert, new_v)):
                bm.edges.new((far_vert, new_v))
            to_delete.append(bm_edge)

        if to_delete:
            bmesh.ops.delete(bm, geom=to_delete, context='EDGES')

        bm.to_mesh(mesh)
        bm.free()
        mesh.update()
        _st.snap_cache_dirty = True

    # ── header ───────────────────────────────────────────────────────
    def _update_header(self, context):
        if self._stage == 'RADIUS' and self._radius_pt1 is not None:
            rd_disp = self._radius_inp + "▌" if self._radius_inp else f"{self._radius:.4f}"
            text = f"FILLET: click the second point for radius  ·  R {rd_disp} m  ·  Esc=back"
            context.area.header_text_set(text)
            return

        r_disp = self._inp + "▌" if self._inp else f"{self._radius:.4f}"
        if self._stage == 'RADIUS':
            text = f"FILLET: type a radius, or click 2 points to measure it, then Enter  ·  Esc=cancel"
        elif self._stage == 'PICK1':
            text = f"FILLET: Radius {r_disp} m (type to change)  ·  click the first line/edge  ·  Enter/Esc=exit"
        else:
            err = f"  ·  {self._error}" if self._error else ""
            text = f"FILLET: Radius {r_disp} m (type to change)  ·  click the second line/edge{err}  ·  Enter/Esc=exit"
        context.area.header_text_set(text)

    # ── draw callback ────────────────────────────────────────────────
    _COL_KEEP    = (0.37, 0.92, 0.67, 1.0)   # mint - edges/segments that remain
    _COL_REMOVE  = (1.00, 0.20, 0.20, 0.85)  # red - portion being trimmed away
    _COL_ARC_OK  = (1.00, 1.00, 0.20, 0.95)  # yellow - valid arc preview
    _COL_ARC_BAD = (1.00, 0.30, 0.30, 0.60)  # dim red - invalid preview
    _COL_HOVER   = (0.30, 0.90, 1.00, 0.95)  # cyan - hovered pick candidate
    _COL_PICKED  = (0.37, 0.92, 0.67, 1.0)   # mint - confirmed pick

    def _draw(self, context):
        try:
            self._draw_impl(context)
        except Exception as e:
            print(f"[CAD] tool_fillet draw error: {e}")

    def _draw_edge_pick(self, context, obj, ei, col, width):
        mesh = obj.data
        mw = obj.matrix_world
        e = mesh.edges[ei]
        a3 = mw @ mesh.vertices[e.vertices[0]].co
        b3 = mw @ mesh.vertices[e.vertices[1]].co
        a2 = world_to_screen(context, a3)
        b2 = world_to_screen(context, b3)
        if a2 and b2:
            gfx_lines([a2, b2], col, width)

    _PICK_BOX_HALF = 5

    def _draw_pick_box(self, context):
        """Small square outline at the cursor, no crosshair lines -
        the edge-picking cursor while in PICK1/PICK2."""
        s = self._PICK_BOX_HALF
        mx, my = self._mx, self._my
        gfx_loop(
            [(mx - s, my - s), (mx + s, my - s),
             (mx + s, my + s), (mx - s, my + s)],
            (1.0, 1.0, 1.0, 0.90), 1.2,
        )

    def _draw_impl(self, context):
        # The pick box / crosshair / OTRACK marker / snap marker /
        # typed-value tooltip all stand in for "the cursor" - only draw
        # them while the real OS cursor is actually hidden (mouse over
        # the usable viewport).
        show_cursor_fx = cursor_fx_visible(self)

        if show_cursor_fx:
            if self._stage in {'PICK1', 'PICK2'}:
                self._draw_pick_box(context)
            else:
                draw_crosshair(context, self._mx, self._my, None, show_box=False)

        if self._stage == 'RADIUS':
            draw_selected_snap_points(context)
            if show_cursor_fx:
                draw_osnap_tracking(context, self._mx, self._my)
            c2 = world_to_screen(context, self._cur)
            if show_cursor_fx and c2:
                gfx_snap_marker(c2, self._snap_type)

            if self._radius_pt1 is not None:
                p1_2d = world_to_screen(context, self._radius_pt1)
                if p1_2d:
                    gfx_dot(p1_2d, self._COL_ARC_OK, 6)
                if p1_2d and c2:
                    gfx_dashed_line(p1_2d, c2, col=self._COL_ARC_OK, dash_len=6, space_len=4, w=1.6)
                dist = (self._cur - self._radius_pt1).length
                disp = self._radius_inp + "▌" if self._radius_inp else f"{dist:.4f}"
                if show_cursor_fx:
                    draw_dim_rows(context, self._mx, self._my, [("R:", disp + " m", True, False)])
            elif show_cursor_fx:
                disp = self._inp + "▌" if self._inp else format_length(context, self._radius)
                draw_float_dim(self._mx + 60, self._my, f"R {disp}", active=True, locked=bool(self._inp))
            return

        if self._edge1 is not None:
            self._draw_edge_pick(context, self._edge1[0], self._edge1[1], self._COL_PICKED, 3.0)

        if self._stage == 'PICK1':
            if self._hover is not None:
                self._draw_edge_pick(context, self._hover[0], self._hover[1], self._COL_HOVER, 3.0)
            disp = self._inp + "▌" if self._inp else format_length(context, self._radius)
            draw_float_dim(self._mx + 60, self._my, f"R {disp}", active=True, locked=bool(self._inp))
            return

        # ── PICK2: live preview against whatever edge is hovered ─────
        if self._hover is None:
            disp = self._inp + "▌" if self._inp else format_length(context, self._radius)
            draw_float_dim(self._mx + 60, self._my, f"R {disp}", active=True, locked=bool(self._inp))
            return

        result = _compute_fillet(self._edge1, self._hover, self._radius)
        if not result.valid:
            self._draw_edge_pick(context, self._hover[0], self._hover[1], self._COL_HOVER, 3.0)
            disp = self._inp + "▌" if self._inp else format_length(context, self._radius)
            draw_float_dim(self._mx + 60, self._my, f"R {disp}", active=True, locked=bool(self._inp))
            gfx_text(result.reason, 10, 55, 12, (1.0, 0.35, 0.35, 1.0))
            return

        for (obj, ei, A, B, _t), far_pt, tangent_pt, changed in (
            (self._edge1, result.far1, result.tangent1, result.changed1),
            (self._hover, result.far2, result.tangent2, result.changed2),
        ):
            if result.kind == 'SHARP' and not changed:
                a2 = world_to_screen(context, A)
                b2 = world_to_screen(context, B)
                if a2 and b2:
                    gfx_lines([a2, b2], self._COL_KEEP, 3.0)
                continue
            a2 = world_to_screen(context, A)
            b2 = world_to_screen(context, B)
            if a2 and b2:
                gfx_dashed_line(a2, b2, col=self._COL_REMOVE, dash_len=5, space_len=4, w=1.4)
            f2 = world_to_screen(context, far_pt)
            t2 = world_to_screen(context, tangent_pt)
            if f2 and t2:
                gfx_lines([f2, t2], self._COL_KEEP, 3.0)
                gfx_dot(t2, self._COL_ARC_OK, 5)

        if result.kind == 'SHARP':
            corner_2d = world_to_screen(context, result.tangent1)
            if corner_2d:
                gfx_dot(corner_2d, self._COL_ARC_OK, 4)
        else:
            pts = _arc_points(result.center, result.radius, result.a_start, result.sweep, segs=32)
            pts_2d = [world_to_screen(context, p) for p in pts]
            pts_2d = [p for p in pts_2d if p is not None]
            if len(pts_2d) > 1:
                gfx_lines(pts_2d, self._COL_ARC_OK, 2.4)
            c2 = world_to_screen(context, result.center)
            if c2:
                gfx_dot(c2, self._COL_ARC_OK, 3)

        draw_float_dim(self._mx + 60, self._my, f"R {format_length(context, result.radius)}", active=True, locked=True)
