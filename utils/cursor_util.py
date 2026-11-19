# ─────────────────────────────────────────────────────────────────────
#  utils/cursor_util.py
#
#  Shared helper so every custom-crosshair modal tool (Line, Circle,
#  Move, Measure, ...) hides the real OS cursor ONLY while the mouse is
#  actually over the 3D viewport's own drawing surface, and lets the
#  normal OS cursor (arrow / gizmo cursors / whatever Blender's UI
#  wants) show through the instant the mouse crosses into the N-panel,
#  T-panel, header, a right-click menu, or any other interface element
#  - reappearing again the moment the mouse comes back into the
#  viewport, all while the tool keeps running.
#
#  Without this, a tool's invoke() calling cursor_modal_set('NONE')
#  once and only calling cursor_modal_restore() in cancel() leaves the
#  OS cursor hidden for the tool's *entire* lifetime, no matter where
#  the mouse wanders in the meantime - which just makes the cursor
#  invisible over UI elements (nothing is drawn there either, since the
#  crosshair is only drawn by the SpaceView3D draw handler), rather
#  than falling back to the normal arrow like the idle Select tool's
#  own cursor overlay already does (see
#  operators/sketch_mode.py:_update_cursor_visibility /
#  _in_viewport for the reference implementation this mirrors).
# ─────────────────────────────────────────────────────────────────────

import bpy

from .. import state as _st


# Sentinel distinguishing "never checked yet" from "checked and the
# WorkSpaceTool idname couldn't be determined" (get_active_tool_idname
# legitimately returns None sometimes) in _tool_switched_away() below.
_UNSET = object()


def _tool_switched_away(operator, context):
    """
    True once the active T-panel/WorkSpaceTool idname has changed since
    this modal tool started - i.e. the user clicked a *different* tool
    button in the T-panel while this tool was still running.

    The baseline is captured lazily, on this operator's first modal()
    tick, rather than in invoke(): invoke() runs and returns
    RUNNING_MODAL before this operator's own T-panel button has
    necessarily become the active WorkSpaceTool in every case a tool
    can start (a menu entry or keymap can invoke the operator directly
    without touching the T-panel selection at all - see
    ui/menus.py/_KM_* in ui/cad_tpanel_tools.py) - so "whatever
    WorkSpaceTool is active right after invoke()" is the correct
    baseline to diff against, not this tool's *own* T-panel idname.

    Selecting a different T-panel tool never reaches a running modal
    operator as an event - Blender's own tool-button click handling
    changes ws.tools' active tool directly and doesn't dispatch
    anything through the 3D viewport's modal handler stack, so this
    has to be detected by polling here every tick, the same way
    CAD_OT_SketchModeManager._check_tool_activation() polls for it
    while idle in SELECT (see that function's docstring for why it
    can't rely on events for this either). Without this check the old
    tool just keeps running underneath the newly-selected one instead
    of ceding to it - the same situation Esc already fixes; this makes
    a T-panel tool switch cancel exactly the way Esc does.
    """
    current = _st.get_active_tool_idname(context)
    if current is None:
        return False

    baseline = getattr(operator, '_tool_switch_baseline', _UNSET)
    if baseline is _UNSET:
        operator._tool_switch_baseline = current
        return False

    return baseline is not None and current != baseline


def reset_tpanel_to_select(operator, context):
    """
    Call from a modal tool's cleanup instead of a bare
    `bpy.ops.wm.tool_set_by_id(name="builtin.select_box")`.

    Skips the reset when the T-panel's active tool has already moved
    on to something else since this tool started - i.e.
    operator._tool_switch_baseline (set by _tool_switched_away(),
    which guarded_modal() checks every tick - see its docstring) no
    longer matches the current WorkSpaceTool idname. That situation
    means this cleanup is running *because* the user just clicked a
    different T-panel tool button, and this tool's cancel() is only
    running to get out of the new tool's way - forcing the T-panel
    back to Select here would immediately stomp on the very click that
    triggered the cancel, fighting the user's new tool selection
    instead of honouring it. A plain Esc/RMB cancel or a normal
    finish never changes the WorkSpaceTool mid-flight, so baseline
    still matches current in that case and the reset proceeds exactly
    as before.
    """
    baseline = getattr(operator, '_tool_switch_baseline', None)
    current = _st.get_active_tool_idname(context)
    if baseline is not None and current is not None and current != baseline:
        return
    try:
        bpy.ops.wm.tool_set_by_id(name="builtin.select_box")
    except Exception:
        pass


