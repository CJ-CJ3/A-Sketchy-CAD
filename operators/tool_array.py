# ─────────────────────────────────────────────────────────────────────
#  operators/tool_array.py
#  CAD_OT_SketchArray – Rectangular and Polar array, switchable without
#  leaving the tool. For each source object, every generated copy
#  (excluding the original) is merged into a single new object on a
#  dedicated "CAD Array Copies" collection; the original object itself
#  is left completely untouched, on its own original collection/layer.
#
#  Workflow
#  --------
#  1.  Activate the Array Tool.
#  2.  Select object(s) - either beforehand (native Blender selection),
#      or right here by clicking in the viewport (Shift adds/toggles).
#  3.  Press Enter to confirm the selection, then specify a point -
#      full OSNAP + OTRACK, same as Move/Copy's base point. For
#      Rectangular Array this is the base point the direction is
#      measured from; for Polar Array it's the center of the circular
#      pattern.
#  4.  Tap Alt at any time before or after that point is placed to
#      switch between Rectangular and Polar - the current object
#      selection is preserved either way.
#  5.  Drag the cursor to set the live parameters (direction+spacing
#      for Rectangular, total sweep angle for Polar) - the merged
#      copies-object is rebuilt every time a parameter changes, so
#      this is a genuine live preview.
#  6.  Press Shift at any time to open a small typed-edit box for the
#      current method's numeric fields - Tab/arrows or Ctrl+1../N cycle
#      which field is being typed into, digits/backspace edit it live,
#      Enter closes the box (values stay; moving the mouse again
#      resumes live drag control), Esc cancels just the box. While in
#      Polar mode, 'D' flips direction (CW/CCW) and 'O' toggles whether
#      copies rotate to follow the circle or keep their original
#      orientation - both available any time in the placement phase.
#      In Path mode, 'O' toggles "Align items" (AutoCAD terminology):
#      on (default) each copy is rotated to match the path's tangent
#      direction at its placement point (computed directly from the
#      picked path's own points - no helper Curve object or modifier
#      involved); off keeps each copy's own orientation and only
#      translates it along the path.
#  7.  Enter (outside the edit box) or a plain click confirms the
#      array as final. Esc cancels the whole tool and removes every
#      preview copies-object - the original object(s) are never
#      touched.
#
#  With multiple objects selected, each source gets its own separate
#  merged copies-object (copies are never combined across different
#  source objects).
# ─────────────────────────────────────────────────────────────────────

import math
import bmesh
import bpy
from mathutils import Vector

from ..utils.math_utils import build_snap_cache, apply_snaps, mouse_to_3d, world_to_screen, format_angle
from ..utils.otrack import SnapAcquisitionManager, update_tracking
from .. import state as _st
from ..utils.cursor_util import (
    update_modal_cursor, restore_modal_cursor, cursor_fx_visible, init_modal_cursor,
    guarded_modal,
    handle_ui_handoff,
    reset_tpanel_to_select,
)
from ..gfx.primitives import (
    gfx_dot, gfx_text, gfx_dashed_line, gfx_circle,
    gfx_rect_fill, gfx_rounded_rect_fill, gfx_rounded_rect_loop, gfx_lines,
)
from ..gfx.markers    import gfx_snap_marker, draw_crosshair, draw_osnap_tracking
from ..gfx.dims       import draw_float_dim, draw_dim_rows

_ARRAY_COLLECTION_NAME = "CAD Array Copies"


def _ensure_array_collection(context):
    """Return (or create) the single shared collection that every
    Array-tool merged copies-object is grouped into, separate from the
    original object's own collection."""
    col = bpy.data.collections.get(_ARRAY_COLLECTION_NAME)
    if col is None:
        col = bpy.data.collections.new(_ARRAY_COLLECTION_NAME)
        context.scene.collection.children.link(col)
    return col


def _closest_point_on_segment_2d(p, a, b):
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 < 1e-9:
        return a, 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / length2
    t = max(0.0, min(1.0, t))
    return (ax + dx * t, ay + dy * t), t


def _polyline_from_object(obj):
    """Chain every edge of *obj* into one ordered list of world-space
    points, walking from an open end if there is one. Only the
    connected component reachable from the first edge is used, which
    is fine for this addon's simple single-path Line/Arc/Spline
    objects. Returns [] if the object has fewer than 2 vertices."""
    mesh = obj.data
    mw = obj.matrix_world
    if len(mesh.vertices) < 2 or len(mesh.edges) < 1:
        return []

    adjacency = {}
    for e in mesh.edges:
        a, b = e.vertices
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)

    start = next((v for v, nbrs in adjacency.items() if len(nbrs) == 1), None)
    if start is None:
        start = next(iter(adjacency))

    ordered = [start]
    visited_edges = set()
    current = start
    while True:
        nxt = None
        for nbr in adjacency.get(current, []):
            key = tuple(sorted((current, nbr)))
            if key not in visited_edges:
                nxt = nbr
                visited_edges.add(key)
                break
        if nxt is None:
            break
        ordered.append(nxt)
        current = nxt

    return [mw @ mesh.vertices[vi].co for vi in ordered]


def _polyline_cumlen(pts):
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + (pts[i] - pts[i - 1]).length)
    return cum, cum[-1] if cum else 0.0


def _closest_point_on_polyline(pts, target):
    """Returns (closest_point, cumulative_distance_at_that_point)."""
    if len(pts) < 2:
        return (pts[0].copy(), 0.0) if pts else (target.copy(), 0.0)
    cum, _total = _polyline_cumlen(pts)
    best_d2 = None
    best_pt = pts[0]
    best_cum = 0.0
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        seg = b - a
        seg_len2 = seg.length_squared
        if seg_len2 < 1e-12:
            t = 0.0
        else:
            t = max(0.0, min(1.0, (target - a).dot(seg) / seg_len2))
        p = a + seg * t
        d2 = (p - target).length_squared
        if best_d2 is None or d2 < best_d2:
            best_d2 = d2
            best_pt = p
            best_cum = cum[i] + seg.length * t
    return best_pt, best_cum


def _point_at_distance(pts, cum, total_len, d):
    """Point on the polyline at cumulative arc-length *d* (clamped to
    the path's own extent)."""
    d = max(0.0, min(total_len, d))
    for i in range(len(pts) - 1):
        if cum[i + 1] >= d - 1e-9:
            seg_len = cum[i + 1] - cum[i]
            t = 0.0 if seg_len < 1e-9 else (d - cum[i]) / seg_len
            return pts[i] + (pts[i + 1] - pts[i]) * t
    return pts[-1].copy()


def _rotate_point_2d(p: Vector, center: Vector, angle_rad: float) -> Vector:
    d = p - center
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    return center + Vector((d.x * c - d.y * s, d.x * s + d.y * c, d.z))


