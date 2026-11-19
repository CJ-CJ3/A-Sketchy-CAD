# ─────────────────────────────────────────────────────────────────────
#  operators/lock_view.py
#  CAD_OT_LockView  – snap to a canonical ortho view and hold it there
#  CAD_OT_UnlockView – release the lock and restore previous state
# ─────────────────────────────────────────────────────────────────────

import math
import bpy
import mathutils

# ── canonical ortho-view quaternions (w, x, y, z) ────────────────────
_S2 = math.sqrt(0.5)
_CANONICAL_QUATS = {
    'TOP':    mathutils.Quaternion(( _S2, -_S2,  0.0,  0.0)),
    'BOTTOM': mathutils.Quaternion(( _S2,  _S2,  0.0,  0.0)),
    'FRONT':  mathutils.Quaternion(( 1.0,  0.0,  0.0,  0.0)),
    'BACK':   mathutils.Quaternion(( 0.0,  0.0,  0.0,  1.0)),
    'RIGHT':  mathutils.Quaternion(( _S2,  0.0,  0.0, -_S2)),
    'LEFT':   mathutils.Quaternion(( _S2,  0.0,  0.0,  _S2)),
}

# view_axis type string for bpy.ops.view3d.view_axis
_VIEW_AXIS_TYPE = {
    'TOP':    'TOP',
    'BOTTOM': 'BOTTOM',
    'FRONT':  'FRONT',
    'BACK':   'BACK',
    'RIGHT':  'RIGHT',
    'LEFT':   'LEFT',
}

# ── saved state before locking ───────────────────────────────────────
_saved: dict = {}   # area.as_pointer() -> saved dict
_lock_kmi:   list = []   # (km, kmi) tuples to remove on unlock


def _install_lock_keymap():
    """Block NUMPAD_5 so the user can't accidentally toggle perspective."""
    if _lock_kmi:
        return
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc is None:
        return
    km = kc.keymaps.get("3D View") or kc.keymaps.new(
        name="3D View", space_type='VIEW_3D'
    )
    kmi = km.keymap_items.new(
        "cad.lock_view_noop", type='NUMPAD_5', value='PRESS'
    )
    _lock_kmi.append((km, kmi))


def _uninstall_lock_keymap():
    for km, kmi in _lock_kmi:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _lock_kmi.clear()


def _snap_viewport(context, view_name: str) -> bool:
    """
    Snap the current 3-D viewport to *view_name* ortho.
    Returns True on success, False if region_data is unavailable.
    """
    region_data = context.region_data
    if region_data is None:
        return False

    try:
        bpy.ops.view3d.view_axis(type=_VIEW_AXIS_TYPE[view_name], align_active=False)
    except Exception:
        region_data.view_rotation = _CANONICAL_QUATS[view_name].copy()

    region_data.view_perspective = 'ORTHO'
    return True


# ════════════════════════════════════════════════════════════════════
#  MAIN LOCK OPERATOR
# ════════════════════════════════════════════════════════════════════

class CAD_OT_LockView(bpy.types.Operator):
    """Snap the viewport to a canonical ortho view and lock it there."""
    bl_idname      = "cad.lock_view"
    bl_label       = "Lock View"
    bl_description = "Snap and lock the viewport to the selected canonical ortho view"
    bl_options     = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            context.area is not None
            and context.area.type == 'VIEW_3D'
            and context.region_data is not None
        )

    def execute(self, context):
        s = getattr(context.scene, 'cad_settings', None)
        if s is None:
            self.report({'ERROR'}, "CAD settings not found")
            return {'CANCELLED'}

        area = context.area
        region_data = context.region_data
        view_name = s.lock_view

        # ── Save previous state (once per area) ─────────────────────
        key = area.as_pointer()
        if key not in _saved:
            _saved[key] = {
                "perspective":   region_data.view_perspective,
                "view_rotation": region_data.view_rotation.copy(),
                "view_distance": region_data.view_distance,
                "view_location": region_data.view_location.copy(),
            }

        # ── Snap to the requested view ───────────────────────────────
        if not _snap_viewport(context, view_name):
            self.report({'WARNING'}, "Could not snap view – try again")
            return {'CANCELLED'}

        # ── Engage lock ──────────────────────────────────────────────
        s.view_locked = True
        _install_lock_keymap()

        if context.area is not None:
            context.area.tag_redraw()

        self.report({'INFO'}, f"View locked to {view_name}")
        return {'FINISHED'}


# ════════════════════════════════════════════════════════════════════
#  UNLOCK OPERATOR
# ════════════════════════════════════════════════════════════════════

class CAD_OT_UnlockView(bpy.types.Operator):
    """Release the view lock and restore the previous viewport state."""
    bl_idname      = "cad.unlock_view"
    bl_label       = "Unlock View"
    bl_description = "Release the view lock and restore the previous viewport"
    bl_options     = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, 'cad_settings', None)
        return s is not None and s.view_locked

    def execute(self, context):
        s = getattr(context.scene, 'cad_settings', None)
        if s is None:
            return {'CANCELLED'}

        _uninstall_lock_keymap()
        s.view_locked = False

        area = context.area
        region_data = context.region_data
        if area and region_data:
            key = area.as_pointer()
            saved = _saved.pop(key, None)
            if saved:
                region_data.view_perspective = saved["perspective"]
                region_data.view_rotation    = saved["view_rotation"]
                region_data.view_distance    = saved["view_distance"]
                region_data.view_location    = saved["view_location"]
        else:
            _saved.clear()

        self.report({'INFO'}, "View unlocked")
        return {'FINISHED'}


# ════════════════════════════════════════════════════════════════════
#  NUMPAD-5 NO-OP (keeps the lock from being broken by Numpad 5)
# ════════════════════════════════════════════════════════════════════

class CAD_OT_LockViewNoop(bpy.types.Operator):
    """Swallows Numpad 5 while a view lock is active."""
    bl_idname  = "cad.lock_view_noop"
    bl_label   = "Lock View – No-Op"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        s = getattr(context.scene, 'cad_settings', None)
        if s and s.view_locked:
            rd = context.region_data
            if rd:
                rd.view_perspective = 'ORTHO'
            return {'FINISHED'}
        # Not locked – pass through to Blender's normal Numpad 5 handler
        bpy.ops.view3d.view_persportho()
        return {'FINISHED'}


# ════════════════════════════════════════════════════════════════════
#  REGISTER / UNREGISTER
# ════════════════════════════════════════════════════════════════════

CLASSES = (
    CAD_OT_LockView,
    CAD_OT_UnlockView,
    CAD_OT_LockViewNoop,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    _uninstall_lock_keymap()
    _saved.clear()
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
