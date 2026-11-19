# ─────────────────────────────────────────────────────────────────────
#  utils/math_utils.py
#  Snapping, axis-lock, coordinate conversion, angle convention
# ─────────────────────────────────────────────────────────────────────

import math
from mathutils import Vector
from bpy_extras import view3d_utils


# ════════════════════════════════════════════════════════════════════
#  ANGLE CONVENTION  (+CCW / -CW,  0° = +X)
# ════════════════════════════════════════════════════════════════════

def world_ang_to_display(rad: float) -> float:
    """Convert a world-space angle in radians to display degrees."""
    return math.degrees(rad)


def display_ang_to_rad(deg: float) -> float:
    """Convert display degrees to radians."""
    return math.radians(deg)


def parse_typed_length(context, text: str) -> float:
    """
    Parse a typed length string (as digits typed into a modal
    operator's numeric-entry buffer, e.g. "12" or "12mm" or "3.5ft")
    into Blender's internal (scene-scaled) units - the exact inverse
    of how Measure's own _format_length() displays a length.

    This is what every typed-distance tool (Line, Rect, Circle,
    Ellipse, Arc, Chamfer, Fillet, Move, Copy, Mirror, Stretch,
    Offset, Point, Spline, ...) should run typed input through before
    using it as a world-space distance. Without it, typing "12" always
    means the same raw Blender unit no matter what the scene's Unit
    System is currently set to (mm vs m, etc.) - since a bare number
    has no unit of its own, only the *display* convention says what it
    means, exactly like Blender's own numeric input fields.

    A bare number (no unit suffix) is interpreted in whichever unit
    the scene is currently displaying (unit_settings.length_unit - mm,
    cm, m, ft, ...); an explicit suffix in the typed text (e.g. "12cm"
    while the scene displays in mm) always overrides that, matching
    Blender's own fields.
    """
    us = context.scene.unit_settings
    if us.system == 'NONE':
        return float(text)
    try:
        import bpy
        meters = bpy.utils.units.to_value(us.system, 'LENGTH', text, str_ref_unit=us.length_unit)
        scale = us.scale_length if us.scale_length else 1.0
        return meters / scale
    except Exception:
        return float(text)


# Explicit conversion table (meters-per-unit) + display suffix for
# every concrete length unit Blender's Scene > Units panel offers.
# NOTE: bpy.utils.units.to_string() does NOT take the scene's chosen
# unit into account - it always picks its own "best fit" (adaptive)
# unit for the given magnitude, silently ignoring
# unit_settings.length_unit. That made dynamic labels flip between
# units on their own instead of honestly showing whichever unit the
# user picked in the dropdown, and never really "displayed in mm"
# just because mm was selected. Converting explicitly here fixes
# that: every label is rendered in *exactly* the unit currently
# selected, and re-reads unit_settings.length_unit fresh every call,
# so it updates immediately whenever the user changes the dropdown.
LENGTH_UNIT_INFO = {
    'KILOMETERS':  (1000.0,     'km'),
    'METERS':      (1.0,        'm'),
    'CENTIMETERS': (0.01,       'cm'),
    'MILLIMETERS': (0.001,      'mm'),
    'MICROMETERS': (0.000001,   '\u00b5m'),   # µm
    'MILES':       (1609.344,   'mi'),
    'FEET':        (0.3048,     'ft'),
    'INCHES':      (0.0254,     'in'),
    'THOU':        (0.0000254,  'thou'),
}


def _fmt_num(value: float, precision: int = 2) -> str:
    """Round to a fixed *precision* decimal places, matching Blender's
    own numeic field convention and this addon's angle formatting
    (see format_angle) - always shows the same number of decimals
    rather than stripping trailing zeros, e.g. 12.0 -> '12.00',
    12.5 -> '12.50'. Default precision is 2, matching the "NN.NN"
    convention used everywhere else measurements are displayed."""
    s = f"{value:.{precision}f}"
    if float(s) == 0.0:
        s = f"{0:.{precision}f}"
    return s


def format_length(context, value: float) -> str:
    """
    Format a world-space distance using the scene's own Unit System
    (Properties panel > Scene > Units) - the exact counterpart to
    parse_typed_length() above, and the same convention Measure's own
    _format_length() uses. Shared here so every tool's live length
    readout (dynamic-input boxes, drag previews, dimension lines, ...)
    follows km/m/cm/mm/µm/miles/ft/in/thou consistently, always in
    whichever single unit is currently selected, instead of Blender's
    own adaptive best-fit (or a hardcoded suffix).

    Because unit_settings.length_unit is read fresh on every call,
    every dynamic label using this helper updates immediately the
    moment the user changes the selected unit - no caching, no stale
    suffix left over from the previous unit.
    """
    us = context.scene.unit_settings
    if us.system == 'NONE':
        return _fmt_num(value)

    meters = value * (us.scale_length if us.scale_length else 1.0)
    info = LENGTH_UNIT_INFO.get(us.length_unit)

    if info is None:
        # 'ADAPTIVE' (or any future/unknown enum value) - let Blender
        # pick its own best-fit unit for the magnitude, same as before.
        try:
            import bpy
            return bpy.utils.units.to_string(
                us.system, 'LENGTH', meters,
                precision=2, split_unit=us.use_separate,
            )
        except Exception:
            return _fmt_num(value)

    factor, suffix = info
    return f"{_fmt_num(meters / factor)} {suffix}"


