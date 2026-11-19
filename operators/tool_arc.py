# ─────────────────────────────────────────────────────────────────────
#  operators/tool_arc.py
#  CAD_OT_DrawArc  –  two sub-methods, switchable before the first
#  point is placed (Alt+Scroll or 'M'):
#    3-Point:            click start -> click end -> bulge through a
#                         third point, click to finish.
#    Center-Start-End:   click centre -> click start -> move to sweep
#                         the end point (locked radius), click to
#                         finish.
#  Same snapping/OTrack/dynamic-input plumbing as the other draw tools.
# ─────────────────────────────────────────────────────────────────────

import math
import time
import bpy
from mathutils import Vector

from ..utils.math_utils  import build_snap_cache, apply_snaps, mouse_to_3d, world_to_screen, parse_typed_length, format_length, format_angle
from ..utils.otrack import SnapAcquisitionManager, update_tracking, resolve_typed_distance_ray
from ..utils.cad_objects import create_cad_object, pick_reference_object
from .. import state as _st
from ..utils.cursor_util import (
    update_modal_cursor, restore_modal_cursor, cursor_fx_visible, init_modal_cursor,
    guarded_modal,
    handle_ui_handoff,
    reset_tpanel_to_select,
)
from ..gfx.primitives    import (
    gfx_dot, gfx_text, gfx_lines, gfx_dashed_line, gfx_circle,
    gfx_rect_fill, gfx_rounded_rect_fill, gfx_rounded_rect_loop,
)
from ..gfx.markers       import gfx_snap_marker, draw_crosshair, draw_vertex_square, draw_selected_snap_points, draw_osnap_tracking
from ..gfx.dims          import draw_float_dim, draw_dim_rows

_METHODS = ('3POINT', 'CSE', 'SCE')
_METHOD_LABELS = {
    '3POINT': '3-Point Arc',
    'CSE':    'Center-Start-End Arc',
    'SCE':    'Start-Center-End Arc',
}
_PROMPTS = {
    '3POINT': {'A': "Click start point", 'B': "Click end point",
               'C': "Move to bulge the arc, click to finish"},
    'CSE':    {'A': "Click centre", 'B': "Click start point",
               'C': "Move to sweep the end point, click to finish"},
    'SCE':    {'A': "Click start point", 'B': "Click center point",
               'C': "Move to sweep the end point, click to finish"},
}


def _circumcircle(p1, p2, p3):
    """Centre/radius of the circle through 3 points, or None if
    (near-)collinear."""
    ax, ay = p1.x, p1.y
    bx, by = p2.x, p2.y
    cx, cy = p3.x, p3.y
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay)
          + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx)
          + (cx * cx + cy * cy) * (bx - ax)) / d
    center = Vector((ux, uy, 0.0))
    radius = (center - p1).length
    return center, radius


def _arc_points(p1, p2, p3, segs=48):
    """Polyline from p1 to p2, sweeping through p3, on the circle
    defined by the 3 points. Falls back to a straight line if the
    points are (near-)collinear."""
    circ = _circumcircle(p1, p2, p3)
    if circ is None:
        return [p1, p2]
    center, radius = circ
    a1 = math.atan2(p1.y - center.y, p1.x - center.x)
    a2 = math.atan2(p2.y - center.y, p2.x - center.x)
    a3 = math.atan2(p3.y - center.y, p3.x - center.x)

    two_pi = 2 * math.pi
    ccw_sweep = (a2 - a1) % two_pi
    a3_rel    = (a3 - a1) % two_pi
    sweep = ccw_sweep if a3_rel <= ccw_sweep else ccw_sweep - two_pi

    pts = []
    for i in range(segs + 1):
        t = a1 + sweep * (i / segs)
        pts.append(Vector((center.x + radius * math.cos(t),
                            center.y + radius * math.sin(t), 0.0)))
    return pts


