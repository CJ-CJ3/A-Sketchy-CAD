# ─────────────────────────────────────────────────────────────────────
#  operators/tool_copy.py
#  CAD_OT_SketchCopy – AutoCAD-style COPY command.
#
#  Like Rotate, Copy supports both whole objects and selected points
#  at once. Unlike Move/Stretch/Rotate, it never touches the original
#  geometry at all - at the BASE-point step it duplicates whatever is
#  selected (new objects + new mesh datablocks, never shared with the
#  originals), and every following step previews/places that
#  duplicate. The original stays exactly where and what it was.
#
#  Workflow
#  --------
#  1.  Activate the Copy Tool.
#  2.  Select object(s) and/or point(s) - either beforehand, or right
#      here: plain click = whole object, Ctrl+click = a single point,
#      Shift adds to either.
#  3.  Press Enter (or click empty space with nothing picked) to
#      confirm the selection and specify a BASE point - full OSNAP +
#      OTRACK. This is the moment the duplicate is actually created.
#  4.  Specify a DESTINATION point - OSNAP, OTRACK, Polar Tracking,
#      axis lock, and typed-distance entry, exactly like Move/Stretch.
#      The *duplicate* is what tracks the cursor live; the original
#      selection never moves.
#  5.  Click/Enter places the copy. Per AutoCAD's default multiple-copy
#      behaviour, the tool then immediately duplicates the ORIGINAL
#      selection again (not the copy just placed) and stays in the
#      DESTINATION step from the same base point, so repeated clicks
#      lay down as many copies as needed. Enter/Esc/RMB stops.
#  6.  Esc/RMB while a copy is still being previewed (not yet placed)
#      deletes that pending duplicate and ends the tool - any copies
#      already placed earlier in the same run are kept.
#
#  A circle quadrant grip still gets the smooth half-circle falloff
#  (see _select_circle_half), applied to the duplicate.
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


