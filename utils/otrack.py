# ─────────────────────────────────────────────────────────────────────
#  utils/otrack.py  –  Object Snap Tracking (OTRACK)
#
#  AutoCAD-style hover-acquired tracking points that project temporary
#  horizontal / vertical / polar guides, whose intersections become
#  extra snap candidates. Nothing here ever creates scene geometry -
#  everything is math + an overlay (see gfx/markers.py:draw_osnap_tracking).
#
#  PUBLIC API (unchanged from the previous version, so tool_line.py /
#  tool_circle.py / tool_rect.py / tool_move.py / tool_slice.py /
#  tool_edit_move.py and gfx/markers.py do not need to change):
#
#      SnapAcquisitionManager, update_tracking,
#      apply_tracking_snap, ConstraintInferenceEngine
#
#  New, additive, used internally and by markers.py if it wants richer
#  drawing later:
#
#      TrackingPoint, Guide, IntersectionSolver, TrackingManager
# ─────────────────────────────────────────────────────────────────────

import math
import time
from dataclasses import dataclass, field
from typing import Optional, List, Tuple

from mathutils import Vector

from .. import state as _st


DWELL_SECONDS = 0.45
MAX_TRACKED_POINTS = 8
SNAP_TYPE = 'TRACKING'

# Polar tracking angles, in degrees, measured from +X, CCW - matches
# the angle convention used elsewhere in math_utils (world_ang_to_display).
DEFAULT_POLAR_ANGLES = (0.0, 30.0, 45.0, 60.0, 90.0, 120.0, 135.0, 150.0,
                         180.0, 210.0, 225.0, 240.0, 270.0, 300.0, 315.0, 330.0)
POLAR_ANGLE_TOLERANCE_DEG = 2.0

# How close (in degrees) the cursor must be to true horizontal/
# vertical relative to an acquired point before a guide appears at
# all - outside this band there is no guide, matching AutoCAD (the
# alignment path doesn't follow the cursor at arbitrary angles).
AXIS_TOLERANCE_DEG = 8.0
# Once locked, the release band is wider than the acquire band so a
# guide doesn't flicker on/off right at the boundary.
AXIS_RELEASE_FACTOR = 1.6

# Guide/guide intersections get a *wider* snap aperture than a lone
# guide point - the whole point of OTRACK is to land exactly where two
# guides cross, so that should be the easiest thing to acquire, not
# the hardest. Once locked onto a specific intersection, an even wider
# release band keeps it locked through small cursor jitter so the snap
# doesn't flicker in and out right at the boundary.
INTERSECTION_APERTURE_MULT = 1.8
INTERSECTION_RELEASE_MULT  = 2.6



# Shared across every TrackingManager instance on purpose: both the
# overlay draw call and apply_tracking_snap() construct a fresh
# TrackingManager every frame (they don't share an object), but the
# axis lock has to persist frame-to-frame or the hysteresis in
# active_guides() does nothing. Cleared by clear_tracking().
_GLOBAL_AXIS_LOCK: dict = {}

# Same idea, for which specific guide/guide intersection is currently
# "stuck" to the cursor (see constrain()'s sticky-lock logic). Kept
# separate from _GLOBAL_AXIS_LOCK since the two hysteresis systems
# track different things and can be active independently.
_GLOBAL_INTERSECTION_LOCK: dict = {}


# ═══════════════════════════════════════════════════════════════════
#  TRACKING POINT
# ═══════════════════════════════════════════════════════════════════

def resolve_typed_distance_ray(otrack_mgr, raw_world: Vector):
    """
    The single shared implementation of "what ray should a typed
    distance be measured along, given the current OTRACK state" -
    used identically by the Line, Circle, and Rectangle tools so none
    of them have their own copy of this logic.

    Returns (origin, direction) - the active OTRACK guide's acquired
    point and its locked axis direction, sign-corrected toward
    whichever side of the point the cursor is currently on - or None
    if there's no acquired point, or more than one guide is active at
    once (near an intersection of two acquired points, where "which
    axis did you mean" is ambiguous and plain click-to-place is used
    instead).
    """
    if not otrack_mgr.points:
        return None
    guides = otrack_mgr.active_guides(raw_world)
    if len(guides) != 1:
        return None
    origin, direction = guides[0].as_ray()
    if (raw_world - origin).dot(direction) < 0:
        direction = -direction
    return origin, direction


