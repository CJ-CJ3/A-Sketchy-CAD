# ─────────────────────────────────────────────────────────────────────
#  operators/tool_spline.py
#  CAD_OT_DrawSpline  –  two sub-methods, switchable before the first
#  point is placed (Alt menu, same chrome as the Arc Tool's):
#    Fit Points:         curve passes exactly through every click.
#    Control Vertices:   curve is shaped by the clicks without
#                         necessarily touching them (uniform cubic
#                         B-spline), drawn dashed while in progress.
#  Same snapping/axis-lock/OTrack plumbing as the Line tool.
# ─────────────────────────────────────────────────────────────────────

import time
import bpy
from mathutils import Vector

from ..utils.math_utils import (
    build_snap_cache, apply_snaps, apply_axis_lock, apply_polar_tracking,
    mouse_to_3d, world_to_screen, parse_typed_length, format_length,
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
from ..gfx.primitives import (
    gfx_dot, gfx_text, gfx_lines, gfx_dashed_line, gfx_circle,
    gfx_rect_fill, gfx_rounded_rect_fill, gfx_rounded_rect_loop,
)
from ..gfx.markers    import (
    gfx_snap_marker, draw_crosshair, draw_vertex_square,
    draw_selected_snap_points, draw_osnap_tracking,
)
from ..gfx.preview    import draw_direction_guide
from ..gfx.dims       import draw_float_dim, draw_dim_rows

_METHODS = ('FIT', 'CV')
_METHOD_LABELS = {'FIT': 'Fit Points', 'CV': 'Control Vertices'}


_MAX_DEGREE = 10


def _degree_to_tangent_scale(degree):
    """Degree 1 -> 0 tangent (straight segments); each step up bulges
    the curve further, so higher degree visibly means more curve."""
    degree = max(1, min(degree, _MAX_DEGREE))
    return (degree - 1) * 0.4


def _hermite_chain(points, tangent_scale, samples_per_seg=12):
    """Catmull-Rom-style Hermite spline through `points`, always
    interpolating every point in the list exactly. Tangent vectors are
    scaled by tangent_scale - 0 gives straight segments, larger values
    bulge the curve more between points."""
    n = len(points)
    if n < 2:
        return list(points)
    if n == 2 or tangent_scale <= 1e-6:
        out = [points[0]]
        for i in range(n - 1):
            p0, p1 = points[i], points[i + 1]
            for s in range(1, samples_per_seg + 1):
                out.append(p0.lerp(p1, s / samples_per_seg))
        return out

    pts = [points[0]] + list(points) + [points[-1]]
    out = [points[0]]
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        m1x = tangent_scale * (p2.x - p0.x) / 2
        m1y = tangent_scale * (p2.y - p0.y) / 2
        m2x = tangent_scale * (p3.x - p1.x) / 2
        m2y = tangent_scale * (p3.y - p1.y) / 2
        for s in range(1, samples_per_seg + 1):
            t = s / samples_per_seg
            t2, t3 = t * t, t * t * t
            h00 = 2 * t3 - 3 * t2 + 1
            h10 = t3 - 2 * t2 + t
            h01 = -2 * t3 + 3 * t2
            h11 = t3 - t2
            x = h00 * p1.x + h10 * m1x + h01 * p2.x + h11 * m2x
            y = h00 * p1.y + h10 * m1y + h01 * p2.y + h11 * m2y
            out.append(Vector((x, y, 0.0)))
    return out


def _fit_points_curve(points, degree, samples_per_seg=12):
    """Curve that always passes exactly through every fit point;
    higher degree bulges the curve more between them."""
    return _hermite_chain(points, _degree_to_tangent_scale(degree), samples_per_seg)


def _bspline_points(points, degree, samples_per_seg=12):
    """Curve shaped by the control points without generally passing
    through interior ones - runs the same degree-scaled bulge through
    the midpoints of consecutive control vertices, anchored at the
    first/last vertex."""
    n = len(points)
    if n < 2:
        return list(points)
    if n == 2:
        return [points[0], points[1]]
    mids = [points[i].lerp(points[i + 1], 0.5) for i in range(n - 1)]
    anchored = [points[0]] + mids + [points[-1]]
    return _hermite_chain(anchored, _degree_to_tangent_scale(degree), samples_per_seg)


def _curve_points(method, points, degree, samples_per_seg=12):
    return (_bspline_points(points, degree, samples_per_seg) if method == 'CV'
            else _fit_points_curve(points, degree, samples_per_seg))


def _point_seg_dist_2d(p, a, b):
    """Screen-space distance from point p to segment a-b."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-9:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


class CAD_OT_DrawSpline(bpy.types.Operator):
    bl_idname  = "cad.draw_spline"
    bl_label   = "CAD Spline"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._context = context   # kept for helpers that don't take context directly (unit-aware parsing, etc.)
        self._method = _st.spline_method if _st.spline_method in _METHODS else 'FIT'

        self._pts       = []
        self._raw       = Vector((0, 0, 0))
        self._cur       = Vector((0, 0, 0))
        self._snap_type = 'FREE'
        self._inp       = ''
        self._typed_ray_frozen = None
        self._axis        = None
        self._polar_angle = None
        self._alt_held    = False
        self._degree      = 3
        self._shift_held  = False
        self._deg_inp     = ''

        self._base_candidate_key   = None
        self._base_candidate_since = 0.0
        self._acquired_base        = None

        # Point editing (grab / delete / insert / undo-last)
        self._hover_idx = None   # index of self._pts currently moused-over
        self._drag_idx  = None   # index currently being grab-repositioned
        self._drag_orig = None   # position to restore if the grab is cancelled

        self._mx, self._my = event.mouse_region_x, event.mouse_region_y
        self._snap_cache = build_snap_cache(context)
        self._ref_obj    = _st.sketch_ref_obj or pick_reference_object(context)
        self._otrack     = SnapAcquisitionManager()
        self._otrack.reset(clear_points=True)

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "SPLINE"
        context.window_manager.modal_handler_add(self)
        self._timer = context.window_manager.event_timer_add(0.05, window=context.window)
        self._hdr(context)
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

    def _hdr(self, context):
        lock  = f"  [{self._axis} LOCKED]" if self._axis else ""
        label = _METHOD_LABELS[self._method]
        if not self._pts:
            alt_hint = "  |  Alt=method menu, Scroll/M=switch"
            deg_hint = "  Shift=degree"
            edit_hint = ""
        else:
            alt_hint = ""
            deg_hint = ""
            edit_hint = "  |  G=grab pt  Del=delete pt  Ctrl+Click=insert  Backspace=undo last"
        context.area.header_text_set(
            f"SPLINE - {label} deg {self._degree} ({len(self._pts)} pts){lock}  |  "
            f"Click=add point  Enter=finish  X/Y=axis{deg_hint}{alt_hint}{edit_hint}  Esc=cancel"
        )

    # ── Alt dropdown menu / indicator box (same chrome as Arc Tool) ────
    _MENU_W        = 210
    _MENU_ROW_H    = 32
    _MENU_HEADER_H = 28
    _MENU_OFFSET_X = 18
    _MENU_OFFSET_Y = 18
    _MENU_RADIUS   = 6
    _MENU_BG         = (0.110, 0.110, 0.110, 0.97)
    _MENU_HEADER_BG  = (0.145, 0.145, 0.145, 0.97)
    _MENU_BG_ACTIVE  = (0.239, 0.435, 0.769, 1.00)
    _MENU_BG_HOVER   = (0.200, 0.200, 0.200, 0.90)
    _MENU_BORDER     = (0.172, 0.172, 0.172, 1.00)
    _MENU_SEP        = (0.220, 0.220, 0.220, 1.00)
    _MENU_HEADER_COL = (0.620, 0.620, 0.620, 1.00)
    _MENU_TEXT_IDLE  = (0.850, 0.850, 0.850, 1.00)
    _MENU_TEXT_ACT   = (1.000, 1.000, 1.000, 1.00)

    _INDICATOR_SIZE   = 28
    _INDICATOR_OFFSET = 18
    _INDICATOR_RADIUS = 6
    _INDICATOR_BG     = (0.110, 0.110, 0.110, 0.97)
    _INDICATOR_BORDER = (0.239, 0.435, 0.769, 1.00)
    _INDICATOR_GLYPH  = (1.000, 1.000, 1.000, 1.00)

    def _indicator_rect(self, context):
        s = self._INDICATOR_SIZE
        o = self._INDICATOR_OFFSET
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
        if self._method == 'FIT':
            pts = [(cx - 7, cy - 6), (cx - 2.5, cy + 1), (cx + 2.5, cy + 3.5), (cx + 7, cy + 6)]
            gfx_lines(pts, col, 1.8)
            gfx_dot((cx - 7, cy - 6), col, 3.5)
            gfx_dot((cx - 2.5, cy + 1), col, 3.5)
            gfx_dot((cx + 2.5, cy + 3.5), col, 3.5)
            gfx_dot((cx + 7, cy + 6), col, 3.5)
        else:   # CV - control polygon (dashed) with square handles off the curve
            gfx_dashed_line((cx - 7, cy - 6), (cx, cy + 6), col=col, dash_len=3, space_len=2, w=1.4)
            gfx_dashed_line((cx, cy + 6), (cx + 7, cy - 4), col=col, dash_len=3, space_len=2, w=1.4)
            for px, py in ((cx - 7, cy - 6), (cx, cy + 6), (cx + 7, cy - 4)):
                draw_vertex_square((px, py), 3, fill_col=None, outline_col=col, outline_w=1.4)

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

    def _draw_spline_menu(self, context):
        bx, by, box_w, box_h = self._menu_rect(context)
        gfx_rounded_rect_fill(bx, by, box_w, box_h, self._MENU_RADIUS, self._MENU_BG)
        gfx_rounded_rect_loop(bx, by, box_w, box_h, self._MENU_RADIUS, self._MENU_BORDER, 1.0)

        header_y = by + box_h - self._MENU_HEADER_H
        gfx_rect_fill(bx + 1, header_y, box_w - 2, self._MENU_HEADER_H - 1, self._MENU_HEADER_BG)
        gfx_text("Spline Method", bx + 10, header_y + (self._MENU_HEADER_H - 10) // 2, 10, self._MENU_HEADER_COL)
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

    _DIGIT_KEYS = {
        'ONE': '1', 'TWO': '2', 'THREE': '3', 'FOUR': '4', 'FIVE': '5',
        'SIX': '6', 'SEVEN': '7', 'EIGHT': '8', 'NINE': '9', 'ZERO': '0',
        'NUMPAD_1': '1', 'NUMPAD_2': '2', 'NUMPAD_3': '3', 'NUMPAD_4': '4',
        'NUMPAD_5': '5', 'NUMPAD_6': '6', 'NUMPAD_7': '7', 'NUMPAD_8': '8',
        'NUMPAD_9': '9', 'NUMPAD_0': '0',
    }

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
        self._alt_held = bool(event.alt) and not self._pts

        if event.type in {'LEFT_ALT', 'RIGHT_ALT'}:
            self._alt_held = (event.value == 'PRESS') and not self._pts
            return {'RUNNING_MODAL'}

        if event.type in {'LEFT_SHIFT', 'RIGHT_SHIFT'}:
            # Degree can only be changed before the spline is started -
            # once the first point is placed the degree is locked in.
            if self._pts:
                return {'RUNNING_MODAL'}
            # Tap Shift to toggle degree-entry mode on. Once open, the
            # box stays up (no need to keep Shift held) until Enter
            # confirms or Esc cancels it.
            if event.value == 'PRESS' and not self._shift_held:
                self._shift_held = True
                self._deg_inp = ''
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'MIDDLEMOUSE':
            return {'PASS_THROUGH'}
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN'} and not (event.type != 'TRACKPADPAN' and not self._pts and event.alt):
            return {'PASS_THROUGH'}

        # ── Method switching - only before the first point is placed ──
        if not self._pts:
            if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} and event.value == 'PRESS' and event.alt:
                idx = _METHODS.index(self._method)
                step = -1 if event.type == 'WHEELUPMOUSE' else 1
                self._method = _METHODS[(idx + step) % len(_METHODS)]
                _st.spline_method = self._method
                self._hdr(context)
                return {'RUNNING_MODAL'}

            if event.type == 'M' and event.value == 'PRESS':
                idx = _METHODS.index(self._method)
                self._method = _METHODS[(idx + 1) % len(_METHODS)]
                _st.spline_method = self._method
                self._hdr(context)
                return {'RUNNING_MODAL'}

            if event.type == 'LEFTMOUSE' and event.value == 'PRESS' and self._alt_held:
                hit = self._menu_hit(context, self._mx, self._my)
                if hit is not None:
                    self._method = hit
                    _st.spline_method = self._method
                self._alt_held = False
                self._hdr(context)
                return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            self._on_move(context)
            return {'RUNNING_MODAL'}

        # ── point editing: grab/drop, delete, insert, undo-last ────────
        # (all disabled while the degree-entry overlay or typed-distance
        # input is active, to avoid ambiguous key handling)
        if self._pts and not self._shift_held and not self._inp:
            if event.type == 'G' and event.value == 'PRESS':
                if self._drag_idx is not None:
                    self._drag_idx = None
                    self._drag_orig = None
                    self._hdr(context)
                elif self._hover_idx is not None:
                    self._drag_idx = self._hover_idx
                    self._drag_orig = self._pts[self._drag_idx].copy()
                    self._hdr(context)
                return {'RUNNING_MODAL'}

            if event.type == 'DEL' and event.value == 'PRESS':
                idx = self._drag_idx if self._drag_idx is not None else self._hover_idx
                if idx is not None and len(self._pts) > 1:
                    self._pts.pop(idx)
                    self._drag_idx = None
                    self._drag_orig = None
                    self._hover_idx = None
                    self._hdr(context)
                return {'RUNNING_MODAL'}

            if event.type == 'BACK_SPACE' and event.value == 'PRESS' and self._drag_idx is None:
                self._pts.pop()
                self._hdr(context)
                return {'RUNNING_MODAL'}

            if (event.type == 'LEFTMOUSE' and event.value == 'PRESS'
                    and event.ctrl and self._drag_idx is None):
                seg_idx = self._hit_test_segment(context)
                if seg_idx is not None:
                    self._pts.insert(seg_idx, self._cur.copy())
                    self._hdr(context)
                return {'RUNNING_MODAL'}

            if event.type == 'LEFTMOUSE' and event.value == 'PRESS' and self._drag_idx is not None:
                # Drop the grabbed point at its current (already live,
                # snapped) position.
                self._drag_idx = None
                self._drag_orig = None
                self._hdr(context)
                return {'RUNNING_MODAL'}

        if self._shift_held and self._pts:
            # Safety net: the spline was started while the degree-entry
            # overlay was still active (e.g. a point placed via typed
            # distance without releasing Shift first). Drop out of
            # degree-edit mode immediately since it's now locked.
            self._shift_held = False
            self._deg_inp = ''

        if self._shift_held:
            if event.value != 'PRESS':
                return {'RUNNING_MODAL'}
            digit = self._DIGIT_KEYS.get(event.type)
            if digit is not None and len(self._deg_inp) < 2:
                self._deg_inp += digit
                return {'RUNNING_MODAL'}
            if event.type == 'BACK_SPACE' and self._deg_inp:
                self._deg_inp = self._deg_inp[:-1]
                return {'RUNNING_MODAL'}
            if event.type == 'ESC':
                # Cancel degree-entry mode without changing the degree.
                self._deg_inp = ''
                self._shift_held = False
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.type in {'RET', 'NUMPAD_ENTER'}:
                if self._deg_inp:
                    try:
                        val = int(self._deg_inp)
                    except ValueError:
                        val = None
                    if val is not None:
                        self._degree = max(1, min(val, _MAX_DEGREE))
                    self._deg_inp = ''
                self._shift_held = False
                self._hdr(context)
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        if event.value != 'PRESS':
            return {'RUNNING_MODAL'}

        ch = event.ascii
        if ch and ch in '0123456789.' and self._drag_idx is None:
            if not self._inp:
                self._typed_ray_frozen = self._typed_distance_ray()
            self._inp += ch
            self._on_move(context)
            return {'RUNNING_MODAL'}
        if event.type == 'BACK_SPACE' and self._inp:
            self._inp = self._inp[:-1]
            if not self._inp:
                self._typed_ray_frozen = None
            self._on_move(context)
            return {'RUNNING_MODAL'}

        if event.type in {'X', 'Y'} and not (event.ctrl or event.shift):
            self._axis = None if self._axis == event.type else event.type
            self._inp = ''
            self._typed_ray_frozen = None
            self._on_move(context)
            self._hdr(context)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE':
            self._commit_and_place(context)
            return {'RUNNING_MODAL'}

        if event.type in {'RET', 'NUMPAD_ENTER', 'SPACE'}:
            if self._inp and not self._pts:
                ray = self._typed_distance_ray()
                if ray is not None:
                    try:
                        distance = parse_typed_length(context, self._inp)
                    except ValueError:
                        distance = None
                    if distance is not None:
                        origin, direction = ray
                        self._pts.append(origin + direction * distance)
                        self._inp = ''
                        self._acquired_base = None
                        self._typed_ray_frozen = None
                        self._otrack.reset(clear_points=True)
                        self._hdr(context)
                return {'RUNNING_MODAL'}
            if self._inp and self._pts:
                self._commit_and_place(context)
                return {'RUNNING_MODAL'}
            if len(self._pts) >= 2:
                self._finish(context)
                self.cancel(context)
                return {'FINISHED'}
            self.cancel(context)
            return {'CANCELLED'}

        if event.type in {'RIGHTMOUSE', 'ESC'}:
            if self._drag_idx is not None:
                # Cancel just the in-progress grab, restoring the
                # point's original position - not the whole tool.
                self._pts[self._drag_idx] = self._drag_orig
                self._drag_idx = None
                self._drag_orig = None
                return {'RUNNING_MODAL'}
            if not self._pts and self._acquired_base is not None:
                self._acquired_base = None
                self._base_candidate_key = None
                self._inp = ''
                self._typed_ray_frozen = None
                return {'RUNNING_MODAL'}
            self.cancel(context)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ── snapping / dynamic input ─────────────────────────────────────

    def _on_move(self, context):
        raw3d = mouse_to_3d(context, self._mx, self._my)
        if raw3d is None:
            return
        self._raw = raw3d
        # While grab-repositioning an existing point, snapping should
        # not treat the *previous* point as a special anchor/axis
        # reference the way normal placement does.
        anchor = self._pts[-1] if (self._pts and self._drag_idx is None) else None
        snapped, stype = apply_snaps(context, raw3d, self._snap_cache,
                                      snap_filter=_st.selection.get_active_stypes(),
                                      anchor=anchor)
        self._snap_type = stype

        if self._axis and self._pts and self._drag_idx is None:
            snapped = apply_axis_lock(snapped, self._pts[-1], self._axis)

        self._polar_angle = None
        if self._pts and not self._axis and self._drag_idx is None:
            try:
                polar = apply_polar_tracking(context, raw3d, self._pts[-1])
                if polar is not None:
                    snapped, self._polar_angle = polar
            except Exception:
                pass

        self._cur = snapped
        update_tracking(self._otrack, self._raw, self._snap_type)

        if self._drag_idx is not None:
            self._pts[self._drag_idx] = snapped.copy()

        if not self._pts:
            self._update_hover_base_point(context, stype)

        if self._inp:
            self._apply_typed()

        # Which existing point (if any) the cursor is currently over -
        # drives grab/delete targeting and the hover highlight.
        self._hover_idx = None
        if self._pts and self._drag_idx is None and not self._inp:
            self._hover_idx = self._hit_test_point(context)

    _POINT_HIT_PX = 12
    _SEG_HIT_PX   = 10

    def _hit_test_point(self, context):
        """Index of the closest existing point within a small pixel
        radius of the cursor, or None."""
        mx, my = self._mx, self._my
        best_i, best_d2 = None, self._POINT_HIT_PX ** 2
        for i, p in enumerate(self._pts):
            p2 = world_to_screen(context, p)
            if not p2:
                continue
            dx, dy = p2[0] - mx, p2[1] - my
            d2 = dx * dx + dy * dy
            if d2 < best_d2:
                best_d2 = d2
                best_i = i
        return best_i

    def _hit_test_segment(self, context):
        """Index to insert a new point *before*, based on which
        control-polygon segment the cursor is closest to (within a
        small pixel radius), or None. Splicing at this index preserves
        point ordering, and since the curve is rebuilt from the full
        point list every frame, inserting a point close to its own
        segment keeps the existing curve shape essentially unchanged
        elsewhere."""
        if len(self._pts) < 2:
            return None
        mx, my = self._mx, self._my
        best_i, best_d = None, self._SEG_HIT_PX
        for i in range(len(self._pts) - 1):
            a2 = world_to_screen(context, self._pts[i])
            b2 = world_to_screen(context, self._pts[i + 1])
            if not a2 or not b2:
                continue
            d = _point_seg_dist_2d((mx, my), a2, b2)
            if d < best_d:
                best_d = d
                best_i = i + 1
        return best_i

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
        origin = self._pts[-1] if self._pts else self._acquired_base
        if origin is not None:
            direction = self._raw - origin
            if direction.length < 1e-6:
                direction = Vector((1, 0, 0))
            return origin, direction.normalized()
        return None

    def _apply_typed(self):
        try:
            val = parse_typed_length(self._context, self._inp)
        except ValueError:
            return
        if self._typed_ray_frozen is not None:
            origin, direction = self._typed_ray_frozen
            self._cur = origin + direction * val

    def _commit_and_place(self, context):
        self._pts.append(self._cur.copy())
        self._polar_angle      = None
        self._inp              = ''
        self._typed_ray_frozen = None
        self._otrack.reset(clear_points=True)
        self._hdr(context)

    def _finish(self, context):
        pts = self._pts
        curve_pts = _curve_points(self._method, pts, self._degree, samples_per_seg=12)
        verts = [(p.x, p.y, 0.0) for p in curve_pts]
        edges = [(i, i + 1) for i in range(len(curve_pts) - 1)]
        create_cad_object(
            context, "CAD_Spline", verts, edges, "spline",
            {"method": self._method, "control_points": [[p.x, p.y, 0] for p in pts]},
            reference_obj=self._ref_obj,
        )

    # ── draw callback ─────────────────────────────────────────────────

    def _draw(self, context):
        try:
            self._draw_impl(context)
        except Exception as e:
            print(f"[CAD] tool_spline draw error: {e}")

    def _draw_impl(self, context):
        # The OTRACK marker / snap marker / typed-value tooltip /
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

        pts_2d_ctrl = []
        for i, p in enumerate(self._pts):
            p2 = world_to_screen(context, p)
            if not p2:
                continue
            pts_2d_ctrl.append(p2)
            if i == self._drag_idx:
                draw_vertex_square(p2, 6, fill_col=(0.2, 1.0, 0.3, 0.35),
                                    outline_col=(0.2, 1.0, 0.3, 1.0), outline_w=2.0)
            elif i == self._hover_idx:
                draw_vertex_square(p2, 5, fill_col=(1.0, 0.6, 0.1, 0.4),
                                    outline_col=(1.0, 1.0, 1.0, 1.0), outline_w=2.0)
            else:
                draw_vertex_square(p2, 4, fill_col=(1, 0.6, 0.1, 0.2), outline_col=(1, 0.6, 0.1, 1), outline_w=1.6)

        # While grab-repositioning a point it's already live inside
        # self._pts (updated in _on_move) - appending self._cur again
        # here would duplicate it and distort the preview.
        preview_pts = self._pts if self._drag_idx is not None else self._pts + [self._cur]
        if len(preview_pts) >= 2:
            curve_pts = _curve_points(self._method, preview_pts, self._degree, samples_per_seg=12)
            pts_2d = [world_to_screen(context, p) for p in curve_pts]
            pts_2d = [p for p in pts_2d if p is not None]
            if len(pts_2d) > 1:
                if self._method == 'CV':
                    ctrl_2d = pts_2d_ctrl if self._drag_idx is not None else pts_2d_ctrl + ([c2] if c2 else [])
                    for i in range(len(ctrl_2d) - 1):
                        gfx_dashed_line(ctrl_2d[i], ctrl_2d[i + 1], col=(0.6, 0.6, 0.6, 0.5), dash_len=4, space_len=3, w=1.2)
                    for i in range(len(pts_2d) - 1):
                        gfx_dashed_line(pts_2d[i], pts_2d[i + 1], col=(1, 1, 0, 0.9), dash_len=6, space_len=4, w=2.2)
                else:
                    gfx_lines(pts_2d, (1, 1, 0, 0.9), 2.2)
                    # Faint control polygon for Fit Points too - purely
                    # visual feedback for point ordering while editing,
                    # the curve still passes exactly through each point.
                    ctrl_2d = pts_2d_ctrl if self._drag_idx is not None else pts_2d_ctrl + ([c2] if c2 else [])
                    for i in range(len(ctrl_2d) - 1):
                        gfx_dashed_line(ctrl_2d[i], ctrl_2d[i + 1], col=(0.6, 0.6, 0.6, 0.35), dash_len=4, space_len=4, w=1.0)

        if self._pts and self._polar_angle is not None:
            last2d = world_to_screen(context, self._pts[-1])
            if last2d and c2:
                draw_direction_guide(context, world_to_screen, self._pts[-1], self._cur, c2)

        # Insert-point affordance: while hovering a control-polygon
        # segment (and not already grabbing/hovering a point), show a
        # small marker at the snapped cursor position hinting that
        # Ctrl+Click will splice a new point in here.
        if (self._pts and self._drag_idx is None and self._hover_idx is None
                and not self._inp and c2 and self._hit_test_segment(context) is not None):
            gfx_circle(c2, 6, (0.3, 1.0, 0.4, 0.9), w=1.6)

        # Alt dropdown menu / indicator, only before the first point
        if not self._pts:
            if self._alt_held:
                self._draw_spline_menu(context)
            else:
                self._draw_method_indicator(context)

        if self._shift_held and show_cursor_fx:
            disp = self._deg_inp + "▌" if self._deg_inp else str(self._degree)
            box_x, box_y = mx, my - 70
            draw_float_dim(box_x, box_y, f"Degree: {disp}", active=True, locked=bool(self._deg_inp))

        if not self._pts:
            if not self._alt_held and show_cursor_fx:
                if self._inp:
                    draw_dim_rows(context, mx, my, [("Dist:", self._inp + "▌", True, False)])
                else:
                    draw_dim_rows(context, mx, my,
                                  [("X:", f"{self._cur.x:.4f}", True, False),
                                   ("Y:", f"{self._cur.y:.4f}", False, False)])
        else:
            dist = (self._cur - self._pts[-1]).length
            disp = self._inp + "▌" if self._inp else format_length(context, dist)
            if show_cursor_fx and c2:
                draw_float_dim(c2[0] + 50, c2[1], disp, active=True, locked=bool(self._inp))

        if show_cursor_fx:
            cx, cy = c2 if c2 else (mx, my)
            draw_crosshair(context, cx, cy, self._axis, show_box=False)
        gfx_text(f"SPLINE ({_METHOD_LABELS[self._method]})   {len(self._pts)} pts", 10, 55, 12, (1.0, 0.85, 0.2, 1.0))
