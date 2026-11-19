# ─────────────────────────────────────────────────────────────────────
#  operators/tool_offset.py
#  CAD_OT_DrawOffset – AutoCAD-style OFFSET command.
#
#  Workflow (matches AutoCAD's own prompt order: distance first, then
#  object, then side)
#  --------
#  1.  Activate the Offset Tool. The tool opens straight into a
#      "Specify offset distance" prompt - type a number and press
#      Enter (or just press Enter/click to accept the last-used
#      distance, shown as the default).
#  2.  Click a supported CAD object (a simple open or closed chain of
#      mesh edges - anything the Line/Arc/Rect/Circle/Spline tools
#      produce).
#  3.  Move the mouse to either side of it - a live parallel copy is
#      previewed at the fixed distance from step 1, with the cursor
#      only choosing *which side* (AutoCAD's "specify point on side to
#      offset" behaviour). A dynamic distance box follows the cursor;
#      typing a new number here overrides the distance from step 1 for
#      this and all following offsets in the session.
#  4.  Click (or Enter) confirms: the offset geometry is added directly
#      into the source object's own mesh, alongside the untouched
#      original - no new object, mesh, collection, or layer is
#      created; original and offset simply coexist as two separate
#      (unconnected) loops in the same mesh data. The source
#      selection is then dropped, so the next pick is a deliberate,
#      explicit choice rather than silently stamping out further
#      copies on every click.
#  5.  Esc/right-click backs out one stage at a time - drop the side
#      selection back to picking an object, drop the object selection
#      back to the distance prompt, then exit the tool entirely.
#
#  Multiple Offset mode (press M any time to toggle):
#  When on, confirming an offset does NOT drop the source selection -
#  instead the offset geometry just created becomes the source for the
#  next one, staying in the side/distance step so the next offset can
#  be placed immediately: Original -> Offset 1 -> Offset 2 -> ...,
#  each one sequentially offset from the previous, all still merged
#  into the same mesh/object as everything else (never an Array
#  modifier, never recomputed from the original each time). Press M
#  again to turn it off - Esc still exits the tool at any point,
#  keeping every offset already confirmed.
#
#  Corners are joined by intersecting each pair of adjacent offset
#  segments' infinite lines (a mitered join) rather than naively
#  translating every vertex, so square/rectangular corners and
#  multi-segment chains stay sharp and continuous instead of
#  splitting apart at the offset distance - closed loops (e.g. a
#  rectangle or a circle's polygon approximation) are also joined at
#  their wrap-around corner.
#
#  Kept independent from the other tools: this file defines its own
#  polyline/adjacency extraction, pick, and offset-geometry math
#  rather than importing helpers from tool_array.py or tool_fillet.py,
#  even though the ideas are similar - so Offset can't be broken by,
#  or accidentally break, changes to those tools.
# ─────────────────────────────────────────────────────────────────────

import math
import bpy
import bmesh
from mathutils import Vector

from ..utils.math_utils import build_snap_cache, apply_snaps, mouse_to_3d, world_to_screen, parse_typed_length, format_length
from ..utils.otrack import SnapAcquisitionManager, update_tracking
from .. import state as _st
from ..utils.cursor_util import (
    update_modal_cursor, restore_modal_cursor, cursor_fx_visible, init_modal_cursor,
    guarded_modal,
    handle_ui_handoff,
    reset_tpanel_to_select,
)
from ..gfx.primitives import gfx_text, gfx_lines, gfx_dot, gfx_dashed_line
from ..gfx.markers    import (
    gfx_snap_marker, draw_crosshair, draw_osnap_tracking, draw_selected_snap_points,
)
from ..gfx.dims       import draw_float_dim

_PICK_RADIUS_PX = 10
_MIN_DIST       = 1e-4


# ── self-contained 2D/3D geometry helpers ──────────────────────────────

def _closest_point_on_segment_2d(p, a, b):
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-9:
        return a, 0.0
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return (ax + dx * t, ay + dy * t), t


