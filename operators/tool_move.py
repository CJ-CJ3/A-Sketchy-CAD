# ─────────────────────────────────────────────────────────────────────
#  operators/tool_move.py
#  CAD_OT_SketchMove – AutoCAD-style MOVE command.
#
#  Companion to CAD_OT_SketchStretch (tool_stretch.py): Move only ever
#  translates whole objects - it never reshapes geometry. If any
#  points are selected instead of/alongside objects, use the Stretch
#  Tool for those; Move ignores point selections entirely.
#
#  Workflow
#  --------
#  1.  Activate the Move Tool.
#  2.  Select object(s) - either beforehand (native Blender selection),
#      or right here by clicking in the viewport (Shift adds/toggles).
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
#  Only object.location changes - mesh data (vertices/edges, and
#  therefore shape/dimensions) is never touched, so Move can never
#  deform geometry, only relocate it.
# ─────────────────────────────────────────────────────────────────────

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
from ..gfx.markers    import gfx_snap_marker, draw_crosshair, draw_osnap_tracking
from ..gfx.preview    import draw_reference_ray, draw_direction_guide, draw_dim_preview


_PICK_RADIUS_PX = 12   # screen-space picking radius for the SELECT phase
_REAL_OSNAP_TYPES = (
    'VERTEX', 'ENDPOINT', 'MIDPOINT', 'CENTER', 'INTERSECTION',
    'NEAREST', 'QUADRANT', 'TANGENT', 'PERPENDICULAR', 'PARALLEL',
)


class CAD_OT_SketchMove(bpy.types.Operator):
    """AutoCAD-style MOVE: select whole object(s), pick a base point,
    pick a destination point - the object(s) are translated so the
    base point lands exactly on the destination. Geometry is never
    reshaped - use the Stretch Tool to move individual points."""
    bl_idname  = "cad.sketch_move"
    bl_label   = "CAD Move"
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
        self._start_locs    = {}

        # Objects only - from the current Blender selection, mesh
        # type. Move never looks at point selections (selected_entities
        # / selected_snap_points) at all; that's the Stretch Tool's job.
        self._sel_objects = [o for o in context.selected_objects if o.type == 'MESH']

        self._phase = 'BASE' if self._sel_objects else 'SELECT'

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "MOVE"
        context.window_manager.modal_handler_add(self)
        self._update_header(context)
        return {'RUNNING_MODAL'}

    def _update_header(self, context):
        if self._phase == 'SELECT':
            txt = ("MOVE: click to select object(s)  "
                   "(Shift=add)  |  Enter=confirm  |  Esc=cancel")
        elif self._phase == 'BASE':
            txt = "MOVE: specify base point  |  Esc=cancel"
        else:
            txt = ("MOVE: specify destination point  |  X/Y=axis  |  "
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
        reset_tpanel_to_select(self, context)
        if context.area:
            context.area.tag_redraw()

    def cancel(self, context):
        """Revert any live-preview translation already applied, then
        clean up. Safe to call from any phase - if the BASE point was
        never committed, _start_locs is still empty and this is a pure
        no-op revert."""
        for obj in self._sel_objects:
            loc = self._start_locs.get(obj.name)
            if loc is not None:
                obj.location = loc
        self._cleanup(context)

    def _finish(self, context):
        """Commit: keep the live-previewed positions as final."""
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

    # ── SELECT phase ─────────────────────────────────────────────────
    def _pick(self, context):
        """Nearest whole object to the cursor, screen-space (found via
        its nearest vertex), within _PICK_RADIUS_PX."""
        mx, my  = self._mx, self._my
        best_d  = float(_PICK_RADIUS_PX)
        best    = None
        for obj in context.view_layer.objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            mw = obj.matrix_world
            for v in obj.data.vertices:
                s2 = world_to_screen(context, mw @ v.co)
                if s2 is None:
                    continue
                d = math.hypot(s2[0] - mx, s2[1] - my)
                if d < best_d:
                    best_d = d
                    best   = obj
        return best

    def _modal_select(self, context, event):
        if event.type == 'MOUSEMOVE':
            return {'RUNNING_MODAL'}

        if event.value == 'PRESS':
            if event.type == 'LEFTMOUSE':
                obj = self._pick(context)
                if obj is not None:
                    if not event.shift:
                        self._sel_objects = [obj]
                    elif obj in self._sel_objects:
                        self._sel_objects.remove(obj)
                    else:
                        self._sel_objects.append(obj)
                elif not event.shift:
                    self._sel_objects = []
                return {'RUNNING_MODAL'}

            if event.type in {'RET', 'NUMPAD_ENTER', 'SPACE'}:
                if not self._sel_objects:
                    self.report({'WARNING'}, "Select object(s) first")
                    return {'RUNNING_MODAL'}
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
        self._base_pt    = self._cur_3d.copy()
        self._start_locs = {o.name: o.location.copy() for o in self._sel_objects}
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
                        print(f"[CAD] move polar tracking error: {e}")

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
        for obj in self._sel_objects:
            obj.location = self._start_locs[obj.name] + delta

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
    # Orange, matching CAD_OT_SketchModeManager._COL_SELECTED - so an
    # object reads as "still selected" rather than "hovered" (which
    # uses yellow) while Move is running.
    _COL_SELECTED = (1.00, 0.50, 0.00, 0.95)

    def _draw_persistent_selection(self, context):
        """Keep every selected object visibly highlighted for the
        *entire* operation - SELECT, BASE, and DEST phases alike."""
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
        self._draw_persistent_selection(context)

        if self._phase == 'SELECT':
            if show_cursor_fx:
                draw_crosshair(context, self._mx, self._my, None, show_box=True)
            gfx_text(
                "MOVE: select object(s), Enter to confirm",
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
            gfx_text("MOVE: specify base point", 10, 55, 12, (0.3, 0.9, 1.0, 1.0))
            return

        # DEST phase - live dimension/guide overlay, reusing the exact
        # same preview primitives as CAD_OT_DrawLine (gfx.preview) so
        # every transform tool's anchor->cursor reference looks and
        # behaves identically. The geometry itself already shows the
        # live preview since _apply_move() translates the real objects
        # every frame.
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
            f"MOVE{ax}   dX {delta.x:+.3f}   dY {delta.y:+.3f}",
            10, 55, 12, (0.3, 0.9, 1.0, 1.0),
        )
