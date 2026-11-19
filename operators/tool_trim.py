# ─────────────────────────────────────────────────────────────────────
#  operators/tool_trim.py  [MODIFIED WITH LASSO TRIM]
#  CAD_OT_SketchTrim – AutoCAD-style TRIM command, Line-Tool-style
#  interaction: no pre-selection step at all, and it draws a live
#  preview exactly like the Line tool does.
#
#  THREE trimming methods, chosen automatically:
#
#  Method 1 - Click Trim
#  ────────────────────
#  Click directly on an edge: removes that edge segment at cursor position.
#
#  Method 2 - Crossing Trim (Fence)
#  ────────────────────────────────
#  Click empty space: draws a straight fence line.
#
#  Method 3 - LASSO TRIM
#  ─────────────────────
#  Press LMB and drag (instead of a plain click) anywhere - on an
#  edge or on empty space - to draw a freehand path, Blender
#  Lasso-Select / Grease-Pencil style. Every edge the path crosses is
#  previewed live and trimmed at the intersection the moment the
#  button is released. A plain press+release with (almost) no
#  movement still behaves exactly like Method 1/2 above - only
#  crossing a small drag threshold turns the gesture into a lasso.
#
#  Method 4 - BOUNDARY TRIM
#  ────────────────────────
#  Shift+Click an edge (or Alt+Click anywhere on an object) to toggle
#  it as a boundary - add as many boundary edges/objects as needed,
#  and Shift/Alt+Click an already-selected one again to deselect it.
#  Then click, drag for a lasso, or click empty space for a fence
#  line on any other connected object to remove the portion outside
#  the boundaries - it's trimmed exactly at the nearest boundary
#  crossing, keeping the side that remains. Boundaries themselves are
#  never modified. Esc clears all boundaries.
#
#  Curved boundaries (Circle/Arc/Spline/multi-segment Line/mesh edge)
#  ────────────────────────────────────────────────────────────────
#  Every curved shape in this addon is drawn as a mesh of many short
#  straight edges. All four methods above walk the full connected
#  polyline chain a clicked/crossed edge belongs to (not just that one
#  micro-edge), find every intersection along the *whole* chain, and
#  remove exactly the interval between the two real crossings either
#  side of the click/crossing - so trimming a Circle, Arc, or Spline
#  removes the correct arc, not one tiny polygon sliver. A straight
#  Line is just a 1-edge chain, so this is a pure generalisation -
#  behaviour for plain lines is unchanged.
#
# ─────────────────────────────────────────────────────────────────────
import math
import bmesh
import bpy
from mathutils import Vector

from ..utils.math_utils import world_to_screen, mouse_to_3d
from .. import state as _st
from ..utils.cursor_util import (
    update_modal_cursor, restore_modal_cursor, cursor_fx_visible, init_modal_cursor,
    guarded_modal,
    handle_ui_handoff,
    reset_tpanel_to_select,
)
from ..gfx.primitives import gfx_dot, gfx_text, gfx_dashed_line, gfx_lines
from ..gfx.markers    import draw_crosshair


_EDGE_PICK_RADIUS_PX = 10
_DRAG_THRESHOLD_PX   = 4    # screen-px the mouse must move past its
                            # press-down point before a plain click
                            # turns into a freehand lasso drag


def _closest_point_on_segment_2d(p, a, b):
    """Screen-space closest point on segment a-b to point p, and the
    parametric t (0..1) along a-b."""
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


def _segment_intersect_t(A: Vector, B: Vector, C: Vector, D: Vector):
    """2D (X/Y only) segment-segment intersection. Returns t along A->B
    (0..1) if the segments cross, else None."""
    rx, ry = B.x - A.x, B.y - A.y
    sx, sy = D.x - C.x, D.y - C.y
    rxs = rx * sy - ry * sx
    if abs(rxs) < 1e-9:
        return None   # parallel / collinear - no single crossing point
    qpx, qpy = C.x - A.x, C.y - A.y
    t = (qpx * sy - qpy * sx) / rxs
    u = (qpx * ry - qpy * rx) / rxs
    eps = 1e-6
    if -eps <= t <= 1 + eps and -eps <= u <= 1 + eps:
        return max(0.0, min(1.0, t))
    return None


# ── polyline-chain helpers (Circle/Arc/Spline/Rect support) ────────────
#  This addon draws every curved shape - Circle, Arc, Spline, Rect - as
#  a mesh made of many short straight edges. The trim logic below used
#  to treat "the edge you clicked" as the entire unit to intersect and
#  split, which is correct for a Line (usually a single edge) but
#  wrong for a curve: clicking near an intersection only found/removed
#  one microscopic polygon segment, leaving the rest of the curve
#  untouched on both sides instead of the whole arc up to the next
#  real crossing. These helpers walk the *connected chain* of edges
#  the clicked edge belongs to (exactly what Line/Arc/Circle/Spline/
#  Rect each produce as one simple open or closed polyline) so
#  intersections and trims operate on the whole curve, not one
#  fragment of it. A straight Line is just a 1-edge chain, so this is
#  a strict generalisation - line-trimming behaviour is unchanged.

def _chain_for_edge(obj, edge_index):
    """Ordered mesh-vertex-index chain containing edge_index, and
    whether it's closed. Falls back to just that edge's own two
    vertices (open, unchanged old behaviour) if the geometry branches
    (vertex degree > 2 anywhere) or isn't a single simple loop/chain."""
    mesh = obj.data
    e0 = mesh.edges[edge_index]
    adj = {}
    for e in mesh.edges:
        a, b = e.vertices[0], e.vertices[1]
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)

    seed = e0.vertices[0]
    comp = {seed}
    stack = [seed]
    while stack:
        v = stack.pop()
        for n in adj.get(v, []):
            if n not in comp:
                comp.add(n)
                stack.append(n)

    if any(len(adj.get(v, [])) > 2 for v in comp):
        return list(e0.vertices), False

    ends = [v for v in comp if len(adj.get(v, [])) == 1]
    if len(ends) == 2:
        start, closed = ends[0], False
    elif len(ends) == 0:
        start, closed = seed, True
    else:
        return list(e0.vertices), False

    order = [start]
    visited = {start}
    prev, cur = None, start
    while True:
        nxt = None
        for n in adj.get(cur, []):
            if n == prev:
                continue
            if n == order[0] and len(order) > 1:
                nxt = 'CLOSE'
                break
            if n not in visited:
                nxt = n
                break
        if nxt is None:
            break
        if nxt == 'CLOSE':
            closed = True
            break
        order.append(nxt)
        visited.add(nxt)
        prev, cur = cur, nxt

    if len(order) != len(comp):
        return list(e0.vertices), False  # branches - not a simple chain

    return order, closed


def _chain_point_at(pts, cum, closed, s):
    """World-space point at arc-length s (clamped) along a chain."""
    n = len(pts)
    total = cum[-1]
    s = max(0.0, min(total, s))
    segs = range(n) if closed else range(n - 1)
    for i in segs:
        if cum[i + 1] >= s - 1e-9:
            a, b = pts[i], pts[(i + 1) % n]
            seg_len = cum[i + 1] - cum[i]
            t = 0.0 if seg_len < 1e-9 else (s - cum[i]) / seg_len
            return a.lerp(b, t)
    return pts[-1 if not closed else 0]


