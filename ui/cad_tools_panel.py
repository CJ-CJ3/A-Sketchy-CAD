# ─────────────────────────────────────────────────────────────────────
#  ui/cad_tools_panel.py
#
#  Scrollable CAD tool list, shown in the 3D viewport's N-panel sidebar
#  (bl_region_type='UI') under a "CAD Tools" tab.
#
#  WHY THIS EXISTS:
#  The native Toolbar (T-panel, bl_region_type='TOOLS') does NOT support
#  mouse-wheel / trackpad scrolling in Blender - when it overflows, the
#  only native way to reach more tools is to drag its right edge wider
#  so icons wrap into extra columns. That's core Blender behaviour and
#  isn't something addon Python can change.
#
#  The 'UI' region (N-panel sidebar) DOES support wheel/trackpad
#  scrolling natively (same View2D scrolling as every other sidebar
#  tab), so this panel gives users a scrollable way to reach every CAD
#  tool while Sketch Mode is active, in addition to the existing
#  Toolbar icons (which are left in place, unchanged).
# ─────────────────────────────────────────────────────────────────────

import bpy

# (button label, operator idname, icon)
# icons are stock Blender icons chosen to approximate each tool visually;
# they are NOT the same as the custom-generated Toolbar icons, since
# those custom .dat files only work inside the toolsystem, not on
# regular UI buttons.
_TOOL_BUTTONS = [
    ("Line",      "cad.draw_line",    'IPO_LINEAR'),
    ("Circle",    "cad.draw_circle",  'MESH_CIRCLE'),
    ("Ellipse",   "cad.draw_ellipse", 'META_ELLIPSOID'),
    ("Point",     "cad.draw_point",   'LAYER_ACTIVE'),
    ("Arc",       "cad.draw_arc",     'SPHERECURVE'),
    ("Spline",    "cad.draw_spline",  'CURVE_BEZCURVE'),
    ("Rectangle", "cad.draw_rect",    'MESH_PLANE'),
    (None, None, None),  # separator
    ("Fillet",    "cad.draw_fillet",  'MOD_BEVEL'),
    ("Chamfer",   "cad.draw_chamfer", 'MOD_BEVEL'),
    ("Extend",    "cad.draw_extend",  'FULLSCREEN_EXIT'),
    ("Trim",      "cad.sketch_trim",  'CUT'),
    ("Offset",    "cad.draw_offset",  'MOD_OFFSET'),
    (None, None, None),  # separator
    ("Move",      "cad.sketch_move",    'TRANSFORM_ORIGINS'),
    ("Scale",     "cad.sketch_scale",   'FULLSCREEN_ENTER'),
    ("Rotate",    "cad.sketch_rotate",  'ORIENTATION_GIMBAL'),
    ("Copy",      "cad.sketch_copy",    'DUPLICATE'),
    ("Mirror",    "cad.sketch_mirror",  'MOD_MIRROR'),
    ("Array",     "cad.sketch_array",   'MOD_ARRAY'),
    ("Stretch",   "cad.sketch_stretch", 'CON_STRETCHTO'),
    (None, None, None),  # separator
    ("Measure",   "cad.measure",      'DRIVER_DISTANCE'),
]


class CAD_PT_tools_panel(bpy.types.Panel):
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "CAD Tools"
    bl_label       = "CAD Tools"

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, 'cad_settings', None)
        return bool(s and s.sketch_mode)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Scrolls with wheel / trackpad", icon='INFO')
        col = layout.column(align=True)
        for label, idname, icon in _TOOL_BUTTONS:
            if idname is None:
                col.separator()
                continue
            col.operator(idname, text=label, icon=icon)


_CLASSES = (CAD_PT_tools_panel,)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    print("[CAD] cad_tools_panel registered")


def unregister() -> None:
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    print("[CAD] cad_tools_panel unregistered")