# ════════════════════════════════════════════════════════════════════
#  ANGLE FORMATTING  (Degrees / Radians - scene Unit System aware)
# ════════════════════════════════════════════════════════════════════
#
# Blender's own Scene > Units panel has a "Rotation" setting
# (unit_settings.system_rotation) that is either 'DEGREES' or
# 'RADIANS' - the exact rotation counterpart of length_unit. Just
# like format_length() above, format_angle() reads this fresh on
# every call so every dynamic angle label (Rotate's live readout,
# Line/Mirror/Copy/Move/Stretch's polar-tracking indicator, Arc's
# sweep-angle box, Array's polar angle field, ...) automatically
# switches format the instant the user changes that dropdown -
# no caching, nothing left over from the previous unit.

_ANGLE_SUFFIX = {
    'DEGREES': '\u00b0',   # °
    'RADIANS': ' rad',
}


def format_angle(context, angle_rad: float, as_degrees: bool = None) -> str:
    """
    Format an angle for display using the scene's own Unit System
    (Properties panel > Scene > Units > Rotation), the exact rotation
    counterpart of format_length().

    *angle_rad* is the angle in radians (Blender's internal
    representation).

    *as_degrees* lets a caller force the unit regardless of the scene
    setting (True = degrees, False = radians). Leave it as None
    (default) to auto-detect from context.scene.unit_settings.
    system_rotation - this is what makes the label update
    automatically whenever the user changes the selected unit.

    Always uses a fixed 2-decimal-place format with the correct unit
    symbol, matching Blender's own rotation field convention:
      format_angle(context, math.radians(45))                  -> "45.00°"
      format_angle(context, math.radians(45), as_degrees=False) -> "0.79 rad"
    """
    if as_degrees is None:
        try:
            as_degrees = context.scene.unit_settings.system_rotation != 'RADIANS'
        except Exception:
            as_degrees = True

    if as_degrees:
        return f"{math.degrees(angle_rad):.2f}\u00b0"
    else:
        return f"{angle_rad:.2f} rad"


def parse_typed_angle(context, text: str, as_degrees: bool = None) -> float:
    """
    Parse a typed angle string (e.g. "45" or "45°" or "90.5d" or
    "1.57 rad") into radians (Blender's internal representation) -
    the exact inverse of format_angle() above.

    *as_degrees* specifies how a bare number (no unit suffix) is
    interpreted. Leave it as None (default) to auto-detect from
    context.scene.unit_settings.system_rotation, matching whatever
    unit the Rotation dropdown currently displays; an explicit suffix
    in the typed text (e.g. "1.57rad" while the scene displays in
    degrees) always overrides that, matching Blender's own fields.

    Recognised suffixes:
      - degrees: "°", "d", "deg", "degree", "degrees"
      - radians: "rad", "r"
    """
    if as_degrees is None:
        try:
            as_degrees = context.scene.unit_settings.system_rotation != 'RADIANS'
        except Exception:
            as_degrees = True

    text = text.strip().lower()

    # Check for an explicit suffix (longer tokens first so 'rad'
    # isn't accidentally matched by the bare 'r' or 'd' check).
    for suffix in ('degree', 'degrees', 'deg', 'rad', '\u00b0', 'd', 'r'):
        if suffix in text:
            text = text.replace(suffix, '').strip()
            if suffix in ('degree', 'degrees', 'deg', '\u00b0', 'd'):
                return math.radians(float(text))
            else:
                return float(text)

    # No suffix: use the currently selected (or forced) default unit.
    value = float(text)
    return math.radians(value) if as_degrees else value


# ════════════════════════════════════════════════════════════════════
#  SNAP UTILITIES
# ════════════════════════════════════════════════════════════════════