def mouse_in_viewport(context, event):
    """
    True only when the mouse is over the 3D viewport's actual drawing
    surface (its WINDOW region) and NOT over any other, overlapping
    region belonging to that same area - the N-panel/UI sidebar,
    T-panel/TOOLS region, header, tool header, asset shelf, etc.

    Blender keeps dispatching a running modal operator's events with
    context.region fixed to the operator's own WINDOW region for the
    operator's whole lifetime - it does NOT switch context.region to
    the TOOLS region while the mouse is over the left T-panel, or to
    UI while it's over the N-panel. Those regions overlap the WINDOW
    region's own rectangle on screen, so a bounds check against
    context.region alone can't tell them apart; each of
    context.area.regions has to be checked directly against the
    absolute mouse position instead.
    """
    area = context.area
    if area is None or area.type != 'VIEW_3D':
        return False

    window_region = None
    overlay_regions = []
    for r in area.regions:
        if r.width <= 0 or r.height <= 0:
            continue
        if r.type == 'WINDOW':
            window_region = r
        else:
            overlay_regions.append(r)

    if window_region is None:
        return False

    def _hit(r):
        return (r.x <= event.mouse_x < r.x + r.width
                and r.y <= event.mouse_y < r.y + r.height)

    return _hit(window_region) and not any(_hit(r) for r in overlay_regions)


# The viewport navigation gizmo isn't just the axis ball - Blender also
# draws a stacked column of navigate buttons (orbit / pan / zoom in /
# zoom out / perspective-ortho toggle / camera view) just below-left of
# it when "Navigation Gizmos" is enabled, and both are toggled together
# by space_data.show_gizmo_navigate. All of this is drawn *inside* the
# viewport's own WINDOW region - it isn't a separate Blender region
# like the N-panel/T-panel, so mouse_in_viewport() alone can't tell it
# apart from the usable drawing surface, and Blender doesn't expose the
# gizmo cluster's real screen rect through the Python API - there is no
# RNA/bpy query for "is the mouse over a gizmo", so a screen-space
# rectangle is the only way to find it from Python at all.
#
# What we CAN do is stop guessing that rectangle's size outright: the
# on-screen size of the axis ball is driven directly by
# preferences.view.gizmo_size_navigate_v3d (defaults to 80px, but is a
# real user-adjustable preference many people change), and the button
# column stacked below it scales proportionally with it. Scaling the
# reserved box off that live preference, instead of two fixed pixel
# constants, means the box tracks the gizmo's actual configured size
# instead of quietly becoming wrong (too small to fully cover a
# person's enlarged gizmo, or unnecessarily large over a shrunk one)
# the moment someone changes that preference from its default.
def _nav_gizmo_box_size(context):
    """
    Return (width_px, height_px) for the reserved top-right corner box,
    scaled from the user's actual configured navigate-gizmo size rather
    than a fixed guess. The multipliers are tuned against Blender's
    default 80px gizmo (giving ~150x300, matching this add-on's
    original fixed constants) and simply scale linearly from there -
    still an approximation of the cluster's true bounds, since Blender
    doesn't expose those directly, but one that stays correct as the
    person's own gizmo-size preference changes instead of silently
    drifting out of sync with it.
    """
    try:
        gizmo_size = context.preferences.view.gizmo_size_navigate_v3d
    except Exception:
        gizmo_size = 80  # Blender's own default
    width  = max(1, round(gizmo_size * 1.875))
    height = max(1, round(gizmo_size * 3.75))
    return width, height