def _chain_slice_points(pts, cum, closed, s_from, s_to):
    """World-space polyline tracing the chain from arc-length s_from to
    s_to, including any original chain vertices strictly in between -
    so a highlighted/removed interval on a curve is drawn following
    the actual curve, not a straight chord across it."""
    n = len(pts)
    out = [_chain_point_at(pts, cum, closed, s_from)]
    for i, cs in enumerate(cum[:-1]):
        if s_from + 1e-6 < cs < s_to - 1e-6:
            out.append(pts[i % n])
    out.append(_chain_point_at(pts, cum, closed, s_to))
    return out


def _chain_interval_points(pts, cum, closed, s_from, s_to):
    """Like _chain_slice_points, but for a CLOSED chain where s_to is
    "before" s_from (i.e. this interval is the arc that wraps through
    the seam vertex back to the start) it traces from s_from to the
    end of the loop and then from the start back to s_to, instead of
    treating the seam as a hard boundary. This is what lets a circle
    with two real intersections trim as one continuous arc through the
    seam, rather than always stopping dead at the arbitrary mesh start
    vertex."""
    if closed and s_to <= s_from + 1e-9:
        total_len = cum[-1]
        part1 = _chain_slice_points(pts, cum, closed, s_from, total_len)
        part2 = _chain_slice_points(pts, cum, closed, 0.0, s_to)
        if part2:
            part2 = part2[1:]  # drop duplicate seam point
        return part1 + part2
    return _chain_slice_points(pts, cum, closed, s_from, s_to)


def _chain_self_intersections(pts, cum, closed):
    """Cut points where the chain crosses ITSELF (a spline looping back
    over an earlier part of its own path). Adjacent segments (which
    always share an endpoint) are skipped since that shared vertex
    isn't a real crossing; every other pair of segments is tested, and
    when they cross, BOTH arc-length positions are added as cut points
    - the chain visits that physical location at two different points
    along its length, and either one is a valid place to cut."""
    n = len(pts)
    segs = range(n) if closed else range(n - 1)
    seg_list = list(segs)
    out = set()
    for a_pos, i in enumerate(seg_list):
        A, B = pts[i], pts[(i + 1) % n]
        for k in seg_list[a_pos + 1:]:
            if k == i:
                continue
            if k == (i + 1) % n or i == (k + 1) % n:
                continue  # adjacent - shares a vertex, not a real crossing
            C, D = pts[k], pts[(k + 1) % n]
            t = _segment_intersect_t(A, B, C, D)
            if t is None:
                continue
            t = max(0.0, min(1.0, t))
            out.add(cum[i] + t * (cum[i + 1] - cum[i]))
            u = _segment_intersect_t(C, D, A, B)
            if u is not None:
                u = max(0.0, min(1.0, u))
                out.add(cum[k] + u * (cum[k + 1] - cum[k]))
    return out


def _chain_geometry(obj, edge_index):
    """(vert_order, closed, world_pts, cumulative_arc_lengths,
    total_length) for the connected chain containing edge_index."""
    vert_order, closed = _chain_for_edge(obj, edge_index)
    mesh = obj.data
    mw = obj.matrix_world
    pts = [mw @ mesh.vertices[i].co for i in vert_order]
    n = len(pts)
    cum = [0.0]
    segs = range(n) if closed else range(n - 1)
    for i in segs:
        a, b = pts[i], pts[(i + 1) % n]
        cum.append(cum[-1] + (b - a).length)
    return vert_order, closed, pts, cum, cum[-1]


