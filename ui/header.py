import bpy
from .. import state as _st


# ════════════════════════════════════════════════════════════════════
#  PATCHED HEADER DRAW
#
#  _patched_header_draw REPLACES bpy.types.VIEW3D_HT_header.draw
#  entirely, giving us full control over item order.
#
#  Three branches:
#    1. SKETCH MODE   – fully custom header (unchanged)
#    2. OBJECT MODE   – standard header with CAD MODE ⚙ injected
#                       AFTER the Interaction Mode ▼ dropdown
#                       and BEFORE the View menu:
#                       [Object Mode ▼]  [CAD MODE ⚙]  [View]  [Select]  [Add]  [Object]
#    3. Other modes   – delegate to the saved original Blender draw
#
#  IMPORTANT – there is NO append() / prepend() call for the normal-mode
#  button anywhere in this file.  Using append() would place the button
#  *after* the draw function returns, i.e. back at the far right end,
#  defeating the purpose.  The patched draw owns the header completely.
# ════════════════════════════════════════════════════════════════════

_OVERLAY_PANEL = "VIEW3D_PT_overlay"
_GIZMO_PANEL   = "VIEW3D_PT_gizmo_display"

# Standard Object Mode menus, left to right.
_OBJECT_MODE_MENUS = (
    "VIEW3D_MT_view",
    "VIEW3D_MT_select_object",
    "VIEW3D_MT_add",
    "VIEW3D_MT_object",
)

# Known bl_idnames for the Interaction Mode dropdown across Blender versions.
_MODE_MENU_NAMES = (
    'VIEW3D_MT_object_mode',   # Blender 3.x
    'VIEW3D_MT_mode',          # Blender 4.x candidate
    'VIEW3D_MT_modes',         # Blender 5.x candidate
    'VIEW3D_MT_modes_pie',     # Ctrl+Tab pie (some 4.x builds)
)


# ── helpers ──────────────────────────────────────────────────────────

def _draw_right_side(layout, context):
    """
    Full right-hand side controls matching Blender's default header.
    Draws (left-to-right):
      Pivot Point | Transform Orientation | Snap | Proportional Editing
      | Viewport Shading | Overlays | Gizmo
    Each block is wrapped in try/except so API differences across
    Blender 3.x – 5.x never crash the header.
    """
    view = context.space_data
    tool_settings = context.scene.tool_settings if context.scene else None

    # ── Transform Pivot Point ────────────────────────────────────────
    try:
        if tool_settings:
            layout.prop(
                tool_settings,
                "transform_pivot_point",
                icon_only=True,
            )
    except Exception:
        pass

    # ── Transform Orientation ────────────────────────────────────────
    try:
        scene = context.scene
        if scene and scene.transform_orientation_slots:
            orient_slot = scene.transform_orientation_slots[0]
            layout.prop(orient_slot, "type", text="")
    except Exception:
        pass

    layout.separator(factor=0.5)

    # ── Snapping ─────────────────────────────────────────────────────
    try:
        if tool_settings:
            row = layout.row(align=True)
            row.prop(tool_settings, "use_snap", text="")
            sub = row.row(align=True)
            sub.active = getattr(tool_settings, "use_snap", False)
            sub.popover(panel="VIEW3D_PT_snapping", icon='NONE', text="")
    except Exception:
        pass

    # ── Proportional Editing ─────────────────────────────────────────
    try:
        if tool_settings:
            row = layout.row(align=True)
            use_prop = getattr(tool_settings, "use_proportional_edit", False)
            row.prop(tool_settings, "use_proportional_edit", icon_only=True)
            if use_prop:
                row.prop(
                    tool_settings,
                    "proportional_edit_falloff",
                    icon_only=True,
                    text="",
                )
    except Exception:
        pass

    layout.separator(factor=0.5)

    # ── Viewport Shading ─────────────────────────────────────────────
    try:
        if view:
            row = layout.row(align=True)
            row.prop(view.shading, "type", expand=True, icon_only=True)
    except Exception:
        pass

    # ── Overlays ─────────────────────────────────────────────────────
    try:
        layout.popover(panel=_OVERLAY_PANEL, text="", icon='OVERLAY')
    except Exception:
        pass

    # ── Gizmo ────────────────────────────────────────────────────────
    try:
        layout.popover(panel=_GIZMO_PANEL, text="", icon='GIZMO')
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
#  VIEWPOINT SELECTOR OPERATOR
#  Thin wrapper around view3d.view_axis that also records which view
#  was chosen in cad_settings.lock_view so the header buttons can
#  highlight the correct button reliably — no quaternion comparison needed.
# ════════════════════════════════════════════════════════════════════

