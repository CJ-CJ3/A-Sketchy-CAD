# ─────────────────────────────────────────────────────────────────────
#  operators/tool_edit_move.py
#  CAD_OT_EditMove  –  move selected vertices / edges / faces while
#                      staying in Sketch Mode (invoked from tool_edit)
#
#  Workflow
#  --------
#  1.  CAD_OT_EditElement detects the M key (or a "Move" button press)
#      while vertices are selected and:
#        a.  writes  state.edit_pending_move = {'obj': obj, 'verts': [vi, …]}
#        b.  calls   bpy.ops.cad.edit_move('INVOKE_DEFAULT')
#
#  2.  CAD_OT_EditMove.invoke() picks up that payload, snapshots the
#      original vertex positions, and starts a modal loop.
#
#  3.  The modal loop mirrors CAD_OT_SketchMove but operates on vertex
#      coordinates rather than object origins:
#        – MOUSEMOVE     → reposition vertices live (with snap / axis lock)
#        – X / Y         → constrain to that world axis
#        – numeric keys  → type an exact distance
#        – LMB / RET     → commit
#        – RMB / ESC     → revert
# ─────────────────────────────────────────────────────────────────────

import bpy
import math
from mathutils import Vector

from ..utils.math_utils import (
    build_snap_cache, apply_snaps, apply_axis_lock, mouse_to_3d,
    world_to_screen, parse_typed_length, format_length,
)
from ..utils.otrack import SnapAcquisitionManager, update_tracking
from ..gfx.primitives import gfx_dot, gfx_text, gfx_dashed_line, gfx_lines, gfx_rect_fill, gfx_loop
from ..gfx.markers    import gfx_snap_marker, draw_crosshair, draw_osnap_tracking
from ..gfx.dims       import draw_float_dim
from .. import state as _st
from ..utils.cursor_util import (
    update_modal_cursor, restore_modal_cursor, cursor_fx_visible, init_modal_cursor,
    guarded_modal,
    handle_ui_handoff,
)


# ── colours (matches tool_edit palette) ──────────────────────────────
_COL_VERT_MOVE  = (1.00, 1.00, 1.00, 1.00)   # white – vertex in motion
_COL_DRAG_LINE  = (0.30, 0.90, 1.00, 0.70)   # cyan  – anchor→cursor guide
_GRIP_HALF      = 6                           # px half-size for vertex squares


