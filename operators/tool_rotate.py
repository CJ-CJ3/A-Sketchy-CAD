# ─────────────────────────────────────────────────────────────────────
#  operators/tool_rotate.py
#  CAD_OT_SketchRotate – AutoCAD-style ROTATE command.
#
#  Unlike Move (objects only) and Stretch (points only), Rotate
#  supports both at once, matching the AutoCAD ROTATE command: whole
#  objects rotate rigidly about the pivot (position AND orientation),
#  while selected points rotate individually about the pivot,
#  reshaping the geometry around them - exactly like Stretch, just
#  with a rotation instead of a translation. A circle quadrant grip
#  still gets the smooth half-circle falloff (see _select_circle_half)
#  so it doesn't pinch into a sharp spike.
#
#  Workflow
#  --------
#  1.  Activate the Rotate Tool.
#  2.  Select object(s) and/or point(s) - either beforehand, or right
#      here: plain click = whole object, Ctrl+click = a single point,
#      Shift adds to either.
#  3.  Press Enter (or click empty space with nothing picked) to
#      confirm the selection and specify a PIVOT point - full OSNAP +
#      OTRACK.
#  4.  Specify the rotation angle - either by dragging (the direction
#      from the pivot to the cursor *is* the angle, measured CCW from
#      the world +X axis, exactly like typing that angle directly)
#      with full OSNAP/OTRACK/Polar Tracking support, or by typing the
#      angle in degrees. A dashed guide arc + live "∠ NN.N°" readout
#      track the sweep continuously.
#  5.  Esc cancels at *any* stage; once past the PIVOT step, Esc/RMB
#      also reverts everything already previewed back to its original
#      position/orientation before cancelling.
#
#  Rotate never adds/removes vertices, edges, or objects - only
#  position/orientation (whole objects) or vertex.co (points) change -
#  so mesh topology and every relationship not involving a selected
#  point/object are left completely intact.
# ─────────────────────────────────────────────────────────────────────

import json
import math
import bpy
from mathutils import Vector

from ..utils.math_utils import (
    build_snap_cache, apply_snaps, apply_polar_tracking,
    mouse_to_3d, world_to_screen, world_ang_to_display, display_ang_to_rad,
    format_angle,
)
from ..utils.otrack import SnapAcquisitionManager, update_tracking
from .. import state as _st
from ..utils.cursor_util import (
    update_modal_cursor, restore_modal_cursor, cursor_fx_visible, init_modal_cursor,
    guarded_modal,
    handle_ui_handoff,
    reset_tpanel_to_select,
)
from ..gfx.primitives import (
    gfx_dot, gfx_text, gfx_dashed_arc, gfx_arc_arrow,
)
from ..gfx.markers    import (
    gfx_snap_marker, draw_crosshair, draw_osnap_tracking, draw_vertex_square,
    draw_selected_snap_points, draw_angle_tooltip,
)
from ..gfx.dims      import draw_float_dim
from ..gfx.preview    import draw_reference_ray, draw_direction_guide


_PICK_RADIUS_PX = 12   # screen-space picking radius for the SELECT phase
_REAL_OSNAP_TYPES = (
    'VERTEX', 'ENDPOINT', 'MIDPOINT', 'CENTER', 'INTERSECTION',
    'NEAREST', 'QUADRANT', 'TANGENT', 'PERPENDICULAR', 'PARALLEL',
)


