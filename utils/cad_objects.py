# ─────────────────────────────────────────────────────────────────────
#  utils/cad_objects.py
#  Blender collection + mesh-object creation helpers
# ─────────────────────────────────────────────────────────────────────

import bpy
import json


def ensure_cad_collection(context) -> bpy.types.Collection:
    """Return (or create) the 'CAD Sketches' collection."""
    col = bpy.data.collections.get("CAD Sketches")
    if col is None:
        col = bpy.data.collections.new("CAD Sketches")
        context.collection.children.link(col)
    return col


def pick_reference_object(context):
    """
    Return the object selected before drawing started, used to make
    the new shape a linked duplicate of it (shared mesh data, same
    view layer).  Returns None if nothing was selected.

    Checks multiple bpy paths because context.selected_objects can be
    empty right after a T-panel tool click even when something was
    previously selected.
    """
    candidates = []

    active = context.view_layer.objects.active
    if active is not None:
        candidates.append(active)

    try:
        candidates.extend(context.selected_objects)
    except AttributeError:
        pass

    try:
        candidates.extend(context.view_layer.objects.selected)
    except AttributeError:
        pass

    for obj in candidates:
        if obj is None:
            continue
        try:
            # Must have mesh data and belong to at least one collection
            if obj.type == 'MESH' and obj.users_collection:
                print(f"[CAD] pick_reference_object -> '{obj.name}'")
                return obj
        except (ReferenceError, RuntimeError):
            continue

    print("[CAD] pick_reference_object -> None (nothing selected)")
    return None


def create_cad_object(context, name: str, verts, edges,
                      cad_type: str, params: dict,
                      reference_obj: bpy.types.Object = None) -> bpy.types.Object:
    """
    Build a new CAD mesh object from *verts* / *edges* and add it to
    the scene as a fully independent object.

    Collection placement
    ────────────────────
    If *reference_obj* is given (an existing object that was selected
    before drawing started), the new shape is linked into the same
    collection(s) as that object so it appears on the same View Layer.
    The reference object itself is never modified.

    If no reference object is given, the new shape goes into the shared
    'CAD Sketches' collection.
    """
    # Always build a fresh, independent mesh — never share mesh data
    # with the reference object.
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, edges, [])
    me.update()

    obj = bpy.data.objects.new(name, me)
    obj["cad_type"]   = cad_type
    obj["cad_params"] = json.dumps(params)

    # Determine target collection(s)
    linked = False
    if reference_obj is not None:
        try:
            ref_name = reference_obj.name
            ref_collections = list(reference_obj.users_collection)
        except (ReferenceError, RuntimeError):
            ref_name = None
            ref_collections = []

        for col in ref_collections:
            col.objects.link(obj)
            linked = True

        if linked:
            print(f"[CAD] '{obj.name}' placed in same View Layer as "
                  f"'{ref_name}' "
                  f"(collections: {[c.name for c in ref_collections]})")
        else:
            print(f"[CAD] reference '{ref_name}' had no collections "
                  f"- falling back to 'CAD Sketches'")

    if not linked:
        ensure_cad_collection(context).objects.link(obj)
        print(f"[CAD] '{obj.name}' placed in 'CAD Sketches' (no reference object)")

    # Make the new object the active selection
    for o in bpy.context.selected_objects:
        o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    from .. import state as _st
    _st.snap_cache_dirty = True

    return obj
