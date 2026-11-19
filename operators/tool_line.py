# ─────────────────────────────────────────────────────────────────────
#  operators/tool_line.py
#  CAD_OT_DrawLine  –  interactive polyline tool
# ─────────────────────────────────────────────────────────────────────

import math
import time
import bpy
from mathutils import Vector

from ..utils.math_utils  import (
    build_snap_cache, apply_snaps, apply_axis_lock, apply_polar_tracking, find_highlight_candidate,
    mouse_to_3d, world_to_screen,
    world_ang_to_display, display_ang_to_rad, parse_typed_length, format_length, format_angle,
)
from ..utils.otrack import (
    SnapAcquisitionManager, update_tracking, resolve_typed_distance_ray,
    snap_extra_ray_to_tracking, SNAP_TYPE,
)
from ..utils.cad_objects import create_cad_object, pick_reference_object
from .. import state as _st
from ..utils.cursor_util import (
    update_modal_cursor, restore_modal_cursor, cursor_fx_visible, init_modal_cursor,
    guarded_modal,
    handle_ui_handoff,
    reset_tpanel_to_select,
)
from ..gfx.primitives    import (
    gfx_lines, gfx_loop, gfx_dot, gfx_text,
    gfx_dashed_line, gfx_dashed_arc,
    gfx_rect_fill,

)
from ..gfx.markers import gfx_snap_marker, draw_highlight_marker, draw_crosshair, draw_axis_guide, draw_vertex_square, draw_selected_snap_points, draw_osnap_tracking, draw_angle_tooltip, draw_command_tooltip
from ..gfx.dims    import draw_float_dim, draw_dim_rows
from ..gfx.preview import (
    draw_reference_ray, draw_direction_guide, draw_dim_preview, draw_angle_guide,
)


