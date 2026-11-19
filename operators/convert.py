# ─────────────────────────────────────────────────────────────────────
#  operators/convert.py
#  CAD_OT_ConvertToMesh  –  strip parametric tags from a CAD object
# ─────────────────────────────────────────────────────────────────────

import bpy


class CAD_OT_ConvertToMesh(bpy.types.Operator):
    bl_idname  = "cad.convert_to_mesh"
    bl_label   = "Convert to Mesh"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        obj = context.active_object
        if obj is None or "cad_type" not in obj:
            self.report(
                {'WARNING'}, "Select a parametric CAD object first"
            )
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        obj = context.active_object
        t   = obj.get("cad_type", "?")
        for key in ("cad_type", "cad_params"):
            if key in obj:
                del obj[key]
        self.report(
            {'INFO'}, f"'{obj.name}' ({t}) converted to editable mesh"
        )
        return {'FINISHED'}