class CAD_OT_SetViewpoint(bpy.types.Operator):
    """Switch to a canonical ortho view and record it for button highlighting."""
    bl_idname  = "cad.set_viewpoint"
    bl_label   = "Set Viewpoint"
    bl_options = {'INTERNAL'}

    view_type: bpy.props.StringProperty(default='FRONT')

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    def execute(self, context):
        # Apply the view via Blender's built-in operator.
        try:
            bpy.ops.view3d.view_axis(type=self.view_type, align_active=False)
        except Exception as e:
            self.report({'WARNING'}, f"view_axis failed: {e}")
            return {'CANCELLED'}

        # Record the selection so the header can highlight the right button.
        s = getattr(context.scene, 'cad_settings', None)
        if s is not None:
            try:
                s.lock_view = self.view_type
            except Exception:
                pass

        return {'FINISHED'}


def _draw_viewpoint_selector(layout, context):
    """
    Draws:  Viewpoint: [Top][Bottom][Front][Back][Left][Right]
    The button matching cad_settings.lock_view is highlighted (depress=True).
    Highlighting is driven by the property that cad.set_viewpoint writes on
    every click — no quaternion comparison, so it is always accurate.
    """
    _VIEWS = (
        ("Top",    "TOP"),
        ("Bottom", "BOTTOM"),
        ("Front",  "FRONT"),
        ("Back",   "BACK"),
        ("Left",   "LEFT"),
        ("Right",  "RIGHT"),
    )

    # Read which view was last selected from the scene property.
    current_view = None
    s = getattr(context.scene, 'cad_settings', None)
    if s is not None:
        current_view = s.lock_view   # e.g. 'FRONT', 'TOP', etc.

    layout.separator(factor=0.5)
    layout.label(text="Viewpoint:")

    # Outer aligned row keeps the buttons flush against each other.
    # depress=True gives the standard Blender blue active-button highlight.
    row = layout.row(align=True)
    for label, view_type in _VIEWS:
        is_active = (current_view == view_type)
        op = row.operator("cad.set_viewpoint", text=label, depress=is_active)
        op.view_type = view_type

    layout.separator(factor=0.5)