def _closest_point_on_segment_3d(p, a, b):
    ab = b - a
    L2 = ab.length_squared
    if L2 < 1e-12:
        return a.copy()
    t = max(0.0, min(1.0, (p - a).dot(ab) / L2))
    return a + ab * t


def _closest_point_on_polyline(pts, target, closed):
    """Returns (closest_point, segment_index) - segment_index i means
    the segment from pts[i] to pts[(i+1) % len(pts)]."""
    n = len(pts)
    segs = range(n) if closed else range(n - 1)
    best_pt, best_d2, best_i = pts[0], float('inf'), 0
    for i in segs:
        a, b = pts[i], pts[(i + 1) % n]
        cp = _closest_point_on_segment_3d(target, a, b)
        d2 = (target - cp).length_squared
        if d2 < best_d2:
            best_d2, best_pt, best_i = d2, cp, i
    return best_pt, best_i


def _mesh_edge_adjacency(mesh):
    """vertex-index -> [neighbour vertex-index, ...] for every edge."""
    adj = {}
    for e in mesh.edges:
        a, b = e.vertices[0], e.vertices[1]
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    return adj


def _walk_chain(adj, start, comp_size):
    """Walk a simple open/closed chain starting at vertex *start*,
    using adjacency *adj*. Returns (ordered_vertex_indices, closed) or
    (None, False) if it isn't a simple chain (branches anywhere)."""
    closed = False
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
    if len(order) != comp_size:
        return None, False  # this component branches - not a simple chain
    return order, closed



def _component_polyline(obj, edge_index):
    """Extract the single connected loop that contains mesh edge
    *edge_index*, ignoring every other edge in the object. This is
    what makes re-offsetting work after a previous offset merged its
    result into the same mesh: the mesh as a whole is no longer one
    simple chain, but each individual loop inside it still is, so
    picking near a specific loop (original or a previous offset)
    offsets just that loop. Returns (points, closed) or (None, False)
    if that loop branches (not simple)."""
    mesh = obj.data
    if edge_index is None or edge_index >= len(mesh.edges):
        return None, False
    adj = _mesh_edge_adjacency(mesh)

    seed = mesh.edges[edge_index].vertices[0]
    comp = {seed}
    stack = [seed]
    while stack:
        v = stack.pop()
        for n in adj.get(v, []):
            if n not in comp:
                comp.add(n)
                stack.append(n)
    if any(len(adj.get(v, [])) > 2 for v in comp):
        return None, False

    ends = [v for v in comp if len(adj.get(v, [])) == 1]
    start = ends[0] if len(ends) == 2 else (next(iter(comp)) if len(ends) == 0 else None)
    if start is None:
        return None, False
    order, closed = _walk_chain(adj, start, len(comp))
    if order is None:
        return None, False
    mw = obj.matrix_world
    return [mw @ mesh.vertices[i].co for i in order], closed


def _line_isect_2d(p1, p2, p3, p4):
    """Infinite-line X/Y intersection of line p1-p2 and line p3-p4, or
    None if parallel."""
    d1x, d1y = p2.x - p1.x, p2.y - p1.y
    d2x, d2y = p4.x - p3.x, p4.y - p3.y
    denom = d1x * d2y - d1y * d2x
    if abs(denom) < 1e-9:
        return None
    t = ((p3.x - p1.x) * d2y - (p3.y - p1.y) * d2x) / denom
    return Vector((p1.x + d1x * t, p1.y + d1y * t, 0.0))


def _seg_left_normal(a, b):
    d = b - a
    d.z = 0.0
    if d.length < 1e-9:
        return Vector((0.0, 1.0, 0.0))
    d = d.normalized()
    return Vector((-d.y, d.x, 0.0))