_PICK_RADIUS_PX  = 12
_MIN_SPACING     = 1e-4
_MAX_COUNT       = 500   # sanity ceiling so a mistyped huge count can't hang the UI

_METHODS = ('RECT', 'GRID', 'POLAR', 'PATH')
_METHOD_LABELS = {
    'RECT': 'Rectangular Array', 'GRID': 'Grid Array',
    'POLAR': 'Polar Array', 'PATH': 'Path Array',
}


class CAD_OT_SketchArray(bpy.types.Operator):
    """Rectangular or Polar array of selected object(s). For each
    source, all copies (excluding the original) are merged into one
    new object on a dedicated collection; the original stays
    completely untouched on its own original collection."""
    bl_idname  = "cad.sketch_array"
    bl_label   = "CAD Array"
    bl_options = {'REGISTER', 'UNDO'}

    _EDIT_ORDER_RECT  = ['SPACING', 'COUNT', 'ANGLE']
    _EDIT_ORDER_GRID  = ['ROW_COUNT', 'ROW_SPACING', 'COL_COUNT', 'COL_SPACING']
    _EDIT_ORDER_POLAR = ['COUNT', 'TOTAL_ANGLE']
    _EDIT_ORDER_PATH  = ['SPACING', 'COUNT']
    _EDIT_LABELS = {
        'SPACING': 'Spacing', 'COUNT': 'Count', 'ANGLE': 'Direction',
        'TOTAL_ANGLE': 'Total Angle',
        'ROW_COUNT': 'Row Count', 'COL_COUNT': 'Column Count',
        'ROW_SPACING': 'Row Distance', 'COL_SPACING': 'Column Distance',
    }
    _SHORT_LABELS = {
        'SPACING': 'Sp:', 'COUNT': 'N:', 'ANGLE': 'Dir:', 'TOTAL_ANGLE': 'Ang:',
        'ROW_COUNT': 'R:', 'COL_COUNT': 'C:',
        'ROW_SPACING': 'Rd:', 'COL_SPACING': 'Cd:',
    }

    @property
    def _edit_order(self):
        if self._method == 'POLAR':
            return self._EDIT_ORDER_POLAR
        if self._method == 'PATH':
            return self._EDIT_ORDER_PATH
        if self._method == 'GRID':
            return self._EDIT_ORDER_GRID
        return self._EDIT_ORDER_RECT

    # ── method dropdown menu / indicator (same style as Arc tool) ────
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

    _INDICATOR_SIZE    = 28
    _INDICATOR_OFFSET  = 18
    _INDICATOR_RADIUS  = 6
    _INDICATOR_BG      = (0.110, 0.110, 0.110, 0.97)
    _INDICATOR_BORDER  = (0.239, 0.435, 0.769, 1.00)
    _INDICATOR_GLYPH   = (1.000, 1.000, 1.000, 1.00)

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
        if self._method == 'RECT':
            gfx_dot((cx - 7, cy), col, 3.5)
            gfx_dot((cx, cy), col, 3.5)
            gfx_dot((cx + 7, cy), col, 3.5)
            gfx_lines([(cx - 7, cy), (cx + 7, cy)], col, 1.4)
        elif self._method == 'GRID':
            for gx in (cx - 7, cx, cx + 7):
                for gy in (cy - 6, cy, cy + 6):
                    gfx_dot((gx, gy), col, 2.6)
        elif self._method == 'POLAR':
            gfx_circle((cx, cy), 7, col, segs=24, w=1.4)
            gfx_dot((cx, cy), col, 2.5)
            gfx_dot((cx + 7, cy), col, 3.0)
            gfx_dot((cx - 3.5, cy + 6), col, 3.0)
            gfx_dot((cx - 3.5, cy - 6), col, 3.0)
        else:   # PATH
            curve_pts = [(cx - 8, cy + 5), (cx - 3, cy - 5), (cx + 3, cy + 5), (cx + 8, cy - 5)]
            gfx_lines(curve_pts, col, 1.4)
            for p in (curve_pts[0], curve_pts[1], curve_pts[2], curve_pts[3]):
                gfx_dot(p, col, 2.6)

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

    def _draw_method_menu(self, context):
        bx, by, box_w, box_h = self._menu_rect(context)
        gfx_rounded_rect_fill(bx, by, box_w, box_h, self._MENU_RADIUS, self._MENU_BG)
        gfx_rounded_rect_loop(bx, by, box_w, box_h, self._MENU_RADIUS, self._MENU_BORDER, 1.0)

        header_y = by + box_h - self._MENU_HEADER_H
        gfx_rect_fill(bx + 1, header_y, box_w - 2, self._MENU_HEADER_H - 1, self._MENU_HEADER_BG)
        gfx_text("Array Method", bx + 10, header_y + (self._MENU_HEADER_H - 10) // 2, 10, self._MENU_HEADER_COL)
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
            label = ("✓ " if is_active else "  ") + _METHOD_LABELS[m]
            label_y = ry + (self._MENU_ROW_H - 12) // 2
            gfx_text(label, bx + 12, label_y, 12, text_col)

    # ── invoke ────────────────────────────────────────────────────────
    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._mx, self._my = event.mouse_region_x, event.mouse_region_y
        self._snap_type  = 'FREE'
        self._snap_cache = build_snap_cache(context)
        self._otrack     = SnapAcquisitionManager()
        self._otrack.reset(clear_points=True)
        self._cur_3d     = mouse_to_3d(context, self._mx, self._my) or Vector((0.0, 0.0, 0.0))

        self._method = _st.array_method if _st.array_method in _METHODS else 'RECT'
        self._alt_held = False

        # Rectangular Array params
        self._base_pt    = None
        self._direction  = Vector((1.0, 0.0, 0.0))
        self._spacing    = 1.0

        # Grid (Square/Rectangular) Array params - rows run along +Y,
        # columns along +X, independently of one another.
        self._rows         = 3
        self._cols         = 3
        self._row_spacing  = 1.0
        self._col_spacing  = 1.0

        # Polar Array params
        self._center_pt        = None
        self._polar_start_angle = 0.0   # reference angle (deg) captured at center-click
        self._polar_total_angle = 360.0
        self._polar_dir          = 1     # +1 = CCW, -1 = CW
        self._polar_rotate_copies = True

        # Path Array params
        self._path_hover  = None   # hovered candidate while picking
        self._path_points = []     # world-space polyline points, chained from the path object
        self._path_cum    = []     # cumulative arc-length at each point
        self._path_total_len = 0.0
        self._path_align  = True   # AutoCAD "Align items": orient copies to follow the path tangent
        self._path_anchor_world = None  # explicit anchor point (step 3), overrides auto-centroid

        self._count      = 3   # total instances including the original
        self._preview_objs = []   # currently-spawned merged copies-objects

        self._editing    = False
        self._edit_field = 'SPACING'
        self._edit_inp   = ''

        self._sel_objects = [o for o in context.selected_objects if o.type == 'MESH']
        self._phase = 'BASE' if self._sel_objects else 'SELECT'

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "ARRAY"
        context.window_manager.modal_handler_add(self)
        self._update_header(context)
        # cursor_modal_set('NONE') hides the OS cursor immediately, but
        # the custom crosshair drawn in its place only appears on the
        # next viewport redraw - without forcing one here there's a gap
        # (until the user happens to move the mouse) with no cursor
        # visible at all.
        context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _update_header(self, context):
        method_name = _METHOD_LABELS[self._method].replace(" Array", "")
        if self._phase == 'SELECT':
            txt = ("ARRAY: click to select object(s)  (Shift=add)  |  "
                   "Enter=confirm  |  Esc=cancel")
        elif self._phase == 'BASE':
            if self._method == 'PATH':
                txt = ("ARRAY (Path): click the line/arc/curve to array along  |  "
                       "Alt/M/Scroll=switch method  |  Esc=cancel")
            else:
                pt_name = "center point" if self._method == 'POLAR' else "base point"
                txt = f"ARRAY ({method_name}): specify {pt_name}  |  Alt/M/Scroll=switch method  |  Esc=cancel"
        elif self._phase == 'ANCHOR':
            txt = ("ARRAY (Path): click the object's anchor point (where copies are placed from)  |  "
                   "Esc=cancel")
        elif self._editing:
            label = self._EDIT_LABELS[self._edit_field]
            txt = (f"ARRAY ({method_name}): editing {label}  |  Tab/\u2193=next  \u2191=previous  |  "
                   f"type value  |  Enter=done  |  Esc=cancel edit")
        elif self._method == 'POLAR':
            dirlabel = "CCW" if self._polar_dir > 0 else "CW"
            rotlabel = "rotate" if self._polar_rotate_copies else "keep orientation"
            txt = (f"ARRAY (Polar): Count {self._count}  \u00b7  Angle {self._polar_total_angle:.2f}\u00b0  \u00b7  "
                   f"{dirlabel}  \u00b7  {rotlabel}  |  drag=angle  \u00b7  Shift=edit  \u00b7  D=direction  \u00b7  "
                   f"O=orientation  \u00b7  Alt/M/Scroll=switch method  |  Click/Enter=confirm  |  Esc=cancel")
        elif self._method == 'PATH':
            alignlabel = "align to path" if self._path_align else "keep orientation"
            txt = (f"ARRAY (Path): Spacing {self._spacing:.4f} m  \u00b7  Count {self._count}  \u00b7  "
                   f"{alignlabel}  |  drag along path=spacing  \u00b7  Shift=edit fields  \u00b7  O=orientation  \u00b7  "
                   f"Alt/M/Scroll=switch method  |  Click/Enter=confirm  |  Esc=cancel")
        elif self._method == 'GRID':
            txt = (f"ARRAY (Grid): Rows {self._rows} \u00d7 {self._row_spacing:.4f} m  \u00b7  "
                   f"Columns {self._cols} \u00d7 {self._col_spacing:.4f} m  |  drag=row/column spacing  \u00b7  "
                   f"Shift=edit fields  \u00b7  Alt/M/Scroll=switch method  |  Click/Enter=confirm  |  Esc=cancel")
        else:
            txt = (f"ARRAY (Rectangular): Spacing {self._spacing:.4f} m  \u00b7  Count {self._count}  |  "
                   f"drag=direction/spacing  \u00b7  Shift=edit fields  \u00b7  Alt/M/Scroll=switch method  |  "
                   f"Click/Enter=confirm  |  Esc=cancel")
        context.area.header_text_set(txt)

    # ── cleanup ──────────────────────────────────────────────────────
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

    def _clear_preview(self):
        for obj in self._preview_objs:
            mesh = obj.data
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:
                pass
            if mesh is not None and mesh.users == 0:
                try:
                    bpy.data.meshes.remove(mesh)
                except Exception:
                    pass
        self._preview_objs = []

    def cancel(self, context):
        """Remove every preview copies-object - the source object(s)
        were never touched, so there's nothing else to revert."""
        self._clear_preview()
        self._cleanup(context)

    def _finish(self, context):
        """Keep the current preview copies-objects as the final array."""
        for o in context.selected_objects:
            o.select_set(False)
        for o in self._preview_objs:
            o.select_set(True)
        if self._preview_objs:
            context.view_layer.objects.active = self._preview_objs[-1]
        self._preview_objs = []   # ownership transferred - don't delete on cleanup
        self._cleanup(context)

    # ── method switching (Alt=dropdown menu, Scroll/M=switch) ────────
    def _method_ready(self):
        if self._method == 'POLAR':
            return self._center_pt is not None
        if self._method == 'PATH':
            return self._path_obj is not None
        return self._base_pt is not None

    def _set_method(self, context, new_method):
        if new_method == self._method:
            return
        self._method = new_method
        _st.array_method = self._method
        self._editing = False
        self._edit_inp = ''
        self._edit_field = self._edit_order[0]
        if self._method != 'PATH' and self._spacing < 0:
            # Negative spacing is a Path-Array-only concept (direction
            # along the path); Rect Array's own _direction vector
            # already carries sign/orientation, so normalize back to a
            # magnitude when leaving Path Array.
            self._spacing = abs(self._spacing)
        if self._phase == 'PLACE' and not self._method_ready():
            # switched to a method whose anchor (base/center point, or
            # picked path) was never specified - re-prompt for it
            # rather than drawing/regenerating against nothing
            self._phase = 'BASE'
            self._clear_preview()
        elif self._phase == 'PLACE':
            self._regenerate_preview(context)
        self._update_header(context)

    def _cycle_method(self, context, step=1):
        idx = _METHODS.index(self._method)
        self._set_method(context, _METHODS[(idx + step) % len(_METHODS)])

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

        menu_ok = self._phase in {'BASE', 'PLACE'} and not self._editing
        self._alt_held = bool(event.alt) and menu_ok

        if event.type in {'LEFT_ALT', 'RIGHT_ALT'}:
            self._alt_held = (event.value == 'PRESS') and menu_ok
            return {'RUNNING_MODAL'}

        if event.type == 'MIDDLEMOUSE':
            return {'PASS_THROUGH'}
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN'} and not (event.type != 'TRACKPADPAN' and menu_ok and event.alt):
            return {'PASS_THROUGH'}

        if menu_ok:
            if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} and event.value == 'PRESS' and event.alt:
                self._cycle_method(context, -1 if event.type == 'WHEELUPMOUSE' else 1)
                return {'RUNNING_MODAL'}

            if event.type == 'M' and event.value == 'PRESS':
                self._cycle_method(context, 1)
                return {'RUNNING_MODAL'}

            if event.type == 'LEFTMOUSE' and event.value == 'PRESS' and self._alt_held:
                hit = self._menu_hit(context, self._mx, self._my)
                if hit is not None:
                    self._set_method(context, hit)
                self._alt_held = False
                return {'RUNNING_MODAL'}

        if self._phase == 'SELECT':
            return self._modal_select(context, event)
        if self._phase == 'BASE':
            return self._modal_base(context, event)
        if self._phase == 'ANCHOR':
            return self._modal_anchor(context, event)
        return self._modal_place(context, event)

    # ── SELECT phase ─────────────────────────────────────────────────
    def _pick(self, context):
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

    # ── BASE phase (base point for Rect, center point for Polar) ─────
    # ── shared reference-point helper (used by drag math, drawing and
    #    the merged-copy builders so they all agree on "where the
    #    object is") ──────────────────────────────────────────────────
    def _obj_pivot_world(self, obj):
        """This addon's CAD objects store absolute world-space
        coordinates directly in their mesh vertices, with
        obj.matrix_world normally at Identity (only ever nudged by the
        Move tool) - so matrix_world.translation is *not* a reliable
        stand-in for "where the object visually is". Use the
        world-space vertex centroid instead, unless the user explicitly
        picked an anchor point (Path Array step 3) - that always wins,
        since it's the point they chose to define where copies are
        placed from."""
        if self._path_anchor_world is not None:
            return self._path_anchor_world.copy()
        mesh = obj.data
        if not mesh.vertices:
            return obj.matrix_world.translation.copy()
        mw = obj.matrix_world
        pts = [mw @ v.co for v in mesh.vertices]
        return sum(pts, Vector((0.0, 0.0, 0.0))) / len(pts)

    def _pick_path_object_at_cursor(self, context):
        """Nearest edge to the cursor, screen-space, returning the
        whole OBJECT it belongs to (the entire object is treated as
        the path) - excludes the array's own source object(s)."""
        mx, my = self._mx, self._my
        best_d = float(_PICK_RADIUS_PX)
        best   = None
        for obj in context.view_layer.objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            if obj in self._sel_objects:
                continue
            mw = obj.matrix_world
            mesh = obj.data
            for e in mesh.edges:
                a3 = mw @ mesh.vertices[e.vertices[0]].co
                b3 = mw @ mesh.vertices[e.vertices[1]].co
                a2 = world_to_screen(context, a3)
                b2 = world_to_screen(context, b3)
                if a2 is None or b2 is None:
                    continue
                (cx, cy), _t = _closest_point_on_segment_2d((mx, my), a2, b2)
                d = math.hypot(cx - mx, cy - my)
                if d < best_d:
                    best_d = d
                    best   = obj
        return best

    def _modal_base(self, context, event):
        if self._method == 'PATH':
            if event.type == 'MOUSEMOVE':
                self._path_hover = self._pick_path_object_at_cursor(context)
                return {'RUNNING_MODAL'}
            if event.value == 'PRESS':
                if event.type == 'LEFTMOUSE':
                    if self._path_hover is not None:
                        pts = _polyline_from_object(self._path_hover)
                        if len(pts) < 2:
                            self.report({'WARNING'}, "Path needs at least 2 connected points")
                            return {'RUNNING_MODAL'}
                        self._path_obj = self._path_hover
                        self._path_points = pts
                        self._path_cum, self._path_total_len = _polyline_cumlen(pts)
                        self._path_anchor_world = None
                        self._otrack.reset(clear_points=True)
                        self._phase = 'ANCHOR'
                        self._update_header(context)
                    return {'RUNNING_MODAL'}
                if event.type in {'RIGHTMOUSE', 'ESC'}:
                    self.cancel(context)
                    return {'CANCELLED'}
            return {'RUNNING_MODAL'}

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
                pt = self._cur_3d.copy()
                self._otrack.reset(clear_points=True)
                self._phase = 'PLACE'
                if self._method == 'POLAR':
                    self._center_pt = pt
                    self._polar_start_angle = math.degrees(
                        math.atan2(self._cur_3d.y - pt.y, self._cur_3d.x - pt.x)
                    )
                else:
                    self._base_pt = pt
                self._regenerate_preview(context)
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ── ANCHOR phase (Path Array step 3: pick the point on the object
    #    that defines where copies are placed from) ───────────────────
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
                self._path_anchor_world = self._cur_3d.copy()
                self._otrack.reset(clear_points=True)
                self._phase = 'PLACE'
                self._regenerate_preview(context)
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ── PLACE phase ──────────────────────────────────────────────────
    def _modal_place(self, context, event):
        if event.type == 'MOUSEMOVE':
            if not self._editing:
                raw = mouse_to_3d(context, self._mx, self._my)
                if raw:
                    anchor = None
                    if self._method == 'POLAR':
                        anchor = self._center_pt
                    elif self._method in ('RECT', 'GRID'):
                        anchor = self._base_pt
                    snapped, stype = apply_snaps(
                        context, raw, self._snap_cache,
                        snap_filter=_st.selection.get_active_stypes(),
                        anchor=anchor,
                    )
                    self._snap_type = stype
                    update_tracking(self._otrack, snapped, stype)
                    self._cur_3d = snapped

                    if self._method == 'POLAR':
                        cur_angle = math.degrees(math.atan2(
                            snapped.y - self._center_pt.y, snapped.x - self._center_pt.x))
                        swept = (cur_angle - self._polar_start_angle) * self._polar_dir
                        swept = swept % 360.0
                        if swept < 1e-6:
                            swept = 360.0
                        self._polar_total_angle = swept
                        self._regenerate_preview(context)
                    elif self._method == 'PATH':
                        if self._path_total_len > 1e-9 and self._sel_objects:
                            ref_obj = self._sel_objects[0]
                            _, start_dist = _closest_point_on_polyline(
                                self._path_points, self._obj_pivot_world(ref_obj))
                            _, cur_dist = _closest_point_on_polyline(self._path_points, snapped)
                            n = max(1, self._count - 1)
                            spacing = (cur_dist - start_dist) / n
                            # Sign preserved: dragging the cursor back
                            # toward (or past) the anchor's own path
                            # position gives a negative spacing, which
                            # walks the array backward along the path
                            # instead of forward.
                            if abs(spacing) > _MIN_SPACING:
                                self._spacing = spacing
                                self._regenerate_preview(context)
                    elif self._method == 'GRID':
                        # Rows/columns stay independent: horizontal
                        # drag distance sets column spacing, vertical
                        # drag distance sets row spacing, at the same
                        # time - no combined single-direction offset.
                        # Sign is kept (not abs()'d) so dragging left
                        # of the base point gives a negative column
                        # spacing (copies go left) and dragging below
                        # gives a negative row spacing (copies go
                        # down), matching typed negative-value input.
                        delta = snapped - self._base_pt
                        changed = False
                        if abs(delta.x) > 1e-6:
                            self._col_spacing = math.copysign(
                                max(_MIN_SPACING, abs(delta.x)), delta.x)
                            changed = True
                        if abs(delta.y) > 1e-6:
                            self._row_spacing = math.copysign(
                                max(_MIN_SPACING, abs(delta.y)), delta.y)
                            changed = True
                        if changed:
                            self._regenerate_preview(context)
                    else:
                        delta = snapped - self._base_pt
                        if delta.length > 1e-6:
                            self._direction = delta.normalized()
                            self._spacing   = max(_MIN_SPACING, delta.length)
                            self._regenerate_preview(context)
            return {'RUNNING_MODAL'}

        if (event.type in {'TAB', 'DOWN_ARROW', 'UP_ARROW'}
                and event.value == 'PRESS' and self._editing):
            order = self._edit_order
            step = -1 if event.type == 'UP_ARROW' else 1
            self._edit_field = order[(order.index(self._edit_field) + step) % len(order)]
            self._edit_inp = ''
            self._update_header(context)
            return {'RUNNING_MODAL'}

        if (self._editing and event.ctrl and event.value == 'PRESS'
                and event.type in {'ONE', 'TWO', 'THREE'}):
            idx = {'ONE': 0, 'TWO': 1, 'THREE': 2}[event.type]
            order = self._edit_order
            if idx < len(order):
                self._edit_field = order[idx]
                self._edit_inp = ''
                self._update_header(context)
            return {'RUNNING_MODAL'}

        if (event.type in {'LEFT_SHIFT', 'RIGHT_SHIFT'} and event.value == 'PRESS'):
            self._editing = not self._editing
            self._edit_inp = ''
            if self._editing:
                self._edit_field = self._edit_order[0]
            self._update_header(context)
            return {'RUNNING_MODAL'}

        if (not self._editing and self._method == 'POLAR' and event.value == 'PRESS'):
            if event.type == 'D':
                self._polar_dir *= -1
                self._regenerate_preview(context)
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if event.type == 'O':
                self._polar_rotate_copies = not self._polar_rotate_copies
                self._regenerate_preview(context)
                self._update_header(context)
                return {'RUNNING_MODAL'}

        if (not self._editing and self._method == 'PATH' and event.value == 'PRESS'
                and event.type == 'O'):
            self._path_align = not self._path_align
            self._regenerate_preview(context)
            self._update_header(context)
            return {'RUNNING_MODAL'}

        if self._editing and event.value == 'PRESS':
            ch = event.ascii
            if self._edit_field in ('COUNT', 'ROW_COUNT', 'COL_COUNT'):
                allowed = '0123456789'
            elif self._edit_field in ('ROW_SPACING', 'COL_SPACING') or \
                    (self._edit_field == 'SPACING' and self._method == 'PATH'):
                # These support a negative sign to reverse the array
                # direction - only meaningful as the very first
                # character, so a mid-string '-' is ignored rather
                # than producing something like "1-2".
                allowed = '0123456789.' + ('-' if self._edit_inp == '' else '')
            else:
                allowed = '0123456789.'
            if ch and ch in allowed:
                self._edit_inp += ch
                self._apply_edit_value(context)
                return {'RUNNING_MODAL'}
            if event.type == 'BACK_SPACE' and self._edit_inp:
                self._edit_inp = self._edit_inp[:-1]
                self._apply_edit_value(context)
                return {'RUNNING_MODAL'}
            if event.type in {'RET', 'NUMPAD_ENTER'}:
                self._editing = False
                self._edit_inp = ''
                self._update_header(context)
                return {'RUNNING_MODAL'}
            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self._editing = False
                self._edit_inp = ''
                self._update_header(context)
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        if event.value == 'PRESS':
            if event.type in {'RET', 'NUMPAD_ENTER', 'LEFTMOUSE'}:
                self._finish(context)
                return {'FINISHED'}
            if event.type in {'RIGHTMOUSE', 'ESC'}:
                self.cancel(context)
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def _apply_edit_value(self, context):
        if not self._edit_inp:
            return
        try:
            if self._edit_field == 'SPACING':
                v = float(self._edit_inp)
                if self._method == 'PATH':
                    # Negative spacing walks the array backward along
                    # the path; Rect Array keeps spacing as a pure
                    # magnitude (its own _direction vector carries the
                    # sign/orientation instead).
                    self._spacing = math.copysign(max(_MIN_SPACING, abs(v)), v) if v != 0 else _MIN_SPACING
                else:
                    self._spacing = max(_MIN_SPACING, v)
            elif self._edit_field == 'COUNT':
                self._count = max(1, min(_MAX_COUNT, int(self._edit_inp)))
            elif self._edit_field == 'ANGLE':
                rad = math.radians(float(self._edit_inp))
                self._direction = Vector((math.cos(rad), math.sin(rad), 0.0))
            elif self._edit_field == 'TOTAL_ANGLE':
                self._polar_total_angle = max(0.0001, min(360.0, float(self._edit_inp)))
            elif self._edit_field == 'ROW_COUNT':
                self._rows = max(1, min(_MAX_COUNT, int(self._edit_inp)))
            elif self._edit_field == 'COL_COUNT':
                self._cols = max(1, min(_MAX_COUNT, int(self._edit_inp)))
            elif self._edit_field == 'ROW_SPACING':
                v = float(self._edit_inp)
                self._row_spacing = math.copysign(max(_MIN_SPACING, abs(v)), v) if v != 0 else _MIN_SPACING
            elif self._edit_field == 'COL_SPACING':
                v = float(self._edit_inp)
                self._col_spacing = math.copysign(max(_MIN_SPACING, abs(v)), v) if v != 0 else _MIN_SPACING
        except ValueError:
            return
        self._regenerate_preview(context)

    # ── merged copies-object builders (one per source, original untouched)
    def _build_rect_copies_object(self, obj, offset_unit_world):
        mesh = obj.data
        t_verts = [v.co.copy() for v in mesh.vertices]
        t_edges = [tuple(e.vertices) for e in mesh.edges]
        t_faces = [tuple(p.vertices) for p in mesh.polygons]
        nv = len(t_verts)
        if nv == 0:
            return None

        n_copies = self._count - 1
        mw_inv3 = obj.matrix_world.inverted().to_3x3()
        local_offset_unit = mw_inv3 @ offset_unit_world

        bm = bmesh.new()
        for i in range(1, n_copies + 1):
            off = local_offset_unit * i
            for co in t_verts:
                bm.verts.new(co + off)
        bm.verts.ensure_lookup_table()

        for ci in range(n_copies):
            base = ci * nv
            for (a, b) in t_edges:
                v1, v2 = bm.verts[base + a], bm.verts[base + b]
                if not bm.edges.get((v1, v2)):
                    bm.edges.new((v1, v2))
            for f in t_faces:
                try:
                    bm.faces.new([bm.verts[base + idx] for idx in f])
                except ValueError:
                    pass

        new_mesh = bpy.data.meshes.new(obj.name + "_array_copies")
        bm.to_mesh(new_mesh)
        bm.free()
        new_obj = bpy.data.objects.new(obj.name + ".array", new_mesh)
        new_obj.matrix_world = obj.matrix_world.copy()
        return new_obj

    def _build_grid_copies_object(self, obj):
        """Independent Rows x Columns grid: columns spread along
        world +X (self._col_spacing apart), rows stack along world +Y
        (self._row_spacing apart), completely independently of each
        other - a 3x5 grid is 3 rows of 5 columns each, not one
        combined direction/spacing like Rectangular Array. The
        original object occupies slot (row 0, col 0) and is skipped
        here (it's never touched), matching every other array method
        in this file."""
        mesh = obj.data
        t_verts = [v.co.copy() for v in mesh.vertices]
        t_edges = [tuple(e.vertices) for e in mesh.edges]
        t_faces = [tuple(p.vertices) for p in mesh.polygons]
        nv = len(t_verts)
        if nv == 0:
            return None

        rows = max(1, self._rows)
        cols = max(1, self._cols)
        if rows * cols <= 1:
            return None

        mw_inv3 = obj.matrix_world.inverted().to_3x3()
        col_offset_local = mw_inv3 @ Vector((self._col_spacing, 0.0, 0.0))
        row_offset_local = mw_inv3 @ Vector((0.0, self._row_spacing, 0.0))

        bm = bmesh.new()
        n_copies = 0
        for r in range(rows):
            for c in range(cols):
                if r == 0 and c == 0:
                    continue  # original's own slot - stays untouched
                off = col_offset_local * c + row_offset_local * r
                for co in t_verts:
                    bm.verts.new(co + off)
                n_copies += 1
        bm.verts.ensure_lookup_table()

        for ci in range(n_copies):
            base = ci * nv
            for (a, b) in t_edges:
                v1, v2 = bm.verts[base + a], bm.verts[base + b]
                if not bm.edges.get((v1, v2)):
                    bm.edges.new((v1, v2))
            for f in t_faces:
                try:
                    bm.faces.new([bm.verts[base + idx] for idx in f])
                except ValueError:
                    pass

        new_mesh = bpy.data.meshes.new(obj.name + "_array_copies")
        bm.to_mesh(new_mesh)
        bm.free()
        new_obj = bpy.data.objects.new(obj.name + ".array", new_mesh)
        new_obj.matrix_world = obj.matrix_world.copy()
        return new_obj

    def _build_polar_copies_object(self, obj):
        mesh = obj.data
        mw = obj.matrix_world
        t_verts_world = [mw @ v.co for v in mesh.vertices]
        t_edges = [tuple(e.vertices) for e in mesh.edges]
        t_faces = [tuple(p.vertices) for p in mesh.polygons]
        nv = len(t_verts_world)
        if nv == 0:
            return None

        n_copies = self._count - 1
        center = self._center_pt
        pivot_world = mw.translation.copy()
        full_circle = abs(self._polar_total_angle - 360.0) < 1e-6
        step = (self._polar_total_angle / self._count if full_circle
                else self._polar_total_angle / max(1, self._count - 1))

        bm = bmesh.new()
        mw_inv = mw.inverted()
        for i in range(1, n_copies + 1):
            angle = math.radians(self._polar_dir * step * i)
            if self._polar_rotate_copies:
                new_pts_world = [_rotate_point_2d(p, center, angle) for p in t_verts_world]
            else:
                new_pivot = _rotate_point_2d(pivot_world, center, angle)
                delta = new_pivot - pivot_world
                new_pts_world = [p + delta for p in t_verts_world]
            for p in new_pts_world:
                bm.verts.new(mw_inv @ p)
        bm.verts.ensure_lookup_table()

        for ci in range(n_copies):
            base = ci * nv
            for (a, b) in t_edges:
                v1, v2 = bm.verts[base + a], bm.verts[base + b]
                if not bm.edges.get((v1, v2)):
                    bm.edges.new((v1, v2))
            for f in t_faces:
                try:
                    bm.faces.new([bm.verts[base + idx] for idx in f])
                except ValueError:
                    pass

        new_mesh = bpy.data.meshes.new(obj.name + "_array_copies")
        bm.to_mesh(new_mesh)
        bm.free()
        new_obj = bpy.data.objects.new(obj.name + ".array", new_mesh)
        new_obj.matrix_world = mw.copy()
        return new_obj

    # ── Path Array (baked positions + optional tangent-follow
    #    rotation - no helper Curve object/datablock is created; the
    #    picked path's own polyline points, already sampled into
    #    self._path_points, are used directly) ─────────────────────
    def _path_tangent_at_distance(self, d):
        """Direction of travel along the picked path's polyline at
        arc-length *d* - exact (not approximated), since a polyline's
        tangent is just the direction of whichever segment contains
        *d*."""
        pts, cum, total_len = self._path_points, self._path_cum, self._path_total_len
        d = max(0.0, min(total_len, d))
        for i in range(len(pts) - 1):
            if cum[i + 1] >= d - 1e-9:
                seg = pts[i + 1] - pts[i]
                if seg.length > 1e-9:
                    return seg.normalized()
        for i in range(len(pts) - 2, -1, -1):
            seg = pts[i + 1] - pts[i]
            if seg.length > 1e-9:
                return seg.normalized()
        return Vector((1.0, 0.0, 0.0))

    def _build_path_copies_object(self, obj):
        """Distribute copies of *obj* along the picked path at even
        arc-length spacing, starting from the object's own anchor
        point (step 3) or centroid. With 'Align items' on (the
        AutoCAD-style default), each copy is additionally rotated in
        the path's plane to match the path's tangent direction at its
        placement point - computed directly from the polyline, so no
        helper Curve object is ever created just to drive a Curve
        modifier."""
        mesh = obj.data
        mw = obj.matrix_world
        t_verts_world = [mw @ v.co for v in mesh.vertices]
        t_edges = [tuple(e.vertices) for e in mesh.edges]
        t_faces = [tuple(p.vertices) for p in mesh.polygons]
        nv = len(t_verts_world)
        if nv == 0:
            return None

        # Same reference point as the live-drag math and the on-screen
        # preview: the world-space vertex centroid (or the explicit
        # anchor point from step 3), not matrix_world.translation -
        # see _obj_pivot_world.
        pivot_world = self._obj_pivot_world(obj)
        _, start_dist = _closest_point_on_polyline(self._path_points, pivot_world)
        ref_tangent = self._path_tangent_at_distance(start_dist)
        ref_angle = math.atan2(ref_tangent.y, ref_tangent.x)

        n_copies_requested = self._count - 1
        placements = []  # list of (offset_from_pivot, extra_rotation_rad)
        for i in range(1, n_copies_requested + 1):
            d = start_dist + self._spacing * i
            # Negative spacing walks backward along the path (toward
            # its start) instead of forward - stop once either end of
            # the path is reached, same as the forward-direction case.
            if d > self._path_total_len + 1e-6 or d < -1e-6:
                break
            pos = _point_at_distance(self._path_points, self._path_cum, self._path_total_len, d)
            if self._path_align:
                tangent = self._path_tangent_at_distance(d)
                rot = math.atan2(tangent.y, tangent.x) - ref_angle
            else:
                rot = 0.0
            placements.append((pos - pivot_world, rot))
        if not placements:
            return None

        bm = bmesh.new()
        mw_inv = mw.inverted()
        for offset, rot in placements:
            cos_r, sin_r = math.cos(rot), math.sin(rot)
            for p in t_verts_world:
                rel = p - pivot_world
                if rot != 0.0:
                    rel = Vector((rel.x * cos_r - rel.y * sin_r,
                                  rel.x * sin_r + rel.y * cos_r,
                                  rel.z))
                bm.verts.new(mw_inv @ (pivot_world + rel + offset))
        bm.verts.ensure_lookup_table()

        for ci in range(len(placements)):
            base = ci * nv
            for (a, b) in t_edges:
                v1, v2 = bm.verts[base + a], bm.verts[base + b]
                if not bm.edges.get((v1, v2)):
                    bm.edges.new((v1, v2))
            for f in t_faces:
                try:
                    bm.faces.new([bm.verts[base + idx] for idx in f])
                except ValueError:
                    pass

        new_mesh = bpy.data.meshes.new(obj.name + "_patharray_copies")
        bm.to_mesh(new_mesh)
        bm.free()
        new_obj = bpy.data.objects.new(obj.name + ".patharray", new_mesh)
        new_obj.matrix_world = mw.copy()
        return new_obj


    def _regenerate_preview(self, context):
        self._clear_preview()
        if self._method == 'GRID':
            if self._rows * self._cols <= 1:
                return
        elif self._count <= 1:
            return
        array_coll = _ensure_array_collection(context)

        if self._method == 'POLAR':
            if self._center_pt is None:
                return
            for obj in self._sel_objects:
                merged = self._build_polar_copies_object(obj)
                if merged is not None:
                    array_coll.objects.link(merged)
                    self._preview_objs.append(merged)
        elif self._method == 'PATH':
            if not self._path_points or self._path_total_len <= 1e-9:
                return
            for obj in self._sel_objects:
                merged = self._build_path_copies_object(obj)
                if merged is not None:
                    array_coll.objects.link(merged)
                    self._preview_objs.append(merged)
        elif self._method == 'GRID':
            if self._base_pt is None:
                return
            for obj in self._sel_objects:
                merged = self._build_grid_copies_object(obj)
                if merged is not None:
                    array_coll.objects.link(merged)
                    self._preview_objs.append(merged)
        else:
            offset_unit = self._direction * self._spacing
            for obj in self._sel_objects:
                merged = self._build_rect_copies_object(obj, offset_unit)
                if merged is not None:
                    array_coll.objects.link(merged)
                    self._preview_objs.append(merged)

    # ── draw callback ────────────────────────────────────────────────
    _COL_SELECTED = (1.00, 0.50, 0.00, 0.95)
    _COL_PREVIEW  = (0.37, 0.92, 0.67, 0.85)

    def _draw_persistent_selection(self, context):
        for obj in self._sel_objects:
            s2 = world_to_screen(context, obj.matrix_world.translation)
            if s2:
                gfx_dot(s2, self._COL_SELECTED, 8)

    def _draw_object_edges(self, context, obj, col):
        mesh = obj.data
        mw = obj.matrix_world
        for e in mesh.edges:
            a3 = mw @ mesh.vertices[e.vertices[0]].co
            b3 = mw @ mesh.vertices[e.vertices[1]].co
            a2 = world_to_screen(context, a3)
            b2 = world_to_screen(context, b3)
            if a2 and b2:
                gfx_lines([a2, b2], col, 3.0)

    def _draw(self, context):
        try:
            self._draw_impl(context)
        except Exception as e:
            print(f"[CAD] tool_array draw error: {e}")

    def _draw_impl(self, context):
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
            gfx_text("ARRAY: select object(s), Enter to confirm", 10, 55, 12, (0.3, 0.9, 1.0, 1.0))
            return

        cur_2d = world_to_screen(context, self._cur_3d) if self._cur_3d else None
        method_label = _METHOD_LABELS[self._method].replace(" Array", "")

        if self._phase == 'BASE':
            if self._method == 'PATH':
                if self._path_hover is not None:
                    self._draw_object_edges(context, self._path_hover, (1.0, 1.0, 0.2, 0.95))
                if show_cursor_fx:
                    draw_crosshair(context, self._mx, self._my, None, show_box=False)
                gfx_text(f"ARRAY ({method_label}): click the line/arc/curve  (Alt/M/Scroll=switch)",
                          10, 55, 12, (0.3, 0.9, 1.0, 1.0))
                if self._alt_held:
                    self._draw_method_menu(context)
                else:
                    self._draw_method_indicator(context)
                return
            if show_cursor_fx:
                if cur_2d:
                    gfx_snap_marker(cur_2d, self._snap_type)
                cx, cy = cur_2d if cur_2d else (self._mx, self._my)
                draw_crosshair(context, cx, cy, None, show_box=False)
            pt_name = "center point" if self._method == 'POLAR' else "base point"
            gfx_text(f"ARRAY ({method_label}): specify {pt_name}  (Alt/M/Scroll=switch)",
                      10, 55, 12, (0.3, 0.9, 1.0, 1.0))
            if self._alt_held:
                self._draw_method_menu(context)
            else:
                self._draw_method_indicator(context)
            return

        if self._phase == 'ANCHOR':
            # Highlight the picked path plus the source object(s), so
            # it's clear the anchor click applies to those objects.
            if self._path_obj is not None:
                self._draw_object_edges(context, self._path_obj, (0.3, 0.9, 1.0, 0.5))
            for obj in self._sel_objects:
                self._draw_object_edges(context, obj, (1.0, 1.0, 0.2, 0.95))
            if show_cursor_fx:
                if cur_2d:
                    gfx_snap_marker(cur_2d, self._snap_type)
                cx, cy = cur_2d if cur_2d else (self._mx, self._my)
                draw_crosshair(context, cx, cy, None, show_box=False)
            gfx_text("ARRAY (Path): click the object's anchor point",
                      10, 55, 12, (0.3, 0.9, 1.0, 1.0))
            return

        if show_cursor_fx:
            draw_crosshair(context, self._mx, self._my, None, show_box=False)

        if self._method == 'POLAR':
            center_2d = world_to_screen(context, self._center_pt)
            if center_2d:
                gfx_dot(center_2d, (0.3, 0.9, 1.0, 0.9), 7)
            if center_2d and cur_2d and not self._editing:
                gfx_dashed_line(center_2d, cur_2d, col=self._COL_PREVIEW, dash_len=6, space_len=4, w=1.4)
                if show_cursor_fx:
                    gfx_snap_marker(cur_2d, self._snap_type)
                radius_px = math.hypot(cur_2d[0] - center_2d[0], cur_2d[1] - center_2d[1])
                if radius_px > 2:
                    gfx_circle(center_2d, radius_px, col=(0.37, 0.92, 0.67, 0.35), segs=48, w=1.2)
        elif self._method == 'PATH':
            if self._path_obj is not None:
                self._draw_object_edges(context, self._path_obj, (0.3, 0.9, 1.0, 0.6))
            # Match Rect/Polar's interactive feel: a dot at the
            # reference start point, a dashed guide line out to where
            # the cursor is currently mapped, and a snap marker glued
            # to the path (not the free 3D mouse position) so the
            # cursor visibly rides along the curve while dragging.
            if (self._path_total_len > 1e-9 and self._sel_objects
                    and not self._editing):
                ref_obj = self._sel_objects[0]
                start_pt = self._obj_pivot_world(ref_obj)
                _, start_dist = _closest_point_on_polyline(self._path_points, start_pt)
                start_on_path = _point_at_distance(
                    self._path_points, self._path_cum, self._path_total_len, start_dist)
                start_2d = world_to_screen(context, start_on_path)

                if self._cur_3d is not None:
                    _, cur_dist = _closest_point_on_polyline(self._path_points, self._cur_3d)
                    cur_on_path = _point_at_distance(
                        self._path_points, self._path_cum, self._path_total_len, cur_dist)
                    path_cur_2d = world_to_screen(context, cur_on_path)
                else:
                    path_cur_2d = None

                if start_2d:
                    gfx_dot(start_2d, (0.3, 0.9, 1.0, 0.9), 7)
                if start_2d and path_cur_2d:
                    gfx_dashed_line(start_2d, path_cur_2d, col=self._COL_PREVIEW,
                                     dash_len=6, space_len=4, w=1.4)
                    if show_cursor_fx:
                        gfx_snap_marker(path_cur_2d, self._snap_type)
        elif self._method == 'GRID':
            base_2d = world_to_screen(context, self._base_pt)
            if base_2d:
                gfx_dot(base_2d, (0.3, 0.9, 1.0, 0.9), 7)
            if base_2d and not self._editing:
                # Two separate dashed guides - one along the column
                # direction (+X), one along the row direction (+Y) -
                # so rows/columns visibly stay independent instead of
                # one combined diagonal drag line.
                col_end_2d = world_to_screen(
                    context, self._base_pt + Vector((self._col_spacing, 0.0, 0.0)))
                row_end_2d = world_to_screen(
                    context, self._base_pt + Vector((0.0, self._row_spacing, 0.0)))
                if col_end_2d:
                    gfx_dashed_line(base_2d, col_end_2d, col=self._COL_PREVIEW,
                                     dash_len=6, space_len=4, w=1.4)
                if row_end_2d:
                    gfx_dashed_line(base_2d, row_end_2d, col=self._COL_PREVIEW,
                                     dash_len=6, space_len=4, w=1.4)
                if show_cursor_fx and cur_2d:
                    gfx_snap_marker(cur_2d, self._snap_type)
        else:
            base_2d = world_to_screen(context, self._base_pt)
            if base_2d:
                gfx_dot(base_2d, (0.3, 0.9, 1.0, 0.9), 7)
            if base_2d and cur_2d and not self._editing:
                gfx_dashed_line(base_2d, cur_2d, col=self._COL_PREVIEW, dash_len=6, space_len=4, w=1.4)
                if show_cursor_fx:
                    gfx_snap_marker(cur_2d, self._snap_type)

        if self._editing:
            rows = []
            for field in self._edit_order:
                is_active = field == self._edit_field
                # Only the field actually being typed into should show
                # the raw typed text (self._edit_inp is a single shared
                # buffer, not per-field) - every other row must show its
                # own committed value, or typing "2" for one field made
                # every field's row display "2" too.
                typed = self._edit_inp if is_active else ''
                if field == 'SPACING':
                    val, unit = typed or f"{self._spacing:.4f}", " m"
                elif field == 'COUNT':
                    val, unit = typed or str(self._count), ""
                elif field == 'ANGLE':
                    val = typed or format_angle(context, math.atan2(self._direction.y, self._direction.x))
                    unit = ""
                elif field == 'ROW_COUNT':
                    val, unit = typed or str(self._rows), ""
                elif field == 'COL_COUNT':
                    val, unit = typed or str(self._cols), ""
                elif field == 'ROW_SPACING':
                    val, unit = typed or f"{self._row_spacing:.4f}", " m"
                elif field == 'COL_SPACING':
                    val, unit = typed or f"{self._col_spacing:.4f}", " m"
                else:  # TOTAL_ANGLE
                    val, unit = typed or f"{self._polar_total_angle:.2f}", "\u00b0"
                if is_active and typed:
                    val += "\u258c"
                rows.append((self._SHORT_LABELS[field], val + unit, is_active, False))
            if show_cursor_fx:
                draw_dim_rows(context, self._mx, self._my, rows)
        elif self._method == 'POLAR':
            dirlabel = "CCW" if self._polar_dir > 0 else "CW"
            draw_float_dim(self._mx + 60, self._my,
                            f"N {self._count}  {self._polar_total_angle:.1f}\u00b0  {dirlabel}",
                            active=True, locked=False)
        elif self._method == 'PATH':
            alignlabel = "align" if self._path_align else "keep orient."
            draw_float_dim(self._mx + 60, self._my,
                            f"S {self._spacing:.4f}  N {self._count}  {alignlabel}",
                            active=True, locked=False)
        elif self._method == 'GRID':
            draw_float_dim(self._mx + 60, self._my,
                            f"R {self._rows}\u00d7{self._row_spacing:.3f}  "
                            f"C {self._cols}\u00d7{self._col_spacing:.3f}",
                            active=True, locked=False)
        else:
            draw_float_dim(self._mx + 60, self._my,
                            f"S {self._spacing:.4f}  N {self._count}",
                            active=True, locked=False)

        if self._alt_held:
            self._draw_method_menu(context)
        else:
            self._draw_method_indicator(context)