def _over_nav_gizmo(context, window_region, event):
    if window_region is None:
        return False
    width, height = _nav_gizmo_box_size(context)
    gx0 = window_region.x + window_region.width - width
    gy0 = window_region.y + window_region.height - height
    return event.mouse_x >= gx0 and event.mouse_y >= gy0


def _get_window_region(context):
    area = context.area
    if area is None or area.type != 'VIEW_3D':
        return None
    for r in area.regions:
        if r.width > 0 and r.height > 0 and r.type == 'WINDOW':
            return r
    return None


def is_gizmo_click(context, event):
    """
    True when this is a mouse-button press specifically over the
    viewport navigation-gizmo corner (the axis ball / navigate button
    column) - the one hand-off case that can turn into Blender's own
    multi-event orbit/pan/zoom drag (see handle_ui_handoff's docstring
    on why that needs sticky pass-through). Used to scope the sticky
    mechanism to gizmo clicks only - see handle_ui_handoff.
    """
    return (
        event.type in {'LEFTMOUSE', 'RIGHTMOUSE', 'MIDDLEMOUSE'}
        and event.value == 'PRESS'
        and _over_nav_gizmo(context, _get_window_region(context), event)
    )


def mouse_in_usable_viewport(context, event):
    """
    Like mouse_in_viewport(), but ALSO excludes the viewport
    navigation-gizmo corner (see _over_nav_gizmo/_nav_gizmo_box_size
    above). This is the check tools should use both for cursor
    visibility and for deciding whether a click belongs to the tool at
    all - a click over the gizmo needs to reach Blender's own gizmo
    handling instead of being swallowed as tool input.
    """
    area = context.area
    if area is None or area.type != 'VIEW_3D':
        return False

    window_region = _get_window_region(context)

    return mouse_in_viewport(context, event) and not _over_nav_gizmo(context, window_region, event)


def is_ui_handoff_click(context, event):
    """
    True when this event is a mouse-button press that should be handed
    straight back to Blender rather than consumed by the tool, because
    it isn't over the viewport's own usable drawing surface - the
    navigation-gizmo corner (see mouse_in_usable_viewport), or any
    other Blender UI entirely: the N-panel, T-panel, header, tool
    header, asset shelf, or even a different editor/area altogether.

    Deliberately just `not mouse_in_usable_viewport(...)` and nothing
    stricter - an earlier version of this also required
    mouse_in_viewport(context, event) to be True, which is fine for the
    gizmo corner (it's still inside the viewport's own WINDOW region)
    but wrongly excluded every other panel: mouse_in_viewport() is
    already False the moment the mouse is over the N-panel/T-panel/etc,
    so that extra check meant clicks landing on a panel's own buttons,
    sliders, or text fields never qualified for hand-off at all and
    fell straight through into the tool's own click handling instead -
    e.g. registering as "place point" input rather than reaching the
    panel widget the person was actually trying to use.

    Tools should check this near the top of modal() and, if True,
    restore the cursor and return PASS_THROUGH immediately rather than
    treating the click as tool input.
    """
    return (
        event.type in {'LEFTMOUSE', 'RIGHTMOUSE', 'MIDDLEMOUSE'}
        and event.value == 'PRESS'
        and not mouse_in_usable_viewport(context, event)
    )


