# ─────────────────────────────────────────────────────────────────────
#  operators/tool_chamfer.py
#  CAD_OT_DrawChamfer – AutoCAD-style CHAMFER command.
#
#  Workflow
#  ────────
#    1. Click the Chamfer Tool toolbar button to activate it.
#    2. A distance prompt is shown immediately; type a value (or click
#       2 points to measure it) and press Enter.
#    3. Click the first line/edge.
#    4. Click the second line/edge.
#    5. Both edges are trimmed/extended back to points that sit exactly
#       *distance* away from their intersection, and a straight segment
#       is inserted connecting those two points - the sharp corner is
#       replaced by a single diagonal cut. Nothing is selected
#       afterwards, and the tool stays active for the next chamfer
#       (Enter/Esc exits).
#
#  Parallel edges are handled the same way Fillet handles them: the
#  two facing ends are aligned and joined directly (the equivalent of
#  a 0-bulge "fillet" cap) - a typed distance is ignored for this case
#  since the gap itself fixes where the connecting segment sits.
# ─────────────────────────────────────────────────────────────────────

import math
import bmesh
import bpy
from mathutils import Vector

from ..utils.math_utils import world_to_screen, mouse_to_3d, build_snap_cache, apply_snaps, parse_typed_length, format_length
from ..utils.otrack import SnapAcquisitionManager, update_tracking
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
_MIN_DIST            = 1e-4
_PARALLEL_EPS        = 1e-4
_DEGENERATE_EPS      = 1e-3


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
    d1x, d1y = B.x - A.x, B.y - A.y
    d2x, d2y = D.x - C.x, D.y - C.y
    denom = d1x * d2y - d1y * d2x
    if abs(denom) < 1e-9:
        return None
    t = ((C.x - A.x) * d2y - (C.y - A.y) * d2x) / denom
    return Vector((A.x + d1x * t, A.y + d1y * t, 0.0))


def _rotate_2d(v: Vector, angle_rad: float) -> Vector:
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    return Vector((v.x * c - v.y * s, v.x * s + v.y * c, 0.0))


# ── chamfer geometry ─────────────────────────────────────────────────

class _ChamferResult:
    __slots__ = ('valid', 'reason', 'distance', 'angle', 'cut1', 'cut2', 'far1', 'far2', 'kind',
                 'changed1', 'changed2')

    def __init__(self, **kw):
        self.valid = False
        self.reason = ''
        self.angle = None
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


