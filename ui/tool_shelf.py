# ─────────────────────────────────────────────────────────────────────
#  ui/tool_shelf.py
#  Ortho lock / restore  +  NUMPAD_5 swallow operator
#  No WorkSpaceTool. No timers. No temp_override.
#  Uses a draw handler to apply the lock at the right moment.
# ─────────────────────────────────────────────────────────────────────

import bpy
import math as _math

# ── saved state ───────────────────────────────────────────────────────
_saved_view:     dict = {}   # area.as_pointer() -> saved dict
_installed_kmi:  list = []   # (km, kmi) to remove on unregister
_lock_pending:   bool = False  # True = apply lock on next draw
_lock_handler         = None   # the PRE_VIEW draw handler reference

# ── canonical ortho-view quaternions (w, x, y, z) ────────────────────
# These match Blender's Numpad view shortcuts exactly.
_S2 = _math.sqrt(0.5)
_CANONICAL_VIEWS = {
    'TOP':    ( _S2, -_S2,  0.0,  0.0),   # Numpad 7      –90° around X
    'BOTTOM': ( _S2,  _S2,  0.0,  0.0),   # Ctrl+Num 7   +90° around X
    'FRONT':  ( 1.0,  0.0,  0.0,  0.0),   # Numpad 1      identity
    'BACK':   ( 0.0,  0.0,  0.0,  1.0),   # Ctrl+Num 1   180° around Z
    'RIGHT':  ( _S2,  0.0,  0.0, -_S2),   # Numpad 3      –90° around Z
    'LEFT':   ( _S2,  0.0,  0.0,  _S2),   # Ctrl+Num 3   +90° around Z
}


def _find_closest_view(view_rotation) -> str:
    """Return the name of the canonical view closest to the current rotation.
    Works by maximising the quaternion dot-product (|q1·q2| == 1 → same rotation)."""
    import mathutils
    best_name = 'TOP'
    best_dot  = -1.0
    for name, wxyz in _CANONICAL_VIEWS.items():
        q   = mathutils.Quaternion(wxyz)
        dot = abs(view_rotation.dot(q))   # abs: q and –q are the same rotation
        if dot > best_dot:
            best_dot  = dot
            best_name = name
    return best_name


# ════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ════════════════════════════════════════════════════════════════════

def apply_ortho_now(context) -> None:
    """
    Snap to the nearest canonical ortho view immediately.
    Call from inside an operator's invoke() – bpy.ops is safe there.
    Falls back to schedule_ortho_lock() when region_data is unavailable.
    """
    area        = context.area
    region_data = context.region_data

    if area is None or region_data is None:
        print("[CAD] apply_ortho_now: region_data unavailable, scheduling fallback")
        schedule_ortho_lock(area)
        return

    # Determine the closest view BEFORE we change anything
    closest = _find_closest_view(region_data.view_rotation)
    print(f"[CAD] closest canonical view: {closest}")

    # Save the current view state
    key = area.as_pointer()
    if key not in _saved_view:
        _saved_view[key] = {
            "perspective":   region_data.view_perspective,
            "view_rotation": region_data.view_rotation.copy(),
            "view_distance": region_data.view_distance,
            "view_location": region_data.view_location.copy(),
        }
        print(f"[CAD] view state saved: {_saved_view[key]['perspective']}")

    # Primary: use Blender's own operator (handles internal Blender state correctly)
    try:
        bpy.ops.view3d.view_axis(type=closest, align_active=False)
    except Exception as e:
        print(f"[CAD] view_axis op failed ({e}) – direct quaternion fallback")
        import mathutils
        region_data.view_rotation = mathutils.Quaternion(_CANONICAL_VIEWS[closest])

    # Explicitly force ortho (view_axis may leave it as-is)
    region_data.view_perspective = 'ORTHO'

    # Record the snapped view in cad_settings so the header buttons
    # highlight correctly as soon as sketch mode opens.
    try:
        s = getattr(context.scene, 'cad_settings', None)
        if s is not None:
            s.lock_view = closest
    except Exception:
        pass

    if context.area is not None:
        context.area.tag_redraw()

    _install_ortho_keymap()
    print(f"[CAD] snapped to {closest} ortho (invoke path)")


def schedule_ortho_lock(area) -> None:
    """
    Called from EnterSketchMode.execute().
    Installs a PRE_VIEW draw handler that fires at the very next
    viewport redraw – at that point region_data is always valid.
    """
    global _lock_pending, _lock_handler

    _lock_pending = True

    # Remove any stale handler first
    _remove_lock_handler()

    _lock_handler = bpy.types.SpaceView3D.draw_handler_add(
        _lock_draw_cb, (), 'WINDOW', 'PRE_VIEW'
    )
    print("[CAD] ortho lock handler installed, waiting for next draw")

    # Force an immediate redraw so the handler fires quickly
    if area:
        area.tag_redraw()