class CAD_OT_EditMove(bpy.types.Operator):
    """Move selected vertices / edges / faces in Sketch Mode"""
    bl_idname  = "cad.edit_move"
    bl_label   = "CAD Edit Move"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, 'cad_settings', None)
        return s is not None and s.sketch_mode

    # ── invoke ────────────────────────────────────────────────────────

    def invoke(self, context, event):
        # Consume the pending-move payload written by tool_edit
        payload = _st.edit_pending_move
        _st.edit_pending_move = None

        if payload is None or not payload.get('verts'):
            self.report({'WARNING'}, "No vertices selected for move")
            return {'CANCELLED'}

        self._obj        = payload['obj']
        self._vert_idxs  = payload['verts']   # list of int

        # Snapshot original world-space positions
        mw   = self._obj.matrix_world
        mesh = self._obj.data
        self._orig_wcos = {
            vi: (mw @ mesh.vertices[vi].co).copy()
            for vi in self._vert_idxs
        }

        # Anchor: world position under the mouse at the moment M was pressed
        self._anchor     = mouse_to_3d(context, event.mouse_region_x,
                                       event.mouse_region_y)
        self._cur_3d     = self._anchor.copy()
        self._snap_cache = build_snap_cache(context)
        self._snap_type  = 'FREE'
        self._axis       = None      # None | 'X' | 'Y'
        self._inp        = ''        # typed numeric distance buffer
        self._mx         = event.mouse_region_x
        self._my         = event.mouse_region_y
        self._otrack     = SnapAcquisitionManager()
        self._otrack.reset(clear_points=True)

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = 'EDIT_MOVE'
        context.window_manager.modal_handler_add(self)
        context.area.header_text_set(
            "MOVE VERTICES: drag or type distance  |  X/Y=axis  |  "
            "LMB/Enter=confirm  |  Esc/RMB=cancel"
        )
        return {'RUNNING_MODAL'}

    # ── cancel / cleanup ──────────────────────────────────────────────

    def cancel(self, context):
        try:
            bpy.types.SpaceView3D.draw_handler_remove(self._handler, 'WINDOW')
        except ValueError:
            pass
        restore_modal_cursor(self, context)
        context.area.header_text_set(None)
        # Return control to the edit tool
        context.scene.cad_settings.active_tool = 'EDIT'
        self._otrack.reset(clear_points=True)
        if context.area:
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
            self._revert(context)
            self.cancel(context)
            return {'CANCELLED'}

        context.area.tag_redraw()
        self._mx = event.mouse_region_x
        self._my = event.mouse_region_y

        # Pass wheel / middle-mouse through so the viewport still works
        if event.type == 'MIDDLEMOUSE':
            return {'PASS_THROUGH'}
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN'}:
            return {'PASS_THROUGH'}

        # ── mouse movement ────────────────────────────────────────────
        if event.type == 'MOUSEMOVE':
            raw = mouse_to_3d(context, self._mx, self._my)
            if raw:
                snapped, stype = apply_snaps(context, raw, self._snap_cache,
                                  snap_filter=_st.selection.get_active_stypes())
                self._snap_type = stype
                update_tracking(self._otrack, snapped, stype)
                if self._axis:
                    snapped = apply_axis_lock(snapped, self._anchor, self._axis)
                self._cur_3d = snapped
                self._apply_move(context)
            return {'RUNNING_MODAL'}

        # ── key presses ───────────────────────────────────────────────
        if event.value == 'PRESS':
            ch = event.ascii

            # Numeric / decimal input for exact distance
            if ch and ch in '0123456789.':
                self._inp += ch
                self._apply_typed_move(context)
                return {'RUNNING_MODAL'}

            if ch == '-' and not self._inp:
                self._inp = '-'
                return {'RUNNING_MODAL'}

            if event.type == 'BACK_SPACE':
                self._inp = self._inp[:-1]
                self._apply_typed_move(context)
                return {'RUNNING_MODAL'}

            # Axis lock
            if event.type in {'X', 'Y'}:
                self._axis = (None if self._axis == event.type else event.type)
                self._apply_move(context)
                return {'RUNNING_MODAL'}

            # Confirm
            if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'}:
                self.cancel(context)
                return {'FINISHED'}

            # Cancel / revert
            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self._revert(context)
                self.cancel(context)
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ── private helpers ───────────────────────────────────────────────

    def _delta(self):
        """Current world-space translation vector."""
        return self._cur_3d - self._anchor

    def _apply_move(self, context):
        """Reposition vertices by the current drag delta."""
        obj    = self._obj
        mw_inv = obj.matrix_world.inverted()
        mesh   = obj.data
        delta  = self._delta()
        for vi in self._vert_idxs:
            orig = self._orig_wcos[vi]
            mesh.vertices[vi].co = mw_inv @ (orig + delta)
        mesh.update()

    def _apply_typed_move(self, context):
        """Apply an exact numeric distance along the current direction."""
        if not self._inp or self._inp == '-':
            return
        try:
            dist = parse_typed_length(context, self._inp)
        except ValueError:
            return
        delta = self._delta()
        if self._axis == 'X':
            move_vec = Vector((dist, 0.0, 0.0))
        elif self._axis == 'Y':
            move_vec = Vector((0.0, dist, 0.0))
        elif delta.length > 1e-6:
            move_vec = delta.normalized() * dist
        else:
            return
        obj    = self._obj
        mw_inv = obj.matrix_world.inverted()
        mesh   = obj.data
        for vi in self._vert_idxs:
            orig = self._orig_wcos[vi]
            mesh.vertices[vi].co = mw_inv @ (orig + move_vec)
        mesh.update()

    def _revert(self, context):
        """Restore all vertices to their pre-move positions."""
        obj    = self._obj
        mw_inv = obj.matrix_world.inverted()
        mesh   = obj.data
        for vi, wco in self._orig_wcos.items():
            mesh.vertices[vi].co = mw_inv @ wco
        mesh.update()

    # ── draw overlay ─────────────────────────────────────────────────

    def _draw(self, context):
        try:
            from .. import state as _st2
            if _st2.sketch_area_ref is None or context.area != _st2.sketch_area_ref:
                return

            mx, my = self._mx, self._my
            # The OTRACK marker / snap marker / crosshair below stand
            # in for "the cursor" - only draw them while the real OS
            # cursor is actually hidden (mouse over the usable
            # viewport).
            show_cursor_fx = cursor_fx_visible(self)
            if show_cursor_fx:
                draw_osnap_tracking(context, mx, my)

            # Dashed line from anchor to cursor
            anchor_2d = world_to_screen(context, self._anchor)
            cur_2d    = world_to_screen(context, self._cur_3d)

            if anchor_2d and cur_2d:
                gfx_dashed_line(anchor_2d, cur_2d,
                                col=(0.30, 0.90, 1.00, 0.70), w=1.5)
                gfx_dot(anchor_2d, (0.30, 0.90, 1.00, 0.90), 6)
                gfx_dot(cur_2d,    (0.30, 0.90, 1.00, 0.90), 6)

                # Distance label at midpoint
                delta = self._delta()
                dist  = delta.length
                if dist > 0.001:
                    mid_x = (anchor_2d[0] + cur_2d[0]) / 2
                    mid_y = (anchor_2d[1] + cur_2d[1]) / 2
                    disp  = (self._inp + "▌" if self._inp
                             else format_length(context, dist))
                    draw_float_dim(mid_x, mid_y + 22, disp, active=True)

            # Draw moved vertices as bright white squares
            obj = self._obj
            mw  = obj.matrix_world
            mesh = obj.data
            h = _GRIP_HALF
            for vi in self._vert_idxs:
                sx_sy = world_to_screen(context, mw @ mesh.vertices[vi].co)
                if sx_sy:
                    sx, sy = sx_sy
                    gfx_rect_fill(sx - h, sy - h, h * 2, h * 2,
                                  (*_COL_VERT_MOVE[:3], 0.25))
                    gfx_loop(
                        [(sx-h, sy-h), (sx+h, sy-h),
                         (sx+h, sy+h), (sx-h, sy+h)],
                        _COL_VERT_MOVE, 2.0,
                    )

            # Snap marker + crosshair
            if show_cursor_fx:
                snap_2d = world_to_screen(context, self._cur_3d)
                if snap_2d:
                    gfx_snap_marker(snap_2d, self._snap_type)
                cx, cy = snap_2d if snap_2d else (mx, my)
                draw_crosshair(context, cx, cy, self._axis, show_box=False)

            # HUD status line
            delta = self._delta()
            ax    = f"  [{self._axis}]" if self._axis else ""
            n     = len(self._vert_idxs)
            gfx_text(
                f"MOVE VERTS{ax}   {n} vert{'s' if n != 1 else ''}   "
                f"dX {delta.x:+.3f}   dY {delta.y:+.3f}",
                10, 55, 12, (0.30, 0.90, 1.00, 1.00),
            )

        except Exception:
            import traceback
            traceback.print_exc()