def build_snap_cache(context) -> dict:
    """Build a cache of snap candidate world positions from visible meshes.

    Each bucket follows the entity-type activation rules a CAD snapping
    system is expected to use, rather than treating every mesh
    identically:

      VERTEX       - every mesh vertex (generic fallback / PERPENDICULAR)
      ENDPOINT     - vertices of entities that actually *have* endpoints
                     (Line/Polyline/Rectangle segments, generic meshes).
                     A closed circular curve (cad_type == 'circle') has
                     no endpoints and contributes none.
      MIDPOINT     - true midpoint of each real segment (Line/Polyline/
                     Rectangle edges, generic meshes). A circle's 64
                     polygon-approximation edges are NOT segments in the
                     CAD sense, so they contribute no midpoints.
      CENTER       - only for genuinely circular geometry: the circle's
                     authored centre (from cad_params), not the object's
                     transform origin - plus face centroids for generic
                     3D meshes (sphere-like projections).
      INTERSECTION - real crossing points between edges, checked across
                     *all* visible entities (not just within the same
                     mesh), so e.g. two separate Line sketches crossing
                     each other are found.
      NEAREST      - NOT pre-sampled points. See NEAREST_EDGES below;
                     the true nearest point is projected onto the
                     segment relative to the cursor at query time (in
                     apply_snaps / find_hover_point), per
                     t = clamp(((P-P1)·(P2-P1)) / |P2-P1|², 0, 1).
      NEAREST_EDGES- (v0, v1) world-space segment pairs used to compute
                     NEAREST live, so the snap point always reflects the
                     current cursor position instead of a handful of
                     fixed samples.
      QUADRANT     - 4 cardinal points of true circles only (from their
                     authored centre/radius), not a bounding-box guess
                     applied to every shape including lines/rectangles.
      TANGENT      - 8 perimeter points of true circles only.
      PERPENDICULAR- same as VERTEX (used for constraint logic)
      PARALLEL     - same pool as MIDPOINT (direction locking)
      GRID         - populated lazily at query time (not pre-built)
      INCREMENT    - empty here; computed per-frame in apply_snaps
    """
    import json

    # Freshly-linked mesh objects (e.g. a sketch shape that was just
    # finished a moment ago) don't always show up in
    # context.visible_objects immediately - that list reflects the
    # evaluated view layer, which can lag one tick behind bpy.data
    # changes. Force a sync so newly-drawn sketches are always
    # available as snap candidates, not just pre-existing scene
    # objects.
    try:
        context.view_layer.update()
    except Exception:
        pass

    cache = {
        'VERTEX':        [],
        'ENDPOINT':      [],
        'MIDPOINT':      [],
        'CENTER':        [],
        'INTERSECTION':  [],
        'NEAREST':       [],
        'NEAREST_EDGES': [],
        'CIRCLES':       [],
        'QUADRANT':      [],
        'TANGENT':       [],
        'PERPENDICULAR': [],
        'PARALLEL':      [],
        'GRID':          [],
        'INCREMENT':     [],
    }

    # Collected once, across every object, so INTERSECTION can check
    # edge pairs between *different* entities - not just within one.
    all_edges_ws = []

    # Cap total edges considered for intersection testing so a very
    # busy scene stays real-time (pairwise cost is O(n^2)).
    _MAX_INTERSECT_EDGES = 400

    for obj in context.visible_objects:
        if obj.type != 'MESH':
            continue
        mw    = obj.matrix_world
        mesh  = obj.data
        verts = [mw @ v.co for v in mesh.vertices]

        cad_type = obj.get("cad_type")   # None for non-CAD/generic meshes
        is_circle = (cad_type == 'circle')

        # VERTEX: always the generic/unrestricted pool.
        cache['VERTEX'].extend(verts)
        cache['PERPENDICULAR'].extend(verts)

        # ENDPOINT: entity.hasEndpoints == true.  A genuinely closed
        # circular curve has no start/end; a polyline, rectangle,
        # construction line, or generic mesh does (each straight
        # segment's own two endpoints, which is exactly what a CAD
        # system considers "Endpoint" candidates - candidatePoints =
        # [line.start, line.end] per segment).
        if not is_circle:
            cache['ENDPOINT'].extend(verts)

        # MIDPOINT / NEAREST_EDGES / PARALLEL along each real segment.
        # A circle's 64-edge polygon approximation isn't a set of real
        # CAD segments, so it's excluded from Midpoint (matching the
        # spec: Circle isn't a supported Midpoint entity) - its own
        # true geometric midpoint concept doesn't apply the same way a
        # Line's (P1+P2)/2 does.
        edges_ws = []
        for edge in mesh.edges:
            v0 = verts[edge.vertices[0]]
            v1 = verts[edge.vertices[1]]
            edges_ws.append((v0, v1))
            if (v1 - v0).length_squared < 1e-12:
                continue
            cache['NEAREST_EDGES'].append((v0, v1))
            if not is_circle:
                mp = (v0 + v1) * 0.5
                cache['MIDPOINT'].append(mp)
                cache['PARALLEL'].append(mp)

        if len(all_edges_ws) < _MAX_INTERSECT_EDGES:
            all_edges_ws.extend(edges_ws)

        # CENTER: entity.hasCenter == true.  Only genuinely circular
        # geometry has a meaningful "centre" in the CAD sense - a
        # Line or Rectangle does not.  Use the circle's *authored*
        # centre (from cad_params, exact by construction) rather than
        # the object's transform origin, which for our sketch objects
        # is just (0, 0, 0) baked-vertex data and does not reflect the
        # true drawn centre at all.
        if is_circle:
            try:
                params = json.loads(obj.get("cad_params", "{}"))
                c = params.get("center")
                r = float(params.get("radius", 0.0))
            except Exception:
                c, r = None, 0.0
            if c is not None:
                center = mw @ Vector((c[0], c[1], c[2] if len(c) > 2 else 0.0))
                cache['CENTER'].append(center)
                cache['CIRCLES'].append((center, r))

                # QUADRANT: exact cardinal points of the real circle.
                z = center.z
                for a in (0.0, math.pi * 0.5, math.pi, math.pi * 1.5):
                    cache['QUADRANT'].append(
                        Vector((center.x + r * math.cos(a),
                                center.y + r * math.sin(a), z))
                    )
                # TANGENT: 8 perimeter points of the real circle.
                for a in (i * math.pi / 4 for i in range(8)):
                    cache['TANGENT'].append(
                        Vector((center.x + r * math.cos(a),
                                center.y + r * math.sin(a), z))
                    )
        elif cad_type is None:
            # Generic (non-CAD) mesh, e.g. an imported sphere: fall
            # back to face centroids as a practical "centre/projection"
            # candidate, since we have no authored centre metadata to
            # rely on. Line/Rectangle CAD objects (cad_type set, but
            # not a circle) intentionally contribute nothing here.
            for poly in mesh.polygons:
                cache['CENTER'].append(mw @ poly.center)

    # INTERSECTION: real crossing points between every pair of edges,
    # across *all* visible entities - Line-Line, Line-Arc-approximation,
    # Arc-Arc-approximation, and so on - not just within a single mesh.
    n = len(all_edges_ws)
    for i in range(n):
        a0, a1 = all_edges_ws[i]
        for j in range(i + 1, n):
            b0, b1 = all_edges_ws[j]
            pt = _edge_intersect_2d(a0, a1, b0, b1)
            if pt is not None:
                cache['INTERSECTION'].append(pt)

    return cache