def _arc_points_cse(center, start, end_angle_pt, segs=48):
    """Polyline for Center-Start-End: fixed centre/radius (from
    centre->start), swept CCW from the start angle to the angle of
    end_angle_pt."""
    radius = (start - center).length
    a1 = math.atan2(start.y - center.y, start.x - center.x)
    a2 = math.atan2(end_angle_pt.y - center.y, end_angle_pt.x - center.x)
    sweep = (a2 - a1) % (2 * math.pi)
    pts = []
    for i in range(segs + 1):
        t = a1 + sweep * (i / segs)
        pts.append(Vector((center.x + radius * math.cos(t),
                            center.y + radius * math.sin(t), 0.0)))
    return pts


class CAD_OT_DrawArc(bpy.types.Operator):
    bl_idname  = "cad.draw_arc"
    bl_label   = "CAD Arc"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._context = context   # kept for helpers that don't take context directly (unit-aware parsing, etc.)
        self._method = _st.arc_method if _st.arc_method in _METHODS else '3POINT'

        self._pt_a      = None   # start (3-point) / centre (CSE)
        self._pt_b      = None   # end (3-point) / start (CSE)
        self._raw       = Vector((0, 0, 0))
        self._cur       = Vector((0, 0, 0))
        self._snap_type = 'FREE'
        self._inp       = ''
        self._typed_ray_frozen = None

        self._base_candidate_key   = None
        self._base_candidate_since = 0.0
        self._acquired_base        = None

        self._phase = 'A'   # 'A' -> 'B' -> 'C'
        self._alt_held = False

        self._mx, self._my = event.mouse_region_x, event.mouse_region_y
        self._snap_cache = build_snap_cache(context)
        self._ref_obj    = _st.sketch_ref_obj or pick_reference_object(context)
        self._otrack     = SnapAcquisitionManager()
        self._otrack.reset(clear_points=True)

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "ARC"
        context.window_manager.modal_handler_add(self)
        # A held Alt key with the mouse perfectly still doesn't
        # generate any further events on its own, so nothing would
        # re-run modal() to notice it - this timer forces periodic
        # polling of event.alt so the dropdown menu still opens/closes
        # reliably even with no mouse movement.
        self._timer = context.window_manager.event_timer_add(0.05, window=context.window)
        self._update_header(context)
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        try:
            bpy.types.SpaceView3D.draw_handler_remove(self._handler, 'WINDOW')
        except ValueError:
            pass
        try:
            context.window_manager.event_timer_remove(self._timer)
        except (ValueError, AttributeError):
            pass
        restore_modal_cursor(self, context)
        context.area.header_text_set(None)
        context.scene.cad_settings.active_tool = "SELECT"
        self._otrack.reset(clear_points=True)
        reset_tpanel_to_select(self, context)
        context.area.tag_redraw()

    def _update_header(self, context):
        label  = _METHOD_LABELS[self._method]
        prompt = _PROMPTS[self._method][self._phase]
        if self._phase == 'A':
            context.area.header_text_set(
                f"ARC ({label}): {prompt}  |  Alt=method menu, Scroll/M=switch  |  Esc=cancel"
            )
        else:
            context.area.header_text_set(f"ARC ({label}): {prompt}  |  Esc=cancel")

    # ── Alt dropdown menu (same chrome as the Select Box Tool's OSNAP
    #    drop-down: dark rounded panel, header row, blue highlight on
    #    the active row) ────────────────────────────────────────────────
    _MENU_W       = 210
    _MENU_ROW_H   = 32
    _MENU_HEADER_H = 28
    _MENU_OFFSET_X = 18
    _MENU_OFFSET_Y = 18
    _MENU_RADIUS   = 6
    _MENU_BG          = (0.110, 0.110, 0.110, 0.97)
    _MENU_HEADER_BG   = (0.145, 0.145, 0.145, 0.97)
    _MENU_BG_ACTIVE   = (0.239, 0.435, 0.769, 1.00)
    _MENU_BG_HOVER    = (0.200, 0.200, 0.200, 0.90)
    _MENU_BORDER      = (0.172, 0.172, 0.172, 1.00)
    _MENU_SEP         = (0.220, 0.220, 0.220, 1.00)
    _MENU_HEADER_COL  = (0.620, 0.620, 0.620, 1.00)
    _MENU_TEXT_IDLE   = (0.850, 0.850, 0.850, 1.00)
    _MENU_TEXT_ACT    = (1.000, 1.000, 1.000, 1.00)

    # ── Small current-method indicator box, shown beside the cursor
    #    whenever Alt isn't held. Same recipe as the Select Box Tool's
    #    collapsed snap-mode chip: 28px rounded square, radius 6,
    #    18px cursor offset, single border, icon centred inside.
    _INDICATOR_SIZE    = 28
    _INDICATOR_OFFSET  = 18
    _INDICATOR_RADIUS  = 6
    _INDICATOR_BG      = (0.110, 0.110, 0.110, 0.97)
    _INDICATOR_BORDER  = (0.239, 0.435, 0.769, 1.00)
    _INDICATOR_GLYPH   = (1.000, 1.000, 1.000, 1.00)

    def _indicator_rect(self, context):
        s = self._INDICATOR_SIZE
        o = self._INDICATOR_OFFSET
        # Opposite corner from the dynamic-input boxes (which sit to
        # the right/upper-right of the cursor), so the two never
        # overlap.
        bx = self._mx - o - s
        by = self._my - o - s
        region = context.region
        if region:
            if bx < 4:
                bx = self._mx + o
            if by < 4:
                by = self._my + o
        return bx, by, s, s

    def _draw_method_indicator(self, context):
        x0, y0, s, _ = self._indicator_rect(context)
        r = self._INDICATOR_RADIUS
        gfx_rounded_rect_fill(x0, y0, s, s, r, self._INDICATOR_BG)
        gfx_rounded_rect_loop(x0, y0, s, s, r, self._INDICATOR_BORDER, 1.6)

        cx, cy = x0 + s / 2, y0 + s / 2
        col = self._INDICATOR_GLYPH
        if self._method == '3POINT':
            pts = [(cx - 7, cy - 6), (cx - 2.5, cy + 1), (cx + 2.5, cy + 3.5), (cx + 7, cy + 6)]
            gfx_lines(pts, col, 1.8)
            gfx_dot((cx - 7, cy - 6), col, 3.5)
            gfx_dot((cx + 2.5, cy + 3.5), col, 3.5)
            gfx_dot((cx + 7, cy + 6), col, 3.5)
        elif self._method == 'CSE':
            gfx_circle((cx, cy), 7, col, segs=24, w=1.6)
            gfx_dot((cx, cy), col, 4.5)
            gfx_dot((cx + 7, cy), col, 3.5)
        else:   # SCE
            gfx_circle((cx, cy), 7, col, segs=24, w=1.6)
            gfx_dot((cx - 7, cy), col, 4.5)
            gfx_dot((cx, cy), col, 3.5)

    def _menu_rect(self, context):
        box_w = self._MENU_W
        box_h = self._MENU_HEADER_H + len(_METHODS) * self._MENU_ROW_H
        bx = self._mx + self._MENU_OFFSET_X
        by = self._my + self._MENU_OFFSET_Y
        region = context.region
        if region:
            if bx + box_w > region.width - 4:
                bx = self._mx - self._MENU_OFFSET_X - box_w
            if by + box_h > region.height - 4:
                by = self._my - self._MENU_OFFSET_Y - box_h
        return bx, by, box_w, box_h

    def _menu_hit(self, context, mx, my):
        bx, by, box_w, box_h = self._menu_rect(context)
        header_y = by + box_h - self._MENU_HEADER_H
        if header_y <= my < by + box_h:
            return None
        if not (bx <= mx < bx + box_w):
            return None
        for ri, m in enumerate(_METHODS):
            ry = by + (len(_METHODS) - 1 - ri) * self._MENU_ROW_H
            if ry <= my < ry + self._MENU_ROW_H:
                return m
        return None

    def _draw_arc_menu(self, context):
        bx, by, box_w, box_h = self._menu_rect(context)
        gfx_rounded_rect_fill(bx, by, box_w, box_h, self._MENU_RADIUS, self._MENU_BG)
        gfx_rounded_rect_loop(bx, by, box_w, box_h, self._MENU_RADIUS, self._MENU_BORDER, 1.0)

        header_y = by + box_h - self._MENU_HEADER_H
        gfx_rect_fill(bx + 1, header_y, box_w - 2, self._MENU_HEADER_H - 1, self._MENU_HEADER_BG)
        gfx_text("Arc Method", bx + 10, header_y + (self._MENU_HEADER_H - 10) // 2, 10, self._MENU_HEADER_COL)
        sep_y = header_y - 1
        gfx_lines([(bx + 1, sep_y), (bx + box_w - 1, sep_y)], self._MENU_SEP, 1.0)

        for ri, m in enumerate(_METHODS):
            ry = by + (len(_METHODS) - 1 - ri) * self._MENU_ROW_H
            is_active = (m == self._method)
            is_hover  = (self._menu_hit(context, self._mx, self._my) == m) and not is_active
            if is_active:
                gfx_rect_fill(bx + 1, ry + 1, box_w - 2, self._MENU_ROW_H - 2, self._MENU_BG_ACTIVE)
            elif is_hover:
                gfx_rect_fill(bx + 1, ry + 1, box_w - 2, self._MENU_ROW_H - 2, self._MENU_BG_HOVER)
            text_col = self._MENU_TEXT_ACT if is_active else self._MENU_TEXT_IDLE
            label = ("\u2713 " if is_active else "  ") + _METHOD_LABELS[m]
            label_y = ry + (self._MENU_ROW_H - 12) // 2
            gfx_text(label, bx + 12, label_y, 12, text_col)

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

        if event.type == 'TIMER':
            return {'RUNNING_MODAL'}

        self._mx, self._my = event.mouse_region_x, event.mouse_region_y
        self._alt_held = bool(event.alt) and self._phase == 'A'

        if event.type in {'LEFT_ALT', 'RIGHT_ALT'}:
            self._alt_held = (event.value == 'PRESS') and self._phase == 'A'
            return {'RUNNING_MODAL'}

        if event.type == 'MIDDLEMOUSE':
            return {'PASS_THROUGH'}
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN'} and not (event.type != 'TRACKPADPAN' and self._phase == 'A' and event.alt):
            return {'PASS_THROUGH'}

        # ── Alt dropdown menu - only before the first point is placed ──
        if self._phase == 'A':
            if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} and event.value == 'PRESS' and event.alt:
                idx = _METHODS.index(self._method)
                step = -1 if event.type == 'WHEELUPMOUSE' else 1
                self._method = _METHODS[(idx + step) % len(_METHODS)]
                _st.arc_method = self._method
                self._update_header(context)
                return {'RUNNING_MODAL'}

            if event.type == 'M' and event.value == 'PRESS':
                idx = _METHODS.index(self._method)
                self._method = _METHODS[(idx + 1) % len(_METHODS)]
                _st.arc_method = self._method
                self._update_header(context)
                return {'RUNNING_MODAL'}

            if event.type == 'LEFTMOUSE' and event.value == 'PRESS' and self._alt_held:
                hit = self._menu_hit(context, self._mx, self._my)
                if hit is not None:
                    self._method = hit
                    _st.arc_method = self._method
                self._alt_held = False
                self._update_header(context)
                return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            raw = mouse_to_3d(context, self._mx, self._my)
            if raw:
                self._raw, self._snap_type = apply_snaps(
                    context, raw, self._snap_cache,
                    snap_filter=_st.selection.get_active_stypes(),
                )
                self._update_cur()
                update_tracking(self._otrack, self._raw, self._snap_type)
                if self._phase == 'A':
                    self._update_hover_base_point(context, self._snap_type)
            return {'RUNNING_MODAL'}

        if event.value != 'PRESS':
            return {'RUNNING_MODAL'}

        is_locked_radius_angle = (self._method in ('CSE', 'SCE') and self._phase == 'C')
        ch = event.ascii
        if ch and ch in '0123456789.':
            if not self._inp and not is_locked_radius_angle:
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

        if event.type == 'LEFTMOUSE' or event.type in {'RET', 'NUMPAD_ENTER', 'SPACE'}:
            if self._phase == 'A':
                self._pt_a = self._cur.copy()
                self._phase = 'B'
                self._otrack.reset(clear_points=True)
                self._acquired_base = None
                self._base_candidate_key = None
                self._inp = ''
                self._typed_ray_frozen = None
                self._update_header(context)
            elif self._phase == 'B':
                self._pt_b = self._cur.copy()
                self._phase = 'C'
                self._otrack.reset(clear_points=True)
                self._inp = ''
                self._typed_ray_frozen = None
                self._update_header(context)
            else:  # C
                self._finish(context)
                self.cancel(context)
                return {'FINISHED'}
            return {'RUNNING_MODAL'}

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
        origin = None
        if self._phase == 'A' and self._acquired_base is not None:
            origin = self._acquired_base
        elif self._phase == 'B' and self._pt_a is not None:
            origin = self._pt_a
        elif self._phase == 'C' and self._pt_b is not None:
            origin = self._pt_b
        if origin is not None:
            direction = self._raw - origin
            if direction.length < 1e-6:
                direction = Vector((1, 0, 0))
            return origin, direction.normalized()
        return None

    def _center_and_start(self):
        """(center, start) points for whichever locked-radius method
        is active - CSE places centre first, SCE places start first."""
        if self._method == 'CSE':
            return self._pt_a, self._pt_b
        if self._method == 'SCE':
            return self._pt_b, self._pt_a
        return None, None

    def _update_cur(self):
        if self._method in ('CSE', 'SCE') and self._phase == 'C':
            center, start = self._center_and_start()
            if center is not None and start is not None:
                radius = (start - center).length
                if self._inp:
                    try:
                        ang_deg = float(self._inp)
                        start_ang = math.atan2(start.y - center.y, start.x - center.x)
                        ang = start_ang + math.radians(ang_deg)
                        self._cur = center + Vector((math.cos(ang), math.sin(ang), 0.0)) * radius
                        return
                    except ValueError:
                        pass
                d = self._raw - center
                if d.length < 1e-6:
                    d = Vector((1, 0, 0))
                self._cur = center + d.normalized() * radius
                return

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
        if self._method in ('CSE', 'SCE'):
            center, start = self._center_and_start()
            pts = _arc_points_cse(center, start, self._cur, segs=48)
            meta = {"method": self._method, "center": [center.x, center.y, 0],
                    "start": [start.x, start.y, 0],
                    "end": [self._cur.x, self._cur.y, 0], "segments": 48}
        else:
            p3 = self._cur.copy()
            pts = _arc_points(self._pt_a, self._pt_b, p3, segs=48)
            meta = {"method": "3POINT", "p1": [self._pt_a.x, self._pt_a.y, 0],
                    "p2": [self._pt_b.x, self._pt_b.y, 0],
                    "p3": [p3.x, p3.y, 0], "segments": 48}
        verts = [(p.x, p.y, 0.0) for p in pts]
        edges = [(i, i + 1) for i in range(len(pts) - 1)]
        create_cad_object(context, "CAD_Arc", verts, edges, "arc", meta, reference_obj=self._ref_obj)

    def _draw(self, context):
        # Everything that stands in for "the cursor" (OTRACK marker,
        # snap marker, coordinate readout, crosshair) must only be
        # drawn while the real OS cursor is actually hidden, i.e. the
        # mouse is over the usable viewport - see cursor_fx_visible().
        show_cursor_fx = cursor_fx_visible(self)

        draw_selected_snap_points(context)
        if show_cursor_fx:
            draw_osnap_tracking(context, self._mx, self._my)
        mx, my = self._mx, self._my
        c2 = world_to_screen(context, self._cur)

        if show_cursor_fx and c2:
            gfx_snap_marker(c2, self._snap_type)

        pa_2d = world_to_screen(context, self._pt_a) if self._pt_a is not None else None
        pb_2d = world_to_screen(context, self._pt_b) if self._pt_b is not None else None
        if pa_2d:
            draw_vertex_square(pa_2d, 5, fill_col=(1, 0, 1, 0.18), outline_col=(1, 0, 1, 1), outline_w=1.8)
        if pb_2d:
            draw_vertex_square(pb_2d, 5, fill_col=(1, 0, 1, 0.18), outline_col=(1, 0, 1, 1), outline_w=1.8)

        if self._method in ('CSE', 'SCE'):
            if self._phase == 'B' and pa_2d and c2:
                gfx_dashed_line(pa_2d, c2, col=(1, 1, 0, 0.85), dash_len=6, space_len=4, w=2.0)
            elif self._phase == 'C' and self._pt_a is not None and self._pt_b is not None:
                center, start = self._center_and_start()
                center_2d = world_to_screen(context, center)
                pts = _arc_points_cse(center, start, self._cur, segs=48)
                pts_2d = [world_to_screen(context, p) for p in pts]
                for i in range(len(pts_2d) - 1):
                    if pts_2d[i] and pts_2d[i + 1]:
                        gfx_dashed_line(pts_2d[i], pts_2d[i + 1], col=(1, 1, 0, 0.9), dash_len=6, space_len=4, w=2.4)
                if center_2d and c2:
                    gfx_dashed_line(center_2d, c2, col=(0.6, 0.6, 0.6, 0.6), dash_len=5, space_len=4, w=1.4)
        else:  # 3POINT
            if self._phase == 'C' and self._pt_a is not None and self._pt_b is not None:
                pts = _arc_points(self._pt_a, self._pt_b, self._cur, segs=48)
                pts_2d = [world_to_screen(context, p) for p in pts]
                pts_2d = [p for p in pts_2d if p is not None]
                if len(pts_2d) > 1:
                    gfx_lines(pts_2d, (1, 1, 0, 0.9), 2.8)
            elif self._phase == 'B' and pa_2d and c2:
                gfx_lines([pa_2d, c2], (1, 1, 0, 0.6), 1.6)

        # Alt dropdown menu (only before the first point), or the small
        # current-method indicator the rest of the time
        if self._phase == 'A' and self._alt_held:
            self._draw_arc_menu(context)
        else:
            self._draw_method_indicator(context)

        if show_cursor_fx and self._phase == 'A' and not self._alt_held:
            if self._inp:
                draw_dim_rows(context, mx, my, [("Dist:", self._inp + "▌", True, False)])
            else:
                draw_dim_rows(context, mx, my,
                              [("X:", f"{self._cur.x:.4f}", True, False),
                               ("Y:", f"{self._cur.y:.4f}", False, False)])
        elif self._method in ('CSE', 'SCE') and self._phase == 'C':
            center, start = self._center_and_start()
            start_ang = math.atan2(start.y - center.y, start.x - center.x)
            cur_ang   = math.atan2(self._cur.y - center.y, self._cur.x - center.x)
            sweep_ang = (cur_ang - start_ang) % (2 * math.pi)
            _deg_unit = context.scene.unit_settings.system_rotation != 'RADIANS'
            disp = self._inp + ("°▌" if _deg_unit else " rad▌") if self._inp else format_angle(context, sweep_ang)
            if c2:
                draw_float_dim(c2[0] + 50, c2[1], disp, active=True, locked=bool(self._inp))
        else:
            origin = self._pt_a if self._phase == 'B' else self._pt_b
            disp = self._inp + "▌" if self._inp else format_length(context, (self._cur - origin).length)
            if c2:
                draw_float_dim(c2[0] + 50, c2[1], disp, active=True, locked=bool(self._inp))

        if show_cursor_fx:
            cx, cy = c2 if c2 else (mx, my)
            draw_crosshair(context, cx, cy, show_box=False)
        gfx_text(f"ARC [{_METHOD_LABELS[self._method]} - {self._phase}]", 10, 55, 12, (1.0, 0.85, 0.2, 1.0))
