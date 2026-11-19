# ─────────────────────────────────────────────────────────────────────
#  operators/tool_edit.py
#  CAD_OT_EditElement  –  in-Sketch-Mode element editing
#
#  State machine (inspired by QuickSnap):
#
#   IDLE
#     MOUSEMOVE          → update hover highlight
#     LMB press
#       on unselected element / new object  → SELECT that element
#       on already-selected element         → → GRAB_PENDING
#     Shift+LMB                             → extend/toggle selection
#     Double-LMB on selected               → → GRAB_PENDING immediately
#     G key (with selection)               → → GRAB_PENDING immediately
#     A                                    → select / deselect all
#     1/2/3                                → change select mode
#     ESC / RMB                            → clear selection / exit
#
#   GRAB_PENDING  (LMB held or G pressed on a selected element)
#     MOUSEMOVE > threshold                → → DRAGGING
#     LMB release before threshold         → back to IDLE (was a click)
#     ESC / RMB                            → back to IDLE, cancel
#
#   DRAGGING
#     MOUSEMOVE   → move selected verts/edges/faces with snap
#     LMB release → commit move, → IDLE
#     ESC / RMB   → revert positions, → IDLE
# ─────────────────────────────────────────────────────────────────────

import bpy
import math
import time
from enum import Enum
from mathutils import Vector

from ..utils.math_utils import world_to_screen, mouse_to_3d, build_snap_cache, apply_snaps
from ..gfx.primitives   import (
    gfx_lines, gfx_loop, gfx_rect_fill, gfx_dot, gfx_text, gfx_tri_fill,
)
from ..gfx.markers      import draw_crosshair, gfx_snap_marker
from .. import state as _st
from ..utils.cursor_util import (
    update_modal_cursor, restore_modal_cursor, cursor_fx_visible, init_modal_cursor,
    guarded_modal,
    handle_ui_handoff,
    reset_tpanel_to_select,
)


# ── state machine ────────────────────────────────────────────────────
class _State(Enum):
    IDLE          = 1
    GRAB_PENDING  = 2   # LMB held / G pressed; waiting for mouse movement
    DRAGGING      = 3   # actively moving elements


# ── colours ──────────────────────────────────────────────────────────
_COL_VERT_IDLE   = (1.00, 0.85, 0.00, 0.85)   # yellow
_COL_VERT_HOVER  = (1.00, 1.00, 0.40, 1.00)   # bright yellow
_COL_VERT_SEL    = (1.00, 0.55, 0.00, 1.00)   # orange  (selected)
_COL_VERT_PEND   = (1.00, 1.00, 1.00, 1.00)   # white   (grab-pending)

_COL_EDGE_IDLE   = (0.55, 0.55, 0.55, 0.45)
_COL_EDGE_HOVER  = (0.20, 0.85, 1.00, 0.95)   # cyan
_COL_EDGE_SEL    = (0.00, 0.70, 1.00, 1.00)   # blue

_COL_FACE_IDLE   = (0.55, 0.55, 0.55, 0.08)
_COL_FACE_HOVER  = (1.00, 0.65, 0.10, 0.22)
_COL_FACE_SEL    = (1.00, 0.55, 0.10, 0.42)

_COL_DRAG_LINE   = (0.40, 1.00, 0.40, 0.70)   # green guide

_GRIP_HALF       = 5     # px half-size for vertex squares
_RAD_VERT        = 14    # px pick radius – vertices
_RAD_EDGE        = 10    # px pick radius – edges
_RAD_FACE        = 30    # px pick radius – face centres
_DRAG_THRESHOLD  = 4     # px mouse travel before GRAB_PENDING → DRAGGING


# ════════════════════════════════════════════════════════════════════
#  Geometry helpers
# ════════════════════════════════════════════════════════════════════