class CAD_OT_SketchRotate(bpy.types.Operator):
    """AutoCAD-style ROTATE: select object(s)/point(s), pick a pivot,
    specify a rotation angle - objects rotate rigidly about the pivot,
    selected points rotate individually, reshaping the geometry around
    them."""
    bl_idname  = "cad.sketch_rotate"
    bl_label   = "CAD Rotate"
    bl_options = {'REGISTER', 'UNDO'}

    # ── invoke ────────────────────────────────────────────────────────
    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._mx, self._my = event.mouse_region_x, event.mouse_region_y
        self._inp           = ''        # typed numeric angle buffer (degrees)
        self._snap_type      = 'FREE'
        self._polar_angle    = None
        self._snap_cache     = build_snap_cache(context)
        self._otrack         = SnapAcquisitionManager()
        self._otrack.reset(clear_points=True)
        self._cur_3d         = mouse_to_3d(context, self._mx, self._my) or Vector((0.0, 0.0, 0.0))
        self._pivot          = None
        self._angle          = 0.0        # current rotation delta (radians)
        self._initial_angle  = 0.0        # object's initial rotation for display baseline
        self._start_locs     = {}   # {obj_name: (orig_location, orig_rotation_euler_z)}
        self._orig_wcos      = {}   # {obj_name: {vi: orig_world_co}}
        self._vert_weight        = {}   # {(obj_name, vi): 0..1} falloff for circle half-rotate
        self._circle_stretch_objs = set()   # obj names whose true-circle metadata is now stale

        # Points: same two parallel selection systems, resolution, and
        # connectivity expansion as the Stretch Tool (see tool_stretch.py
        # for the detailed rationale on each case).
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

        # Objects: from the current Blender selection, mesh only.
        # Whole-object selection always wins over any point selection
        # that happens to reference the *same* object - so "I selected
        # my object" (context.selected_objects) reliably rotates the
        # entire object, even if one of its vertices also happens to
        # be registered as a point (e.g. a leftover/incidental pick).
        # Point-only rotation is reserved for objects that were never
        # captured as a whole-object selection in the first place -
        # i.e. genuine point-selection mode (Ctrl+click here, or a
        # Box/Lasso/click point pick on an otherwise-unselected object).
        self._sel_objects = [o for o in context.selected_objects if o.type == 'MESH']
        sel_obj_names = {o.name for o in self._sel_objects}
        self._sel_verts = {
            name: verts for name, verts in self._sel_verts.items()
            if name not in sel_obj_names
        }
        self._vert_weight = {
            key: w for key, w in self._vert_weight.items() if key[0] not in sel_obj_names
        }
        self._circle_stretch_objs -= sel_obj_names

        self._expand_connectivity(context)

        self._phase = 'PIVOT' if (self._sel_objects or self._sel_verts) else 'SELECT'

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "ROTATE"
        context.window_manager.modal_handler_add(self)
        self._update_header(context)
        return {'RUNNING_MODAL'}

    def _update_header(self, context):
        if self._phase == 'SELECT':
            txt = ("ROTATE: click to select object(s)/point(s)  "
                   "(Ctrl=point  Shift=add)  |  Enter=confirm  |  Esc=cancel")
        elif self._phase == 'PIVOT':
            txt = "ROTATE: specify pivot point  |  Esc=cancel"
        else:
            txt = ("ROTATE: specify rotation angle  |  "
                   "type degrees  |  Click/Enter=confirm  |  Esc=cancel")
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
        # The selection is only cleared here, once the rotation is
        # fully committed or cancelled - never while the tool is
        # running.
        _st.selection.selected_entities    = []
        _st.selection.active_entity        = None
        _st.selection.selected_snap_points = []
        reset_tpanel_to_select(self, context)
        if context.area:
            context.area.tag_redraw()

    def cancel(self, context):
        """Revert any live-preview rotation already applied, then
        clean up. Safe to call from any phase."""
        for obj in self._sel_objects:
            orig = self._start_locs.get(obj.name)
            if orig is not None:
                orig_loc, orig_rot_z = orig
                obj.location = orig_loc
                obj.rotation_euler.z = orig_rot_z
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
        """Commit: keep the live-previewed rotation as final."""
        for obj_name in self._circle_stretch_objs:
            obj = bpy.data.objects.get(obj_name)
            if obj is None:
                continue
            # A circle with one half rotated relative to the other is
            # no longer a true circle - drop the CAD-circle metadata,
            # same reasoning as the Stretch Tool.
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
        if self._phase == 'PIVOT':
            return self._modal_pivot(context, event)
        return self._modal_angle(context, event)

    # ── point resolution helpers (mirrors tool_stretch.py) ──────────
    def _resolve_vertex_at(self, context, world_pt: Vector, tol: float = 1e-4):
        for obj in context.view_layer.objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            mw = obj.matrix_world
            for vi, v in enumerate(obj.data.vertices):
                if (mw @ v.co - world_pt).length <= tol:
                    return (obj, vi)
        return None

    def _resolve_midpoint_at(self, context, world_pt: Vector, tol: float = 1e-4):
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
        if not self._sel_verts:
            return
        all_verts = []
        for obj in context.view_layer.objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            mw = obj.matrix_world
            for vi, v in enumerate(obj.data.vertices):
                all_verts.append((obj, vi, mw @ v.co))

        for _ in range(4):
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
        """Same smooth-falloff half-selection as the Stretch Tool - here
        the weight scales how much of the *rotation angle* each vertex
        receives (see _apply_angle), so a lone quadrant grip sweeps the
        whole associated half smoothly instead of pinching into a
        spike."""
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
                    if event.ctrl:
                        quad_angle = self._vertex_quadrant_angle(obj, vi)
                        circ = self._circle_params(obj) if quad_angle is not None else None
                        if not event.shift:
                            self._sel_objects = []
                            self._sel_verts           = {}
                            self._vert_weight         = {}
                            self._circle_stretch_objs = set()
                            if circ is not None:
                                self._select_circle_half(obj, quad_angle, *circ)
                            else:
                                self._sel_verts = {obj.name: {vi}}
                        elif circ is not None:
                            if obj in self._sel_objects:
                                self._sel_objects.remove(obj)
                            self._select_circle_half(obj, quad_angle, *circ)
                        else:
                            if obj in self._sel_objects:
                                self._sel_objects.remove(obj)
                            s = self._sel_verts.setdefault(obj.name, set())
                            if vi in s:
                                s.discard(vi)
                                if not s:
                                    del self._sel_verts[obj.name]
                            else:
                                s.add(vi)
                    else:
                        if not event.shift:
                            self._sel_objects = [obj]
                            self._sel_verts   = {}
                        else:
                            self._sel_verts.pop(obj.name, None)
                            if obj in self._sel_objects:
                                self._sel_objects.remove(obj)
                            else:
                                self._sel_objects.append(obj)
                elif not event.shift:
                    self._sel_objects = []
                    self._sel_verts   = {}
                return {'RUNNING_MODAL'}

            if event.type in {'RET', 'NUMPAD_ENTER', 'SPACE'}:
                if not self._sel_objects and not self._sel_verts:
                    self.report({'WARNING'}, "Select object(s) or point(s) first")
                    return {'RUNNING_MODAL'}
                self._expand_connectivity(context)
                self._phase = 'PIVOT'
                self._update_header(context)
                return {'RUNNING_MODAL'}

            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ── PIVOT phase ──────────────────────────────────────────────────
    def _modal_pivot(self, context, event):
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
                self._commit_pivot(context)
                return {'RUNNING_MODAL'}
            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def _commit_pivot(self, context):
        self._pivot = self._cur_3d.copy()
        self._start_locs = {
            o.name: (o.location.copy(), o.rotation_euler.z) for o in self._sel_objects
        }
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
        self._angle = 0.0   # delta starts at zero
        # Store the first selected object's current rotation as the baseline
        # for display purposes - so the angle display shows current absolute
        # rotation, not just the delta from this tool's starting point
        if self._sel_objects:
            self._initial_angle = self._sel_objects[0].rotation_euler.z
        else:
            self._initial_angle = 0.0
        self._phase = 'ANGLE'
        self._update_header(context)

    # ── ANGLE phase ──────────────────────────────────────────────────
    def _modal_angle(self, context, event):
        if event.type == 'MOUSEMOVE':
            raw = mouse_to_3d(context, self._mx, self._my)
            if raw:
                snapped, stype = apply_snaps(
                    context, raw, self._snap_cache,
                    snap_filter=_st.selection.get_active_stypes(),
                    anchor=self._pivot,
                )
                self._snap_type = stype
                update_tracking(self._otrack, snapped, stype)

                self._polar_angle = None
                if stype not in _REAL_OSNAP_TYPES:
                    try:
                        polar = apply_polar_tracking(context, raw, self._pivot)
                        if polar is not None:
                            snapped, self._polar_angle = polar
                    except Exception as e:
                        print(f"[CAD] rotate polar tracking error: {e}")

                self._cur_3d = snapped
                rel = self._cur_3d - self._pivot
                if rel.length > 1e-6:
                    self._angle = math.atan2(rel.y, rel.x)
                self._apply_angle(self._angle)
            return {'RUNNING_MODAL'}

        if event.value == 'PRESS':
            ch = event.ascii
            if ch and ch in '0123456789.':
                self._inp += ch
                self._apply_typed_angle()
                return {'RUNNING_MODAL'}
            if ch == '-' and not self._inp:
                self._inp = '-'
                return {'RUNNING_MODAL'}
            if event.type == 'BACK_SPACE':
                self._inp = self._inp[:-1]
                self._apply_typed_angle()
                return {'RUNNING_MODAL'}

            if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'}:
                self._finish(context)
                return {'FINISHED'}

            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ── shared rotation helpers ──────────────────────────────────────
    def _rotate_point(self, pt: Vector, angle: float) -> Vector:
        """Rotate pt about self._pivot by angle (radians), CCW about
        +Z - matches this addon's flat XY-plane sketch convention used
        throughout (Line/Rect/Circle all work purely in X/Y)."""
        piv = self._pivot
        rx, ry = pt.x - piv.x, pt.y - piv.y
        ca, sa = math.cos(angle), math.sin(angle)
        return Vector((piv.x + rx * ca - ry * sa,
                       piv.y + rx * sa + ry * ca,
                       pt.z))

    def _apply_angle(self, angle: float):
        for obj in self._sel_objects:
            orig_loc, orig_rot_z = self._start_locs[obj.name]
            obj.location = self._rotate_point(orig_loc, angle)
            obj.rotation_euler.z = orig_rot_z + angle
        for obj_name, wcos in self._orig_wcos.items():
            obj = bpy.data.objects.get(obj_name)
            if obj is None:
                continue
            mw_inv = obj.matrix_world.inverted()
            mesh   = obj.data
            for vi, orig_co in wcos.items():
                w = self._vert_weight.get((obj_name, vi), 1.0)
                new_world = self._rotate_point(orig_co, angle * w)
                mesh.vertices[vi].co = mw_inv @ new_world
            mesh.update()

    def _apply_typed_angle(self):
        if not self._inp or self._inp == '-':
            return
        try:
            deg = float(self._inp)
        except ValueError:
            return
        self._angle = display_ang_to_rad(deg)
        self._apply_angle(self._angle)

    # ── draw callback ────────────────────────────────────────────────
    # Orange, matching CAD_OT_SketchModeManager._COL_SELECTED - so a
    # point/object reads as "still selected" rather than "hovered"
    # (which uses yellow) while Rotate is running.
    _COL_SELECTED = (1.00, 0.50, 0.00, 0.95)

    def _draw_persistent_selection(self, context):
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
        for obj in self._sel_objects:
            s2 = world_to_screen(context, obj.matrix_world.translation)
            if s2:
                gfx_dot(s2, self._COL_SELECTED, 8)

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
                "ROTATE: select object(s)/point(s), Enter to confirm",
                10, 55, 12, (0.3, 0.9, 1.0, 1.0),
            )
            return

        cur_2d = world_to_screen(context, self._cur_3d) if self._cur_3d else None

        if self._phase == 'PIVOT':
            if show_cursor_fx:
                if cur_2d:
                    gfx_snap_marker(cur_2d, self._snap_type)
                cx, cy = cur_2d if cur_2d else (self._mx, self._my)
                draw_crosshair(context, cx, cy, None, show_box=False)
            gfx_text("ROTATE: specify pivot point", 10, 55, 12, (0.3, 0.9, 1.0, 1.0))
            return

        # ANGLE phase - rotation guide: the shared anchor->cursor
        # reference ray (gfx.preview, same as Line/Move/Stretch) from
        # pivot to cursor, the same green Polar Tracking direction
        # guide when engaged, plus a dashed arc sweeping from the 0°
        # reference (+X) to the current angle and a live "∠ NN.N°"
        # readout - the angle-specific parts Line has no equivalent
        # for. The geometry itself already shows the live preview
        # since _apply_angle() rotates the real objects/vertices every
        # frame.
        piv_2d = world_to_screen(context, self._pivot)
        if piv_2d and cur_2d:
            draw_reference_ray(piv_2d, cur_2d)
            gfx_dot(piv_2d, (0.3, 0.9, 1.0, 0.9), 7)
            gfx_dot(cur_2d, (0.3, 0.9, 1.0, 0.9), 6)

            if self._polar_angle is not None:
                draw_direction_guide(
                    context, world_to_screen, self._pivot, self._cur_3d, cur_2d,
                )

            ref_3d = self._pivot + Vector((1, 0, 0))
            ref_2d = world_to_screen(context, ref_3d)
            ref_screen = (
                math.atan2(ref_2d[1] - piv_2d[1], ref_2d[0] - piv_2d[0])
                if ref_2d else 0.0
            )
            tip_screen = math.atan2(cur_2d[1] - piv_2d[1], cur_2d[0] - piv_2d[0])
            ARC_R = 52
            gfx_dashed_arc(
                piv_2d, ARC_R, ref_screen, tip_screen,
                col=(0.2, 0.95, 0.2, 0.75), w=1.5,
            )
            gfx_arc_arrow(
                piv_2d, ARC_R, tip_screen, (tip_screen >= ref_screen),
            )

            if self._polar_angle is not None:
                gfx_text(
                    f"Polar: {format_angle(context, math.radians(self._polar_angle))}",
                    cur_2d[0] + 14, cur_2d[1] + 30, 11, (1.0, 1.0, 1.0, 0.95),
                )

        # Always display the angle in the blue dimension box in ANGLE phase,
        # even if cursor is off-screen or hasn't moved yet. Offset the box
        # to the upper-right of the cursor so the crosshair doesn't block it.
        if self._phase == 'ANGLE':
            current_angle = self._initial_angle + self._angle
            disp = self._inp + "▌" if self._inp else format_angle(context, current_angle)
            display_pos = cur_2d if cur_2d else (self._mx, self._my)
            # Offset by 40 pixels right and 35 pixels up to avoid cursor overlap
            offset_x = display_pos[0] + 40
            offset_y = display_pos[1] - 35
            if show_cursor_fx:
                draw_float_dim(offset_x, offset_y, disp, active=True)

        if show_cursor_fx:
            if cur_2d:
                gfx_snap_marker(cur_2d, self._snap_type)
            cx, cy = cur_2d if cur_2d else (self._mx, self._my)
            draw_crosshair(context, cx, cy, None, show_box=False)

        # Display current absolute rotation (initial + delta) in status bar
        if self._phase == 'ANGLE':
            current_angle = self._initial_angle + self._angle
            gfx_text(
                f"ROTATE   ∠ {format_angle(context, current_angle)}",
                10, 55, 12, (0.3, 0.9, 1.0, 1.0),
            )
