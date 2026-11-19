# ─────────────────────────────────────────────────────────────────────
#  operators/tool_extend.py
#  CAD_OT_DrawExtend – AutoCAD-style EXTEND command, mirroring the
#  Trim tool's interaction model but doing the opposite operation.
#
#  Method 1 - Click Extend
#  ────────────────────────
#  Click directly on an edge, near whichever end is short of wherever
#  you want it to reach. Every other edge in the scene is
#  automatically considered as a possible boundary; the nearest one
#  that end can validly reach is used - exactly like Trim's Click Trim
#  auto-finds the nearest crossing edge, just grown instead of cut.
#
#  Method 2 - Fence Extend
#  ────────────────────────
#  Click empty space to draw a straight fence line. Every edge the
#  fence crosses is extended toward its own nearest valid boundary the
#  moment the fence is finished (second click).
#
#  Method 3 - Lasso Extend
#  ────────────────────────
#  Press and drag (instead of a plain click) to draw a freehand path.
#  Every edge the path crosses is extended the moment the button is
#  released. A plain press+release with almost no movement still
#  behaves like Method 1/2 - only crossing a small drag threshold
#  turns the gesture into a lasso.
#
#  Method 4 - Boundary pin
#  ────────────────────────
#  Shift+click an edge to pin it as an explicit boundary - add as many
#  as needed. Once pinned, Methods 1-3 above only extend to those
#  specific edges instead of auto-searching the whole scene. Esc
#  clears all pinned boundaries before exiting the tool.
#
#  Which end moves is always decided by which end of the edge is
#  nearer wherever it was touched (clicked, fenced, or lassoed) - so a
#  line can be extended from either end depending on what's crossed.
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
from ..gfx.primitives import gfx_dot, gfx_text, gfx_lines, gfx_dashed_line, gfx_loop
from ..gfx.dims       import draw_float_dim

_EDGE_PICK_RADIUS_PX = 10
_PARALLEL_EPS        = 1e-9
_DRAG_THRESHOLD_PX    = 4


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
    """Infinite-line intersection of line A-B and line C-D. Returns
    the point, or None if the lines are parallel."""
    d1x, d1y = B.x - A.x, B.y - A.y
    d2x, d2y = D.x - C.x, D.y - C.y
    denom = d1x * d2y - d1y * d2x
    if abs(denom) < _PARALLEL_EPS:
        return None
    t = ((C.x - A.x) * d2y - (C.y - A.y) * d2x) / denom
    return Vector((A.x + d1x * t, A.y + d1y * t, 0.0))


def _segment_intersect_t(A: Vector, B: Vector, C: Vector, D: Vector):
    """2D (X/Y only) segment-segment intersection. Returns t along A->B
    (0..1) if the segments cross, else None."""
    rx, ry = B.x - A.x, B.y - A.y
    sx, sy = D.x - C.x, D.y - C.y
    rxs = rx * sy - ry * sx
    if abs(rxs) < 1e-9:
        return None
    qpx, qpy = C.x - A.x, C.y - A.y
    t = (qpx * sy - qpy * sx) / rxs
    u = (qpx * ry - qpy * rx) / rxs
    eps = 1e-6
    if -eps <= t <= 1 + eps and -eps <= u <= 1 + eps:
        return max(0.0, min(1.0, t))
    return None


class _ExtendResult:
    __slots__ = ('valid', 'reason', 'anchor', 'new_point', 'length')

    def __init__(self, **kw):
        self.valid = False
        self.reason = ''
        for k, v in kw.items():
            setattr(self, k, v)


