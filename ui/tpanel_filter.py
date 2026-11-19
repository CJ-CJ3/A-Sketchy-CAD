# ─────────────────────────────────────────────────────────────────────
#  ui/tpanel_filter.py
#
#  While CAD Sketch Mode is active, hide every T-panel tool in
#  VIEW_3D / OBJECT mode except Select Box.
#
#  Uses the same _tools-list surgery technique as remove_annotate_tool.py.
#  Public API:
#      hide_non_cad_tools()  – call from CAD_OT_SketchModeManager.invoke()
#      restore_all_tools()   – call from _do_cancel() and addon unregister()
# ─────────────────────────────────────────────────────────────────────

from bpy.utils.toolsystem import ToolDef

# ── keep-set: match by label OR by idname for maximum compatibility ───
_KEEP_LABELS  = frozenset({"Select Box"})
_KEEP_IDNAMES = frozenset({"builtin.select_box"})

# Modes to filter (None = shared/all-modes tools, 'OBJECT' = object-mode only)
_TARGET_MODES = (None, 'OBJECT')

# Storage: mode → list of (original_index, item) in ascending index order
_removed_per_mode: dict = {}


# ── internal helpers ──────────────────────────────────────────────────

def _get_tool_list(mode):
    """Return the mutable tools list for *mode*, or None on failure."""
    try:
        from bl_ui.space_toolsystem_common import ToolSelectPanelHelper
        cls = ToolSelectPanelHelper._tool_class_from_space_type("VIEW_3D")
        if mode not in cls._tools:
            return None
        return cls._tools[mode]
    except Exception as exc:
        print(f"[CAD tpanel_filter] _get_tool_list({mode!r}) error: {exc}")
        return None


def _identifiers_of(item) -> set:
    """
    Recursively collect every label and idname found inside *item*.

    Handles:
      • individual ToolDef
      • flat tuple of ToolDefs  (e.g. Move/Rotate/Scale flyout)
      • nested tuples           (rare, but present in some Blender builds)
      • None separators         (returns empty set)
    """
    found = set()
    if isinstance(item, ToolDef):
        if item.label:
            found.add(item.label)
        try:
            if item.idname:
                found.add(item.idname)
        except AttributeError:
            pass
    elif isinstance(item, (tuple, list)):
        for sub in item:
            found.update(_identifiers_of(sub))
    return found


def _should_keep(item) -> bool:
    """True if the item contains any tool we want to preserve."""
    ids = _identifiers_of(item)
    return bool(ids & _KEEP_LABELS) or bool(ids & _KEEP_IDNAMES)


def _dump_tools(mode, tools, tag=""):
    """Debug helper – prints the label/idname of every tool in the list."""
    print(f"[CAD tpanel_filter] {tag} mode={mode!r}  ({len(tools)} items):")
    for i, item in enumerate(tools):
        ids = _identifiers_of(item)
        kind = "tuple" if isinstance(item, tuple) else \
               "ToolDef" if isinstance(item, ToolDef) else \
               "sep" if item is None else type(item).__name__
        print(f"  [{i:2d}] {kind:8s}  ids={sorted(ids)}")


# ── public API ────────────────────────────────────────────────────────

def hide_non_cad_tools() -> None:
    """
    Remove every tool except Move / Rotate / Select Box from the
    OBJECT-mode T-panel.  Stores removed items for exact restoration.
    Safe to call multiple times (second call is a no-op).
    """
    if _removed_per_mode:
        print("[CAD tpanel_filter] already active – skipping hide")
        return

    for mode in _TARGET_MODES:
        tools = _get_tool_list(mode)
        if tools is None:
            continue

        _dump_tools(mode, tools, tag="BEFORE filter")

        # Collect (original_index, item) for items to remove.
        # Separators (None) are removed too; they are restored in place.
        to_remove = [
            (i, item)
            for i, item in enumerate(tools)
            if not _should_keep(item)
        ]

        if not to_remove:
            print(f"[CAD tpanel_filter] mode={mode!r}: nothing to remove")
            continue

        # Pop in reverse-index order to keep earlier indices valid.
        for i, _item in reversed(to_remove):
            tools.pop(i)

        _removed_per_mode[mode] = to_remove   # ascending order for restore

        _dump_tools(mode, tools, tag="AFTER  filter")
        print(
            f"[CAD tpanel_filter] mode={mode!r}: "
            f"removed {len(to_remove)}, kept {len(tools)}"
        )


def restore_all_tools() -> None:
    """
    Re-insert every removed item at its original index.
    Forward-order insertion recreates the original layout exactly.
    Safe to call when nothing was removed (no-op).
    """
    if not _removed_per_mode:
        return

    for mode, removed_list in _removed_per_mode.items():
        tools = _get_tool_list(mode)
        if tools is None:
            continue

        for idx, item in removed_list:   # ascending (forward) order
            tools.insert(idx, item)

        print(
            f"[CAD tpanel_filter] mode={mode!r}: "
            f"restored {len(removed_list)} items"
        )

    _removed_per_mode.clear()
