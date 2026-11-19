# ─────────────────────────────────────────────────────────────────────
#  operators/tool_mirror.py
#  CAD_OT_SketchMirror – AutoCAD-style MIRROR command.
#
#  Like Copy, Mirror never touches the original selection while the
#  operation is running - it duplicates first and previews on the
#  duplicate. Unlike every other transform tool, the transform itself
#  is a reflection across a user-defined line rather than a
#  translation/rotation, and it ends with an explicit Keep/Delete
#  Original choice instead of just committing.
#
#  Whole-object and point-based selections are unified onto the same
#  mechanism: a whole object is treated as "every vertex of its mesh
#  selected", exactly like a point selection with nothing excluded -
#  so the reflection math never has to special-case object transforms
#  (which can't be represented correctly by location/rotation/scale
#  alone for an arbitrary mirror-line angle; mirroring actual vertex
#  positions is always geometrically correct, whatever the object's
#  current orientation).
#
#  Workflow
#  --------
#  1.  Activate the Mirror Tool.
#  2.  Select object(s) and/or point(s) - either beforehand, or right
#      here: plain click = whole object, Ctrl+click = a single point,
#      Shift adds to either.
#  3.  Press Enter (or click empty space with nothing picked) to
#      confirm the selection. This is also the point a duplicate of
#      the whole selection is created - full OSNAP + OTRACK apply to
#      picking the mirror line's first point (P1).
#  4.  Move the cursor to orient the mirror line - it is hard-locked
#      to the four orthographic directions only (0°/90°/180°/-90°),
#      always snapping to whichever is nearest the cursor; it never
#      rotates to an arbitrary angle. OSNAP/OTRACK/typed-distance
#      still control the *distance* along that locked axis, and Polar
#      Tracking's own near-snap still gets first refusal on refining
#      the point - the four-direction lock is what applies afterward,
#      regardless. Uses the Line tool's own preview system (dashed
#      reference ray, dynamic distance box, live angle arc+box, and
#      the same green direction guide, now always shown since the
#      line is always locked to something). The duplicate is reflected
#      across the live P1->cursor line every frame.
#  5.  Click to place the mirror line's second point (P2), locking its
#      orientation.
#  6.  A Keep/Delete Original prompt appears: K (or click) = Keep
#      Original (duplicate stays as the mirrored copy, original
#      untouched); D (or click) = Delete Original (whole-object
#      sources: the original object is removed, the mirrored
#      duplicate becomes the sole result; point-based sources: the
#      mirror is applied directly to the original's selected vertices
#      in place, and the temporary duplicate is discarded).
#  7.  Esc at any stage deletes whatever duplicate exists and ends the
#      tool without modifying the original geometry at all.
# ─────────────────────────────────────────────────────────────────────

import json
import math
import bpy
from mathutils import Vector

from ..utils.math_utils import (
    build_snap_cache, apply_snaps, apply_polar_tracking,
    mouse_to_3d, world_to_screen, world_ang_to_display, parse_typed_length, format_length, format_angle,
)
from ..utils.otrack import SnapAcquisitionManager, update_tracking
from .. import state as _st
from ..utils.cursor_util import (
    update_modal_cursor, restore_modal_cursor, cursor_fx_visible, init_modal_cursor,
    guarded_modal,
    handle_ui_handoff,
    reset_tpanel_to_select,
)
from ..gfx.primitives import gfx_dot, gfx_text, gfx_rect_fill, gfx_loop
from ..gfx.markers    import (
    gfx_snap_marker, draw_crosshair, draw_osnap_tracking, draw_vertex_square,
    draw_selected_snap_points,
)
from ..gfx.preview    import (
    draw_reference_ray, draw_direction_guide, draw_dim_preview, draw_angle_guide,
)


_PICK_RADIUS_PX = 12
_REAL_OSNAP_TYPES = (
    'VERTEX', 'ENDPOINT', 'MIDPOINT', 'CENTER', 'INTERSECTION',
    'NEAREST', 'QUADRANT', 'TANGENT', 'PERPENDICULAR', 'PARALLEL',
)