# ── helper: mid-segment tick ──────────────────────────────────────────
def _seg_tick(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    ln = math.hypot(dx, dy)
    if ln < 2:
        return
    px, py  = -dy / ln * 4, dx / ln * 4
    mx2, my2 = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    gfx_lines(
        [(mx2 - px, my2 - py), (mx2 + px, my2 + py)],
        (1, 1, 0, 0.50), 1.0,
    )


class CAD_OT_DrawLine(bpy.types.Operator):
    bl_idname  = "cad.draw_line"
    bl_label   = "CAD Line"
    bl_options = {'REGISTER', 'UNDO'}

    # ── invoke ────────────────────────────────────────────────────────
    def invoke(self, context, event):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        self._context     = context   # kept for helpers that don't take context directly (unit-aware parsing, etc.)
        self._pts        = []
        self._raw        = Vector((0, 0, 0))
        self._cur        = Vector((0, 0, 0))
        self._snap_type  = 'FREE'
        self._highlight  = None   # (Vector, stype) | None - wider-radius early-feedback candidate
        self._polar_angle = None  # float degrees | None - active Polar Tracking direction
        self._axis       = None
        self._inp        = ''
        self._inp_mode   = 'LENGTH'   # or 'ANGLE'
        self._lock_len   = None
        self._lock_ang   = None

        # ── Object Snap Tracking (OTRACK) dwell-acquisition state ──────
        # Hovering the cursor over the same valid snap point for
        # _TRACK_DWELL seconds acquires it (while still hovering) into
        # _st.selection.tracked_points, projecting alignment guides.
        # Multiple points can be acquired simultaneously.
        self._otrack = SnapAcquisitionManager()
        self._otrack.reset(clear_points=True)

        # ── Hover-acquired temporary base point (implicit first point) ──
        # Hovering a valid snap candidate for _BASE_ACQUIRE_TIME, before
        # any point has been placed and before typing starts, "acquires"
        # it as a hidden basePoint - a temporary coordinate origin the
        # user never actually clicked. It stays locked even once the
        # cursor moves elsewhere. Typing a distance + Enter then
        # computes P = basePoint + distance * normalize(cursor -
        # basePoint) and implicitly commits it as the line's first
        # point, exactly as if the user had clicked there.
        self._base_candidate_key   = None
        self._base_candidate_since = 0.0
        self._acquired_base        = None   # Vector | None
        self._typed_ray_frozen     = None    # (origin, direction) | None - locked at first keystroke
        self._mid_held   = False
        self._mx         = event.mouse_region_x
        self._my         = event.mouse_region_y
        self._snap_cache = build_snap_cache(context)

        # If an object was selected before drawing started, the
        # finished line goes into the same collection ("layer") as it.
        # Prefer the reference object tracked continuously by the
        # Sketch Mode manager (state.sketch_ref_obj) - this survives
        # the brief selection-change that can happen right when a
        # T-panel tool button is clicked. Fall back to a live check.
        self._ref_obj = _st.sketch_ref_obj or pick_reference_object(context)

        init_modal_cursor(self, context, event)
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL'
        )
        context.scene.cad_settings.active_tool = "LINE"
        context.window_manager.modal_handler_add(self)
        self._hdr(context)
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
        # Sketch Mode was exited (e.g. ESC/Return-to-Object from the
        # manager) while this draw tool was still running - stop
        # immediately so it doesn't keep operating outside Sketch Mode.
        if not context.scene.cad_settings.sketch_mode:
            self.cancel(context)
            return {'CANCELLED'}

        context.area.tag_redraw()
        self._mx, self._my = event.mouse_region_x, event.mouse_region_y

        if event.type == 'MIDDLEMOUSE':
            self._mid_held = (event.value == 'PRESS')
            return {'PASS_THROUGH'}

        # Alt + mouse wheel  →  cycle the single active running snap
        # type (same as the Select tool's Alt-menu scroll behaviour).
        # sketch_mode.py's own modal is fully suspended while this
        # operator's modal owns events, so that logic has to be
        # duplicated here rather than relying on it "still running
        # underneath" - it isn't.
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} and event.value == 'PRESS' and event.alt:
            modes = [m[2] for m in _st.SelectionState.SNAP_MODES]
            if modes:
                try:
                    idx = modes.index(_st.selection.snap_mode)
                except ValueError:
                    idx = 0
                step = -1 if event.type == 'WHEELUPMOUSE' else 1
                _st.SelectionState.toggle_snap_type(modes[(idx + step) % len(modes)])
                context.area.tag_redraw()
                self._on_move(context)
            return {'RUNNING_MODAL'}
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN'}:
            return {'PASS_THROUGH'}

        # Alt + letter  →  toggle that running snap type's membership
        # in the active set (same multi-select behaviour as the Select
        # tool's Alt-menu: Alt+A then Alt+M leaves both Endpoint and
        # Midpoint active). Duplicated here for the same reason as above.
        if event.value == 'PRESS' and event.alt and not event.ctrl and not event.shift:
            _letter_to_snap = {m[0]: m[2] for m in _st.SelectionState.SNAP_MODES}
            if event.type in _letter_to_snap:
                _st.SelectionState.toggle_snap_membership(_letter_to_snap[event.type])
                context.area.tag_redraw()
                self._on_move(context)
                return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            if self._mid_held:
                return {'PASS_THROUGH'}
            self._on_move(context)
            return {'RUNNING_MODAL'}

        if event.value != 'PRESS':
            return {'RUNNING_MODAL'}

        # ── numeric input ─────────────────────────────────────────────
        ch = event.ascii
        if ch and ch in '0123456789.':
            if not self._inp and not self._pts:
                # First keystroke of a typed distance before any point
                # is placed: lock in the ray (acquired marker + axis
                # direction) now, once, rather than recomputing it
                # every frame while the rest of the digits are typed -
                # this is what the origin/direction must not drift
                # from while you finish typing the number.
                self._typed_ray_frozen = self._typed_distance_ray()
            self._inp += ch
            self._refresh_cur()
            return {'RUNNING_MODAL'}
        if ch == '-' and not self._inp:
            self._inp = '-'
            self._refresh_cur()
            return {'RUNNING_MODAL'}
        if event.type == 'BACK_SPACE':
            if self._inp:
                self._inp = self._inp[:-1]
            else:
                if self._inp_mode == 'LENGTH':
                    self._lock_len = None
                else:
                    self._lock_ang = None
            if not self._inp:
                self._typed_ray_frozen = None
            self._refresh_cur()
            return {'RUNNING_MODAL'}

        # ── axis lock ─────────────────────────────────────────────────
        if event.type in {'X', 'Y', 'Z'} and not (event.ctrl or event.shift):
            if event.type == 'Z':
                self._axis = None
            else:
                self._axis = None if self._axis == event.type else event.type
            self._lock_ang = None
            self._inp      = ''
            self._typed_ray_frozen = None
            self._on_move(context)
            self._hdr(context)
            return {'RUNNING_MODAL'}

        # ── switch input mode ─────────────────────────────────────────
        if event.type in {'TAB', 'LEFT_ALT', 'RIGHT_ALT'}:
            self._flip_mode(context)
            return {'RUNNING_MODAL'}

        # ── place point ───────────────────────────────────────────────
        if event.type == 'LEFTMOUSE':
            self._commit_and_place(context)
            return {'RUNNING_MODAL'}

        # ── finish / close ────────────────────────────────────────────
        if event.type in {'RET', 'NUMPAD_ENTER'}:
            if not self._pts and self._inp:
                ray = self._typed_distance_ray()
                if ray is not None:
                    # Implicit first-point commit: the user never
                    # clicked - they acquired a reference (either an
                    # OTRACK marker with a locked H/V guide, or a
                    # plain hover-acquired basePoint), moved the
                    # cursor to imply a direction, and typed a
                    # distance. Compute P = origin + distance*direction
                    # and commit it exactly as if it had been clicked.
                    try:
                        distance = parse_typed_length(context, self._inp)
                    except ValueError:
                        distance = None
                    if distance is not None:
                        origin, direction = ray
                        p = origin + direction * distance
                        self._pts.append(p)
                        self._sync_snap_cache()
                        self._inp              = ''
                        self._acquired_base    = None
                        self._typed_ray_frozen = None
                        self._clear_tracking(context)
                        self._hdr(context)
                    return {'RUNNING_MODAL'}
            if (self._inp or self._lock_len or self._lock_ang) and self._pts:
                self._commit_and_place(context)
            elif len(self._pts) >= 2:
                self._build(context)
                self.cancel(context)
                return {'FINISHED'}
            elif len(self._pts) <= 1:
                self.cancel(context)
                return {'CANCELLED'}
            return {'RUNNING_MODAL'}

        if event.type == 'C' and not (event.ctrl or event.shift):
            if len(self._pts) >= 3:
                self._pts.append(self._pts[0].copy())
                self._build(context, closed=True)
                self.cancel(context)
                return {'FINISHED'}
            return {'RUNNING_MODAL'}

        if event.type == 'T' and not (event.ctrl or event.shift or event.alt):
            # Clear all acquired OSNAP tracking points without
            # cancelling the line in progress.
            if _st.selection.tracked_points:
                self._clear_tracking(context)
            return {'RUNNING_MODAL'}

        if event.type == 'ESC':
            if _st.selection.tracked_points:
                # Clear tracking first; a second Esc cancels the line.
                self._clear_tracking(context)
                return {'RUNNING_MODAL'}
            if not self._pts and self._acquired_base is not None:
                # Then clear an acquired-but-not-yet-committed hover
                # base point.
                self._acquired_base        = None
                self._base_candidate_key   = None
                self._inp                  = ''
                self._typed_ray_frozen     = None
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            self.cancel(context)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ── private helpers ───────────────────────────────────────────────

    def _hdr(self, context):
        lock = f"  [{self._axis} LOCKED]" if self._axis else ""
        context.area.header_text_set(
            f"LINE ({len(self._pts)} pts){lock}  │  "
            f"Click=place  Enter=finish  C=close  Esc=cancel  "
            f"T=clear tracking  "
            f"X/Y=axis  Tab=switch"
        )

    def _flip_mode(self, context):
        if self._inp:
            try:
                if self._inp_mode == 'LENGTH':
                    self._lock_len = parse_typed_length(context, self._inp)
                else:
                    self._lock_ang = float(self._inp)
                    self._axis     = None
            except ValueError:
                pass
        self._inp_mode = 'ANGLE' if self._inp_mode == 'LENGTH' else 'LENGTH'
        self._inp = ''
        self._typed_ray_frozen = None
        self._refresh_cur()

    _BASE_ACQUIRE_TIME = 0.25   # seconds to acquire a hover candidate as basePoint

    def _update_hover_base_point(self, context, stype):
        """
        Before any point has been placed and before the user starts
        typing a distance, continuously track whichever valid snap
        candidate has been hovered for _BASE_ACQUIRE_TIME - acquiring
        it as a temporary basePoint. Once typing starts, the acquired
        point freezes; it is not affected by the cursor moving away
        (that's the whole point - "hover then move elsewhere and still
        use that point").
        """
        if self._pts or self._inp:
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

    def _on_move(self, context):
        raw3d = mouse_to_3d(context, self._mx, self._my)
        if not raw3d:
            return
        anchor = self._pts[-1] if self._pts else None
        try:
            snapped, stype = apply_snaps(context, raw3d, self._snap_cache,
                                      snap_filter=_st.selection.get_active_stypes(),
                                      anchor=anchor)
        except Exception as e:
            # apply_snaps() only behaves differently once `anchor` is
            # set (Tangent/Parallel/Perpendicular + angle-increment
            # candidates all require it) - that's the one thing that
            # changes the instant self._pts stops being empty. If a
            # bug in any of those anchor-only paths throws, fall back
            # to a plain search with no anchor rather than losing
            # basic Endpoint/Midpoint/Center detection entirely for
            # the rest of the operation.
            print(f"[CAD] apply_snaps(anchor set) error, retrying without anchor: {e}")
            try:
                snapped, stype = apply_snaps(context, raw3d, self._snap_cache,
                                          snap_filter=_st.selection.get_active_stypes(),
                                          anchor=None)
            except Exception as e2:
                print(f"[CAD] apply_snaps error: {e2}")
                snapped, stype = raw3d, 'FREE'
        self._snap_type = stype

        if self._axis and self._pts:
            snapped = apply_axis_lock(snapped, self._pts[-1], self._axis)

        # Everything below this point is early-feedback/guidance layered
        # on top of the real OSNAP result above (highlight radius,
        # Polar Tracking) - each wrapped so that a bug in either can
        # never prevent the core snapped/stype result from being
        # applied. This file has no other exception handling, so an
        # unguarded error here would silently stop self._raw from ever
        # updating again, which looks indistinguishable from "snapping
        # stopped working" even though apply_snaps() above ran fine.
        self._highlight = None
        try:
            if stype in ('FREE', 'GRID', 'INCREMENT'):
                self._highlight = find_highlight_candidate(
                    context, self._mx, self._my, raw3d, self._snap_cache,
                    snap_filter=_st.selection.get_active_stypes(),
                    anchor=self._pts[-1] if self._pts else None,
                )
        except Exception as e:
            print(f"[CAD] highlight search error: {e}")

        self._polar_angle = None
        try:
            _REAL_OSNAP_TYPES = (
                'VERTEX', 'ENDPOINT', 'MIDPOINT', 'CENTER', 'INTERSECTION',
                'NEAREST', 'QUADRANT', 'TANGENT', 'PERPENDICULAR', 'PARALLEL',
            )
            if self._pts and not self._axis and stype not in _REAL_OSNAP_TYPES:
                polar = apply_polar_tracking(context, raw3d, self._pts[-1])
                if polar is not None:
                    snapped, self._polar_angle = polar

                    # Cross-system snap: once our own green Polar
                    # Tracking guide is engaged, see if it crosses an
                    # active OTRACK guide near the cursor - same
                    # widened tolerance/hysteresis as an ordinary
                    # OTRACK intersection, so the two guide systems
                    # lock onto their meeting point just as readily as
                    # two OTRACK guides would with each other.
                    if self._otrack.points:
                        ray_dir = snapped - self._pts[-1]
                        if ray_dir.length > 1e-9:
                            xhit = snap_extra_ray_to_tracking(
                                context, raw3d, self._pts[-1], ray_dir,
                            )
                            if xhit is not None:
                                snapped = xhit
                                stype   = SNAP_TYPE
                                self._snap_type = stype
        except Exception as e:
            print(f"[CAD] polar tracking error: {e}")

        self._raw = snapped
        self._refresh_cur()
        update_tracking(self._otrack, snapped, stype)
        self._update_hover_base_point(context, stype)

    def _typed_distance_ray(self):
        """(origin, direction) to use for typed-distance entry before
        any point has been placed, or None if nothing applies.

        Prefers a ray already frozen at the moment typing started
        (see the numeric-input handler) - falling back to a fresh
        computation only when nothing was frozen (e.g. this is being
        called from _refresh_cur before any digit has been typed, for
        live-preview purposes unrelated to a committed typed value).

        The fresh computation itself prefers a currently-active,
        axis-locked OTRACK guide (so the typed distance is constrained
        to horizontal/vertical from the acquired marker, matching
        AutoCAD) over the generic hover-acquired basePoint, which
        points at an arbitrary angle toward wherever the cursor
        happens to be. When two guides are both active (the cursor is
        near an intersection of two acquired points), typed distance
        is ambiguous, so it's left to plain click-to-place instead of
        guessing which axis was meant.
        """
        if self._pts:
            return None

        if self._typed_ray_frozen is not None:
            return self._typed_ray_frozen

        if self._otrack.points:
            ray = resolve_typed_distance_ray(self._otrack, self._raw)
            if ray is not None:
                return ray

        if self._acquired_base is not None:
            direction = self._raw - self._acquired_base
            if direction.length < 1e-6:
                direction = Vector((1, 0, 0))
            return self._acquired_base, direction.normalized()

        return None

    def _refresh_cur(self):
        if not self._pts:
            if self._inp:
                ray = self._typed_distance_ray()
                if ray is not None:
                    try:
                        distance = parse_typed_length(self._context, self._inp)
                        origin, direction = ray
                        self._cur = origin + direction * distance
                        return
                    except ValueError:
                        pass
            self._cur = self._raw.copy()
            return

        last    = self._pts[-1]
        eff_len = self._lock_len
        eff_ang = self._lock_ang

        if self._inp_mode == 'LENGTH' and self._inp:
            try:
                eff_len = parse_typed_length(self._context, self._inp)
            except ValueError:
                pass
        if self._inp_mode == 'ANGLE' and self._inp:
            try:
                eff_ang = float(self._inp)
            except ValueError:
                pass

        if eff_ang is not None:
            rad     = display_ang_to_rad(eff_ang)
            dir_vec = Vector((math.cos(rad), math.sin(rad), 0))
            self._cur = (
                last + dir_vec * abs(eff_len) if eff_len is not None
                else last + dir_vec * max((self._raw - last).length, 0.01)
            )
        elif eff_len is not None:
            d = self._raw - last
            if d.length < 1e-6:
                d = Vector((1, 0, 0))
            self._cur = last + d.normalized() * abs(eff_len)
        else:
            self._cur = self._raw.copy()

    def _sync_snap_cache(self):
        """
        Feed the in-progress polyline's own points/segments into
        self._snap_cache using the exact same bucket format
        build_snap_cache() produces for real geometry - VERTEX,
        ENDPOINT, MIDPOINT, and NEAREST_EDGES.

        This is the reuse point: apply_snaps() has no idea whether a
        candidate came from pre-existing scene geometry or from here -
        it just searches whatever's in the cache, honouring whichever
        snap types are enabled in the Alt menu. So Endpoint, Midpoint,
        Nearest, and Perpendicular (which shares the VERTEX pool) all
        become available against the line you're currently drawing,
        including snapping/closing back onto its own start point,
        without any separate closed-loop-specific logic - it's just
        more entries in the one real snap-candidate pool.
        """
        cache = self._snap_cache
        for key in ('VERTEX', 'ENDPOINT', 'MIDPOINT'):
            cache.setdefault(key, [])
        cache.setdefault('NEAREST_EDGES', [])

        for p in self._pts:
            if not any((p - q).length < 1e-6 for q in cache['VERTEX']):
                cache['VERTEX'].append(p.copy())
            if not any((p - q).length < 1e-6 for q in cache['ENDPOINT']):
                cache['ENDPOINT'].append(p.copy())

        for i in range(len(self._pts) - 1):
            p1, p2 = self._pts[i], self._pts[i + 1]
            cache['NEAREST_EDGES'].append((p1.copy(), p2.copy()))
            mid = (p1 + p2) * 0.5
            if not any((mid - q).length < 1e-6 for q in cache['MIDPOINT']):
                cache['MIDPOINT'].append(mid)

    def _commit_and_place(self, context):
        self._pts.append(self._cur.copy())
        self._sync_snap_cache()
        self._polar_angle      = None
        self._inp              = ''
        self._lock_len         = None
        self._lock_ang         = None
        self._typed_ray_frozen = None
        self._clear_tracking(context)
        self._hdr(context)

    def _clear_tracking(self, context):
        """
        Confirmation step: once a point is actually placed, remove all
        temporary OSNAP tracking references and their overlay graphics
        - they were only ever construction aids for locating that one
        point, not something that persists into the next segment.
        """
        self._otrack.reset(clear_points=True)
        self._typed_ray_frozen = None
        context.area.tag_redraw()

    def _build(self, context, closed=False):
        pts   = self._pts
        verts = [(p.x, p.y, 0.0) for p in pts]
        edges = [(i, i + 1) for i in range(len(pts) - 1)]
        total = sum(
            (pts[i + 1] - pts[i]).length for i in range(len(pts) - 1)
        )
        create_cad_object(
            context, "CAD_Line", verts, edges, "polyline",
            {"point_count": len(pts), "total_length": total, "closed": closed},
            reference_obj=self._ref_obj,
        )
        context.scene.cad_settings.last_length = total

    # ── draw callback ─────────────────────────────────────────────────

    def _draw(self, context):
        try:
            self._draw_impl(context)
        except Exception as e:
            print(f"[CAD] tool_line draw error: {e}")

    def _safe(self, label, fn, *args, **kwargs):
        """Run one overlay sub-section in isolation.

        Every visual piece (arc annotation, angle box, command
        tooltip, snap marker, crosshair, ...) used to live in one
        unbroken chain inside a single try/except around the whole
        draw call. If any single piece raised, Blender's draw handler
        silently swallowed it - but everything coded *after* that
        point in the function simply never ran for that frame, while
        everything before it kept rendering fine. That produced
        exactly the "half the overlay is missing" symptom: one
        section failing invisibly took the rest down with it. Running
        each section through this wrapper means one bad piece just
        skips itself (logged once, not spammed) instead of hiding
        every element that was supposed to be drawn after it.
        """
        try:
            fn(*args, **kwargs)
        except Exception as e:
            if label not in self._logged_draw_errors:
                self._logged_draw_errors.add(label)
                import traceback
                print(f"[CAD] tool_line draw error in '{label}': {e}")
                traceback.print_exc()

    def _draw_impl(self, context):
        if not hasattr(self, '_logged_draw_errors'):
            self._logged_draw_errors = set()

        # The crosshair/snap-marker/coordinate-readout family below all
        # stand in for "the cursor" while this tool is active - they
        # must only be drawn while the real OS cursor is actually
        # hidden (i.e. the mouse is over the usable viewport), or they
        # keep floating over the N-panel/T-panel/header/nav-gizmo even
        # after the real arrow cursor has already reappeared there.
        show_cursor_fx = cursor_fx_visible(self)

        self._safe('selected_snap_points', draw_selected_snap_points, context)
        if show_cursor_fx:
            self._safe('osnap_tracking', draw_osnap_tracking, context, self._mx, self._my)

        mx, my  = self._mx, self._my
        has_pts = len(self._pts) >= 1
        cur_2d  = world_to_screen(context, self._cur)

        # (Polar Tracking's color feedback is applied directly to the
        # real in-progress line segment below, not drawn as a separate
        # line here - a separate dashed overlay ended up hidden behind
        # the solid segment anyway.)

        # Only show the little endpoint squares (on the line's own
        # placed points) when the active Alt-menu snap mode actually
        # includes Vertex/Endpoint - otherwise every other snap
        # indicator (Midpoint, Center, etc.) stays hidden while this
        # one kept showing regardless of what was selected.
        _snap_filter  = _st.selection.get_active_stypes()
        # The draw tool's own endpoint square (on placed click-points)
        # has been removed entirely - it's no longer drawn regardless
        # of the active Alt-menu snap mode.
        show_endpoint = False

        # Axis guide
        if self._axis and has_pts:
            draw_axis_guide(context, self._pts[-1], self._axis)

        # Already-placed segments
        if len(self._pts) >= 2:
            s2 = [world_to_screen(context, p) for p in self._pts]
            if all(s2):
                gfx_lines(s2, (1, 1, 1, 0.95), 2.4)
                if show_endpoint:
                    for p in s2:
                        draw_vertex_square(
                            p, 4,
                            fill_col=(1, 1, 0, 0.18),
                            outline_col=(1, 1, 0, 0.95),
                            outline_w=1.6,
                        )
                for i in range(len(s2) - 1):
                    _seg_tick(s2[i], s2[i + 1])

        seg_len = 0.0
        seg_ang = 0.0

        if has_pts and cur_2d:
            last_3d = self._pts[-1]
            last_2d = world_to_screen(context, last_3d)

            if last_2d:
                seg_col = (1.00, 1.00, 1.00, 0.95)   # always white rubber-band
                gfx_lines([last_2d, cur_2d], seg_col, 2.4)
                if self._polar_angle is not None:
                    gfx_text(
                        f"Polar: {format_angle(context, math.radians(self._polar_angle))}",
                        cur_2d[0] + 14, cur_2d[1] + 14,
                        11, seg_col,
                    )
                if show_endpoint:
                    draw_vertex_square(
                        last_2d, 5,
                        fill_col=(1, 1, 0, 0.18),
                        outline_col=(1, 1, 0, 0.95),
                        outline_w=1.8,
                    )
                    draw_vertex_square(
                        cur_2d, 5,
                        fill_col=(1, 1, 0, 0.18),
                        outline_col=(1, 1, 0, 0.95),
                        outline_w=1.8,
                    )
                delta   = self._cur - last_3d
                seg_len = delta.length
                seg_ang = world_ang_to_display(math.atan2(delta.y, delta.x))

                if delta.length > 1e-4:
                    # Direction guide - green dashed line starting at
                    # the cursor and extending onward toward infinity
                    # in the direction away from the segment's base
                    # point. Only shown once Polar Tracking has
                    # actually snapped to a configured increment (0°,
                    # 45°, 90°, ...) - it disappears the moment the
                    # cursor drifts outside that angle tolerance again.
                    if self._polar_angle is not None:
                        self._safe(
                            'direction_guide', draw_direction_guide,
                            context, world_to_screen, last_3d, self._cur, cur_2d,
                        )

                    # Angle guide - shared with the Mirror tool via
                    # gfx.preview.draw_angle_guide() (baseline +
                    # dashed arc + live angle box), wrapped in its own
                    # try/except like every other overlay element here.
                    try:
                        _deg_unit = context.scene.unit_settings.system_rotation != 'RADIANS'
                        a_str = (
                            self._inp + ("°▌" if _deg_unit else " rad▌")
                            if self._inp_mode == 'ANGLE' and self._inp
                            else f"\U0001f512 {format_angle(context, math.radians(self._lock_ang))}"
                            if self._lock_ang is not None
                            else format_angle(context, math.atan2(delta.y, delta.x))
                        )
                        draw_angle_guide(
                            context, world_to_screen, last_2d, last_3d, cur_2d, a_str,
                        )
                    except Exception as e:
                        if not hasattr(self, '_logged_draw_errors'):
                            self._logged_draw_errors = set()
                        if 'angle_geom' not in self._logged_draw_errors:
                            self._logged_draw_errors.add('angle_geom')
                            print(f"[CAD] angle guide geometry failed: {e}")

                    # Dynamic dimension line - AutoCAD-style live
                    # preview, shared with Move/Stretch via
                    # gfx.preview.draw_dim_preview() so every tool's
                    # distance readout looks and behaves identically.
                    if seg_len > 0.05:
                        eff_l = self._lock_len
                        if self._inp_mode == 'LENGTH' and self._inp:
                            try:
                                eff_l = parse_typed_length(context, self._inp)
                            except ValueError:
                                pass
                        l_str = (
                            self._inp + "▌"
                            if self._inp_mode == 'LENGTH' and self._inp
                            else f"\U0001f512 {format_length(context, self._lock_len)}"
                            if self._lock_len is not None
                            else f"{format_length(context, seg_len)}"
                        )
                        self._safe(
                            'dim_preview', draw_dim_preview,
                            last_2d, cur_2d, l_str,
                            active=(self._inp_mode == 'LENGTH'),
                            locked=(self._lock_len is not None),
                        )

        # Command-line style tooltip when placing the next point
        if show_cursor_fx and has_pts and cur_2d:
            self._safe(
                'command_tooltip', draw_command_tooltip,
                cur_2d[0], cur_2d[1], "Specify next point or",
            )

        # Snap marker: a real, engaged snap takes priority; otherwise
        # show the wider-radius highlight candidate (at *its* position,
        # not the cursor's) as early feedback - the cursor itself
        # hasn't snapped to it yet, only the marker is showing early.
        if show_cursor_fx and cur_2d:
            self._safe('snap_marker', gfx_snap_marker, cur_2d, self._snap_type)
        if show_cursor_fx and self._highlight is not None:
            hpt, hstype = self._highlight
            h2d = world_to_screen(context, hpt)
            if h2d:
                self._safe('highlight_marker', draw_highlight_marker, h2d, hstype)

        # Crosshair snaps onto the current (possibly snapped) point
        # rather than the raw mouse position.
        if show_cursor_fx:
            cx, cy = cur_2d if cur_2d else (mx, my)
            self._safe(
                'crosshair', draw_crosshair,
                context, cx, cy, self._axis, show_box=False, gap=False,
            )  # Active tool: crosshair only

        # Coordinate readout when no points placed yet - except while a
        # distance is actively being typed (pre-first-point OTRACK/base
        # entry), in which case the box must echo the typed digits
        # themselves, not the raw cursor coordinates, or the box looks
        # blank/wrong while the user types.
        if show_cursor_fx and not has_pts:
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

        mode_lbl = f"  [{'L' if self._inp_mode == 'LENGTH' else '∠'} field]"
        gfx_text(
            f"LINE  {len(self._pts)} pts{mode_lbl}"
            f"   X {self._cur.x:>8.3f}   Y {self._cur.y:>8.3f}"
            f"   {seg_ang:+.2f}°"
            f"   Enter=finish  C=close  Esc=cancel",
            10, 55, 12, (1.0, 0.85, 0.2, 1.0),
        )
