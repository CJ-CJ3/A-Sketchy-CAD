# ─────────────────────────────────────────────────────────────────────
#  ui/measurement_panel.py
#
#  "CAD Dimension" sidebar (N-panel) tab.
#
#  NOTE ON SCOPE: Blender's *Properties editor* tab bar (Object,
#  Modifiers, Data, ...) is a fixed set hard-coded by Blender itself -
#  the Python API doesn't expose a way for an addon to add a new
#  tab/icon there. The supported equivalent for "a brand-new tab" is a
#  3D-viewport sidebar (N-panel) category, which is what this file
#  registers: press N in the viewport and a "CAD Dimension" tab shows
#  up alongside Item/Tool/View.
#
#  Double-clicking a dimension's line/arc or text label in the Measure
#  tool selects it (see CAD_OT_Measure.modal()) and this tab then edits
#  that dimension's:
#
#    * Offset       - 'dim_offset' + 'side_override', how far the
#                     dimension line and its extension lines stand off
#                     from the measured edge (magnitude) and which side
#                     they're drawn on (sign) - standard CAD convention
#    * Text Size    - 'text_size', font size of the value label
#    * Keep Dimension - 'kept', whether it survives the Measure tool
#                     exiting
#
#  All three write straight into the measurement's own dict (the very
#  same dict the viewport draws from every frame), then tag the 3D
#  views for redraw, so edits appear immediately. None of them touch
#  p1/p2/dist, so the measured distance can never change from here.
#
#  ── IMPORTANT: no ID writes during draw() ───────────────────────────
#  Blender runs Panel.draw() in a restricted context where writing to
#  any ID datablock (Scene included) raises
#
#      "Writing to ID classes in this context is not allowed:
#       Scene, Scene datablock, error setting Scene.<UNKNOWN>"
#
#  and the panel body then renders as an error box / blank tab. An
#  earlier version of this file kept the kept-measurements list in a
#  Scene CollectionProperty and rebuilt it (items.clear() / items.add()
#  / cad_measurement_index = ...) from inside _draw_body() to keep it in
#  sync with state.kept_measurements - which is exactly that forbidden
#  write, on every single redraw. The list is therefore drawn directly
#  from state.kept_measurements now (plain rows + a small operator to
#  pick one) and the selected index lives in a module-level global
#  rather than on the Scene, so draw() is strictly read-only and the
#  list can't drift out of sync either. Keep it that way: anything that
#  mutates state belongs in an operator or a property setter, never in
#  draw().
# ─────────────────────────────────────────────────────────────────────

import bpy

from .. import state as _st
from ..utils.math_utils import format_length
from ..gfx.dims import default_dim_offset, min_dim_offset


# Index into state.kept_measurements that the "Kept Measurements" list
# has selected. Module-level (not a Scene property) so that clamping it
# to the list length is a plain Python assignment and can be done from
# inside draw() without tripping Blender's ID-write restriction.
_kept_index = 0


def _label_for(context, m):
    kind = m.get('kind', 'LEN')
    if kind == 'LEN':
        return f"Length: {format_length(context, m.get('dist', 0.0))}"
    elif kind == 'AREA':
        return f"Area: {m.get('area', 0.0):.3f}"
    elif kind == 'RADIUS':
        return f"Radius: {format_length(context, m.get('radius', 0.0))}"
    elif kind == 'DIAMETER':
        return f"Diameter: {format_length(context, m.get('radius', 0.0) * 2.0)}"
    elif kind == 'ARC':
        return f"Arc Length: {format_length(context, m.get('length', 0.0))}"
    else:
        return "Angle"


def _clamped_kept_index():
    """The kept-measurements selection, clamped into range *without*
    writing anything Blender could object to - safe to call from
    draw()."""
    global _kept_index
    n = len(_st.kept_measurements)
    if n == 0:
        return -1
    if _kept_index >= n or _kept_index < 0:
        _kept_index = max(0, min(_kept_index, n - 1))
    return _kept_index


