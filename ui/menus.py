# ─────────────────────────────────────────────────────────────────────
#  ui/menus.py
#  Mode-pie injection, dropdown menus, snap popover, context menu
# ─────────────────────────────────────────────────────────────────────

import bpy
from .header import _draw_viewpoint_selector


# ════════════════════════════════════════════════════════════════════
#  SKETCH MODE DROPDOWN  (shown in the header mode button)
# ════════════════════════════════════════════════════════════════════

class CAD_MT_SketchModeDropdown(bpy.types.Menu):
    """Mode selector dropdown shown in Sketch Mode header."""
    bl_label  = "Sketch Mode"
    bl_idname = "CAD_MT_SketchModeDropdown"

    def draw(self, context):
        lo = self.layout
        lo.operator(
            "cad.exit_sketch_mode",
            text="Object Mode",
            icon='OBJECT_DATAMODE',
        )
        lo.separator()
        lo.label(text="✓  Sketch Mode", icon='GREASEPENCIL')


# ════════════════════════════════════════════════════════════════════
#  SNAP POPOVER  (header snap button)
# ════════════════════════════════════════════════════════════════════

class CAD_PT_SketchSnapPopover(bpy.types.Panel):
    """Snap settings popover for Sketch Mode header."""
    bl_label       = "Sketch Snap"
    bl_idname      = "CAD_PT_SketchSnapPopover"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'HEADER'

    def draw(self, context):
        lo = self.layout
        s  = context.scene.cad_settings
        col = lo.column(align=True)
        col.prop(s, "snap_grid",     icon='SNAP_GRID')
        sub = col.column()
        sub.enabled = s.snap_grid
        sub.prop(s, "snap_increment", text="  Grid Size")
        col.separator()
        col.prop(s, "snap_vertex",   icon='SNAP_VERTEX')
        col.prop(s, "snap_endpoint", icon='SNAP_MIDPOINT')

        lo.separator()
        box = lo.box()
        box.label(text="Polar Tracking", icon='DRIVER_ROTATIONAL_DIFFERENCE')
        col = box.column(align=True)
        col.prop(s, "polar_tracking_enabled", text="Enable")
        sub = col.column(align=True)
        sub.enabled = s.polar_tracking_enabled
        sub.prop_menu_enum(s, "polar_angle_increment", text="Angle Increment: " + s.polar_angle_increment + "°")
        sub.prop(s, "polar_angle_tolerance", text="Tolerance")

        lo.separator()
        box = lo.box()
        box.label(text="Cursor", icon='RESTRICT_SELECT_OFF')
        col = box.column(align=True)
        col.prop(s, "cursor_crosshair_pct", text="Crosshair Size")
        col.prop(s, "cursor_pickbox_px",  text="Pick Box")
        col.prop(s, "cursor_aperture_px", text="Aperture")
        col.prop(s, "cursor_show_aperture")
        col.separator()
        col.prop(s, "cursor_show_grips")
        sub = col.column()
        sub.enabled = s.cursor_show_grips
        sub.prop(s, "cursor_grip_px", text="Grip Size")
        sub.prop(s, "cursor_grip_proximity_only", text="Only Near Cursor")
        sub2 = sub.column()
        sub2.enabled = s.cursor_grip_proximity_only
        sub2.prop(s, "cursor_grip_reveal_px", text="Reveal Radius")

        lo.separator()
        lo.operator(
            "cad.convert_to_mesh",
            text="Convert to Mesh",
            icon='MESH_DATA',
        )


# ════════════════════════════════════════════════════════════════════
#  RIGHT-CLICK CONTEXT MENU  –  appended via register()
# ════════════════════════════════════════════════════════════════════

class CAD_MT_Context(bpy.types.Menu):
    bl_label  = "Parametric CAD"
    bl_idname = "CAD_MT_context"

    def draw(self, context):
        lo = self.layout
        lo.label(text="Sketch Mode", icon='GREASEPENCIL')
        lo.operator(
            "cad.enter_sketch_mode",
            text="Enter Sketch Mode",
            icon='GREASEPENCIL',
        )
        lo.separator()
        lo.label(text="Draw", icon='GREASEPENCIL')
        lo.operator("cad.draw_line",   text="Line",      icon='IPO_LINEAR')
        lo.operator("cad.draw_circle", text="Circle",    icon='MESH_CIRCLE')
        lo.operator("cad.draw_ellipse",text="Ellipse",   icon='MESH_UVSPHERE')
        lo.operator("cad.draw_point",  text="Point",     icon='DOT')
        lo.operator("cad.draw_arc",    text="Arc",       icon='SPHERECURVE')
        lo.operator("cad.draw_spline", text="Spline",    icon='CURVE_BEZCURVE')
        lo.operator("cad.draw_rect",   text="Rectangle", icon='MESH_PLANE')
        lo.separator()
        lo.operator(
            "cad.convert_to_mesh",
            text="Convert to Mesh",
            icon='MESH_DATA',
        )


def menu_append(self, context):
    """Appended to VIEW3D_MT_object_context_menu."""
    self.layout.separator()
    self.layout.menu("CAD_MT_context", icon='TOOL_SETTINGS')


# ════════════════════════════════════════════════════════════════════
#  TOOL-SHELF (N-PANEL) MODE PANEL
#  Placed at the very top of the "View" sidebar tab (bl_order = 0)
#  so it sits in front of the native View properties.
#  • Outside sketch mode → large "CAD MODE" button (enters sketch).
#  • Inside  sketch mode → "← Object Mode" button (exits sketch).
# ════════════════════════════════════════════════════════════════════

class CAD_PT_ModePanel(bpy.types.Panel):
    """CAD Mode toggle + Lock View in the N-panel View tab."""
    bl_label       = "CAD"
    bl_idname      = "CAD_PT_ModePanel"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "View"
    bl_order       = 0

    def draw(self, context):
        layout = self.layout
        s = getattr(context.scene, 'cad_settings', None)
        in_sketch = s.sketch_mode if s else False
        locked    = s.view_locked if s else False

        # ── CAD MODE button ──────────────────────────────────────────
        if in_sketch:
            col = layout.column(align=True)
            col.alert = True
            col.operator(
                "cad.exit_sketch_mode",
                text="<- Object Mode",
                icon='OBJECT_DATAMODE',
            )
        else:
            col = layout.column(align=True)
            col.scale_y = 1.4
            col.operator(
                "cad.enter_sketch_mode",
                text="CAD MODE",
                icon='GREASEPENCIL',
            )

        layout.separator()

        # ── LOCK VIEW section ────────────────────────────────────────
        box = layout.box()
        col = box.column(align=True)

        col.label(
            text="Lock View",
            icon='LOCKED' if locked else 'UNLOCKED',
        )

        col.separator(factor=0.4)
        row = col.row(align=True)
        row.enabled = not locked
        _draw_viewpoint_selector(row, context)

        col.separator(factor=0.6)

        if locked:
            col.operator(
                "cad.unlock_view",
                text="Unlock View",
                icon='UNLOCKED',
            )
            col.separator(factor=0.4)
            status = col.row()
            status.alert = True
            status.label(text="Locked: " + s.lock_view, icon='INFO')
        else:
            col.operator(
                "cad.lock_view",
                text="Lock View",
                icon='LOCKED',
            )