class CAD_OT_SketchCopy(bpy.types.Operator):
    """AutoCAD-style COPY: select object(s)/point(s), pick a base
    point, pick one or more destination points - each click places a
    duplicate there. The original selection is never modified."""
    bl_idname  = "cad.sketch_copy"
    bl_label   = "CAD Copy"
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
        self._copies_made   = 0

        # Per-placement duplicate state - rebuilt fresh by
        # _spawn_duplicate() every time a new copy starts previewing.
        self._dup_objects   = []   # duplicate objects for whole-object copies
        self._start_locs    = {}   # {dup_obj_name: orig_location}
        self._orig_wcos     = {}   # {dup_obj_name: {vi: orig_world_co}}
        self._vert_weight        = {}
        self._circle_stretch_objs = set()

        self._vert_weight_src         = {}   # {(src_obj_name, vi): 0..1} - copied onto each duplicate
        self._circle_stretch_objs_src = set()   # src obj names with a circle half-selection

        # Points: same two parallel selection systems, resolution, and
        # connectivity expansion as Stretch/Rotate (see tool_stretch.py
        # for the detailed rationale on each case) - applied here to
        # the *source* selection, which is what gets duplicated.
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

        # Objects: native Blender selection always wins over an
        # incidental point entry on the same object (see Rotate for
        # the full rationale) - "I selected my object" reliably
        # duplicates the whole object.
        self._sel_objects = [o for o in context.selected_objects if o.type == 'MESH']
        sel_obj_names = {o.name for o in self._sel_objects}
        self._sel_verts = {
            name: verts for name, verts in self._sel_verts.items()
            if name not in sel_obj_names
        }

        self._expand_connectivity(context, self._sel_verts)

        self._phase = 'BASE' if (self._sel_objects or self._sel_verts) else 'SELECT'

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "COPY"
        context.window_manager.modal_handler_add(self)
        self._update_header(context)
        return {'RUNNING_MODAL'}

    def _update_header(self, context):
        if self._phase == 'SELECT':
            txt = ("COPY: click to select object(s)/point(s)  "
                   "(Ctrl=point  Shift=add)  |  Enter=confirm  |  Esc=cancel")
        elif self._phase == 'BASE':
            txt = "COPY: specify base point  |  Esc=cancel"
        else:
            txt = ("COPY: specify destination point  |  X/Y=axis  |  "
                   "type distance  |  Click=place copy (repeat for more)  |  "
                   "Enter/Esc=finish")
        context.area.header_text_set(txt)

    # ── duplication ──────────────────────────────────────────────────
    def _spawn_duplicate(self, context):
        """Create a fresh duplicate of the ORIGINAL selection (never
        of a previously-placed copy) and reset all the per-placement
        snapshot state to it, ready for a new live preview from
        self._base_pt. The original objects/mesh data are never
        touched - each duplicate gets its own new object + new mesh
        datablock."""
        self._dup_objects   = []
        self._start_locs    = {}
        self._orig_wcos     = {}
        self._vert_weight        = {}
        self._circle_stretch_objs = set()

        dup_map = {}   # orig obj name -> duplicate object
        for obj in self._sel_objects:
            dup = obj.copy()
            dup.data = obj.data.copy()
            for coll in obj.users_collection:
                coll.objects.link(dup)
            if not obj.users_collection:
                context.scene.collection.objects.link(dup)
            self._dup_objects.append(dup)
            dup_map[obj.name] = dup

        for obj_name, verts in self._sel_verts.items():
            obj = bpy.data.objects.get(obj_name)
            if obj is None:
                continue
            dup = obj.copy()
            dup.data = obj.data.copy()
            for coll in obj.users_collection:
                coll.objects.link(dup)
            if not obj.users_collection:
                context.scene.collection.objects.link(dup)
            dup_map[obj_name] = dup
            for w_key, w_val in self._vert_weight_src.items():
                if w_key[0] == obj_name:
                    self._vert_weight[(dup.name, w_key[1])] = w_val
            if obj_name in self._circle_stretch_objs_src:
                self._circle_stretch_objs.add(dup.name)

        self._start_locs = {d.name: d.location.copy() for d in self._dup_objects}
        for obj_name, verts in self._sel_verts.items():
            dup = dup_map.get(obj_name)
            if dup is None:
                continue
            mw = dup.matrix_world
            self._orig_wcos[dup.name] = {
                vi: (mw @ dup.data.vertices[vi].co).copy() for vi in verts
            }

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

    def _delete_pending_duplicate(self):
        """Remove the duplicate currently being previewed (not yet
        placed) - used on cancel. Copies already placed earlier in the
        same run are untouched."""
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
        for dup_name in list(self._orig_wcos.keys()):
            obj = bpy.data.objects.get(dup_name)
            if obj is None:
                continue
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
        self._dup_objects = []
        self._orig_wcos   = {}

    def cancel(self, context):
        if self._phase == 'DEST':
            self._delete_pending_duplicate()
        self._cleanup(context)

    def _strip_stale_circle_metadata(self):
        for dup_name in self._circle_stretch_objs:
            obj = bpy.data.objects.get(dup_name)
            if obj is None:
                continue
            if "cad_type" in obj:
                del obj["cad_type"]
            if "cad_params" in obj:
                del obj["cad_params"]

    def _finish(self, context):
        """Commit: keep the just-placed copy as final and stop."""
        self._strip_stale_circle_metadata()
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
        """Same smooth-falloff half-selection as Stretch/Rotate,
        recorded against the *source* object here; _spawn_duplicate()
        copies the resulting weights onto each new duplicate."""
        if not hasattr(self, '_vert_weight_src'):
            self._vert_weight_src        = {}
            self._circle_stretch_objs_src = set()
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
                            self._sel_verts           = {}
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
        self._base_pt = self._cur_3d.copy()
        self._spawn_duplicate(context)
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
                        print(f"[CAD] copy polar tracking error: {e}")

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

            if event.type == 'LEFTMOUSE':
                # Place this copy permanently, then immediately spawn
                # a fresh duplicate of the ORIGINAL selection for the
                # next possible placement - AutoCAD's default
                # multiple-copy behaviour.
                self._strip_stale_circle_metadata()
                self._copies_made += 1
                self._inp   = ''
                self._axis  = None
                self._spawn_duplicate(context)
                self._apply_move()
                return {'RUNNING_MODAL'}

            if event.type in {'RET', 'NUMPAD_ENTER'}:
                # Place this copy and stop - unlike a click, Enter
                # doesn't queue up another one.
                self._copies_made += 1
                self._finish(context)
                return {'FINISHED'}

            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                if self._copies_made == 0:
                    return {'CANCELLED'}
                return {'FINISHED'}

        return {'RUNNING_MODAL'}

    # ── shared translation helpers ──────────────────────────────────
    def _translate_to(self, delta: Vector):
        for obj in self._dup_objects:
            obj.location = self._start_locs[obj.name] + delta
        for dup_name, wcos in self._orig_wcos.items():
            obj = bpy.data.objects.get(dup_name)
            if obj is None:
                continue
            mw_inv = obj.matrix_world.inverted()
            mesh   = obj.data
            for vi, orig_co in wcos.items():
                w = self._vert_weight.get((dup_name, vi), 1.0)
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
    _COL_SELECTED = (1.00, 0.50, 0.00, 0.95)   # orange - matches SELECT tool
    _COL_COPY     = (0.55, 0.85, 1.00, 0.95)   # light cyan - marks the preview copy

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
                "COPY: select object(s)/point(s), Enter to confirm",
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
            gfx_text("COPY: specify base point", 10, 55, 12, (0.3, 0.9, 1.0, 1.0))
            return

        # DEST phase - shared reference-ray/dim-preview/polar-guide,
        # same as Move/Stretch. The duplicate itself already shows the
        # live preview (it's real geometry, translated every frame);
        # a small cyan marker on each duplicated object highlights
        # which one is the copy, so it doesn't read as the original.
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

        for obj in self._dup_objects:
            s2 = world_to_screen(context, obj.matrix_world.translation)
            if s2:
                gfx_dot(s2, self._COL_COPY, 5)
        for dup_name in self._orig_wcos.keys():
            obj = bpy.data.objects.get(dup_name)
            if obj is None:
                continue
            s2 = world_to_screen(context, obj.matrix_world.translation)
            if s2:
                gfx_dot(s2, self._COL_COPY, 5)

        if show_cursor_fx:
            if cur_2d:
                gfx_snap_marker(cur_2d, self._snap_type)
            cx, cy = cur_2d if cur_2d else (self._mx, self._my)
            draw_crosshair(context, cx, cy, self._axis, show_box=False)

        delta = self._cur_3d - self._base_pt
        ax    = f"  [{self._axis}]" if self._axis else ""
        copies = f"  ({self._copies_made} placed)" if self._copies_made else ""
        gfx_text(
            f"COPY{ax}   dX {delta.x:+.3f}   dY {delta.y:+.3f}{copies}",
            10, 55, 12, (0.3, 0.9, 1.0, 1.0),
        )
