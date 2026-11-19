# ─────────────────────────────────────────────────────────────────────
#  operators/sketch_mode.py
# ─────────────────────────────────────────────────────────────────────

import bpy
import math
from mathutils import Vector
from .. import state as _st
from ..ui.tool_shelf import apply_ortho_now, restore_view
from ..ui import tpanel_filter as _tpanel
from ..ui import cad_tpanel_tools as _cad_tools
from ..gfx import cursor as _cursor_gfx
from ..utils.cursor_util import (
    mouse_in_usable_viewport, handle_ui_handoff,
    update_modal_cursor, restore_modal_cursor,
)
from ..gfx.markers import draw_crosshair, gfx_snap_marker, draw_snap_indicator, draw_selected_snap_points
from ..utils.math_utils import (
    build_snap_cache, find_hover_point, find_hover_point_world, mouse_to_3d, apply_snaps, world_to_screen,
    point_in_polygon, get_active_select_gesture,
)
from ..gfx.primitives import (
    gfx_loop, gfx_rect_fill, gfx_lines,
    gfx_text, gfx_rounded_rect_fill, gfx_rounded_rect_loop,
    gfx_circle, gfx_dot, gfx_tri_fill,
)


# ════════════════════════════════════════════════════════════════════
#  ONE-CLICK TOOL ACTIVATION
#
#  Selecting a tool in the T-panel only changes the *active
#  WorkSpaceTool* - it does not start the modal draw operator.
#  Normally the operator only starts on the next LEFTMOUSE press inside
#  the viewport, and that first click is "consumed" just to arm the
#  tool (hide the cursor, start the draw handler) without placing a
#  point - requiring a throw-away click before drawing can begin.
#
#  To fix this, the already-running CAD_OT_SketchModeManager modal
#  watches for the active T-panel tool changing to one of these and
#  immediately starts the matching draw operator itself, as soon as
#  the mouse is back over the viewport. This only ever happens from
#  idle SELECT - once a tool is actually running, picking a different
#  tool button has no effect until the running tool finishes or is
#  cancelled (Esc / right-click), same as stock Blender.
# ════════════════════════════════════════════════════════════════════

_AUTO_INVOKE_TOOLS = {k: v[0] for k, v in _st.AUTO_INVOKE_TOOLS.items()}


def _get_active_tool_idname(context):
    return _st.get_active_tool_idname(context)


# ════════════════════════════════════════════════════════════════════
#  ENTER
# ════════════════════════════════════════════════════════════════════

class CAD_OT_EnterSketchMode(bpy.types.Operator):
    bl_idname      = "cad.enter_sketch_mode"
    bl_label       = "Sketch Mode"
    bl_description = (
        "Enter CAD Sketch Mode\n"
        "Locks view to Top Orthographic automatically"
    )

    @classmethod
    def poll(cls, context):
        return (
            context.area is not None
            and context.area.type == 'VIEW_3D'
            and not getattr(
                context.scene.cad_settings, 'sketch_mode', False
            )
        )

    def execute(self, context):
        # Remember which mode we came from so RETURN can go back to it.
        _st.previous_mode = context.mode   # e.g. 'OBJECT', 'EDIT_MESH', 'SCULPT'

        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        bpy.ops.cad.sketch_mode_manager('INVOKE_DEFAULT')
        print("[CAD] EnterSketchMode executed")
        return {'FINISHED'}


# ════════════════════════════════════════════════════════════════════
#  EXIT
# ════════════════════════════════════════════════════════════════════

class CAD_OT_ExitSketchMode(bpy.types.Operator):
    bl_idname      = "cad.exit_sketch_mode"
    bl_label       = "Exit to Object Mode"
    bl_description = "Return to Object Mode and restore previous view"

    def execute(self, context):
        restore_view(context)

        if _st.sketch_manager_ref is not None:
            _st.sketch_manager_ref._do_cancel(context)
        else:
            s = context.scene.cad_settings
            s.sketch_mode = False
            s.active_tool = ""

        if context.area:
            context.area.tag_redraw()

        return {'FINISHED'}


# ════════════════════════════════════════════════════════════════════
#  MANAGER
# ════════════════════════════════════════════════════════════════════