def _live_selection(context):
    """The single measurement currently selected inside a running
    CAD_OT_Measure session (what double-clicking a dimension selects),
    or None."""
    op = _st.measure_operator_ref
    if op is None:
        return None
    selected = getattr(op, '_selected', None)
    measurements = getattr(op, '_measurements', None)
    if selected and measurements is not None and len(selected) == 1:
        i = next(iter(selected))
        if 0 <= i < len(measurements):
            return measurements[i]
    return None


def _active_measurement(context):
    """
    The one measurement the panel's Offset / Text Size / Keep
    Dimension fields currently edit.

    A live double-click selection in a running CAD_OT_Measure session
    wins - that's the same dict the modal operator draws every frame
    from self._measurements, not a disconnected copy, so edits show up
    immediately in the still-running session. Otherwise falls back to
    whichever entry the "Kept Measurements" list has picked.
    """
    m = _live_selection(context)
    if m is not None:
        return m

    idx = _clamped_kept_index()
    if idx >= 0:
        return _st.kept_measurements[idx]
    return None


def _active_measurement_is_live(context):
    """True when _active_measurement() came from a running session's
    live selection rather than state.kept_measurements - i.e. whether
    un-checking Keep Dimension should touch state.kept_measurements at
    all (it shouldn't; the operator's own cancel() reconciles that list
    from its measurements' 'kept' flags when the session ends)."""
    return _live_selection(context) is not None


def _resolve_view3d_area(context):
    """
    The VIEW_3D area to show/hide the sidebar on.

    reveal_dimension_panel()/hide_dimension_panel() are called every
    modal tick (see CAD_OT_Measure._sync_dimension_panel()), and
    context.area during a running modal operator tracks whatever area
    the mouse happens to be over at that instant - not necessarily the
    viewport itself. The mouse only has to drift over the N-panel's own
    UI region, the header, a different editor entirely, etc. for one
    tick, and a version of this that just did
    `area = context.area; if area.type != 'VIEW_3D': return` would
    silently skip that tick's show/hide - which is exactly what made
    the sidebar's auto-hide unreliable before this.

    state.sketch_area_ref is the actual answer to "which viewport is
    Sketch Mode (and therefore Measure) running in" - it's set once
    when Sketch Mode starts and doesn't wobble with mouse position - so
    that's used first. context.area is still tried as a fallback (e.g.
    if called outside a Sketch Mode session), then the first VIEW_3D
    area on the current screen as a last resort, so this never quietly
    does nothing just because of where the mouse happened to be.
    """
    area = _st.sketch_area_ref
    if area is not None and area.type == 'VIEW_3D':
        return area

    area = getattr(context, 'area', None)
    if area is not None and area.type == 'VIEW_3D':
        return area

    screen = getattr(context, 'screen', None)
    if screen is not None:
        for a in screen.areas:
            if a.type == 'VIEW_3D':
                return a

    return None


def reveal_dimension_panel(context):
    """
    Make sure the 3D viewport's N-panel sidebar is open, so
    double-clicking a dimension's line/arc or text label (see
    CAD_OT_Measure.modal()) brings that dimension's editable properties
    into view without the user having to press N themselves.
    """
    area = _resolve_view3d_area(context)
    if area is None:
        return
    try:
        space = area.spaces.active
        space.show_region_ui = True
    except Exception:
        pass
    for region in area.regions:
        if region.type == 'UI':
            # NOTE: there is no Python-scriptable way to switch which
            # N-panel tab is active - bpy.types.Region/Area/Space expose
            # no "active_panel_category" (or equivalent) property on any
            # Blender version; that's a C-only, UI-click-driven concept.
            # So this can only guarantee the sidebar itself is open
            # (above). The hasattr() guard is kept (as a future-proofing
            # no-op) in case some build ever adds one.
            if hasattr(region, 'active_panel_category'):
                try:
                    region.active_panel_category = CAD_PT_dimension_props.bl_category
                except Exception:
                    pass
            region.tag_redraw()
    area.tag_redraw()


