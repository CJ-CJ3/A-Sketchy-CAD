# ─────────────────────────────────────────────────────────────────────
#  operators/tool_slice.py
#  CAD_OT_SketchSlice  –  bisect visible mesh geometry with a cut line
# ─────────────────────────────────────────────────────────────────────

import math
import bpy
import bmesh
from mathutils import Vector

from ..utils.math_utils  import (
    build_snap_cache, apply_snaps, mouse_to_3d, world_to_screen,
)
from ..utils.otrack import SnapAcquisitionManager, update_tracking
from ..gfx.primitives import gfx_lines, gfx_dot, gfx_text, gfx_dashed_line
from ..gfx.markers    import gfx_snap_marker, draw_crosshair, draw_vertex_square, draw_osnap_tracking
from .. import state as _st
from ..utils.cursor_util import (
    update_modal_cursor, restore_modal_cursor, cursor_fx_visible, init_modal_cursor,
    guarded_modal,
    handle_ui_handoff,
    reset_tpanel_to_select,
)


class CAD_OT_SketchSlice(bpy.types.Operator):
    bl_idname  = "cad.sketch_slice"
    bl_label   = "CAD Slice"
    bl_options = {'REGISTER', 'UNDO'}

    # ── invoke ────────────────────────────────────────────────────────
    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._p1         = None
        self._cur        = Vector((0, 0, 0))
        self._snap_type  = 'FREE'
        self._snap_cache = build_snap_cache(context)
        self._mx         = event.mouse_region_x
        self._my         = event.mouse_region_y
        self._otrack     = SnapAcquisitionManager()
        self._otrack.reset(clear_points=True)

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "SLICE"
        context.window_manager.modal_handler_add(self)
        context.area.header_text_set(
            "SLICE: Click start point  |  Esc=cancel"
        )
        return {'RUNNING_MODAL'}

    # ── cancel ────────────────────────────────────────────────────────
    def cancel(self, context):
        try:
            bpy.types.SpaceView3D.draw_handler_remove(
                self._handler, 'WINDOW'
            )
        except ValueError:
            pass
        restore_modal_cursor(self, context)
        context.area.header_text_set(None)
        context.scene.cad_settings.active_tool = "SELECT"
        self._otrack.reset(clear_points=True)
        reset_tpanel_to_select(self, context)
        context.area.tag_redraw()

    # ── modal ─────────────────────────────────────────────────────────
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

        if event.type == 'MOUSEMOVE':
            raw = mouse_to_3d(context, self._mx, self._my)
            if raw:
                snapped, stype = apply_snaps(
                    context, raw, self._snap_cache
                ,
                                  snap_filter=_st.selection.get_active_stypes())
                self._snap_type = stype
                self._cur       = snapped
                update_tracking(self._otrack, snapped, stype)
            return {'RUNNING_MODAL'}

        if event.value != 'PRESS':
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE':
            if self._p1 is None:
                self._p1 = self._cur.copy()
                self._otrack.reset(clear_points=True)
                context.area.header_text_set(
                    "SLICE: Click end point  |  Esc=cancel"
                )
            else:
                self._execute_slice(context)
                self.cancel(context)
                return {'FINISHED'}

        if event.type in {'RIGHTMOUSE', 'ESC'}:
            self.cancel(context)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ── private helpers ───────────────────────────────────────────────

    def _execute_slice(self, context):
        p1, p2    = self._p1, self._cur
        direction = p2 - p1
        if direction.length < 1e-6:
            return

        # The slice plane is perpendicular to the XY cut-line, vertical.
        normal  = Vector((-direction.y, direction.x, 0.0)).normalized()
        targets = [o for o in context.visible_objects if o.type == 'MESH']
        if not targets:
            return

        for obj in targets:
            mw_inv   = obj.matrix_world.inverted()
            local_co = mw_inv @ Vector((p1.x, p1.y, 0.0))
            # Transform normal via inverse-transpose
            local_no = (
                mw_inv.to_3x3().transposed() @ normal
            ).normalized()

            me   = obj.data
            bm   = bmesh.new()
            bm.from_mesh(me)
            geom_in = bm.verts[:] + bm.edges[:] + bm.faces[:]
            bmesh.ops.bisect_plane(
                bm,
                geom=geom_in,
                plane_co=local_co,
                plane_no=local_no,
                clear_outer=False,
                clear_inner=False,
            )
            bm.to_mesh(me)
            bm.free()
            me.update()

        self.report({'INFO'}, f"Sliced {len(targets)} object(s)")

    # ── draw callback ─────────────────────────────────────────────────

    def _draw(self, context):
        # The OTRACK marker / snap marker / crosshair all stand in for
        # "the cursor" - only draw them while the real OS cursor is
        # actually hidden (mouse over the usable viewport).
        show_cursor_fx = cursor_fx_visible(self)

        if show_cursor_fx:
            draw_osnap_tracking(context, self._mx, self._my)
        mx, my = self._mx, self._my
        cur_2d = world_to_screen(context, self._cur)

        _snap_filter  = _st.selection.get_active_stypes()
        # The draw tool's own endpoint square (on placed click-points)
        # has been removed entirely - it's no longer drawn regardless
        # of the active Alt-menu snap mode.
        show_endpoint = False

        if self._p1 and cur_2d:
            p1_2d = world_to_screen(context, self._p1)
            if p1_2d:
                dx = cur_2d[0] - p1_2d[0]
                dy = cur_2d[1] - p1_2d[1]
                ln = math.hypot(dx, dy)
                if ln > 1:
                    ux, uy = dx / ln, dy / ln
                    ext = 2000
                    start_ext = (p1_2d[0] - ux * ext, p1_2d[1] - uy * ext)
                    end_ext   = (cur_2d[0] + ux * ext, cur_2d[1] + uy * ext)
                    gfx_dashed_line(
                        start_ext, end_ext,
                        col=(1.0, 0.3, 0.3, 0.6), w=1.5,
                    )
                # Main segment bright
                gfx_lines([p1_2d, cur_2d], (1.0, 0.3, 0.3, 0.95), 2.8)
                if show_endpoint:
                    draw_vertex_square(
                        p1_2d, 5,
                        fill_col=(1.0, 0.3, 0.3, 0.18),
                        outline_col=(1.0, 0.3, 0.3, 1.0),
                        outline_w=1.8,
                    )
                    draw_vertex_square(
                        cur_2d, 5,
                        fill_col=(1.0, 0.3, 0.3, 0.18),
                        outline_col=(1.0, 0.3, 0.3, 1.0),
                        outline_w=1.8,
                    )

        if show_cursor_fx and cur_2d:
            gfx_snap_marker(cur_2d, self._snap_type)
            if show_endpoint:
                draw_vertex_square(
                    cur_2d, 4,
                    fill_col=(1.0, 0.3, 0.3, 0.18),
                    outline_col=(1.0, 0.3, 0.3, 1.0),
                    outline_w=1.6,
                )

        if show_cursor_fx:
            cx, cy = cur_2d if cur_2d else (mx, my)
            draw_crosshair(context, cx, cy, show_box=False)  # Active tool: crosshair only

        phase = "Click end point" if self._p1 else "Click start point"
        gfx_text(
            f"SLICE  {phase}   X {self._cur.x:.3f}   Y {self._cur.y:.3f}",
            10, 55, 12, (1.0, 0.5, 0.2, 1.0),
        )