def _compute_corner_chamfer(A1, B1, A2, B2, distance, angle_deg=None, t1_click=0.0, t2_click=0.0):
    """Two intersecting (non-parallel) edges -> straight chamfer cut.

    distance == 0 is a special case: there's only one sensible meeting
    point (the intersection itself), so click position is ignored and
    each edge's near/far side is picked purely by distance to the
    intersection - whichever endpoint is naturally closer gets
    extended to meet the other edge exactly; the farther endpoint, and
    any edge that already reaches or crosses the intersection, are
    never trimmed/shortened - only ever extended when genuinely short.

    distance > 0 uses click-side selection: whichever endpoint the
    click is nearer to is trimmed back to the chamfer cut (the
    "corner" side); the other endpoint is the kept, anchor side - the
    same click-side convention Trim/Extend/Fillet use, instead of
    always picking whichever end happens to sit closer to P.

    cut1 always sits *distance* away from the intersection along
    edge1 (the kept/'far' side). cut2 is placed one of two ways:
      - angle_deg is None (default): the same *distance* along edge2
        (equal-leg chamfer) - the original/preserved behaviour.
      - angle_deg given: cut2 is where the chamfer line - starting at
        cut1, angled *angle_deg* degrees from edge1 - crosses edge2's
        line, exactly like AutoCAD's Distance/Angle chamfer method."""
    P = _line_isect_2d(A1, B1, A2, B2)
    if P is None:
        return None  # caller routes to the parallel branch instead

    if distance < 0.0:
        return _ChamferResult(reason="Distance must be zero or greater")

    if distance == 0.0:
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
        # positive-distance chamfer - the click determines the corner
        # side whenever an edge genuinely straddles the intersection
        # on both sides (an ambiguous X-crossing); an ordinary edge
        # that only reaches the intersection from one direction always
        # keeps its one sensible far side regardless of click position.
        far1, near1 = _select_far_near(A1, B1, P, t1_click)
        far2, near2 = _select_far_near(A2, B2, P, t2_click)

        if (far1 - P).length < 1e-9 or (far2 - P).length < 1e-9:
            return _ChamferResult(reason="Edge endpoint coincides with the intersection point")

        ray1 = (far1 - P).normalized()
        ray2 = (far2 - P).normalized()
        cosang = max(-1.0, min(1.0, ray1.dot(ray2)))
        theta = math.acos(cosang)
        if theta < _DEGENERATE_EPS or theta > math.pi - _DEGENERATE_EPS:
            return _ChamferResult(reason="Edges are collinear - no corner to chamfer")

        changed1 = (near1 - P).length > 1e-9
        changed2 = (near2 - P).length > 1e-9

        return _ChamferResult(
            valid=True, kind='SHARP', distance=0.0, angle=None,
            cut1=P, cut2=P, far1=far1, far2=far2,
            changed1=changed1, changed2=changed2,
        )

    far1, near1 = _select_far_near(A1, B1, P, t1_click)
    far2, near2 = _select_far_near(A2, B2, P, t2_click)

    if (far1 - P).length < 1e-9 or (far2 - P).length < 1e-9:
        return _ChamferResult(reason="Edge endpoint coincides with the intersection point")

    ray1 = (far1 - P).normalized()
    ray2 = (far2 - P).normalized()
    cosang = max(-1.0, min(1.0, ray1.dot(ray2)))
    theta = math.acos(cosang)

    if theta < _DEGENERATE_EPS or theta > math.pi - _DEGENERATE_EPS:
        return _ChamferResult(reason="Edges are collinear - no corner to chamfer")

    cut1 = P + ray1 * distance

    if angle_deg is None:
        cut2 = P + ray2 * distance
        return _ChamferResult(
            valid=True, kind='CORNER', distance=distance, angle=None,
            cut1=cut1, cut2=cut2, far1=far1, far2=far2,
        )

    if angle_deg <= 0.0 or angle_deg >= 180.0:
        return _ChamferResult(reason="Angle must be between 0 and 180 degrees")

    # base direction = back along edge1, into the corner; rotate it
    # toward edge2 by angle_deg (same signed side ray2 sits on)
    base = -ray1
    base_ang = math.atan2(base.y, base.x)
    ray2_ang = math.atan2(ray2.y, ray2.x)
    diff = (ray2_ang - base_ang + math.pi) % (2 * math.pi) - math.pi
    sign = 1.0 if diff >= 0 else -1.0
    chamfer_dir = _rotate_2d(base, sign * math.radians(angle_deg))

    cut2 = _line_isect_2d(cut1, cut1 + chamfer_dir, A2, B2)
    if cut2 is None:
        return _ChamferResult(reason="Angle is parallel to the second edge")
    if (cut2 - P).dot(ray2) <= 1e-6:
        return _ChamferResult(reason="Angle produces an invalid chamfer for these edges")

    return _ChamferResult(
        valid=True, kind='CORNER', distance=distance, angle=angle_deg,
        cut1=cut1, cut2=cut2, far1=far1, far2=far2,
    )


def _compute_parallel_chamfer(A1, B1, A2, B2):
    """Two parallel edges -> align the facing ends and join them with
    a straight perpendicular segment (equivalent to Fillet's 0-radius
    parallel cap). The gap itself fixes the cut points."""
    d1 = (B1 - A1)
    if d1.length < 1e-9:
        return _ChamferResult(reason="Edge has zero length")
    d1n = d1.normalized()

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
        return _ChamferResult(reason="Edges are coincident - nothing to chamfer")

    t_cap = max(near1.dot(d1n), near2.dot(d1n))
    cut1 = near1 + d1n * (t_cap - near1.dot(d1n))
    cut2 = near2 + d2n * (t_cap - near2.dot(d1n))

    return _ChamferResult(
        valid=True, kind='PARALLEL', distance=(cut1 - cut2).length / 2.0,
        cut1=cut1, cut2=cut2, far1=far1, far2=far2,
    )