def sync_mouse_position(operator, context, event):
    """
    Keep operator._mx/operator._my - the 2D screen-space position every
    tool's own crosshair/cursor-overlay draw handler reads every time it
    draws - in sync with the real mouse on every MOUSEMOVE /
    INBETWEEN_MOUSEMOVE event, unconditionally, before anything else
    (including hand-off) gets a chance to short-circuit and return.

    Every tool used to update self._mx/self._my itself, but only a
    handful of lines *after* calling handle_ui_handoff() at the top of
    its own modal() - and handle_ui_handoff() returns True (the caller
    must immediately return PASS_THROUGH) for exactly the events where
    the mouse is over Blender UI, or where a hand-off drag/right-click
    menu is sticky-in-progress. Every one of those events returned
    before that tool ever reached its own self._mx/self._my line, so
    the position froze at wherever it last was *inside* the viewport
    the instant the mouse left it or a hand-off began - normally
    harmless since the crosshair isn't drawn then anyway
    (cursor_fx_visible() is gated on the same hidden/hand-off state) -
    except a hand-off can occasionally end without ever handing modal()
    a fresh in-viewport MOUSEMOVE to catch up on (e.g. a right-click
    context menu closes on a stray click elsewhere, and the pending
    RIGHTMOUSE-release Blender replays into this operator arrives
    *before* the next real MOUSEMOVE does). In that gap, the crosshair
    reappears at its old, stale position and doesn't catch up until
    some later event - a click - happens to reach the tool's own
    self._mx/self._my line.

    Tracking position here instead, unconditionally and first, decouples
    "where is the mouse" from "is a hand-off in progress right now" -
    the position is always current the instant drawing is allowed to
    resume, with no dependency on which particular event flavour ends
    the hand-off.
    """
    if event.type not in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
        return
    mx, my = event.mouse_region_x, event.mouse_region_y
    if (mx, my) != (getattr(operator, '_mx', None), getattr(operator, '_my', None)):
        operator._mx, operator._my = mx, my
        _tag_redraw(context)