class CAD_OT_SketchModeManager(bpy.types.Operator):
    bl_idname  = "cad.sketch_mode_manager"
    bl_label   = "Sketch Mode Manager"
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        _st.sketch_manager_ref = self
        _st.sketch_area_ref    = context.area

        s = context.scene.cad_settings
        s.sketch_mode = True
        s.active_tool = "SELECT"

        # Track the active T-panel tool so we can auto-invoke the
        # matching draw operator the instant the user picks one while
        # idle in SELECT - see _check_tool_activation().
        self._last_tool_idname    = _get_active_tool_idname(context)
        self._pending_tool_invoke = None

        # Most recently activated drawing/editing tool (e.g. "LINE",
        # "MOVE"), so a lone Enter press in idle SELECT mode can jump
        # straight back into it - see the 'RET' handler in modal() and
        # _activate_tool().
        self._last_used_tool = None

        # Remember whatever object was selected *before* Sketch Mode
        # started (e.g. a pre-existing Cube or CAD line the user
        # clicked on) so newly-drawn shapes can be linked into the
        # same collection(s) as it - see _update_sketch_ref_obj().
        _st.sketch_ref_obj = None
        self._update_sketch_ref_obj(context)

        # ── AutoCAD-style idle cursor state ─────────────────────────
        # See _draw_cursor_overlay() / _update_cursor_visibility() /
        # _on_tool_transition() for how these are used.
        self._mx, self._my         = event.mouse_region_x, event.mouse_region_y
        self._in_viewport           = False
        self._cursor_hidden       = False
        self._lmb_down               = False
        self._prev_active_tool       = s.active_tool   # "SELECT"
        self._overlay_error_logged   = False

        # ── SELECT-tool vertex selection + drag ──────────────────────
        # _grip_sel: {obj_name: set(vi)} — vertices currently selected
        #            (shown in green).
        # _vdrag_*: drag state for moving the whole selection together.
        # Confirmed by a single LMB click after dragging starts.
        self._grip_sel         = {}            # {obj.name: set[vi]}
        self._vdrag_state      = None          # None | 'DRAGGING'
        self._vdrag_obj        = None          # target mesh object
        self._vdrag_orig_vcos  = {}            # {vi: world_co} snapshot
        self._vdrag_anchor     = None          # world-space mouse at drag start
        self._vdrag_snap_cache = {}

        # ── Snap-point selection gesture state (Box/Lasso/Circle) ─────
        # Selection of snap points (per the active Alt-menu snap mode)
        # is separate from vertex-grip selection above. 'BOX' / 'CIRCLE'
        # / 'LASSO' - which multi-select gesture is currently armed or
        # in progress. None = plain click-select only.
        self._snap_select_mode        = None
        self._snap_select_drag        = False
        self._snap_select_start       = None   # (x, y) screen, box select
        self._snap_select_additive    = False  # Shift held at drag start
        self._snap_select_lasso_pts   = []      # [(x, y), ...] screen
        self._snap_select_circle_r    = 25.0    # px
        self._last_lmb_press   = 0.0           # for double-click detection
        try:
            self._select_snap_cache = build_snap_cache(context)
        except Exception as e:
            print(f"[CAD] cursor overlay: initial snap cache build failed: {e}")
            self._select_snap_cache = {'VERTEX': [], 'MIDPOINT': [], 'CENTER': []}
        _st.snap_cache_dirty = False
        self._last_scene_signature = self._compute_scene_signature(context)

        # A fresh Sketch Mode session should never carry over point
        # selections/OTRACK references from a previous session - if
        # any of that geometry was deleted in the meantime (in Object
        # Mode, via the Outliner, etc.) their markers would otherwise
        # keep being drawn forever with nothing left to back them.
        _st.selection.selected_snap_points = []
        _st.selection.tracked_points        = []

        self._cursor_handler = None
        try:
            self._cursor_handler = bpy.types.SpaceView3D.draw_handler_add(
                self._draw_cursor_overlay, (context,), 'WINDOW', 'POST_PIXEL'
            )
        except Exception as e:
            print(f"[CAD] cursor overlay: failed to add draw handler: {e}")

        # Ensure the T-panel is visible, then strip it down to
        # Select Box using _tools list surgery.
        if context.space_data:
            context.space_data.show_region_toolbar = True
        _tpanel.hide_non_cad_tools()
        _cad_tools.show()      # inject Line / Circle / Rect / Move / Stretch / Rotate / Copy / Mirror / Trim into T-panel

        apply_ortho_now(context)
        context.area.tag_redraw()
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _do_cancel(self, context):
        # ── Tear down the idle-cursor overlay first, regardless of which
        #    state we're exiting from, so Blender's window cursor and
        #    draw-handler list are never left in a stale state. ──────────
        handler = getattr(self, '_cursor_handler', None)
        if handler is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(handler, 'WINDOW')
            except ValueError:
                pass
            self._cursor_handler = None

        restore_modal_cursor(self, context)

        restore_view(context)

        s = context.scene.cad_settings
        s.sketch_mode = False
        s.active_tool = ""

        # Restore all T-panel tools that were hidden during Sketch Mode.
        _cad_tools.hide()      # remove Line / Circle / Rect / Move / Stretch / Rotate / Copy / Mirror / Trim first
        _tpanel.restore_all_tools()

        # ── Restore the Blender interaction mode that was active before ──────
        # Map context.mode strings → bpy.ops.object.mode_set() mode values
        _MODE_MAP = {
            'OBJECT':         'OBJECT',
            'EDIT_MESH':      'EDIT',
            'EDIT_CURVE':     'EDIT',
            'EDIT_SURFACE':   'EDIT',
            'EDIT_TEXT':      'EDIT',
            'EDIT_ARMATURE':  'EDIT',
            'EDIT_METABALL':  'EDIT',
            'EDIT_LATTICE':   'EDIT',
            'SCULPT':         'SCULPT',
            'PAINT_VERTEX':   'VERTEX_PAINT',
            'PAINT_WEIGHT':   'WEIGHT_PAINT',
            'PAINT_TEXTURE':  'TEXTURE_PAINT',
            'POSE':           'POSE',
            'PARTICLE':       'PARTICLE_EDIT',
        }
        prev = _st.previous_mode
        if prev and prev != 'OBJECT':
            target = _MODE_MAP.get(prev)
            if target and context.active_object is not None:
                try:
                    bpy.ops.object.mode_set(mode=target)
                    print(f"[CAD] RETURN: restored mode '{target}' (was '{prev}')")
                except Exception as e:
                    print(f"[CAD] RETURN: could not restore mode '{target}': {e}")
        _st.previous_mode = None

        _st.sketch_manager_ref = None
        _st.sketch_area_ref    = None
        _st.sketch_ref_obj     = None
        _st.selection.reset()   # clear hover/selection highlights on exit
        _st.kept_measurements.clear()   # kept Measure results don't outlive the Sketch Mode session

        if context.area:
            context.area.tag_redraw()

    def cancel(self, context):
        self._do_cancel(context)

    def modal(self, context, event):
        if not context.area:
            self._do_cancel(context)
            return {'CANCELLED'}

        # Sticky navigation-gizmo hand-off - see
        # utils/cursor_util.handle_ui_handoff's docstring for why a
        # single-press check isn't enough for an orbit/pan/zoom drag.
        # Checked before anything else so a drag in progress is never
        # reinterpreted as a box-select/snap-pick as it crosses back
        # over the "usable" viewport partway through.
        if handle_ui_handoff(self, context, event):
            return {'PASS_THROUGH'}

        context.area.tag_redraw()
        s = context.scene.cad_settings

        if not s.sketch_mode:
            self._do_cancel(context)
            return {'CANCELLED'}

        # ── Idle-cursor bookkeeping ──────────────────────────────────
        # Track whether the mouse is currently over the 3D viewport's
        # own *usable* drawing surface (where region_data / crosshair
        # drawing is valid) and remember its position there for
        # _draw_cursor_overlay(). "Usable" excludes not just the
        # T-panel/header/N-panel (see mouse_in_usable_viewport's
        # docstring for why a context.region check alone can't detect
        # those) but also the viewport navigation-gizmo corner, so a
        # click there falls through to Blender's own gizmo handling
        # instead of being swallowed as a box-select/snap-pick below.
        self._in_viewport = (
            context.region is not None
            and context.region.type == 'WINDOW'
            and context.region_data is not None
            and mouse_in_usable_viewport(context, event)
        )
        if self._in_viewport:
            self._mx, self._my = event.mouse_region_x, event.mouse_region_y

        # Track LMB state - drives the "object selection mode" cursor
        # (pick box only) while SELECT is the active tool.
        if event.type == 'LEFTMOUSE':
            self._lmb_down = (event.value == 'PRESS')

        # Keep state.sketch_ref_obj up to date with whatever object is
        # currently selected while the user is in SELECT (idle) mode,
        # so a draw tool started right afterwards knows which
        # collection ("layer") the finished shape should join.
        if s.active_tool == 'SELECT':
            self._update_sketch_ref_obj(context)

        # Detect cad_settings.active_tool transitions (a draw/move/slice
        # tool starting or finishing) so the idle-cursor bookkeeping -
        # snap cache, cursor-hidden flag, etc. - stays correct.
        if s.active_tool != self._prev_active_tool:
            self._on_tool_transition(context, self._prev_active_tool, s.active_tool)
            self._prev_active_tool = s.active_tool

        # Independent of tool transitions: any newly-created/modified
        # CAD geometry (e.g. a sketch that was just finished) marks
        # the shared snap cache dirty. Catch that immediately here so
        # the idle AutoSnap hover always sees it on the very next
        # tick, rather than depending on the transition check above
        # having already fired for this exact frame.
        if _st.snap_cache_dirty:
            try:
                self._select_snap_cache = build_snap_cache(context)
            except Exception as e:
                print(f"[CAD] cursor overlay: snap cache rebuild failed: {e}")
            _st.snap_cache_dirty = False
            self._purge_stale_snap_selection()

        # Deleting an object (Object Mode delete, Outliner, X-key
        # outside this modal, etc.) never went through
        # create_cad_object() and so never set snap_cache_dirty -
        # meaning the old snap cache (and any selected/tracked points
        # sourced from it) kept referencing geometry that no longer
        # exists, leaving their markers stuck on screen forever. A
        # cheap per-tick signature check catches deletions too.
        sig = self._compute_scene_signature(context)
        if sig != self._last_scene_signature:
            self._last_scene_signature = sig
            try:
                self._select_snap_cache = build_snap_cache(context)
            except Exception as e:
                print(f"[CAD] cursor overlay: snap cache rebuild failed: {e}")
            self._purge_stale_snap_selection()
            context.area.tag_redraw()

        # Hide/restore the OS cursor for the idle SELECT state. Active
        # draw/move/slice tools manage their own cursor visibility.
        self._update_cursor_visibility(context, event)

        # One-click tool activation: if the user just selected a tool
        # from the T-panel, start its modal operator as soon as the
        # mouse re-enters the viewport.
        self._check_tool_activation(context)

        # Re-enforce ortho every frame
        rd = context.region_data
        if rd is not None and rd.view_perspective != 'ORTHO':
            rd.view_perspective = 'ORTHO'
            print("[CAD] re-enforced ORTHO in modal")

        # Track ALT key for snap-mode box display
        if event.type == 'LEFT_ALT' or event.type == 'RIGHT_ALT':
            _st.selection.alt_mode_active = (event.value == 'PRESS')

        # Keyboard shortcuts (no modifiers)
        if event.value == 'PRESS' and not (
            event.ctrl or event.shift or event.alt
        ):
            shortcut = {
                'W': 'SELECT',
                'L': 'LINE',
                'C': 'CIRCLE',
                'R': 'RECT',
                'G': 'MOVE',
                'X': 'SLICE',
            }
            if event.type in shortcut:
                self._activate_tool(context, shortcut[event.type])
                return {'RUNNING_MODAL'}

            if event.type in {'RET', 'NUMPAD_ENTER'} and s.active_tool == 'SELECT' and self._last_used_tool:
                self._activate_tool(context, self._last_used_tool)
                return {'RUNNING_MODAL'}

            if event.type == 'ONE':
                s.select_mode = 'VERTEX'; _st.selection.hover_priority = 'VERTEX'; return {'RUNNING_MODAL'}
            if event.type == 'TWO':
                s.select_mode = 'EDGE';   _st.selection.hover_priority = 'EDGE';   return {'RUNNING_MODAL'}
            if event.type == 'THREE':
                s.select_mode = 'FACE';   _st.selection.hover_priority = 'FACE';   return {'RUNNING_MODAL'}
            if event.type == 'FOUR':
                _st.selection.hover_priority = 'AUTO'; return {'RUNNING_MODAL'}

        # ALT + key  →  toggle a running Object Snap's membership in the
        # active set. AutoCAD-style: Alt+A then Alt+M leaves both
        # Endpoint and Midpoint active simultaneously - pressing either
        # again un-checks just that one. This is the *only* path that
        # can result in more than one active snap type; scroll wheel,
        # number keys, and HUD click all still replace the selection
        # (see toggle_snap_type vs toggle_snap_membership in state.py).
        if event.value == 'PRESS' and event.alt and not event.ctrl and not event.shift:
            _letter_to_snap = {m[0]: m[2] for m in _st.SelectionState.SNAP_MODES}
            if event.type in _letter_to_snap:
                _st.SelectionState.toggle_snap_membership(_letter_to_snap[event.type])
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}

        # F3  →  master Running Object Snap on/off, exactly like AutoCAD.
        # The active type in active_snap_types is remembered even
        # while OSNAP is switched off.
        if event.type == 'F3' and event.value == 'PRESS':
            _st.SelectionState.osnap_enabled = not _st.SelectionState.osnap_enabled
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        # Alt + mouse wheel → switch the active running snap type,
        # cycling through SNAP_MODES in order.
        if event.type in {'WHEELUPMOUSE','WHEELDOWNMOUSE'} and event.value == 'PRESS' and event.alt:
            modes=[m[2] for m in _st.SelectionState.SNAP_MODES]
            if modes:
                try:
                    idx=modes.index(_st.selection.snap_mode)
                except ValueError:
                    idx=0
                step=-1 if event.type=='WHEELUPMOUSE' else 1
                _st.SelectionState.toggle_snap_type(modes[(idx+step)%len(modes)])
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}

        # LMB click while expanded → select the hovered snap row
        if (event.type == 'LEFTMOUSE' and event.value == 'PRESS'
                and _st.selection.alt_mode_active):
            hit = self._snap_box_hit_row(context, self._mx, self._my)
            if hit is not None:
                _st.SelectionState.toggle_snap_type(hit)
                return {'RUNNING_MODAL'}

        # Number keys 1-9,0 → select snap mode by position in display list
        if event.value == 'PRESS' and not event.ctrl and not event.shift and not event.alt:
            _num_map = {
                'ONE': 0, 'TWO': 1, 'THREE': 2, 'FOUR': 3, 'FIVE': 4,
                'SIX': 5, 'SEVEN': 6, 'EIGHT': 7, 'NINE': 8, 'ZERO': 9,
            }
            if event.type in _num_map:
                mode_map   = {m[2]: m for m in _st.SelectionState.SNAP_MODES}
                disp_modes = [mode_map[k] for k in
                              CAD_OT_SketchModeManager._SB_DISPLAY_MODES
                              if k in mode_map]
                idx = _num_map[event.type]
                if idx < len(disp_modes):
                    _st.SelectionState.toggle_snap_type(disp_modes[idx][2])
                    return {'RUNNING_MODAL'}

            if event.type == 'TAB':
                return {'RUNNING_MODAL'}  # block Tab → Edit Mode while CAD mode is active

            if event.type == 'ESC' and s.active_tool == 'SELECT':
                if self._snap_select_mode is not None:
                    # Cancel an armed/in-progress Box, Circle, or Lasso
                    # select gesture first.
                    self._snap_select_mode      = None
                    self._snap_select_drag      = False
                    self._snap_select_lasso_pts = []
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
                if _st.selection.selected_snap_points:
                    # Then clear any selected snap points.
                    _st.selection.selected_snap_points = []
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
                bpy.ops.cad.exit_sketch_mode()
                return {'RUNNING_MODAL'}

        # ── SELECT-tool vertex selection + drag ──────────────────────
        # Stay in Sketch/Object mode here: vertex picking is screen-space
        # projection only and must never activate Edit Mode.
        if s.active_tool == 'SELECT':

            # ── Hover update (runs every MOUSEMOVE) ──────────────────
            if event.type == 'MOUSEMOVE' and self._in_viewport:
                self._update_hover_selection(context)

            # Advance hover animation clock (drives snap glow pulse)
            if event.type == 'MOUSEMOVE':
                import time as _time
                dt = getattr(self, '_last_mouse_t', 0.0)
                now = _time.monotonic()
                _st.SelectionState.tick_hover(now - dt if dt else 0.0)
                self._last_mouse_t = now
                if context.area:
                    context.area.tag_redraw()

            # ── Snap-point selection: Box / Lasso / Circle select ────
            # Operates on whichever snap points match the ALT-menu's
            # active snap_mode (Endpoint, Midpoint, Center, ...) - the
            # same candidate set used for the AutoSnap hover indicator.
            # Selected points are stored in
            # _st.selection.selected_snap_points and drawn as green
            # squares by _draw_snap_point_selection(). Which gesture
            # (Box/Circle/Lasso) an empty-space drag uses is read
            # directly from the 3D viewport toolbar's own active tool
            # (get_active_select_gesture) - not a separate/duplicate
            # setting of our own.

            # Cancel an armed/in-progress Box or Circle select (right-click;
            # Esc is handled earlier, with priority over exiting Sketch Mode)
            if (self._snap_select_mode in {'BOX', 'CIRCLE'}
                    and event.type == 'RIGHTMOUSE' and event.value == 'PRESS'):
                self._snap_select_mode = None
                self._snap_select_drag = False
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}

            # Circle select radius (scroll wheel while armed)
            if (self._snap_select_mode == 'CIRCLE'
                    and event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}
                    and event.value == 'PRESS'):
                delta = 5.0 if event.type == 'WHEELUPMOUSE' else -5.0
                self._snap_select_circle_r = max(5.0, self._snap_select_circle_r + delta)
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}

            # Box select drag
            if self._snap_select_mode == 'BOX':
                if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                    self._snap_select_start    = (self._mx, self._my)
                    self._snap_select_drag     = True
                    self._snap_select_additive = event.shift
                    return {'RUNNING_MODAL'}
                if event.type == 'MOUSEMOVE' and self._snap_select_drag:
                    # Apply live so the selection updates continuously
                    # while dragging, not only once on release.
                    self._apply_box_select(
                        context, self._snap_select_start,
                        (self._mx, self._my), additive=self._snap_select_additive,
                    )
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
                if (event.type == 'LEFTMOUSE' and event.value == 'RELEASE'
                        and self._snap_select_drag):
                    self._apply_box_select(
                        context, self._snap_select_start,
                        (self._mx, self._my), additive=self._snap_select_additive,
                    )
                    self._snap_select_drag = False
                    self._snap_select_mode = None
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}

            # Circle select paint (hold LMB and move to keep selecting)
            if self._snap_select_mode == 'CIRCLE':
                if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                    self._snap_select_drag = True
                    self._apply_circle_select(
                        context, self._mx, self._my,
                        self._snap_select_circle_r, subtract=event.shift,
                    )
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
                if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                    self._snap_select_drag = False
                    return {'RUNNING_MODAL'}
                if event.type == 'MOUSEMOVE':
                    if self._snap_select_drag:
                        self._apply_circle_select(
                            context, self._mx, self._my,
                            self._snap_select_circle_r, subtract=event.shift,
                        )
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}

            # Lasso select drag (armed via the toolbar's active tool -
            # see the empty-space click handler below)
            if self._snap_select_mode == 'LASSO':
                if event.type == 'MOUSEMOVE' and self._snap_select_drag:
                    self._snap_select_lasso_pts.append((self._mx, self._my))
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
                if event.type == 'RIGHTMOUSE' and event.value == 'PRESS':
                    self._snap_select_mode      = None
                    self._snap_select_drag      = False
                    self._snap_select_lasso_pts = []
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
                if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                    self._apply_lasso_select(
                        context, self._snap_select_lasso_pts, additive=event.shift,
                    )
                    self._snap_select_mode      = None
                    self._snap_select_drag      = False
                    self._snap_select_lasso_pts = []
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}

            # Plain click on a single snap point (matches the active
            # Alt-menu snap_mode) toggles/replaces the selection. If the
            # click is on empty space instead, automatically start a Box
            # select drag (no need to press B first) - unless there's a
            # legacy vertex/edge/face grip right there, in which case
            # that existing pick/drag behaviour takes priority.
            if (event.type == 'LEFTMOUSE' and event.value == 'PRESS'
                    and self._in_viewport and self._vdrag_state != 'DRAGGING'
                    and self._snap_select_mode is None):
                snap_hit = find_hover_point(
                    context, self._mx, self._my, self._select_snap_cache,
                    s.cursor_aperture_px,
                    snap_filter=_st.selection.get_active_stypes(),
                )
                if snap_hit is not None:
                    # snap_hit[0] is a 2D screen pos from find_hover_point;
                    # re-resolve to the matching world-space point so we
                    # store/compare in world space, not screen space.
                    world_pt = self._resolve_snap_world_point(
                        context, snap_hit[0], snap_hit[1],
                    )
                    if world_pt is not None:
                        key = tuple(round(c, 6) for c in world_pt)
                        sel = _st.selection.selected_snap_points
                        if event.shift:
                            if key in sel:
                                sel.remove(key)
                            else:
                                sel.append(key)
                        else:
                            _st.selection.selected_snap_points = [key]
                        context.area.tag_redraw()
                        return {'RUNNING_MODAL'}
                elif self._pick_vertex_for_select(context) is None:
                    # Truly empty space (no snap point, no legacy grip):
                    # start whichever multi-select gesture is currently
                    # active in the 3D viewport toolbar (Box/Circle/Lasso
                    # Select), so each tool actually does its own
                    # distinct thing instead of everything defaulting to
                    # Box. A plain click with no real movement still
                    # naturally ends up selecting nothing (a zero-area
                    # box, a zero-radius circle sweep, or a degenerate
                    # lasso), which - combined with non-additive
                    # replacement - also handles "click on empty
                    # deselects".
                    tool = get_active_select_gesture(context)
                    if tool == 'CIRCLE':
                        self._snap_select_mode = 'CIRCLE'
                        self._snap_select_drag = True
                        self._apply_circle_select(
                            context, self._mx, self._my,
                            self._snap_select_circle_r, subtract=event.shift,
                        )
                    elif tool == 'LASSO':
                        self._snap_select_mode      = 'LASSO'
                        self._snap_select_drag      = True
                        self._snap_select_lasso_pts = [(self._mx, self._my)]
                    else:  # 'BOX'
                        self._snap_select_mode     = 'BOX'
                        self._snap_select_start    = (self._mx, self._my)
                        self._snap_select_drag     = True
                        self._snap_select_additive = event.shift
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}



            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                # Confirm an in-progress drag
                if self._vdrag_state == 'DRAGGING':
                    self._vdrag_commit(context)
                    return {'RUNNING_MODAL'}

                hit = self._pick_vertex_for_select(context) if self._in_viewport else None

                # ── SelectionState click handling ────────────────────
                if hit is not None:
                    obj, vi, _ = hit
                    entity = ('V', obj.name, vi)
                    if event.shift:
                        sel = _st.selection.selected_entities
                        if entity in sel:
                            sel.remove(entity)
                        else:
                            sel.append(entity)
                    else:
                        _st.selection.selected_entities = [entity]
                    _st.selection.active_entity = entity

                elif not event.shift:
                    # Click on empty space → clear selection
                    _st.selection.selected_entities = []
                    _st.selection.active_entity = None

                if hit is not None:
                    # LMB on a grip vertex — select / deselect it
                    obj, vi, _ = hit
                    if event.shift:
                        sel = self._grip_sel.setdefault(obj.name, set())
                        if vi in sel:
                            sel.discard(vi)
                        else:
                            sel.add(vi)
                        if not sel:
                            del self._grip_sel[obj.name]
                    else:
                        self._grip_sel = {obj.name: {vi}}
                    return {'RUNNING_MODAL'}

                else:
                    # Clicked empty space
                    if not event.shift:
                        self._grip_sel.clear()
                    # fall through to PASS_THROUGH so Blender handles
                    # object selection as normal

            if event.type == 'MOUSEMOVE' and self._vdrag_state == 'DRAGGING':
                self._vdrag_apply(context)
                return {'RUNNING_MODAL'}

            if (event.type in {'ESC', 'RIGHTMOUSE'}
                    and event.value == 'PRESS'
                    and self._vdrag_state == 'DRAGGING'):
                self._vdrag_cancel(context)
                return {'RUNNING_MODAL'}

            # ESC with no drag → clear vertex selection (object-level ESC
            # is handled above in the keyboard-shortcut block)
            if (event.type == 'ESC' and event.value == 'PRESS'
                    and self._grip_sel):
                self._grip_sel.clear()
                return {'RUNNING_MODAL'}

        # Block Ctrl+Tab pie menu (outside the no-modifier guard above)
        if event.type == 'TAB' and event.value == 'PRESS' and event.ctrl:
            return {'RUNNING_MODAL'}

        # Anything else falls through to Blender un-consumed - most
        # notably a right-click opening the native Object Context Menu,
        # or a click on the navigation gizmo. Blender delivers no
        # further events to this operator while that popup/gizmo
        # interaction is up, so if the OS cursor is currently hidden
        # for the idle crosshair, restore it now rather than waiting on
        # a MOUSEMOVE that won't arrive until the menu/gizmo closes.
        if (event.type in {'LEFTMOUSE', 'RIGHTMOUSE', 'MIDDLEMOUSE'}
                and event.value == 'PRESS' and self._cursor_hidden):
            restore_modal_cursor(self, context)

        return {'PASS_THROUGH'}

    def _compute_scene_signature(self, context):
        """Cheap fingerprint of the scene's CAD-relevant geometry.

        Only used to detect that *something* was added/removed (object
        deleted, undo/redo, etc.) so the snap cache and any
        selected/tracked points can be revalidated - not meant to
        detect in-place edits (those already flow through
        snap_cache_dirty via create_cad_object()).
        """
        try:
            objs = context.scene.objects
            return (len(objs), tuple(sorted(o.name for o in objs if o.type == 'MESH')))
        except Exception:
            return None

    def _purge_stale_snap_selection(self):
        """Drop selected/tracked snap points that no longer match any
        point in the freshly-rebuilt snap cache.

        Without this, a point picked via click-select (green square)
        or acquired via OTRACK keeps being drawn forever at its old
        world position even after the mesh/object it belonged to has
        been deleted, since those lists just store raw coordinates,
        not a live reference to the geometry.
        """
        if not (_st.selection.selected_snap_points or _st.selection.tracked_points):
            return

        live = set()
        for stype, bucket in self._select_snap_cache.items():
            if stype == 'NEAREST_EDGES' or stype == 'CIRCLES':
                continue  # not point buckets
            for item in bucket:
                try:
                    live.add(tuple(round(c, 4) for c in item))
                except TypeError:
                    continue

        def _still_live(pt):
            try:
                return tuple(round(c, 4) for c in pt) in live
            except TypeError:
                return False

        _st.selection.selected_snap_points = [
            p for p in _st.selection.selected_snap_points if _still_live(p)
        ]
        _st.selection.tracked_points = [
            p for p in _st.selection.tracked_points if _still_live(p)
        ]

    def _check_tool_activation(self, context):
        """
        Detect when the active T-panel tool changes to one of the CAD
        tools and immediately start its modal operator. This means
        selecting the tool is enough to arm it (hide the system
        cursor, show the on-screen crosshair, etc.) - no throw-away
        click in the viewport is needed first.

        Only fires from here, i.e. while CAD_OT_SketchModeManager
        itself is getting the event dispatch - which in practice means
        idle SELECT, since a running draw/edit tool consumes events
        itself and doesn't hand off to a newly-picked tool mid-use.
        """
        s = context.scene.cad_settings

        current = _get_active_tool_idname(context)
        if current != self._last_tool_idname:
            self._last_tool_idname = current
            self._pending_tool_invoke = _AUTO_INVOKE_TOOLS.get(current)

        if self._pending_tool_invoke is None:
            return

        # Right after a T-panel click the mouse is still over the TOOLS
        # region, where region_data (needed to project the mouse into
        # 3D space) is unavailable. Wait until the mouse has moved back
        # into the viewport's WINDOW region before invoking - which
        # happens the instant the user moves toward the viewport.
        if context.region is None or context.region.type != 'WINDOW':
            return
        if context.region_data is None:
            return

        tool_id = self._pending_tool_invoke
        self._pending_tool_invoke = None

        if s.active_tool == tool_id:
            return  # already running

        self._activate_tool(context, tool_id)

    # ── Hover-based CAD selection ─────────────────────────────────────

    _HOVER_RADIUS_PX = 10   # pixels – search radius for hover detection

    def _update_hover_selection(self, context):
        """Project visible vertices/edges/faces into screen space, find the
        nearest candidate within _HOVER_RADIUS_PX of the cursor, and store
        it as SelectionState.hovered_entity.

        Candidate filtering is controlled by SelectionState.hover_priority:
            'AUTO'   – all types, tiebreak order: vertex > edge > face
            'VERTEX' – vertices only
            'EDGE'   – edges only
            'FACE'   – faces only

        Only one hovered entity exists at a time; overwritten every call.
        """
        mx, my   = self._mx, self._my
        radius   = self._HOVER_RADIUS_PX
        priority = _st.selection.hover_priority   # 'AUTO'|'VERTEX'|'EDGE'|'FACE'

        want_v = priority in ('AUTO', 'VERTEX')
        want_e = priority in ('AUTO', 'EDGE')
        want_f = priority in ('AUTO', 'FACE')

        # In AUTO mode we run three separate passes (V → E → F) so that a
        # vertex always wins over a coincident edge or face centre, and an
        # edge always wins over a face centre at equal distance.
        # In single-type modes we run exactly one pass.

        best_d   = float(radius)
        best_ent = None

        for obj in context.view_layer.objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            mw   = obj.matrix_world
            mesh = obj.data

            # Build vertex→screen cache (needed for edges regardless)
            v2d = {}
            for vi, v in enumerate(mesh.vertices):
                s2 = world_to_screen(context, mw @ v.co)
                if s2 is not None:
                    v2d[vi] = s2

            # ── vertices ──────────────────────────────────────────────
            if want_v:
                for vi, s2 in v2d.items():
                    d = math.hypot(s2[0] - mx, s2[1] - my)
                    if d < best_d:
                        best_d   = d
                        best_ent = ('V', obj.name, vi)

            # ── edges ─────────────────────────────────────────────────
            if want_e:
                # In AUTO mode only consider edges when no vertex was found
                # within the same radius (vertex priority).
                if priority == 'AUTO' and best_ent is not None and best_ent[0] == 'V':
                    pass   # a vertex already won – skip edges for this obj
                else:
                    for ei, e in enumerate(mesh.edges):
                        sa = v2d.get(e.vertices[0])
                        sb = v2d.get(e.vertices[1])
                        if sa is None or sb is None:
                            continue
                        ax, ay = sa; bx, by = sb
                        dx, dy = bx - ax, by - ay
                        seg2   = dx * dx + dy * dy
                        if seg2 < 1e-9:
                            d = math.hypot(mx - ax, my - ay)
                        else:
                            t = max(0.0, min(1.0,
                                ((mx - ax) * dx + (my - ay) * dy) / seg2))
                            d = math.hypot(mx - (ax + t * dx),
                                           my - (ay + t * dy))
                        if d < best_d:
                            best_d   = d
                            best_ent = ('E', obj.name, ei)

            # ── faces (centre-point test) ──────────────────────────────
            if want_f:
                # In AUTO mode only consider faces when no V/E was found.
                if priority == 'AUTO' and best_ent is not None:
                    pass   # vertex or edge already won – skip faces
                else:
                    for fi, f in enumerate(mesh.polygons):
                        sc = world_to_screen(context, mw @ f.center)
                        if sc is None:
                            continue
                        d = math.hypot(sc[0] - mx, sc[1] - my)
                        if d < best_d:
                            best_d   = d
                            best_ent = ('F', obj.name, fi)

        _st.selection.hovered_entity = best_ent

    # ── SELECT-tool vertex drag helpers ──────────────────────────────

    # ── Snap-point selection (Box / Lasso / Circle / click) helpers ──

    def _snap_candidates(self, context):
        """
        Return [((x, y, z) rounded world key, (sx, sy) screen pos), ...]
        for every point in self._select_snap_cache matching the current
        Alt-menu snap_mode filter, deduplicated by world position.
        """
        snap_filter = _st.selection.get_active_stypes()
        stypes = snap_filter if snap_filter is not None else \
            ('VERTEX', 'MIDPOINT', 'CENTER')

        out  = []
        seen = set()
        for stype in stypes:
            for p in self._select_snap_cache.get(stype, []):
                key = (round(p[0], 6), round(p[1], 6), round(p[2], 6))
                if key in seen:
                    continue
                seen.add(key)
                s2 = world_to_screen(context, p)
                if s2 is None:
                    continue
                out.append((key, s2))
        return out

    def _resolve_snap_world_point(self, context, screen_pos, stype):
        """
        Given the (screen_pos, stype) result of find_hover_point(), find
        the matching world-space candidate point (closest one of that
        stype projecting to screen_pos) so it can be stored/compared in
        world space.
        """
        if stype == 'NEAREST':
            # NEAREST isn't a flat point list - find_hover_point already
            # projected the true nearest-on-segment point to screen_pos,
            # so just unproject that directly.
            return mouse_to_3d(context, screen_pos[0], screen_pos[1])

        best, best_d = None, float('inf')
        for p in self._select_snap_cache.get(stype, []):
            s2 = world_to_screen(context, p)
            if s2 is None:
                continue
            d = (s2[0] - screen_pos[0]) ** 2 + (s2[1] - screen_pos[1]) ** 2
            if d < best_d:
                best_d, best = d, p
        return best

    def _apply_box_select(self, context, p0, p1, additive=False):
        if p0 is None:
            return
        x0, y0 = p0
        x1, y1 = p1
        xmin, xmax = min(x0, x1), max(x0, x1)
        ymin, ymax = min(y0, y1), max(y0, y1)
        hits = [
            key for key, (sx, sy) in self._snap_candidates(context)
            if xmin <= sx <= xmax and ymin <= sy <= ymax
        ]
        sel = [] if not additive else list(_st.selection.selected_snap_points)
        for key in hits:
            if key not in sel:
                sel.append(key)
        _st.selection.selected_snap_points = sel

    def _apply_circle_select(self, context, cx, cy, r, subtract=False):
        sel = _st.selection.selected_snap_points
        r2  = r * r
        for key, (sx, sy) in self._snap_candidates(context):
            if (sx - cx) ** 2 + (sy - cy) ** 2 > r2:
                continue
            if subtract:
                if key in sel:
                    sel.remove(key)
            else:
                if key not in sel:
                    sel.append(key)

    def _apply_lasso_select(self, context, poly, additive=False):
        if len(poly) < 3:
            return
        hits = [
            key for key, (sx, sy) in self._snap_candidates(context)
            if point_in_polygon((sx, sy), poly)
        ]
        sel = [] if not additive else list(_st.selection.selected_snap_points)
        for key in hits:
            if key not in sel:
                sel.append(key)
        _st.selection.selected_snap_points = sel

    # ── SELECT-tool vertex selection + drag helpers ───────────────────

    def _vdrag_find_grip(self, context):
        """
        Return (obj, vi, world_co) for the closest visible grip vertex
        within the pick aperture, or None.
        """
        mx, my   = self._mx, self._my
        aperture = getattr(context.scene.cad_settings, 'cursor_aperture_px', 7)
        best_d   = float(aperture)
        best     = None
        for obj in context.view_layer.objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            mw = obj.matrix_world
            for vi, v in enumerate(obj.data.vertices):
                s2 = world_to_screen(context, mw @ v.co)
                if s2 is None:
                    continue
                d = math.hypot(s2[0] - mx, s2[1] - my)
                if d < best_d:
                    best_d = d
                    best   = (obj, vi, (mw @ v.co).copy())
        return best

    def _pick_vertex_for_select(self, context):
        """
        Object-mode vertex picking for Sketch SELECT.

        This only projects mesh vertices into screen space and never
        switches Blender into Edit Mode.
        """
        return self._vdrag_find_grip(context)

    def _vdrag_begin(self, context, obj):
        """
        Begin dragging all currently-selected vertices on *obj*.
        Snapshots their world-space positions for cancel/revert.
        """
        mw   = obj.matrix_world
        mesh = obj.data
        sel  = self._grip_sel.get(obj.name, set())
        self._vdrag_obj       = obj
        self._vdrag_orig_vcos = {vi: (mw @ mesh.vertices[vi].co).copy()
                                 for vi in sel}
        self._vdrag_anchor    = mouse_to_3d(context, self._mx, self._my)
        self._vdrag_snap_cache = build_snap_cache(context)
        self._vdrag_state     = 'DRAGGING'

    def _vdrag_apply(self, context):
        """Move all dragged vertices by the snapped delta from anchor."""
        obj = self._vdrag_obj
        if obj is None:
            return
        raw        = mouse_to_3d(context, self._mx, self._my)
        snapped, _ = apply_snaps(context, raw, self._vdrag_snap_cache,
                                  snap_filter=_st.selection.get_active_stypes())
        delta      = snapped - self._vdrag_anchor
        mw_inv     = obj.matrix_world.inverted()
        mesh       = obj.data
        for vi, orig in self._vdrag_orig_vcos.items():
            mesh.vertices[vi].co = mw_inv @ (orig + delta)
        mesh.update()

    def _vdrag_commit(self, context):
        """Accept current positions and end drag."""
        self._vdrag_state     = None
        self._vdrag_obj       = None
        self._vdrag_orig_vcos = {}
        self._vdrag_anchor    = None
        self._vdrag_snap_cache = {}
        try:
            self._select_snap_cache = build_snap_cache(context)
        except Exception:
            pass

    def _vdrag_cancel(self, context):
        """Revert all vertices to their original positions and end drag."""
        obj = self._vdrag_obj
        if obj is not None:
            mw_inv = obj.matrix_world.inverted()
            mesh   = obj.data
            for vi, co in self._vdrag_orig_vcos.items():
                mesh.vertices[vi].co = mw_inv @ co
            mesh.update()
        self._vdrag_commit(context)

    def _update_sketch_ref_obj(self, context):
        """
        Refresh state.sketch_ref_obj from the current selection.

        Only overwrites the stored reference when there IS a current
        selection, so the "last selected object" is preserved across
        the brief moment where clicking a T-panel tool button may
        itself clear context.selected_objects before the draw
        operator's invoke() runs.
        """
        from ..utils.cad_objects import pick_reference_object
        ref = pick_reference_object(context)
        if ref is not None:
            _st.sketch_ref_obj = ref

    def _activate_tool(self, context, tool_id: str):
        s = context.scene.cad_settings
        # Snapshot the reference object one last time right before
        # handing off to the draw tool, in case the T-panel click
        # itself altered the selection.
        self._update_sketch_ref_obj(context)
        s.active_tool = tool_id
        if tool_id != 'SELECT':
            self._last_used_tool = tool_id
        try:
            if   tool_id == 'LINE':      bpy.ops.cad.draw_line(    'INVOKE_DEFAULT')
            elif tool_id == 'CIRCLE':    bpy.ops.cad.draw_circle(  'INVOKE_DEFAULT')
            elif tool_id == 'ELLIPSE':   bpy.ops.cad.draw_ellipse( 'INVOKE_DEFAULT')
            elif tool_id == 'POINT':     bpy.ops.cad.draw_point(   'INVOKE_DEFAULT')
            elif tool_id == 'ARC':       bpy.ops.cad.draw_arc(     'INVOKE_DEFAULT')
            elif tool_id == 'SPLINE':    bpy.ops.cad.draw_spline(  'INVOKE_DEFAULT')
            elif tool_id == 'FILLET':    bpy.ops.cad.draw_fillet(  'INVOKE_DEFAULT')
            elif tool_id == 'CHAMFER':   bpy.ops.cad.draw_chamfer( 'INVOKE_DEFAULT')
            elif tool_id == 'EXTEND':    bpy.ops.cad.draw_extend(  'INVOKE_DEFAULT')
            elif tool_id == 'RECT':      bpy.ops.cad.draw_rect(    'INVOKE_DEFAULT')
            elif tool_id == 'MOVE':      bpy.ops.cad.sketch_move(  'INVOKE_DEFAULT')
            elif tool_id == 'SCALE':     bpy.ops.cad.sketch_scale( 'INVOKE_DEFAULT')
            elif tool_id == 'ARRAY':     bpy.ops.cad.sketch_array( 'INVOKE_DEFAULT')
            elif tool_id == 'STRETCH':   bpy.ops.cad.sketch_stretch('INVOKE_DEFAULT')
            elif tool_id == 'ROTATE':    bpy.ops.cad.sketch_rotate('INVOKE_DEFAULT')
            elif tool_id == 'COPY':      bpy.ops.cad.sketch_copy(  'INVOKE_DEFAULT')
            elif tool_id == 'MIRROR':    bpy.ops.cad.sketch_mirror('INVOKE_DEFAULT')
            elif tool_id == 'TRIM':      bpy.ops.cad.sketch_trim(  'INVOKE_DEFAULT')
            elif tool_id == 'OFFSET':    bpy.ops.cad.draw_offset( 'INVOKE_DEFAULT')
            elif tool_id == 'MEASURE':   bpy.ops.cad.measure(     'INVOKE_DEFAULT')
            elif tool_id == 'SLICE':     bpy.ops.cad.sketch_slice( 'INVOKE_DEFAULT')
            elif tool_id == 'EDIT':      bpy.ops.cad.edit_element( 'INVOKE_DEFAULT')
            elif tool_id == 'EDIT_MOVE': bpy.ops.cad.edit_move(    'INVOKE_DEFAULT')
        except Exception as e:
            print(f"[CAD v0.92] tool activation error: {e}")

        if context.area:
            context.area.tag_redraw()

    # ════════════════════════════════════════════════════════════════
    #  IDLE CURSOR  (AutoCAD-style crosshair / pick box / AutoSnap /
    #  grips overlay for Sketch Mode)
    # ════════════════════════════════════════════════════════════════

    def _on_tool_transition(self, context, old_tool: str, new_tool: str):
        """
        Called whenever cad_settings.active_tool changes value. Keeps the
        idle-cursor overlay's bookkeeping in sync with whichever operator
        currently owns drawing / cursor duties.
        """
        if new_tool == 'SELECT':
            # Returning to idle/selection - new geometry may have been
            # created, moved, or sliced, so refresh the snap-point cache
            # used by the AutoSnap-hover check.
            try:
                self._select_snap_cache = build_snap_cache(context)
            except Exception as e:
                print(f"[CAD] cursor overlay: snap cache rebuild failed: {e}")
                self._select_snap_cache = {'VERTEX': [], 'MIDPOINT': [], 'CENTER': []}
            self._lmb_down = False
        else:
            # A draw/move/slice tool just took over - it manages its own
            # cursor_modal_set() / cursor_modal_restore() calls from here
            # on, so this overlay's own idle-crosshair duties are done.
            #
            # Blender's cursor_modal_set()/cursor_modal_restore() are a
            # stack, not a flag: every set() has to be matched by exactly
            # one restore() or the pushed state is left behind forever.
            # If the mouse was still over the viewport in SELECT mode the
            # instant this tool switch happened, _update_cursor_visibility()
            # already pushed a cursor_modal_set('NONE') for it
            # (self._cursor_hidden is True here) - simply dropping that
            # bookkeeping without popping it leaves that push orphaned on
            # Blender's stack. The new tool's own invoke()/modal() then
            # pushes *its* NONE on top for the same reason, and later pops
            # only its own layer back off when the mouse leaves the
            # viewport - leaving this orphaned layer underneath still
            # active, so the OS cursor stays invisible even out over the
            # T-panel/N-panel, unpredictably, depending on whether the
            # mouse happened to be in the viewport at the exact moment the
            # tool was activated. Popping it here, before dropping the
            # bookkeeping, keeps the stack balanced no matter which tool
            # takes over next.
            restore_modal_cursor(self, context)
            self._lmb_down = False

    def _update_cursor_visibility(self, context, event):
        """
        While SELECT is the active tool, hide the OS cursor over the 3D
        viewport so the custom crosshair / pick-box overlay is the only
        cursor shown, and restore it the instant the mouse leaves the
        viewport - satisfying "Outside drawing area -> standard OS arrow
        cursor".

        Active draw/move/slice tools already manage cursor visibility
        themselves (cursor_modal_set in invoke / cursor_modal_restore in
        cancel), so this is a no-op while one of them is running.
        """
        s = context.scene.cad_settings
        if s.active_tool != 'SELECT':
            return

        # s.active_tool only updates once _check_tool_activation() gets
        # to actually invoke the new tool's own operator, which requires
        # the mouse to already be back over the viewport's WINDOW region
        # (see _check_tool_activation). Between the moment a CAD tool is
        # picked in the T-panel and that moment, s.active_tool is still
        # (stale) 'SELECT' - so relying on it alone leaves a real window
        # where the mouse can re-enter the viewport, this overlay pushes
        # its own cursor_modal_set('NONE') for its SELECT-tool crosshair
        # (believing SELECT is still active), and then the about-to-start
        # CAD tool pushes a *second* one on top the instant it invokes.
        # Later, only the CAD tool's own layer ever gets popped - this
        # overlay's layer is left stranded, keeping the OS cursor hidden
        # forever afterwards, anywhere on screen, until something else
        # happens to pop it. Checking Blender's real active WorkSpaceTool
        # closes the window at the source: the instant a CAD tool is
        # picked from the T-panel, Blender's own tool system already
        # reflects that, even though this add-on's own s.active_tool
        # bookkeeping hasn't caught up yet.
        if _get_active_tool_idname(context) in _AUTO_INVOKE_TOOLS:
            # If this overlay had already hidden the cursor for its own
            # idle crosshair a moment earlier - i.e. the mouse was in the
            # viewport in the instant before the T-panel click - pop
            # that now, rather than leaving it stranded underneath the
            # CAD tool's own separate push.
            restore_modal_cursor(self, context)
            return

        update_modal_cursor(self, context, event)

    # ════════════════════════════════════════════════════════════════
    #  SELECTION HIGHLIGHTS  (hover=yellow, selected=orange)
    # ════════════════════════════════════════════════════════════════

    # Colours for the new hover/selection system
    _COL_HOVER    = (1.00, 1.00, 0.00, 0.95)   # yellow
    _COL_SELECTED = (1.00, 0.50, 0.00, 0.95)   # orange
    _HIGHLIGHT_HALF = 4   # half-size of vertex highlight square (px) — kept compact
    _HIGHLIGHT_EDGE_W = 3.0   # line width for highlighted edges

    def _draw_selection_highlights(self, context):
        """Draw yellow highlight for hovered_entity and orange for
        selected_entities.  Called every frame from _draw_cursor_overlay
        while SELECT is the active tool.

        Entities: ('V', obj_name, vi)  or  ('E', obj_name, ei)
        """
        sel = set(map(tuple, _st.selection.selected_entities))

        # Gather all entities to draw
        to_draw = {}   # entity → colour
        for ent in sel:
            to_draw[ent] = self._COL_SELECTED
        # Yellow proximity hover highlight permanently disabled.

        if not to_draw:
            return

        # Group by object name to avoid repeated lookups
        by_obj = {}
        for ent, col in to_draw.items():
            name = ent[1]
            by_obj.setdefault(name, []).append((ent, col))

        for obj_name, items in by_obj.items():
            obj = context.view_layer.objects.get(obj_name)
            if obj is None or obj.type != 'MESH' or not obj.visible_get():
                continue
            mw   = obj.matrix_world
            mesh = obj.data

            # Build vertex→screen cache for edge endpoints
            v2d = {}
            for vi, v in enumerate(mesh.vertices):
                s2 = world_to_screen(context, mw @ v.co)
                if s2 is not None:
                    v2d[vi] = s2

            for ent, col in items:
                kind = ent[0]
                idx  = ent[2]

                if kind == 'V':
                    continue  # vertex selection square permanently disabled

                elif kind == 'E' and idx < len(mesh.edges):
                    e  = mesh.edges[idx]
                    sa = v2d.get(e.vertices[0])
                    sb = v2d.get(e.vertices[1])
                    if sa and sb:
                        gfx_lines([sa, sb], col, self._HIGHLIGHT_EDGE_W)

                elif kind == 'F' and idx < len(mesh.polygons):
                    sc = world_to_screen(context, mw @ mesh.polygons[idx].center)
                    if sc is None:
                        continue
                    h = self._HIGHLIGHT_HALF + 2
                    # Diamond shape centred on face centre
                    gfx_loop([(sc[0],   sc[1]-h),
                               (sc[0]+h, sc[1]  ),
                               (sc[0],   sc[1]+h),
                               (sc[0]-h, sc[1]  )],
                              col, 2.0)

    _COL_SNAP_SELECTED = (0.15, 0.90, 0.25, 1.0)   # green

    def _draw_snap_point_selection(self, context):
        """
        Draw a green square on every currently-selected snap point
        (Endpoint/Midpoint/Center/etc, per the active Alt-menu mode) via
        the shared gfx.markers.draw_selected_snap_points() helper - also
        used by the Line/Circle/Rect draw tools so a selection persists
        visually across tool switches - plus the live visual for
        whichever multi-select gesture (Box, Circle, Lasso) is currently
        armed/in-progress (Select-tool only).
        """
        draw_selected_snap_points(context)

        gcol = (1.0, 1.0, 1.0, 0.85)

        if self._snap_select_mode == 'BOX' and self._snap_select_drag \
                and self._snap_select_start is not None:
            x0, y0 = self._snap_select_start
            x1, y1 = self._mx, self._my
            gfx_loop([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], gcol, 1.2)

        elif self._snap_select_mode == 'CIRCLE':
            cx, cy = self._mx, self._my
            r = self._snap_select_circle_r
            n = 24
            pts = [
                (cx + r * math.cos(2 * math.pi * i / n),
                 cy + r * math.sin(2 * math.pi * i / n))
                for i in range(n)
            ]
            gfx_loop(pts, gcol, 1.2)

        elif self._snap_select_mode == 'LASSO' and len(self._snap_select_lasso_pts) > 1:
            gfx_lines(self._snap_select_lasso_pts, gcol, 1.2)

    # ════════════════════════════════════════════════════════════════
    #  SNAP / SELECT MODE HUD BOX  –  Blender-style drop-down
    # ════════════════════════════════════════════════════════════════
    #
    #  Collapsed (ALT not held):  28×28 px square chip, icon only
    #
    #  Expanded  (ALT held) — matches reference screenshot style:
    #
    #    ┌────────────────────────────┐
    #    │  Snap To                   │  ← header row, no highlight
    #    ├────────────────────────────┤
    #    │ [icon]  Endpoint        1  │  ← active row = solid blue
    #    │ [icon]  Midpoint        2  │
    #    │ [icon]  Center          3  │
    #    │ [icon]  Intersection    4  │
    #    │ [icon]  Nearest         5  │
    #    │ [icon]  Increment       6  │
    #    │ [icon]  Quadrant        7  │
    #    │ [icon]  Tangent         8  │
    #    │ [icon]  Perpendicular   9  │
    #    │ [icon]  Parallel        0  │
    #    └────────────────────────────┘
    #
    #  LMB click on a row while expanded → selects that snap mode
    #  Number keys 1-9,0 select snap modes directly (no ALT needed)

    # ── Layout ───────────────────────────────────────────────────────
    _SB_ICON_SIZE   = 16      # icon cell px
    _SB_ROW_H       = 36      # row height (matches reference ~36 px density)
    _SB_HEADER_H    = 32      # "Snap To" header row height
    _SB_PAD_L       = 10      # left padding inside box
    _SB_PAD_R       = 12      # right padding
    _SB_ICON_CX     = 22      # icon centre X from box left
    _SB_LABEL_X     = 40      # label text X from box left
    _SB_NUM_RX      = 14      # number X from box right edge
    _SB_ALT_RX      = 44      # ALT+X hint X from box right edge (left of number)
    _SB_OFFSET_X    = 18      # cursor → box left
    _SB_OFFSET_Y    = 18      # cursor → box bottom
    _SB_RADIUS      = 6       # corner radius (Blender uses ~5-6)
    _SB_FONT_SIZE   = 12      # label font size (Blender default UI ~11-12)
    _SB_HEADER_SIZE = 10      # header label font size
    _SB_NUM_SIZE    = 10      # number hint font size
    _SB_ALT_SIZE    = 9       # ALT+X hint font size

    # Collapsed chip
    _SB_CELL        = 28      # px — both width and height, always square

    # Expanded box — wide enough for "Perpendicular" + ALT+P + number
    _SB_EXP_W       = 248

    # ── Colours (sampled from reference image) ───────────────────────
    # Panel background: very dark grey #1C1C1C
    _SB_BG           = (0.110, 0.110, 0.110, 0.97)
    # Header row bg: slightly lighter #252525
    _SB_HEADER_BG    = (0.145, 0.145, 0.145, 0.97)
    # Active row: Blender blue #3D70C4
    _SB_BG_ACTIVE    = (0.239, 0.435, 0.769, 1.00)
    # Hover row (mouse-over while expanded): subtle highlight
    _SB_BG_HOVER     = (0.200, 0.200, 0.200, 0.90)
    # Border: barely-visible edge #2C2C2C
    _SB_BORDER       = (0.172, 0.172, 0.172, 1.00)
    # Separator line under header
    _SB_SEPARATOR    = (0.220, 0.220, 0.220, 1.00)
    # Header text: medium grey
    _SB_HEADER_COL   = (0.620, 0.620, 0.620, 1.00)
    # Icon / label idle
    _SB_ICON_IDLE    = (0.780, 0.780, 0.780, 1.00)
    _SB_TEXT_IDLE    = (0.850, 0.850, 0.850, 1.00)
    # Icon / label active (on blue row)
    _SB_ICON_ACT     = (1.000, 1.000, 1.000, 1.00)
    _SB_TEXT_ACT     = (1.000, 1.000, 1.000, 1.00)
    # Number hint idle / active
    _SB_NUM_IDLE     = (0.420, 0.420, 0.420, 1.00)
    _SB_NUM_ACT      = (0.820, 0.880, 1.000, 1.00)
    # ALT+X hint idle / active
    _SB_ALT_IDLE     = (0.360, 0.360, 0.360, 1.00)
    _SB_ALT_ACT      = (0.700, 0.820, 1.000, 1.00)
    # Collapsed chip border (active blue)
    _SB_BORDER_ACT   = (0.239, 0.435, 0.769, 1.00)

    # Number-key labels for each mode in display order
    # These map to the 10 modes shown: 1-9 then 0
    _SB_NUM_LABELS = ['1','2','3','4','5','6','7','8','9','0']

    # ── Display order for the drop-down ──────────────────────────────
    # Matches the reference image: Endpoint, Midpoint, Center,
    # Intersection, Nearest, Increment, Quadrant, Tangent,
    # Perpendicular, Parallel  (10 rows, no Vertex/Grid)
    _SB_DISPLAY_MODES = [
        'ENDPOINT', 'MIDPOINT', 'CENTER', 'INTERSECTION',
        'NEAREST', 'INCREMENT', 'QUADRANT', 'TANGENT',
        'PERPENDICULAR', 'PARALLEL',
    ]

    # ── Hover tracking for mouse-over row highlight ───────────────────
    _sb_mouse_row = -1   # index of row under cursor while expanded, -1 = none

    # ── icon drawers ─────────────────────────────────────────────────
    # Each function receives (cx, cy, sz, col).
    # Icons mirror the geometry icons used in gfx/markers.py _draw_snap_shape
    # so the HUD chip and the snap indicator on the model look related.
    # sz = _SB_ICON_SIZE (16 px default in the HUD box).

    @staticmethod
    def _icon_endpoint(cx, cy, sz, col):
        """●—  dot at line end."""
        r = sz * 0.40
        gfx_lines([(cx - r, cy), (cx + r * 0.2, cy)], col, 1.6)
        gfx_dot((cx + r * 0.2, cy), col, r * 1.5)

    @staticmethod
    def _icon_midpoint(cx, cy, sz, col):
        """—◆—  line with diamond at centre."""
        r = sz * 0.46
        gfx_lines([(cx - r, cy), (cx + r, cy)], col, 1.6)
        d = sz * 0.22
        gfx_loop([(cx, cy + d), (cx + d * 0.8, cy),
                   (cx, cy - d), (cx - d * 0.8, cy)], col, 1.6)

    @staticmethod
    def _icon_vertex(cx, cy, sz, col):
        """◣●  hollow square + centre dot."""
        h = sz * 0.38
        gfx_loop([(cx - h, cy - h), (cx + h, cy - h),
                   (cx + h, cy + h), (cx - h, cy + h)], col, 1.6)
        gfx_dot((cx, cy), col, h * 0.6)

    @staticmethod
    def _icon_center(cx, cy, sz, col):
        """Fusion-style Center snap: circle outline + small filled centre dot."""
        r = sz * 0.42
        gfx_circle((cx, cy), r, col, segs=24, w=1.5)
        dot_r = sz * 0.11
        segs = 10
        for i in range(segs):
            a0 = 2 * math.pi * i / segs
            a1 = 2 * math.pi * (i + 1) / segs
            p0 = (cx + dot_r * math.cos(a0), cy + dot_r * math.sin(a0))
            p1 = (cx + dot_r * math.cos(a1), cy + dot_r * math.sin(a1))
            gfx_tri_fill((cx, cy), p0, p1, col)

    @staticmethod
    def _icon_intersection(cx, cy, sz, col):
        """╳●  diagonal cross + centre dot."""
        r = sz * 0.40
        gfx_lines([(cx - r, cy - r), (cx + r, cy + r)], col, 1.6)
        gfx_lines([(cx + r, cy - r), (cx - r, cy + r)], col, 1.6)
        gfx_dot((cx, cy), col, r * 0.7)

    @staticmethod
    def _icon_nearest(cx, cy, sz, col):
        """—⊙—  line + open ring at nearest point."""
        r = sz * 0.44
        gfx_lines([(cx - r, cy), (cx + r, cy)], col, 1.6)
        gfx_circle((cx, cy), sz * 0.18, col, segs=12, w=1.4)

    @staticmethod
    def _icon_grid(cx, cy, sz, col):
        """#  3×3 hash grid."""
        step = sz * 0.28
        for i in (-1, 0, 1):
            gfx_lines([(cx + i * step, cy - sz * 0.44),
                        (cx + i * step, cy + sz * 0.44)], col, 1.2)
            gfx_lines([(cx - sz * 0.44, cy + i * step),
                        (cx + sz * 0.44, cy + i * step)], col, 1.2)

    @staticmethod
    def _icon_increment(cx, cy, sz, col):
        """Grid spacing icon: ruler + stepped marks."""
        w=sz*0.42
        gfx_lines([(cx-w,cy),(cx+w,cy)], col, 1.6)
        for x in (-0.25,0,0.25):
            px=cx+(w*1.4*x)
            gfx_lines([(px,cy-sz*0.18),(px,cy+sz*0.18)], col,1.3)
        gfx_dot((cx+w*0.7,cy), col, sz*0.10)

    @staticmethod
    def _icon_quadrant(cx, cy, sz, col):
        """Fusion-style Quadrant snap: circle + single tick at top quadrant point."""
        r = sz * 0.34
        gfx_circle((cx, cy), r, col, segs=28, w=1.5)
        tick = sz * 0.16
        gfx_lines([(cx, cy + r), (cx, cy + r + tick)], col, 1.6)
        dot_r = sz * 0.085
        dy = cy + r
        segs = 10
        for i in range(segs):
            a0 = 2 * math.pi * i / segs
            a1 = 2 * math.pi * (i + 1) / segs
            p0 = (cx + dot_r * math.cos(a0), dy + dot_r * math.sin(a0))
            p1 = (cx + dot_r * math.cos(a1), dy + dot_r * math.sin(a1))
            gfx_tri_fill((cx, dy), p0, p1, col)

    @staticmethod
    def _icon_tangent(cx, cy, sz, col):
        """◯—  arc + tangent bar."""
        r = sz * 0.30
        segs = 12
        arc_pts = [(cx + r * math.cos(math.radians(i * 180 / segs)),
                    cy + r * math.sin(math.radians(i * 180 / segs)))
                   for i in range(segs + 1)]
        for i in range(len(arc_pts) - 1):
            gfx_lines([arc_pts[i], arc_pts[i + 1]], col, 1.4)
        bar_y = cy - r * 0.1
        gfx_lines([(cx - sz * 0.44, bar_y), (cx + sz * 0.44, bar_y)], col, 1.4)

    @staticmethod
    def _icon_perpendicular(cx, cy, sz, col):
        """∟  right-angle corner."""
        r = sz * 0.40
        gfx_lines([(cx - r, cy + r), (cx - r, cy - r), (cx + r, cy - r)], col, 1.6)
        sq = r * 0.35
        gfx_loop([(cx - r,      cy - r),
                   (cx - r + sq, cy - r),
                   (cx - r + sq, cy - r + sq),
                   (cx - r,      cy - r + sq)], col, 1.2)

    @staticmethod
    def _icon_parallel(cx, cy, sz, col):
        """═  two parallel lines."""
        off = sz * 0.20
        r   = sz * 0.44
        gfx_lines([(cx - r, cy + off), (cx + r, cy + off)], col, 1.6)
        gfx_lines([(cx - r, cy - off), (cx + r, cy - off)], col, 1.6)

    # Icon dispatcher keyed by snap_mode — built lazily to avoid forward refs
    _SNAP_ICON_FN = None

    @classmethod
    def _get_icon_fn(cls, key):
        if cls._SNAP_ICON_FN is None:
            cls._SNAP_ICON_FN = {
                'ENDPOINT':      cls._icon_endpoint,
                'MIDPOINT':      cls._icon_midpoint,
                'VERTEX':        cls._icon_vertex,
                'CENTER':        cls._icon_center,
                'INTERSECTION':  cls._icon_intersection,
                'NEAREST':       cls._icon_nearest,
                'GRID':          cls._icon_grid,
                'INCREMENT':     cls._icon_increment,
                'QUADRANT':      cls._icon_quadrant,
                'TANGENT':       cls._icon_tangent,
                'PERPENDICULAR': cls._icon_perpendicular,
                'PARALLEL':      cls._icon_parallel,
            }
        return cls._SNAP_ICON_FN.get(key, cls._icon_endpoint)

    def _draw_snap_icon(self, key, cx, cy, col):
        """Draw the icon for *key* centred on (cx, cy)."""
        fn = self._get_icon_fn(key)
        try:
            fn(cx, cy, self._SB_ICON_SIZE, col)
        except Exception:
            pass

    def _draw_snap_mode_box(self, context):
        """Draw the snap/select mode HUD box beside the cursor.

        Collapsed (ALT not held): tiny icon-only square chip.
        Expanded  (ALT held)    : Blender-style drop-down with header,
                                   icons, labels, and number hints.
                                   LMB click on a row selects that mode.
        """
        import blf
        mx, my     = self._mx, self._my
        active_key = _st.selection.snap_mode
        expanded   = _st.selection.alt_mode_active
        fid        = 0

        # Resolve display-order mode list from SNAP_MODES registry
        mode_map   = {m[2]: m for m in _st.SelectionState.SNAP_MODES}
        disp_modes = [mode_map[k] for k in self._SB_DISPLAY_MODES if k in mode_map]

        # ── position ─────────────────────────────────────────────────
        if expanded:
            box_w = self._SB_EXP_W
            box_h = self._SB_HEADER_H + len(disp_modes) * self._SB_ROW_H
        else:
            box_w = self._SB_CELL
            box_h = self._SB_CELL

        bx = mx + self._SB_OFFSET_X
        by = my + self._SB_OFFSET_Y
        region = context.region
        if region:
            if bx + box_w > region.width  - 4:
                bx = mx - self._SB_OFFSET_X - box_w
            if by + box_h > region.height - 4:
                by = my - self._SB_OFFSET_Y - box_h

        # ── collapsed: single icon chip ───────────────────────────────
        if not expanded:
            osnap_on = _st.SelectionState.osnap_enabled
            border_col = self._SB_BORDER_ACT if osnap_on else self._SB_BORDER
            icon_col   = self._SB_ICON_ACT if osnap_on else self._SB_ICON_IDLE
            gfx_rounded_rect_fill(bx, by, box_w, box_h,
                                  self._SB_RADIUS, self._SB_BG)
            gfx_rounded_rect_loop(bx, by, box_w, box_h,
                                  self._SB_RADIUS, border_col, 1.6)
            self._draw_snap_icon(active_key,
                                 bx + box_w / 2, by + box_h / 2,
                                 icon_col)
            return

        # ── expanded panel ────────────────────────────────────────────

        # Outer panel (full rounded)
        gfx_rounded_rect_fill(bx, by, box_w, box_h,
                              self._SB_RADIUS, self._SB_BG)
        gfx_rounded_rect_loop(bx, by, box_w, box_h,
                              self._SB_RADIUS, self._SB_BORDER, 1.0)

        # Header row background (slightly lighter, top of panel)
        header_y = by + box_h - self._SB_HEADER_H
        # Rounded fill only on top corners — approximate with rect + manual top corners
        gfx_rect_fill(bx + 1, header_y, box_w - 2, self._SB_HEADER_H - 1,
                      self._SB_HEADER_BG)

        # "Snap To" header text
        try:
            blf.size(fid, self._SB_HEADER_SIZE, 72)
        except Exception:
            blf.size(fid, self._SB_HEADER_SIZE)
        gfx_text("Snap To",
                 bx + self._SB_PAD_L,
                 header_y + (self._SB_HEADER_H - self._SB_HEADER_SIZE) // 2,
                 self._SB_HEADER_SIZE, self._SB_HEADER_COL)

        # Separator line under header
        sep_y = header_y - 1
        gfx_lines([(bx + 1, sep_y), (bx + box_w - 1, sep_y)],
                  self._SB_SEPARATOR, 1.0)

        # Set label font size for all rows
        try:
            blf.size(fid, self._SB_FONT_SIZE, 72)
        except Exception:
            blf.size(fid, self._SB_FONT_SIZE)

        # Determine which row the mouse is hovering over
        hover_row = -1
        for ri in range(len(disp_modes)):
            ry = by + (len(disp_modes) - 1 - ri) * self._SB_ROW_H
            if ry <= my < ry + self._SB_ROW_H and bx <= mx < bx + box_w:
                hover_row = ri
                break

        # Draw rows top → bottom (index 0 = top of list = Endpoint)
        for ri, (letter, label, key) in enumerate(disp_modes):
            is_checked = key in _st.selection.active_snap_types
            is_focus   = (key == active_key)
            is_hover   = (ri == hover_row) and not is_checked
            # Row Y: row 0 is directly under header
            ry = by + (len(disp_modes) - 1 - ri) * self._SB_ROW_H

            row_cy = ry + self._SB_ROW_H / 2   # vertical centre of row

            # Row background: checked (running snap on) always gets the
            # solid highlight; an unchecked but hovered/focused row
            # gets the lighter hover tint for navigation feedback.
            if is_checked:
                gfx_rect_fill(bx + 1, ry + 1, box_w - 2, self._SB_ROW_H - 2,
                              self._SB_BG_ACTIVE)
            elif is_hover or is_focus:
                gfx_rect_fill(bx + 1, ry + 1, box_w - 2, self._SB_ROW_H - 2,
                              self._SB_BG_HOVER)

            icon_col = self._SB_ICON_ACT  if is_checked else self._SB_ICON_IDLE
            text_col = self._SB_TEXT_ACT  if is_checked else self._SB_TEXT_IDLE
            num_col  = self._SB_NUM_ACT   if is_checked else self._SB_NUM_IDLE
            alt_col  = self._SB_ALT_ACT   if is_checked else self._SB_ALT_IDLE

            # Icon
            self._draw_snap_icon(key, bx + self._SB_ICON_CX, row_cy, icon_col)

            # Label — vertically centred in row. The single active row
            # gets a leading checkmark.
            label_y = ry + (self._SB_ROW_H - self._SB_FONT_SIZE) // 2
            label_txt = ("\u2713 " + label) if is_checked else label
            gfx_text(label_txt, bx + self._SB_LABEL_X, label_y,
                     self._SB_FONT_SIZE, text_col)

            # Number hint right-aligned
            num_str = self._SB_NUM_LABELS[ri] if ri < len(self._SB_NUM_LABELS) else ''
            if num_str:
                try:
                    blf.size(fid, self._SB_NUM_SIZE, 72)
                except Exception:
                    blf.size(fid, self._SB_NUM_SIZE)
                num_w = blf.dimensions(fid, num_str)[0]
                num_x = bx + box_w - self._SB_NUM_RX - num_w
                num_y = ry + (self._SB_ROW_H - self._SB_NUM_SIZE) // 2
                gfx_text(num_str, num_x, num_y, self._SB_NUM_SIZE, num_col)

            # ALT+X hint — left of the number, dimmer
            alt_str = f"ALT+{letter}"
            try:
                blf.size(fid, self._SB_ALT_SIZE, 72)
            except Exception:
                blf.size(fid, self._SB_ALT_SIZE)
            alt_w = blf.dimensions(fid, alt_str)[0]
            alt_x = bx + box_w - self._SB_ALT_RX - alt_w
            alt_y = ry + (self._SB_ROW_H - self._SB_ALT_SIZE) // 2
            gfx_text(alt_str, alt_x, alt_y, self._SB_ALT_SIZE, alt_col)

            try:
                blf.size(fid, self._SB_FONT_SIZE, 72)
            except Exception:
                blf.size(fid, self._SB_FONT_SIZE)

    def _snap_box_hit_row(self, context, mx, my):
        """Return the snap mode key if (mx, my) hits a row in the expanded box,
        or None if no hit.  Called from modal LMB handler."""
        if not _st.selection.alt_mode_active:
            return None

        mode_map   = {m[2]: m for m in _st.SelectionState.SNAP_MODES}
        disp_modes = [mode_map[k] for k in self._SB_DISPLAY_MODES if k in mode_map]

        box_w = self._SB_EXP_W
        box_h = self._SB_HEADER_H + len(disp_modes) * self._SB_ROW_H

        bx = mx + self._SB_OFFSET_X   # NOTE: must use stored self._mx, not event mx
        # Recalculate bx/by from stored cursor position
        cmx, cmy = self._mx, self._my
        bx = cmx + self._SB_OFFSET_X
        by = cmy + self._SB_OFFSET_Y
        region = context.region
        if region:
            if bx + box_w > region.width  - 4:
                bx = cmx - self._SB_OFFSET_X - box_w
            if by + box_h > region.height - 4:
                by = cmy - self._SB_OFFSET_Y - box_h

        # Check header — no action
        header_y = by + box_h - self._SB_HEADER_H
        if my >= header_y:
            return None

        # Check rows
        for ri, (letter, label, key) in enumerate(disp_modes):
            ry = by + (len(disp_modes) - 1 - ri) * self._SB_ROW_H
            if bx <= mx < bx + box_w and ry <= my < ry + self._SB_ROW_H:
                return key
        return None

    def _draw_cursor_overlay(self, context):
        """
        SpaceView3D / WINDOW / POST_PIXEL draw handler implementing the
        AutoCAD-style idle cursor for Sketch Mode:

            Idle                | crosshair + pick box
            Hovering snap point | crosshair + AutoSnap marker
            Object selection    | pick box only           (LMB held)
            Active draw tool    | nothing here - the tool draws its
                                  own crosshair-only cursor
            Outside viewport    | nothing - OS arrow cursor shows

        Also draws yellow "grip" squares on the vertices of any currently
        selected mesh objects whenever SELECT is the active tool.

        The whole body is wrapped in a broad except: a drawing-callback
        exception repeats on every redraw and is a well-known way to
        destabilise Blender's UI, so any error here is logged once and
        then silenced rather than left to spam / hang the viewport.
        """
        try:
            scene = context.scene
            if scene is None:
                return
            s = scene.cad_settings
            if not getattr(s, 'sketch_mode', False):
                return

            # This handler is registered globally for every VIEW_3D
            # window's WINDOW region - only draw in the viewport where
            # Sketch Mode was actually started. NOTE: bpy_struct wrappers
            # such as Area are not guaranteed to be the same Python object
            # across callbacks even when they refer to the same underlying
            # area, so use '==' (RNA pointer equality) rather than 'is'.
            if _st.sketch_area_ref is None or context.area != _st.sketch_area_ref:
                return

            if context.region is None or context.region_data is None:
                return

            # ── Kept Measurements: measurements the user chose to keep
            #    (Tab, while drawing or on a selected one) via the
            #    Measure tool. Drawn here so they stay visible for the
            #    rest of the Sketch Mode session, even after Measure
            #    itself has been closed - but skipped while Measure is
            #    the active tool, since it loads these same entries
            #    into its own self._measurements and draws them itself
            #    (so they can be selected/deleted/un-kept from within
            #    the tool); drawing them here too would just duplicate
            #    them on screen.
            if _st.kept_measurements and context.scene.cad_settings.active_tool != 'MEASURE':
                try:
                    from .tool_measure import draw_measurements_list
                    draw_measurements_list(context, _st.kept_measurements)
                except Exception:
                    pass

            # ── Grips on all visible mesh vertices (yellow = unselected,
            #    green = selected, white = being dragged) ───────────────
            if s.active_tool == 'SELECT' and getattr(s, 'cursor_show_grips', True):
                _cursor_gfx.draw_grips_for_selection(
                    context, s,
                    sel_verts   = self._grip_sel,
                    drag_obj    = self._vdrag_obj if self._vdrag_state == 'DRAGGING' else None,
                    drag_verts  = set(self._vdrag_orig_vcos.keys()),
                    mx          = self._mx,
                    my          = self._my,
                    snap_cache  = self._select_snap_cache,
                    snap_filter = _st.selection.get_active_stypes(),
                )

            # ── SelectionState: draw hover (yellow) + selected (orange) ──
            if s.active_tool == 'SELECT':
                self._draw_selection_highlights(context)

            # ── Snap-point selection: green squares + active gesture ──
            if s.active_tool == 'SELECT':
                self._draw_snap_point_selection(context)

            # ── Snap / select mode HUD box ────────────────────────────
            if s.active_tool == 'SELECT' and self._in_viewport:
                self._draw_snap_mode_box(context)

            # Drag guide lines from original vert positions to current
            if self._vdrag_state == 'DRAGGING' and self._vdrag_obj is not None:
                obj  = self._vdrag_obj
                mw   = obj.matrix_world
                mesh = obj.data
                for vi, orig_wco in self._vdrag_orig_vcos.items():
                    try:
                        cur_s2  = world_to_screen(context, mw @ mesh.vertices[vi].co)
                        orig_s2 = world_to_screen(context, orig_wco)
                        if cur_s2 and orig_s2 and cur_s2 != orig_s2:
                            gfx_lines([orig_s2, cur_s2], (0.4, 1.0, 0.4, 0.65), 1.2)
                    except Exception:
                        pass

            # The active draw/move/slice tool owns the cursor while it
            # is running and draws its own crosshair.
            if s.active_tool != 'SELECT':
                return

            if not self._in_viewport:
                return

            mx, my = self._mx, self._my

            # Optional debug visualisation of the (normally invisible)
            # Object Snap aperture box.
            if getattr(s, 'cursor_show_aperture', False):
                _cursor_gfx.draw_aperture(mx, my, s.cursor_aperture_px)

            hover = find_hover_point(
                context, mx, my, self._select_snap_cache, s.cursor_aperture_px,
                snap_filter=_st.selection.get_active_stypes(),
            )

            if hover is not None:
                # Hovering an Object Snap point → crosshair snaps onto the
                # candidate, plus a red snap indicator whose shape matches
                # the active snap mode so the user instantly knows what will
                # be captured (triangle = midpoint, cross = intersection, …)
                hover_2d, hover_type = hover
                draw_crosshair(context, hover_2d[0], hover_2d[1],
                               axis_lock=None, show_box=False)
                draw_snap_indicator(
                    hover_2d,
                    hover_type,
                    hover_t=_st.SelectionState.snap_hover_t,
                    mx=mx, my=my,
                )
                return

            if self._lmb_down:
                # Mid-drag (Box/Circle/Lasso select) -> pick-box cursor,
                # no free crosshair, but the wide highlight search below
                # still applies exactly as it does when idle.
                _cursor_gfx.draw_pickbox(mx, my, s.cursor_pickbox_px)
            else:
                # Idle -> crosshair + pick box
                draw_crosshair(context, mx, my, axis_lock=None, show_box=False)
                _cursor_gfx.draw_pickbox(mx, my, s.cursor_pickbox_px)

            # Wider-radius early feedback: nothing was within the real
            # (tight) aperture above - search again at a larger, but
            # still purely screen-pixel, radius. Runs regardless of
            # whether a Box/Circle/Lasso drag is in progress, not just
            # when idle. The crosshair above deliberately stays
            # free/unsnapped (drawn at mx,my, not at the candidate) -
            # only the tight search above is allowed to actually move it.
            wide = find_hover_point(
                context, mx, my, self._select_snap_cache,
                getattr(s, 'snap_highlight_px', 50.0),
                snap_filter=_st.selection.get_active_stypes(),
            )
            if wide is not None:
                wide_2d, wide_type = wide
                draw_snap_indicator(
                    wide_2d, wide_type,
                    hover_t=_st.SelectionState.snap_hover_t,
                    mx=mx, my=my,
                )

        except Exception as e:
            if not self._overlay_error_logged:
                print(f"[CAD] cursor overlay draw error: {e}")
                self._overlay_error_logged = True


