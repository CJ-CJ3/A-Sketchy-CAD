# ============================================================
#  Parametric CAD Tools for Blender  –  v0.92  SKETCH MODE
#  INSTALL:  Edit ▸ Preferences ▸ Add-ons ▸ Install (folder)
# ============================================================

bl_info = {
    "name":        "Parametric CAD Tools v0.92",
    "author":      "CAD Addon",
    "version":     (0, 9, 2),
    "blender":     (3, 3, 0),
    "location":    "View3D › Header (beside Object Mode button) → Sketch Mode",
    "description": "Dedicated Sketch Mode – Line, Circle, Rect, Move, Slice tools",
    "warning":     "Z = 0 world plane  |  Object Mode base",
    "category":    "Add Mesh",
}

import bpy


def _safe_register_class(cls):
    try:
        bpy.utils.register_class(cls)
    except RuntimeError as e:
        if "already registered" in str(e):
            try:
                bpy.utils.unregister_class(cls)
            except Exception:
                pass
            bpy.utils.register_class(cls)
        else:
            raise


def _safe_unregister_class(cls):
    try:
        bpy.utils.unregister_class(cls)
    except Exception:
        pass

# ── sub-package imports (order matters) ──────────────────────────────
from . import state
from . import properties

from .utils import math_utils
from .utils import cad_objects

from .gfx import primitives
from .gfx import markers
from .gfx import dims
from .gfx import cursor
from .gfx import toolbar

from .ui import tool_shelf      # ortho lock + no-op operator
from .ui import tpanel_filter   # T-panel tool filter for CAD mode
from .ui import cad_tpanel_tools  # T-panel buttons: Line / Circle / Rect
from .ui import cad_tools_panel  # scrollable "CAD Tools" N-panel sidebar tab
from .ui import header          # header patch (imports tool_shelf)
from .ui import menus
from .ui import measurement_panel  # "CAD Dimension" N-panel sidebar tab

from .operators import sketch_mode
from .operators import lock_view
from .operators import tool_line
from .operators import tool_circle
from .operators import tool_ellipse
from .operators import tool_point
from .operators import tool_arc
from .operators import tool_spline
from .operators import tool_fillet
from .operators import tool_chamfer
from .operators import tool_extend
from .operators import tool_rect
from .operators import tool_move
from .operators import tool_scale
from .operators import tool_array
from .operators import tool_stretch
from .operators import tool_rotate
from .operators import tool_copy
from .operators import tool_mirror
from .operators import tool_trim
from .operators import tool_offset
from .operators import tool_measure
from .operators import tool_slice
from .operators import convert
from .operators import tool_edit

# ── class list ───────────────────────────────────────────────────────
from .properties            import CADSettings
from .operators.sketch_mode import (
    CAD_OT_EnterSketchMode,
    CAD_OT_ExitSketchMode,
    CAD_OT_SketchModeManager,
)
from .operators.lock_view import (
    CAD_OT_LockView,
    CAD_OT_UnlockView,
    CAD_OT_LockViewNoop,
)
from .operators.tool_line   import CAD_OT_DrawLine
from .operators.tool_circle import CAD_OT_DrawCircle
from .operators.tool_ellipse import CAD_OT_DrawEllipse
from .operators.tool_point import CAD_OT_DrawPoint
from .operators.tool_arc import CAD_OT_DrawArc
from .operators.tool_spline import CAD_OT_DrawSpline
from .operators.tool_fillet import CAD_OT_DrawFillet
from .operators.tool_chamfer import CAD_OT_DrawChamfer
from .operators.tool_extend import CAD_OT_DrawExtend
from .operators.tool_rect   import CAD_OT_DrawRect
from .operators.tool_move   import CAD_OT_SketchMove
from .operators.tool_scale  import CAD_OT_SketchScale
from .operators.tool_array  import CAD_OT_SketchArray
from .operators.tool_stretch import CAD_OT_SketchStretch
from .operators.tool_rotate import CAD_OT_SketchRotate
from .operators.tool_copy   import CAD_OT_SketchCopy
from .operators.tool_mirror import CAD_OT_SketchMirror
from .operators.tool_trim   import CAD_OT_SketchTrim
from .operators.tool_offset import CAD_OT_DrawOffset
from .operators.tool_measure import CAD_OT_Measure
from .operators.tool_edit_move import CAD_OT_EditMove
from .operators.tool_slice  import CAD_OT_SketchSlice
from .operators.convert     import CAD_OT_ConvertToMesh
from .operators.tool_edit   import CAD_OT_EditElement
from .ui.menus              import (
    CAD_MT_SketchModeDropdown,
    CAD_PT_SketchSnapPopover,
    CAD_MT_Context,
    CAD_PT_ModePanel,
)
# CAD_OT_OrthoLockNoop is registered inside tool_shelf.register()