def handle_ui_handoff(operator, context, event):
    """
    Call as the VERY FIRST thing in a modal tool's modal(), before any
    other logic (including update_modal_cursor). Returns True if the
    event was handed off to Blender and the caller must immediately
    `return {'PASS_THROUGH'}` without doing anything else with it.

    A single press over the gizmo corner isn't enough on its own:
    orbit/pan/zoom is a *drag* that Blender's own gizmo handling drives
    across many follow-up MOUSEMOVE events, and those can carry the
    cursor anywhere on screen - including back over the "usable"
    viewport - long before the drag ends. If the tool only passed
    through the opening press and then resumed normal handling of
    those follow-up moves (as a plain update_modal_cursor()-based check
    would), it fights Blender for control of the drag: the view
    stutters/fails to orbit, and the tool's own cursor logic starts
    flipping the OS cursor back off based on the drag's current
    position rather than where it started.

    So instead this makes the hand-off *sticky*: once a press starts
    over the gizmo, operator._ui_handoff_button remembers which button
    is down, and every event is passed straight through - regardless of
    where the mouse currently is - until that exact button is released,
    which is exactly how Blender's own modal operators (e.g. the
    built-in gizmo drag) claim a drag for its full duration.

    A click isn't the only thing that has to reach Blender, though.
    Before any button is ever pressed, Blender's gizmo system relies on
    ordinary MOUSEMOVE events reaching it to run its own hover hit-test
    and update which sub-widget is highlighted - the axis ball versus
    each of the small orbit/pan/zoom/perspective buttons stacked below
    it. If a tool swallows every MOUSEMOVE unconditionally (as they all
    do for their own crosshair/snap tracking), the gizmo group never
    gets to run that hit-test, so nothing is ever "the highlighted
    gizmo" - and a LEFTMOUSE press that arrives afterwards, even though
    it's correctly passed through by is_ui_handoff_click() below, has
    no highlighted widget for Blender to invoke, so the click silently
    does nothing. So plain hover movement over the gizmo corner (no
    button down yet) has to be hand off too, not just the click/drag -
    exactly as if no tool were running at all, matching the Select
    tool's own behaviour there.

    Every hand-off path below also forces a redraw before returning.
    Without it, the moment the mouse leaves the usable viewport, this
    function returns True and the tool's modal() returns PASS_THROUGH
    immediately - skipping straight past the tool's own
    context.area.tag_redraw() call, which normally runs a few lines
    later in modal() and is what actually paints the crosshair's
    hidden/shown state on screen. The crosshair's *cursor_fx_visible()*
    state does flip correctly the instant hand-off starts - but with no
    redraw ever requested, the SpaceView3D draw handler never repaints,
    so the last frame rendered while the mouse was still inside the
    viewport - crosshair included - just stays on screen exactly as-is,
    visually "stuck", until something unrelated happens to force
    Blender to repaint that area (e.g. a click, which triggers a
    redraw as an incidental side effect of Blender's own UI feedback,
    finally showing the correct up-to-date frame). Forcing the redraw
    here removes that dependency on some other, unrelated event coming
    along.

    Before any of the above hand-off logic runs, this also syncs
    operator._mx/operator._my - see sync_mouse_position()'s docstring
    for why that has to happen here, first, rather than further down in
    each tool's own modal().

    "_just_activated": init_modal_cursor() (called from every tool's
    invoke()) sets this True to mark the tool's very first event after
    activation - the event a T-panel click, a menu entry, or a
    keyboard shortcut is standing in for, since invoke() itself never
    gets a modal() callback of its own; the operator's first real
    modal() dispatch only happens on the *next* event Blender delivers.
    A tool's invoke() only runs once the mouse is confirmed to be over
    the usable viewport, so that guaranteed-good first event must
    always be processed normally - never handed off - regardless of
    any other hand-off state (a
    stale sticky _ui_handoff_button left behind by whatever UI
    interaction armed this tool, a boundary rounding case, etc.).
    Without this, that first event could be swallowed by hand-off logic
    that was really meant for a *different* interaction, leaving the
    tool sitting fully initialized-but-inert - cursor hidden, draw
    handler installed, snap cache built - until some unrelated later
    event (typically the user's first click) finally lets a modal()
    call through. Consuming the flag here, unconditionally and before
    every other check, guarantees exactly one such free pass per
    activation and none after.
    """
    sync_mouse_position(operator, context, event)

    if getattr(operator, '_just_activated', False):
        operator._just_activated = False
        return False

    active_button = getattr(operator, '_ui_handoff_button', None)

    if active_button is not None:
        # A hand-off drag is already in progress - keep the real
        # cursor showing and pass every event through untouched,
        # wherever the mouse currently is, until the button that
        # started it comes back up.
        restore_modal_cursor(operator, context)
        if event.type == active_button and event.value == 'RELEASE':
            operator._ui_handoff_button = None
        _tag_redraw(context)
        return True

    if is_ui_handoff_click(context, event):
        # Only a press over the nav-gizmo corner can turn into a
        # multi-event orbit/pan/zoom drag that needs sticky pass-through
        # (see the docstring above) - an ordinary click on a T-panel/
        # N-panel/header button is fully owned and resolved by Blender's
        # own UI widget the instant it's pressed. Blender does not
        # reliably deliver that click's matching RELEASE back down to
        # this modal handler stack the way it does for a real viewport
        # click or a gizmo drag - a widget can consume the whole
        # press/release pair itself. Arming the sticky flag for every
        # off-viewport click, as this used to do, left it permanently
        # stuck "on" after an ordinary panel click whose RELEASE never
        # arrived - every later event, including the mouse re-entering
        # the viewport, then kept being handed off forever, so a tool
        # selected from the T-panel looked fully initialized (cursor
        # hidden, draw handler installed) but never actually processed
        # an event until an unrelated viewport click's own RELEASE
        # happened to clear the stuck flag. Scoping stickiness to only
        # the gizmo corner - the one case that genuinely needs it -
        # means every other UI click is a clean, self-contained one-shot
        # hand-off that can never get stuck.
        if is_gizmo_click(context, event):
            operator._ui_handoff_button = event.type
        restore_cursor_for_handoff(operator, context, event)
        _tag_redraw(context)
        return True

    # No button down and no click just now - but if this is a plain
    # mouse-move (or Blender's finer-grained INBETWEEN_MOUSEMOVE, sent
    # on high-poll-rate mice) anywhere outside the usable viewport -
    # the gizmo corner, or any other panel/editor - hand it off too,
    # so Blender's own hover/highlight/focus handling keeps running
    # there (gizmo widget hit-testing, button hover states, N-panel
    # field focus, etc.), exactly as if no tool were running.
    if (event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}
            and not mouse_in_usable_viewport(context, event)):
        restore_cursor_for_handoff(operator, context, event)
        _tag_redraw(context)
        return True

    return False