def hide_dimension_panel(context):
    """
    Close the 3D viewport's N-panel sidebar entirely - the exact same
    effect as the user pressing N themselves - the counterpart to
    reveal_dimension_panel(), called wherever a dimension selection
    becomes empty (Esc, X/Delete, Shift-click toggling the last one
    off, clicking empty space, starting a new measurement, or the
    Measure tool exiting).

    CAD_PT_dimension_props.poll() already makes the "CAD Dimension" tab
    itself disappear from the tab strip the instant nothing's selected
    - that alone is enough if the user had switched to some other tab
    (Item/Tool/View) in the meantime, since there's then something else
    left for the sidebar to usefully show. But if "CAD Dimension" was
    still the active tab when the selection emptied out, poll() alone
    would just leave the sidebar sitting open on whatever tab Blender
    falls back to - so this closes the sidebar outright, matching
    "nothing to edit -> get out of the way" rather than "show something
    else instead".
    """
    area = _resolve_view3d_area(context)
    if area is None:
        return
    try:
        space = area.spaces.active
        space.show_region_ui = False
    except Exception:
        pass
    for region in area.regions:
        if region.type == 'UI':
            region.tag_redraw()
    area.tag_redraw()


def _tag_redraw_3d_views(context):
    """Redraw every 3D viewport (and their sidebars) so an edit made
    here shows on the dimension immediately."""
    screen = getattr(context, 'screen', None)
    if not screen:
        return
    for area in screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


# ── dynamic (get/set) Scene properties proxying the active
#    measurement's dict entries - these hold no storage of their own,
#    they just read/write whichever measurement is active, so the
#    panel's fields always show that measurement's own values instead
#    of a separately-stored copy that could drift out of sync. Setters
#    only ever run on user interaction, never during draw().
def _get_dim_offset(self):
    """
    The panel's signed "Offset" value: sign encodes which side of the
    measured segment the dimension line sits on (matching standard CAD
    dimensioning - a positive offset one direction, negative the
    other), magnitude encodes the distance, exactly like 'dim_offset'
    always has.

    The sign shown is whichever side is *currently in force* for this
    measurement: its explicit 'side_override' (set by dragging, the
    Tab/arrow-key flip, or a previous edit through this very field) if
    it has one, else +1.0 to match _dim_side()'s implicit default
    before anything's ever overridden it. That keeps this field
    consistent with what's actually on screen rather than a value that
    could silently disagree with the drawn side.
    """
    m = _active_measurement(bpy.context)
    if m is None:
        return default_dim_offset()
    magnitude = float(m.get('dim_offset') or default_dim_offset())
    side = m.get('side_override')
    sign = -1.0 if (side is not None and side < 0) else 1.0
    return magnitude * sign


def _set_dim_offset(self, value):
    """
    Setting a negative value flips the dimension line to the opposite
    side of the measured segment (writes 'side_override', the same
    field dragging or the Tab/arrow-key flip use) and setting a
    positive value flips it back - the sign is a direction toggle, not
    just a display convention. The magnitude (distance from the
    measured segment) is stored in 'dim_offset' exactly as before,
    clamped to min_dim_offset() same as ever so the dimension line can
    never be dragged on top of the geometry it's measuring - a value of
    -0.0 or anything smaller in magnitude than that minimum still
    flips the side, it just can't collapse the distance to zero.
    """
    m = _active_measurement(bpy.context)
    if m is not None:
        value = float(value)
        m['dim_offset'] = max(min_dim_offset(), abs(value))
        # A value of exactly 0.0 keeps whatever side is already in
        # force rather than snapping to +1 - there's no "positive
        # zero" to read a direction from, so leave the existing choice
        # (explicit or implicit) alone instead of overriding it.
        if value != 0.0:
            m['side_override'] = 1.0 if value > 0.0 else -1.0
        _tag_redraw_3d_views(bpy.context)


def _get_text_size(self):
    m = _active_measurement(bpy.context)
    if m is None:
        return 13
    return int(m.get('text_size') or 13)


def _set_text_size(self, value):
    m = _active_measurement(bpy.context)
    if m is not None:
        m['text_size'] = max(6, int(value))
        _tag_redraw_3d_views(bpy.context)


def _get_keep_dimension(self):
    m = _active_measurement(bpy.context)
    if m is None:
        return 1  # 'DONT_KEEP'
    return 0 if m.get('kept', False) else 1  # 'KEEP' / 'DONT_KEEP'