def _compute_extend(edge, t_click, boundary):
    """edge: (obj, edge_index, A, B) world-space endpoints of the edge
    to extend. t_click: parametric position (0..1) of where it was
    clicked/hovered along A->B - picks which end moves. boundary:
    (obj, edge_index, A2, B2) the target edge to extend to."""
    _o, _ei, A, B = edge
    _bo, _bei, A2, B2 = boundary

    d = B - A
    if d.length < 1e-9:
        return _ExtendResult(reason="Edge has zero length")

    moving_orig, anchor = (A, B) if t_click < 0.5 else (B, A)
    direction = (moving_orig - anchor)
    if direction.length < 1e-9:
        return _ExtendResult(reason="Edge has zero length")
    dn = direction.normalized()
    orig_len = direction.length

    db = B2 - A2
    if db.length < 1e-9:
        return _ExtendResult(reason="Boundary edge has zero length")

    P = _line_isect_2d(anchor, moving_orig, A2, B2)
    if P is None:
        return _ExtendResult(reason="Edge is parallel to the boundary")

    t_along = (P - anchor).dot(dn)
    if t_along < 0:
        return _ExtendResult(reason="Boundary is on the opposite side - click the other end")
    if t_along <= orig_len + 1e-6:
        return _ExtendResult(reason="Edge already reaches or crosses the boundary")

    if (P - (anchor + dn * t_along)).length > 1e-4:
        return _ExtendResult(reason="No valid extension for these edges")

    tb = (P - A2).dot(db) / (db.length ** 2)
    if tb < -1e-6 or tb > 1 + 1e-6:
        return _ExtendResult(reason="Boundary edge doesn't reach far enough")

    return _ExtendResult(
        valid=True, anchor=anchor, new_point=P, length=(t_along - orig_len),
    )


def _find_best_extend(context, hovered, t_click, boundaries=None):
    """Search candidate boundary edges and return the _ExtendResult
    for whichever valid one is nearest. If `boundaries` is a non-empty
    list, only those edges are considered (Trim's Boundary-Trim
    convention, Shift+Click to pin); otherwise every other edge in the
    scene is a candidate, auto-detecting the nearest one - the same
    principle as Trim's Method 1 finding the nearest crossing edge,
    just growing instead of cutting."""
    obj, ei, A, B = hovered
    best = None

    if boundaries:
        for b_obj, b_ei, A2, B2 in boundaries:
            if b_obj is obj and b_ei == ei:
                continue
            result = _compute_extend((obj, ei, A, B), t_click, (b_obj, b_ei, A2, B2))
            if result.valid and (best is None or result.length < best.length):
                best = result
        if best is not None:
            return best
        return _ExtendResult(reason="Doesn't reach the selected boundary")

    for other in context.view_layer.objects:
        if other.type != 'MESH' or not other.visible_get():
            continue
        mw = other.matrix_world
        mesh = other.data
        for oei, e in enumerate(mesh.edges):
            if other is obj and oei == ei:
                continue
            A2 = mw @ mesh.vertices[e.vertices[0]].co
            B2 = mw @ mesh.vertices[e.vertices[1]].co
            result = _compute_extend((obj, ei, A, B), t_click, (other, oei, A2, B2))
            if result.valid and (best is None or result.length < best.length):
                best = result
    if best is not None:
        return best
    return _ExtendResult(reason="No edge found to extend to")


def _iter_scene_edges(context):
    """Yield (obj, edge_index, A, B) for every edge of every visible
    mesh object in the scene, world-space endpoints."""
    for obj in context.view_layer.objects:
        if obj.type != 'MESH' or not obj.visible_get():
            continue
        mw = obj.matrix_world
        mesh = obj.data
        for ei, e in enumerate(mesh.edges):
            yield (obj, ei,
                   mw @ mesh.vertices[e.vertices[0]].co,
                   mw @ mesh.vertices[e.vertices[1]].co)


def _path_crossings(context, path_points, boundaries):
    """Given a fence (2 points) or lasso (N points) world-space path,
    find every scene edge crossed by any segment of that path and
    return a dict {(obj, ei): _ExtendResult} for the valid extends -
    first crossing found per edge wins."""
    hits = {}
    for i in range(len(path_points) - 1):
        C, D = path_points[i], path_points[i + 1]
        for obj, ei, A, B in _iter_scene_edges(context):
            key = (obj, ei)
            if key in hits:
                continue
            t = _segment_intersect_t(A, B, C, D)
            if t is None:
                continue
            result = _find_best_extend(context, (obj, ei, A, B), t, boundaries)
            if result.valid:
                hits[key] = (obj, ei, result)
    return list(hits.values())


