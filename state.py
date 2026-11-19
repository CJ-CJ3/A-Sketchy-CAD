# ─────────────────────────────────────────────────────────────────────
#  state.py  –  module-level shared globals
#
#  All other modules import from here so there is exactly ONE copy of
#  each reference at runtime.
# ─────────────────────────────────────────────────────────────────────

# Running CAD_OT_SketchModeManager instance (or None when inactive)
sketch_manager_ref = None

# bpy.types.Area where Sketch Mode is currently running (or None)
sketch_area_ref = None

# Saved original VIEW3D_HT_header.draw function (restored on unregister)
orig_header_draw = None

# Object that was selected the last time SELECT was the active tool in
# Sketch Mode - used so newly-drawn shapes can be linked into the same
# collection(s) ("layer") as whatever the user had selected before
# picking a draw tool. Updated continuously by
# CAD_OT_SketchModeManager.modal() while active_tool == 'SELECT'.
# Object that was selected the last time SELECT was the active tool in
# Sketch Mode - used so newly-drawn shapes can be linked into the same
# collection(s) ("layer") as whatever the user had selected before
# picking a draw tool. Updated continuously by
# CAD_OT_SketchModeManager.modal() while active_tool == 'SELECT'.
sketch_ref_obj = None

# Set to True by anything that creates or modifies CAD geometry (see
# utils/cad_objects.create_cad_object). The SELECT-tool idle overlay
# checks this every modal tick and rebuilds its snap cache whenever
# it's True, then clears it - so newly-finished sketches are always
# picked up immediately, without depending on active_tool transition
# timing.
snap_cache_dirty = True

# Currently selected Arc Tool sub-method - persists across tool
# re-activations until the user explicitly switches it (Alt+Scroll or
# the 'M' shortcut inside CAD_OT_DrawArc, before the first point is
# placed). '3POINT' or 'CSE' (Center-Start-End).
arc_method = '3POINT'

# Currently selected Spline Tool sub-method - persists across tool
# re-activations until switched (Alt menu inside CAD_OT_DrawSpline,
# before the first point is placed). 'FIT' or 'CV'.
spline_method = 'FIT'

# Last-used Fillet Tool radius - persists across tool re-activations
# until the user types a new value (Shift/typed digits inside
# CAD_OT_DrawFillet).
fillet_radius = 0.5

# Last-used Chamfer Tool distance - persists across tool
# re-activations until the user types a new value.
chamfer_distance = 0.5

# Last-used Offset distance - persists across tool re-activations
# (signed: negative just means the last offset was on the opposite
# side, same convention as the live drag/typed value in the tool).
offset_distance = 1.0

# Currently selected Array Tool sub-method - persists across tool
# re-activations until switched (Alt menu inside CAD_OT_SketchArray).
# 'RECT', 'GRID', 'POLAR', or 'PATH'.
array_method = 'RECT'# Blender interaction mode that was active before entering Sketch Mode
# (e.g. 'OBJECT', 'EDIT_MESH', 'SCULPT', …).  Used by RETURN to restore it.
previous_mode = None

# Pending vertex-move request set by CAD_OT_EditElement when the user
# presses M (or clicks the Move button while vertices are selected).
# Consumed immediately by CAD_OT_EditMove.invoke().
# Format: {'obj': bpy.types.Object, 'verts': [int, ...]} or None.
edit_pending_move = None

# Measurements the user has chosen to "keep" when exiting the Measure
# tool (see CAD_OT_Measure's Keep-on-Exit toggle, default key 'K').
# Same dict shape as CAD_OT_Measure._measurements entries (tagged by
# 'kind': 'LEN' / 'AREA' / 'RADIUS' / 'DIAMETER' / 'ARC' / 'ANGLE').
#
# Drawn by CAD_OT_SketchModeManager's always-on overlay handler
# (_draw_cursor_overlay in operators/sketch_mode.py) so kept
# measurements stay visible on screen for the rest of the Sketch Mode
# session - even after Measure itself has been closed and other tools
# are in use - rather than disappearing the moment Measure exits like
# an ordinary (not-kept) measurement does. Cleared by the "Clear Kept
# Measurements" button/operator, or automatically when Sketch Mode
# itself is exited.
kept_measurements = []