class CAD_OT_SketchMirror(bpy.types.Operator):
    """AutoCAD-style MIRROR: select object(s)/point(s), draw a mirror
    line, then choose whether to keep or delete the original."""
    bl_idname  = "cad.sketch_mirror"
    bl_label   = "CAD Mirror"
    bl_options = {'REGISTER', 'UNDO'}

    # ── invoke ────────────────────────────────────────────────────────
    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._context = context   # kept for helpers that don't take context directly (unit-aware parsing, etc.)
        self._mx, self._my = event.mouse_region_x, event.mouse_region_y
        self._inp          = ''
        self._axis          = None
        self._snap_type     = 'FREE'
        self._polar_angle   = None
        self._snap_cache    = build_snap_cache(context)
        self._otrack        = SnapAcquisitionManager()
        self._otrack.reset(clear_points=True)
        self._cur_3d        = mouse_to_3d(context, self._mx, self._my) or Vector((0.0, 0.0, 0.0))
        self._p1            = None
        self._choice_hover  = None   # 'KEEP' | 'DELETE' | None

        self._dup_objects   = []          # every duplicate object created for this run
        self._whole_dup_map = {}          # {orig_obj_name: dup_obj} - whole-object sources
        self._point_dup_map = {}          # {orig_obj_name: dup_obj} - point-based sources
        self._orig_wcos     = {}          # {dup_obj_name: {vi: orig_world_co}}
        self._vert_weight         = {}
        self._circle_stretch_objs = set()
        self._vert_weight_src         = {}
        self._circle_stretch_objs_src = set()

        # Points: same resolution/connectivity/circle-quadrant handling
        # as Stretch/Rotate/Copy.
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
                    self._select_circle_half(self._sel_verts, obj, quad_angle, center, radius)
                else:
                    self._sel_verts.setdefault(obj_name, set()).add(vi)

        for pt in _st.selection.selected_snap_points:
            wpt = Vector(pt)
            quad = self._find_circle_quadrant(context, wpt)
            if quad is not None:
                obj, quad_angle, center, radius = quad
                self._select_circle_half(self._sel_verts, obj, quad_angle, center, radius)
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

        # Whole objects: native Blender selection always wins over an
        # incidental point entry on the same object (see Rotate/Copy).
        self._sel_objects = [o for o in context.selected_objects if o.type == 'MESH']
        sel_obj_names = {o.name for o in self._sel_objects}
        self._sel_verts = {
            name: verts for name, verts in self._sel_verts.items()
            if name not in sel_obj_names
        }

        self._expand_connectivity(context, self._sel_verts)

        self._phase = 'P1' if (self._sel_objects or self._sel_verts) else 'SELECT'

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "MIRROR"
        context.window_manager.modal_handler_add(self)
        self._update_header(context)
        return {'RUNNING_MODAL'}

    def _update_header(self, context):
        if self._phase == 'SELECT':
            txt = ("MIRROR: click to select object(s)/point(s)  "
                   "(Ctrl=point  Shift=add)  |  Enter=confirm  |  Esc=cancel")
        elif self._phase == 'P1':
            txt = "MIRROR: specify first point of mirror line  |  Esc=cancel"
        elif self._phase == 'P2':
            txt = ("MIRROR: orient mirror line (locked to 0°/90°/180°/-90°)  |  "
                   "type distance  |  Click=confirm  |  Esc=cancel")
        else:
            txt = "MIRROR: Keep Original (K)  or  Delete Original (D)  |  Esc=cancel"
        context.area.header_text_set(txt)

    # ── duplication ──────────────────────────────────────────────────
    def _spawn_duplicates(self, context):
        """Create one duplicate per source object (whole-object or
        point-based) and snapshot the vertex set each one needs to
        mirror - ALL vertices for a whole-object source, just the
        selected subset for a point-based one."""
        self._dup_objects   = []
        self._whole_dup_map = {}
        self._point_dup_map = {}
        self._orig_wcos     = {}
        self._vert_weight         = {}
        self._circle_stretch_objs = set()

        def _dup_of(obj):
            dup = obj.copy()
            dup.data = obj.data.copy()
            for coll in obj.users_collection:
                coll.objects.link(dup)
            if not obj.users_collection:
                context.scene.collection.objects.link(dup)
            self._dup_objects.append(dup)
            return dup

        for obj in self._sel_objects:
            dup = _dup_of(obj)
            self._whole_dup_map[obj.name] = dup
            mw = dup.matrix_world
            self._orig_wcos[dup.name] = {
                vi: (mw @ v.co).copy() for vi, v in enumerate(dup.data.vertices)
            }

        for obj_name, verts in self._sel_verts.items():
            obj = bpy.data.objects.get(obj_name)
            if obj is None:
                continue
            dup = _dup_of(obj)
            self._point_dup_map[obj_name] = dup
            mw = dup.matrix_world
            self._orig_wcos[dup.name] = {
                vi: (mw @ dup.data.vertices[vi].co).copy() for vi in verts
            }
            for w_key, w_val in self._vert_weight_src.items():
                if w_key[0] == obj_name:
                    self._vert_weight[(dup.name, w_key[1])] = w_val
            if obj_name in self._circle_stretch_objs_src:
                self._circle_stretch_objs.add(dup.name)

    def _delete_all_duplicates(self):
        for obj in list(self._dup_objects):
            data = obj.data
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:
                pass
            if data is not None and data.users == 0:
                try:
                    bpy.data.meshes.remove(data)
                except Exception:
                    pass
        self._dup_objects   = []
        self._orig_wcos     = {}
        self._whole_dup_map = {}
        self._point_dup_map = {}

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
        _st.selection.selected_entities    = []
        _st.selection.active_entity        = None
        _st.selection.selected_snap_points = []
        reset_tpanel_to_select(self, context)
        if context.area:
            context.area.tag_redraw()

    def cancel(self, context):
        """Delete any duplicate(s) created so far and end - the
        original selection is never modified, at any stage."""
        self._delete_all_duplicates()
        self._cleanup(context)

    def _strip_stale_circle_metadata(self, obj_names):
        for name in obj_names:
            obj = bpy.data.objects.get(name)
            if obj is None:
                continue
            if "cad_type" in obj:
                del obj["cad_type"]
            if "cad_params" in obj:
                del obj["cad_params"]

    def _apply_choice(self, context, keep_original: bool):
        if keep_original:
            # Duplicate(s) already mirrored in place - just keep them.
            self._strip_stale_circle_metadata(self._circle_stretch_objs)
        else:
            # Whole-object sources: delete the original, the mirrored
            # duplicate becomes the sole surviving result.
            for orig_name, dup in self._whole_dup_map.items():
                orig = bpy.data.objects.get(orig_name)
                if orig is not None:
                    data = orig.data
                    try:
                        bpy.data.objects.remove(orig, do_unlink=True)
                    except Exception:
                        pass
                    if data is not None and data.users == 0:
                        try:
                            bpy.data.meshes.remove(data)
                        except Exception:
                            pass
            self._strip_stale_circle_metadata(
                d.name for d in self._whole_dup_map.values()
            )

            # Point-based sources: apply the same reflection directly
            # to the ORIGINAL's selected vertices, then discard the
            # temporary duplicate - it was only needed for preview.
            for orig_name, dup in self._point_dup_map.items():
                orig = bpy.data.objects.get(orig_name)
                verts = self._sel_verts.get(orig_name)
                if orig is not None and verts:
                    mw     = orig.matrix_world
                    mw_inv = mw.inverted()
                    for vi in verts:
                        orig_co = mw @ orig.data.vertices[vi].co
                        w = self._vert_weight.get((dup.name, vi), 1.0)
                        mirrored = self._mirror_point(orig_co)
                        if w < 1.0:
                            mirrored = orig_co.lerp(mirrored, w)
                        orig.data.vertices[vi].co = mw_inv @ mirrored
                    orig.data.update()
                    if orig_name in self._circle_stretch_objs_src:
                        if "cad_type" in orig:
                            del orig["cad_type"]
                        if "cad_params" in orig:
                            del orig["cad_params"]
                try:
                    data = dup.data
                    bpy.data.objects.remove(dup, do_unlink=True)
                    if data is not None and data.users == 0:
                        bpy.data.meshes.remove(data)
                except Exception:
                    pass
                self._dup_objects = [o for o in self._dup_objects if o.name != dup.name]

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
        if self._phase == 'P1':
            return self._modal_p1(context, event)
        if self._phase == 'P2':
            return self._modal_p2(context, event)
        return self._modal_choice(context, event)

    # ── point/circle resolution helpers (mirrors tool_stretch.py) ───
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

    def _expand_connectivity(self, context, sel_verts, tol: float = 1e-4):
        if not sel_verts:
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
            for obj_name, verts in sel_verts.items():
                obj = bpy.data.objects.get(obj_name)
                if obj is None:
                    continue
                mw = obj.matrix_world
                for vi in verts:
                    selected_pts.append(mw @ obj.data.vertices[vi].co)

            for obj, vi, wco in all_verts:
                if vi in sel_verts.get(obj.name, ()):
                    continue
                for spt in selected_pts:
                    if (wco - spt).length <= tol:
                        sel_verts.setdefault(obj.name, set()).add(vi)
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

    def _select_circle_half(self, sel_verts, obj, quad_angle: float,
                             center: Vector, radius: float):
        mesh = obj.data
        mw   = obj.matrix_world
        s = sel_verts.setdefault(obj.name, set())
        for vi, v in enumerate(mesh.vertices):
            wco = mw @ v.co
            ang = math.atan2(wco.y - center.y, wco.x - center.x)
            d   = self._ang_diff(ang, quad_angle)
            t   = min(d / (math.pi * 0.5), 1.0)
            weight = 1.0 - (3.0 * t * t - 2.0 * t * t * t)
            s.add(vi)
            self._vert_weight_src[(obj.name, vi)] = weight
        self._circle_stretch_objs_src.add(obj.name)

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
                            self._sel_verts               = {}
                            self._vert_weight_src         = {}
                            self._circle_stretch_objs_src = set()
                            if circ is not None:
                                self._select_circle_half(self._sel_verts, obj, quad_angle, *circ)
                            else:
                                self._sel_verts = {obj.name: {vi}}
                        elif circ is not None:
                            if obj in self._sel_objects:
                                self._sel_objects.remove(obj)
                            self._select_circle_half(self._sel_verts, obj, quad_angle, *circ)
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
                self._expand_connectivity(context, self._sel_verts)
                self._phase = 'P1'
                self._update_header(context)
                return {'RUNNING_MODAL'}

            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ── P1 phase ─────────────────────────────────────────────────────
    def _modal_p1(self, context, event):
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
                self._p1 = self._cur_3d.copy()
                self._spawn_duplicates(context)
                self._otrack.reset(clear_points=True)
                self._inp   = ''
                self._axis  = None
                self._phase = 'P2'
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ── P2 phase ─────────────────────────────────────────────────────
    _ORTHO_ANGLES_DEG = (0.0, 90.0, 180.0, -90.0)

    def _constrain_to_ortho(self, pt: Vector):
        """Force the direction from P1 to pt onto the nearest of the
        four orthographic directions (0°/90°/180°/-90°), preserving
        pt's distance from P1 (so OSNAP/typed-distance still control
        how far along that locked axis the point sits - only the
        angle itself is hard-constrained, never left arbitrary).
        Returns (locked_angle_deg, constrained_point)."""
        delta = pt - self._p1
        dist  = math.hypot(delta.x, delta.y)
        if dist < 1e-9:
            return 0.0, self._p1.copy()
        raw_deg = math.degrees(math.atan2(delta.y, delta.x))
        best = min(
            self._ORTHO_ANGLES_DEG,
            key=lambda a: abs(((raw_deg - a + 180.0) % 360.0) - 180.0),
        )
        rad = math.radians(best)
        constrained = self._p1 + Vector((math.cos(rad) * dist, math.sin(rad) * dist, 0.0))
        constrained.z = pt.z
        return best, constrained

    def _mirror_point(self, pt: Vector) -> Vector:
        """Reflect pt across the mirror line through self._p1 in the
        current preview direction (self._cur_3d), purely in the X/Y
        drawing plane - matching this addon's flat-sketch convention
        used throughout (Line/Rect/Circle/Stretch/Rotate all work in
        X/Y only)."""
        d = self._cur_3d - self._p1
        if d.length < 1e-6:
            return pt.copy()
        d = d.normalized()
        v = pt - self._p1
        proj = d * v.dot(d)
        perp = v - proj
        return self._p1 + proj - perp

    def _apply_mirror_preview(self):
        for dup_name, wcos in self._orig_wcos.items():
            obj = bpy.data.objects.get(dup_name)
            if obj is None:
                continue
            mw_inv = obj.matrix_world.inverted()
            mesh   = obj.data
            for vi, orig_co in wcos.items():
                mirrored = self._mirror_point(orig_co)
                w = self._vert_weight.get((dup_name, vi), 1.0)
                if w < 1.0:
                    mirrored = orig_co.lerp(mirrored, w)
                mesh.vertices[vi].co = mw_inv @ mirrored
            mesh.update()

    def _modal_p2(self, context, event):
        if event.type == 'MOUSEMOVE':
            raw = mouse_to_3d(context, self._mx, self._my)
            if raw:
                snapped, stype = apply_snaps(
                    context, raw, self._snap_cache,
                    snap_filter=_st.selection.get_active_stypes(),
                    anchor=self._p1,
                )
                self._snap_type = stype
                update_tracking(self._otrack, snapped, stype)

                # Reuse Polar Tracking's own near-snap system first -
                # if the cursor is close to one of the scene's
                # configured tracking increments, let it refine the
                # point exactly as it does for every other tool.
                if stype not in _REAL_OSNAP_TYPES:
                    try:
                        polar = apply_polar_tracking(context, raw, self._p1)
                        if polar is not None:
                            snapped, _ = polar
                    except Exception as e:
                        print(f"[CAD] mirror polar tracking error: {e}")

                # Hard constraint: the Mirror tool's line is restricted
                # to the four orthographic directions only, regardless
                # of what OSNAP/Polar Tracking suggested - direction is
                # always forced to the nearest of 0°/90°/180°/-90°,
                # while the distance along that locked axis still
                # comes from wherever the cursor/snap point was.
                locked_deg, constrained = self._constrain_to_ortho(snapped)
                self._polar_angle = locked_deg

                self._cur_3d = constrained
                self._apply_mirror_preview()
            return {'RUNNING_MODAL'}

        if event.value == 'PRESS':
            ch = event.ascii
            if ch and ch in '0123456789.':
                self._inp += ch
                self._apply_typed_p2()
                return {'RUNNING_MODAL'}
            if event.type == 'BACK_SPACE':
                self._inp = self._inp[:-1]
                self._apply_typed_p2()
                return {'RUNNING_MODAL'}

            if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'}:
                self._phase = 'CHOICE'
                self._choice_hover = None
                self._update_header(context)
                return {'RUNNING_MODAL'}

            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def _apply_typed_p2(self):
        """Typed digits set the mirror line's length from P1 along
        its current live direction - the angle still comes from the
        cursor/polar tracking, only the distance is overridden, same
        division of labour as the Line tool's length/angle input."""
        if not self._inp:
            return
        try:
            dist = parse_typed_length(self._context, self._inp)
        except ValueError:
            return
        direction = self._cur_3d - self._p1
        if direction.length < 1e-6:
            return
        self._cur_3d = self._p1 + direction.normalized() * dist
        self._apply_mirror_preview()

    # ── CHOICE phase ─────────────────────────────────────────────────
    def _choice_button_rects(self, context):
        """Screen-space rects for the Keep/Delete buttons, positioned
        below the cursor's last known P2 location."""
        p2_2d = world_to_screen(context, self._cur_3d) or (self._mx, self._my)
        bw, bh, gap = 150, 30, 8
        x0 = p2_2d[0] - bw - gap / 2
        x1 = p2_2d[0] + gap / 2
        y0 = p2_2d[1] - 60
        return {
            'KEEP':   (x0, y0, bw, bh),
            'DELETE': (x1, y0, bw, bh),
        }

    @staticmethod
    def _pt_in_rect(px, py, rect):
        x, y, w, h = rect
        return x <= px <= x + w and y <= py <= y + h

    def _modal_choice(self, context, event):
        if event.type == 'MOUSEMOVE':
            rects = self._choice_button_rects(context)
            self._choice_hover = None
            for key, r in rects.items():
                if self._pt_in_rect(self._mx, self._my, r):
                    self._choice_hover = key
                    break
            return {'RUNNING_MODAL'}

        if event.value == 'PRESS':
            if event.type == 'LEFTMOUSE':
                rects = self._choice_button_rects(context)
                for key, r in rects.items():
                    if self._pt_in_rect(self._mx, self._my, r):
                        self._apply_choice(context, keep_original=(key == 'KEEP'))
                        return {'FINISHED'}
                return {'RUNNING_MODAL'}

            if event.type == 'K':
                self._apply_choice(context, keep_original=True)
                return {'FINISHED'}
            if event.type == 'D':
                self._apply_choice(context, keep_original=False)
                return {'FINISHED'}
            if event.type in {'RET', 'NUMPAD_ENTER'}:
                # Enter defaults to Keep Original - the non-destructive
                # choice.
                self._apply_choice(context, keep_original=True)
                return {'FINISHED'}

            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ── draw callback ────────────────────────────────────────────────
    _COL_SELECTED = (1.00, 0.50, 0.00, 0.95)
    _COL_MIRROR   = (0.55, 0.85, 1.00, 0.95)

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

    def _draw_choice_buttons(self, context):
        rects = self._choice_button_rects(context)
        labels = {'KEEP': "K   Keep Original", 'DELETE': "D   Delete Original"}
        for key, (x, y, w, h) in rects.items():
            hovered = (self._choice_hover == key)
            if key == 'DELETE':
                bg = (0.35, 0.08, 0.08, 0.95) if hovered else (0.20, 0.06, 0.06, 0.92)
                bc = (0.90, 0.25, 0.25, 1.0)
            else:
                bg = (0.08, 0.30, 0.10, 0.95) if hovered else (0.10, 0.18, 0.10, 0.92)
                bc = (0.30, 0.85, 0.35, 1.0)
            gfx_rect_fill(x, y, w, h, bg)
            gfx_loop([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], bc, 1.4)
            gfx_text(labels[key], x + 10, y + 9, 12, (1.0, 1.0, 1.0, 1.0))

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
                "MIRROR: select object(s)/point(s), Enter to confirm",
                10, 55, 12, (0.3, 0.9, 1.0, 1.0),
            )
            return

        cur_2d = world_to_screen(context, self._cur_3d) if self._cur_3d else None

        if self._phase == 'P1':
            if show_cursor_fx:
                if cur_2d:
                    gfx_snap_marker(cur_2d, self._snap_type)
                cx, cy = cur_2d if cur_2d else (self._mx, self._my)
                draw_crosshair(context, cx, cy, None, show_box=False)
            gfx_text("MIRROR: specify first point of mirror line", 10, 55, 12, (0.3, 0.9, 1.0, 1.0))
            return

        p1_2d = world_to_screen(context, self._p1) if self._p1 else None

        if self._phase == 'P2':
            if p1_2d and cur_2d:
                # Mirror line itself - solid, distinct colour, drawn
                # a bit longer than P1->cursor so the active axis
                # reads clearly on both sides of the anchor.
                d = self._cur_3d - self._p1
                if d.length > 1e-6:
                    ext_3d_a = self._p1 - d.normalized() * 40
                    ext_3d_b = self._cur_3d + d.normalized() * 40
                    ext_2d_a = world_to_screen(context, ext_3d_a)
                    ext_2d_b = world_to_screen(context, ext_3d_b)
                    if ext_2d_a and ext_2d_b:
                        gfx_loop([ext_2d_a, ext_2d_b], (1.0, 0.85, 0.20, 0.95), 2.0)

                draw_reference_ray(p1_2d, cur_2d)
                gfx_dot(p1_2d, (0.3, 0.9, 1.0, 0.9), 7)
                gfx_dot(cur_2d, (0.3, 0.9, 1.0, 0.9), 6)

                if self._polar_angle is not None:
                    draw_direction_guide(
                        context, world_to_screen, self._p1, self._cur_3d, cur_2d,
                    )

                delta = self._cur_3d - self._p1
                dist  = delta.length
                if dist > 0.001:
                    disp = self._inp + "▌" if self._inp else format_length(context, dist)
                    draw_dim_preview(p1_2d, cur_2d, disp, active=True)

                deg = world_ang_to_display(math.atan2(delta.y, delta.x))
                draw_angle_guide(
                    context, world_to_screen, p1_2d, self._p1, cur_2d,
                    format_angle(context, math.atan2(delta.y, delta.x)),
                )

                if self._polar_angle is not None:
                    gfx_text(
                        f"Ortho: {format_angle(context, math.radians(self._polar_angle))}",
                        cur_2d[0] + 14, cur_2d[1] + 30, 11, (1.0, 1.0, 1.0, 0.95),
                    )

            if show_cursor_fx:
                if cur_2d:
                    gfx_snap_marker(cur_2d, self._snap_type)
                cx, cy = cur_2d if cur_2d else (self._mx, self._my)
                draw_crosshair(context, cx, cy, None, show_box=False)
            gfx_text(
                "MIRROR: specify second point of mirror line",
                10, 55, 12, (0.3, 0.9, 1.0, 1.0),
            )
            return

        # CHOICE phase - keep the mirror line + preview visible while
        # the user decides, plus the Keep/Delete buttons.
        if p1_2d and cur_2d:
            gfx_loop([p1_2d, cur_2d], (1.0, 0.85, 0.20, 0.85), 1.6)
        for dup_name in self._orig_wcos.keys():
            obj = bpy.data.objects.get(dup_name)
            if obj is None:
                continue
            s2 = world_to_screen(context, obj.matrix_world.translation)
            if s2:
                gfx_dot(s2, self._COL_MIRROR, 5)
        self._draw_choice_buttons(context)
        gfx_text(
            "MIRROR: choose Keep Original or Delete Original",
            10, 55, 12, (0.3, 0.9, 1.0, 1.0),
        )