# ═══════════════════════════════════════════════════════════════════
#  UNDO/REDO RESYNC
#
#  Blender's own undo (memfile) already restores CAD geometry
#  correctly, since it's ordinary bpy.data mesh objects (see
#  utils/cad_objects.create_cad_object) - that part was never the
#  problem. What Blender's undo knows nothing about is this addon's
#  *parallel* Python-side bookkeeping that isn't part of any ID
#  datablock: CAD_OT_SketchModeManager's cached snap points
#  (self._select_snap_cache), the selection/tracking state in
#  state.SelectionState, and state.sketch_ref_obj. A memfile Undo/Redo
#  swaps out the underlying data - possibly moving vertices back to an
#  earlier position, possibly deleting/recreating objects entirely -
#  without touching any of that cached Python state at all, so it can
#  silently go stale: snap points drawn at coordinates that no longer
#  have any geometry there, a selection highlight for an object that
#  Undo just deleted, etc.
#
#  The always-on idle overlay (CAD_OT_SketchModeManager.modal(), see
#  _compute_scene_signature()'s docstring) already catches *some* of
#  this per-tick, but only object additions/removals (it fingerprints
#  object count + names) - an Undo that only moves vertices in place
#  (undoing a Move/Rotate/Scale/Edit) changes neither, so that path
#  alone would miss it, and even where it does apply it only takes
#  effect on the next redraw tick rather than immediately.
#
#  These two handlers close both gaps: they fire synchronously on
#  every single Undo/Redo Blender performs (bpy.app.handlers guarantees
#  this - unlike the per-tick check, it can't be skipped by mashing
#  Ctrl+Z several times before a redraw happens), and they force a
#  full snap-cache rebuild unconditionally rather than only when the
#  object-count fingerprint changed - covering in-place edits too.
#  Combined with guarded_modal() now refusing to let Ctrl+Z/Ctrl+Shift+Z
#  reach Blender's global undo stack while a CAD tool is mid-command
#  (see its docstring), this is what makes Undo/Redo - including many
#  presses of either in a row - land on a scene state the addon's own
#  bookkeeping actually agrees with, instead of drifting out of sync
#  after the first one.
# ═══════════════════════════════════════════════════════════════════