def _set_keep_dimension(self, value):
    global _kept_index
    context = bpy.context
    m = _active_measurement(context)
    if m is None:
        return
    keep = (value == 0)  # 0 == 'KEEP', 1 == 'DONT_KEEP'
    m['kept'] = keep

    # If this measurement is only being edited via state.kept_measurements
    # (no live CAD_OT_Measure session driving it - see
    # _active_measurement_is_live()), that list IS the record of what's
    # kept, so choosing "Don't Keep" here has to remove it right away
    # rather than waiting for a session's cancel() (which won't run) to
    # notice the flag flipped. A live session's own measurements list
    # keeps the un-kept entry around (just no longer flagged) exactly
    # like Tab does, and reconciles state.kept_measurements itself on
    # exit.
    if not keep and not _active_measurement_is_live(context):
        kept = _st.kept_measurements
        if m in kept:
            kept.remove(m)
            _kept_index = max(0, min(_kept_index, len(kept) - 1))

    _tag_redraw_3d_views(context)


class CAD_OT_select_kept_measurement(bpy.types.Operator):
    """Make this kept measurement the one the CAD Dimension fields edit"""
    bl_idname = "cad.select_kept_measurement"
    bl_label = "Select Kept Measurement"
    bl_options = {'INTERNAL'}

    index: bpy.props.IntProperty(default=0)

    def execute(self, context):
        global _kept_index
        _kept_index = self.index
        _tag_redraw_3d_views(context)
        return {'FINISHED'}


class CAD_OT_clear_kept_measurements(bpy.types.Operator):
    """Remove every kept measurement from the viewport"""
    bl_idname = "cad.clear_kept_measurements"
    bl_label = "Clear Kept Measurements"

    @classmethod
    def poll(cls, context):
        return bool(_st.kept_measurements)

    def execute(self, context):
        global _kept_index
        for m in _st.kept_measurements:
            m['kept'] = False
        _st.kept_measurements.clear()
        _kept_index = 0
        _tag_redraw_3d_views(context)
        return {'FINISHED'}