def _tag_redraw(context):
    """Best-effort context.area.tag_redraw() - area can occasionally be
    None right at a hand-off boundary (e.g. the mouse has moved to a
    different area/editor entirely), and this is purely a "make sure
    the crosshair's now-hidden state actually gets painted" nicety, not
    something worth an operator-crashing exception over."""
    area = getattr(context, 'area', None)
    if area is not None:
        area.tag_redraw()


def cursor_fx_visible(operator):
    """
    True exactly while the tool's drawn "cursor" overlay - the
    crosshair, the OSnap/OTRACK marker, the little coordinate tooltip
    that tracks the mouse, etc. - should be shown.

    This mirrors operator._cursor_hidden, which update_modal_cursor()
    keeps in sync every event: _cursor_hidden is True exactly when the
    real OS cursor has been hidden via cursor_modal_set('NONE')
    because the mouse is over the viewport's own usable drawing
    surface. Everywhere a tool draws something that stands in for the
    cursor itself, it should gate that draw call behind this function
    rather than drawing unconditionally at self._mx/self._my - drawing
    unconditionally means the crosshair keeps appearing over the
    N-panel/T-panel/header/nav-gizmo even after update_modal_cursor()
    has already restored the real arrow there, which is exactly the
    bug this helper exists to prevent (the drawn crosshair is a
    separate thing from the real OS cursor and doesn't hide itself
    just because the OS cursor reappeared - each drawn element has to
    check this explicitly).
    """
    return bool(getattr(operator, '_cursor_hidden', False))


def init_modal_cursor(operator, context, event):
    """
    Call from a modal tool's invoke() instead of a bare
    `context.window.cursor_modal_set('NONE'); operator._cursor_hidden =
    True` pair. Starts the tool with operator._cursor_hidden matching
    reality - hidden only if the mouse is already over the usable
    viewport at the moment the tool starts, left showing the normal
    system cursor otherwise.

    Every tool used to hide the cursor unconditionally right in
    invoke(), on the assumption that a tool is always started by a
    click inside the viewport. It usually is - but not always: a T-
    panel button, a menu entry, or a keyboard shortcut can all start a
    tool while the mouse is sitting over the T-panel, N-panel, header,
    or a menu. In that case the unconditional hide left the real system
    cursor invisible over UI the person could still see and was still
    interacting with, until the very next MOUSEMOVE let
    update_modal_cursor() notice and restore it - a jarring "cursor
    missing" flash right as the tool starts, and exactly the opposite
    of Blender's own Annotate tool, which only ever shows its custom
    cursor once the mouse is actually inside the viewport.
    """
    if mouse_in_usable_viewport(context, event):
        context.window.cursor_modal_set('NONE')
        operator._cursor_hidden = True
    else:
        operator._cursor_hidden = False

    # See handle_ui_handoff()'s docstring (the "_just_activated" note)
    # for why this is set here.
    operator._just_activated = True


def update_modal_cursor(operator, context, event):
    """
    Call this as the very first thing in a modal tool's modal(), every
    event, for the whole time it owns the OS cursor via
    cursor_modal_set('NONE') / cursor_modal_restore().

    Keeps operator._cursor_hidden (set True alongside the tool's
    initial cursor_modal_set('NONE') call in invoke()) in sync with
    whether the mouse is actually over the viewport's own drawing
    surface right now - hiding the real cursor (so only the custom
    crosshair shows) while it is, and restoring the real cursor the
    instant it isn't, e.g. over the N-panel, a menu, or a gizmo.

    Restoring goes through restore_modal_cursor() rather than a bare
    cursor_modal_restore() call, purely so there is exactly one place in
    the codebase that pairs "was it actually hidden?" with "pop it" -
    every restore in the tool behaves identically because they all
    funnel through the same function.
    """
    hidden = getattr(operator, '_cursor_hidden', False)
    in_viewport = mouse_in_usable_viewport(context, event)
    if in_viewport and not hidden:
        context.window.cursor_modal_set('NONE')
        operator._cursor_hidden = True
    elif not in_viewport and hidden:
        restore_modal_cursor(operator, context)