@dataclass
class TrackingPoint:
    """A single hover-acquired OTRACK reference point.

    Purely a data holder - never touches bpy.data, never becomes a
    scene object. `key` is the rounded (x, y, z) tuple used for
    equality/dedup against state.selection.tracked_points, which is
    still stored as plain tuples for backward compatibility with the
    rest of the add-on and for easy PropertyGroup-free persistence.
    """
    position: Vector
    snap_type: str = 'UNKNOWN'
    source_object: Optional[str] = None
    source_element: Optional[tuple] = None
    acquired_time: float = field(default_factory=time.time)
    active: bool = True

    @property
    def key(self) -> tuple:
        return tuple(round(c, 6) for c in self.position)

    @classmethod
    def from_key(cls, key: tuple, snap_type: str = SNAP_TYPE) -> "TrackingPoint":
        return cls(position=Vector(key), snap_type=snap_type)


# ═══════════════════════════════════════════════════════════════════
#  GUIDES
# ═══════════════════════════════════════════════════════════════════

class Guide:
    """A temporary, purely-mathematical construction line through one
    tracking point. Never rendered as geometry - see markers.py for
    the GPU overlay that visualises these.
    """
    KIND_H = 'HORIZONTAL'
    KIND_V = 'VERTICAL'
    KIND_POLAR = 'POLAR'

    __slots__ = ('kind', 'origin', 'direction', 'source')

    def __init__(self, kind: str, origin: Vector, direction: Optional[Vector] = None,
                 source: Optional[TrackingPoint] = None):
        self.kind = kind
        self.origin = origin
        # direction is unit-length for POLAR guides; unused for H/V.
        self.direction = direction
        self.source = source

    def as_ray(self) -> Tuple[Vector, Vector]:
        """Return (origin, direction) as an infinite ray/line for the
        intersection solver, regardless of guide kind."""
        if self.kind == Guide.KIND_H:
            return self.origin, Vector((1.0, 0.0, 0.0))
        if self.kind == Guide.KIND_V:
            return self.origin, Vector((0.0, 1.0, 0.0))
        return self.origin, self.direction

    @staticmethod
    def horizontal(pt: TrackingPoint) -> "Guide":
        return Guide(Guide.KIND_H, pt.position, source=pt)

    @staticmethod
    def vertical(pt: TrackingPoint) -> "Guide":
        return Guide(Guide.KIND_V, pt.position, source=pt)

    @staticmethod
    def polar(pt: TrackingPoint, angle_deg: float) -> "Guide":
        rad = math.radians(angle_deg)
        direction = Vector((math.cos(rad), math.sin(rad), 0.0))
        return Guide(Guide.KIND_POLAR, pt.position, direction, source=pt)


# ═══════════════════════════════════════════════════════════════════
#  INTERSECTION SOLVER
# ═══════════════════════════════════════════════════════════════════

class IntersectionSolver:
    """Line-line intersection for OTRACK guides (all infinite lines in
    the XY plane - CAD drawing is 2D-on-a-plane here, matching the
    rest of the snap system in math_utils.py).
    """

    @staticmethod
    def line_line(g1: Guide, g2: Guide) -> Optional[Vector]:
        """Return the intersection point of two guides, or None if the
        guides are (numerically) parallel."""
        o1, d1 = g1.as_ray()
        o2, d2 = g2.as_ray()

        # Fast paths for the common H/V cases avoid the general 2x2
        # solve and its associated float error entirely.
        if g1.kind == Guide.KIND_H and g2.kind == Guide.KIND_V:
            return Vector((o2.x, o1.y, 0.0))
        if g1.kind == Guide.KIND_V and g2.kind == Guide.KIND_H:
            return Vector((o1.x, o2.y, 0.0))
        if g1.kind == Guide.KIND_H and g2.kind == Guide.KIND_H:
            return None  # parallel
        if g1.kind == Guide.KIND_V and g2.kind == Guide.KIND_V:
            return None  # parallel

        # General case: solve o1 + t*d1 = o2 + s*d2 for t (2x2 linear system).
        denom = d1.x * d2.y - d1.y * d2.x
        if abs(denom) < 1e-9:
            return None  # parallel (or same direction)
        dx, dy = o2.x - o1.x, o2.y - o1.y
        t = (dx * d2.y - dy * d2.x) / denom
        return Vector((o1.x + d1.x * t, o1.y + d1.y * t, 0.0))

    @staticmethod
    def all_intersections(guides: List[Guide]) -> List[Vector]:
        """All pairwise intersections among a guide list, deduplicated.
        Intentionally O(n^2) over *guides* (not over tracking points *
        candidate positions) - n stays small because MAX_TRACKED_POINTS
        bounds it, and each point contributes at most 2-3 guides."""
        out: List[Vector] = []
        seen = set()
        n = len(guides)
        for i in range(n):
            for j in range(i + 1, n):
                pt = IntersectionSolver.line_line(guides[i], guides[j])
                if pt is None:
                    continue
                key = (round(pt.x, 6), round(pt.y, 6), 0.0)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Vector(key))
        return out


