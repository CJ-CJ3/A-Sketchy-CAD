# ─────────────────────────────────────────────────────────────────────
#  operators/tool_point.py
#  CAD_OT_DrawPoint  –  single click places a point. Supports typed
#  X/Y dynamic input and OTrack guide lines, same plumbing as the
#  other draw tools.
# ─────────────────────────────────────────────────────────────────────

import time
import bpy
from mathutils import Vector

from ..utils.math_utils  import build_snap_cache, apply_snaps, mouse_to_3d, world_to_screen, parse_typed_length, format_length
from ..utils.otrack import SnapAcquisitionManager, update_tracking, resolve_typed_distance_ray
from ..utils.cad_objects import create_cad_object, pick_reference_object
from .. import state as _st
from ..utils.cursor_util import (
    update_modal_cursor, restore_modal_cursor, cursor_fx_visible, init_modal_cursor,
    guarded_modal,
    handle_ui_handoff,
    reset_tpanel_to_select,
)
from ..gfx.primitives    import gfx_text
from ..gfx.markers       import gfx_snap_marker, draw_crosshair, draw_vertex_square, draw_selected_snap_points, draw_osnap_tracking
from ..gfx.dims          import draw_dim_rows


class CAD_OT_DrawPoint(bpy.types.Operator):
    bl_idname  = "cad.draw_point"
    bl_label   = "CAD Point"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._context = context   # kept for helpers that don't take context directly (unit-aware parsing, etc.)
        self._raw       = Vector((0, 0, 0))
        self._cur       = Vector((0, 0, 0))
        self._snap_type = 'FREE'
        self._inp       = ''
        self._typed_ray_frozen = None

        self._base_candidate_key   = None
        self._base_candidate_since = 0.0
        self._acquired_base        = None

        self._mx, self._my = event.mouse_region_x, event.mouse_region_y
        self._snap_cache = build_snap_cache(context)
        self._ref_obj    = _st.sketch_ref_obj or pick_reference_object(context)
        self._otrack     = SnapAcquisitionManager()
        self._otrack.reset(clear_points=True)

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "POINT"
        context.window_manager.modal_handler_add(self)
        context.area.header_text_set("POINT: Click to place  |  Enter/Esc to exit")
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

        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN'}:
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            raw = mouse_to_3d(context, self._mx, self._my)
            if raw:
                self._raw, self._snap_type = apply_snaps(
                    context, raw, self._snap_cache,
                    snap_filter=_st.selection.get_active_stypes(),
                )
                self._update_cur()
                update_tracking(self._otrack, self._raw, self._snap_type)
                self._update_hover_base_point(context, self._snap_type)
            return {'RUNNING_MODAL'}

        if event.value != 'PRESS':
            return {'RUNNING_MODAL'}

        ch = event.ascii
        if ch and ch in '0123456789.':
            if not self._inp:
                self._typed_ray_frozen = self._typed_distance_ray()
            self._inp += ch
            self._update_cur()
            return {'RUNNING_MODAL'}
        if event.type == 'BACK_SPACE' and self._inp:
            self._inp = self._inp[:-1]
            if not self._inp:
                self._typed_ray_frozen = None
            self._update_cur()
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE':
            self._finish(context)
            return {'RUNNING_MODAL'}

        if event.type in {'RET', 'NUMPAD_ENTER', 'SPACE'}:
            self.cancel(context)
            return {'FINISHED'}

        if event.type in {'RIGHTMOUSE', 'ESC'}:
            self.cancel(context)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    _BASE_ACQUIRE_TIME = 0.25

    def _update_hover_base_point(self, context, stype):
        if self._inp:
            return
        if stype in ('FREE', 'GRID', 'INCREMENT', 'TRACKING'):
            key = None
        else:
            key = tuple(round(c, 6) for c in self._raw)
        now = time.time()
        if key == self._base_candidate_key:
            if key is not None and now - self._base_candidate_since >= self._BASE_ACQUIRE_TIME:
                self._acquired_base = Vector(key)
            return
        self._base_candidate_key   = key
        self._base_candidate_since = now

    def _typed_distance_ray(self):
        if self._otrack.points:
            ray = resolve_typed_distance_ray(self._otrack, self._raw)
            if ray is not None:
                return ray
        if self._acquired_base is not None:
            direction = self._raw - self._acquired_base
            if direction.length < 1e-6:
                direction = Vector((1, 0, 0))
            return self._acquired_base, direction.normalized()
        return None

    def _update_cur(self):
        if self._inp and self._typed_ray_frozen is not None:
            try:
                val = parse_typed_length(self._context, self._inp)
                origin, direction = self._typed_ray_frozen
                self._cur = origin + direction * val
                return
            except ValueError:
                pass
        self._cur = self._raw.copy()

    def _finish(self, context):
        create_cad_object(
            context, "CAD_Point", [(self._cur.x, self._cur.y, 0.0)], [], "point",
            {"location": [self._cur.x, self._cur.y, 0]},
            reference_obj=self._ref_obj,
        )
        self._inp = ''
        self._typed_ray_frozen = None
        self._acquired_base = None
        self._base_candidate_key = None

    def _draw(self, context):
        # The OTRACK marker / snap marker / coordinate readout /
        # crosshair all stand in for "the cursor" - only draw them
        # while the real OS cursor is actually hidden (mouse over the
        # usable viewport).
        show_cursor_fx = cursor_fx_visible(self)

        draw_selected_snap_points(context)
        if show_cursor_fx:
            draw_osnap_tracking(context, self._mx, self._my)
        mx, my = self._mx, self._my
        c2 = world_to_screen(context, self._cur)

        if show_cursor_fx and c2:
            gfx_snap_marker(c2, self._snap_type)
            draw_vertex_square(c2, 5, fill_col=(1, 0.6, 0.1, 0.25), outline_col=(1, 0.6, 0.1, 1), outline_w=1.8)

        if show_cursor_fx:
            if self._inp:
                draw_dim_rows(
                    context, mx, my,
                    [("Dist:", self._inp + "▌", True, False)],
                )
            else:
                draw_dim_rows(
                    context, mx, my,
                    [("X:", f"{self._cur.x:.4f}", True, False),
                     ("Y:", f"{self._cur.y:.4f}", False, False)],
                )

            cx, cy = c2 if c2 else (mx, my)
            draw_crosshair(context, cx, cy, show_box=False)
        gfx_text(f"POINT   X {self._cur.x:.3f}   Y {self._cur.y:.3f}", 10, 55, 12, (1.0, 0.85, 0.2, 1.0))
