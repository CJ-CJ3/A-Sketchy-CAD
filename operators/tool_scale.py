# ─────────────────────────────────────────────────────────────────────
#  operators/tool_scale.py
#  CAD_OT_SketchScale – AutoCAD-style SCALE command (uniform 2D).
#
#  Workflow
#  --------
#  1.  Activate the Scale Tool.
#  2.  Select object(s) - either beforehand (native Blender selection),
#      or right here by clicking in the viewport (Shift adds/toggles).
#  3.  Press Enter (or click empty space with nothing picked) to
#      confirm the selection and specify an ANCHOR point - full OSNAP
#      + OTRACK, exactly like Move's base point.
#  4.  Type a scale factor (e.g. 2, 0.5, 1.5). The selection is scaled
#      live every frame about the anchor as digits are typed - a
#      genuine live preview, not a ghost overlay - then committed on
#      Enter.
#  5.  Esc cancels at *any* stage; once past the ANCHOR step, Esc/RMB
#      also reverts everything already previewed back to its original
#      size before cancelling.
#
#  Only vertex positions change (recomputed from each vertex's
#  original world position relative to the anchor) - mesh topology is
#  never touched, so Scale can only resize geometry, never reshape it.
# ─────────────────────────────────────────────────────────────────────

import math
import bpy
from mathutils import Vector

from ..utils.math_utils import build_snap_cache, apply_snaps, mouse_to_3d, world_to_screen
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
from ..gfx.dims       import draw_float_dim
from ..gfx.preview    import draw_reference_ray


_PICK_RADIUS_PX = 12   # screen-space picking radius for the SELECT phase