def _offset_polyline(pts, distance, closed):
    """Parallel offset of *pts* by signed *distance* (positive = left
    of each segment's travel direction). Adjacent offset segments are
    joined by intersecting their infinite lines (a mitered corner)
    rather than just translating each vertex independently, so sharp
    corners stay sharp and connected instead of gapping open at the
    offset distance. Falls back to a plain translated endpoint at any
    corner where the two segments are parallel (no intersection)."""
    n = len(pts)
    if n < 2 or abs(distance) < 1e-9:
        return list(pts)

    segs = ([(pts[i], pts[(i + 1) % n]) for i in range(n)] if closed
            else [(pts[i], pts[i + 1]) for i in range(n - 1)])
    offset_segs = [(a + _seg_left_normal(a, b) * distance,
                     b + _seg_left_normal(a, b) * distance) for a, b in segs]
    m = len(offset_segs)

    def joint(i):
        a1, b1 = offset_segs[(i - 1) % m]
        a2, b2 = offset_segs[i % m]
        ip = _line_isect_2d(a1, b1, a2, b2)
        return ip if ip is not None else b1

    if closed:
        return [joint(i) for i in range(m)]

    out = [offset_segs[0][0]]
    for i in range(1, m):
        out.append(joint(i))
    out.append(offset_segs[-1][1])
    return out


# ── operator ─────────────────────────────────────────────────────────