_CLASSES = (
    CADSettings,
    CAD_OT_EnterSketchMode,
    CAD_OT_ExitSketchMode,
    CAD_OT_SketchModeManager,
    CAD_OT_LockView,
    CAD_OT_UnlockView,
    CAD_OT_LockViewNoop,
    CAD_OT_DrawLine,
    CAD_OT_DrawCircle,
    CAD_OT_DrawEllipse,
    CAD_OT_DrawPoint,
    CAD_OT_DrawArc,
    CAD_OT_DrawSpline,
    CAD_OT_DrawFillet,
    CAD_OT_DrawChamfer,
    CAD_OT_DrawExtend,
    CAD_OT_DrawRect,
    CAD_OT_SketchMove,
    CAD_OT_SketchScale,
    CAD_OT_SketchArray,
    CAD_OT_SketchStretch,
    CAD_OT_SketchRotate,
    CAD_OT_SketchCopy,
    CAD_OT_SketchMirror,
    CAD_OT_SketchTrim,
    CAD_OT_DrawOffset,
    CAD_OT_Measure,
    CAD_OT_EditMove,
    CAD_OT_SketchSlice,
    CAD_OT_ConvertToMesh,
    CAD_OT_EditElement,
    CAD_MT_SketchModeDropdown,
    CAD_PT_SketchSnapPopover,
    CAD_MT_Context,
    CAD_PT_ModePanel,
)


def register():
    # Make registration safe on re-enable / reload.
    try:
        unregister()
    except Exception:
        pass

    for cls in _CLASSES:
        _safe_register_class(cls)

    if hasattr(bpy.types.Scene, 'cad_settings'):
        del bpy.types.Scene.cad_settings
    bpy.types.Scene.cad_settings = bpy.props.PointerProperty(type=CADSettings)

    # ortho-lock no-op operator
    tool_shelf.register()

    # T-panel buttons for Line / Circle / Rect
    cad_tpanel_tools.register()

    # scrollable "CAD Tools" N-panel sidebar tab (wheel/trackpad scroll
    # works here natively, unlike the Toolbar region above)
    cad_tools_panel.register()

    # "CAD Dimension" N-panel sidebar tab
    measurement_panel.register()

    # Undo/Redo resync handlers (see operators/sketch_mode.py's
    # "UNDO/REDO RESYNC" section) - keeps the addon's own Python-side
    # snap cache / selection state from drifting out of sync with the
    # scene every time the user presses Ctrl+Z/Ctrl+Shift+Z.
    sketch_mode.register()

    # header monkey-patch + Sketch Mode button
    header.install_header_patch()

    # right-click context menu
    try:
        bpy.types.VIEW3D_MT_object_context_menu.remove(menus.menu_append)
    except Exception:
        pass
    bpy.types.VIEW3D_MT_object_context_menu.append(menus.menu_append)

    print("[CAD v0.92] Registered.")
    print("           Sketch Mode: Interaction Mode dropdown, below Sculpt Mode")
    print("           Also:   right-click › Parametric CAD")


def unregister():
    from . import state as _state

    if _state.sketch_manager_ref is not None:
        try:
            _state.sketch_manager_ref._do_cancel(bpy.context)
        except Exception:
            pass

    header.uninstall_header_patch()

    # Safety: if the addon is disabled while CAD mode is still active,
    # make sure the full T-panel tool list is restored before we tear down.
    tpanel_filter.restore_all_tools()

    # Remove the CAD T-panel tool buttons
    cad_tpanel_tools.unregister()

    cad_tools_panel.unregister()

    measurement_panel.unregister()

    sketch_mode.unregister()

    # release any active view lock
    lock_view.unregister()

    try:
        bpy.types.VIEW3D_MT_object_context_menu.remove(menus.menu_append)
    except Exception:
        pass

    tool_shelf.unregister()

    if hasattr(bpy.types.Scene, 'cad_settings'):
        del bpy.types.Scene.cad_settings

    for cls in reversed(_CLASSES):
        _safe_unregister_class(cls)

    print("[CAD v0.92] Unregistered.")