def _edge_intersect_2d(a0, a1, b0, b1):
    """Return the XY intersection point of two line segments, or None."""
    dx1, dy1 = a1.x - a0.x, a1.y - a0.y
    dx2, dy2 = b1.x - b0.x, b1.y - b0.y
    denom = dx1 * dy2 - dy1 * dx2
    if abs(denom) < 1e-9:
        return None
    t = ((b0.x - a0.x) * dy2 - (b0.y - a0.y) * dx2) / denom
    u = ((b0.x - a0.x) * dy1 - (b0.y - a0.y) * dx1) / denom
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        x = a0.x + t * dx1
        y = a0.y + t * dy1
        z = (a0.z + a1.z + b0.z + b1.z) * 0.25
        return Vector((x, y, z))
    return None


def _nearest_point_on_segment_3d(p: Vector, v0: Vector, v1: Vector) -> Vector:
    """
    True nearest-point-on-segment projection, per:
        t = ((P - P1) . (P2 - P1)) / |P2 - P1|^2
        t = clamp(t, 0, 1)
        nearest = P1 + t * (P2 - P1)
    """
    d = v1 - v0
    denom = d.x * d.x + d.y * d.y
    if denom < 1e-12:
        return v0.copy()
    t = ((p.x - v0.x) * d.x + (p.y - v0.y) * d.y) / denom
    t = max(0.0, min(1.0, t))
    return v0 + d * t


def _nearest_point_on_segment_2d(px, py, x0, y0, x1, y1):
    """Screen-space equivalent of _nearest_point_on_segment_3d."""
    dx, dy = x1 - x0, y1 - y0
    denom = dx * dx + dy * dy
    if denom < 1e-12:
        return x0, y0
    t = ((px - x0) * dx + (py - y0) * dy) / denom
    t = max(0.0, min(1.0, t))
    return x0 + dx * t, y0 + dy * t


def _tangent_points_from_point(p0: Vector, center: Vector, radius: float):
    """
    Solve for the (up to two) points T on circle(center, radius) such
    that the line P0-T is tangent to the circle:
        |T - center| = radius
        (T - center) . (P0 - T) = 0   (radius _|_ tangent)

    Using the right triangle P0-T-center (right angle at T):
        d     = |P0 - center|
        alpha = arccos(radius / d)      (angle at centre, T to P0)
        phi   = angle from centre to P0

    Returns [] if P0 is inside or on the circle (no real tangent).
    """
    d = (p0 - center)
    dist = math.hypot(d.x, d.y)
    if dist <= radius or dist < 1e-9:
        return []
    ratio = max(-1.0, min(1.0, radius / dist))
    alpha = math.acos(ratio)
    phi   = math.atan2(d.y, d.x)
    z     = center.z
    t1 = Vector((center.x + radius * math.cos(phi + alpha),
                 center.y + radius * math.sin(phi + alpha), z))
    t2 = Vector((center.x + radius * math.cos(phi - alpha),
                 center.y + radius * math.sin(phi - alpha), z))
    return [t1, t2]


def _priority_adjusted(d, stype):
    """
    Nudge a raw distance by an infinitesimal, priority-based offset so
    that when two candidates of different snap types are effectively
    at the same location (e.g. an Intersection that exactly coincides
    with an Endpoint), the one AutoCAD's documented internal priority
    order ranks higher wins - without changing which candidate wins
    in the normal case where one is genuinely closer than another.
    """
    from .. import state as _st
    try:
        rank = _st.SelectionState.SNAP_PRIORITY.index(stype)
    except ValueError:
        rank = len(_st.SelectionState.SNAP_PRIORITY)
    return d + rank * 1e-5