def _patched_header_draw(self, context):
    layout = self.layout
    s      = getattr(context.scene, 'cad_settings', None)

    # ── 1. SKETCH MODE header ────────────────────────────────────────
    if s and s.sketch_mode:
        layout.template_header()

        for menu_name in ("VIEW3D_MT_view", "VIEW3D_MT_select_object", "VIEW3D_MT_add"):
            if hasattr(bpy.types, menu_name):
                layout.menu(menu_name)

        layout.separator(factor=0.5)

        # Greyed-out while already in Sketch Mode (poll returns False).
        layout.operator(
            "cad.enter_sketch_mode",
            text="CAD MODE",
            icon='GREASEPENCIL',
        )

        layout.separator(factor=0.5)

        # (Snap select-tool buttons removed - now reads the toolbar's
        # own active tool instead of duplicating it; see
        # utils.math_utils.get_active_select_gesture)

        # ── Viewpoint selector ───────────────────────────────────────
        _draw_viewpoint_selector(layout, context)

        layout.separator_spacer()

        # ── Default Blender right-side controls ──────────────────────
        tool_settings = context.scene.tool_settings if context.scene else None

        # Transform Pivot Point
        try:
            if tool_settings:
                layout.prop(tool_settings, "transform_pivot_point", icon_only=True)
        except Exception:
            pass

        # Transform Orientation
        try:
            scene = context.scene
            if scene and scene.transform_orientation_slots:
                layout.prop(scene.transform_orientation_slots[0], "type", text="")
        except Exception:
            pass

        layout.separator(factor=0.5)

        # Snapping (Blender built-in)
        try:
            if tool_settings:
                row = layout.row(align=True)
                row.prop(tool_settings, "use_snap", text="")
                sub = row.row(align=True)
                sub.active = getattr(tool_settings, "use_snap", False)
                sub.popover(panel="VIEW3D_PT_snapping", icon='NONE', text="")
        except Exception:
            pass

        # Proportional Editing
        try:
            if tool_settings:
                row = layout.row(align=True)
                use_prop = getattr(tool_settings, "use_proportional_edit", False)
                row.prop(tool_settings, "use_proportional_edit", icon_only=True)
                if use_prop:
                    row.prop(tool_settings, "proportional_edit_falloff",
                             icon_only=True, text="")
        except Exception:
            pass

        layout.separator(factor=0.5)

        # ── CAD Snap popover ─────────────────────────────────────────
        layout.popover(panel="CAD_PT_SketchSnapPopover", text="", icon='SNAP_ON')

        # ── Polar Tracking: toggle + AutoCAD-style angle-preset
        # dropdown, drawn together as one unit (toggle button, then
        # the drop-down arrow button right beside it) so it reads the
        # same way AutoCAD's status-bar POLAR button + its angle-list
        # arrow do.
        row = layout.row(align=True)
        row.prop(s, "polar_tracking_enabled", text="", icon='DRIVER_ROTATIONAL_DIFFERENCE')
        sub = row.row(align=True)
        sub.active = s.polar_tracking_enabled
        sub.prop_menu_enum(s, "polar_angle_increment", text=s.polar_angle_increment + "°")

        # ── Viewport Shading ─────────────────────────────────────────
        view = context.space_data
        if view:
            row2 = layout.row(align=True)
            row2.prop(view.shading, "type", expand=True, icon_only=True)

        # ── Overlays ─────────────────────────────────────────────────
        try:
            layout.popover(panel=_OVERLAY_PANEL, text="", icon='OVERLAY')
        except Exception:
            pass

        # ── RETURN to Object Mode (keep at far right) ─────────────────
        layout.operator(
            "cad.exit_sketch_mode",
            text="RETURN",
            icon='LOOP_BACK',
        )

        return

    # ── 2. OBJECT MODE – CAD MODE ⚙ between dropdown and View menu ──
    #
    #   Result: [Object Mode ▼]  [CAD MODE ⚙]  [View]  [Select]  [Add]  [Object]
    #
    if context.mode == 'OBJECT':
        # Editor-type icon + workspace tabs ───────────────────────────
        layout.template_header()

        # Interaction Mode / mode selector dropdown ───────────────────
        # In Blender 4.x+ the mode selector is NOT included in
        # template_header(); it must be drawn explicitly.
        # We use operator_menu_enum so it works across 3.x – 5.x.
        obj = context.active_object
        try:
            mode_key = obj.mode if obj else 'OBJECT'
            act_mode_item = (
                bpy.types.Object.bl_rna.properties['mode']
                .enum_items[mode_key]
            )
            row = layout.row(align=True)
            row.enabled = obj is not None
            row.operator_menu_enum(
                "object.mode_set", "mode",
                text=act_mode_item.name,
                icon=act_mode_item.icon,
            )
        except Exception:
            pass

        # CAD MODE button (gear icon) immediately after the dropdown ──
        layout.operator(
            "cad.enter_sketch_mode",
            text="CAD MODE",
            icon='PREFERENCES',   # ⚙ gear
        )

        # Standard Object Mode menus ──────────────────────────────────
        for menu_name in _OBJECT_MODE_MENUS:
            if hasattr(bpy.types, menu_name):
                layout.menu(menu_name)

        # Right-side controls ─────────────────────────────────────────
        layout.separator_spacer()
        _draw_right_side(layout, context)
        return

    # ── 3. All other modes – original Blender header, untouched ─────
    if _st.orig_header_draw:
        _st.orig_header_draw(self, context)


# ════════════════════════════════════════════════════════════════════
#  MODE DROPDOWN INJECTION  (secondary entry point via Ctrl+Tab / pie)
# ════════════════════════════════════════════════════════════════════

