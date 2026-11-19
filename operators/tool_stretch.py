# ─────────────────────────────────────────────────────────────────────
#  operators/tool_stretch.py
#  CAD_OT_SketchStretch – AutoCAD-style STRETCH command.
#
#  Companion to CAD_OT_SketchMove (tool_move.py): Move only ever
#  translates whole objects; Stretch only ever moves selected POINTS
#  (endpoints, midpoints, vertices, circle quadrant grips, etc.),
#  reshaping the geometry around them while everything unselected
#  stays exactly where it was.
#
#  Workflow
#  --------
#  1.  Activate the Stretch Tool.
#  2.  Select point(s) - either beforehand (Box/Circle/Lasso/click
#      select in the SELECT tool), or right here by clicking in the
#      viewport: click picks the nearest point, Shift adds/toggles.
#  3.  Press Enter (or click empty space with nothing picked, since
#      that already means "nothing more to add") to confirm the
#      selection and specify a BASE point - full OSNAP + OTRACK.
#  4.  Specify a DESTINATION point - OSNAP, OTRACK, Polar Tracking,
#      axis lock, and typed-distance entry, exactly like the other
#      draw tools. The selection is translated live every frame so the
#      base point tracks the cursor/snap result - a genuine live
#      preview, not a ghost overlay - then committed on click/Enter.
#  5.  Esc cancels at *any* stage; once past the BASE step, Esc/RMB
#      also reverts everything already previewed back to its original
#      position before cancelling.
#
#  Stretch never adds/removes vertices or edges - it only ever moves
#  existing vertex.co - so mesh topology and every relationship not
#  involving a selected point are left completely intact. A Midpoint
#  resolves to its edge's two vertices (translating that whole segment
#  rigidly); a circle quadrant grip resolves to a smooth falloff over
#  that whole half of the circle (see _select_circle_half) rather than
#  a single displaced point.
# ─────────────────────────────────────────────────────────────────────

import json
import math
import bpy
from mathutils import Vector

from ..utils.math_utils import (
    build_snap_cache, apply_snaps, apply_axis_lock, apply_polar_tracking,
    mouse_to_3d, world_to_screen, parse_typed_length, format_length, format_angle,
)
from ..utils.otrack import SnapAcquisitionManager, update_tracking
from .. import state as _st
from ..utils.cursor_util import (
    update_modal_cursor, restore_modal_cursor, cursor_fx_visible, init_modal_cursor,
    guarded_modal,
    handle_ui_handoff,
    reset_tpanel_to_select,
)
from ..gfx.primitives import gfx_dot, gfx_text
from ..gfx.markers    import (
    gfx_snap_marker, draw_crosshair, draw_osnap_tracking, draw_vertex_square,
    draw_selected_snap_points,
)
from ..gfx.preview    import draw_reference_ray, draw_direction_guide, draw_dim_preview


_PICK_RADIUS_PX = 12   # screen-space picking radius for the SELECT phase
_REAL_OSNAP_TYPES = (
    'VERTEX', 'ENDPOINT', 'MIDPOINT', 'CENTER', 'INTERSECTION',
    'NEAREST', 'QUADRANT', 'TANGENT', 'PERPENDICULAR', 'PARALLEL',
)