def apply_snaps(context, loc: Vector, snap_cache: dict, snap_filter=None, anchor=None,
                 threshold_mult: float = 0.6):
    """
    Return (snapped_location, snap_type_string).

    Priority: geometry snaps > GRID > INCREMENT > FREE

    *snap_filter* is an optional tuple/list of stype strings.  When supplied
    only those stype buckets from snap_cache are searched.
    Pass None (default) for unrestricted behaviour.

    *anchor* is the current in-progress operation's start point (e.g.
    the last-placed point of a line being drawn), required for the
    context-dependent constraint snaps TANGENT, PARALLEL, and
    PERPENDICULAR, which are only meaningful relative to an active
    line-creation operation. When anchor is None, those three types
    activate no candidates at all (matching their spec's
    lineCreationActive == true precondition).

    *threshold_mult* scales the base capture radius (snap_increment).
    The default (0.6) is the real, precise snap-activation radius used
    for actually moving the working cursor - see find_highlight_candidate
    below for the separate, wider radius used only for early visual
    feedback, which must never be passed here when the result is going
    to be used to move the cursor or place geometry.
    """
    s         = context.scene.cad_settings
    threshold = s.snap_increment * threshold_mult
    best_d, best_pt, best_type = threshold, None, 'FREE'

    # OBJECT SNAP TRACKING (OTRACK) intersections take priority over
    # nearby geometry once the cursor is within their tolerance - two
    # tracking guides crossing is a specific, deliberate target (the
    # entire point of tracking), so it shouldn't have to lose out to
    # whatever ordinary vertex/midpoint happens to be nearby.
    from .otrack import apply_tracking_intersection_snap
    intersection_hit = apply_tracking_intersection_snap(context, loc)
    if intersection_hit is not None:
        return intersection_hit

    # NOTE: previously this whole point-snap search (which covers every
    # geometry stype - VERTEX, MIDPOINT, CENTER, etc.) was gated purely
    # by the legacy `s.snap_vertex` / `s.snap_endpoint` scene toggles.
    # That meant the Alt-menu snap-mode selection (snap_filter) had no
    # effect whenever those two toggles were off: Midpoint/Center/etc.
    # could never be snapped to even though the user had explicitly
    # picked that mode, while a leftover/incorrect toggle state could
    # also make the "wrong" stype win. Now the search always runs when
    # an explicit snap_filter is supplied (Alt-menu mode is active), so
    # the mode the user picked is what actually gets searched for.
    if snap_filter or s.snap_vertex or s.snap_endpoint:
        stypes = snap_filter if snap_filter is not None else (
            'VERTEX', 'ENDPOINT', 'MIDPOINT', 'CENTER',
            'INTERSECTION', 'NEAREST', 'QUADRANT', 'TANGENT',
            'PERPENDICULAR', 'PARALLEL',
        )
        for stype in stypes:
            if stype in ('GRID', 'INCREMENT'):
                continue  # handled below
            if stype == 'NEAREST':
                # Project the cursor onto each real segment and clamp
                # to [0, 1] - the true nearest point on the geometry,
                # not a fixed set of pre-sampled points.
                for v0, v1 in snap_cache.get('NEAREST_EDGES', []):
                    proj = _nearest_point_on_segment_3d(loc, v0, v1)
                    d = math.hypot(proj.x - loc.x, proj.y - loc.y)
                    if _priority_adjusted(d, 'NEAREST') < best_d:
                        best_d, best_pt, best_type = (
                            _priority_adjusted(d, 'NEAREST'),
                            Vector((proj.x, proj.y, 0.0)), 'NEAREST',
                        )
                continue

            if stype == 'TANGENT':
                # Requires an active line-creation anchor (P0). Solve
                # |T-C| = R and (T-C).(P0-T) = 0 for each circle, and
                # keep whichever of the (up to two) solutions is
                # closest to the cursor.
                if anchor is None:
                    continue
                for center, radius in snap_cache.get('CIRCLES', []):
                    for t in _tangent_points_from_point(anchor, center, radius):
                        d = math.hypot(t.x - loc.x, t.y - loc.y)
                        if _priority_adjusted(d, 'TANGENT') < best_d:
                            best_d, best_pt, best_type = (
                                _priority_adjusted(d, 'TANGENT'),
                                Vector((t.x, t.y, 0.0)), 'TANGENT',
                            )
                continue

            if stype == 'PARALLEL':
                # Requires an active line-creation anchor. Activates
                # only once the current direction (cursor - anchor) is
                # already close to a reference edge's direction, then
                # snaps exactly onto that direction: snapPoint =
                # anchor + projection of (cursor - anchor) onto Dref.
                if anchor is None:
                    continue
                cur_dir = Vector((loc.x - anchor.x, loc.y - anchor.y))
                cur_len = cur_dir.length
                if cur_len < 1e-9:
                    continue
                cur_dir = cur_dir / cur_len
                # cos(angular tolerance); ~6 degrees
                cos_tol = math.cos(math.radians(6.0))
                for v0, v1 in snap_cache.get('NEAREST_EDGES', []):
                    ref = Vector((v1.x - v0.x, v1.y - v0.y))
                    ref_len = ref.length
                    if ref_len < 1e-9:
                        continue
                    ref = ref / ref_len
                    align = abs(cur_dir.x * ref.x + cur_dir.y * ref.y)
                    if align < cos_tol:
                        continue
                    proj_len = (loc.x - anchor.x) * ref.x + (loc.y - anchor.y) * ref.y
                    px = anchor.x + ref.x * proj_len
                    py = anchor.y + ref.y * proj_len
                    d = math.hypot(px - loc.x, py - loc.y)
                    if _priority_adjusted(d, 'PARALLEL') < best_d:
                        best_d, best_pt, best_type = (
                            _priority_adjusted(d, 'PARALLEL'),
                            Vector((px, py, 0.0)), 'PARALLEL',
                        )
                continue

            if stype == 'PERPENDICULAR':
                # Requires an active line-creation anchor. snapPoint =
                # foot of perpendicular from the anchor onto each
                # reference edge's (infinite) line:
                #   t = ((P0-A).(B-A)) / |B-A|^2 ; foot = A + t(B-A)
                if anchor is None:
                    continue
                for v0, v1 in snap_cache.get('NEAREST_EDGES', []):
                    d_edge = Vector((v1.x - v0.x, v1.y - v0.y))
                    denom = d_edge.x * d_edge.x + d_edge.y * d_edge.y
                    if denom < 1e-12:
                        continue
                    t = ((anchor.x - v0.x) * d_edge.x +
                         (anchor.y - v0.y) * d_edge.y) / denom
                    fx = v0.x + d_edge.x * t
                    fy = v0.y + d_edge.y * t
                    d = math.hypot(fx - loc.x, fy - loc.y)
                    if _priority_adjusted(d, 'PERPENDICULAR') < best_d:
                        best_d, best_pt, best_type = (
                            _priority_adjusted(d, 'PERPENDICULAR'),
                            Vector((fx, fy, 0.0)), 'PERPENDICULAR',
                        )
                continue
            for pt in snap_cache.get(stype, []):
                d = math.hypot(pt.x - loc.x, pt.y - loc.y)
                d_adj = _priority_adjusted(d, stype)
                if d_adj < best_d:
                    best_d, best_pt, best_type = d_adj, Vector((pt.x, pt.y, 0.0)), stype

    if best_pt:
        return best_pt, best_type

    # OBJECT SNAP TRACKING (OTRACK): acquired object snap points project
    # transient horizontal/vertical construction paths. Intersections
    # and guide-aligned cursor positions become temporary snap
    # candidates, checked in screen pixels so behaviour matches the
    # object-snap aperture regardless of zoom level.
    from .otrack import apply_tracking_snap
    track_hit = apply_tracking_snap(context, loc)
    if track_hit is not None:
        return track_hit

    # INCREMENT: snap at multiples of snap_increment from origin
    # (grid-based), or - when an active line-creation anchor exists -
    # also at multiples of snap_angle_increment degrees around that
    # anchor (polar/angular increment), keeping whichever candidate is
    # closer to the cursor.
    if snap_filter and 'INCREMENT' in snap_filter:
        inc = s.snap_increment
        sx  = round(loc.x / inc) * inc
        sy  = round(loc.y / inc) * inc
        grid_pt   = Vector((sx, sy, 0.0))
        grid_dist = math.hypot(grid_pt.x - loc.x, grid_pt.y - loc.y)

        best_inc_pt, best_inc_d = grid_pt, grid_dist

        if anchor is not None:
            dx, dy = loc.x - anchor.x, loc.y - anchor.y
            r = math.hypot(dx, dy)
            if r > 1e-9:
                theta      = math.atan2(dy, dx)
                inc_rad    = math.radians(s.snap_angle_increment)
                theta_snap = round(theta / inc_rad) * inc_rad
                ax = anchor.x + r * math.cos(theta_snap)
                ay = anchor.y + r * math.sin(theta_snap)
                a_dist = math.hypot(ax - loc.x, ay - loc.y)
                if a_dist < best_inc_d:
                    best_inc_pt, best_inc_d = Vector((ax, ay, 0.0)), a_dist

        # Activation condition: distance(cursor, snappedPoint) < tolerance
        if best_inc_d < threshold:
            return best_inc_pt, 'INCREMENT'

    if s.snap_grid or (snap_filter and 'GRID' in snap_filter):
        inc = s.snap_increment
        return (
            Vector((round(loc.x / inc) * inc,
                    round(loc.y / inc) * inc,
                    0.0)),
            'GRID',
        )

    return Vector((loc.x, loc.y, 0.0)), 'FREE'