def _dist_pt_seg_2d(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    seg2   = dx * dx + dy * dy
    if seg2 < 1e-9:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _collect_elements(context, obj):
    mw, mesh = obj.matrix_world, obj.data
    verts_2d = []
    for vi, v in enumerate(mesh.vertices):
        s = world_to_screen(context, mw @ v.co)
        if s:
            verts_2d.append((s, vi, mw @ v.co))
    v2d = {vi: s for s, vi, _ in verts_2d}
    edges_2d = []
    for ei, e in enumerate(mesh.edges):
        sa, sb = v2d.get(e.vertices[0]), v2d.get(e.vertices[1])
        if sa and sb:
            edges_2d.append((sa, sb, ei))
    faces_2d = []
    for fi, f in enumerate(mesh.polygons):
        pts = [v2d.get(vi) for vi in f.vertices]
        if all(p for p in pts):
            sc = world_to_screen(context, mw @ f.center)
            if sc:
                faces_2d.append((pts, fi, sc))
    return verts_2d, edges_2d, faces_2d


def _all_mesh_objects(context):
    return [o for o in context.view_layer.objects
            if o.type == 'MESH' and o.visible_get()]


# ════════════════════════════════════════════════════════════════════
#  Operator
# ════════════════════════════════════════════════════════════════════

class CAD_OT_EditElement(bpy.types.Operator):
    """Edit vertices, edges or faces of any mesh object in Sketch Mode"""
    bl_idname  = "cad.edit_element"
    bl_label   = "CAD Edit Element"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, 'cad_settings', None)
        return s is not None and s.sketch_mode

    # ── invoke ────────────────────────────────────────────────────────

    def invoke(self, context, event):
        s = context.scene.cad_settings

        self._mx = event.mouse_region_x
        self._my = event.mouse_region_y

        # State machine
        self._state = _State.IDLE

        # Current edit-target object
        self._target_obj = None

        # Element selection
        self._sel_verts = set()
        self._sel_edges = set()
        self._sel_faces = set()

        # Hover (refreshed every MOUSEMOVE)
        self._hov_obj = None
        self._hov_vi  = None
        self._hov_ei  = None
        self._hov_fi  = None

        # GRAB_PENDING / DRAGGING data
        self._grab_verts      = []     # vert indices to move
        self._drag_orig_vcos  = {}     # {vi: world_co} snapshot at drag start
        self._drag_anchor     = None   # world-space position at LMB press
        self._grab_start_mx   = 0      # screen pos at LMB press (threshold check)
        self._grab_start_my   = 0
        self._snap_cache      = build_snap_cache(context)

        # Double-click timing
        self._last_lmb_press  = 0.0

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL',
        )
        s.active_tool = 'EDIT'
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

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
        s        = context.scene.cad_settings

        # ── global exits ─────────────────────────────────────────────
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            if self._state in {_State.GRAB_PENDING, _State.DRAGGING}:
                self._revert_drag(context)
                self._state = _State.IDLE
            else:
                self._sel_verts.clear()
                self._sel_edges.clear()
                self._sel_faces.clear()
                self.cancel(context)
                return {'CANCELLED'}
            return {'RUNNING_MODAL'}

        # Let mouse wheel and middle-mouse reach Blender (viewport zoom/
        # orbit, Toolbar scroll) instead of being swallowed by this
        # modal's catch-all return.
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'MIDDLEMOUSE', 'TRACKPADPAN'}:
            return {'PASS_THROUGH'}

        # ── select-mode shortcuts ─────────────────────────────────────
        if event.value == 'PRESS' and self._state == _State.IDLE:
            if event.type == 'ONE'   and not event.ctrl and not event.shift:
                s.select_mode = 'VERTEX'; self._clear_sel(); return {'RUNNING_MODAL'}
            if event.type == 'TWO'   and not event.ctrl and not event.shift:
                s.select_mode = 'EDGE';   self._clear_sel(); return {'RUNNING_MODAL'}
            if event.type == 'THREE' and not event.ctrl and not event.shift:
                s.select_mode = 'FACE';   self._clear_sel(); return {'RUNNING_MODAL'}

        # ── A → select / deselect all ─────────────────────────────────
        if (event.type == 'A' and event.value == 'PRESS'
                and self._state == _State.IDLE and self._target_obj):
            self._toggle_all(context, s.select_mode)
            return {'RUNNING_MODAL'}

        # ── G → grab selection (inline drag, no click needed) ────────
        if (event.type == 'G' and event.value == 'PRESS'
                and self._state == _State.IDLE):
            gv = self._sel_to_verts(s.select_mode)
            if gv and self._target_obj:
                self._enter_grab_pending(context, gv)
            return {'RUNNING_MODAL'}

        # ── M → move selected vertices with the full move-tool UX ────
        if (event.type == 'M' and event.value == 'PRESS'
                and self._state == _State.IDLE):
            gv = self._sel_to_verts(s.select_mode)
            if gv and self._target_obj:
                from .. import state as _st_ref
                _st_ref.edit_pending_move = {
                    'obj':   self._target_obj,
                    'verts': gv,
                }
                # Suspend this operator temporarily while edit_move runs;
                # edit_move.cancel() sets active_tool back to 'EDIT'
                # which will restart this operator via the sketch manager.
                self.cancel(context)
                bpy.ops.cad.edit_move('INVOKE_DEFAULT')
            return {'RUNNING_MODAL'}

        # ── MOUSEMOVE ─────────────────────────────────────────────────
        if event.type == 'MOUSEMOVE':
            if self._state == _State.IDLE:
                self._update_hover(context, s.select_mode)

            elif self._state == _State.GRAB_PENDING:
                dx = self._mx - self._grab_start_mx
                dy = self._my - self._grab_start_my
                if math.hypot(dx, dy) >= _DRAG_THRESHOLD:
                    # Mouse moved enough → commit to DRAGGING
                    self._state = _State.DRAGGING
                    self._apply_drag(context)

            elif self._state == _State.DRAGGING:
                self._apply_drag(context)

            return {'RUNNING_MODAL'}

        # ── LMB PRESS ────────────────────────────────────────────────
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            now    = time.time()
            double = (now - self._last_lmb_press) < 0.35
            self._last_lmb_press = now

            if self._state == _State.IDLE:
                # Refresh hover at click position before deciding
                self._update_hover(context, s.select_mode)

                # Check if the click lands on an already-selected element
                on_selected = self._hover_is_selected(s.select_mode)

                if on_selected or double:
                    # Single click on selected OR double-click anywhere
                    # selected → enter GRAB_PENDING
                    gv = self._sel_to_verts(s.select_mode)
                    if gv and self._target_obj:
                        self._enter_grab_pending(context, gv)
                        return {'RUNNING_MODAL'}

                # Plain click on unselected element or object
                self._do_select(context, s.select_mode, event.shift)

            return {'RUNNING_MODAL'}

        # ── LMB RELEASE ──────────────────────────────────────────────
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            if self._state == _State.GRAB_PENDING:
                # Released before moving far enough → treat as a plain click
                # that re-selects without moving anything
                self._state = _State.IDLE

            elif self._state == _State.DRAGGING:
                # Commit the move
                self._commit_drag()
                self._state = _State.IDLE

            return {'RUNNING_MODAL'}

        return {'PASS_THROUGH'}

    # ── cancel / cleanup ──────────────────────────────────────────────

    def cancel(self, context):
        s = context.scene.cad_settings
        if s.active_tool == 'EDIT':
            s.active_tool = 'SELECT'
            reset_tpanel_to_select(self, context)
        restore_modal_cursor(self, context)
        if self._handler is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._handler, 'WINDOW')
            self._handler = None
        if context.area:
            context.area.tag_redraw()

    # ── hover ─────────────────────────────────────────────────────────

    def _update_hover(self, context, select_mode):
        mx, my = self._mx, self._my
        self._hov_obj = self._hov_vi = self._hov_ei = self._hov_fi = None
        best = float('inf')

        # Target first so it has priority over other objects
        candidates = (
            [self._target_obj] +
            [o for o in _all_mesh_objects(context) if o is not self._target_obj]
        ) if self._target_obj else _all_mesh_objects(context)

        for obj in candidates:
            vd, ed, fd = _collect_elements(context, obj)

            if select_mode == 'VERTEX':
                for (sx, sy), vi, _ in vd:
                    d = math.hypot(sx - mx, sy - my)
                    if d < _RAD_VERT and d < best:
                        best = d
                        self._hov_obj, self._hov_vi = obj, vi
                        self._hov_ei = self._hov_fi = None

            elif select_mode == 'EDGE':
                for (ax, ay), (bx, by), ei in ed:
                    d = _dist_pt_seg_2d(mx, my, ax, ay, bx, by)
                    if d < _RAD_EDGE and d < best:
                        best = d
                        self._hov_obj, self._hov_ei = obj, ei
                        self._hov_vi = self._hov_fi = None

            elif select_mode == 'FACE':
                for _, fi, (cx, cy) in fd:
                    d = math.hypot(cx - mx, cy - my)
                    if d < _RAD_FACE and d < best:
                        best = d
                        self._hov_obj, self._hov_fi = obj, fi
                        self._hov_vi = self._hov_ei = None

    def _hover_is_selected(self, select_mode):
        """True when the current hover lands on an element in the active selection."""
        if self._hov_obj is not self._target_obj:
            return False
        if select_mode == 'VERTEX':
            return self._hov_vi is not None and self._hov_vi in self._sel_verts
        if select_mode == 'EDGE':
            return self._hov_ei is not None and self._hov_ei in self._sel_edges
        if select_mode == 'FACE':
            return self._hov_fi is not None and self._hov_fi in self._sel_faces
        return False

    # ── selection ─────────────────────────────────────────────────────

    def _do_select(self, context, select_mode, extend=False):
        # Clicked empty space
        if self._hov_obj is None:
            if not extend:
                self._clear_sel()
                self._target_obj = None
            return

        # Switching to a different object resets selection
        if self._hov_obj is not self._target_obj:
            self._clear_sel()
            self._target_obj = self._hov_obj

        if select_mode == 'VERTEX' and self._hov_vi is not None:
            if extend:
                self._sel_verts.symmetric_difference_update({self._hov_vi})
            else:
                self._sel_verts = {self._hov_vi}
                self._sel_edges.clear(); self._sel_faces.clear()

        elif select_mode == 'EDGE' and self._hov_ei is not None:
            if extend:
                self._sel_edges.symmetric_difference_update({self._hov_ei})
            else:
                self._sel_edges = {self._hov_ei}
                self._sel_verts.clear(); self._sel_faces.clear()

        elif select_mode == 'FACE' and self._hov_fi is not None:
            if extend:
                self._sel_faces.symmetric_difference_update({self._hov_fi})
            else:
                self._sel_faces = {self._hov_fi}
                self._sel_verts.clear(); self._sel_edges.clear()

    def _clear_sel(self):
        self._sel_verts.clear()
        self._sel_edges.clear()
        self._sel_faces.clear()

    def _toggle_all(self, context, select_mode):
        mesh = self._target_obj.data
        if select_mode == 'VERTEX':
            all_v = set(range(len(mesh.vertices)))
            self._sel_verts = set() if self._sel_verts == all_v else all_v
        elif select_mode == 'EDGE':
            all_e = set(range(len(mesh.edges)))
            self._sel_edges = set() if self._sel_edges == all_e else all_e
        elif select_mode == 'FACE':
            all_f = set(range(len(mesh.polygons)))
            self._sel_faces = set() if self._sel_faces == all_f else all_f

    # ── convert current selection to vertex indices for drag ──────────

    def _sel_to_verts(self, select_mode):
        if not self._target_obj:
            return []
        mesh = self._target_obj.data
        if select_mode == 'VERTEX' and self._sel_verts:
            return list(self._sel_verts)
        if select_mode == 'EDGE' and self._sel_edges:
            v = set()
            for ei in self._sel_edges:
                v.update(mesh.edges[ei].vertices)
            return list(v)
        if select_mode == 'FACE' and self._sel_faces:
            v = set()
            for fi in self._sel_faces:
                v.update(mesh.polygons[fi].vertices)
            return list(v)
        return []

    # ── GRAB_PENDING / DRAGGING ───────────────────────────────────────

    def _enter_grab_pending(self, context, vert_indices):
        """Snapshot positions and record start coords; wait for mouse travel."""
        obj  = self._target_obj
        mw   = obj.matrix_world
        mesh = obj.data
        self._grab_verts     = vert_indices
        self._drag_orig_vcos = {vi: (mw @ mesh.vertices[vi].co).copy()
                                for vi in vert_indices}
        self._drag_anchor    = mouse_to_3d(context, self._mx, self._my)
        self._grab_start_mx  = self._mx
        self._grab_start_my  = self._my
        self._snap_cache     = build_snap_cache(context)
        self._state          = _State.GRAB_PENDING

    def _apply_drag(self, context):
        """Move the grabbed vertices relative to the drag anchor (with snap)."""
        obj = self._target_obj
        if not obj or not self._grab_verts:
            return
        raw         = mouse_to_3d(context, self._mx, self._my)
        snapped, _  = apply_snaps(context, raw, self._snap_cache,
                                  snap_filter=_st.selection.get_active_stypes())
        delta       = snapped - self._drag_anchor
        mw_inv      = obj.matrix_world.inverted()
        mesh        = obj.data
        for vi in self._grab_verts:
            orig = self._drag_orig_vcos.get(vi)
            if orig is not None:
                mesh.vertices[vi].co = mw_inv @ (orig + delta)
        mesh.update()

    def _commit_drag(self):
        self._grab_verts     = []
        self._drag_orig_vcos = {}
        self._drag_anchor    = None

    def _revert_drag(self, context):
        """Put vertices back to their pre-drag positions."""
        obj = self._target_obj
        if obj:
            mw_inv = obj.matrix_world.inverted()
            mesh   = obj.data
            for vi, co in self._drag_orig_vcos.items():
                mesh.vertices[vi].co = mw_inv @ co
            mesh.update()
        self._commit_drag()

    # ── draw overlay ─────────────────────────────────────────────────

    def _draw(self, context):
        try:
            s = context.scene.cad_settings
            if not s.sketch_mode:
                return
            if _st.sketch_area_ref is None or context.area != _st.sketch_area_ref:
                return

            mx, my      = self._mx, self._my
            mode        = s.select_mode
            target      = self._target_obj
            is_pending  = self._state == _State.GRAB_PENDING
            is_dragging = self._state == _State.DRAGGING

            for obj in _all_mesh_objects(context):
                is_tgt = (obj is target)
                vd, ed, fd = _collect_elements(context, obj)

                if mode == 'VERTEX':
                    proximity_only = bool(getattr(s, 'cursor_grip_proximity_only', True))
                    reveal_px      = max(1, int(getattr(s, 'cursor_grip_reveal_px', 40)))
                    reveal_sq      = reveal_px * reveal_px

                    snap_filter = _st.selection.get_active_stypes()
                    show_vertex   = snap_filter is None or (
                        'VERTEX' in snap_filter or 'ENDPOINT' in snap_filter
                    )
                    show_midpoint = snap_filter is not None and 'MIDPOINT' in snap_filter

                    for (ax, ay), (bx, by), _ in ed:
                        a = 0.35 if is_tgt else 0.10
                        gfx_lines([(ax, ay), (bx, by)], (0.45, 0.45, 0.45, a), 1.0)

                    for (sx, sy), vi, _ in vd:
                        is_sel  = is_tgt and vi in self._sel_verts and show_vertex
                        is_grab = is_sel and (is_pending or is_dragging)
                        is_hov  = False  # proximity hover highlight disabled

                        if is_grab:
                            col, fill, h = _COL_VERT_PEND, (*_COL_VERT_PEND[:3], 0.25), _GRIP_HALF + 2
                        elif is_hov:
                            col, fill, h = _COL_VERT_HOVER, (*_COL_VERT_HOVER[:3], 0.18), _GRIP_HALF + 1
                        else:
                            # Idle/endpoint vertex squares permanently
                            # disabled, on target and non-target objects.
                            continue

                        gfx_rect_fill(sx - h, sy - h, h * 2, h * 2, fill)
                        gfx_loop(
                            [(sx-h, sy-h),(sx+h, sy-h),(sx+h, sy+h),(sx-h, sy+h)],
                            col, 1.8 if (is_sel or is_hov) else 1.1,
                        )

                    # Midpoint candidate squares, only when Midpoint is
                    # the active Object Snap mode. Only drawn for the
                    # target object, same as idle vertex squares above.
                    if show_midpoint and is_tgt:
                        h = _GRIP_HALF
                        for (ax, ay), (bx, by), _ in ed:
                            sx, sy = (ax + bx) * 0.5, (ay + by) * 0.5
                            if proximity_only:
                                dx, dy = sx - mx, sy - my
                                if (dx * dx + dy * dy) > reveal_sq:
                                    continue
                            gfx_rect_fill(sx - h, sy - h, h * 2, h * 2,
                                          (*_COL_VERT_IDLE[:3], 0.08))
                            gfx_loop(
                                [(sx-h, sy-h),(sx+h, sy-h),(sx+h, sy+h),(sx-h, sy+h)],
                                _COL_VERT_IDLE, 1.1,
                            )

                elif mode == 'EDGE':
                    for (ax, ay), (bx, by), ei in ed:
                        is_sel  = is_tgt and ei in self._sel_edges
                        is_grab = is_sel and (is_pending or is_dragging)
                        is_hov  = False  # proximity hover highlight disabled
                        if is_grab:
                            gfx_lines([(ax, ay), (bx, by)], _COL_VERT_PEND, 3.5)
                        elif is_sel:
                            gfx_lines([(ax, ay), (bx, by)], _COL_EDGE_SEL,  3.0)
                        elif is_hov:
                            gfx_lines([(ax, ay), (bx, by)], _COL_EDGE_HOVER, 2.5)
                        elif is_tgt:
                            gfx_lines([(ax, ay), (bx, by)], _COL_EDGE_IDLE,  1.2)
                        else:
                            gfx_lines([(ax, ay), (bx, by)], (0.38, 0.38, 0.38, 0.18), 1.0)
                    # Vertex endpoint dots permanently disabled in Edge mode.

                elif mode == 'FACE':
                    for (ax, ay), (bx, by), _ in ed:
                        a = 0.50 if is_tgt else 0.13
                        gfx_lines([(ax, ay), (bx, by)], (0.58, 0.58, 0.58, a), 1.0)
                    for pts, fi, sc in fd:
                        is_sel  = is_tgt and fi in self._sel_faces
                        is_grab = is_sel and (is_pending or is_dragging)
                        is_hov  = (obj is self._hov_obj and fi == self._hov_fi
                                   and self._state == _State.IDLE)
                        col = (_COL_VERT_PEND  if is_grab else
                               _COL_FACE_SEL   if is_sel  else
                               _COL_FACE_HOVER if is_hov  else
                               _COL_FACE_IDLE  if is_tgt  else
                               (0.38, 0.38, 0.38, 0.03))
                        if is_sel or is_hov or is_tgt or is_grab:
                            cx = sum(p[0] for p in pts) / len(pts)
                            cy = sum(p[1] for p in pts) / len(pts)
                            for i in range(len(pts)):
                                gfx_tri_fill((cx, cy), pts[i], pts[(i+1)%len(pts)], col)
                            if is_sel or is_hov or is_grab:
                                gfx_loop(pts, (*col[:3], 1.0), 1.8)

            # Drag anchor line
            if is_dragging and self._drag_anchor:
                a2 = world_to_screen(context, self._drag_anchor)
                if a2:
                    gfx_lines([a2, (mx, my)], _COL_DRAG_LINE, 1.2)

            # Crosshair - stands in for "the cursor", so only draw it
            # while the real OS cursor is actually hidden (mouse over
            # the usable viewport).
            if cursor_fx_visible(self):
                draw_crosshair(context, mx, my, axis_lock=None, show_box=False)

            # HUD
            mode_str = s.select_mode
            n_sel = (len(self._sel_verts) if mode_str == 'VERTEX' else
                     len(self._sel_edges) if mode_str == 'EDGE'   else
                     len(self._sel_faces))

            if is_dragging:
                hint = "DRAGGING — move mouse to reposition  |  LMB=confirm  ESC/RMB=cancel"
            elif is_pending:
                hint = "GRAB READY — move mouse to drag  |  release without moving=cancel"
            elif n_sel and target:
                hint = (f"EDIT [{mode_str}]  {n_sel} selected  |  "
                        "Click selected=grab&drag  Shift=extend  G=grab  M=move  A=all  "
                        "1/2/3=mode  ESC=exit")
            elif target:
                hint = (f"EDIT [{mode_str}]  Click to select  |  "
                        "Shift=extend  A=all  1/2/3=mode  ESC=exit")
            else:
                hint = f"EDIT [{mode_str}]  Click any object to start  |  1/2/3=mode  ESC=exit"

            gfx_text(hint, 12, 22, 11, (0.92, 0.92, 0.92, 0.78))

        except Exception as e:
            import traceback
            traceback.print_exc()