# ═══════════════════════════════════════════════════════════════════
#  TRACKING MANAGER  (hover acquisition + guide/candidate generation)
# ═══════════════════════════════════════════════════════════════════

class TrackingManager:
    """Owns the set of acquired TrackingPoints for the lifetime of one
    modal drawing command, generates their guides, and produces
    candidate intersection points. This is the new internal engine;
    SnapAcquisitionManager below is kept as a thin compatibility
    wrapper so existing operator code doesn't need to change yet.
    """

    def __init__(self, dwell_seconds: float = DWELL_SECONDS,
                 polar_angles: Tuple[float, ...] = DEFAULT_POLAR_ANGLES,
                 enable_h: bool = True, enable_v: bool = True,
                 enable_polar: bool = False):
        self.dwell_seconds = dwell_seconds
        self.polar_angles = polar_angles
        self.enable_h = enable_h
        self.enable_v = enable_v
        self.enable_polar = enable_polar

        self.points: List[TrackingPoint] = []
        self._hover_key: Optional[tuple] = None
        self._hover_since: float = 0.0
        self._axis_lock: dict = _GLOBAL_AXIS_LOCK  # shared, see module docstring above

    # -- acquisition ---------------------------------------------------
    def reset(self, clear_points: bool = True):
        self._hover_key = None
        self._hover_since = 0.0
        if clear_points:
            self.points = []
            _GLOBAL_AXIS_LOCK.clear()
            _st.selection.tracked_points = []

    def update(self, snapped_world: Optional[Vector], snap_type: str) -> bool:
        """Advance hover-dwell acquisition. Returns True if a new point
        was just acquired this call."""
        key = _tracking_key(snapped_world, snap_type)
        now = time.time()

        if key != self._hover_key:
            self._hover_key = key
            self._hover_since = now
            return False

        if key is None or now - self._hover_since < self.dwell_seconds:
            return False

        if any(p.key == key for p in self.points):
            return False

        self.points.append(TrackingPoint(position=Vector(key), snap_type=snap_type))
        if len(self.points) > MAX_TRACKED_POINTS:
            del self.points[:-MAX_TRACKED_POINTS]

        # Mirror into state for the legacy overlay / other read sites.
        _st.selection.tracked_points = [p.key for p in self.points]
        return True

    # -- guides ----------------------------------------------------------
    def active_guides(self, cursor_world: Vector) -> List[Guide]:
        """The guide (if any) active for each acquired point right now.

        Unlike a forced "always pick the closer axis", a guide only
        exists once the cursor is genuinely within AXIS_TOLERANCE_DEG
        of true horizontal or true vertical relative to the point -
        outside that band, no guide is shown at all, matching
        AutoCAD's OTRACK (the alignment path only appears near the
        axis, it doesn't follow the cursor everywhere).

        A small hysteresis (a wider release band than acquire band)
        keeps a guide that's already locked from flickering off right
        at the tolerance boundary.
        """
        out: List[Guide] = []
        for pt in self.points:
            if not pt.active:
                continue

            dx = cursor_world.x - pt.position.x
            dy = cursor_world.y - pt.position.y
            adx, ady = abs(dx), abs(dy)
            if adx < 1e-6 and ady < 1e-6:
                continue
            key = pt.key
            locked = self._axis_lock.get(key)

            acquire_tan = math.tan(math.radians(AXIS_TOLERANCE_DEG))
            release_tan = math.tan(math.radians(AXIS_TOLERANCE_DEG * AXIS_RELEASE_FACTOR))

            is_h_acquire = ady <= adx * acquire_tan
            is_v_acquire = adx <= ady * acquire_tan
            is_h_release = ady <= adx * release_tan
            is_v_release = adx <= ady * release_tan

            if not (self.enable_h and self.enable_v):
                if self.enable_h and is_h_acquire:
                    out.append(Guide.horizontal(pt))
                elif self.enable_v and is_v_acquire:
                    out.append(Guide.vertical(pt))
                self._axis_lock[key] = None
                continue

            chosen = None
            if locked == 'H' and is_h_release:
                chosen = 'H'
            elif locked == 'V' and is_v_release:
                chosen = 'V'
            elif is_h_acquire:
                chosen = 'H'
            elif is_v_acquire:
                chosen = 'V'

            self._axis_lock[key] = chosen
            if chosen == 'H':
                out.append(Guide.horizontal(pt))
            elif chosen == 'V':
                out.append(Guide.vertical(pt))
        return out

    def guides(self, cursor_world: Optional[Vector] = None) -> List[Guide]:
        """All guides projected by every active tracking point."""
        out: List[Guide] = []
        for pt in self.points:
            if not pt.active:
                continue
            if self.enable_h:
                out.append(Guide.horizontal(pt))
            if self.enable_v:
                out.append(Guide.vertical(pt))
            if self.enable_polar:
                for angle in self.polar_angles:
                    out.append(Guide.polar(pt, angle))
        return out

    def constrain(self, cursor_world: Vector, cursor_2d, world_to_screen_fn,
                  aperture_px: float, extra_guides: Optional[List["Guide"]] = None,
                  intersection_only: bool = False) -> Optional[Vector]:
        """The OTRACK snap result for this frame, or None if the
        cursor isn't within `aperture_px` of anything OTRACK offers.

        Per spec: OTRACK never grabs the cursor from arbitrary
        distance - a guide is only a candidate once the cursor is
        within the snap radius of it (or of a guide/guide
        intersection). Intersections take priority over a lone guide's
        own alignment point when both are in range, since a specific
        point is more likely what the user means than "somewhere along
        this line".

        Intersections get a *wider* aperture than a lone guide point
        (INTERSECTION_APERTURE_MULT) - the whole point of tracking two
        guides is to land exactly on where they cross, so that should
        be the easiest thing to acquire, not the hardest. Once locked
        onto a specific intersection, it stays preferred within a
        wider still release radius (INTERSECTION_RELEASE_MULT) so tiny
        cursor jitter right at the boundary doesn't flicker the snap
        on and off.

        *extra_guides*, if given, are additional construction rays
        (e.g. the Line tool's own live Polar Tracking ray) that get
        tested for intersections against every OTRACK guide, without
        becoming lone-guide snap candidates themselves - letting the
        two systems' guides cross-snap against each other.
        """
        guides = self.active_guides(cursor_world)
        all_guides = guides + list(extra_guides) if extra_guides else guides
        if not all_guides or (extra_guides and not guides):
            _GLOBAL_INTERSECTION_LOCK.pop('point', None)
            if not guides:
                return None

        all_ipts = IntersectionSolver.all_intersections(all_guides)
        intersection_aperture = aperture_px * INTERSECTION_APERTURE_MULT
        release_aperture       = aperture_px * INTERSECTION_RELEASE_MULT

        best_pt, best_d, best_is_intersection = None, None, False

        # Sticky lock: prefer the intersection we were snapped to last
        # frame while the cursor is still within the wider release
        # radius of it, instead of re-searching from scratch - this is
        # what prevents flicker right at the acquire boundary.
        locked = _GLOBAL_INTERSECTION_LOCK.get('point')
        if locked is not None and any(
            (ipt - locked).length < 1e-4 for ipt in all_ipts
        ):
            p2 = world_to_screen_fn(locked)
            if p2 is not None:
                d = math.hypot(p2[0] - cursor_2d[0], p2[1] - cursor_2d[1])
                if d <= release_aperture:
                    best_pt, best_d, best_is_intersection = locked, d, True

        if not best_is_intersection:
            for ipt in all_ipts:
                p2 = world_to_screen_fn(ipt)
                if p2 is None:
                    continue
                d = math.hypot(p2[0] - cursor_2d[0], p2[1] - cursor_2d[1])
                if d <= intersection_aperture and (best_d is None or d < best_d):
                    best_pt, best_d, best_is_intersection = ipt, d, True

        if best_is_intersection:
            _GLOBAL_INTERSECTION_LOCK['point'] = best_pt
            return best_pt
        _GLOBAL_INTERSECTION_LOCK.pop('point', None)

        if intersection_only:
            return None

        for g in guides:
            origin, direction = g.as_ray()
            t = (cursor_world - origin).dot(direction)
            foot = origin + direction * t
            p2 = world_to_screen_fn(foot)
            if p2 is None:
                continue
            d = math.hypot(p2[0] - cursor_2d[0], p2[1] - cursor_2d[1])
            if d <= aperture_px and (best_d is None or d < best_d):
                best_pt, best_d = foot, d

        return best_pt

    def candidate_points(self, cursor_world: Vector) -> List[Vector]:
        """Virtual snap candidates for the current cursor position:
        every pairwise intersection among the *active* guides (one
        H-or-V ray per point, plus any live polar ray), and each active
        ray's own closest point to the cursor (so a single acquired
        point with no second point to intersect against still lets you
        snap anywhere along its active axis, not only at intersections).
        """
        guides = self.active_guides(cursor_world)
        out = IntersectionSolver.all_intersections(guides)

        for g in guides:
            origin, direction = g.as_ray()
            to_cursor = cursor_world - origin
            t = to_cursor.dot(direction)
            out.append(origin + direction * t)

        return out


