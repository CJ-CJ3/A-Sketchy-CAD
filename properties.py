# ─────────────────────────────────────────────────────────────────────
#  properties.py  –  CADSettings scene PropertyGroup
# ─────────────────────────────────────────────────────────────────────

import bpy


class CADSettings(bpy.types.PropertyGroup):
    # ── snap ────────────────────────────────────────────────────────
    snap_grid: bpy.props.BoolProperty(
        name="Grid Snap",
        default=True,
    )
    snap_vertex: bpy.props.BoolProperty(
        name="Vertex Snap",
        default=True,
    )
    snap_endpoint: bpy.props.BoolProperty(
        name="Endpoint Snap",
        default=True,
    )
    snap_increment: bpy.props.FloatProperty(
        name="Grid Size",
        default=0.5,
        min=0.001,
        max=100.0,
        step=10,
        description="Grid cell size in scene units",
    )

    polar_tracking_enabled: bpy.props.BoolProperty(
        name="Enable Polar Tracking",
        description=(
            "Constrain the drawing direction to preset angle increments "
            "as the cursor moves, snapping the angle (not the distance) "
            "once the cursor is within tolerance of a tracking direction"
        ),
        default=True,
    )
    polar_angle_increment: bpy.props.EnumProperty(
        name="Polar Angle Increment",
        description="Preset angle increment set used by Polar Tracking",
        items=[
            ('90',   "90°",   "Track 0°, 90°, 180°, 270°"),
            ('45',   "45°",   "Track every 45° (0°, 45°, 90°, ...)"),
            ('30',   "30°",   "Track every 30° (0°, 30°, 60°, ...)"),
            ('22.5', "22.5°", "Track every 22.5° (0°, 22.5°, 45°, ...)"),
            ('15',   "15°",   "Track every 15° (0°, 15°, 30°, ...)"),
        ],
        default='45',
    )
    polar_angle_tolerance: bpy.props.FloatProperty(
        name="Polar Tracking Tolerance",
        description="Angular tolerance in degrees for snapping to a Polar Tracking direction",
        default=5.0, min=0.5, max=15.0, step=10,
    )
    snap_angle_increment: bpy.props.FloatProperty(
        name="Angle Increment",
        default=5.0,
        min=0.1,
        max=90.0,
        step=100,
        description="Angular increment in degrees for polar/angle "
                    "increment snapping while drawing from a start point",
    )

    # ── OTRACK (Object Snap Tracking) ─────────────────────────────────
    otrack_enable_h: bpy.props.BoolProperty(
        name="Track Horizontal",
        description="Project a horizontal tracking guide through each acquired point",
        default=True,
    )
    otrack_enable_v: bpy.props.BoolProperty(
        name="Track Vertical",
        description="Project a vertical tracking guide through each acquired point",
        default=True,
    )
    otrack_enable_polar: bpy.props.BoolProperty(
        name="Track Polar",
        description="Project polar tracking guides through each acquired point (off by default - OTRACK is currently X/Y-axis only)",
        default=False,
    )
    otrack_polar_increment: bpy.props.FloatProperty(
        name="Polar Angle Increment",
        description=(
            "Angle increment in degrees for OTRACK polar guides "
            "(0 = use the default AutoCAD-style angle set: "
            "0/30/45/60/90 and their mirrors)"
        ),
        default=0.0, min=0.0, max=180.0, step=100,
    )
    otrack_dwell_seconds: bpy.props.FloatProperty(
        name="Hover Acquisition Delay",
        description="Seconds the cursor must dwell on a snap point before it is acquired for tracking",
        default=0.45, min=0.05, max=3.0, step=1,
    )
    otrack_max_points: bpy.props.IntProperty(
        name="Max Tracking Points",
        description="Maximum number of simultaneously acquired OTRACK points",
        default=8, min=1, max=32,
    )

    # ── mode state ───────────────────────────────────────────────────
    sketch_mode: bpy.props.BoolProperty(
        name="Sketch Mode Active",
        default=False,
    )
    active_tool: bpy.props.StringProperty(default="SELECT")

    # ── vertex / edge / face select (like Edit Mode) ─────────────────
    select_mode: bpy.props.EnumProperty(
        name="Select Mode",
        items=[
            ('VERTEX', "Vertex", "Select vertices", 'VERTEXSEL', 1),
            ('EDGE',   "Edge",   "Select edges",    'EDGESEL',   2),
            ('FACE',   "Face",   "Select faces",    'FACESEL',   4),
        ],
        default='VERTEX',
    )

    # ── lock view ────────────────────────────────────────────────────
    lock_view: bpy.props.EnumProperty(
        name="Lock View",
        description="Snap and lock the viewport to this canonical ortho view",
        items=[
            ('TOP',    "Top",    "View from above (Numpad 7)",        'TRIA_UP',    0),
            ('BOTTOM', "Bottom", "View from below (Ctrl+Numpad 7)",   'TRIA_DOWN',  1),
            ('FRONT',  "Front",  "View from the front (Numpad 1)",    'TRIA_UP',    2),
            ('BACK',   "Back",   "View from behind (Ctrl+Numpad 1)",  'TRIA_DOWN',  3),
            ('RIGHT',  "Right",  "View from the right (Numpad 3)",    'TRIA_RIGHT', 4),
            ('LEFT',   "Left",   "View from the left (Ctrl+Numpad 3)",'TRIA_LEFT',  5),
        ],
        default='TOP',
    )
    view_locked: bpy.props.BoolProperty(
        name="View Locked",
        description="Whether the viewport is currently locked to a canonical view",
        default=False,
    )

    # ── last result ──────────────────────────────────────────────────
    last_length: bpy.props.FloatProperty(default=0.0)
    last_angle:  bpy.props.FloatProperty(default=0.0)
    last_radius: bpy.props.FloatProperty(default=0.0)
    last_width:  bpy.props.FloatProperty(default=0.0)
    last_height: bpy.props.FloatProperty(default=0.0)

    # ── AutoCAD-style cursor (Sketch Mode) ─────────────────────────────
    # See gfx/cursor.py and operators/sketch_mode.py for usage.
    cursor_pickbox_px: bpy.props.IntProperty(
        name="Pick Box Size",
        description=(
            "Half-size, in pixels, of the small square 'pick box' drawn "
            "at the centre of the crosshair (AutoCAD PICKBOX default = 3)"
        ),
        default=3, min=1, max=20,
    )
    snap_highlight_px: bpy.props.FloatProperty(
        name="Highlight Radius (px)",
        description=(
            "Early-feedback detection radius in screen pixels - the "
            "snap indicator appears as soon as the cursor is within "
            "this pixel distance of a snap point. The real "
            "snap-activation radius stays governed separately by "
            "Aperture Size, and should be kept noticeably smaller "
            "than this so the indicator appears well before the "
            "cursor actually engages the snap"
        ),
        default=110.0, min=1.0, max=500.0, step=10,
    )
    snap_highlight_world_units: bpy.props.FloatProperty(
        name="Highlight Radius (units)",
        description=(
            "Early-feedback detection distance in scene units - the "
            "snap indicator appears as soon as the cursor is within "
            "this actual distance of a snap point, regardless of "
            "zoom level. The real snap-activation radius stays "
            "governed separately by Aperture Size (screen pixels)"
        ),
        default=4.0, min=0.01, max=1000.0, step=10,
    )
    snap_highlight_mult: bpy.props.FloatProperty(
        name="Highlight Radius Multiplier",
        description=(
            "How much wider than the actual snap-activation radius the "
            "highlight marker's detection radius is. The marker appears "
            "early, as the cursor approaches, but the point doesn't "
            "actually snap until inside the smaller activation radius"
        ),
        default=5.5, min=1.0, max=8.0, step=10,
    )
    cursor_aperture_px: bpy.props.IntProperty(
        name="Aperture Size",
        description=(
            "Object-snap aperture radius in pixels - how close the cursor "
            "must be to a snap point before the AutoSnap marker engages "
            "(AutoCAD APERTURE valid range is 1-50)"
        ),
        default=20, min=1, max=50,
    )
    cursor_show_aperture: bpy.props.BoolProperty(
        name="Show Aperture (debug)",
        description=(
            "Visualise the normally-invisible object-snap aperture box "
            "around the cursor - useful for tuning Aperture Size"
        ),
        default=False,
    )
    cursor_show_grips: bpy.props.BoolProperty(
        name="Show Grips",
        description=(
            "Draw blue grip handles on the vertices of selected objects "
            "while in Sketch Mode"
        ),
        default=True,
    )
    cursor_grip_px: bpy.props.IntProperty(
        name="Grip Size",
        description="Half-size, in pixels, of the grip squares drawn on selected objects",
        default=4, min=2, max=12,
    )
    cursor_grip_proximity_only: bpy.props.BoolProperty(
        name="Show Grips Near Cursor Only",
        description=(
            "Only draw a vertex/grip square once the crosshair comes "
            "within Grip Reveal Radius of it, instead of showing every "
            "grip on the selection all the time"
        ),
        default=True,
    )
    cursor_grip_reveal_px: bpy.props.IntProperty(
        name="Grip Reveal Radius",
        description=(
            "How close, in pixels, the crosshair must get to a vertex "
            "before its grip square is revealed (only used when "
            "'Show Grips Near Cursor Only' is enabled)"
        ),
        default=40, min=5, max=300,
    )
    cursor_crosshair_pct: bpy.props.IntProperty(
        name="Crosshair Size",
        description=(
            "Length of each crosshair arm, as a percentage of the "
            "viewport's largest dimension (mirrors AutoCAD's CURSORSIZE: "
            "100 = old-style full-screen crosshair, 5 = AutoCAD's modern "
            "short default)"
        ),
        default=5, min=1, max=100,
    )