# The currently-running CAD_OT_Measure modal operator instance, or None
# when the Measure tool isn't active. Set in CAD_OT_Measure.invoke(),
# cleared in its cancel(). Lets other code (the "CAD Dimension" N-panel,
# in ui/measurement_panel.py) reach into the *live* session data
# (operator._measurements / operator._selected) so double-clicking a
# dimension while Measure is still running can be edited from the panel
# with changes appearing immediately - state.kept_measurements alone
# only gets synced from a session's measurements when the tool exits
# (see CAD_OT_Measure.cancel()), so it can't be used for this on its own.
measure_operator_ref = None
# ─────────────────────────────────────────────────────────────────────
#  SelectionState  –  hover / active / multi-selection for Sketch Mode
#
#  Shared by CAD_OT_SketchModeManager (hover update + LMB handling)
#  and its draw callback (visual highlight).
#
#  Entity keys are tuples so they work for both vertices and edges:
#    vertex  → ('V', obj_name, vert_index)
#    edge    → ('E', obj_name, edge_index)
# ─────────────────────────────────────────────────────────────────────

class SelectionState:
    """Lightweight container for hover / click selection state.

    Only one hovered_entity can exist at any time (overwritten every
    MOUSEMOVE).  selected_entities is an ordered list; Shift+LMB
    adds/removes, plain LMB replaces it entirely.

    Entity format (tuple):
        ('V', obj_name: str, vert_index: int)
        ('E', obj_name: str, edge_index: int)
        ('F', obj_name: str, face_index: int)

    hover_priority controls which entity types are considered during hover:
        'AUTO'   - prefer vertex, then edge, then face (default)
        'VERTEX' - vertices only
        'EDGE'   - edges only
        'FACE'   - faces only
    Hotkeys: 1=VERTEX  2=EDGE  3=FACE  4=AUTO
    """
    hovered_entity   = None   # tuple | None
    active_entity    = None   # tuple | None  (last single-clicked item)
    selected_entities: list   = []
    hover_priority   = 'AUTO' # 'AUTO' | 'VERTEX' | 'EDGE' | 'FACE'

    # Snap points (Endpoint/Midpoint/Center/etc, per the active Alt-menu
    # snap_mode) explicitly selected by the user via click, Box, Lasso,
    # or Circle select. Each entry is a rounded (x, y, z) world-space
    # tuple. Drawn as green squares by the SELECT-tool idle overlay.
    selected_snap_points: list = []

    # Object Snap Tracking (OTRACK): points "acquired" by dwelling the
    # cursor over a valid snap point while drawing. Each acquired point
    # projects dashed horizontal/vertical alignment guides across the
    # viewport; the cursor snaps to points aligned with one guide, or
    # to the intersection of two. Cleared on finishing/cancelling the
    # current draw operation, or explicitly via Esc.
    tracked_points: list = []

    # ── Snap / select mode display box ───────────────────────────────
    # Ordered list of all selectable snap modes shown in the HUD box.
    # Each entry: (letter, label, internal_key)
    SNAP_MODES = [
        ('A', 'Endpoint',      'ENDPOINT'),
        ('M', 'Midpoint',      'MIDPOINT'),
        ('V', 'Vertex',        'VERTEX'),
        ('C', 'Center',        'CENTER'),
        ('I', 'Intersection',  'INTERSECTION'),
        ('N', 'Nearest',       'NEAREST'),
        ('G', 'Grid',          'GRID'),
        ('R', 'Increment',     'INCREMENT'),
        ('Q', 'Quadrant',      'QUADRANT'),
        ('T', 'Tangent',       'TANGENT'),
        ('P', 'Perpendicular', 'PERPENDICULAR'),
        ('L', 'Parallel',      'PARALLEL'),
    ]

    # Maps snap_mode key → which snap-cache buckets are searched.
    # None means search all available buckets (unrestricted).
    SNAP_MODE_TO_STYPES = {
        # build_snap_cache() populates a real, separate 'ENDPOINT'
        # bucket (segment endpoints) distinct from 'VERTEX' (every
        # mesh vertex, generic fallback) - this previously only
        # searched 'VERTEX', so selecting Endpoint never actually
        # looked in the bucket meant for it.
        'ENDPOINT':      ('ENDPOINT', 'VERTEX'),
        'MIDPOINT':      ('MIDPOINT',),
        'VERTEX':        ('VERTEX',),
        'CENTER':        ('CENTER',),
        'INTERSECTION':  ('INTERSECTION',),
        'NEAREST':       ('NEAREST',),
        'GRID':          ('GRID',),
        'INCREMENT':     ('INCREMENT',),
        'QUADRANT':      ('QUADRANT',),
        'TANGENT':       ('TANGENT',),
        'PERPENDICULAR': ('PERPENDICULAR',),
        'PARALLEL':      ('PARALLEL',),
    }

    snap_mode        = 'ENDPOINT'  # the one currently-active running snap type
    alt_mode_active  = False       # True while ALT is held → box expands

    # ── Running Object Snap ─────────────────────────────────────────────
    # Defaults to the standard set of running OSNAP types (matching
    # AutoCAD's typical out-of-the-box defaults) rather than Endpoint
    # alone - otherwise Midpoint/Center/Intersection never highlight
    # or snap for anyone who hasn't first pressed Alt+<letter> to add
    # them. Alt+<letter> (toggle_snap_membership) can still add/remove
    # further types; the scroll wheel/number keys/HUD click
    # (toggle_snap_type) still replace this with a single type.
    active_snap_types: set = {'ENDPOINT', 'MIDPOINT', 'CENTER', 'INTERSECTION'}

    # Master on/off switch for the whole running-OSNAP system, exactly
    # like AutoCAD's F3 toggle. When off, no geometry candidates are
    # searched at all (pure free-cursor/grid/increment behaviour) even
    # though the checked types in active_snap_types are remembered.
    osnap_enabled: bool = True

    # AutoCAD's documented internal priority order, used to break ties
    # when more than one active running-snap type has a candidate at
    # (effectively) the same location - e.g. two lines whose endpoints
    # coincide exactly with their intersection.
    SNAP_PRIORITY = [
        'ENDPOINT', 'MIDPOINT', 'CENTER', 'QUADRANT', 'INTERSECTION',
        'PERPENDICULAR', 'TANGENT', 'NEAREST', 'PARALLEL',
    ]

    @classmethod
    def get_active_stypes(cls):
        """
        Return the snap-cache bucket names for the one currently-active
        running OSNAP type, as a tuple (get_active_stypes stays plural/
        tuple-shaped since active_snap_types is a single-element set,
        so the rest of the codebase - which expects a tuple/None - does
        not need to change).

        Returns () when OSNAP is globally switched off (F3), so no
        geometry candidates are searched at all. Returns None
        (unrestricted - search everything) only if OSNAP is on but no
        type is set, as a safe fallback matching legacy behaviour.
        """
        if not cls.osnap_enabled:
            return ()
        if not cls.active_snap_types:
            return None
        out = set()
        for mode in cls.active_snap_types:
            stypes = cls.SNAP_MODE_TO_STYPES.get(mode)
            if stypes:
                out.update(stypes)
        return tuple(out) if out else None

    @classmethod
    def toggle_snap_type(cls, mode: str):
        """
        Select one running-OSNAP type (scroll wheel, number keys, HUD
        click).

        This always replaces whichever type(s) were previously active
        with just `mode` - it's the "quick single-mode selection" path,
        kept separate from toggle_snap_membership below so multi-select
        can only ever be reached via the documented keyboard shortcuts.
        """
        cls.active_snap_types = {mode}
        cls.snap_mode = mode  # also focus/highlight it in the HUD

    @classmethod
    def toggle_snap_membership(cls, mode: str):
        """
        Alt+<letter>: add/remove `mode` from the set of simultaneously
        active running-OSNAP types, without disturbing the others -
        AutoCAD's Alt-menu checkbox behaviour (Alt+A then Alt+M leaves
        both Endpoint and Midpoint active). Pressing the same
        shortcut again un-checks that one type; at least one type is
        always kept active so OSNAP never silently goes fully empty
        via this path (F3 is the explicit "OSNAP off" switch).
        """
        types = set(cls.active_snap_types)
        if mode in types and len(types) > 1:
            types.discard(mode)
        else:
            types.add(mode)
        cls.active_snap_types = types
        cls.snap_mode = mode  # last-touched type gets HUD focus

    # Animation clock: seconds since last hover-enter, used to pulse the
    # snap glow.  Reset to 0.0 each time hovered_entity changes.
    snap_hover_t     = 0.0
    _last_hover_ent  = None        # internal: detect hover-enter

    @classmethod
    def tick_hover(cls, dt: float):
        """Advance the hover animation clock; reset on new hover target."""
        if cls.hovered_entity != cls._last_hover_ent:
            cls.snap_hover_t    = 0.0
            cls._last_hover_ent = cls.hovered_entity
        else:
            cls.snap_hover_t += dt

    @classmethod
    def snap_index(cls):
        keys = [m[2] for m in cls.SNAP_MODES]
        return keys.index(cls.snap_mode) if cls.snap_mode in keys else 0

    @classmethod
    def reset(cls):
        cls.hovered_entity    = None
        cls.active_entity     = None
        cls.selected_entities = []
        cls.hover_priority    = 'AUTO'
        cls.snap_mode         = 'ENDPOINT'
        cls.alt_mode_active   = False
        cls.snap_hover_t      = 0.0
        cls.active_snap_types = {'ENDPOINT', 'MIDPOINT', 'CENTER', 'INTERSECTION'}
        cls.osnap_enabled     = True
        cls._last_hover_ent   = None