# ═══════════════════════════════════════════════════════════════════
#  BACKWARD-COMPATIBLE WRAPPER  (existing operator files use this)
# ═══════════════════════════════════════════════════════════════════

class SnapAcquisitionManager:
    """Manages hover-dwell acquisition of temporary OTRACK references.

    Thin wrapper around TrackingManager kept so tool_line.py,
    tool_circle.py, tool_rect.py, tool_move.py, tool_slice.py and
    tool_edit_move.py keep working unmodified. New code should prefer
    TrackingManager directly.
    """

    def __init__(self, dwell_seconds=DWELL_SECONDS):
        self._mgr = TrackingManager(dwell_seconds=dwell_seconds)

    @property
    def dwell_seconds(self):
        return self._mgr.dwell_seconds

    @property
    def hover_key(self):
        return self._mgr._hover_key

    @property
    def hover_since(self):
        return self._mgr._hover_since

    def reset(self, clear_points=True):
        self._mgr.reset(clear_points=clear_points)

    def update(self, snapped_world, snap_type):
        return self._mgr.update(snapped_world, snap_type)

    @property
    def points(self):
        return self._mgr.points

    def active_guides(self, cursor_world):
        """The currently active OTRACK guide(s) (0, 1, or 2 - the 2
        case being an active intersection) for `cursor_world`, one per
        acquired point. Used by tools to lock typed-distance entry to
        the same axis the visual guide is locked to."""
        return self._mgr.active_guides(cursor_world)