class CAD_OT_DrawExtend(bpy.types.Operator):
    """Extend a line/edge until it meets the nearest valid edge it can
    reach, stopping exactly at the intersection - no separate boundary
    pick, same click-and-go model as Trim's Method 1."""
    bl_idname  = "cad.draw_extend"
    bl_label   = "CAD Extend"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._hover     = None   # (obj, edge_index, A, B, t)
        self._result    = None
        self._error     = ''
        self._boundaries = []    # [(obj, edge_index, A, B), ...] - optional Shift+Click pins

        # ── Fence Extend state ────────────────────────────────────────
        self._fence_active = False
        self._fence_p1     = None   # Vector | None - first fence click, world space
        self._fence_cur_3d = None   # Vector | None - live second point (cursor)
        self._fence_hits   = []     # [(obj, ei, ExtendResult), ...]

        # ── Lasso Extend state (press+drag past threshold) ────────────
        self._lmb_down        = False
        self._down_mx         = 0
        self._down_my         = 0
        self._lasso_active    = False
        self._lasso_points    = []      # Vector3D points along the lasso path
        self._lasso_points_2d = []      # cached 2D screen points, for drawing
        self._lasso_hits      = []      # [(obj, ei, ExtendResult), ...]

        self._mx, self._my = event.mouse_region_x, event.mouse_region_y

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "EXTEND"
        context.window_manager.modal_handler_add(self)
        self._update_header(context)
        return {'RUNNING_MODAL'}

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

    # ── edge picking ─────────────────────────────────────────────────
    def _pick_edge_at_cursor(self, context):
        """Nearest edge to the cursor, screen-space, within
        _EDGE_PICK_RADIUS_PX. Returns (obj, edge_index, A, B, t) or
        None. t is the parametric position (0..1) of the closest point
        along the edge to the cursor."""
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
                (cx, cy), t = _closest_point_on_segment_2d((mx, my), a2, b2)
                d = math.hypot(cx - mx, cy - my)
                if d < best_d:
                    best_d = d
                    best   = (obj, ei, a3, b3, t)
        return best

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
            hit = self._pick_edge_at_cursor(context)
            self._hover = hit
            if hit is not None and not self._is_boundary(hit[0], hit[1]):
                edge = hit[:4]
                self._result = _find_best_extend(context, edge, hit[4], self._boundaries)
                self._error = '' if self._result.valid else self._result.reason
            else:
                self._result = None
                self._error = ''

            if self._fence_active:
                raw = mouse_to_3d(context, self._mx, self._my)
                self._fence_cur_3d = raw if raw else self._fence_cur_3d
                if self._fence_cur_3d is not None:
                    self._fence_hits = _path_crossings(
                        context, [self._fence_p1, self._fence_cur_3d], self._boundaries)
                return {'RUNNING_MODAL'}

            if self._lmb_down and not self._lasso_active:
                dx = self._mx - self._down_mx
                dy = self._my - self._down_my
                if dx * dx + dy * dy > _DRAG_THRESHOLD_PX ** 2:
                    self._lasso_active = True
                    down_pt = mouse_to_3d(context, self._down_mx, self._down_my)
                    self._lasso_points    = [down_pt] if down_pt else []
                    self._lasso_points_2d = [(self._down_mx, self._down_my)]
                    self._lasso_hits      = []

            if self._lasso_active:
                raw = mouse_to_3d(context, self._mx, self._my)
                if raw:
                    self._lasso_points.append(raw)
                    self._lasso_points_2d.append((self._mx, self._my))
                    self._lasso_hits = _path_crossings(context, self._lasso_points, self._boundaries)
                return {'RUNNING_MODAL'}

            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if self._fence_active:
                p2 = mouse_to_3d(context, self._mx, self._my) or self._fence_cur_3d
                hits = _path_crossings(context, [self._fence_p1, p2], self._boundaries) if p2 else []
                self._fence_active = False
                self._fence_p1     = None
                self._fence_cur_3d = None
                self._fence_hits   = []
                if hits:
                    self._commit_batch(hits)
                self._hover = None
                self._result = None
                self._update_header(context)
                return {'RUNNING_MODAL'}

            if self._hover is not None:
                obj, ei, A, B, t = self._hover

                if event.shift:
                    if self._is_boundary(obj, ei):
                        self._boundaries = [b for b in self._boundaries
                                             if not (b[0] is obj and b[1] == ei)]
                    else:
                        self._boundaries.append((obj, ei, A, B))
                    self._update_header(context)
                    return {'RUNNING_MODAL'}

                if self._is_boundary(obj, ei):
                    return {'RUNNING_MODAL'}

                if self._result is not None and self._result.valid:
                    self._commit_batch([(obj, ei, self._result)])
                    self._hover = None
                    self._result = None
                    self._error = ''
                    self._update_header(context)
                return {'RUNNING_MODAL'}

            # clicked empty space - defer: could become a Fence (plain
            # click) or a Lasso (drag past threshold), decided on the
            # next MOUSEMOVE/RELEASE
            self._lmb_down = True
            self._down_mx, self._down_my = self._mx, self._my
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            if self._lasso_active:
                hits = self._lasso_hits
                self._lasso_active    = False
                self._lasso_points    = []
                self._lasso_points_2d = []
                self._lasso_hits      = []
                self._lmb_down        = False
                if hits:
                    self._commit_batch(hits)
                self._hover = None
                self._result = None
                self._update_header(context)
                return {'RUNNING_MODAL'}

            if self._lmb_down:
                # plain click+release on empty space, no drag - start
                # (or this is the very first click of) a Fence line
                self._lmb_down = False
                down_pt = mouse_to_3d(context, self._down_mx, self._down_my)
                if down_pt is not None:
                    self._fence_active = True
                    self._fence_p1     = down_pt
                    self._fence_cur_3d = down_pt
                    self._update_header(context)
            return {'RUNNING_MODAL'}

        if event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            if self._fence_active:
                self._fence_active = False
                self._fence_p1     = None
                self._fence_cur_3d = None
                self._fence_hits   = []
                self._update_header(context)
                return {'RUNNING_MODAL'}
            self.cancel(context)
            return {'CANCELLED'}

        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            if self._fence_active:
                self._fence_active = False
                self._fence_p1     = None
                self._fence_cur_3d = None
                self._fence_hits   = []
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if self._boundaries:
                self._boundaries = []
                self._update_header(context)
                return {'RUNNING_MODAL'}
            self.cancel(context)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def _is_boundary(self, obj, ei):
        return any(b[0] is obj and b[1] == ei for b in self._boundaries)

    # ── commit: move the extended edges' free vertices ──────────────
    def _commit_batch(self, items):
        """items: [(obj, edge_index, ExtendResult), ...]. Groups by
        object and processes each object in a single bmesh session,
        resolving every target bm_edge/anchor-vertex *before* any new
        geometry is added - adding verts/edges invalidates BMesh's
        index-based lookup tables, so indices must not be re-read
        mid-loop (see the Fillet tool's identical fix)."""
        by_obj = {}
        for obj, ei, result in items:
            by_obj.setdefault(obj, []).append((ei, result))

        for obj, entries in by_obj.items():
            mesh = obj.data
            mw = obj.matrix_world
            mw_inv = mw.inverted()

            bm = bmesh.new()
            bm.from_mesh(mesh)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()

            resolved = []
            for ei, result in entries:
                if ei >= len(bm.edges):
                    continue
                bm_edge = bm.edges[ei]
                v0, v1 = bm_edge.verts
                w0 = mw @ v0.co
                w1 = mw @ v1.co
                anchor_vert = v0 if (w0 - result.anchor).length <= (w1 - result.anchor).length else v1
                resolved.append((bm_edge, anchor_vert, result.new_point))

            to_delete = []
            for bm_edge, anchor_vert, new_pt in resolved:
                new_v = bm.verts.new(mw_inv @ new_pt)
                if not bm.edges.get((anchor_vert, new_v)):
                    bm.edges.new((anchor_vert, new_v))
                to_delete.append(bm_edge)

            if to_delete:
                bmesh.ops.delete(bm, geom=to_delete, context='EDGES')

            bm.to_mesh(mesh)
            bm.free()
            mesh.update()

        _st.snap_cache_dirty = True
        for o in bpy.context.selected_objects:
            o.select_set(False)
        bpy.context.view_layer.objects.active = None

    # ── header ───────────────────────────────────────────────────────
    def _update_header(self, context):
        if self._fence_active:
            n = len(self._fence_hits)
            text = f"EXTEND FENCE: click to finish ({n} edge{'s' if n != 1 else ''} crossed)  ·  Enter=finish  ·  Esc=cancel fence"
            context.area.header_text_set(text)
            return

        if self._boundaries:
            n = len(self._boundaries)
            plural = "boundary" if n == 1 else "boundaries"
            err = f"  ·  {self._error}" if self._error else ""
            text = (f"EXTEND: {n} {plural} pinned  ·  click/fence/lasso an edge to extend to it{err}  ·  "
                    f"Shift+click=add/remove boundary  ·  Esc=clear boundaries")
        else:
            err = f"  ·  {self._error}" if self._error else ""
            text = (f"EXTEND: click an edge, or click empty space for a fence, or drag for a lasso{err}  ·  "
                    f"Shift+click=pin a boundary  ·  Enter/Esc=exit")
        context.area.header_text_set(text)

    # ── draw callback ────────────────────────────────────────────────
    _COL_BOUNDARY = (0.30, 0.90, 1.00, 0.95)
    _COL_HOVER    = (1.00, 1.00, 0.20, 0.95)
    _COL_EXT_OK   = (0.37, 0.92, 0.67, 1.00)
    _COL_EXT_BAD  = (1.00, 0.30, 0.30, 0.70)

    def _draw(self, context):
        try:
            self._draw_impl(context)
        except Exception as e:
            print(f"[CAD] tool_extend draw error: {e}")

    def _draw_edge(self, context, obj, ei, col, width, dashed=False):
        mesh = obj.data
        mw = obj.matrix_world
        e = mesh.edges[ei]
        a3 = mw @ mesh.vertices[e.vertices[0]].co
        b3 = mw @ mesh.vertices[e.vertices[1]].co
        a2 = world_to_screen(context, a3)
        b2 = world_to_screen(context, b3)
        if a2 and b2:
            if dashed:
                gfx_dashed_line(a2, b2, col=col, dash_len=6, space_len=4, w=width)
            else:
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

    def _draw_hit_preview(self, context, hit):
        obj, ei, result = hit
        a2 = world_to_screen(context, result.anchor)
        anchor_2d = a2
        new_2d = world_to_screen(context, result.new_point)
        if anchor_2d and new_2d:
            gfx_dashed_line(anchor_2d, new_2d, col=self._COL_EXT_OK, dash_len=6, space_len=4, w=2.0)
            gfx_dot(new_2d, self._COL_EXT_OK, 6)
        self._draw_edge(context, obj, ei, self._COL_EXT_OK, 3.0)

    def _draw_impl(self, context):
        # The pick box stands in for "the cursor" - only draw it while
        # the real OS cursor is actually hidden (mouse over the usable
        # viewport).
        if cursor_fx_visible(self):
            self._draw_pick_box(context)

        for b_obj, b_ei, _A, _B in self._boundaries:
            self._draw_edge(context, b_obj, b_ei, self._COL_BOUNDARY, 3.0)

        if self._fence_active:
            p1_2d = world_to_screen(context, self._fence_p1)
            p2_2d = world_to_screen(context, self._fence_cur_3d) if self._fence_cur_3d else None
            if p1_2d and p2_2d:
                gfx_dashed_line(p1_2d, p2_2d, col=(1.0, 1.0, 1.0, 0.85), dash_len=5, space_len=4, w=1.4)
            if p1_2d:
                gfx_dot(p1_2d, (1.0, 1.0, 1.0, 0.9), 5)
            for hit in self._fence_hits:
                self._draw_hit_preview(context, hit)
            return

        if self._lasso_active:
            pts_2d = self._lasso_points_2d
            if len(pts_2d) > 1:
                gfx_lines(pts_2d, (1.0, 1.0, 1.0, 0.85), 1.6)
            for hit in self._lasso_hits:
                self._draw_hit_preview(context, hit)
            return

        if self._hover is None:
            return

        obj, ei, A, B, t = self._hover

        if self._is_boundary(obj, ei):
            self._draw_edge(context, obj, ei, self._COL_BOUNDARY, 3.0)
            return

        result = self._result

        if result is not None and result.valid:
            a2 = world_to_screen(context, A)
            b2 = world_to_screen(context, B)
            if a2 and b2:
                gfx_lines([a2, b2], self._COL_EXT_OK, 3.0)
            anchor_2d = world_to_screen(context, result.anchor)
            new_2d = world_to_screen(context, result.new_point)
            if anchor_2d and new_2d:
                gfx_dashed_line(anchor_2d, new_2d, col=self._COL_EXT_OK, dash_len=6, space_len=4, w=2.0)
                gfx_dot(new_2d, self._COL_EXT_OK, 6)
            draw_float_dim(self._mx + 60, self._my, f"+{result.length:.4f} m", active=True, locked=True)
        else:
            self._draw_edge(context, obj, ei, self._COL_EXT_BAD, 3.0, dashed=True)
            reason = result.reason if result is not None else ''
            if reason:
                gfx_text(reason, 10, 55, 12, (1.0, 0.35, 0.35, 1.0))
