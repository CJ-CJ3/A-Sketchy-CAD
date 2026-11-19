# ─────────────────────────────────────────────────────────────────────
#  operators/tool_rect.py
#  CAD_OT_DrawRect  –  click-corner then click/type opposite corner
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
from ..gfx.primitives    import gfx_loop, gfx_text, gfx_dashed_line, gfx_dot
from ..gfx.markers       import gfx_snap_marker, draw_crosshair, draw_vertex_square, draw_selected_snap_points, draw_osnap_tracking
from ..gfx.dims          import draw_float_dim, draw_dim_rows


class CAD_OT_DrawRect(bpy.types.Operator):
    bl_idname  = "cad.draw_rect"
    bl_label   = "CAD Rectangle"
    bl_options = {'REGISTER', 'UNDO'}

    # ── invoke ────────────────────────────────────────────────────────
    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._context     = context   # kept for helpers that don't take context directly (unit-aware parsing, etc.)
        self._corn       = None
        self._raw        = Vector((0, 0, 0))
        self._cur        = Vector((0, 0, 0))
        self._snap_type  = 'FREE'
        self._inp        = ''
        self._typed_ray_frozen = None  # (origin, direction) | None - OTRACK ray locked at first keystroke

        # ── Hover-acquired temporary base point (implicit first corner) ─
        # Same mechanism as CAD_OT_DrawLine: hovering a valid snap
        # candidate for _BASE_ACQUIRE_TIME before the first corner is
        # placed acquires it as a hidden basePoint, letting a typed
        # distance (relative to it or to an acquired OTRACK guide)
        # place the first corner itself, exactly as if it had been
        # clicked there.
        self._base_candidate_key   = None
        self._base_candidate_since = 0.0
        self._acquired_base        = None   # Vector | None

        self._inp_mode   = 'WIDTH'    # or 'HEIGHT'
        self._lock_w     = None
        self._lock_h     = None
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
        context.scene.cad_settings.active_tool = "RECT"
        context.window_manager.modal_handler_add(self)
        context.area.header_text_set(
            "RECT: Click first corner  |  Esc=cancel"
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
                self._refresh_cur()
                update_tracking(self._otrack, self._raw, self._snap_type)
                self._update_hover_base_point(context, self._snap_type)
            return {'RUNNING_MODAL'}

        if event.value != 'PRESS':
            return {'RUNNING_MODAL'}

        if event.type in {'TAB', 'LEFT_ALT', 'RIGHT_ALT'} and self._corn:
            self._flip_rect_mode()
            return {'RUNNING_MODAL'}

        ch = event.ascii
        if ch and ch in '0123456789.':
            if not self._inp:
                self._typed_ray_frozen = self._typed_distance_ray()
            self._inp += ch
            self._refresh_cur()
            return {'RUNNING_MODAL'}
        if event.type == 'BACK_SPACE':
            if self._inp:
                self._inp = self._inp[:-1]
            elif self._corn:
                if self._inp_mode == 'WIDTH':
                    self._lock_w = None
                else:
                    self._lock_h = None
            if not self._inp:
                self._typed_ray_frozen = None
            self._refresh_cur()
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE':
            if self._corn is None:
                self._corn = self._cur.copy()
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
            if self._corn is None and self._acquired_base is not None:
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
        """Before the first corner has been placed and before typing
        has started, continuously track whichever valid snap candidate
        has been hovered for _BASE_ACQUIRE_TIME - acquiring it as a
        temporary basePoint. Identical to CAD_OT_DrawLine's version."""
        if self._corn or self._inp:
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

        Before the first corner is placed: prefers a currently-active
        OTRACK guide (resolve_typed_distance_ray) over the generic
        hover-acquired basePoint - same priority as
        CAD_OT_DrawLine._typed_distance_ray(). After the first corner
        is placed, this is the ray the W/H typed value is measured
        along, exactly as before.
        """
        if self._otrack.points:
            ray = resolve_typed_distance_ray(self._otrack, self._raw)
            if ray is not None:
                return ray

        if not self._corn and self._acquired_base is not None:
            direction = self._raw - self._acquired_base
            if direction.length < 1e-6:
                direction = Vector((1, 0, 0))
            return self._acquired_base, direction.normalized()

        return None

    def _flip_rect_mode(self):
        if self._inp:
            try:
                val = abs(parse_typed_length(self._context, self._inp))
                if self._inp_mode == 'WIDTH':
                    self._lock_w = val
                else:
                    self._lock_h = val
            except ValueError:
                pass
        self._inp_mode = 'HEIGHT' if self._inp_mode == 'WIDTH' else 'WIDTH'
        self._inp = ''
        self._typed_ray_frozen = None
        self._refresh_cur()

    def _refresh_cur(self):
        if not self._corn:
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

        if self._inp and self._typed_ray_frozen is not None:
            try:
                val = parse_typed_length(self._context, self._inp)
                origin, direction = self._typed_ray_frozen
                self._cur = origin + direction * val
                return
            except ValueError:
                pass

        eff_w = self._lock_w
        eff_h = self._lock_h

        if self._inp_mode == 'WIDTH' and self._inp:
            try:
                eff_w = abs(parse_typed_length(self._context, self._inp))
            except ValueError:
                pass
        if self._inp_mode == 'HEIGHT' and self._inp:
            try:
                eff_h = abs(parse_typed_length(self._context, self._inp))
            except ValueError:
                pass

        sx = 1 if self._raw.x >= self._corn.x else -1
        sy = 1 if self._raw.y >= self._corn.y else -1
        ex = self._corn.x + sx * eff_w if eff_w is not None else self._raw.x
        ey = self._corn.y + sy * eff_h if eff_h is not None else self._raw.y
        self._cur = Vector((ex, ey, 0.0))

    def _finish(self, context):
        c, e = self._corn, self._cur
        w, h = e.x - c.x, e.y - c.y
        verts = [
            (c.x, c.y, 0), (e.x, c.y, 0),
            (e.x, e.y, 0), (c.x, e.y, 0),
        ]
        edges = [(0, 1), (1, 2), (2, 3), (3, 0)]
        create_cad_object(
            context, "CAD_Rectangle", verts, edges, "rectangle",
            {"corner": [c.x, c.y, 0], "end": [e.x, e.y, 0],
             "width": abs(w), "height": abs(h)},
            reference_obj=self._ref_obj,
        )
        s = context.scene.cad_settings
        s.last_width  = abs(w)
        s.last_height = abs(h)
        s.last_length = abs(w)

    # ── draw callback ─────────────────────────────────────────────────

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
        c2     = world_to_screen(context, self._cur)

        _snap_filter  = _st.selection.get_active_stypes()
        # The draw tool's own endpoint square (on placed click-points)
        # has been removed entirely - it's no longer drawn regardless
        # of the active Alt-menu snap mode.
        show_endpoint = False

        if show_cursor_fx and c2:
            gfx_snap_marker(c2, self._snap_type)
            if show_endpoint:
                draw_vertex_square(
                    c2, 4,
                    fill_col=(1, 1, 0, 0.18),
                    outline_col=(1, 1, 0, 0.95),
                    outline_w=1.6,
                )

        if self._corn is not None:
            cc, ce = self._corn, self._cur
            p3 = [
                Vector((cc.x, cc.y, 0)),
                Vector((ce.x, cc.y, 0)),
                Vector((ce.x, ce.y, 0)),
                Vector((cc.x, ce.y, 0)),
            ]
            p2 = [world_to_screen(context, p) for p in p3]
            if all(p2):
                # Diagonal guide: corner-to-corner, dashed green - only
                # shown once the shape is (near enough) a square, as
                # confirmation the diagonal is sitting at exactly 45°.
                W_val = abs(ce.x - cc.x)
                H_val = abs(ce.y - cc.y)
                if max(W_val, H_val) > 1e-6:
                    is_square = abs(W_val - H_val) / max(W_val, H_val) < 0.01
                else:
                    is_square = False
                if is_square:
                    gfx_dashed_line(
                        p2[0], p2[2],
                        col=(0.2, 0.95, 0.2, 0.55),
                        dash_len=6, space_len=5, w=1.2,
                    )
                gfx_loop(p2, (1, 1, 0, 0.9), 2.8)
                if show_endpoint:
                    for p in p2:
                        draw_vertex_square(
                            p, 4,
                            fill_col=(1, 1, 0, 0.18),
                            outline_col=(1, 1, 0, 0.95),
                            outline_w=1.6,
                        )
                W_val = abs(ce.x - cc.x)
                H_val = abs(ce.y - cc.y)

                w_str = (
                    self._inp + "▌"
                    if self._inp_mode == 'WIDTH' and self._inp
                    else f"\U0001f512 {format_length(context, self._lock_w)}"
                    if self._lock_w is not None
                    else format_length(context, W_val)
                )
                h_str = (
                    self._inp + "▌"
                    if self._inp_mode == 'HEIGHT' and self._inp
                    else f"\U0001f512 {format_length(context, self._lock_h)}"
                    if self._lock_h is not None
                    else format_length(context, H_val)
                )
                # Dynamic dimension lines - same AutoCAD-style live
                # preview as CAD_OT_DrawLine's segment dimension:
                # dotted extension lines from each edge's endpoints out
                # to a dotted dimension line running parallel to it,
                # with the value centred on that line. Reused here for
                # both the width (bottom edge) and height (right edge)
                # of the rectangle, offset outward from the shape.
                def _draw_edge_dim(pa, pb, offset, direction, val_str,
                                    active, locked):
                    ox, oy = direction[0] * offset, direction[1] * offset
                    d1 = (pa[0] + ox, pa[1] + oy)
                    d2 = (pb[0] + ox, pb[1] + oy)
                    dim_col = (0.90, 0.90, 0.90, 0.90)
                    gfx_dashed_line(pa, d1, col=dim_col,
                                     dash_len=1.5, space_len=3.5, w=1.1)
                    gfx_dashed_line(pb, d2, col=dim_col,
                                     dash_len=1.5, space_len=3.5, w=1.1)
                    gfx_dashed_line(d1, d2, col=dim_col,
                                     dash_len=1.5, space_len=3.5, w=1.1)
                    # Round dot markers at each dimension-line end,
                    # matching AutoCAD's tracking-point glyphs
                    gfx_dot(d1, col=(1.0, 1.0, 1.0, 0.95), size=6.0)
                    gfx_dot(d2, col=(1.0, 1.0, 1.0, 0.95), size=6.0)
                    mid = ((d1[0] + d2[0]) / 2, (d1[1] + d2[1]) / 2)
                    draw_float_dim(
                        mid[0], mid[1], val_str,
                        active=active, locked=locked,
                    )

                # Offset scales with each edge's on-screen length,
                # matching the Line tool's behaviour, clamped for
                # legibility on very short/long edges.
                w_edge_len = math.hypot(p2[1][0] - p2[0][0], p2[1][1] - p2[0][1])
                h_edge_len = math.hypot(p2[2][0] - p2[1][0], p2[2][1] - p2[1][1])
                w_off = max(24.0, min(w_edge_len * 0.18, 90.0))
                h_off = max(24.0, min(h_edge_len * 0.18, 90.0))

                _draw_edge_dim(
                    p2[0], p2[1], w_off, (0.0, -1.0), f"W: {w_str}",
                    (self._inp_mode == 'WIDTH'), (self._lock_w is not None),
                )
                _draw_edge_dim(
                    p2[1], p2[2], h_off, (1.0, 0.0), f"H: {h_str}",
                    (self._inp_mode == 'HEIGHT'), (self._lock_h is not None),
                )

                # Diagonal distance readout - the corner-to-corner drag
                # is effectively a line being drawn, so it gets the
                # same floating dimension label as CAD_OT_DrawLine's
                # segment length (and CAD_OT_DrawCircle's radius),
                # rather than only the decomposed W/H values.
                diag_val = math.hypot(W_val, H_val)
                if diag_val > 0.05:
                    dx_s   = p2[2][0] - p2[0][0]
                    dy_s   = p2[2][1] - p2[0][1]
                    dist_s = math.hypot(dx_s, dy_s)
                    if dist_s > 1:
                        perp_x = -dy_s / dist_s * 26
                        perp_y =  dx_s / dist_s * 26
                    else:
                        perp_x, perp_y = 0, 26
                    dmx = (p2[0][0] + p2[2][0]) / 2 + perp_x
                    dmy = (p2[0][1] + p2[2][1]) / 2 + perp_y
                    draw_float_dim(
                        dmx, dmy, format_length(context, diag_val),
                        active=False, locked=False,
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
            f"RECT  [{'W' if self._inp_mode == 'WIDTH' else 'H'} field]"
            f"   X {self._cur.x:.3f}   Y {self._cur.y:.3f}",
            10, 55, 12, (1.0, 0.85, 0.2, 1.0),
        )