def clear_tracking():
    _st.selection.tracked_points = []
    _GLOBAL_AXIS_LOCK.clear()
    _GLOBAL_INTERSECTION_LOCK.clear()


def update_tracking(manager, snapped_world, snap_type):
    if manager is None:
        return False
    return manager.update(snapped_world, snap_type)


def _build_tracking_manager(context, tracked):
    """Shared setup for apply_tracking_snap() /
    apply_tracking_intersection_snap() / snap_extra_ray_to_tracking() -
    a fresh TrackingManager configured from the scene's OTRACK
    settings, loaded with the currently acquired tracking points."""
    settings = getattr(context.scene, 'cad_settings', None)
    enable_polar = getattr(settings, 'otrack_enable_polar', False) if settings else False
    polar_angles = _resolve_polar_angles(settings)
    mgr = TrackingManager(
        enable_h=getattr(settings, 'otrack_enable_h', True) if settings else True,
        enable_v=getattr(settings, 'otrack_enable_v', True) if settings else True,
        enable_polar=enable_polar,
        polar_angles=polar_angles,
    )
    mgr.points = [TrackingPoint.from_key(tuple(p)) for p in tracked]
    return mgr, settings


def apply_tracking_snap(context, loc, threshold_px=None):
    """Return (point, SNAP_TYPE) when loc is near an inferred OTRACK
    point (H/V/polar guide intersection, or a single-guide alignment
    through the cursor)."""
    from .math_utils import world_to_screen

    tracked = [Vector(p) for p in _st.selection.tracked_points]
    if not tracked:
        return None

    settings = getattr(context.scene, 'cad_settings', None)
    if threshold_px is None:
        threshold_px = getattr(settings, 'cursor_aperture_px', 7) if settings else 7
    threshold_px = max(4.0, float(threshold_px))

    cursor_2d = world_to_screen(context, loc)
    if cursor_2d is None:
        return None

    mgr, _ = _build_tracking_manager(context, tracked)

    result = mgr.constrain(loc, cursor_2d, lambda p: world_to_screen(context, p), threshold_px)
    if result is None:
        return None
    return result, SNAP_TYPE