def _cad_resync_after_undo_redo(*_args):
    # bpy.app.handlers callback signatures differ across Blender
    # versions (some pass just `scene`, others also a depsgraph) -
    # *_args absorbs whatever's actually sent without caring.
    try:
        _st.snap_cache_dirty = True

        # A running Sketch Mode session's own cached snap points and
        # selected/tracked snap markers are the parts most likely to
        # visibly desync (stale marker positions) - rebuild + purge
        # them immediately rather than waiting for the next idle-tick
        # signature check to notice.
        mgr = _st.sketch_manager_ref
        if mgr is not None:
            context = bpy.context
            try:
                mgr._select_snap_cache = build_snap_cache(context)
                mgr._purge_stale_snap_selection()
                mgr._last_scene_signature = mgr._compute_scene_signature(context)
            except Exception as e:
                print(f"[CAD] undo/redo resync: snap cache rebuild failed: {e}")

        # state.sketch_ref_obj is a direct bpy.types.Object reference
        # (see its comment in state.py) - an Undo that deletes/recreates
        # that specific object leaves it pointing at dead memory
        # (ReferenceError on next access) rather than becoming None the
        # way a name-based lookup would. Proactively drop it here so
        # later code just sees "nothing remembered" instead of crashing
        # the first time it touches the stale reference.
        if _st.sketch_ref_obj is not None:
            try:
                _st.sketch_ref_obj.name  # touch it; raises if the object is dead
            except (ReferenceError, RuntimeError):
                _st.sketch_ref_obj = None

        # Same dead-reference risk for the live Measure session other
        # code reaches into (see state.py's comment on
        # measure_operator_ref) - only relevant if Measure is somehow
        # still the active modal operator across an Undo, but cheap to
        # guard regardless.
        if _st.measure_operator_ref is not None:
            try:
                _st.measure_operator_ref.bl_idname
            except (ReferenceError, RuntimeError):
                _st.measure_operator_ref = None

        # Force every 3D viewport to redraw immediately rather than
        # waiting for the next mouse-move tick, so stale markers don't
        # linger on screen even for a moment after the Undo/Redo itself
        # already completed.
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception as e:
        # A handler that raises is reported by Blender but otherwise
        # harmless to the Undo/Redo itself (which has already
        # completed by the time this runs) - never let a bug here look
        # like Undo/Redo itself failed.
        import traceback
        print(f"[CAD] undo/redo resync handler failed: {e}")
        traceback.print_exc()


def register():
    if _cad_resync_after_undo_redo not in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.append(_cad_resync_after_undo_redo)
    if _cad_resync_after_undo_redo not in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.append(_cad_resync_after_undo_redo)


def unregister():
    if _cad_resync_after_undo_redo in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.remove(_cad_resync_after_undo_redo)
    if _cad_resync_after_undo_redo in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.remove(_cad_resync_after_undo_redo)