class CAD_OT_SketchScale(bpy.types.Operator):
    """AutoCAD-style SCALE: select whole object(s), pick an anchor
    point, type a scale factor - the selection is resized uniformly
    about the anchor, which stays fixed."""
    bl_idname  = "cad.sketch_scale"
    bl_label   = "CAD Scale"
    bl_options = {'REGISTER', 'UNDO'}

    # ── invoke ────────────────────────────────────────────────────────
    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._mx, self._my = event.mouse_region_x, event.mouse_region_y
        self._inp        = ''         # typed numeric scale-factor buffer
        self._factor      = 1.0
        self._snap_type   = 'FREE'
        self._snap_cache  = build_snap_cache(context)
        self._otrack      = SnapAcquisitionManager()
        self._otrack.reset(clear_points=True)
        self._cur_3d      = mouse_to_3d(context, self._mx, self._my) or Vector((0.0, 0.0, 0.0))
        self._anchor      = None
        self._orig_wcos   = {}   # {obj_name: {vi: orig_world_co}}

        # Objects only, mesh type - Scale always resizes whole objects.
        self._sel_objects = [o for o in context.selected_objects if o.type == 'MESH']

        self._phase = 'ANCHOR' if self._sel_objects else 'SELECT'

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "SCALE"
        context.window_manager.modal_handler_add(self)
        self._update_header(context)
        return {'RUNNING_MODAL'}

    def _update_header(self, context):
        if self._phase == 'SELECT':
            txt = ("SCALE: click to select object(s)  "
                   "(Shift=add)  |  Enter=confirm  |  Esc=cancel")
        elif self._phase == 'ANCHOR':
            txt = "SCALE: specify anchor point  |  Esc=cancel"
        else:
            txt = ("SCALE: drag to resize, or type a factor (e.g. 2, 0.5, 1.5)  |  "
                   "Enter=apply  |  Esc=cancel")
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
        """Revert any live-preview scaling already applied, then clean
        up. Safe to call from any phase - if the ANCHOR was never
        committed, _orig_wcos is still empty and this is a no-op."""
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
        """Commit: keep the live-previewed scale as final."""
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
        if self._phase == 'ANCHOR':
            return self._modal_anchor(context, event)
        return self._modal_scale(context, event)

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
                self._phase = 'ANCHOR'
                self._update_header(context)
                return {'RUNNING_MODAL'}

            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ── ANCHOR phase ─────────────────────────────────────────────────
    def _modal_anchor(self, context, event):
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
                self._commit_anchor(context)
                return {'RUNNING_MODAL'}
            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def _commit_anchor(self, context):
        self._anchor    = self._cur_3d.copy()
        self._orig_wcos = {}
        all_pts = []
        for obj in self._sel_objects:
            mw = obj.matrix_world
            wcos = {vi: (mw @ v.co).copy() for vi, v in enumerate(obj.data.vertices)}
            self._orig_wcos[obj.name] = wcos
            all_pts.extend(wcos.values())

        # Reference distance for drag-scaling: anchor -> centroid of
        # the selected geometry. Moving the cursor out to that same
        # distance from the anchor is factor 1.0 (no change); farther
        # grows it, closer shrinks it - same principle as Blender's
        # native S key, just anchored at a user-picked point instead
        # of the transform pivot.
        if all_pts:
            centroid = sum(all_pts, Vector((0.0, 0.0, 0.0))) / len(all_pts)
            self._ref_dist = max(1e-4, (centroid - self._anchor).length)
        else:
            self._ref_dist = 1.0

        self._otrack.reset(clear_points=True)
        self._inp    = ''
        self._factor = 1.0
        self._phase  = 'SCALE'
        self._update_header(context)

    # ── SCALE phase ──────────────────────────────────────────────────
    def _modal_scale(self, context, event):
        if event.type == 'MOUSEMOVE':
            raw = mouse_to_3d(context, self._mx, self._my)
            if raw:
                snapped, stype = apply_snaps(
                    context, raw, self._snap_cache,
                    snap_filter=_st.selection.get_active_stypes(),
                    anchor=self._anchor,
                )
                self._snap_type = stype
                update_tracking(self._otrack, snapped, stype)
                self._cur_3d = snapped
                dist = (snapped - self._anchor).length
                self._factor = max(1e-4, dist / self._ref_dist)
                self._inp = ''
                self._apply_factor(self._factor)
            return {'RUNNING_MODAL'}

        if event.value == 'PRESS':
            ch = event.ascii
            if ch and ch in '0123456789.':
                self._inp += ch
                self._apply_typed_factor()
                return {'RUNNING_MODAL'}
            if event.type == 'BACK_SPACE':
                self._inp = self._inp[:-1]
                self._apply_typed_factor()
                return {'RUNNING_MODAL'}

            if event.type in {'RET', 'NUMPAD_ENTER'}:
                self._finish(context)
                return {'FINISHED'}

            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ── shared scaling helpers ───────────────────────────────────────
    def _apply_factor(self, factor: float):
        anchor = self._anchor
        for obj_name, wcos in self._orig_wcos.items():
            obj = bpy.data.objects.get(obj_name)
            if obj is None:
                continue
            mw_inv = obj.matrix_world.inverted()
            mesh   = obj.data
            for vi, orig_co in wcos.items():
                new_world = Vector((
                    anchor.x + (orig_co.x - anchor.x) * factor,
                    anchor.y + (orig_co.y - anchor.y) * factor,
                    orig_co.z,   # never touch Z - flat 2D scale, matches Rotate's convention
                ))
                mesh.vertices[vi].co = mw_inv @ new_world
            mesh.update()

    def _apply_typed_factor(self):
        if not self._inp:
            self._factor = 1.0
            self._apply_factor(self._factor)
            return
        try:
            factor = float(self._inp)
        except ValueError:
            return
        if factor <= 0.0:
            return
        self._factor = factor
        self._apply_factor(self._factor)

    # ── draw callback ────────────────────────────────────────────────
    # Orange, matching CAD_OT_SketchModeManager._COL_SELECTED - so an
    # object reads as "still selected" rather than "hovered" (which
    # uses yellow) while Scale is running.
    _COL_SELECTED = (1.00, 0.50, 0.00, 0.95)

    def _draw_persistent_selection(self, context):
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
                "SCALE: select object(s), Enter to confirm",
                10, 55, 12, (0.3, 0.9, 1.0, 1.0),
            )
            return

        cur_2d = world_to_screen(context, self._cur_3d) if self._cur_3d else None

        if self._phase == 'ANCHOR':
            if show_cursor_fx:
                if cur_2d:
                    gfx_snap_marker(cur_2d, self._snap_type)
                cx, cy = cur_2d if cur_2d else (self._mx, self._my)
                draw_crosshair(context, cx, cy, None, show_box=False)
            gfx_text("SCALE: specify anchor point", 10, 55, 12, (0.3, 0.9, 1.0, 1.0))
            return

        # SCALE phase - the geometry itself already shows the live
        # preview since _apply_factor() resizes the real vertices every
        # frame; here we draw the anchor->cursor drag guide plus a
        # marker for where factor == 1.0 sits along that direction.
        anchor_2d = world_to_screen(context, self._anchor)
        if anchor_2d and cur_2d:
            draw_reference_ray(anchor_2d, cur_2d)
            direction = self._cur_3d - self._anchor
            if direction.length > 1e-6:
                ref_world = self._anchor + direction.normalized() * self._ref_dist
                ref_2d = world_to_screen(context, ref_world)
                if ref_2d:
                    gfx_dot(ref_2d, (0.6, 0.6, 0.6, 0.85), 4)
            gfx_dot(cur_2d, (0.3, 0.9, 1.0, 0.9), 6)
        if anchor_2d:
            gfx_dot(anchor_2d, (0.3, 0.9, 1.0, 0.9), 7)
        if show_cursor_fx:
            if cur_2d:
                gfx_snap_marker(cur_2d, self._snap_type)

            draw_crosshair(context, self._mx, self._my, None, show_box=False)

        disp = self._inp + "▌" if self._inp else f"{self._factor:.4f}"
        draw_float_dim(self._mx + 60, self._my, f"× {disp}", active=True, locked=bool(self._inp))

        gfx_text(
            f"SCALE   ×{self._factor:.4f}",
            10, 55, 12, (0.3, 0.9, 1.0, 1.0),
        )