class CAD_PT_dimension_props(bpy.types.Panel):
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "CAD Dimension"
    bl_label       = "CAD Dimension"

    @classmethod
    def poll(cls, context):
        """
        Whether the "CAD Dimension" tab itself shows up at all in the
        N-panel's vertical tab strip.

        An earlier version tried to achieve "hide when nothing's
        selected" by toggling space.show_region_ui (i.e. opening/
        closing the *entire* sidebar). That was the wrong tool for the
        job on two counts: show_region_ui is one flag shared by every
        tab in that sidebar (Item/Tool/View/...), not something scoped
        to this one tab, so it could only ever open or close the whole
        sidebar rather than just make this tab disappear from among
        the others - and toggling it from inside a running modal
        operator (CAD_OT_Measure) turned out not to reliably stick
        across redraws anyway.

        poll() is the actual Blender mechanism for a panel to hide/show
        itself independently of its siblings: returning False here
        removes just this tab (and its icon in the strip) for this
        redraw, leaving Item/Tool/View exactly as they were - and
        Blender re-evaluates it on every redraw, so it tracks selection
        changes (double-click, X/Delete, Esc, Shift-click, clicking
        empty space, picking a different entry in the Kept Measurements
        list, ...) automatically without any of those call sites having
        to explicitly show/hide anything themselves.
        """
        return _active_measurement(context) is not None

    def draw(self, context):
        # The whole body is wrapped in try/except: a Panel.draw() that
        # raises partway through is caught by Blender itself, which then
        # renders *nothing* below the panel's header for the rest of
        # that draw call - no error box, no partial layout, nothing
        # visibly wrong at all in the UI (only a traceback in the system
        # console, which most users never have open). That "silently
        # blank tab" failure mode is worse than any specific bug it
        # might be hiding, so every real error is surfaced in-panel
        # here instead.
        try:
            self._draw_body(context)
        except Exception as exc:
            import traceback
            print("[CAD Dimension panel] draw() failed:")
            traceback.print_exc()
            box = self.layout.box()
            box.label(text="CAD Dimension panel error:", icon='ERROR')
            box.label(text=str(exc))
            box.label(text="See the system console for the full traceback.")

    def _draw_body(self, context):
        layout = self.layout
        scene = context.scene

        m = _active_measurement(context)
        is_live = _active_measurement_is_live(context)

        # The three editable dimension properties always sit right at
        # the top of the tab (not tucked away behind a selection first)
        # - the box greys out when there's nothing selected to apply
        # them to, rather than disappearing, so the layout never jumps
        # around as selection changes.
        box = layout.box()
        header = box.row(align=True)
        if m is not None:
            header.label(text=_label_for(context, m), icon='DRIVER_DISTANCE')
            if is_live:
                header.label(text="", icon='RESTRICT_SELECT_OFF')
        else:
            header.label(text="No dimension selected", icon='INFO')

        body = box.column()
        body.enabled = m is not None
        col = body.column(align=True)
        col.prop(scene, "cad_dim_offset", text="Offset")
        col.prop(scene, "cad_dim_text_size", text="Text Size")
        body.separator()
        body.label(text="Keep Dimension:")
        body.prop(scene, "cad_dim_keep", expand=True)

        if m is not None and m.get('kind') != 'LEN':
            info = box.column(align=True)
            info.label(text="Offset / Text Size only apply", icon='INFO')
            info.label(text="to Length dimensions.")

        layout.separator()
        col = layout.column(align=True)
        col.label(text="Units")
        col.prop(scene.unit_settings, "system", text="")
        if scene.unit_settings.system != 'NONE':
            col.prop(scene.unit_settings, "length_unit", text="")

        layout.separator()
        row = layout.row(align=True)
        row.label(text="Kept Measurements")
        row.operator("cad.clear_kept_measurements", text="", icon='X')

        kept = _st.kept_measurements
        sel = _clamped_kept_index()
        if not kept:
            col = layout.column(align=True)
            col.label(text="None yet - press K on a selected")
            col.label(text="measurement to keep it.")
        else:
            lst = layout.box().column(align=True)
            for i, km in enumerate(kept):
                op_row = lst.row(align=True)
                op = op_row.operator(
                    "cad.select_kept_measurement",
                    text=_label_for(context, km),
                    icon='DRIVER_DISTANCE',
                    emboss=True,
                    depress=(i == sel and not is_live),
                )
                op.index = i

        if m is None:
            col = layout.column(align=True)
            col.label(text="Double-click a dimension, or pick")
            col.label(text="one above, to edit it.")


_CLASSES = (
    CAD_OT_select_kept_measurement,
    CAD_OT_clear_kept_measurements,
    CAD_PT_dimension_props,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.cad_dim_offset = bpy.props.FloatProperty(
        name="Offset",
        description="Distance of the dimension and extension lines from the measured "
                    "edge. Sign controls which side they're drawn on: positive is one "
                    "direction, negative is the opposite (standard CAD dimensioning "
                    "convention) - magnitude is always the distance",
        soft_min=-200.0, soft_max=200.0,
        get=_get_dim_offset, set=_set_dim_offset,
    )
    bpy.types.Scene.cad_dim_text_size = bpy.props.IntProperty(
        name="Text Size",
        description="Font size (px) of the dimension's value label",
        min=6, soft_max=72,
        get=_get_text_size, set=_set_text_size,
    )
    bpy.types.Scene.cad_dim_keep = bpy.props.EnumProperty(
        name="Keep Dimension",
        description="Whether this dimension survives after the Measure tool exits "
                    "(same flag Tab toggles on a selected measurement)",
        items=[
            ('KEEP', "Keep", "Keep this dimension after the Measure tool exits"),
            ('DONT_KEEP', "Don't Keep", "Discard this dimension when the Measure tool exits"),
        ],
        get=_get_keep_dimension, set=_set_keep_dimension,
    )


def unregister():
    for attr in ("cad_dim_keep", "cad_dim_text_size", "cad_dim_offset",
                 # legacy props from the older Scene-collection version
                 # of this panel, removed if an old session left them
                 "cad_measurement_index", "cad_measurement_items"):
        if hasattr(bpy.types.Scene, attr):
            try:
                delattr(bpy.types.Scene, attr)
            except Exception:
                pass

    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