def _sketch_mode_menu_entry(self, context):
    """
    Appended to every known Interaction Mode dropdown class.
    Adds CAD MODE below Sculpt Mode in the mode picker.
    """
    if not (context.area and context.area.type == 'VIEW_3D'):
        return
    s = getattr(context.scene, 'cad_settings', None)
    if s and s.sketch_mode:
        return
    if context.mode != 'OBJECT':
        return
    self.layout.separator()
    self.layout.operator(
        "cad.enter_sketch_mode",
        text="CAD MODE",
        icon='GREASEPENCIL',
    )


# ════════════════════════════════════════════════════════════════════
#  STALE-CALLBACK CLEANUP
#
#  Previous versions of this addon used append() to add a button called
#  _sketch_mode_header_btn to VIEW3D_HT_header.  After a module reload
#  the old function object cannot be removed by reference (Blender keeps
#  it in its internal list).  We purge it by name instead.
# ════════════════════════════════════════════════════════════════════

_STALE_NAMES = frozenset({
    '_sketch_mode_header_btn',   # name used in previous versions
})


def _purge_stale_header_callbacks():
    """
    Remove any append/prepend callbacks left by old installs that we can
    no longer reach by reference.  Matches by __name__ so it works even
    after module reload produces new function objects.
    """
    try:
        # _dyn_ui_initialize() returns the mutable list of draw callbacks
        # registered via .append() / .prepend() on this type.
        callbacks = bpy.types.VIEW3D_HT_header._dyn_ui_initialize()
        stale = [cb for cb in list(callbacks)
                 if getattr(cb, '__name__', '') in _STALE_NAMES]
        for cb in stale:
            try:
                bpy.types.VIEW3D_HT_header.remove(cb)
                print(f"[CAD] Removed stale header callback: {cb.__name__}")
            except Exception:
                pass
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
#  INSTALL / UNINSTALL
# ════════════════════════════════════════════════════════════════════

def install_header_patch():
    # ── 0. Register the viewpoint selector operator ───────────────────
    try:
        bpy.utils.unregister_class(CAD_OT_SetViewpoint)
    except Exception:
        pass
    bpy.utils.register_class(CAD_OT_SetViewpoint)

    # ── 1. Purge stale callbacks from old installs first ─────────────
    _purge_stale_header_callbacks()

    # ── 2. Replace VIEW3D_HT_header.draw with our patched version ────
    #       This is the ONLY mechanism used to draw the CAD MODE button
    #       in normal Object Mode.  No append() is used for this.
    if _st.orig_header_draw is None:
        _st.orig_header_draw = bpy.types.VIEW3D_HT_header.draw
        bpy.types.VIEW3D_HT_header.draw = _patched_header_draw

    # ── 3. Inject into the Interaction Mode dropdown (secondary) ─────
    for name in _MODE_MENU_NAMES:
        if hasattr(bpy.types, name):
            menu_type = getattr(bpy.types, name)
            try:
                menu_type.remove(_sketch_mode_menu_entry)
            except Exception:
                pass
            menu_type.append(_sketch_mode_menu_entry)
            print(f"[CAD] CAD MODE appended to mode menu: {name}")

    print("[CAD] Header patched.")
    print("[CAD] Button position: [Object Mode ▼]  [CAD MODE ⚙]  [View]  [Select]  [Add]  [Object]")


def uninstall_header_patch():
    # ── Unregister the viewpoint selector operator ────────────────────
    try:
        bpy.utils.unregister_class(CAD_OT_SetViewpoint)
    except Exception:
        pass

    # ── Restore original header draw ─────────────────────────────────
    if _st.orig_header_draw is not None:
        bpy.types.VIEW3D_HT_header.draw = _st.orig_header_draw
        _st.orig_header_draw = None

    # ── Remove from Interaction Mode dropdowns ────────────────────────
    for name in _MODE_MENU_NAMES:
        if hasattr(bpy.types, name):
            try:
                getattr(bpy.types, name).remove(_sketch_mode_menu_entry)
            except Exception:
                pass

    # ── Final sweep for any stale callbacks ──────────────────────────
    _purge_stale_header_callbacks()

    print("[CAD] Header restored.")