def restore_view(context) -> None:
    """
    Restore the viewport to its pre-sketch state.
    Safe to call multiple times.
    """
    global _lock_pending

    _lock_pending = False
    _remove_lock_handler()
    _uninstall_ortho_keymap()

    area        = context.area
    region_data = context.region_data

    if area is None or region_data is None:
        # Try to find the saved area even without a valid context
        _saved_view.clear()
        return

    key   = area.as_pointer()
    saved = _saved_view.pop(key, None)

    if saved is None:
        print("[CAD] restore_view: nothing saved, skipping")
        return

    print(f"[CAD] restoring view to: {saved['perspective']}")
    region_data.view_perspective = saved["perspective"]
    region_data.view_rotation    = saved["view_rotation"]
    region_data.view_distance    = saved["view_distance"]
    region_data.view_location    = saved["view_location"]


# ════════════════════════════════════════════════════════════════════
#  DRAW HANDLER  –  fires at the next viewport PRE_VIEW
#  At this point context.region_data is ALWAYS valid.
# ════════════════════════════════════════════════════════════════════

def _lock_draw_cb() -> None:
    """
    PRE_VIEW draw callback.
    Runs inside the viewport draw loop so region_data is guaranteed
    to be non-None.  Removes itself after running once.
    """
    global _lock_pending

    if not _lock_pending:
        _remove_lock_handler()
        return

    ctx = bpy.context

    # Sanity checks
    if ctx.area is None or ctx.area.type != 'VIEW_3D':
        return
    if ctx.region_data is None:
        return

    # Only act when sketch mode is actually active
    s = getattr(ctx.scene, 'cad_settings', None)
    if s is None or not s.sketch_mode:
        _remove_lock_handler()
        _lock_pending = False
        return

    _apply_lock(ctx)

    # Remove the handler – we only need to run once
    _lock_pending = False
    _remove_lock_handler()


def _apply_lock(ctx) -> None:
    """
    Snap to the nearest canonical ortho view directly (no bpy.ops).
    Safe to call from a draw callback where operators are forbidden.
    ctx.region_data is guaranteed non-None when this is called.
    """
    area        = ctx.area
    region_data = ctx.region_data
    key         = area.as_pointer()

    # Save state once
    if key not in _saved_view:
        _saved_view[key] = {
            "perspective":   region_data.view_perspective,
            "view_rotation": region_data.view_rotation.copy(),
            "view_distance": region_data.view_distance,
            "view_location": region_data.view_location.copy(),
        }
        print(f"[CAD] view state saved: {_saved_view[key]['perspective']}")

    # Snap to nearest canonical view
    import mathutils
    closest = _find_closest_view(region_data.view_rotation)
    region_data.view_rotation    = mathutils.Quaternion(_CANONICAL_VIEWS[closest])
    region_data.view_perspective = 'ORTHO'
    print(f"[CAD] snapped to {closest} ortho (draw-callback path)")

    # Record the snapped view so the header buttons highlight correctly.
    try:
        s = getattr(ctx.scene, 'cad_settings', None)
        if s is not None:
            s.lock_view = closest
    except Exception:
        pass

    if context.area is not None:
        context.area.tag_redraw()

    _install_ortho_keymap()


def _remove_lock_handler() -> None:
    global _lock_handler
    if _lock_handler is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(
                _lock_handler, 'WINDOW'
            )
        except Exception:
            pass
        _lock_handler = None


# ════════════════════════════════════════════════════════════════════
#  ORTHO KEYMAP  –  swallow NUMPAD_5
# ════════════════════════════════════════════════════════════════════

def _install_ortho_keymap() -> None:
    if _installed_kmi:
        return
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc is None:
        return
    km = kc.keymaps.get("3D View")
    if km is None:
        km = kc.keymaps.new(name="3D View", space_type='VIEW_3D')
    kmi = km.keymap_items.new(
        "cad.ortho_lock_noop", type='NUMPAD_5', value='PRESS'
    )
    _installed_kmi.append((km, kmi))
    print("[CAD] NUMPAD_5 swallow installed")


def _uninstall_ortho_keymap() -> None:
    for km, kmi in _installed_kmi:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _installed_kmi.clear()


# ════════════════════════════════════════════════════════════════════
#  NO-OP OPERATOR  –  swallows NUMPAD_5
# ════════════════════════════════════════════════════════════════════

class CAD_OT_OrthoLockNoop(bpy.types.Operator):
    """Consumes NUMPAD_5 while Sketch Mode is active."""
    bl_idname  = "cad.ortho_lock_noop"
    bl_label   = "Ortho Lock"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        s = getattr(context.scene, 'cad_settings', None)
        if s and s.sketch_mode:
            rd = context.region_data
            if rd:
                rd.view_perspective = 'ORTHO'
            return {'FINISHED'}             # swallowed
        bpy.ops.view3d.view_persportho()
        return {'FINISHED'}


# ════════════════════════════════════════════════════════════════════
#  REGISTER / UNREGISTER
# ════════════════════════════════════════════════════════════════════

_TOOL_CLASSES = (CAD_OT_OrthoLockNoop,)


def register():
    for cls in _TOOL_CLASSES:
        bpy.utils.register_class(cls)
    print("[CAD] tool_shelf registered")


def unregister():
    global _lock_pending
    _lock_pending = False
    _remove_lock_handler()
    _uninstall_ortho_keymap()
    for cls in reversed(_TOOL_CLASSES):
        bpy.utils.unregister_class(cls)