def find_highlight_candidate(context, mx: float, my: float, loc: Vector, snap_cache: dict,
                              snap_filter=None, anchor=None):
    """
    Wider-radius companion to apply_snaps(), used only to decide
    whether the highlighted snap marker should appear early - as the
    cursor approaches a valid snap point, but before it's actually
    close enough to engage.

    The radius is calibrated in *screen pixels* (cursor_aperture_px *
    snap_highlight_mult), not world units, so it appears at a
    consistent, predictable distance regardless of current zoom level
    or grid/snap_increment size - a fixed world-space radius would
    look enormous zoomed in and invisible zoomed out.

    Returns (point, stype) if something real is within the wider
    highlight radius, or None otherwise. Deliberately never returns
    'FREE'/'GRID'/'INCREMENT' - those aren't "a snap point nearby",
    they're just where the cursor already is.

    IMPORTANT: the returned point is for drawing the marker glyph
    only. The actual working cursor/placement location must keep
    coming from apply_snaps() at its normal (tighter) radius, so the
    point doesn't visually or functionally snap until the cursor is
    genuinely within that smaller radius - only the glyph shows early.
    """
    s = context.scene.cad_settings
    aperture_px = getattr(s, 'cursor_aperture_px', 7) * getattr(s, 'snap_highlight_mult', 2.5)

    # World units per screen pixel near the cursor, so the pixel
    # aperture can be converted into the world-space threshold_mult
    # that apply_snaps()'s search actually operates in. Sampled along
    # both screen axes (not just X) and the larger of the two used -
    # a single-axis sample under-detects candidates that are mostly
    # offset along the other axis whenever the two axes don't scale
    # identically.
    p0 = mouse_to_3d(context, mx, my)
    px = mouse_to_3d(context, mx + 1.0, my)
    py = mouse_to_3d(context, mx, my + 1.0)
    if p0 is None or px is None or py is None:
        return None
    px_scale = max((px - p0).length, (py - p0).length)
    if px_scale < 1e-9:
        return None
    world_radius = aperture_px * px_scale
    increment = max(s.snap_increment, 1e-6)
    mult = world_radius / increment

    pt, stype = apply_snaps(context, loc, snap_cache, snap_filter=snap_filter,
                             anchor=anchor, threshold_mult=mult)
    if stype in ('FREE', 'GRID', 'INCREMENT') or pt is None:
        return None
    return pt, stype