class CAD_OT_DrawOffset(bpy.types.Operator):
    """AutoCAD-style OFFSET: pick a curve/edge chain, move the mouse to
    choose the side and distance (or type an exact value), click to
    create a parallel copy - the original is always kept."""
    bl_idname  = "cad.draw_offset"
    bl_label   = "CAD Offset"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._context = context   # kept for helpers that don't take context directly (unit-aware parsing, etc.)
        self._phase      = 'DISTANCE'  # 'DISTANCE' -> 'SELECT' -> 'SIDE'
        self._src_obj    = None
        self._src_pts    = []
        self._src_closed = False
        self._hover_obj  = None
        self._hover_edge = None
        self._multi      = False  # Multiple Offset mode (toggled with M)

        self._distance   = _st.offset_distance if abs(_st.offset_distance) > _MIN_DIST else 1.0
        self._magnitude  = abs(self._distance)   # fixed size chosen in the DISTANCE step
        self._inp        = ''
        self._raw        = Vector((0, 0, 0))
        self._cur        = Vector((0, 0, 0))
        self._snap_type  = 'FREE'

        # Measure-the-distance-by-clicking-two-points sub-mode, same
        # mechanic as Fillet's radius measuring: click once to place
        # the first point, move to the second - the distance between
        # them becomes the offset distance - and a typed number while
        # measuring is read as an exact distance along that same ray
        # instead of a free-form value.
        self._dist_pt1       = None
        self._dist_typed_ray = None

        self._mx, self._my = event.mouse_region_x, event.mouse_region_y
        self._snap_cache = build_snap_cache(context)
        self._otrack     = SnapAcquisitionManager()
        self._otrack.reset(clear_points=True)

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "OFFSET"
        context.window_manager.modal_handler_add(self)
        self._hdr(context)
        context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def cancel(self, context):
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

    def _hdr(self, context):
        multi_tag = "  [MULTIPLE]" if self._multi else ""
        if self._phase == 'DISTANCE':
            if self._dist_pt1 is not None:
                typed = f"  [typing: {self._inp}]" if self._inp else ""
                context.area.header_text_set(
                    f"OFFSET{multi_tag}: measuring distance {format_length(context, self._magnitude)}{typed}  |  "
                    f"click 2nd point to set  ·  type number=exact along this direction  "
                    f"M=toggle Multiple  Esc=cancel measuring"
                )
            else:
                typed = f"  [typing: {self._inp}]" if self._inp else ""
                context.area.header_text_set(
                    f"OFFSET{multi_tag}: specify offset distance (default {format_length(context, self._magnitude)}){typed}  |  "
                    f"click 2 points to measure it  ·  or type a number then Enter  ·  "
                    f"Enter/Click empty=accept default  M=toggle Multiple  |  Esc=exit"
                )
        elif self._phase == 'SELECT':
            context.area.header_text_set(
                f"OFFSET{multi_tag}: distance {format_length(context, self._magnitude)}  |  "
                f"click a line/arc/rect/circle/spline to offset  M=toggle Multiple  |  Esc=change distance"
            )
        else:  # SIDE
            typed = f"  [typing: {self._inp}]" if self._inp else ""
            chain_hint = "  (next offset chains from this one)" if self._multi else ""
            context.area.header_text_set(
                f"OFFSET{multi_tag}: distance {format_length(context, self._magnitude)}{typed}{chain_hint}  |  "
                f"move mouse=choose side  type number=change distance (-=flip side)  "
                f"Backspace=edit  Click/Enter=confirm  M=toggle Multiple  Esc=drop selection"
            )

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
        self._context = context   # kept in sync every event, for helpers that don't take context directly
        if not context.scene.cad_settings.sketch_mode:
            self.cancel(context)
            return {'CANCELLED'}

        context.area.tag_redraw()
        self._mx, self._my = event.mouse_region_x, event.mouse_region_y

        if event.type == 'MIDDLEMOUSE':
            return {'PASS_THROUGH'}
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN'}:
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            self._on_move(context)
            return {'RUNNING_MODAL'}

        if event.type == 'M' and event.value == 'PRESS':
            self._multi = not self._multi
            self._hdr(context)
            return {'RUNNING_MODAL'}

        if self._phase == 'DISTANCE':
            return self._modal_distance(context, event)
        if self._phase == 'SELECT':
            return self._modal_select(context, event)
        return self._modal_side(context, event)

    def _modal_distance(self, context, event):
        if event.value != 'PRESS':
            return {'RUNNING_MODAL'}
        ch = event.ascii

        # ── measuring sub-mode: first click placed a point, typed
        #    digits are an exact distance along the ray to the cursor
        #    (same convention as Fillet's radius measuring) ──
        if self._dist_pt1 is not None:
            if ch and ch in '0123456789.':
                if not self._inp:
                    direction = self._raw - self._dist_pt1
                    if direction.length < 1e-6:
                        direction = Vector((1.0, 0.0, 0.0))
                    self._dist_typed_ray = (self._dist_pt1, direction.normalized())
                self._inp += ch
                self._update_dist_measure_cur()
                self._magnitude = max(_MIN_DIST, (self._cur - self._dist_pt1).length)
                self._hdr(context)
                return {'RUNNING_MODAL'}

            if event.type == 'BACK_SPACE' and self._inp:
                self._inp = self._inp[:-1]
                if not self._inp:
                    self._dist_typed_ray = None
                self._update_dist_measure_cur()
                self._magnitude = max(_MIN_DIST, (self._cur - self._dist_pt1).length)
                self._hdr(context)
                return {'RUNNING_MODAL'}

            if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'}:
                self._magnitude = max(_MIN_DIST, (self._cur - self._dist_pt1).length)
                self._distance  = self._magnitude
                _st.offset_distance  = self._magnitude
                self._dist_pt1       = None
                self._dist_typed_ray = None
                self._inp             = ''
                self._otrack.reset(clear_points=True)
                self._phase = 'SELECT'
                self._hdr(context)
                return {'RUNNING_MODAL'}

            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self._dist_pt1       = None
                self._dist_typed_ray = None
                self._inp             = ''
                self._hdr(context)
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        # ── not measuring yet: click starts a 2-point measurement,
        #    typed digits enter a distance directly, Enter/click on
        #    nothing accepts the current default ──
        if ch and ch in '0123456789.-':
            if ch == '-' and self._inp:
                return {'RUNNING_MODAL'}  # only meaningful as the leading char
            self._inp += ch
            self._apply_typed()
            self._hdr(context)
            return {'RUNNING_MODAL'}

        if event.type == 'BACK_SPACE' and self._inp:
            self._inp = self._inp[:-1]
            if self._inp:
                self._apply_typed()
            self._hdr(context)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and not self._inp:
            # Start measuring: distance between this click and the
            # next becomes the offset distance - never creates or
            # modifies geometry, purely a measurement like Fillet's.
            self._dist_pt1       = self._cur.copy()
            self._dist_typed_ray = None
            self._inp             = ''
            self._hdr(context)
            return {'RUNNING_MODAL'}

        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'}:
            if abs(self._magnitude) < _MIN_DIST:
                self.report({'WARNING'}, "Offset distance can't be zero")
                return {'RUNNING_MODAL'}
            _st.offset_distance = self._magnitude
            self._inp   = ''
            self._phase = 'SELECT'
            self._hdr(context)
            return {'RUNNING_MODAL'}

        if event.type in {'RIGHTMOUSE', 'ESC'}:
            self.cancel(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def _update_dist_measure_cur(self):
        """Resolve self._cur while measuring: a typed distance (if
        any) overrides the raw snapped cursor position, read along the
        ray from the first click - same convention as Fillet."""
        if self._inp and self._dist_typed_ray is not None:
            try:
                val = parse_typed_length(self._context, self._inp)
                origin, direction = self._dist_typed_ray
                self._cur = origin + direction * val
                return
            except ValueError:
                pass
        self._cur = self._raw.copy()

    def _modal_select(self, context, event):
        if event.value == 'PRESS':
            if event.type == 'LEFTMOUSE':
                if self._hover_obj is not None:
                    pts, closed = _component_polyline(self._hover_obj, self._hover_edge)
                    if not pts or len(pts) < 2:
                        self.report({'WARNING'}, "That loop isn't a simple line/curve chain")
                        return {'RUNNING_MODAL'}
                    self._src_obj    = self._hover_obj
                    self._src_pts    = pts
                    self._src_closed = closed
                    self._distance   = self._magnitude
                    self._phase      = 'SIDE'
                    self._inp        = ''
                    self._hdr(context)
                return {'RUNNING_MODAL'}
            if event.type in {'RIGHTMOUSE', 'ESC'}:
                # Back out one stage: drop straight back into the
                # distance prompt (pre-filled with the current value)
                # rather than exiting the whole tool.
                self._inp   = ''
                self._phase = 'DISTANCE'
                self._hdr(context)
                return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'}

    def _modal_side(self, context, event):
        if event.value == 'PRESS':
            ch = event.ascii
            if ch and ch in '0123456789.-':
                if ch == '-' and self._inp:
                    return {'RUNNING_MODAL'}  # only meaningful as the leading char
                self._inp += ch
                self._apply_typed()
                return {'RUNNING_MODAL'}

            if event.type == 'BACK_SPACE' and self._inp:
                self._inp = self._inp[:-1]
                if self._inp:
                    self._apply_typed()
                return {'RUNNING_MODAL'}

            if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'}:
                self._confirm(context)
                return {'RUNNING_MODAL'}

            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self._phase      = 'SELECT'
                self._src_obj    = None
                self._src_pts    = []
                self._src_closed = False
                self._inp         = ''
                self._hover_obj   = None
                self._hover_edge  = None
                self._hdr(context)
                return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'}

    # ── snapping / dynamic input ─────────────────────────────────────
    def _on_move(self, context):
        raw3d = mouse_to_3d(context, self._mx, self._my)
        if raw3d is None:
            return
        self._raw = raw3d
        snapped, stype = apply_snaps(context, raw3d, self._snap_cache,
                                      snap_filter=_st.selection.get_active_stypes())
        self._snap_type = stype
        self._cur = snapped
        update_tracking(self._otrack, raw3d, stype)

        if self._phase == 'SELECT':
            self._hover_obj, self._hover_edge = self._pick_edge_at_cursor(context)
        elif self._phase == 'DISTANCE' and self._dist_pt1 is not None:
            self._update_dist_measure_cur()
            self._magnitude = max(_MIN_DIST, (self._cur - self._dist_pt1).length)
            self._hdr(context)
        elif self._phase == 'SIDE' and not self._inp:
            self._update_distance_from_cursor()

    def _update_distance_from_cursor(self):
        """Distance's *magnitude* was already fixed in the DISTANCE
        step - the mouse here only chooses which side of the geometry
        (the sign), matching AutoCAD's 'specify point on side to
        offset' prompt."""
        if len(self._src_pts) < 2:
            return
        cp, i = _closest_point_on_polyline(self._src_pts, self._cur, self._src_closed)
        n = len(self._src_pts)
        a, b = self._src_pts[i], self._src_pts[(i + 1) % n]
        nrm = _seg_left_normal(a, b)
        raw_signed = (self._cur - cp).dot(nrm)
        sign = 1.0 if raw_signed >= 0.0 else -1.0
        self._distance = sign * abs(self._magnitude)

    def _apply_typed(self):
        try:
            val = parse_typed_length(self._context, self._inp)
        except ValueError:
            return
        self._distance  = val
        self._magnitude = abs(val)

    def _pick_edge_at_cursor(self, context):
        """Nearest (object, edge_index) to the cursor, within a small
        pixel radius - the edge index is what lets _component_polyline
        pick out just the single loop under the cursor from a mesh
        that may contain several (original + one or more previous
        offsets, all merged into the same object)."""
        mx, my = self._mx, self._my
        best_d, best_obj, best_edge = float(_PICK_RADIUS_PX), None, None
        for obj in context.view_layer.objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            mesh = obj.data
            if len(mesh.edges) == 0:
                continue
            mw = obj.matrix_world
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
                    best_d, best_obj, best_edge = d, obj, ei
        return best_obj, best_edge

    # ── confirm ──────────────────────────────────────────────────────
    def _confirm(self, context):
        if abs(self._distance) < _MIN_DIST:
            self.report({'WARNING'}, "Offset distance is zero")
            return
        obj = self._src_obj
        if obj is None:
            return
        off_pts = _offset_polyline(self._src_pts, self._distance, self._src_closed)

        # Add the offset geometry straight into the source object's
        # own mesh, alongside the untouched original - no new object,
        # mesh, collection, or layer. The two loops stay disconnected
        # (no shared vertices/edges between them), so the original
        # shape is completely unmodified; the offset is simply extra
        # geometry living in the same mesh data.
        mw_inv = obj.matrix_world.inverted()
        bm = bmesh.new()
        bm.from_mesh(obj.data)

        new_verts = [bm.verts.new(mw_inv @ p) for p in off_pts]
        bm.verts.ensure_lookup_table()
        n = len(new_verts)
        pairs = ([(i, (i + 1) % n) for i in range(n)] if self._src_closed
                 else [(i, i + 1) for i in range(n - 1)])
        for a, b in pairs:
            v1, v2 = new_verts[a], new_verts[b]
            if not bm.edges.get((v1, v2)):
                bm.edges.new((v1, v2))

        bm.to_mesh(obj.data)
        bm.free()
        obj.data.update()

        self._magnitude = abs(self._distance)
        _st.offset_distance = self._distance

        if self._multi:
            # Multiple Offset mode: chain sequentially - the offset
            # just created becomes the source for the next one, still
            # in the same mesh/object (never an Array, never computed
            # from the original each time). Stay in SIDE, ready
            # immediately for the next side/distance.
            self._src_pts   = off_pts          # world-space points of the offset just made
            self._src_closed = self._src_closed  # open/closed-ness never changes from offsetting
            self._inp        = ''
            self._hdr(context)
            return

        # Single-offset mode: drop the source selection and go back
        # to SELECT instead of silently staying ready to stamp out
        # further copies on every subsequent click - avoids piling up
        # duplicate offset geometry from the same source by accident.
        # Offsetting the same object again (or the original loop, or
        # any other object) is still just a click away - the pick in
        # SELECT works per-loop, not per-object, so the original and
        # every previous offset merged into this object's mesh remain
        # individually selectable.
        self._phase      = 'SELECT'
        self._src_obj    = None
        self._src_pts    = []
        self._src_closed = False
        self._inp         = ''
        self._hover_obj   = None
        self._hover_edge  = None
        self._hdr(context)

    # ── draw callback ─────────────────────────────────────────────────
    def _draw(self, context):
        try:
            self._draw_impl(context)
        except Exception as e:
            print(f"[CAD] tool_offset draw error: {e}")

    def _draw_polyline(self, context, pts, closed, col, w):
        seq = list(pts) + ([pts[0]] if closed and pts else [])
        pts_2d = [world_to_screen(context, p) for p in seq]
        pts_2d = [p for p in pts_2d if p is not None]
        if len(pts_2d) >= 2:
            gfx_lines(pts_2d, col, w)

    def _draw_impl(self, context):
        # The OTRACK marker / snap marker / typed-value tooltip /
        # crosshair all stand in for "the cursor" - only draw them
        # while the real OS cursor is actually hidden (mouse over the
        # usable viewport).
        show_cursor_fx = cursor_fx_visible(self)

        draw_selected_snap_points(context)
        if show_cursor_fx:
            draw_osnap_tracking(context, self._mx, self._my)
        c2 = world_to_screen(context, self._cur)
        if show_cursor_fx and c2:
            gfx_snap_marker(c2, self._snap_type)

        if self._phase == 'DISTANCE':
            if self._dist_pt1 is not None:
                p1_2d = world_to_screen(context, self._dist_pt1)
                if p1_2d:
                    gfx_dot(p1_2d, (0.3, 0.9, 1.0, 0.9), 7)
                if p1_2d and c2:
                    gfx_dashed_line(p1_2d, c2, col=(0.3, 1.0, 0.4, 0.9), dash_len=6, space_len=4, w=1.6)
                disp = (self._inp + "\u258c") if self._inp else format_length(context, self._magnitude)
                if show_cursor_fx and c2:
                    draw_float_dim(c2[0] + 50, c2[1], disp, active=True, locked=bool(self._inp))
                gfx_text("OFFSET: click 2nd point to set the measured distance", 10, 55, 12, (1.0, 0.85, 0.2, 1.0))
            else:
                disp = (self._inp + "\u258c") if self._inp else format_length(context, self._magnitude)
                if show_cursor_fx and c2:
                    draw_float_dim(c2[0] + 50, c2[1], disp, active=True, locked=bool(self._inp))
                gfx_text("OFFSET: click 2 points to measure distance, or type a number",
                          10, 55, 12, (1.0, 0.85, 0.2, 1.0))
        elif self._phase == 'SELECT':
            if self._hover_obj is not None:
                pts, closed = _component_polyline(self._hover_obj, self._hover_edge)
                if pts:
                    self._draw_polyline(context, pts, closed, (0.3, 0.9, 1.0, 0.95), 2.4)
            multi_tag = "  [MULTIPLE]" if self._multi else ""
            gfx_text(f"OFFSET{multi_tag}  distance {format_length(context, self._magnitude)}  -  click a line/arc/rect/circle/spline",
                      10, 55, 12, (1.0, 0.85, 0.2, 1.0))
        else:  # SIDE
            # Source geometry, dim; offset preview, bright.
            self._draw_polyline(context, self._src_pts, self._src_closed, (0.55, 0.55, 0.55, 0.7), 1.6)
            off_pts = _offset_polyline(self._src_pts, self._distance, self._src_closed)
            self._draw_polyline(context, off_pts, self._src_closed, (0.3, 1.0, 0.4, 0.95), 2.4)

            disp = (self._inp + "\u258c") if self._inp else format_length(context, self._distance)
            if show_cursor_fx and c2:
                draw_float_dim(c2[0] + 50, c2[1], disp, active=True, locked=bool(self._inp))
            multi_tag = "  [MULTIPLE]" if self._multi else ""
            gfx_text(f"OFFSET{multi_tag}  distance {format_length(context, self._distance)}", 10, 55, 12, (1.0, 0.85, 0.2, 1.0))

        if show_cursor_fx:
            cx, cy = c2 if c2 else (self._mx, self._my)
            draw_crosshair(context, cx, cy, None, show_box=False)