class CAD_OT_SketchTrim(bpy.types.Operator):
    """AutoCAD-style TRIM with three interaction modes:
    - Plain click on an edge: trim at cursor (Click-Trim)
    - Plain click on empty space: start a straight Fence line
    - Press-and-drag anywhere: freehand Lasso Trim
    """
    bl_idname  = "cad.sketch_trim"
    bl_label   = "CAD Trim"
    bl_options = {'REGISTER', 'UNDO'}

    # ── invoke ────────────────────────────────────────────────────────
    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._mx, self._my = event.mouse_region_x, event.mouse_region_y
        self._hover = None   # (obj, edge_index, ts_sorted, remove_i, seg_pts) | None

        # ── Fence Trim state ──────────────────────────────────────────
        self._fence_active = False
        self._fence_p1     = None   # Vector | None - first fence click, world space
        self._fence_cur_3d = None   # Vector | None - live second point (cursor)
        self._fence_hits   = []     # [(obj, ei, ts_sorted, remove_i, seg_pts), ...]

        # ── LASSO TRIM state ─────────────────────────────────────────
        # Pure LMB-drag lasso, Blender lasso-select style: a plain
        # press+release with (almost) no movement is still a normal
        # click (Click-Trim, or start/finish a Fence line); a
        # press+drag past _DRAG_THRESHOLD_PX becomes a freehand lasso
        # instead. Nothing here needs a modifier key.
        self._lmb_down       = False   # LMB currently held (outside fence mode)?
        self._drag_started   = False   # Did the current press exceed the drag threshold?
        self._down_mx        = 0       # Screen-space press-down position, for
        self._down_my        = 0       # measuring drag distance.
        self._lasso_active   = False   # Is a lasso currently being drawn?
        self._lasso_points   = []      # Vector3D points along the lasso path
        self._lasso_points_2d = []     # Cached 2D screen points, for drawing + hit-testing
        self._lasso_hits     = []      # removal mode: [(obj, ei, ts_sorted, remove_i, seg_pts), ...]
                                        # boundary-select mode: [(obj, ei, [0,1], 0, A, B), ...] (unchanged)

        # ── BOUNDARY TRIM state ────────────────────────────────────────
        self._boundary_objs   = set()   # {bpy.types.Object, ...} - all boundary objects
        self._boundary_segments = []    # [(obj, edge_index, A, B), ...] - source of truth
        self._boundary_edges  = []      # [(A, B), ...] derived world-space boundary segments
        self._boundary_hover  = None    # (obj, ei, ts_sorted, i, seg_pts) | None
        self._shift_gesture   = False   # Shift held at press-down, deciding click vs drag
        self._lasso_boundary_mode = False   # active lasso is selecting boundaries, not removing

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "TRIM"
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
        _st.snap_cache_dirty = True
        reset_tpanel_to_select(self, context)
        if context.area:
            context.area.tag_redraw()

    def cancel(self, context):
        self._cleanup(context)

    # ── modal dispatch ───────────────────────────────────────────────
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
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN'}:
            return {'PASS_THROUGH'}

        # ── Alt+Click always toggles the whole object as a boundary ───
        if (event.type == 'LEFTMOUSE' and event.value == 'PRESS' and event.alt
                and not self._fence_active and not self._lasso_active and not self._lmb_down):
            hit = self._pick_edge_at_cursor(context)
            if hit is not None:
                obj, _ei, _t = hit
                if obj in self._boundary_objs:
                    self._boundary_segments = [
                        s for s in self._boundary_segments if s[0] is not obj
                    ]
                else:
                    mesh = obj.data
                    mw   = obj.matrix_world
                    self._boundary_segments.extend(
                        (obj, ei_,
                         mw @ mesh.vertices[e_.vertices[0]].co,
                         mw @ mesh.vertices[e_.vertices[1]].co)
                        for ei_, e_ in enumerate(mesh.edges)
                    )
                self._boundary_objs  = {s[0] for s in self._boundary_segments}
                self._boundary_edges = [(s[2], s[3]) for s in self._boundary_segments]
                self._hover = None
                self._update_header(context)
            return {'RUNNING_MODAL'}

        # ── Shift+Click toggles a single boundary edge; Shift+Click and
        #    hold (drag) lasso-toggles every edge crossed by the path.
        #    Decided on MOUSEMOVE/RELEASE like the plain-click lasso.
        if (event.type == 'LEFTMOUSE' and event.value == 'PRESS' and event.shift
                and not self._fence_active and not self._lasso_active and not self._lmb_down):
            self._lmb_down      = True
            self._drag_started  = False
            self._shift_gesture = True
            self._down_mx, self._down_my = self._mx, self._my
            return {'RUNNING_MODAL'}

        if self._shift_gesture and event.type == 'MOUSEMOVE':
            if not self._drag_started:
                dx = self._mx - self._down_mx
                dy = self._my - self._down_my
                if dx * dx + dy * dy > _DRAG_THRESHOLD_PX ** 2:
                    self._drag_started      = True
                    self._lasso_active       = True
                    self._lasso_boundary_mode = True
                    raw = mouse_to_3d(context, self._down_mx, self._down_my)
                    if raw is not None:
                        self._lasso_points    = [raw]
                        self._lasso_points_2d = [(self._down_mx, self._down_my)]
                    else:
                        self._lasso_points    = []
                        self._lasso_points_2d = []
                    self._lasso_hits = []
            if self._lasso_active:
                self._update_lasso_path(context)
            return {'RUNNING_MODAL'}

        if self._shift_gesture and event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            if self._lasso_active and self._lasso_boundary_mode:
                for obj, ei, _ts, _i, _a, _b in self._lasso_hits:
                    key = (obj, ei)
                    if any((o, e) == key for o, e, _A, _B in self._boundary_segments):
                        self._boundary_segments = [
                            s for s in self._boundary_segments if (s[0], s[1]) != key
                        ]
                    else:
                        mesh = obj.data
                        mw   = obj.matrix_world
                        e_ = mesh.edges[ei]
                        A = mw @ mesh.vertices[e_.vertices[0]].co
                        B = mw @ mesh.vertices[e_.vertices[1]].co
                        self._boundary_segments.append((obj, ei, A, B))
                self._boundary_objs  = {s[0] for s in self._boundary_segments}
                self._boundary_edges = [(s[2], s[3]) for s in self._boundary_segments]
                self._lasso_active        = False
                self._lasso_boundary_mode = False
                self._lasso_points        = []
                self._lasso_points_2d     = []
                self._lasso_hits          = []
            elif not self._drag_started:
                hit = self._pick_edge_at_cursor(context)
                if hit is not None:
                    obj, ei, _t = hit
                    key = (obj, ei)
                    if any((o, e) == key for o, e, _A, _B in self._boundary_segments):
                        self._boundary_segments = [
                            s for s in self._boundary_segments if (s[0], s[1]) != key
                        ]
                    else:
                        mesh = obj.data
                        mw   = obj.matrix_world
                        e_ = mesh.edges[ei]
                        A = mw @ mesh.vertices[e_.vertices[0]].co
                        B = mw @ mesh.vertices[e_.vertices[1]].co
                        self._boundary_segments.append((obj, ei, A, B))
                    self._boundary_objs  = {s[0] for s in self._boundary_segments}
                    self._boundary_edges = [(s[2], s[3]) for s in self._boundary_segments]
            self._hover          = None
            self._lmb_down       = False
            self._drag_started   = False
            self._shift_gesture  = False
            self._update_header(context)
            return {'RUNNING_MODAL'}

        if self._shift_gesture and event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self._lasso_active        = False
            self._lasso_boundary_mode = False
            self._lasso_points        = []
            self._lasso_points_2d     = []
            self._lasso_hits          = []
            self._lmb_down            = False
            self._drag_started        = False
            self._shift_gesture       = False
            self._update_header(context)
            return {'RUNNING_MODAL'}

        # ── BOUNDARY TRIM: intercepts click/fence/lasso while a
        #    boundary is set, restricting removal to it ────────────────
        if self._boundary_edges:
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                if self._fence_active:
                    p2 = mouse_to_3d(context, self._mx, self._my) or self._fence_p1
                    hits = self._fence_crossings(context, self._fence_p1, p2)
                    self._commit_trims(hits)
                    self._fence_active = False
                    self._fence_p1     = None
                    self._fence_cur_3d = None
                    self._fence_hits   = []
                    self._boundary_hover = None
                else:
                    self._lmb_down     = True
                    self._drag_started = False
                    self._down_mx, self._down_my = self._mx, self._my
                self._update_header(context)
                return {'RUNNING_MODAL'}

            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                if self._lasso_active:
                    if self._lasso_hits:
                        self._commit_trims(self._lasso_hits)
                    self._lasso_active    = False
                    self._lasso_points    = []
                    self._lasso_points_2d = []
                    self._lasso_hits      = []
                elif self._lmb_down and not self._drag_started:
                    if self._boundary_hover is not None:
                        obj, ei, ts_sorted, i, _pts = self._boundary_hover
                        self._commit_trims([(obj, ei, ts_sorted, i)])
                        self._boundary_hover = None
                    else:
                        raw = mouse_to_3d(context, self._mx, self._my)
                        if raw is not None:
                            self._fence_active = True
                            self._fence_p1     = raw
                            self._fence_cur_3d = raw
                            self._fence_hits   = []
                self._lmb_down     = False
                self._drag_started = False
                self._update_header(context)
                return {'RUNNING_MODAL'}

            if event.type == 'MOUSEMOVE':
                if self._lmb_down and not self._fence_active:
                    if not self._drag_started:
                        dx = self._mx - self._down_mx
                        dy = self._my - self._down_my
                        if dx * dx + dy * dy > _DRAG_THRESHOLD_PX ** 2:
                            self._drag_started = True
                            self._lasso_active  = True
                            raw = mouse_to_3d(context, self._down_mx, self._down_my)
                            if raw is not None:
                                self._lasso_points    = [raw]
                                self._lasso_points_2d = [(self._down_mx, self._down_my)]
                            else:
                                self._lasso_points    = []
                                self._lasso_points_2d = []
                            self._lasso_hits     = []
                            self._boundary_hover = None
                            self._update_header(context)
                    if self._lasso_active:
                        self._update_lasso_path(context)
                    return {'RUNNING_MODAL'}

                if self._fence_active:
                    raw = mouse_to_3d(context, self._mx, self._my)
                    self._fence_cur_3d = raw if raw is not None else self._fence_p1
                    self._fence_hits = self._fence_crossings(
                        context, self._fence_p1, self._fence_cur_3d
                    )
                else:
                    self._boundary_hover = self._boundary_pick_hover(context)
                return {'RUNNING_MODAL'}

            if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
                if self._lasso_active:
                    self._lasso_active    = False
                    self._lasso_points    = []
                    self._lasso_points_2d = []
                    self._lasso_hits      = []
                    self._lmb_down        = False
                    self._drag_started    = False
                elif self._fence_active:
                    self._fence_active = False
                    self._fence_p1     = None
                    self._fence_cur_3d = None
                    self._fence_hits   = []
                else:
                    self._boundary_objs     = set()
                    self._boundary_segments = []
                    self._boundary_edges    = []
                    self._boundary_hover    = None
                self._update_header(context)
                return {'RUNNING_MODAL'}

            return {'RUNNING_MODAL'}

        # ── LEFTMOUSE PRESS ──────────────────────────────────────────
        # If a fence is already waiting on its second point, a press
        # always finalizes it (unchanged two-click fence behaviour -
        # dragging isn't part of that gesture). Otherwise we don't yet
        # know if this is a click or the start of a drag, so just
        # remember where it went down and decide on MOUSEMOVE/RELEASE.
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if self._fence_active:
                p2 = mouse_to_3d(context, self._mx, self._my) or self._fence_p1
                hits = self._fence_crossings(context, self._fence_p1, p2)
                self._commit_trims(hits)
                self._fence_active = False
                self._fence_p1     = None
                self._fence_cur_3d = None
                self._fence_hits   = []
                self._hover = None
                self._update_header(context)
            else:
                self._lmb_down     = True
                self._drag_started = False
                self._down_mx, self._down_my = self._mx, self._my
            return {'RUNNING_MODAL'}

        # ── LEFTMOUSE RELEASE ────────────────────────────────────────
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            if self._lasso_active:
                # Drag gesture finished - apply every trim the freehand
                # path crossed, in one go (same _commit_trims used by
                # Click-Trim and Fence-Trim, so it's still a single
                # undo step even across many objects/edges).
                if self._lasso_hits:
                    self._commit_trims(self._lasso_hits)
                self._lasso_active    = False
                self._lasso_points    = []
                self._lasso_points_2d = []
                self._lasso_hits       = []
            elif self._lmb_down and not self._drag_started:
                # Released without ever crossing the drag threshold -
                # a plain click: identical to the tool's original
                # behaviour (trim the hovered edge, or start a fence
                # line from empty space).
                if self._hover is not None:
                    obj, ei, ts_sorted, i, _pts = self._hover
                    self._commit_trims([(obj, ei, ts_sorted, i)])
                    self._hover = None
                else:
                    raw = mouse_to_3d(context, self._mx, self._my)
                    if raw is not None:
                        self._fence_active = True
                        self._fence_p1     = raw
                        self._fence_cur_3d = raw
                        self._fence_hits   = []
            self._lmb_down     = False
            self._drag_started = False
            self._update_header(context)
            return {'RUNNING_MODAL'}

        # ── MOUSEMOVE ────────────────────────────────────────────────
        if event.type == 'MOUSEMOVE':
            if self._lmb_down and not self._fence_active:
                if not self._drag_started:
                    dx = self._mx - self._down_mx
                    dy = self._my - self._down_my
                    if dx * dx + dy * dy > _DRAG_THRESHOLD_PX ** 2:
                        # Crossed the drag threshold - this press is a
                        # freehand lasso, not a click. Seed the path
                        # with the original press-down point so the
                        # lasso starts exactly where the finger/mouse
                        # went down, not wherever it first exceeded
                        # the threshold.
                        self._drag_started = True
                        self._lasso_active  = True
                        raw = mouse_to_3d(context, self._down_mx, self._down_my)
                        if raw is not None:
                            self._lasso_points    = [raw]
                            self._lasso_points_2d = [(self._down_mx, self._down_my)]
                        else:
                            self._lasso_points    = []
                            self._lasso_points_2d = []
                        self._lasso_hits = []
                        self._hover      = None
                        self._update_header(context)

                if self._lasso_active:
                    self._update_lasso_path(context)
                return {'RUNNING_MODAL'}

            if self._fence_active:
                raw = mouse_to_3d(context, self._mx, self._my)
                self._fence_cur_3d = raw if raw is not None else self._fence_p1
                self._fence_hits = self._fence_crossings(
                    context, self._fence_p1, self._fence_cur_3d
                )
            else:
                self._hover = None
                hit = self._pick_edge_at_cursor(context)
                if hit is not None:
                    obj, ei, t_hover = hit
                    vert_order, closed, pts, cum, ts_sorted, (s0, s1) = \
                        self._compute_trim_intervals(context, obj, ei)
                    t_chain = s0 + t_hover * (s1 - s0)
                    i = self._interval_for_t(ts_sorted, t_chain, closed, cum[-1])
                    if i is not None:
                        j = (i + 1) % len(ts_sorted)
                        seg_pts = _chain_interval_points(pts, cum, closed, ts_sorted[i], ts_sorted[j])
                        self._hover = (obj, ei, ts_sorted, i, seg_pts)
            return {'RUNNING_MODAL'}

        if event.value == 'PRESS':
            if event.type in {'RIGHTMOUSE', 'ESC'}:
                if self._lasso_active:
                    # Cancel just the in-progress lasso first, same
                    # "cancel-current-op, don't quit the tool" feel as
                    # the fence line's own Esc handling below.
                    self._lasso_active    = False
                    self._lasso_points    = []
                    self._lasso_points_2d = []
                    self._lasso_hits      = []
                    self._lmb_down        = False
                    self._drag_started    = False
                    self._update_header(context)
                    return {'RUNNING_MODAL'}
                if self._fence_active:
                    self._fence_active = False
                    self._fence_p1     = None
                    self._fence_cur_3d = None
                    self._fence_hits   = []
                    self._update_header(context)
                    return {'RUNNING_MODAL'}
                self.cancel(context)
                return {'FINISHED'}

        return {'RUNNING_MODAL'}

    # ── boundary/intersection helpers (UNCHANGED from original) ───────
    def _all_edges_world(self, context):
        """(obj, edge_index, world_A, world_B) for every edge of every
        visible mesh in the scene."""
        out = []
        for obj in context.view_layer.objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            mw = obj.matrix_world
            mesh = obj.data
            for ei, e in enumerate(mesh.edges):
                a = mw @ mesh.vertices[e.vertices[0]].co
                b = mw @ mesh.vertices[e.vertices[1]].co
                out.append((obj, ei, a, b))
        return out

    def _pick_edge_at_cursor(self, context):
        """Nearest edge to the cursor, screen-space, within
        _EDGE_PICK_RADIUS_PX. Returns (obj, edge_index, hover_t) or None."""
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
                _, t = _closest_point_on_segment_2d((mx, my), a2, b2)
                cx = a2[0] + (b2[0] - a2[0]) * t
                cy = a2[1] + (b2[1] - a2[1]) * t
                d = math.hypot(cx - mx, cy - my)
                if d < best_d:
                    best_d = d
                    best   = (obj, ei, t)
        return best

    def _compute_trim_intervals(self, context, obj, edge_index):
        """Chain-aware: walk the connected polyline chain containing
        edge_index (a Line's single edge is just a 1-edge chain, so
        this is a strict generalisation - line behaviour unchanged),
        find every intersection between ANY edge of that chain and
        every OTHER edge in the scene, and return chain arc-length cut
        points instead of a single edge's local 0..1 t - so a Circle/
        Arc/Spline gets split at every real crossing along its whole
        length, not just within the one micro-edge that was clicked.

        Returns (vert_order, closed, pts, cum, ts_sorted, edge_span)
        where edge_span=(s0,s1) is the arc-length range of just the
        originally-picked edge_index within the chain, so a caller
        holding an edge-local hover-t can convert it to a chain
        arc-length value comparable with ts_sorted."""
        vert_order, closed, pts, cum, total_len = _chain_geometry(obj, edge_index)
        n = len(vert_order)
        segs = list(range(n)) if closed else list(range(n - 1))

        mesh = obj.data
        edge_lookup = {frozenset(e.vertices): ei for ei, e in enumerate(mesh.edges)}
        chain_edge_indices = set()
        edge_span = (0.0, 0.0)
        for i in segs:
            va, vb = vert_order[i], vert_order[(i + 1) % n]
            oei = edge_lookup.get(frozenset((va, vb)))
            if oei is not None:
                chain_edge_indices.add(oei)
                if oei == edge_index:
                    edge_span = (cum[i], cum[i + 1])

        # Only real crossings become cut points - NOT every intermediate
        # mesh vertex. Circle/Arc/Spline are drawn as many short
        # straight edges, so seeding with every vertex (old: set(cum))
        # made every interval just one microscopic segment regardless
        # of where the real crossings were.
        #
        # For an OPEN chain (Line/Arc/Spline with two free ends) the
        # two tips are always legitimate boundaries, so they're kept as
        # permanent cut points alongside any real crossings.
        #
        # For a CLOSED chain (e.g. Circle) the chain's start/end vertex
        # is just an artifact of how the mesh happens to be wound, not
        # a real cut - forcing it in as a permanent cut point would
        # chop a single continuous arc in two wherever it happened to
        # pass through that vertex, even with no intersection there.
        # So a closed chain uses ONLY genuine crossings, treated
        # circularly (wrapping from the last crossing back to the
        # first through the seam) by the interval/slicing helpers
        # below. With zero crossings at all, [cum[0]] is used as a
        # single circular cut point so the whole loop is one interval
        # (a click removes the entire circle).
        #
        # The crossing-position test uses the FULL 0..1 range of t (not
        # excluding the segment's own endpoints) - a crossing edge very
        # commonly lands exactly on one of the curve's existing micro-
        # vertices (e.g. anything snapped there), which shows up as
        # t~0 or t~1 on the segment it touches. Excluding that range
        # used to silently drop the intersection entirely. Landing
        # exactly on an existing vertex just re-adds a value already in
        # cum, which is harmless (real_ts is a set).
        real_ts = set()
        for b_obj, b_ei, C, D in self._all_edges_world(context):
            if b_obj is obj and b_ei in chain_edge_indices:
                continue
            for i in segs:
                A, B = pts[i], pts[(i + 1) % n]
                t = _segment_intersect_t(A, B, C, D)
                if t is not None:
                    t = max(0.0, min(1.0, t))
                    real_ts.add(cum[i] + t * (cum[i + 1] - cum[i]))

        # Self-intersections: the same chain crossing its own earlier
        # path (e.g. an overlapping Spline). Ordinary trim logic
        # excludes the chain's own edges from the crossing scan above
        # (correct for adjacent segments, which trivially "intersect"
        # at their shared vertex) but that also silently skipped real,
        # distant self-crossings - so a looped Spline could never be
        # split at the point it overlaps itself. This scans the chain
        # against itself explicitly.
        real_ts |= _chain_self_intersections(pts, cum, closed)

        if closed:
            ts_sorted = sorted(real_ts) if real_ts else [cum[0]]
        else:
            ts_sorted = sorted({cum[0], cum[-1]} | real_ts)
        return vert_order, closed, pts, cum, ts_sorted, edge_span

    def _interval_for_t(self, ts_sorted, t_hover, closed=False, total_len=None):
        """Index i of the interval containing t_hover.

        Open chain: ts_sorted[i] <= t_hover <= ts_sorted[i+1], i.e. the
        n points define n-1 linear intervals - unchanged behaviour.

        Closed chain: ts_sorted holds only genuine crossings (or a
        single point when there are none), so the n points define n
        CIRCULAR intervals, wrapping from the last point back to the
        first through the seam. total_len (the chain's full arc length,
        i.e. cum[-1]) is required to resolve that wraparound compare."""
        n = len(ts_sorted)
        if not closed:
            for i in range(n - 1):
                if ts_sorted[i] - 1e-6 <= t_hover <= ts_sorted[i + 1] + 1e-6:
                    return i
            return None

        if n == 0:
            return None
        if n == 1:
            return 0
        eps = 1e-6
        for i in range(n):
            a = ts_sorted[i]
            b = ts_sorted[(i + 1) % n]
            b_unwrapped = b if b > a else b + total_len
            th = t_hover if t_hover >= a - eps else t_hover + total_len
            if a - eps <= th <= b_unwrapped + eps:
                return i
        return None

    def _fence_crossings(self, context, p1, p2):
        """Every edge the fence segment p1->p2 crosses. When Boundary
        Trim has an active boundary, restricted to edges outside the
        boundary object, intersected only against the boundary."""
        if p1 is None or p2 is None or (p2 - p1).length < 1e-6:
            return []
        boundary = bool(self._boundary_edges)
        edge_src = (self._boundary_all_edges_world(context) if boundary
                    else self._all_edges_world(context))
        results = []
        for obj, ei, A, B in edge_src:
            t_edge = _segment_intersect_t(A, B, p1, p2)
            if t_edge is None or t_edge <= 1e-4 or t_edge >= 1 - 1e-4:
                continue
            if boundary:
                vert_order, closed, pts, cum, ts_sorted, (s0, s1) = \
                    self._boundary_trim_intervals(obj, ei)
            else:
                vert_order, closed, pts, cum, ts_sorted, (s0, s1) = \
                    self._compute_trim_intervals(context, obj, ei)
            t_chain = s0 + t_edge * (s1 - s0)
            i = self._interval_for_t(ts_sorted, t_chain, closed, cum[-1])
            if i is None:
                continue
            j = (i + 1) % len(ts_sorted)
            seg_pts = _chain_interval_points(pts, cum, closed, ts_sorted[i], ts_sorted[j])
            results.append((obj, ei, ts_sorted, i, seg_pts))
        return results

    # ── BOUNDARY TRIM methods ──────────────────────────────────────────

    def _boundary_all_edges_world(self, context):
        """Same as _all_edges_world but skips every boundary object."""
        for obj, ei, A, B in self._all_edges_world(context):
            if obj in self._boundary_objs:
                continue
            yield obj, ei, A, B

    def _boundary_pick_hover(self, context):
        """Nearest non-boundary edge to the cursor, with its trim
        interval computed only against the chosen boundary edges (not
        the whole scene). Returns (obj, ei, ts_sorted, i, seg_pts) or
        None."""
        hit = self._pick_edge_at_cursor_excluding(context, self._boundary_objs)
        if hit is None:
            return None
        obj, ei, t_hover = hit
        vert_order, closed, pts, cum, ts_sorted, (s0, s1) = self._boundary_trim_intervals(obj, ei)
        t_chain = s0 + t_hover * (s1 - s0)
        i = self._interval_for_t(ts_sorted, t_chain, closed, cum[-1])
        if i is None:
            return None
        j = (i + 1) % len(ts_sorted)
        seg_pts = _chain_interval_points(pts, cum, closed, ts_sorted[i], ts_sorted[j])
        return (obj, ei, ts_sorted, i, seg_pts)

    def _pick_edge_at_cursor_excluding(self, context, exclude_objs):
        """Same as _pick_edge_at_cursor but skips edges belonging to
        any object in exclude_objs (the boundary objects, which must
        stay unchanged)."""
        mx, my = self._mx, self._my
        best_d = float(_EDGE_PICK_RADIUS_PX)
        best   = None
        for obj in context.view_layer.objects:
            if obj.type != 'MESH' or not obj.visible_get() or obj in exclude_objs:
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
                _, t = _closest_point_on_segment_2d((mx, my), a2, b2)
                cx = a2[0] + (b2[0] - a2[0]) * t
                cy = a2[1] + (b2[1] - a2[1]) * t
                d = math.hypot(cx - mx, cy - my)
                if d < best_d:
                    best_d = d
                    best   = (obj, ei, t)
        return best

    def _boundary_trim_intervals(self, obj, edge_index):
        """Like _compute_trim_intervals, but intersects the chain only
        against the chosen boundary segments, ignoring every other
        edge in the scene. Same chain-aware generalisation."""
        vert_order, closed, pts, cum, total_len = _chain_geometry(obj, edge_index)
        n = len(vert_order)
        segs = list(range(n)) if closed else list(range(n - 1))

        edge_span = (0.0, 0.0)
        mesh = obj.data
        edge_lookup = {frozenset(e.vertices): ei for ei, e in enumerate(mesh.edges)}
        for i in segs:
            va, vb = vert_order[i], vert_order[(i + 1) % n]
            oei = edge_lookup.get(frozenset((va, vb)))
            if oei == edge_index:
                edge_span = (cum[i], cum[i + 1])
                break

        # Same fix as _compute_trim_intervals: only real crossings
        # become cut points, using the full 0..1 range (a crossing
        # landing exactly on an existing curve vertex must still count,
        # not be filtered out). Open chains additionally keep their two
        # free-end tips as permanent boundaries; a closed chain (e.g.
        # Circle) uses only genuine crossings, handled circularly, so
        # it doesn't get an artificial break at its arbitrary seam
        # vertex. Self-intersections (a boundary object looping over
        # itself) are included too.
        real_ts = set()
        for C, D in self._boundary_edges:
            for i in segs:
                A, B = pts[i], pts[(i + 1) % n]
                t = _segment_intersect_t(A, B, C, D)
                if t is not None:
                    t = max(0.0, min(1.0, t))
                    real_ts.add(cum[i] + t * (cum[i + 1] - cum[i]))
        real_ts |= _chain_self_intersections(pts, cum, closed)

        if closed:
            ts_sorted = sorted(real_ts) if real_ts else [cum[0]]
        else:
            ts_sorted = sorted({cum[0], cum[-1]} | real_ts)
        return vert_order, closed, pts, cum, ts_sorted, edge_span

    # ── LASSO TRIM methods [NEW] ──────────────────────────────────────

    def _add_lasso_point(self, point_3d):
        """Add point to lasso if it moved significantly (> 5 pixels)."""
        if not self._lasso_points:
            self._lasso_points.append(point_3d)
            self._lasso_points_2d.append((self._mx, self._my))
            return

        # Check distance from last point (in screen space)
        last_2d = self._lasso_points_2d[-1]
        dist_sq = (self._mx - last_2d[0])**2 + (self._my - last_2d[1])**2

        if dist_sq > 25:  # 5 pixel threshold
            self._lasso_points.append(point_3d)
            self._lasso_points_2d.append((self._mx, self._my))

    def _update_lasso_path(self, context):
        """Update lasso path and find intersecting edges."""
        raw_3d = mouse_to_3d(context, self._mx, self._my)
        if raw_3d is None:
            return

        self._add_lasso_point(raw_3d)
        self._update_lasso_hits(context)

    def _point_in_lasso_2d(self, point_2d):
        """Ray casting algorithm: point-in-polygon test (2D)."""
        if len(self._lasso_points_2d) < 3:
            return False

        x, y = point_2d
        n = len(self._lasso_points_2d)
        inside = False

        p1x, p1y = self._lasso_points_2d[0]
        for i in range(1, n + 1):
            p2x, p2y = self._lasso_points_2d[i % n]
            if y > min(p1y, p2y):
                if y <= max(p1y, p2y):
                    if x <= max(p1x, p2x):
                        if p1y != p2y:
                            xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                        if p1x == p2x or x <= xinters:
                            inside = not inside
            p1x, p1y = p2x, p2y

        return inside

    def _segment_intersect_2d(self, a, b, c, d):
        """Check if 2D line segments a-b and c-d intersect (CCW method)."""
        def ccw(ax, ay, bx, by, cx, cy):
            return (cy - ay) * (bx - ax) > (by - ay) * (cx - ax)

        ax, ay = a
        bx, by = b
        cx, cy = c
        dx, dy = d

        return ccw(ax, ay, cx, cy, dx, dy) != ccw(bx, by, cx, cy, dx, dy) and \
               ccw(ax, ay, bx, by, cx, cy) != ccw(ax, ay, bx, by, dx, dy)

    def _segment_crosses_lasso(self, context, A, B):
        """Check if edge segment A-B crosses the lasso polygon.
        Returns (crosses: bool, t_approx: float | None)"""
        a_2d = world_to_screen(context, A)
        b_2d = world_to_screen(context, B)

        if a_2d is None or b_2d is None:
            return False, None

        a_inside = self._point_in_lasso_2d(a_2d)
        b_inside = self._point_in_lasso_2d(b_2d)

        # If endpoints on opposite sides, segment crosses boundary
        if a_inside != b_inside:
            # Find approximate crossing point via binary search
            for t in [i * 0.1 for i in range(11)]:
                pt_3d = A.lerp(B, t)
                pt_2d = world_to_screen(context, pt_3d)
                if pt_2d and self._point_in_lasso_2d(pt_2d) != a_inside:
                    return True, t
            return True, 0.5

        # Check if segment intersects lasso boundary
        for i in range(len(self._lasso_points_2d)):
            p1 = self._lasso_points_2d[i]
            p2 = self._lasso_points_2d[(i + 1) % len(self._lasso_points_2d)]

            if self._segment_intersect_2d(a_2d, b_2d, p1, p2):
                return True, 0.5

        return False, None

    def _update_lasso_hits(self, context):
        """Find all edges currently intersecting the lasso. In boundary-
        selection mode, every crossed edge (full length) is a hit. In
        removal mode, restricted to the boundary if one is set, and
        each hit is the specific interval to remove."""
        self._lasso_hits = []

        if len(self._lasso_points_2d) < 2:
            return

        if self._lasso_boundary_mode:
            for obj, ei, A, B in self._all_edges_world(context):
                crosses, _t = self._segment_crosses_lasso(context, A, B)
                if crosses:
                    self._lasso_hits.append((obj, ei, [0.0, 1.0], 0, A, B))
            return

        boundary = bool(self._boundary_edges)
        edge_src = (self._boundary_all_edges_world(context) if boundary
                    else self._all_edges_world(context))
        for obj, ei, A, B in edge_src:
            crosses, t_approx = self._segment_crosses_lasso(context, A, B)
            if crosses and t_approx is not None:
                if boundary:
                    vert_order, closed, pts, cum, ts_sorted, (s0, s1) = \
                        self._boundary_trim_intervals(obj, ei)
                else:
                    vert_order, closed, pts, cum, ts_sorted, (s0, s1) = \
                        self._compute_trim_intervals(context, obj, ei)
                t_chain = s0 + t_approx * (s1 - s0)
                i = self._interval_for_t(ts_sorted, t_chain, closed, cum[-1])
                if i is not None:
                    j = (i + 1) % len(ts_sorted)
                    seg_pts = _chain_interval_points(pts, cum, closed, ts_sorted[i], ts_sorted[j])
                    self._lasso_hits.append((obj, ei, ts_sorted, i, seg_pts))

    # ── Trim application ──────────────────────────────────────────────
    def _commit_trims(self, hits):
        """Apply trims grouped per object."""
        by_obj = {}
        for hit in hits:
            obj, edge_index, ts_sorted, remove_i = hit[0], hit[1], hit[2], hit[3]
            by_obj.setdefault(obj.name, []).append((edge_index, ts_sorted, remove_i))
        for obj_name, trims in by_obj.items():
            obj = bpy.data.objects.get(obj_name)
            if obj is not None:
                self._trim_edges_on_object(obj, trims)

    def _trim_edges_on_object(self, obj, trims):
        """Execute trim on BMesh level.

        Chain-aware: each trim's ts_sorted are chain arc-length cut
        points (see _compute_trim_intervals), which may span several
        of the object's original mesh edges when edge_index belongs
        to a Circle/Arc/Spline (many small edges) rather than a
        single-edge Line. Every original edge of the chain that falls
        (fully or partially) inside the removed [ts_sorted[remove_i],
        ts_sorted[remove_i+1]] interval is deleted; the chain is
        rebuilt from fresh edges between the ts_sorted-derived cut
        points everywhere except that removed interval - reusing
        original vertices where a cut coincides with one, and only
        creating a new vertex where a cut is a genuine intersection
        partway through a segment. For a Line (a 1-edge chain) this
        reduces to exactly the original single-edge behaviour."""
        mesh = obj.data
        mw   = obj.matrix_world
        mw_inv = mw.inverted()

        # Resolve every trim's chain geometry *before* any mutation -
        # mirrors the original code's "resolve references before
        # mutation" approach, just generalised to a whole chain of
        # edges instead of one.
        edge_lookup = {frozenset(e.vertices): ei for ei, e in enumerate(mesh.edges)}
        resolved = []       # (vert_order, closed, pts, cum, ts_sorted, remove_i)
        edges_to_delete = set()
        for edge_index, ts_sorted, remove_i in trims:
            vert_order, closed, pts, cum, total_len = _chain_geometry(obj, edge_index)
            resolved.append((vert_order, closed, pts, cum, ts_sorted, remove_i))
            n = len(vert_order)
            segs = range(n) if closed else range(n - 1)
            for i in segs:
                va, vb = vert_order[i], vert_order[(i + 1) % n]
                oei = edge_lookup.get(frozenset((va, vb)))
                if oei is not None:
                    edges_to_delete.add(oei)

        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()

        # Cache new intersection-point vertices by rounded chain
        # arc-length so two trims on the same chain (e.g. a lasso
        # crossing one circle in two places) that happen to share a
        # cut point reuse one vertex instead of creating a coincident
        # duplicate.
        new_vert_cache = {}

        def vert_for_s(vert_order, pts, cum, closed, s):
            for k, cv in enumerate(cum):
                if abs(cv - s) < 1e-4:
                    # Any bm.verts.new() call earlier in this pass (for
                    # a previous cut point, or another trim on this
                    # same chain) invalidates bmesh's internal index
                    # table, so a plain bm.verts[idx] lookup here can
                    # raise "outdated internal index table" - refresh
                    # it first.
                    bm.verts.ensure_lookup_table()
                    return bm.verts[vert_order[k % len(vert_order)]]
            key = (id(vert_order), round(s, 4))
            if key in new_vert_cache:
                return new_vert_cache[key]
            v = bm.verts.new(mw_inv @ _chain_point_at(pts, cum, closed, s))
            new_vert_cache[key] = v
            return v

        for vert_order, closed, pts, cum, ts_sorted, remove_i in resolved:
            n_ts = len(ts_sorted)
            # Open chain: n_ts points -> n_ts-1 linear intervals.
            # Closed chain: ts_sorted holds only genuine crossings (see
            # _compute_trim_intervals), so its n_ts points define n_ts
            # CIRCULAR intervals - the last one wraps from the final
            # crossing back to the first through the seam, and must be
            # rebuilt too (skipped only when it's the removed one).
            pair_range = range(n_ts) if closed else range(n_ts - 1)
            for i in pair_range:
                if i == remove_i:
                    continue
                j = (i + 1) % n_ts
                s_from, s_to = ts_sorted[i], ts_sorted[j]

                # Walk the actual curve for this kept interval (not
                # just a straight chord from cut-point to cut-point),
                # so any original curve vertices strictly between the
                # two cuts - and, for the wraparound interval, the
                # seam itself - are preserved and the retained arc
                # keeps its real shape.
                interval_pts = _chain_interval_points(pts, cum, closed, s_from, s_to)
                verts_here = [vert_for_s(vert_order, pts, cum, closed, s_from)]
                for p in interval_pts[1:-1]:
                    verts_here.append(bm.verts.new(mw_inv @ p))
                verts_here.append(vert_for_s(vert_order, pts, cum, closed, s_to))

                for k in range(len(verts_here) - 1):
                    va, vb = verts_here[k], verts_here[k + 1]
                    if va is vb:
                        continue
                    if not bm.edges.get((va, vb)):
                        bm.edges.new((va, vb))

        bm.edges.ensure_lookup_table()
        to_delete = [bm.edges[oei] for oei in edges_to_delete if oei < len(bm.edges)]
        bmesh.ops.delete(bm, geom=to_delete, context='EDGES')

        bm.to_mesh(mesh)
        bm.free()
        mesh.update()
        _st.snap_cache_dirty = True

    # ── draw callback ────────────────────────────────────────────────
    _COL_REMOVE = (1.00, 0.20, 0.20, 0.95)   # red
    _COL_FENCE  = (0.90, 0.90, 0.90, 0.90)   # gray
    _COL_LASSO  = (1.00, 1.00, 1.00, 0.90)   # white (changed from green)

    _COL_BOUNDARY = (0.25, 0.55, 1.00, 0.95)  # blue

    def _draw_highlight_polyline(self, context, seg_pts, col, w=3.0, dot_r=6):
        """Draw a to-be-removed/hover interval as a dashed polyline
        following seg_pts (which, for a Circle/Arc/Spline interval
        spanning several original edges, traces the actual curve
        rather than a straight chord across it), plus end dots."""
        pts_2d = [world_to_screen(context, p) for p in seg_pts]
        pts_2d = [p for p in pts_2d if p is not None]
        if len(pts_2d) < 2:
            return
        for i in range(len(pts_2d) - 1):
            gfx_dashed_line(pts_2d[i], pts_2d[i + 1], col=col, dash_len=5, space_len=4, w=w)
        gfx_dot(pts_2d[0], col, dot_r)
        gfx_dot(pts_2d[-1], col, dot_r)

    def _draw(self, context):
        # The crosshair stands in for "the cursor" - only draw it
        # while the real OS cursor is actually hidden (mouse over the
        # usable viewport).
        if cursor_fx_visible(self):
            draw_crosshair(context, self._mx, self._my, None, show_box=False)

        # ── Draw BOUNDARY TRIM ──────────────────────────────────────────
        if self._boundary_edges:
            for A, B in self._boundary_edges:
                a2 = world_to_screen(context, A)
                b2 = world_to_screen(context, B)
                if a2 and b2:
                    gfx_lines([a2, b2], self._COL_BOUNDARY, 3.0)

            if not (self._lasso_active or self._fence_active):
                if self._boundary_hover is not None:
                    _obj, _ei, _ts, _i, seg_pts = self._boundary_hover
                    self._draw_highlight_polyline(context, seg_pts, self._COL_REMOVE)
                    gfx_text(
                        "BOUNDARY TRIM: click to remove highlighted segment",
                        10, 55, 12, (1.0, 0.3, 0.3, 1.0),
                    )
                else:
                    gfx_text(
                        "BOUNDARY TRIM: Shift/Alt+Click toggles boundaries  ·  click/drag/fence to remove",
                        10, 55, 12, (0.3, 0.9, 1.0, 1.0),
                    )
                return

        # ── Draw LASSO [NEW] ──────────────────────────────────────────
        if self._lasso_active:
            # Draw lasso path as white dashed line segments
            if len(self._lasso_points_2d) > 1:
                for i in range(len(self._lasso_points_2d) - 1):
                    p1 = self._lasso_points_2d[i]
                    p2 = self._lasso_points_2d[i + 1]
                    gfx_dashed_line(p1, p2, col=self._COL_LASSO, dash_len=5, space_len=4, w=2.0)

                # Draw dashed line from last point to current cursor
                last = self._lasso_points_2d[-1]
                gfx_dashed_line(last, (self._mx, self._my),
                              col=self._COL_LASSO, dash_len=5, space_len=4, w=2.0)

            # Draw start point (larger dot)
            if self._lasso_points_2d:
                start = self._lasso_points_2d[0]
                gfx_dot(start, self._COL_LASSO, 8)

            # Draw lasso hit segments (same as fence/click trim preview).
            # Boundary-selection-mode hits keep their original 6-tuple
            # shape (obj, ei, [0,1], 0, A, B) - a straight full-edge
            # highlight, unrelated to trim-interval curve tracing.
            # Removal-mode hits use the chain-aware polyline highlight.
            if self._lasso_boundary_mode:
                for _obj, _ei, _ts, _i, A, B in self._lasso_hits:
                    a2 = world_to_screen(context, A)
                    b2 = world_to_screen(context, B)
                    if a2 and b2:
                        gfx_dashed_line(a2, b2, col=self._COL_BOUNDARY, dash_len=5, space_len=4, w=3.0)
                        gfx_dot(a2, self._COL_BOUNDARY, 5)
                        gfx_dot(b2, self._COL_BOUNDARY, 5)
            else:
                for _obj, _ei, _ts, _i, seg_pts in self._lasso_hits:
                    self._draw_highlight_polyline(context, seg_pts, self._COL_REMOVE, dot_r=5)

            # Status text
            n = len(self._lasso_hits)
            if self._lasso_boundary_mode:
                gfx_text(
                    f"BOUNDARY LASSO: release to toggle  -  {n} edge{'s' if n != 1 else ''}",
                    10, 55, 12, (0.3, 0.55, 1.0, 1.0) if n else (0.3, 0.9, 1.0, 1.0),
                )
            else:
                gfx_text(
                    f"LASSO TRIM: release to finish  -  {n} segment{'s' if n != 1 else ''} will be removed",
                    10, 55, 12, (1.0, 0.3, 0.3, 1.0) if n else (0.3, 0.9, 1.0, 1.0),
                )
            return

        # ── Draw FENCE ────────────────────────────────────────────────
        if self._fence_active:
            p1_2d  = world_to_screen(context, self._fence_p1)
            cur_2d = world_to_screen(context, self._fence_cur_3d) if self._fence_cur_3d else None
            if p1_2d and cur_2d:
                gfx_dashed_line(p1_2d, cur_2d, col=self._COL_FENCE, dash_len=6, space_len=5, w=1.6)
                gfx_dot(p1_2d, self._COL_FENCE, 6)
                gfx_dot(cur_2d, self._COL_FENCE, 5)

            for _obj, _ei, _ts, _i, seg_pts in self._fence_hits:
                self._draw_highlight_polyline(context, seg_pts, self._COL_REMOVE, dot_r=5)

            n = len(self._fence_hits)
            gfx_text(
                f"TRIM (fence): click to finish  -  {n} segment{'s' if n != 1 else ''} will be removed",
                10, 55, 12, (1.0, 0.3, 0.3, 1.0) if n else (0.3, 0.9, 1.0, 1.0),
            )
            return

        # ── Draw CLICK TRIM HOVER ─────────────────────────────────────
        if self._hover is not None:
            _obj, _ei, _ts, _i, seg_pts = self._hover
            self._draw_highlight_polyline(context, seg_pts, self._COL_REMOVE)
            gfx_text(
                "TRIM: click to remove highlighted segment",
                10, 55, 12, (1.0, 0.3, 0.3, 1.0),
            )
        else:
            gfx_text(
                "TRIM: click an edge to trim, drag for a freehand lasso, click empty space for a fence line, or Shift/Alt+Click to set a boundary",
                10, 55, 12, (0.3, 0.9, 1.0, 1.0),
            )

    def _update_header(self, context):
        """Update header text based on current state."""
        if self._lasso_active:
            text = "LASSO TRIM: release to apply | ESC to cancel"
        elif self._fence_active:
            text = "TRIM (fence): click to finish | ESC to cancel"
        elif self._boundary_edges:
            text = "BOUNDARY TRIM: Shift/Alt+Click toggles boundaries, click/drag/fence to remove | Esc=clear"
        else:
            text = "TRIM: click an edge to trim, drag for a freehand lasso, click empty space for a fence line, or Shift/Alt+Click to set a boundary | ESC=exit"
        context.area.header_text_set(text)