class CAD_OT_SketchStretch(bpy.types.Operator):
    """AutoCAD-style STRETCH: select point(s), pick a base point, pick
    a destination point - only the selected points move, reshaping the
    geometry around them while everything else stays fixed."""
    bl_idname  = "cad.sketch_stretch"
    bl_label   = "CAD Stretch"
    bl_options = {'REGISTER', 'UNDO'}

    # ── invoke ────────────────────────────────────────────────────────
    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._context = context   # kept for helpers that don't take context directly (unit-aware parsing, etc.)
        self._mx, self._my = event.mouse_region_x, event.mouse_region_y
        self._inp          = ''        # typed numeric distance buffer
        self._axis          = None      # None | 'X' | 'Y' | 'Z'
        self._snap_type     = 'FREE'
        self._polar_angle   = None
        self._snap_cache    = build_snap_cache(context)
        self._otrack        = SnapAcquisitionManager()
        self._otrack.reset(clear_points=True)
        self._cur_3d        = mouse_to_3d(context, self._mx, self._my) or Vector((0.0, 0.0, 0.0))
        self._base_pt       = None
        self._orig_wcos     = {}
        self._vert_weight        = {}   # {(obj_name, vi): 0..1} falloff for circle half-stretch
        self._circle_stretch_objs = set()   # obj names whose true-circle metadata is now stale

        # Points: two parallel selection systems both feed this.
        #   1) selected_entities - populated by single click/grip select
        #      in the SELECT tool; already vertex-index based.
        #   2) selected_snap_points - populated by Box/Circle/Lasso
        #      select; stores raw world *coordinates*, not vertex
        #      indices, so each one has to be resolved back to real
        #      mesh geometry:
        #        - an Endpoint/Vertex resolves to that single vertex.
        #        - a Midpoint resolves to the *pair* of vertices that
        #          define its edge - moving it translates that whole
        #          segment rigidly (reshaping the object around it)
        #          rather than doing nothing, since a midpoint isn't
        #          itself a real, independently-movable vertex.
        #        - a circle Quadrant grip resolves to the whole
        #          associated half of the circle, with a smooth
        #          falloff (see _select_circle_half), not just that
        #          one polygon vertex.
        # All of the above are honoured as-is, so however the
        # selection was made before switching to Stretch, it's picked
        # up immediately.
        # {obj_name: set(vert_index)}
        self._sel_verts = {}
        for ent in _st.selection.selected_entities:
            if ent[0] == 'V':
                _, obj_name, vi = ent
                obj = bpy.data.objects.get(obj_name)
                quad_angle, circ = None, None
                if obj is not None:
                    quad_angle = self._vertex_quadrant_angle(obj, vi)
                    if quad_angle is not None:
                        circ = self._circle_params(obj)
                if quad_angle is not None and circ is not None:
                    center, radius = circ
                    self._select_circle_half(obj, quad_angle, center, radius)
                else:
                    self._sel_verts.setdefault(obj_name, set()).add(vi)

        for pt in _st.selection.selected_snap_points:
            wpt = Vector(pt)
            quad = self._find_circle_quadrant(context, wpt)
            if quad is not None:
                obj, quad_angle, center, radius = quad
                self._select_circle_half(obj, quad_angle, center, radius)
                continue
            hit = self._resolve_vertex_at(context, wpt)
            if hit is not None:
                obj, vi = hit
                self._sel_verts.setdefault(obj.name, set()).add(vi)
                continue
            edge_hit = self._resolve_midpoint_at(context, wpt)
            if edge_hit is not None:
                obj, vi_a, vi_b = edge_hit
                s = self._sel_verts.setdefault(obj.name, set())
                s.add(vi_a)
                s.add(vi_b)

        # Connectivity: pull in any other mesh vertex - same object or
        # a different one - that sits exactly on top of an already
        # selected vertex, so a shared/touching join stretches as one
        # instead of tearing apart. Iterated to a fixed point so a
        # chain of touching endpoints (A-B-C) all come along together.
        self._expand_connectivity(context)

        self._phase = 'BASE' if self._sel_verts else 'SELECT'

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "STRETCH"
        context.window_manager.modal_handler_add(self)
        self._update_header(context)
        return {'RUNNING_MODAL'}

    def _update_header(self, context):
        if self._phase == 'SELECT':
            txt = ("STRETCH: click to select point(s)  "
                   "(Shift=add)  |  Enter=confirm  |  Esc=cancel")
        elif self._phase == 'BASE':
            txt = "STRETCH: specify base point  |  Esc=cancel"
        else:
            txt = ("STRETCH: specify destination point  |  X/Y=axis  |  "
                   "type distance  |  Click/Enter=confirm  |  Esc=cancel")
        context.area.header_text_set(txt)

    # ── cancel / finish ──────────────────────────────────────────────
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
        # The point selection is only cleared here, once the stretch
        # is fully committed or cancelled - never while the tool is
        # running, so a selection made before activating Stretch (or
        # built up in its own SELECT phase) stays intact and visibly
        # selected right up until the operation actually ends.
        _st.selection.selected_entities    = []
        _st.selection.active_entity        = None
        _st.selection.selected_snap_points = []
        reset_tpanel_to_select(self, context)
        if context.area:
            context.area.tag_redraw()

    def cancel(self, context):
        """Revert any live-preview translation already applied, then
        clean up. Safe to call from any phase - if the BASE point was
        never committed, _orig_wcos is still empty and this is a pure
        no-op revert."""
        for obj_name, wcos in self._orig_wcos.items():
            obj = bpy.data.objects.get(obj_name)
            if obj is None:
                continue
            mw_inv = obj.matrix_world.inverted()
            mesh   = obj.data
            for vi, orig_co in wcos.items():
                mesh.vertices[vi].co = mw_inv @ orig_co
            mesh.update()
        self._cleanup(context)

    def _finish(self, context):
        """Commit: keep the live-previewed positions as final."""
        for obj_name in self._circle_stretch_objs:
            obj = bpy.data.objects.get(obj_name)
            if obj is None:
                continue
            # A circle that had one half stretched asymmetrically is
            # no longer a true circle - drop the CAD-circle metadata
            # so future OSNAP/OTRACK builds stop reporting stale
            # Quadrant/Center/Tangent points derived from the old
            # (center, radius), and treat it as ordinary freeform
            # geometry (its actual mesh vertices/edges) from now on.
            if "cad_type" in obj:
                del obj["cad_type"]
            if "cad_params" in obj:
                del obj["cad_params"]
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
        self._context = context   # kept in sync every event, for helpers that don't take context directly
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

        if self._phase == 'SELECT':
            return self._modal_select(context, event)
        if self._phase == 'BASE':
            return self._modal_base(context, event)
        return self._modal_dest(context, event)

    # ── point resolution helpers ─────────────────────────────────────
    def _resolve_vertex_at(self, context, world_pt: Vector, tol: float = 1e-4):
        """Find the mesh vertex (if any) whose world position matches
        world_pt - used to translate a selected_snap_points coordinate
        (from Box/Circle/Lasso select) back into a real (obj, vi) that
        can actually be moved/stretched."""
        for obj in context.view_layer.objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            mw = obj.matrix_world
            for vi, v in enumerate(obj.data.vertices):
                if (mw @ v.co - world_pt).length <= tol:
                    return (obj, vi)
        return None

    def _resolve_midpoint_at(self, context, world_pt: Vector, tol: float = 1e-4):
        """Find the mesh edge (if any) whose midpoint matches world_pt,
        returning (obj, vi_a, vi_b). A Midpoint marker isn't itself a
        vertex, so "moving" it means rigidly translating the two
        vertices that define that edge together - that's what actually
        reshapes the geometry around it instead of doing nothing."""
        for obj in context.view_layer.objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            mw = obj.matrix_world
            mesh = obj.data
            for e in mesh.edges:
                va, vb = e.vertices[0], e.vertices[1]
                mid = (mw @ mesh.vertices[va].co + mw @ mesh.vertices[vb].co) / 2.0
                if (mid - world_pt).length <= tol:
                    return (obj, va, vb)
        return None

    def _expand_connectivity(self, context, tol: float = 1e-4):
        """Pull in every other mesh vertex - in this object or any
        other - that's coincident with an already-selected vertex, so
        a shared/touching join (two lines meeting at a point, a
        polyline's shared corner, etc.) stretches as one connected
        unit instead of tearing apart when only one side of the join
        was actually selected. Runs to a fixed point so a chain of
        touching endpoints all come along together."""
        if not self._sel_verts:
            return
        all_verts = []
        for obj in context.view_layer.objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            mw = obj.matrix_world
            for vi, v in enumerate(obj.data.vertices):
                all_verts.append((obj, vi, mw @ v.co))

        for _ in range(4):   # small fixed-point loop for chains
            added = False
            selected_pts = []
            for obj_name, verts in self._sel_verts.items():
                obj = bpy.data.objects.get(obj_name)
                if obj is None:
                    continue
                mw = obj.matrix_world
                for vi in verts:
                    selected_pts.append(mw @ obj.data.vertices[vi].co)

            for obj, vi, wco in all_verts:
                if vi in self._sel_verts.get(obj.name, ()):
                    continue
                for spt in selected_pts:
                    if (wco - spt).length <= tol:
                        self._sel_verts.setdefault(obj.name, set()).add(vi)
                        added = True
                        break

            if not added:
                break

    def _circle_params(self, obj):
        """(center, radius) in world space if obj is a true CAD circle
        (cad_type == 'circle' with valid cad_params), else None."""
        if obj is None or obj.get("cad_type") != "circle":
            return None
        try:
            params = json.loads(obj.get("cad_params", "{}"))
            c = params.get("center")
            r = float(params.get("radius", 0.0))
        except Exception:
            return None
        if c is None or r <= 0:
            return None
        mw = obj.matrix_world
        center = mw @ Vector((c[0], c[1], c[2] if len(c) > 2 else 0.0))
        return center, r

    def _find_circle_quadrant(self, context, world_pt: Vector, tol: float = 1e-3):
        """If world_pt sits on one of a circle object's 4 quadrant
        points, return (obj, quadrant_angle, center, radius)."""
        for obj in context.view_layer.objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            circ = self._circle_params(obj)
            if circ is None:
                continue
            center, r = circ
            for a in (0.0, math.pi * 0.5, math.pi, math.pi * 1.5):
                qpt = Vector((center.x + r * math.cos(a),
                              center.y + r * math.sin(a), center.z))
                if (qpt - world_pt).length <= tol:
                    return (obj, a, center, r)
        return None

    def _vertex_quadrant_angle(self, obj, vi: int, tol: float = 1e-3):
        """If mesh vertex vi of obj sits at one of its own circle's 4
        quadrant points, return that angle - used for the grip-click
        (selected_entities) selection path."""
        circ = self._circle_params(obj)
        if circ is None:
            return None
        center, r = circ
        wco = obj.matrix_world @ obj.data.vertices[vi].co
        for a in (0.0, math.pi * 0.5, math.pi, math.pi * 1.5):
            qpt = Vector((center.x + r * math.cos(a),
                          center.y + r * math.sin(a), center.z))
            if (qpt - wco).length <= tol:
                return a
        return None

    @staticmethod
    def _ang_diff(a: float, b: float) -> float:
        d = abs(a - b) % (2 * math.pi)
        return (2 * math.pi - d) if d > math.pi else d

    def _select_circle_half(self, obj, quad_angle: float, center: Vector, radius: float):
        """Select every vertex of this circle's mesh, weighted by a
        smooth falloff based on angular distance from quad_angle: 1.0
        at the grabbed quadrant point itself, easing smoothly down to
        0.0 at +/-90° (the boundary with the anchored opposite half),
        and staying at 0.0 for the rest of that opposite half. Applying
        the *same* drag delta scaled by this weight (see
        _translate_to()) is what turns "move one vertex" into "stretch
        the whole associated half, smoothly, while the other half
        stays put" - without any sharp kink, since both the weight and
        its slope reach zero at the boundary."""
        mesh = obj.data
        mw   = obj.matrix_world
        s = self._sel_verts.setdefault(obj.name, set())
        for vi, v in enumerate(mesh.vertices):
            wco = mw @ v.co
            ang = math.atan2(wco.y - center.y, wco.x - center.x)
            d   = self._ang_diff(ang, quad_angle)
            t   = min(d / (math.pi * 0.5), 1.0)
            weight = 1.0 - (3.0 * t * t - 2.0 * t * t * t)   # smoothstep falloff
            s.add(vi)
            self._vert_weight[(obj.name, vi)] = weight
        self._circle_stretch_objs.add(obj.name)

    # ── SELECT phase ─────────────────────────────────────────────────
    def _pick(self, context):
        """Nearest mesh vertex to the cursor, screen-space, within
        _PICK_RADIUS_PX."""
        mx, my  = self._mx, self._my
        best_d  = float(_PICK_RADIUS_PX)
        best    = None
        for obj in context.view_layer.objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            mw = obj.matrix_world
            for vi, v in enumerate(obj.data.vertices):
                s2 = world_to_screen(context, mw @ v.co)
                if s2 is None:
                    continue
                d = math.hypot(s2[0] - mx, s2[1] - my)
                if d < best_d:
                    best_d = d
                    best   = (obj, vi)
        return best

    def _modal_select(self, context, event):
        if event.type == 'MOUSEMOVE':
            return {'RUNNING_MODAL'}

        if event.value == 'PRESS':
            if event.type == 'LEFTMOUSE':
                hit = self._pick(context)
                if hit is not None:
                    obj, vi = hit
                    # A circle's quadrant grip selects the whole
                    # associated half (see _select_circle_half) rather
                    # than just that single polygon vertex.
                    quad_angle = self._vertex_quadrant_angle(obj, vi)
                    circ = self._circle_params(obj) if quad_angle is not None else None
                    if not event.shift:
                        self._sel_verts           = {}
                        self._vert_weight         = {}
                        self._circle_stretch_objs = set()
                        if circ is not None:
                            self._select_circle_half(obj, quad_angle, *circ)
                        else:
                            self._sel_verts = {obj.name: {vi}}
                    elif circ is not None:
                        self._select_circle_half(obj, quad_angle, *circ)
                    else:
                        s = self._sel_verts.setdefault(obj.name, set())
                        if vi in s:
                            s.discard(vi)
                            if not s:
                                del self._sel_verts[obj.name]
                        else:
                            s.add(vi)
                elif not event.shift:
                    self._sel_verts = {}
                return {'RUNNING_MODAL'}

            if event.type in {'RET', 'NUMPAD_ENTER', 'SPACE'}:
                if not self._sel_verts:
                    self.report({'WARNING'}, "Select point(s) first")
                    return {'RUNNING_MODAL'}
                self._expand_connectivity(context)
                self._phase = 'BASE'
                self._update_header(context)
                return {'RUNNING_MODAL'}

            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ── BASE phase ───────────────────────────────────────────────────
    def _modal_base(self, context, event):
        if event.type == 'MOUSEMOVE':
            raw = mouse_to_3d(context, self._mx, self._my)
            if raw:
                snapped, stype = apply_snaps(
                    context, raw, self._snap_cache,
                    snap_filter=_st.selection.get_active_stypes(),
                )
                self._snap_type = stype
                update_tracking(self._otrack, snapped, stype)
                self._cur_3d = snapped
            return {'RUNNING_MODAL'}

        if event.value == 'PRESS':
            if event.type == 'LEFTMOUSE':
                self._commit_base(context)
                return {'RUNNING_MODAL'}
            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def _commit_base(self, context):
        self._base_pt   = self._cur_3d.copy()
        self._orig_wcos = {}
        for obj_name, verts in self._sel_verts.items():
            obj = bpy.data.objects.get(obj_name)
            if obj is None:
                continue
            mw = obj.matrix_world
            self._orig_wcos[obj_name] = {
                vi: (mw @ obj.data.vertices[vi].co).copy() for vi in verts
            }
        self._otrack.reset(clear_points=True)
        self._inp   = ''
        self._axis  = None
        self._phase = 'DEST'
        self._update_header(context)

    # ── DEST phase ───────────────────────────────────────────────────
    def _modal_dest(self, context, event):
        if event.type == 'MOUSEMOVE':
            raw = mouse_to_3d(context, self._mx, self._my)
            if raw:
                snapped, stype = apply_snaps(
                    context, raw, self._snap_cache,
                    snap_filter=_st.selection.get_active_stypes(),
                    anchor=self._base_pt,
                )
                self._snap_type = stype
                update_tracking(self._otrack, snapped, stype)

                self._polar_angle = None
                if not self._axis and stype not in _REAL_OSNAP_TYPES:
                    try:
                        polar = apply_polar_tracking(context, raw, self._base_pt)
                        if polar is not None:
                            snapped, self._polar_angle = polar
                    except Exception as e:
                        print(f"[CAD] stretch polar tracking error: {e}")

                if self._axis:
                    snapped = apply_axis_lock(snapped, self._base_pt, self._axis)

                self._cur_3d = snapped
                self._apply_move()
            return {'RUNNING_MODAL'}

        if event.value == 'PRESS':
            ch = event.ascii
            if ch and ch in '0123456789.':
                self._inp += ch
                self._apply_typed_move()
                return {'RUNNING_MODAL'}
            if ch == '-' and not self._inp:
                self._inp = '-'
                return {'RUNNING_MODAL'}
            if event.type == 'BACK_SPACE':
                self._inp = self._inp[:-1]
                self._apply_typed_move()
                return {'RUNNING_MODAL'}

            if event.type in {'X', 'Y', 'Z'}:
                self._axis = None if self._axis == event.type else event.type
                self._apply_move()
                return {'RUNNING_MODAL'}

            if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'}:
                self._finish(context)
                return {'FINISHED'}

            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ── shared translation helpers ──────────────────────────────────
    def _translate_to(self, delta: Vector):
        for obj_name, wcos in self._orig_wcos.items():
            obj = bpy.data.objects.get(obj_name)
            if obj is None:
                continue
            mw_inv = obj.matrix_world.inverted()
            mesh   = obj.data
            for vi, orig_co in wcos.items():
                w = self._vert_weight.get((obj_name, vi), 1.0)
                mesh.vertices[vi].co = mw_inv @ (orig_co + delta * w)
            mesh.update()

    def _apply_move(self):
        self._translate_to(self._cur_3d - self._base_pt)

    def _apply_typed_move(self):
        if not self._inp or self._inp == '-':
            return
        try:
            dist = parse_typed_length(self._context, self._inp)
        except ValueError:
            return
        if self._axis == 'X':
            move_vec = Vector((dist, 0, 0))
        elif self._axis == 'Y':
            move_vec = Vector((0, dist, 0))
        else:
            direction = self._cur_3d - self._base_pt
            if direction.length < 1e-6:
                return
            move_vec = direction.normalized() * dist
        self._translate_to(move_vec)

    # ── draw callback ────────────────────────────────────────────────
    # Orange, matching CAD_OT_SketchModeManager._COL_SELECTED - so a
    # point reads as "still selected" rather than "hovered" (which
    # uses yellow) while Stretch is running.
    _COL_SELECTED = (1.00, 0.50, 0.00, 0.95)

    def _draw_persistent_selection(self, context):
        """Keep every selected point visibly highlighted for the
        *entire* operation - SELECT, BASE, and DEST phases alike - so
        a selection made before (or during) Stretch never appears to
        vanish while choosing the base point, dragging, or previewing.
        Only cleared for real once the stretch is committed or
        cancelled (see _cleanup())."""
        for obj_name, verts in self._sel_verts.items():
            obj = bpy.data.objects.get(obj_name)
            if obj is None:
                continue
            mw = obj.matrix_world
            for vi in verts:
                s2 = world_to_screen(context, mw @ obj.data.vertices[vi].co)
                if s2:
                    draw_vertex_square(
                        s2, 5, fill_col=(*self._COL_SELECTED[:3], 0.30),
                        outline_col=self._COL_SELECTED, outline_w=1.8,
                    )

    def _draw(self, context):
        # The crosshair/OTRACK-marker/snap-marker family below all
        # stand in for "the cursor" - only draw them while the real OS
        # cursor is actually hidden (mouse over the usable viewport).
        show_cursor_fx = cursor_fx_visible(self)

        if show_cursor_fx:
            draw_osnap_tracking(context, self._mx, self._my)
        draw_selected_snap_points(context)
        self._draw_persistent_selection(context)

        if self._phase == 'SELECT':
            if show_cursor_fx:
                draw_crosshair(context, self._mx, self._my, None, show_box=True)
            gfx_text(
                "STRETCH: select point(s), Enter to confirm",
                10, 55, 12, (0.3, 0.9, 1.0, 1.0),
            )
            return

        cur_2d = world_to_screen(context, self._cur_3d) if self._cur_3d else None

        if self._phase == 'BASE':
            if show_cursor_fx:
                if cur_2d:
                    gfx_snap_marker(cur_2d, self._snap_type)
                cx, cy = cur_2d if cur_2d else (self._mx, self._my)
                draw_crosshair(context, cx, cy, None, show_box=False)
            gfx_text("STRETCH: specify base point", 10, 55, 12, (0.3, 0.9, 1.0, 1.0))
            return

        # DEST phase - live dimension/guide overlay, reusing the exact
        # same preview primitives as CAD_OT_DrawLine (gfx.preview) so
        # every transform tool's anchor->cursor reference looks and
        # behaves identically. The geometry itself already shows the
        # live preview since _apply_move() translates the real
        # vertices every frame.
        base_2d = world_to_screen(context, self._base_pt)
        if base_2d and cur_2d:
            draw_reference_ray(base_2d, cur_2d)
            gfx_dot(base_2d, (0.3, 0.9, 1.0, 0.9), 6)
            gfx_dot(cur_2d,   (0.3, 0.9, 1.0, 0.9), 6)

            if self._polar_angle is not None:
                draw_direction_guide(
                    context, world_to_screen, self._base_pt, self._cur_3d, cur_2d,
                )

            delta = self._cur_3d - self._base_pt
            dist  = delta.length
            if dist > 0.001:
                disp = self._inp + "▌" if self._inp else format_length(context, dist)
                draw_dim_preview(base_2d, cur_2d, disp, active=True)
            if self._polar_angle is not None:
                gfx_text(
                    f"Polar: {format_angle(context, math.radians(self._polar_angle))}",
                    cur_2d[0] + 14, cur_2d[1] + 14, 11, (1.0, 1.0, 1.0, 0.95),
                )

        if show_cursor_fx:
            if cur_2d:
                gfx_snap_marker(cur_2d, self._snap_type)
            cx, cy = cur_2d if cur_2d else (self._mx, self._my)
            draw_crosshair(context, cx, cy, self._axis, show_box=False)

        delta = self._cur_3d - self._base_pt
        ax    = f"  [{self._axis}]" if self._axis else ""
        gfx_text(
            f"STRETCH{ax}   dX {delta.x:+.3f}   dY {delta.y:+.3f}",
            10, 55, 12, (0.3, 0.9, 1.0, 1.0),
        )
