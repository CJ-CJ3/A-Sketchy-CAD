# ─────────────────────────────────────────────────────────────────────
#  operators/tool_circle.py
#  CAD_OT_DrawCircle  –  click-centre then click/type radius
# ─────────────────────────────────────────────────────────────────────

import math
import time
import bpy
from mathutils import Vector

from ..utils.math_utils  import (
    build_snap_cache, apply_snaps, mouse_to_3d, world_to_screen, parse_typed_length, format_length,
)
from ..utils.otrack import SnapAcquisitionManager, update_tracking, resolve_typed_distance_ray
from ..utils.cad_objects import create_cad_object, pick_reference_object
from .. import state as _st
from ..utils.cursor_util import (
    update_modal_cursor, restore_modal_cursor, cursor_fx_visible, init_modal_cursor,
    guarded_modal,
    handle_ui_handoff,
    reset_tpanel_to_select,
)
from ..gfx.primitives    import gfx_dot, gfx_circle, gfx_lines, gfx_text
from ..gfx.markers       import gfx_snap_marker, draw_crosshair, draw_vertex_square, draw_selected_snap_points, draw_osnap_tracking
from ..gfx.dims          import draw_float_dim, draw_dim_rows


class CAD_OT_DrawCircle(bpy.types.Operator):
    bl_idname  = "cad.draw_circle"
    bl_label   = "CAD Circle"
    bl_options = {'REGISTER', 'UNDO'}

    # ── invoke ────────────────────────────────────────────────────────
    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._context     = context   # kept for helpers that don't take context directly (unit-aware parsing, etc.)
        self._cen        = None
        self._raw        = Vector((0, 0, 0))
        self._cur        = Vector((0, 0, 0))
        self._snap_type  = 'FREE'
        self._inp        = ''
        self._typed_ray_frozen = None  # (origin, direction) | None - OTRACK ray locked at first keystroke

        # ── Hover-acquired temporary base point (implicit centre) ──────
        # Same mechanism as CAD_OT_DrawLine: hovering a valid snap
        # candidate for _BASE_ACQUIRE_TIME before the centre is placed
        # acquires it as a hidden basePoint, letting a typed distance
        # (relative to it or to an acquired OTRACK guide) place the
        # centre itself, exactly as if it had been clicked there.
        self._base_candidate_key   = None
        self._base_candidate_since = 0.0
        self._acquired_base        = None   # Vector | None

        self._rmode      = 'RADIUS'   # or 'DIAMETER'
        self._mid_held   = False
        self._mx         = event.mouse_region_x
        self._my         = event.mouse_region_y
        self._snap_cache = build_snap_cache(context)
        self._ref_obj    = _st.sketch_ref_obj or pick_reference_object(context)
        self._otrack     = SnapAcquisitionManager()
        self._otrack.reset(clear_points=True)

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "CIRCLE"
        context.window_manager.modal_handler_add(self)
        context.area.header_text_set(
            "CIRCLE: Click centre  |  Alt=R/D  |  Esc=cancel"
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
        self._context = context   # kept in sync every event, for helpers that don't take context directly
        if not context.scene.cad_settings.sketch_mode:
            self.cancel(context)
            return {'CANCELLED'}

        context.area.tag_redraw()
        self._mx, self._my = event.mouse_region_x, event.mouse_region_y

        if event.type == 'MIDDLEMOUSE':
            self._mid_held = (event.value == 'PRESS')
            return {'PASS_THROUGH'}
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN'}:
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            if self._mid_held:
                return {'PASS_THROUGH'}
            raw = mouse_to_3d(context, self._mx, self._my)
            if raw:
                self._raw, self._snap_type = apply_snaps(
                    context, raw, self._snap_cache
                ,
                                  snap_filter=_st.selection.get_active_stypes())
                self._update_cur()
                update_tracking(self._otrack, self._raw, self._snap_type)
                self._update_hover_base_point(context, self._snap_type)
            return {'RUNNING_MODAL'}

        if event.value != 'PRESS':
            return {'RUNNING_MODAL'}

        if event.type in {'LEFT_ALT', 'RIGHT_ALT'}:
            self._rmode = ('DIAMETER' if self._rmode == 'RADIUS'
                           else 'RADIUS')
            self._update_cur()
            return {'RUNNING_MODAL'}

        ch = event.ascii
        if ch and ch in '0123456789.':
            if not self._inp:
                self._typed_ray_frozen = self._typed_distance_ray()
            self._inp += ch
            self._update_cur()
            return {'RUNNING_MODAL'}
        if event.type == 'BACK_SPACE':
            self._inp = self._inp[:-1]
            if not self._inp:
                self._typed_ray_frozen = None
            self._update_cur()
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE':
            if self._cen is None:
                self._cen = self._cur.copy()
                self._otrack.reset(clear_points=True)
                self._acquired_base        = None
                self._base_candidate_key   = None
                self._inp                  = ''
                self._typed_ray_frozen     = None
            else:
                self._finish(context)
                self.cancel(context)
                return {'FINISHED'}

        if event.type == 'ESC':
            if self._cen is None and self._acquired_base is not None:
                self._acquired_base        = None
                self._base_candidate_key   = None
                self._inp                  = ''
                self._typed_ray_frozen     = None
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            self.cancel(context)
            return {'CANCELLED'}

        if event.type == 'RIGHTMOUSE':
            self.cancel(context)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ── private helpers ───────────────────────────────────────────────

    _BASE_ACQUIRE_TIME = 0.25   # seconds to acquire a hover candidate as basePoint

    def _update_hover_base_point(self, context, stype):
        """Before the centre has been placed and before typing has
        started, continuously track whichever valid snap candidate has
        been hovered for _BASE_ACQUIRE_TIME - acquiring it as a
        temporary basePoint. Identical to CAD_OT_DrawLine's version."""
        if self._cen or self._inp:
            return

        if stype in ('FREE', 'GRID', 'INCREMENT', 'TRACKING'):
            key = None
        else:
            key = tuple(round(c, 6) for c in self._raw)

        now = time.time()
        if key == self._base_candidate_key:
            if (key is not None
                    and now - self._base_candidate_since >= self._BASE_ACQUIRE_TIME):
                self._acquired_base = Vector(key)
            return

        self._base_candidate_key   = key
        self._base_candidate_since = now

    def _typed_distance_ray(self):
        """(origin, direction) to use for typed-distance entry.

        Before the centre is placed: prefers a currently-active OTRACK
        guide (resolve_typed_distance_ray) over the generic
        hover-acquired basePoint - same priority as
        CAD_OT_DrawLine._typed_distance_ray(). After the centre is
        placed, this is the ray the radius is measured along, exactly
        as before.
        """
        if self._otrack.points:
            ray = resolve_typed_distance_ray(self._otrack, self._raw)
            if ray is not None:
                return ray

        if not self._cen and self._acquired_base is not None:
            direction = self._raw - self._acquired_base
            if direction.length < 1e-6:
                direction = Vector((1, 0, 0))
            return self._acquired_base, direction.normalized()

        return None

    def _effective_radius(self) -> float:
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
                return (val / 2) if self._rmode == 'DIAMETER' else val
        return (self._cur - self._cen).length if self._cen else 0.0

    def _update_cur(self):
        if not self._cen:
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
            r = self._effective_radius()
            d = self._raw - self._cen
            if d.length < 1e-6:
                d = Vector((1, 0, 0))
            self._cur = self._cen + d.normalized() * r
        else:
            self._cur = self._raw.copy()

    def _finish(self, context):
        r    = self._effective_radius()
        segs = 64
        step = 2 * math.pi / segs
        verts = [
            (self._cen.x + r * math.cos(i * step),
             self._cen.y + r * math.sin(i * step),
             0.0)
            for i in range(segs)
        ]
        edges = [(i, (i + 1) % segs) for i in range(segs)]
        create_cad_object(
            context, "CAD_Circle", verts, edges, "circle",
            {"center": [self._cen.x, self._cen.y, 0],
             "radius": r, "segments": segs},
            reference_obj=self._ref_obj,
        )
        context.scene.cad_settings.last_radius = r

    # ── draw callback ─────────────────────────────────────────────────

    def _draw(self, context):
        # The crosshair/snap-marker/coordinate-readout family below all
        # stand in for "the cursor" - only draw them while the real OS
        # cursor is actually hidden (mouse over the usable viewport).
        show_cursor_fx = cursor_fx_visible(self)

        draw_selected_snap_points(context)
        if show_cursor_fx:
            draw_osnap_tracking(context, self._mx, self._my)
        mx, my = self._mx, self._my
        c2     = world_to_screen(context, self._cur)

        _snap_filter  = _st.selection.get_active_stypes()
        # The draw tool's own endpoint square (on placed click-points)
        # has been removed entirely - it's no longer drawn regardless
        # of the active Alt-menu snap mode.
        show_endpoint = False
        show_center = _snap_filter is None or ('CENTER' in _snap_filter)

        if show_cursor_fx and c2:
            gfx_snap_marker(c2, self._snap_type)
            if show_endpoint:
                draw_vertex_square(
                    c2, 4,
                    fill_col=(1, 1, 0, 0.18),
                    outline_col=(1, 1, 0, 0.95),
                    outline_w=1.6,
                )

        if self._cen is not None:
            cn2 = world_to_screen(context, self._cen)
            if cn2 and show_center:
                draw_vertex_square(
                    cn2, 5,
                    fill_col=(1, 0, 1, 0.18),
                    outline_col=(1, 0, 1, 1),
                    outline_w=1.8,
                )
            if cn2 and c2:
                eff_r = self._effective_radius()
                rpx   = math.hypot(c2[0] - cn2[0], c2[1] - cn2[1])
                gfx_circle(cn2, rpx, (1, 1, 0, 0.9), w=2.8)
                gfx_lines([cn2, c2], (1, 1, 0, 0.40), 2.2)
                lbl     = "D" if self._rmode == 'DIAMETER' else "R"
                val     = eff_r * 2 if self._rmode == 'DIAMETER' else eff_r
                inp_str = (self._inp + "▌" if self._inp
                           else format_length(context, val))
                draw_float_dim(
                    c2[0] + 50, c2[1],
                    f"{lbl}: {inp_str}",
                    active=True, locked=bool(self._inp),
                )
        elif show_cursor_fx:
            if self._inp:
                draw_dim_rows(
                    context, mx, my,
                    [("Dist:", self._inp + "▌", True, False)],
                )
            else:
                draw_dim_rows(
                    context, mx, my,
                    [
                        ("X:", f"{self._cur.x:.4f}", True,  False),
                        ("Y:", f"{self._cur.y:.4f}", False, False),
                    ],
                )

        if show_cursor_fx:
            cx, cy = c2 if c2 else (mx, my)
            draw_crosshair(context, cx, cy, show_box=False)  # Active tool: crosshair only
        gfx_text(
            f"CIRCLE [{'Radius' if self._rmode == 'RADIUS' else 'Diameter'}]"
            f"   X {self._cur.x:.3f}   Y {self._cur.y:.3f}",
            10, 55, 12, (1.0, 0.85, 0.2, 1.0),
        )