def _compute_chamfer(edge1, edge2, distance, angle_deg=None):
    """edge1/edge2: (obj, edge_index, A, B, t) world-space endpoints
    plus the parametric click/hover position (0..1) along the edge,
    used to pick which end/corner the chamfer applies to (for
    distance > 0 - distance == 0 ignores it, see
    _compute_corner_chamfer). Returns a _ChamferResult (check .valid)."""
    _o1, _e1, A1, B1, t1 = edge1
    _o2, _e2, A2, B2, t2 = edge2

    d1 = (B1 - A1)
    d2 = (B2 - A2)
    if d1.length < 1e-9 or d2.length < 1e-9:
        return _ChamferResult(reason="Edge has zero length")
    d1n, d2n = d1.normalized(), d2.normalized()
    cross = d1n.x * d2n.y - d1n.y * d2n.x

    if abs(cross) < _PARALLEL_EPS:
        return _compute_parallel_chamfer(A1, B1, A2, B2)

    result = _compute_corner_chamfer(A1, B1, A2, B2, distance, angle_deg, t1, t2)
    if result is None:
        return _compute_parallel_chamfer(A1, B1, A2, B2)
    return result


# ── operator ─────────────────────────────────────────────────────────

class CAD_OT_DrawChamfer(bpy.types.Operator):
    """Replace a sharp corner between two edges with a straight
    diagonal segment at a fixed distance, trimming/extending both
    edges as needed."""
    bl_idname  = "cad.draw_chamfer"
    bl_label   = "CAD Chamfer"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._context   = context   # kept for helpers that don't take context directly (unit-aware parsing, etc.)
        self._stage    = 'DISTANCE'  # 'DISTANCE' -> 'PICK1' -> 'PICK2'
        self._distance = max(0.0, _st.chamfer_distance)
        self._inp    = ''
        self._error  = ''

        self._edge1  = None
        self._edge2  = None
        self._hover  = None
        self._result = None

        # optional Distance/Angle chamfer method (Shift opens angle
        # entry, only available before the first edge is selected) -
        self._angle = None
        self._angle_inp = ''
        self._editing_angle = False

        self._dist_pt1 = None
        self._dist_inp = ''
        self._dist_typed_ray = None
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
        context.scene.cad_settings.active_tool = "CHAMFER"
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

    def _update_dist_cur(self):
        if self._dist_inp and self._dist_typed_ray is not None:
            try:
                val = parse_typed_length(self._context, self._dist_inp)
                origin, direction = self._dist_typed_ray
                self._cur = origin + direction * val
                return
            except ValueError:
                pass
        self._cur = self._raw.copy()

    # ── edge picking ─────────────────────────────────────────────────
    def _pick_edge_at_cursor(self, context):
        """Nearest edge to the cursor, screen-space, within
        _EDGE_PICK_RADIUS_PX. Returns (obj, edge_index, A, B, t) or
        None. t is the parametric click position (0..1) along the
        edge, used to decide which end/corner the chamfer applies to.
        It is computed in world space (projecting the cursor's actual
        3D position onto the edge's own line), not screen space - a
        purely screen-space parameter is skewed by perspective
        foreshortening on long edges, making one end effectively
        unreachable/hard to click precisely."""
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
        self._context = context   # kept in sync every event, for helpers that don't take context directly
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
            if self._stage == 'DISTANCE':
                raw = mouse_to_3d(context, self._mx, self._my)
                if raw:
                    self._raw, self._snap_type = apply_snaps(
                        context, raw, self._snap_cache,
                        snap_filter=_st.selection.get_active_stypes(),
                    )
                    update_tracking(self._otrack, self._raw, self._snap_type)
                    self._update_dist_cur()
                    if self._dist_pt1 is not None:
                        self._distance = max(_MIN_DIST, (self._cur - self._dist_pt1).length)
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

        # ── pick the distance by clicking 2 points instead of typing ─
        if self._stage == 'DISTANCE' and event.value == 'PRESS':
            ch = event.ascii
            if ch and ch in '0123456789.' and self._dist_pt1 is not None:
                if not self._dist_inp:
                    direction = self._raw - self._dist_pt1
                    if direction.length < 1e-6:
                        direction = Vector((1.0, 0.0, 0.0))
                    self._dist_typed_ray = (self._dist_pt1, direction.normalized())
                self._dist_inp += ch
                self._update_dist_cur()
                self._distance = max(_MIN_DIST, (self._cur - self._dist_pt1).length)
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if event.type == 'BACK_SPACE' and self._dist_inp:
                self._dist_inp = self._dist_inp[:-1]
                if not self._dist_inp:
                    self._dist_typed_ray = None
                self._update_dist_cur()
                if self._dist_pt1 is not None:
                    self._distance = max(_MIN_DIST, (self._cur - self._dist_pt1).length)
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if event.type == 'LEFTMOUSE':
                if self._dist_pt1 is None:
                    self._dist_pt1 = self._cur.copy()
                    self._dist_inp = ''
                    self._dist_typed_ray = None
                    self._inp = ''
                else:
                    self._distance = max(_MIN_DIST, (self._cur - self._dist_pt1).length)
                    _st.chamfer_distance = self._distance
                    self._dist_pt1       = None
                    self._dist_inp       = ''
                    self._dist_typed_ray = None
                    self._otrack.reset(clear_points=True)
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if event.type in {'RET', 'NUMPAD_ENTER'} and self._dist_pt1 is not None:
                self._distance = max(_MIN_DIST, (self._cur - self._dist_pt1).length)
                _st.chamfer_distance = self._distance
                self._dist_pt1       = None
                self._dist_inp       = ''
                self._dist_typed_ray = None
                self._otrack.reset(clear_points=True)
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if self._dist_pt1 is not None and event.type in {'RIGHTMOUSE', 'ESC'}:
                self._dist_pt1       = None
                self._dist_inp       = ''
                self._dist_typed_ray = None
                self._update_header(context)
                return {'RUNNING_MODAL'}

        # ── Shift opens the Angle input box - only before the first
        #    edge is selected (DISTANCE or PICK1 stage) ───────────────
        if (self._stage in {'DISTANCE', 'PICK1'} and not self._editing_angle and self._dist_pt1 is None
                and event.type in {'LEFT_SHIFT', 'RIGHT_SHIFT'} and event.value == 'PRESS'):
            self._editing_angle = True
            self._angle_inp = ''
            self._update_header(context)
            return {'RUNNING_MODAL'}

        if self._editing_angle and event.value == 'PRESS':
            ch = event.ascii
            if ch and ch in '0123456789.':
                self._angle_inp += ch
                try:
                    self._angle = float(self._angle_inp)
                except ValueError:
                    pass
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if event.type == 'BACK_SPACE' and self._angle_inp:
                self._angle_inp = self._angle_inp[:-1]
                if self._angle_inp:
                    try:
                        self._angle = float(self._angle_inp)
                    except ValueError:
                        pass
                else:
                    self._angle = None  # back to the equal-distance method
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if event.type in {'RET', 'NUMPAD_ENTER', 'RIGHTMOUSE', 'ESC'}:
                self._editing_angle = False
                self._angle_inp = ''
                self._update_header(context)
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        # ── typed distance entry - editable any time before the
        #    second edge is actually clicked ─────────────────────────
        if self._stage in {'DISTANCE', 'PICK1', 'PICK2'} and event.value == 'PRESS':
            ch = event.ascii
            if ch and ch in '0123456789.':
                self._inp += ch
                try:
                    self._distance = max(0.0, parse_typed_length(context, self._inp))
                except ValueError:
                    pass
                self._error = ''
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if event.type == 'BACK_SPACE' and self._inp:
                self._inp = self._inp[:-1]
                if self._inp:
                    try:
                        self._distance = max(0.0, parse_typed_length(context, self._inp))
                    except ValueError:
                        pass
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if self._stage == 'DISTANCE' and event.type in {'RET', 'NUMPAD_ENTER'}:
                _st.chamfer_distance = self._distance
                self._inp = ''
                self._stage = 'PICK1'
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if self._stage == 'DISTANCE' and event.type in {'RIGHTMOUSE', 'ESC'}:
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
                    self._result = _compute_chamfer(self._edge1, self._edge2, self._distance, self._angle)
                    if self._result.valid:
                        self._commit(context)
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

    # ── commit: trim both edges, insert the chamfer segment ──────────
    def _commit(self, context):
        result = self._result
        edge1, edge2 = self._edge1, self._edge2

        _st.chamfer_distance = result.distance

        by_obj = {}
        for (obj, ei, _A, _B, _t), far_pt, cut_pt, changed in (
            (edge1, result.far1, result.cut1, result.changed1),
            (edge2, result.far2, result.cut2, result.changed2),
        ):
            if result.kind == 'SHARP' and not changed:
                continue  # already reaches/crosses the intersection - leave it alone
            by_obj.setdefault(obj, []).append((ei, far_pt, cut_pt))

        for obj, items in by_obj.items():
            self._trim_edges_on_object(obj, items)

        if result.kind != 'SHARP':
            verts = [(result.cut1.x, result.cut1.y, 0.0), (result.cut2.x, result.cut2.y, 0.0)]
            edges = [(0, 1)]
            create_cad_object(
                context, "CAD_Chamfer", verts, edges, "chamfer",
                {"distance": result.distance, "kind": result.kind},
                reference_obj=edge1[0],
            )

        for o in context.selected_objects:
            o.select_set(False)
        context.view_layer.objects.active = None

    def _trim_edges_on_object(self, obj, items):
        mesh = obj.data
        mw = obj.matrix_world
        mw_inv = mw.inverted()

        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()

        resolved = []
        for edge_index, far_pt, cut_pt in items:
            if edge_index >= len(bm.edges):
                continue
            bm_edge = bm.edges[edge_index]
            v0, v1 = bm_edge.verts
            w0 = mw @ v0.co
            w1 = mw @ v1.co
            far_vert = v0 if (w0 - far_pt).length <= (w1 - far_pt).length else v1
            resolved.append((bm_edge, far_vert, cut_pt))

        to_delete = []
        for bm_edge, far_vert, cut_pt in resolved:
            new_v = bm.verts.new(mw_inv @ cut_pt)
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
        if self._editing_angle:
            a_disp = self._angle_inp + "▌" if self._angle_inp else (f"{self._angle:.2f}" if self._angle is not None else "")
            text = f"CHAMFER: enter angle {a_disp}°, then Enter (empty=equal-distance)  ·  Esc=back"
            context.area.header_text_set(text)
            return

        if self._stage == 'DISTANCE' and self._dist_pt1 is not None:
            dd_disp = self._dist_inp + "▌" if self._dist_inp else f"{self._distance:.4f}"
            text = f"CHAMFER: click the second point for distance  ·  D {dd_disp} m  ·  Esc=back"
            context.area.header_text_set(text)
            return

        d_disp = self._inp + "▌" if self._inp else f"{self._distance:.4f}"
        ang_disp = f"  ·  Angle {self._angle:.2f}°" if self._angle is not None else ""
        if self._stage == 'DISTANCE':
            text = (f"CHAMFER: type a distance, or click 2 points to measure it, then Enter{ang_disp}  ·  "
                     f"Shift=set angle  ·  Esc=cancel")
        elif self._stage == 'PICK1':
            text = (f"CHAMFER: Distance {d_disp} m (type to change){ang_disp}  ·  "
                     f"click the first line/edge  ·  Shift=set angle  ·  Enter/Esc=exit")
        else:
            err = f"  ·  {self._error}" if self._error else ""
            text = (f"CHAMFER: Distance {d_disp} m{ang_disp} (type to change)  ·  "
                     f"click the second line/edge{err}  ·  Enter/Esc=exit")
        context.area.header_text_set(text)

    # ── draw callback ────────────────────────────────────────────────
    _COL_KEEP    = (0.37, 0.92, 0.67, 1.0)
    _COL_REMOVE  = (1.00, 0.20, 0.20, 0.85)
    _COL_CUT_OK  = (1.00, 1.00, 0.20, 0.95)
    _COL_HOVER   = (0.30, 0.90, 1.00, 0.95)
    _COL_PICKED  = (0.37, 0.92, 0.67, 1.0)

    def _draw(self, context):
        try:
            self._draw_impl(context)
        except Exception as e:
            print(f"[CAD] tool_chamfer draw error: {e}")

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

        if self._editing_angle:
            if show_cursor_fx:
                a_disp = self._angle_inp + "▌" if self._angle_inp else (f"{self._angle:.2f}" if self._angle is not None else "-")
                draw_dim_rows(context, self._mx, self._my, [("Angle:", a_disp + "°", True, False)])
            return

        if self._stage == 'DISTANCE':
            draw_selected_snap_points(context)
            if show_cursor_fx:
                draw_osnap_tracking(context, self._mx, self._my)
            c2 = world_to_screen(context, self._cur)
            if show_cursor_fx and c2:
                gfx_snap_marker(c2, self._snap_type)

            if self._dist_pt1 is not None:
                p1_2d = world_to_screen(context, self._dist_pt1)
                if p1_2d:
                    gfx_dot(p1_2d, self._COL_CUT_OK, 6)
                if p1_2d and c2:
                    gfx_dashed_line(p1_2d, c2, col=self._COL_CUT_OK, dash_len=6, space_len=4, w=1.6)
                dist = (self._cur - self._dist_pt1).length
                disp = self._dist_inp + "▌" if self._dist_inp else f"{dist:.4f}"
                if show_cursor_fx:
                    draw_dim_rows(context, self._mx, self._my, [("D:", disp + " m", True, False)])
            elif show_cursor_fx:
                disp = self._inp + "▌" if self._inp else format_length(context, self._distance)
                draw_float_dim(self._mx + 60, self._my, f"D {disp}", active=True, locked=bool(self._inp))
            return

        if self._edge1 is not None:
            self._draw_edge_pick(context, self._edge1[0], self._edge1[1], self._COL_PICKED, 3.0)

        if self._stage == 'PICK1':
            if self._hover is not None:
                self._draw_edge_pick(context, self._hover[0], self._hover[1], self._COL_HOVER, 3.0)
            disp = self._inp + "▌" if self._inp else format_length(context, self._distance)
            draw_float_dim(self._mx + 60, self._my, f"D {disp}", active=True, locked=bool(self._inp))
            return

        # ── PICK2: live preview against whatever edge is hovered ─────
        if self._hover is None:
            disp = self._inp + "▌" if self._inp else format_length(context, self._distance)
            draw_float_dim(self._mx + 60, self._my, f"D {disp}", active=True, locked=bool(self._inp))
            return

        result = _compute_chamfer(self._edge1, self._hover, self._distance, self._angle)
        if not result.valid:
            self._draw_edge_pick(context, self._hover[0], self._hover[1], self._COL_HOVER, 3.0)
            disp = self._inp + "▌" if self._inp else format_length(context, self._distance)
            draw_float_dim(self._mx + 60, self._my, f"D {disp}", active=True, locked=bool(self._inp))
            gfx_text(result.reason, 10, 55, 12, (1.0, 0.35, 0.35, 1.0))
            return

        for (obj, ei, A, B, _t), far_pt, cut_pt, changed in (
            (self._edge1, result.far1, result.cut1, result.changed1),
            (self._hover, result.far2, result.cut2, result.changed2),
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
            c2p = world_to_screen(context, cut_pt)
            if f2 and c2p:
                gfx_lines([f2, c2p], self._COL_KEEP, 3.0)
                gfx_dot(c2p, self._COL_CUT_OK, 5)

        if result.kind == 'SHARP':
            corner_2d = world_to_screen(context, result.cut1)
            if corner_2d:
                gfx_dot(corner_2d, self._COL_CUT_OK, 4)
        else:
            c1_2d = world_to_screen(context, result.cut1)
            c2_2d = world_to_screen(context, result.cut2)
            if c1_2d and c2_2d:
                gfx_lines([c1_2d, c2_2d], self._COL_CUT_OK, 2.4)

        d_label = f"D {format_length(context, result.distance)}"
        if result.angle is not None:
            d_label += f"  A {result.angle:.2f}°"
        draw_float_dim(self._mx + 60, self._my, d_label, active=True, locked=True)