def apply_tracking_intersection_snap(context, loc, threshold_px=None):
    """OTRACK guide/guide-intersection snap only (no lone-guide
    alignment fallback) - used to give a genuine intersection priority
    over nearby geometry, since landing exactly on where two tracking
    guides cross is a deliberate, specific target that shouldn't have
    to compete with whatever ordinary vertex happens to be nearby.
    Returns (point, SNAP_TYPE) or None."""
    from .math_utils import world_to_screen

    tracked = [Vector(p) for p in _st.selection.tracked_points]
    if not tracked:
        return None

    settings = getattr(context.scene, 'cad_settings', None)
    if threshold_px is None:
        threshold_px = getattr(settings, 'cursor_aperture_px', 7) if settings else 7
    threshold_px = max(4.0, float(threshold_px))

    cursor_2d = world_to_screen(context, loc)
    if cursor_2d is None:
        return None

    mgr, _ = _build_tracking_manager(context, tracked)

    result = mgr.constrain(
        loc, cursor_2d, lambda p: world_to_screen(context, p),
        threshold_px, intersection_only=True,
    )
    if result is None:
        return None
    return result, SNAP_TYPE


def snap_extra_ray_to_tracking(context, loc, ray_origin: Vector, ray_direction: Vector,
                                threshold_px=None):
    """Cross-system intersection snap: test an *external* construction
    ray (e.g. the Line tool's own live Polar Tracking guide) against
    every active OTRACK guide for an intersection near the cursor, so
    the two systems' guide lines can snap against each other exactly
    like two OTRACK guides do. Same widened aperture + sticky-lock
    hysteresis as ordinary OTRACK intersections. Returns the
    intersection point (Vector) or None - deliberately not the
    (point, SNAP_TYPE) tuple shape of the other two helpers, since
    callers decide their own snap_type for this case."""
    from .math_utils import world_to_screen

    tracked = [Vector(p) for p in _st.selection.tracked_points]
    if not tracked:
        return None
    if ray_direction.length < 1e-9:
        return None

    if threshold_px is None:
        settings = getattr(context.scene, 'cad_settings', None)
        threshold_px = getattr(settings, 'cursor_aperture_px', 7) if settings else 7
    threshold_px = max(4.0, float(threshold_px))

    cursor_2d = world_to_screen(context, loc)
    if cursor_2d is None:
        return None

    mgr, _ = _build_tracking_manager(context, tracked)
    extra = [Guide(Guide.KIND_POLAR, ray_origin, ray_direction.normalized())]

    return mgr.constrain(
        loc, cursor_2d, lambda p: world_to_screen(context, p),
        threshold_px, extra_guides=extra, intersection_only=True,
    )


def _resolve_polar_angles(settings) -> Tuple[float, ...]:
    if settings is None:
        return DEFAULT_POLAR_ANGLES
    inc = getattr(settings, 'otrack_polar_increment', None)
    if not inc:
        return DEFAULT_POLAR_ANGLES
    n = int(round(360.0 / inc))
    return tuple((i * inc) % 360.0 for i in range(n))


class TrackingEngine:
    """Kept for backward compatibility with any external callers;
    delegates to Guide/IntersectionSolver. Horizontal/vertical only -
    see TrackingManager.candidate_points for the full H/V/polar set."""

    @staticmethod
    def guide_snap_points(tracked, loc):
        out = []
        for pt in tracked:
            out.append(Vector((pt.x, loc.y, 0.0)))
            out.append(Vector((loc.x, pt.y, 0.0)))
        return out


class ConstraintInferenceEngine:
    """Kept for backward compatibility (gfx/markers.py imports this
    directly for the current H/V-only overlay). Delegates to the new
    IntersectionSolver so behaviour matches TrackingManager exactly."""

    @staticmethod
    def intersections(tracked):
        guides: List[Guide] = []
        for key in tracked:
            pt = TrackingPoint.from_key(tuple(key))
            guides.append(Guide.horizontal(pt))
            guides.append(Guide.vertical(pt))
        return IntersectionSolver.all_intersections(guides)


def _tracking_key(snapped_world, snap_type):
    if snap_type in ('FREE', 'GRID', 'INCREMENT', SNAP_TYPE):
        return None
    if snapped_world is None:
        return None
    return tuple(round(c, 6) for c in snapped_world)