# Module-level singleton — import and use this everywhere.
selection = SelectionState()

# ────────────────────────────────────────────────────────────────────
#  WorkSpaceTool <-> CAD tool mapping
#
#  Maps each WorkSpaceTool idname registered in cad_tpanel_tools.py to
#  the matching cad_settings.active_tool id and the bl_idname of the
#  operator that implements it. Used by
#  CAD_OT_SketchModeManager._check_tool_activation() to auto-invoke
#  the right operator the instant its WorkSpaceTool becomes active
#  (selecting a tool in the T-panel), so a throw-away click in the
#  viewport isn't needed just to arm it.
# ────────────────────────────────────────────────────────────────────
AUTO_INVOKE_TOOLS = {
    "cad_addon.line_tool":    ("LINE",    "cad.draw_line"),
    "cad_addon.circle_tool":  ("CIRCLE",  "cad.draw_circle"),
    "cad_addon.ellipse_tool": ("ELLIPSE", "cad.draw_ellipse"),
    "cad_addon.point_tool":   ("POINT",   "cad.draw_point"),
    "cad_addon.arc_tool":     ("ARC",     "cad.draw_arc"),
    "cad_addon.spline_tool":  ("SPLINE",  "cad.draw_spline"),
    "cad_addon.fillet_tool":  ("FILLET",  "cad.draw_fillet"),
    "cad_addon.chamfer_tool": ("CHAMFER", "cad.draw_chamfer"),
    "cad_addon.extend_tool":  ("EXTEND",  "cad.draw_extend"),
    "cad_addon.rect_tool":    ("RECT",    "cad.draw_rect"),
    "cad_addon.move_tool":    ("MOVE",    "cad.sketch_move"),
    "cad_addon.scale_tool":   ("SCALE",   "cad.sketch_scale"),
    "cad_addon.array_tool":   ("ARRAY",   "cad.sketch_array"),
    "cad_addon.stretch_tool": ("STRETCH", "cad.sketch_stretch"),
    "cad_addon.rotate_tool":  ("ROTATE",  "cad.sketch_rotate"),
    "cad_addon.copy_tool":    ("COPY",    "cad.sketch_copy"),
    "cad_addon.mirror_tool":  ("MIRROR",  "cad.sketch_mirror"),
    "cad_addon.trim_tool":    ("TRIM",    "cad.sketch_trim"),
    "cad_addon.offset_tool":  ("OFFSET",  "cad.draw_offset"),
    "cad_addon.measure_tool": ("MEASURE", "cad.measure"),
}


def get_active_tool_idname(context):
    """Return the idname of the active VIEW_3D tool for the current mode,
    or None if it can't be determined."""
    try:
        ws = context.workspace
        if ws is None:
            return None
        tool = ws.tools.from_space_view3d_mode(context.mode, create=False)
        return tool.idname if tool else None
    except Exception:
        return None