def restore_cursor_for_handoff(operator, context, event):
    """
    Call right before returning PASS_THROUGH for a mouse-button press
    that hands control to native Blender interaction - the
    navigation-gizmo click case (is_ui_handoff_click), or a right-click
    that opens the native Object Context Menu. Blender delivers no
    further events to this operator while a popup menu or gizmo drag is
    up, so the cursor has to be restored *now*, before handing the
    event off, rather than waiting for a MOUSEMOVE that will never
    come until the menu/gizmo interaction closes.
    """
    restore_modal_cursor(operator, context)


def restore_modal_cursor(operator, context):
    """
    Call from cancel()/finish() instead of a bare
    context.window.cursor_modal_restore(). Only actually restores when
    operator._cursor_hidden is True, so cancel() never pairs a
    cursor_modal_restore() with a cursor_modal_set() that never
    happened (e.g. the tool was cancelled while the mouse was already
    outside the viewport, in which case update_modal_cursor() already
    restored it).

    Calls only cursor_modal_restore() - nothing else. cursor_modal_set()
    / cursor_modal_restore() are a matched push/pop stack: Blender
    remembers exactly what cursor state to return to and repaints it
    correctly on its own. An earlier version of this also force-called
    cursor_set('DEFAULT') right after, on the theory that some platforms
    needed an extra nudge to repaint - but that was masking a real bug
    elsewhere (a push somewhere in this add-on going unmatched, leaving
    the OS cursor stuck for reasons unrelated to this function), not
    fixing one here. Papering over it with a forced DEFAULT also meant
    a restore always ended up showing the plain arrow even when Blender
    itself would have restored to something more specific (a gizmo's
    own cursor, a different tool's cursor, etc.) - fighting Blender's
    own state instead of trusting it. Now that the actual unmatched-push
    sources are fixed at the root (see _update_cursor_visibility() in
    sketch_mode.py and guarded_modal() below), a plain
    cursor_modal_restore() is sufficient and correct.
    """
    if getattr(operator, '_cursor_hidden', False):
        context.window.cursor_modal_restore()
        operator._cursor_hidden = False


def _is_undo_redo_event(event):
    """
    True for the keys Blender's default keymap binds to Undo (Ctrl+Z)
    and Redo (Ctrl+Shift+Z / Ctrl+Y), on PRESS. Used by guarded_modal()
    to keep a mid-operation Ctrl+Z from ever reaching Blender's global
    undo stack while a CAD modal tool is running - see its docstring
    for why that combination corrupts state.
    """
    if event.value != 'PRESS' or not event.ctrl:
        return False
    if event.type == 'Z':
        return True
    if event.type == 'Y' and not event.shift:
        return True
    return False


