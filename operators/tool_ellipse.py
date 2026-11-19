# ─────────────────────────────────────────────────────────────────────
#  operators/tool_ellipse.py
#  CAD_OT_DrawEllipse  –  center-based: click to place centre,
#  then define major axis, then minor axis. Matches Circle tool's
#  multi-click interaction pattern.
# ─────────────────────────────────────────────────────────────────────

import math
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
from ..gfx.primitives    import gfx_dot, gfx_lines, gfx_text, gfx_loop, gfx_dashed_line
from ..gfx.markers       import gfx_snap_marker, draw_crosshair, draw_vertex_square, draw_selected_snap_points, draw_osnap_tracking
from ..gfx.dims          import draw_float_dim, draw_dim_rows


class CAD_OT_DrawEllipse(bpy.types.Operator):
    bl_idname  = "cad.draw_ellipse"
    bl_label   = "CAD Ellipse"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._context    = context   # kept for helpers that don't take context directly (unit-aware parsing, etc.)
        self._cen       = None
        self._raw       = Vector((0, 0, 0))
        self._cur       = Vector((0, 0, 0))
        self._snap_type = 'FREE'
        self._inp       = ''
        self._typed_ray_frozen = None

        self._base_candidate_key   = None
        self._base_candidate_since = 0.0
        self._acquired_base        = None

        self._phase     = 'CENTER'   # 'CENTER' -> 'MAJOR' -> 'MINOR'
        self._major     = 0.0
        self._minor     = 0.3   # default minor shown while major is being defined

        self._mx, self._my = event.mouse_region_x, event.mouse_region_y
        self._snap_cache = build_snap_cache(context)
        self._ref_obj    = _st.sketch_ref_obj or pick_reference_object(context)
        self._otrack     = SnapAcquisitionManager()
        self._otrack.reset(clear_points=True)

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "ELLIPSE"
        context.window_manager.modal_handler_add(self)
        context.area.header_text_set("ELLIPSE: Click centre  |  Esc=cancel")
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
                if self._phase == 'CENTER':
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
            if self._phase == 'CENTER':
                self._cen = self._cur.copy()
                self._phase = 'MAJOR'
                self._otrack.reset(clear_points=True)
                self._acquired_base = None
                self._base_candidate_key = None
                self._inp = ''
                self._typed_ray_frozen = None
                context.area.header_text_set("ELLIPSE: Click/type major axis  |  Esc=cancel")
            elif self._phase == 'MAJOR':
                self._major = self._effective_axis()
                self._phase = 'MINOR'
                self._otrack.reset(clear_points=True)
                self._inp = ''
                self._typed_ray_frozen = None
                context.area.header_text_set("ELLIPSE: Click/type minor axis  |  Esc=cancel")
            else:  # MINOR
                self._minor = self._effective_axis()
                self._finish(context)
                self.cancel(context)
                return {'FINISHED'}
            return {'RUNNING_MODAL'}

        if event.type in {'RET', 'NUMPAD_ENTER', 'SPACE'} and self._cen is not None:
            if self._phase == 'MAJOR':
                self._major = self._effective_axis()
                self._phase = 'MINOR'
                self._otrack.reset(clear_points=True)
                self._inp = ''
                self._typed_ray_frozen = None
                context.area.header_text_set("ELLIPSE: Click/type minor axis  |  Esc=cancel")
            else:  # MINOR
                self._minor = self._effective_axis()
                self._finish(context)
                self.cancel(context)
                return {'FINISHED'}
            return {'RUNNING_MODAL'}

        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
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
        self._base_candidate_key = key
        self._base_candidate_since = now

    def _typed_distance_ray(self):
        if self._otrack.points:
            ray = resolve_typed_distance_ray(self._otrack, self._raw)
            if ray is not None:
                return ray
        if self._phase == 'CENTER' and self._acquired_base is not None:
            direction = self._raw - self._acquired_base
            if direction.length < 1e-6:
                direction = Vector((1, 0, 0))
            return self._acquired_base, direction.normalized()
        if self._phase == 'MAJOR' and self._cen is not None:
            return self._cen, Vector((1, 0, 0))
        if self._phase == 'MINOR' and self._cen is not None:
            return self._cen, Vector((0, 1, 0))
        return None

    def _effective_axis(self) -> float:
        if self._cen and self._inp:
            try:
                val = parse_typed_length(self._context, self._inp)
            except ValueError:
                val = None
            if val is not None:
                if self._typed_ray_frozen is not None:
                    origin, direction = self._typed_ray_frozen
                    point = origin + direction * val
                    return (point - self._cen).length
                return val
        return (self._cur - self._cen).length if self._cen else 0.0

    def _update_cur(self):
        if self._phase == 'CENTER':
            if self._inp and self._typed_ray_frozen is not None:
                try:
                    val = parse_typed_length(self._context, self._inp)
                    origin, direction = self._typed_ray_frozen
                    self._cur = origin + direction * val
                    return
                except ValueError:
                    pass
            self._cur = self._raw.copy()
            return
        if self._inp:
            if self._typed_ray_frozen is not None:
                try:
                    val = parse_typed_length(self._context, self._inp)
                    origin, direction = self._typed_ray_frozen
                    self._cur = origin + direction * val
                    return
                except ValueError:
                    pass
            r = self._effective_axis()
            if self._phase == 'MAJOR':
                # Constrain to horizontal (X-axis) from center
                self._cur = Vector((self._cen.x + r, self._cen.y, 0.0))
            else:  # MINOR
                # Constrain to vertical (Y-axis) from center
                self._cur = Vector((self._cen.x, self._cen.y + r, 0.0))
        else:
            if self._phase == 'MAJOR':
                # Constrain to horizontal (X-axis) from center
                dx = self._raw.x - self._cen.x
                self._cur = Vector((self._cen.x + dx, self._cen.y, 0.0))
            else:  # MINOR
                # Constrain to vertical (Y-axis) from center
                dy = self._raw.y - self._cen.y
                self._cur = Vector((self._cen.x, self._cen.y + dy, 0.0))

    def _finish(self, context):
        major = max(self._major, 1e-6)
        minor = max(self._minor, 1e-6)
        segs = 64
        step = 2 * math.pi / segs
        verts = [
            (self._cen.x + major * math.cos(i * step),
             self._cen.y + minor * math.sin(i * step), 0.0)
            for i in range(segs)
        ]
        edges = [(i, (i + 1) % segs) for i in range(segs)]
        create_cad_object(
            context, "CAD_Ellipse", verts, edges, "ellipse",
            {"center": [self._cen.x, self._cen.y, 0], "major": major, "minor": minor, "segments": segs},
            reference_obj=self._ref_obj,
        )

    def _draw(self, context):
        # The crosshair/snap-marker/coordinate-readout family below all
        # stand in for "the cursor" - only draw them while the real OS
        # cursor is actually hidden (mouse over the usable viewport).
        show_cursor_fx = cursor_fx_visible(self)

        draw_selected_snap_points(context)
        if show_cursor_fx:
            draw_osnap_tracking(context, self._mx, self._my)
        mx, my = self._mx, self._my
        c2 = world_to_screen(context, self._cur)

        _snap_filter = _st.selection.get_active_stypes()
        show_center = _snap_filter is None or ('CENTER' in _snap_filter)

        if show_cursor_fx and c2:
            gfx_snap_marker(c2, self._snap_type)

        if self._cen is None:
            if show_cursor_fx:
                draw_dim_rows(
                    context, mx, my,
                    [("X:", f"{self._cur.x:.4f}", True, False),
                     ("Y:", f"{self._cur.y:.4f}", False, False)],
                )
        else:
            cn2 = world_to_screen(context, self._cen)
            if cn2 and show_center:
                draw_vertex_square(cn2, 5, fill_col=(1, 0, 1, 0.18), outline_col=(1, 0, 1, 1), outline_w=1.8)

            if self._phase == 'MAJOR':
                major = max(self._effective_axis(), 1e-6)
                minor = max(self._minor, 1e-6)
            else:  # MINOR
                major = max(self._major, 1e-6)
                minor = max(self._effective_axis(), 1e-6)

            # Live ellipse preview
            segs, step = 64, 2 * math.pi / 64
            pts_2d = []
            for i in range(segs):
                p3 = Vector((self._cen.x + major * math.cos(i * step),
                             self._cen.y + minor * math.sin(i * step), 0.0))
                p2 = world_to_screen(context, p3)
                if p2:
                    pts_2d.append(p2)
            if len(pts_2d) > 1:
                gfx_loop(pts_2d, (1, 1, 0, 0.9), 2.8)

            # Green dashed guide line (orthographic constraint visualization)
            if cn2 and c2 and self._phase in ('MAJOR', 'MINOR'):
                gfx_dashed_line(cn2, c2, col=(0.3, 1.0, 0.3, 0.7), dash_len=6, space_len=4, w=1.8)

            if c2 and self._phase in ('MAJOR', 'MINOR'):
                axis_val   = major if self._phase == 'MAJOR' else minor
                axis_label = "MA" if self._phase == 'MAJOR' else "MI"
                inp_str = (self._inp + "▌" if self._inp else format_length(context, axis_val))
                draw_float_dim(c2[0] + 50, c2[1], f"{axis_label}: {inp_str}", active=True, locked=bool(self._inp))

        if show_cursor_fx:
            cx, cy = c2 if c2 else (mx, my)
            draw_crosshair(context, cx, cy, show_box=False)
        gfx_text(f"ELLIPSE [{self._phase}]   MAJOR {self._major:.3f}   MINOR {self._minor:.3f}",
                 10, 55, 12, (1.0, 0.85, 0.2, 1.0))