# AutoCAD-style Polar Tracking preset angle sets, in degrees, matching
# the exact five options in the Polar Angle Increment dropdown.
POLAR_TRACKING_PRESETS = {
    '90':   tuple(range(0, 360, 90)),
    '45':   tuple(range(0, 360, 45)),
    '30':   tuple(range(0, 360, 30)),
    '22.5': tuple(i * 22.5 for i in range(16)),
    '15':   tuple(range(0, 360, 15)),
}


def apply_polar_tracking(context, loc: Vector, anchor: Vector):
    """
    AutoCAD-style Polar Tracking: constrains *direction* only, never
    distance - this is angle guidance, not a grid/distance snap.

    Returns (snapped_point, angle_deg) if `loc` (already projected
    onto the active drawing plane) is within the configured tolerance
    of one of the active preset's tracking angles measured from
    `anchor`, or None if it isn't currently close enough to any of
    them (in which case the caller should leave `loc` untouched - the
    tracking guide should only appear/engage near a valid direction,
    exactly like AutoCAD, not constrain the cursor everywhere).

    The original cursor distance from `anchor` is always preserved;
    only the angle changes:
        x = anchor.x + cos(snapped_angle) * distance
        y = anchor.y + sin(snapped_angle) * distance
    """
    s = context.scene.cad_settings
    if not getattr(s, 'polar_tracking_enabled', True):
        return None

    delta = loc - anchor
    distance = delta.length
    if distance < 1e-9:
        return None

    angle = math.degrees(math.atan2(delta.y, delta.x)) % 360.0

    preset = POLAR_TRACKING_PRESETS.get(
        getattr(s, 'polar_angle_increment', '45'),
        POLAR_TRACKING_PRESETS['45'],
    )
    tolerance = getattr(s, 'polar_angle_tolerance', 5.0)

    best_angle, best_diff = None, tolerance
    for a in preset:
        diff = abs((angle - a + 180.0) % 360.0 - 180.0)
        if diff <= best_diff:
            best_angle, best_diff = a, diff

    if best_angle is None:
        return None

    rad = math.radians(best_angle)
    snapped = Vector((
        anchor.x + math.cos(rad) * distance,
        anchor.y + math.sin(rad) * distance,
        anchor.z,
    ))
    return snapped, best_angle


def apply_axis_lock(loc: Vector, anchor: Vector, axis: str) -> Vector:
    """Constrain *loc* to the X or Y axis relative to *anchor*."""
    if axis == 'X':
        return Vector((loc.x, anchor.y, 0.0))
    if axis == 'Y':
        return Vector((anchor.x, loc.y, 0.0))
    return loc


# ════════════════════════════════════════════════════════════════════
#  COORDINATE HELPERS
# ════════════════════════════════════════════════════════════════════

def mouse_to_3d(context, mx: float, my: float) -> Vector:
    """
    Project a 2-D mouse position onto the Z = 0 world plane.
    Returns a Vector(x, y, 0) or the nearest XY approximation.
    """
    region = context.region
    rv3d   = context.region_data
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, (mx, my))
    vdir   = view3d_utils.region_2d_to_vector_3d(region, rv3d, (mx, my))
    if abs(vdir.z) < 1e-7:
        return Vector((origin.x, origin.y, 0.0))
    t = -origin.z / vdir.z
    p = origin + t * vdir
    p.z = 0.0
    return p


def world_to_screen(context, loc):
    """
    Project a world-space location to 2-D region coordinates.
    Returns a 2-tuple (x, y) or None if behind the camera.
    """
    return view3d_utils.location_3d_to_region_2d(
        context.region,
        context.region_data,
        Vector(loc),
    )


# ════════════════════════════════════════════════════════════════════
#  CURSOR / APERTURE HOVER TEST
#
#  Used by the Sketch-Mode idle cursor (CAD_OT_SketchModeManager) to
#  decide whether the mouse is hovering close enough to a cached snap
#  point to show the AutoSnap marker (AutoCAD "Aperture" behaviour).
#  This is intentionally separate from apply_snaps()/build_snap_cache()
#  above so the existing draw-tool snapping logic is left untouched.
# ════════════════════════════════════════════════════════════════════