def guarded_modal(operator, context, event, modal_fn):
    """
    Call as `return guarded_modal(self, context, event, self._modal)`
    from a tool's actual `modal()` method, with the tool's real logic
    moved into `_modal(self, context, event)`.

    ── Blocking Ctrl+Z/Ctrl+Shift+Z while a tool is running ───────────
    Every CAD modal tool (Line, Move, Edit, ...) keeps its own
    in-progress state in plain Python attributes on `self` - a
    remembered starting position to revert to on Esc
    (CAD_OT_SketchMove._start_locs), a snap cache built once at
    invoke() time, direct references to the bpy.types.Object(s) it's
    working on, etc. None of that is part of Blender's undo system;
    it's the tool's own bookkeeping for the *single* undo step it
    intends to push when it finishes.

    Global Undo (Ctrl+Z) triggers a full memfile restore of the whole
    .blend - including whatever mesh/object data the running tool is
    mid-edit on - and does it *underneath* the still-running modal
    operator, since nothing here was stopping that keypress from
    falling through to Blender's default keymap (every tool's own
    handling ends in `return {'PASS_THROUGH'}` for anything it doesn't
    recognise, Ctrl+Z included). The result is exactly the kind of bug
    reported against this system: the tool's cached starting state no
    longer matches the (now time-travelled) scene, so finishing the
    command afterwards can commit a position computed against data
    that no longer exists, silently apply a change that looks skipped
    or wrong, or reference a since-replaced mesh datablock and raise a
    ReferenceError partway through - and because the tool's own
    'UNDO'-tagged operator hasn't finished yet, whatever *it* eventually
    pushes lands on top of a scene state the user's Ctrl+Z never
    actually got to see, making the next Undo behave unpredictably too.

    So: while any CAD modal tool has focus, Ctrl+Z/Ctrl+Shift+Z is
    consumed right here - before it ever reaches modal_fn - with a
    status-bar hint instead of being silently swallowed, rather than
    being passed through to Blender's global undo stack. Esc/RMB still
    cancels the tool (reverting its own preview) same as always; once
    it's finished or cancelled, plain Ctrl+Z sees a scene state that
    matches exactly one clean 'UNDO'-tagged step, which is what makes
    it (and repeated presses of it) behave consistently. This is the
    same convention Blender's own modal transforms follow (translate/
    rotate/scale don't let Ctrl+Z through mid-drag either).

    cursor_modal_set('NONE') / cursor_modal_restore() are a stack: every
    push has to be matched by exactly one pop, or the pushed state is
    left behind on Blender's window forever. Every restore path in this
    tool's own code (cancel(), the various FINISHED/CANCELLED returns,
    update_modal_cursor(), handle_ui_handoff()) already pairs correctly
    - but none of that matters if an *unhandled exception* escapes
    modal() itself. Blender's WM catches it, prints a traceback, and
    silently drops the operator from the modal handler stack - it does
    NOT call the operator's cancel() in that case, since the exception
    interrupted normal control flow rather than the operator asking to
    stop. If the mouse happened to be over the viewport at that moment
    (operator._cursor_hidden True, a 'NONE' currently pushed), that
    push is now orphaned: nothing will ever pop it, so the OS cursor
    stays invisible from then on even out over the T-panel/N-panel,
    with no visible cause - exactly the "random, no clear trigger"
    failure mode this closes off. A single bad snap/raycast/geometry
    edge case anywhere in the tool's ~100+ event branches is enough to
    trigger it, once, ever - it doesn't need to be a repeatable bug.

    Wrapping the whole call site here means any such exception still
    gets logged (so the underlying bug is still visible during
    development), but the tool is guaranteed to call its own cancel()
    - which pops the cursor - before the operator is allowed to end.

    This is also where a T-panel tool switch is caught (see
    _tool_switched_away()'s docstring) and turned into the same clean
    cancel() Esc already triggers - checked once here, ahead of the
    tool's own event handling, so every modal tool gets this for free
    without each one needing its own copy of the check.
    """
    if _tool_switched_away(operator, context):
        try:
            operator.cancel(context)
        except Exception as e:
            import traceback
            print(f"[CAD] {operator.bl_idname}.cancel() raised while "
                  f"cancelling for a T-panel tool switch: {e}")
            traceback.print_exc()
            try:
                restore_modal_cursor(operator, context)
            except Exception:
                pass
        return {'CANCELLED'}

    if _is_undo_redo_event(event):
        operator.report(
            {'WARNING'},
            "Finish or Esc the current command before Undo/Redo"
        )
        return {'RUNNING_MODAL'}

    try:
        return modal_fn(context, event)
    except Exception as e:
        import traceback
        print(f"[CAD] {operator.bl_idname}.modal() raised, cancelling cleanly: {e}")
        traceback.print_exc()
        try:
            operator.cancel(context)
        except Exception as cancel_err:
            # Even the tool's own cancel() choked (on the same bad
            # state that caused the original exception, most likely) -
            # fall back to popping just the cursor directly, since a
            # visible OS cursor matters more than any other cleanup
            # cancel() would have done.
            print(f"[CAD] {operator.bl_idname}.cancel() also raised: {cancel_err}")
            try:
                restore_modal_cursor(operator, context)
            except Exception:
                pass
        return {'CANCELLED'}