def find_hover_point(context, mx, my, snap_cache, aperture_px, max_points=500, snap_filter=None):
    """
    Search *snap_cache* for the closest VERTEX / MIDPOINT / CENTER point
    within *aperture_px* pixels of (mx, my).

    *snap_filter* is an optional tuple of stype strings.  When supplied
    only those buckets are searched, matching the active snap_mode filter.
    Pass None for unrestricted search (original behaviour).

    Returns (screen_pos_2d, snap_type) or None.
    """
    best       = None
    best_dist  = float(aperture_px)
    checked    = 0

    stypes = snap_filter if snap_filter is not None else ('VERTEX', 'MIDPOINT', 'CENTER')
    for stype in stypes:
        if stype == 'NEAREST':
            # True nearest point on the geometry relative to the
            # cursor - projected in screen space, clamped to the
            # segment - not a handful of fixed pre-sampled positions.
            for v0, v1 in snap_cache.get('NEAREST_EDGES', []):
                if checked >= max_points:
                    return best
                checked += 1

                s0 = world_to_screen(context, v0)
                s1 = world_to_screen(context, v1)
                if s0 is None or s1 is None:
                    continue

                nx, ny = _nearest_point_on_segment_2d(
                    mx, my, s0[0], s0[1], s1[0], s1[1],
                )
                d = math.hypot(nx - mx, ny - my)
                d_adj = _priority_adjusted(d, 'NEAREST')
                if d_adj <= best_dist:
                    best_dist = d_adj
                    best = ((nx, ny), 'NEAREST')
            continue

        for pt in snap_cache.get(stype, []):
            if checked >= max_points:
                return best
            checked += 1

            p2 = world_to_screen(context, pt)
            if p2 is None:
                continue

            d = math.hypot(p2[0] - mx, p2[1] - my)
            d_adj = _priority_adjusted(d, stype)
            if d_adj <= best_dist:
                best_dist = d_adj
                best = (p2, stype)

    return best


def find_hover_point_world(context, mx: float, my: float, snap_cache: dict,
                            radius_world: float, snap_filter=None):
    """
    Like find_hover_point() above, but the detection radius is a
    literal distance in scene units, measured from the cursor's
    projected 3-D position - not a screen-pixel radius. Use this when
    "within N units" needs to mean the same physical distance
    regardless of the current zoom level (find_hover_point's pixel
    radius shrinks/grows in world terms as you zoom).

    Only searches the simple point buckets (VERTEX, ENDPOINT,
    MIDPOINT, CENTER, INTERSECTION, QUADRANT) - TANGENT, PERPENDICULAR,
    PARALLEL and NEAREST need an active anchor/edge projection and
    aren't meaningful for a passive hover-only search like this one.

    Returns (screen_pos_2d, stype) for the closest candidate within
    radius_world, or None.
    """
    loc = mouse_to_3d(context, mx, my)
    if loc is None:
        return None

    stypes = snap_filter if snap_filter else (
        'VERTEX', 'ENDPOINT', 'MIDPOINT', 'CENTER', 'INTERSECTION', 'QUADRANT',
    )
    best_d, best_pt, best_type = radius_world, None, None
    for stype in stypes:
        for p in snap_cache.get(stype, []):
            d = (p - loc).length
            if d < best_d:
                best_d, best_pt, best_type = d, p, stype

    if best_pt is None:
        return None
    pos_2d = world_to_screen(context, best_pt)
    if pos_2d is None:
        return None
    return pos_2d, best_type

def point_in_polygon(pt, poly):
    """
    Standard ray-casting point-in-polygon test.

    *pt* is a 2D (x, y) screen-space point. *poly* is a list of 2D
    (x, y) screen-space points describing the (implicitly closed)
    lasso-select boundary. Returns True if *pt* lies inside *poly*.
    """
    x, y = pt
    n = len(poly)
    if n < 3:
        return False
    inside = False
    x1, y1 = poly[0]
    for i in range(1, n + 1):
        x2, y2 = poly[i % n]
        if y > min(y1, y2):
            if y <= max(y1, y2):
                if x <= max(x1, x2):
                    if y1 != y2:
                        x_int = (y - y1) * (x2 - x1) / (y2 - y1) + x1
                    if x1 == x2 or x <= x_int:
                        inside = not inside
        x1, y1 = x2, y2
    return inside


def get_active_select_gesture(context):
    """
    Read the toolbar's own active tool (Tweak / Box Select / Circle
    Select / Lasso Select - or a custom tool using one of Blender's
    built-in select-gesture keymaps) and map it to 'BOX', 'CIRCLE', or
    'LASSO'. Falls back to 'BOX' for Tweak or any unrecognised tool, so
    a plain click-drag still does something sensible.

    This intentionally does not maintain any separate/duplicate
    "which select tool" state of our own - the toolbar is the single
    source of truth for which selection gesture is active.
    """
    try:
        tool = context.workspace.tools.from_space_view3d_mode(
            context.mode, create=False
        )
    except Exception:
        tool = None

    if tool is None or not tool.idname:
        return 'BOX'

    idname = tool.idname.lower()
    if 'lasso' in idname:
        return 'LASSO'
    if 'circle' in idname:
        return 'CIRCLE'
    return 'BOX'
