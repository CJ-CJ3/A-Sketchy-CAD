# ─────────────────────────────────────────────────────────────────────
#  ui/cad_tpanel_tools.py
#
#  T-panel buttons for CAD tools: Line · Circle · Rectangle
#
#  Icons:  Blender toolbar icons are binary .dat geometry files that
#  must live in  <blender>/datafiles/icons/.  We generate the three
#  .dat files in Python at register() time and copy them there.
#
#  ToolDef icon= is set to the icon *name* (filename without .dat),
#  which is what bpy.utils.system_resource looks up.
#
#  Tool buttons are injected by show() / hide() so they only appear
#  while CAD Sketch Mode is active.
# ─────────────────────────────────────────────────────────────────────

import bpy
import os
import math
from bpy.utils.toolsystem import ToolDef

# ── stable idnames ────────────────────────────────────────────────────
_IDNAME_LINE    = "cad_addon.line_tool"
_IDNAME_CIRCLE  = "cad_addon.circle_tool"
_IDNAME_ELLIPSE = "cad_addon.ellipse_tool"
_IDNAME_POINT   = "cad_addon.point_tool"
_IDNAME_ARC     = "cad_addon.arc_tool"
_IDNAME_SPLINE  = "cad_addon.spline_tool"
_IDNAME_FILLET  = "cad_addon.fillet_tool"
_IDNAME_CHAMFER = "cad_addon.chamfer_tool"
_IDNAME_EXTEND  = "cad_addon.extend_tool"
_IDNAME_RECT    = "cad_addon.rect_tool"
_IDNAME_MOVE    = "cad_addon.move_tool"
_IDNAME_SCALE   = "cad_addon.scale_tool"
_IDNAME_ARRAY   = "cad_addon.array_tool"
_IDNAME_STRETCH = "cad_addon.stretch_tool"
_IDNAME_ROTATE  = "cad_addon.rotate_tool"
_IDNAME_COPY    = "cad_addon.copy_tool"
_IDNAME_MIRROR  = "cad_addon.mirror_tool"
_IDNAME_TRIM    = "cad_addon.trim_tool"
_IDNAME_OFFSET  = "cad_addon.offset_tool"
_IDNAME_MEASURE = "cad_addon.measure_tool"

_KM_LINE    = "CAD Line Tool"
_KM_CIRCLE  = "CAD Circle Tool"
_KM_ELLIPSE = "CAD Ellipse Tool"
_KM_POINT   = "CAD Point Tool"
_KM_ARC     = "CAD Arc Tool"
_KM_SPLINE  = "CAD Spline Tool"
_KM_FILLET  = "CAD Fillet Tool"
_KM_CHAMFER = "CAD Chamfer Tool"
_KM_EXTEND  = "CAD Extend Tool"
_KM_RECT    = "CAD Rect Tool"
_KM_MOVE    = "CAD Move Tool"
_KM_SCALE   = "CAD Scale Tool"
_KM_ARRAY   = "CAD Array Tool"
_KM_STRETCH = "CAD Stretch Tool"
_KM_ROTATE  = "CAD Rotate Tool"
_KM_COPY    = "CAD Copy Tool"
_KM_MIRROR  = "CAD Mirror Tool"
_KM_TRIM    = "CAD Trim Tool"
_KM_OFFSET  = "CAD Offset Tool"
_KM_MEASURE = "CAD Measure Tool"

# Icon names (= .dat filename without extension)
_ICON_LINE    = "ops.cad_addon.line"
_ICON_CIRCLE  = "ops.cad_addon.circle"
_ICON_ELLIPSE = "ops.cad_addon.ellipse"
_ICON_POINT   = "ops.cad_addon.point"
_ICON_ARC     = "ops.cad_addon.arc"
_ICON_SPLINE  = "ops.cad_addon.spline"
_ICON_FILLET  = "ops.cad_addon.fillet"
_ICON_CHAMFER = "ops.cad_addon.chamfer"
_ICON_EXTEND  = "ops.cad_addon.extend"
_ICON_RECT    = "ops.cad_addon.rect"
_ICON_MOVE    = "ops.cad_addon.move"
_ICON_SCALE   = "ops.cad_addon.scale"
_ICON_ARRAY   = "ops.cad_addon.array"
_ICON_STRETCH = "ops.cad_addon.stretch"
_ICON_COPY    = "ops.cad_addon.copy"
_ICON_MIRROR  = "ops.cad_addon.mirror"
_ICON_OFFSET  = "ops.cad_addon.offset"

_addon_keymaps: list = []
_tools_visible: bool = False


# ─────────────────────────────────────────────────────────────────────
# .dat geometry icon generator
#
# Format confirmed from mesh_snap_utilities_line (the working reference):
#   Bytes 0-3:  b'VCO\x00'       magic identifier
#   Byte  4:    coords_range_x   (= 255, full 0-255 coordinate space)
#   Byte  5:    coords_range_y   (= 255, full 0-255 coordinate space)
#   Bytes 6-7:  start_x, start_y (= 0, 0  unused)
#   n_tris x 6 bytes:  XY coords  (x1,y1,x2,y2,x3,y3 uint8, one row per tri)
#   n_tris x 12 bytes: RGBA color (r,g,b,a per vertex x 3 verts, one row per tri)
#
#   ALL XY data comes first, then ALL color data.
#   Y=0 is the BOTTOM of the icon (OpenGL convention).
# ─────────────────────────────────────────────────────────────────────

_MR, _MG, _MB, _MA = 93, 255, 160, 255  # mint green, fully opaque


def _make_dat(triangles) -> bytes:
    """
    Build a VCO .dat icon binary from a list of triangles.
    triangles: list of 3-element lists, each element ((x, y), (r, g, b, a))
    x, y: integer 0-255, Y=0 is bottom of icon.
    ALL XY written first, then ALL RGBA — matching the VCO spec exactly.
    """
    coords = bytearray()
    colors = bytearray()
    for tri in triangles:
        for (x, y), (r, g, b, a) in tri:
            coords += bytes([max(0, min(255, int(round(x)))),
                             max(0, min(255, int(round(y))))])
            colors += bytes([r & 0xFF, g & 0xFF, b & 0xFF, a & 0xFF])
    # Header: b'VCO\x00' + coords_range_x + coords_range_y + start_x + start_y
    header = b'VCO\x00' + bytes([255, 255, 0, 0])
    return header + bytes(coords) + bytes(colors)


def _col(r=_MR, g=_MG, b=_MB, a=_MA):
    return (r, g, b, a)


def _rect_tris(x0, y0, x1, y1, c=None):
    """Filled rectangle as two triangles."""
    c = c or _col()
    return [
        [((x0, y0), c), ((x1, y0), c), ((x1, y1), c)],
        [((x0, y0), c), ((x1, y1), c), ((x0, y1), c)],
    ]


def _thick_line_tris(x0, y0, x1, y1, w, c=None):
    """Thick line as a rotated rectangle (2 triangles)."""
    c = c or _col()
    dx, dy = x1 - x0, y1 - y0
    length = math.sqrt(dx * dx + dy * dy)
    if length < 1:
        return []
    nx, ny = -dy / length * w / 2, dx / length * w / 2
    A = (x0 - nx, y0 - ny)
    B = (x0 + nx, y0 + ny)
    C = (x1 + nx, y1 + ny)
    D = (x1 - nx, y1 - ny)
    return [
        [(A, c), (B, c), (C, c)],
        [(A, c), (C, c), (D, c)],
    ]


def _circle_tris(cx, cy, r, thickness, steps=64, c=None):
    """Circle ring as a strip of thin quads."""
    c = c or _col()
    tris = []
    r_out = r + thickness / 2
    r_in  = r - thickness / 2
    for i in range(steps):
        a0 = 2 * math.pi * i       / steps
        a1 = 2 * math.pi * (i + 1) / steps
        ox0, oy0 = cx + math.cos(a0) * r_out, cy + math.sin(a0) * r_out
        ox1, oy1 = cx + math.cos(a1) * r_out, cy + math.sin(a1) * r_out
        ix0, iy0 = cx + math.cos(a0) * r_in,  cy + math.sin(a0) * r_in
        ix1, iy1 = cx + math.cos(a1) * r_in,  cy + math.sin(a1) * r_in
        tris.append([((ox0, oy0), c), ((ox1, oy1), c), ((ix1, iy1), c)])
        tris.append([((ox0, oy0), c), ((ix1, iy1), c), ((ix0, iy0), c)])
    return tris


def _ellipse_tris(cx, cy, rx, ry, thickness, steps=64, c=None):
    """Elliptical ring as a strip of thin quads - same construction as
    _circle_tris but with independent X/Y radii, so the icon reads as
    a squashed circle (an ellipse) rather than a perfect circle."""
    c = c or _col()
    tris = []
    for i in range(steps):
        a0 = 2 * math.pi * i       / steps
        a1 = 2 * math.pi * (i + 1) / steps
        cos0, sin0 = math.cos(a0), math.sin(a0)
        cos1, sin1 = math.cos(a1), math.sin(a1)
        # Outer/inner offset scaled per-axis so the ring thickness
        # stays visually even all the way round the ellipse.
        ox0, oy0 = cx + cos0 * (rx + thickness / 2), cy + sin0 * (ry + thickness / 2)
        ox1, oy1 = cx + cos1 * (rx + thickness / 2), cy + sin1 * (ry + thickness / 2)
        ix0, iy0 = cx + cos0 * (rx - thickness / 2), cy + sin0 * (ry - thickness / 2)
        ix1, iy1 = cx + cos1 * (rx - thickness / 2), cy + sin1 * (ry - thickness / 2)
        tris.append([((ox0, oy0), c), ((ox1, oy1), c), ((ix1, iy1), c)])
        tris.append([((ox0, oy0), c), ((ix1, iy1), c), ((ix0, iy0), c)])
    return tris


# ── LINE: diagonal stroke with filled circular dot at each endpoint ───

def _dot_tris(cx, cy, r, steps=20, c=None):
    """Filled circle as a fan of triangles."""
    c = c or _col()
    tris = []
    for i in range(steps):
        a0 = 2 * math.pi * i       / steps
        a1 = 2 * math.pi * (i + 1) / steps
        tris.append([
            ((cx,              cy),              c),
            ((cx + math.cos(a0) * r, cy + math.sin(a0) * r), c),
            ((cx + math.cos(a1) * r, cy + math.sin(a1) * r), c),
        ])
    return tris

_WHITE = _col(230, 230, 230, 255)   # light grey for endpoint dots

def _build_line_dat() -> bytes:
    tris = []
    tris += _thick_line_tris(42, 42, 213, 213, w=18)          # main stroke (green)
    tris += _dot_tris(42,  42,  16, c=_WHITE)                 # bottom-left dot (white)
    tris += _dot_tris(213, 213, 16, c=_WHITE)                 # top-right dot (white)
    return _make_dat(tris)


# ── CIRCLE: ring + plus sign in the centre ───────────────────────────

def _plus_tris(cx, cy, arm, thick, c=None):
    """Plus sign centred at (cx, cy)."""
    c = c or _col()
    tris = []
    # Horizontal bar
    tris += _rect_tris(cx - arm, cy - thick / 2, cx + arm, cy + thick / 2, c)
    # Vertical bar
    tris += _rect_tris(cx - thick / 2, cy - arm, cx + thick / 2, cy + arm, c)
    return tris

def _build_circle_dat() -> bytes:
    tris = []
    tris += _circle_tris(128, 128, r=88, thickness=16, steps=64)   # ring (green)
    tris += _plus_tris(128, 128, arm=34, thick=14, c=_WHITE)       # plus (white)
    return _make_dat(tris)


# ── ELLIPSE: squashed ring + plus sign in the centre, same recipe as
#    the circle icon so the two read as an obviously-related pair ─────

def _build_ellipse_dat() -> bytes:
    tris = []
    tris += _ellipse_tris(128, 128, rx=98, ry=64, thickness=16, steps=64)  # ring (green)
    tris += _plus_tris(128, 128, arm=28, thick=14, c=_WHITE)               # plus (white)
    return _make_dat(tris)


def _build_point_dat() -> bytes:
    """Solid dot with four short tick marks extending from it."""
    tris = []
    cx, cy = 128, 128
    dot_r  = 20
    gap    = 30
    tick_len   = 26
    tick_thick = 14
    tris += _circle_tris(cx, cy, r=dot_r / 2, thickness=dot_r, steps=32)   # filled dot (green)
    for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        x0 = cx + dx * gap
        y0 = cy + dy * gap
        x1 = cx + dx * (gap + tick_len)
        y1 = cy + dy * (gap + tick_len)
        tris += _thick_line_tris(x0, y0, x1, y1, tick_thick, c=_WHITE)
    return _make_dat(tris)


def _arc_tris(cx, cy, r, thickness, a_start, a_end, steps=32, c=None):
    """Partial ring (arc) as a strip of thin quads, from a_start to
    a_end (radians)."""
    c = c or _col()
    tris = []
    r_out = r + thickness / 2
    r_in  = r - thickness / 2
    for i in range(steps):
        a0 = a_start + (a_end - a_start) * i       / steps
        a1 = a_start + (a_end - a_start) * (i + 1) / steps
        ox0, oy0 = cx + math.cos(a0) * r_out, cy + math.sin(a0) * r_out
        ox1, oy1 = cx + math.cos(a1) * r_out, cy + math.sin(a1) * r_out
        ix0, iy0 = cx + math.cos(a0) * r_in,  cy + math.sin(a0) * r_in
        ix1, iy1 = cx + math.cos(a1) * r_in,  cy + math.sin(a1) * r_in
        tris.append([((ox0, oy0), c), ((ox1, oy1), c), ((ix1, iy1), c)])
        tris.append([((ox0, oy0), c), ((ix1, iy1), c), ((ix0, iy0), c)])
    return tris


def _square_marker_tris(cx, cy, half, thick, c=None):
    """Hollow square outline marker centred at (cx, cy), matching the
    square node markers in the reference bezier/arc icon."""
    c = c or _col()
    tris = []
    o, i = half, half - thick
    tris += _rect_tris(cx - o, cy - o, cx + o, cy - i, c)   # top
    tris += _rect_tris(cx - o, cy + i, cx + o, cy + o, c)   # bottom
    tris += _rect_tris(cx - o, cy - i, cx - i, cy + i, c)   # left
    tris += _rect_tris(cx + i, cy - i, cx + o, cy + i, c)   # right
    return tris


def _build_spline_dat() -> bytes:
    """Blender-style Draw Curve icon: a smooth S-curve (green) with
    fit-point markers (white dots) along it."""
    p0, p1, p2, p3 = (40, 60), (90, 190), (160, 66), (216, 196)

    def _bezier(t):
        mt = 1 - t
        x = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
        y = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
        return x, y

    tris = []
    steps = 28
    prev = _bezier(0.0)
    for i in range(1, steps + 1):
        cur = _bezier(i / steps)
        tris += _thick_line_tris(prev[0], prev[1], cur[0], cur[1], w=16)
        prev = cur
    for t in (0.0, 0.35, 0.65, 1.0):
        x, y = _bezier(t)
        tris += _dot_tris(x, y, 14, c=_WHITE)
    return _make_dat(tris)


def _build_fillet_dat() -> bytes:
    """Rounded-corner glyph: an L-shaped path whose corner is a
    quarter-circle arc, reading as an obvious 'fillet' silhouette -
    two straight legs (green) blended by a curved corner (green),
    with a white dot marking the tangent radius."""
    w = 18
    leg = 92
    r = 64
    # Corner/arc centre sits up-and-right of the inner corner so the
    # arc sweeps from the vertical leg to the horizontal leg.
    cx, cy = 128 + 6, 40 + r
    tris = []
    # Vertical leg: straight up from the arc's lower tangent point
    tris += _thick_line_tris(cx - r, cy, cx - r, cy + leg, w)
    # Horizontal leg: straight right from the arc's other tangent point
    tris += _thick_line_tris(cx, cy - r, cx + leg, cy - r, w)
    # Quarter-circle corner joining the two legs tangentially
    tris += _arc_tris(cx, cy, r, thickness=w, a_start=math.pi, a_end=math.pi * 1.5, steps=24)
    # White dot marking the tangent point / radius handle
    tris += _dot_tris(cx - r, cy - r, 13, c=_WHITE)
    return _make_dat(tris)



def _build_chamfer_dat() -> bytes:
    """Cut-corner glyph: an L-shaped path whose corner is replaced by
    a straight diagonal segment (not an arc) - two straight legs
    (green) joined by a straight diagonal cut (green), with white dots
    marking the two chamfer points."""
    w = 18
    leg = 92
    r = 64
    cx, cy = 128 + 6, 40 + r
    tris = []
    # Vertical leg: straight up from the lower chamfer point
    tris += _thick_line_tris(cx - r, cy, cx - r, cy + leg, w)
    # Horizontal leg: straight right from the other chamfer point
    tris += _thick_line_tris(cx, cy - r, cx + leg, cy - r, w)
    # Straight diagonal cut joining the two legs (the chamfer itself)
    tris += _thick_line_tris(cx - r, cy, cx, cy - r, w)
    # White dots marking the two chamfer points
    tris += _dot_tris(cx - r, cy, 13, c=_WHITE)
    tris += _dot_tris(cx, cy - r, 13, c=_WHITE)
    return _make_dat(tris)


def _build_array_dat() -> bytes:
    """Linear-array glyph: three square outlines (green) evenly spaced
    along a direction, joined by a dashed guide line (white) with an
    arrowhead at the end - reads as 'repeat this shape along a
    direction'. The first square marks the original; the following
    two read as generated copies."""
    sq = 40
    w = 12
    y = 108
    xs = (34, 104, 174)

    tris = []
    for x in xs:
        tris += _thick_line_tris(x, y, x + sq, y, w)
        tris += _thick_line_tris(x + sq, y, x + sq, y + sq, w)
        tris += _thick_line_tris(x + sq, y + sq, x, y + sq, w)
        tris += _thick_line_tris(x, y + sq, x, y, w)

    guide_y = y + sq / 2
    dash_len, gap_len = 14, 9
    x = xs[0] + sq + 6
    end_x = xs[-1] - 6
    while x < end_x:
        x2 = min(x + dash_len, end_x)
        tris += _thick_line_tris(x, guide_y, x2, guide_y, 5, c=_WHITE)
        x = x2 + gap_len

    tris += _arrow_tris(xs[-1] + sq + 20, guide_y, 1.0, 0.0, 22, c=_WHITE)
    return _make_dat(tris)


def _build_extend_dat() -> bytes:
    """Extend glyph: a solid edge (green) reaching out through a
    dashed extension (green) to meet a solid boundary line (green),
    with a white dot marking the meeting point and an arrowhead
    showing the direction of travel."""
    w = 16
    y = 128
    edge_x0, edge_x1 = 34, 118
    ext_x1 = 176
    boundary_x = 200

    tris = []
    tris += _thick_line_tris(edge_x0, y, edge_x1, y, w)

    dash_len, gap_len = 16, 10
    x = edge_x1
    while x < ext_x1:
        x2 = min(x + dash_len, ext_x1)
        tris += _thick_line_tris(x, y, x2, y, w * 0.7)
        x = x2 + gap_len

    tris += _thick_line_tris(boundary_x, 40, boundary_x, 216, w)
    tris += _arrow_tris(ext_x1 + 14, y, 1.0, 0.0, 24, c=_WHITE)
    tris += _dot_tris(boundary_x, y, 13, c=_WHITE)
    return _make_dat(tris)


def _build_arc_dat() -> bytes:
    """Curved arc stroke (green) through 3 points, each marked with a
    hollow square node (white) - start, midpoint, end - matching the
    reference bezier-curve icon style. Endpoints are placed
    symmetrically about the icon centre (128, 128) so the whole shape
    reads as centred rather than shifted to one side."""
    p0 = (40.0, 40.0)     # bottom-left
    p1 = (66.0, 176.0)    # through point - bulge toward upper-left
    p2 = (216.0, 216.0)   # top-right

    ax, ay = p0
    bx, by = p1
    cx, cy = p2
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay)
          + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx)
          + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)

    a0 = math.atan2(ay - uy, ax - ux)
    a1 = math.atan2(by - uy, bx - ux)
    a2 = math.atan2(cy - uy, cx - ux)
    two_pi = 2 * math.pi
    ccw_sweep = (a2 - a0) % two_pi
    a1_rel    = (a1 - a0) % two_pi
    sweep = ccw_sweep if a1_rel <= ccw_sweep else ccw_sweep - two_pi

    tris = []
    tris += _arc_tris(ux, uy, r, thickness=16, a_start=a0, a_end=a0 + sweep, steps=40)
    for pt in (p0, p1, p2):
        tris += _square_marker_tris(pt[0], pt[1], half=17, thick=8, c=_WHITE)
    return _make_dat(tris)


# ── RECT: four sides + plus sign in the top-right corner ─────────────

def _build_rect_dat() -> bytes:
    tris = []
    lo, hi, w = 32, 220, 14
    tris += _thick_line_tris(lo, lo, hi, lo, w)     # bottom
    tris += _thick_line_tris(hi, lo, hi, hi, w)     # right
    tris += _thick_line_tris(hi, hi, lo, hi, w)     # top
    tris += _thick_line_tris(lo, hi, lo, lo, w)     # left
    # Plus sign inside the top-right corner
    tris += _plus_tris(185, 185, arm=22, thick=11, c=_WHITE)
    return _make_dat(tris)


# ── MOVE: 4-way arrow cross, matching Blender's move-gizmo iconography ─

def _arrow_tris(tip_x, tip_y, dir_x, dir_y, size, c=None):
    """Small triangular arrowhead, tip at (tip_x, tip_y), pointing in
    the (dir_x, dir_y) unit direction."""
    c = c or _col()
    px, py = -dir_y, dir_x
    base_x, base_y = tip_x - dir_x * size, tip_y - dir_y * size
    A = (tip_x, tip_y)
    B = (base_x + px * size * 0.6, base_y + py * size * 0.6)
    C = (base_x - px * size * 0.6, base_y - py * size * 0.6)
    return [[(A, c), (B, c), (C, c)]]


def _build_scale_dat() -> bytes:
    """Nested-square glyph: a small inner square and a larger outer
    square sharing a bottom-left anchor corner (white dot), with a
    diagonal arrow (white) growing outward from the anchor - reads as
    'resize about this fixed point'."""
    ax, ay = 56, 56
    w = 14
    inner = 74
    outer = 150
    tris = []

    def _square(half_size):
        x1, y1 = ax, ay
        x2, y2 = ax + half_size, ay + half_size
        return (
            _thick_line_tris(x1, y1, x2, y1, w) +
            _thick_line_tris(x2, y1, x2, y2, w) +
            _thick_line_tris(x2, y2, x1, y2, w) +
            _thick_line_tris(x1, y2, x1, y1, w)
        )

    tris += _square(inner)
    tris += _square(outer)

    diag = math.sqrt(0.5)
    tip_x, tip_y = ax + outer + 26, ay + outer + 26
    tris += _thick_line_tris(ax + inner * 0.5, ay + inner * 0.5, tip_x - 20, tip_y - 20, 6, c=_WHITE)
    tris += _arrow_tris(tip_x, tip_y, diag, diag, 26, c=_WHITE)
    tris += _dot_tris(ax, ay, 13, c=_WHITE)
    return _make_dat(tris)


def _build_move_dat() -> bytes:
    tris = []
    cx, cy = 128, 128
    arm, w = 82, 16
    # 4 arms of the cross (green)
    tris += _thick_line_tris(cx, cy, cx, cy + arm, w)
    tris += _thick_line_tris(cx, cy, cx, cy - arm, w)
    tris += _thick_line_tris(cx, cy, cx + arm, cy, w)
    tris += _thick_line_tris(cx, cy, cx - arm, cy, w)
    # arrowheads at each tip (white)
    tris += _arrow_tris(cx, cy + arm + 20, 0,  1, 26, c=_WHITE)
    tris += _arrow_tris(cx, cy - arm - 20, 0, -1, 26, c=_WHITE)
    tris += _arrow_tris(cx + arm + 20, cy,  1,  0, 26, c=_WHITE)
    tris += _arrow_tris(cx - arm - 20, cy, -1,  0, 26, c=_WHITE)
    # centre dot (white)
    tris += _dot_tris(cx, cy, 10, c=_WHITE)
    return _make_dat(tris)


def _build_stretch_dat() -> bytes:
    tris = []
    cx, cy = 128, 128
    half, w = 46, 16
    # Two bracket "jaws" top/bottom (green), suggesting a stretched span
    tris += _thick_line_tris(cx - 60, cy - half, cx + 60, cy - half, w)
    tris += _thick_line_tris(cx - 60, cy + half, cx + 60, cy + half, w)
    # Vertical connector through the middle (green)
    tris += _thick_line_tris(cx, cy - half, cx, cy + half, w)
    # Outward arrowheads at the left/right ends of the connector axis
    # (white) - pulling apart, echoing the stretch direction
    tris += _arrow_tris(cx - 60 - 22, cy - half, -1, 0, 24, c=_WHITE)
    tris += _arrow_tris(cx + 60 + 22, cy - half,  1, 0, 24, c=_WHITE)
    tris += _arrow_tris(cx - 60 - 22, cy + half, -1, 0, 24, c=_WHITE)
    tris += _arrow_tris(cx + 60 + 22, cy + half,  1, 0, 24, c=_WHITE)
    return _make_dat(tris)


# ── COPY: two overlapping circle outlines - classic duplicate glyph ───

def _partial_ring_tris(cx, cy, r, thickness, ang_start, ang_end, steps=64, c=None):
    """Ring arc from ang_start sweeping CCW to ang_end (radians) -
    same tessellation as _circle_tris but restricted to an angular
    range, so part of a ring can be left undrawn (e.g. the portion
    hidden behind an overlapping circle)."""
    c = c or _col()
    diff = (ang_end - ang_start) % (2 * math.pi)
    if diff <= 1e-6:
        return []
    n = max(2, int(round(steps * diff / (2 * math.pi))))
    r_out = r + thickness / 2
    r_in  = r - thickness / 2
    tris = []
    for i in range(n):
        a0 = ang_start + diff * i / n
        a1 = ang_start + diff * (i + 1) / n
        ox0, oy0 = cx + math.cos(a0) * r_out, cy + math.sin(a0) * r_out
        ox1, oy1 = cx + math.cos(a1) * r_out, cy + math.sin(a1) * r_out
        ix0, iy0 = cx + math.cos(a0) * r_in,  cy + math.sin(a0) * r_in
        ix1, iy1 = cx + math.cos(a1) * r_in,  cy + math.sin(a1) * r_in
        tris.append([((ox0, oy0), c), ((ox1, oy1), c), ((ix1, iy1), c)])
        tris.append([((ox0, oy0), c), ((ix1, iy1), c), ((ix0, iy0), c)])
    return tris


# ── COPY: two overlapping circle outlines - classic duplicate glyph ───
# The front circle is a complete ring, fully visible. The rear circle
# is only drawn across the arc that sits *outside* the front circle -
# geometrically clipped by the two circles' actual intersection
# angle, not just visually layered on top of each other - so the
# overlapping lens-shaped region genuinely shows only the front
# circle, matching the reference "front circle whole, rear circle only
# visible around its outside" look, independent of the toolbar's
# background colour/theme.

# ── MIRROR: two right-angle triangles facing away from each other,
#    split by a vertical mirror line ────────────────────────────────

def _rounded_polyline_tris(points, thickness, c=None):
    """Open polyline through *points* as thick straight segments, with
    a filled round dot at every vertex (including both ends) so every
    joint and cap reads as smoothly rounded - matching a rounded-
    stroke vector path without needing per-corner arc-sweep math."""
    c = c or _col()
    tris = []
    for i in range(len(points) - 1):
        tris += _thick_line_tris(*points[i], *points[i + 1], thickness, c=c)
    for p in points:
        tris += _dot_tris(p[0], p[1], thickness / 2, steps=20, c=c)
    return tris


def _build_offset_dat() -> bytes:
    """Traced from the addon's Offset reference artwork (pixel-fitted
    from the source image, not eyeballed): a 'b'-shaped bowl path -
    a straight top bar, a short vertical, a big circular bowl arc,
    then a straight bottom bar - with a smaller stepped path nested
    inside it, offset from (never touching) the bowl path. Reads as
    exactly what Offset does: one path is the original, the other a
    parallel copy of it at a distance, corners rounded off just like
    the tool's own mitered offset joins. Recoloured to the addon's
    own mint-green (matching every other tool icon) instead of the
    reference art's white/blue - the outer bowl at full strength, the
    inner stepped path dimmer, so the two stay distinguishable the
    same way this file already distinguishes paired shapes elsewhere
    (e.g. the Move icon's two arrow tones)."""
    tris = []
    outer = _col()            # full-strength mint green - matches every other icon
    inner = _col(a=150)       # same green, dimmer - the nested/offset path

    # ── outer "b" bowl ───────────────────────────────────────────────
    Tg = 15
    A = (54, 214)    # top-left cap
    B = (137, 214)   # top bar end / corner
    C = (140, 163)   # bottom of the short vertical / arc start
    bowl_cx, bowl_cy, bowl_r = 145.0, 105.0, 57.0
    a_top, a_bot = 94.0, -71.6   # degrees; sweeps the right-hand bulge through 0
    E = (bowl_cx + bowl_r * math.cos(math.radians(a_bot)),
         bowl_cy + bowl_r * math.sin(math.radians(a_bot)))
    F = (54, E[1])   # bottom-left cap, level with the arc's lower end

    tris += _thick_line_tris(*A, *B, Tg, c=outer)
    tris += _thick_line_tris(*B, *C, Tg, c=outer)
    tris += _arc_tris(bowl_cx, bowl_cy, bowl_r, Tg,
                       math.radians(a_top), math.radians(a_bot), steps=40, c=outer)
    tris += _thick_line_tris(*E, *F, Tg, c=outer)
    for p in (A, B, C, E, F):
        tris += _dot_tris(p[0], p[1], Tg / 2, steps=20, c=outer)

    # ── inner stepped path ───────────────────────────────────────────
    Tb = 13
    inner_pts = [
        (53, 183),    # top-left cap
        (110, 182),   # top bar end / corner
        (110, 130),   # short vertical down
        (163, 119),   # the step, jogging right
        (163, 73),    # second vertical down
        (54, 73),     # bottom bar back to the left, end cap
    ]
    tris += _rounded_polyline_tris(inner_pts, Tb, c=inner)

    return _make_dat(tris)


def _build_mirror_dat() -> bytes:
    tris = []
    w = 15   # matches the other icons' established outline weight (Rect uses 14)

    # Vertical mirror line: dashed, centred exactly between the two
    # triangles, extending a bit above and below their vertical extent.
    dash_h, gap_h = 26, 16
    y = 26
    while y < 230:
        y_end = min(y + dash_h, 230)
        tris += _thick_line_tris(128, y, 128, y_end, w * 0.6)
        y = y_end + gap_h

    # Left triangle - plain outline, no fill: vertical inner edge near
    # (but not touching) the axis, base along the bottom, hypotenuse
    # sloping out to the acute outer corner - right-angle side faces
    # the axis, acute point faces away from it, with a clear gap
    # between the triangle and the dashed line.
    lA = (104, 56)      # top (near axis)
    lB = (104, 200)     # bottom (right-angle corner, near axis)
    lC = (32, 200)      # outer point, facing away from the axis
    tris += _thick_line_tris(*lA, *lB, w)
    tris += _thick_line_tris(*lB, *lC, w)
    tris += _thick_line_tris(*lC, *lA, w)

    # Right triangle: mirror image of the left one, same gap on the
    # other side of the axis.
    rA = (152, 56)
    rB = (152, 200)
    rC = (224, 200)
    tris += _thick_line_tris(*rA, *rB, w)
    tris += _thick_line_tris(*rB, *rC, w)
    tris += _thick_line_tris(*rC, *rA, w)

    return _make_dat(tris)


def _build_copy_dat() -> bytes:
    tris = []
    r, thickness = 68, 16
    offset = 56   # centre-to-centre offset (purely horizontal)

    rear_c  = (128 - offset * 0.5, 128)
    front_c = (128 + offset * 0.5, 128)
    d = math.hypot(front_c[0] - rear_c[0], front_c[1] - rear_c[1])

    dir_to_front = math.atan2(front_c[1] - rear_c[1], front_c[0] - rear_c[0])
    ratio = max(-1.0, min(1.0, d / (2 * r)))
    theta = math.acos(ratio)   # half-angle of the hidden (overlapped) arc

    back_c = _col(a=140)
    # Rear circle: only the arc *outside* the overlap with the front
    # circle - sweep the long way round, skipping the lens region
    # that faces the front circle.
    tris += _partial_ring_tris(
        rear_c[0], rear_c[1], r, thickness,
        dir_to_front + theta, dir_to_front - theta + 2 * math.pi,
        c=back_c,
    )
    # Front circle: complete ring, always fully visible, drawn last.
    tris += _circle_tris(front_c[0], front_c[1], r, thickness)
    return _make_dat(tris)


# ─────────────────────────────────────────────────────────────────────
# Icon .dat files — written into the addon's own icons/ subfolder.
#
# KEY FIX: the old approach wrote to Blender's system datafiles/icons/
# directory, which is read-only for normal (non-admin) users, causing
# silent PermissionErrors and blank toolbar buttons.
#
# The correct approach (same as mesh_snap_utilities_line) is to:
#   1. Write .dat files into a folder INSIDE the addon package itself.
#   2. Pass the FULL PATH (without .dat) to icon= in ToolDef.
# Blender appends .dat and loads it from wherever the path points —
# it does NOT have to be inside Blender's own datafiles directory.
# ─────────────────────────────────────────────────────────────────────

# Addon-local icons directory - the FALLBACK only, used if the
# extension-user-data API below isn't available (older Blender / a
# legacy, non-Extensions install). Assuming this is writable is what
# silently broke Line/Circle/Rect's icons: Blender Extensions can be
# installed to a read-only location, and the old write-at-import-time
# code caught that failure with a bare `except: pass`, leaving no
# .dat file for the ToolDef's icon= path to actually find.
_ADDON_DIR = os.path.dirname(__file__)          # …/ui/


def _get_icons_dir() -> str:
    """A directory we can actually write generated icon .dat files
    into, every time, regardless of whether the extension itself was
    installed read-only.

    bpy.utils.extension_path_user() (Blender 4.2+) returns a
    per-extension directory under the user's writable config/extension
    data path - not inside the extension package - which is exactly
    what's needed for a runtime-generated asset like this. Falls back
    to the addon's own icons/ folder for older Blender versions or
    non-Extensions (legacy Install-from-Disk) setups, where that
    folder normally *is* writable.
    """
    try:
        root_pkg = (__package__ or __name__).split('.')[0]
        d = bpy.utils.extension_path_user(root_pkg, path="icons", create=True)
        if d:
            return d
    except Exception as exc:
        print(f"[CAD] extension_path_user() unavailable, falling back: {exc}")
    return os.path.join(os.path.dirname(_ADDON_DIR), "icons")


_ICONS_DIR = _get_icons_dir()

# Full paths passed to ToolDef icon= (Blender appends .dat automatically)
_PATH_LINE    = os.path.join(_ICONS_DIR, _ICON_LINE)
_PATH_CIRCLE  = os.path.join(_ICONS_DIR, _ICON_CIRCLE)
_PATH_ELLIPSE = os.path.join(_ICONS_DIR, _ICON_ELLIPSE)
_PATH_POINT   = os.path.join(_ICONS_DIR, _ICON_POINT)
_PATH_ARC     = os.path.join(_ICONS_DIR, _ICON_ARC)
_PATH_SPLINE  = os.path.join(_ICONS_DIR, _ICON_SPLINE)
_PATH_FILLET  = os.path.join(_ICONS_DIR, _ICON_FILLET)
_PATH_CHAMFER = os.path.join(_ICONS_DIR, _ICON_CHAMFER)
_PATH_EXTEND  = os.path.join(_ICONS_DIR, _ICON_EXTEND)
_PATH_RECT    = os.path.join(_ICONS_DIR, _ICON_RECT)
_PATH_MOVE    = os.path.join(_ICONS_DIR, _ICON_MOVE)
_PATH_SCALE   = os.path.join(_ICONS_DIR, _ICON_SCALE)
_PATH_ARRAY   = os.path.join(_ICONS_DIR, _ICON_ARRAY)
_PATH_STRETCH = os.path.join(_ICONS_DIR, _ICON_STRETCH)
_PATH_COPY    = os.path.join(_ICONS_DIR, _ICON_COPY)
_PATH_MIRROR  = os.path.join(_ICONS_DIR, _ICON_MIRROR)
_PATH_OFFSET  = os.path.join(_ICONS_DIR, _ICON_OFFSET)


def _install_icons() -> None:
    """Write .dat geometry files into a writable icons directory."""
    global _ICONS_DIR, _PATH_LINE, _PATH_CIRCLE, _PATH_ELLIPSE, _PATH_POINT, _PATH_ARC, _PATH_SPLINE, _PATH_FILLET, _PATH_CHAMFER, _PATH_EXTEND, _PATH_RECT, _PATH_SCALE, _PATH_ARRAY, _PATH_COPY, _PATH_MIRROR, _PATH_OFFSET

    # Re-resolve fresh here rather than trusting whatever _ICONS_DIR
    # was computed as at raw module-import time - extension_path_user()
    # can behave differently (or even fail) the instant this submodule
    # is first imported during Blender's addon-loading sequence, before
    # the extension is fully registered. register() is a much safer
    # point to ask again; if it now resolves somewhere different (or
    # correctly, where the import-time attempt silently fell back),
    # update every path built from it so nothing is left pointing at
    # the old, possibly-unwritable location.
    fresh_dir = _get_icons_dir()
    if fresh_dir != _ICONS_DIR:
        print(f"[CAD] icons dir changed on register(): {_ICONS_DIR} -> {fresh_dir}")
        _ICONS_DIR   = fresh_dir
        _PATH_LINE    = os.path.join(_ICONS_DIR, _ICON_LINE)
        _PATH_CIRCLE  = os.path.join(_ICONS_DIR, _ICON_CIRCLE)
        _PATH_ELLIPSE = os.path.join(_ICONS_DIR, _ICON_ELLIPSE)
        _PATH_POINT   = os.path.join(_ICONS_DIR, _ICON_POINT)
        _PATH_ARC     = os.path.join(_ICONS_DIR, _ICON_ARC)
        _PATH_SPLINE  = os.path.join(_ICONS_DIR, _ICON_SPLINE)
        _PATH_FILLET  = os.path.join(_ICONS_DIR, _ICON_FILLET)
        _PATH_CHAMFER = os.path.join(_ICONS_DIR, _ICON_CHAMFER)
        _PATH_EXTEND  = os.path.join(_ICONS_DIR, _ICON_EXTEND)
        _PATH_RECT    = os.path.join(_ICONS_DIR, _ICON_RECT)
        _PATH_SCALE   = os.path.join(_ICONS_DIR, _ICON_SCALE)
        _PATH_ARRAY   = os.path.join(_ICONS_DIR, _ICON_ARRAY)
        _PATH_COPY    = os.path.join(_ICONS_DIR, _ICON_COPY)
        _PATH_MIRROR  = os.path.join(_ICONS_DIR, _ICON_MIRROR)
        _PATH_OFFSET  = os.path.join(_ICONS_DIR, _ICON_OFFSET)

    try:
        os.makedirs(_ICONS_DIR, exist_ok=True)
    except Exception as exc:
        print(f"[CAD] Could not create icons dir {_ICONS_DIR}: {exc}")
        return

    for path, builder in (
        (_PATH_LINE    + ".dat", _build_line_dat),
        (_PATH_CIRCLE  + ".dat", _build_circle_dat),
        (_PATH_ELLIPSE + ".dat", _build_ellipse_dat),
        (_PATH_POINT   + ".dat", _build_point_dat),
        (_PATH_ARC     + ".dat", _build_arc_dat),
        (_PATH_SPLINE  + ".dat", _build_spline_dat),
        (_PATH_FILLET  + ".dat", _build_fillet_dat),
        (_PATH_CHAMFER + ".dat", _build_chamfer_dat),
        (_PATH_EXTEND  + ".dat", _build_extend_dat),
        (_PATH_RECT    + ".dat", _build_rect_dat),
        (_PATH_SCALE   + ".dat", _build_scale_dat),
        (_PATH_ARRAY   + ".dat", _build_array_dat),
        (_PATH_COPY    + ".dat", _build_copy_dat),
        (_PATH_MIRROR  + ".dat", _build_mirror_dat),
        (_PATH_OFFSET  + ".dat", _build_offset_dat),
    ):
        try:
            data = builder()
            with open(path, "wb") as f:
                f.write(data)
            print(f"[CAD] icon written: {path}")
        except Exception as exc:
            print(f"[CAD] icon write failed ({path}): {exc}")


def _remove_icons() -> None:
    """Remove addon .dat files on unregister (optional cleanup)."""
    for path in (_PATH_LINE, _PATH_CIRCLE, _PATH_ELLIPSE, _PATH_POINT, _PATH_ARC, _PATH_SPLINE, _PATH_FILLET, _PATH_CHAMFER, _PATH_EXTEND, _PATH_RECT, _PATH_SCALE, _PATH_ARRAY, _PATH_COPY, _PATH_MIRROR, _PATH_OFFSET):
        full = path + ".dat"
        if os.path.exists(full):
            try:
                os.remove(full)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────
# Write icons immediately at module load so they exist before
# @ToolDef.from_fn decorators run (decorator captures icon= at load time).
# Always overwrite — never skip if file already exists, so that
# updating the addon always gets fresh icons on next Blender start.
# ─────────────────────────────────────────────────────────────────────
try:
    os.makedirs(_ICONS_DIR, exist_ok=True)
except Exception as _exc:
    print(f"[CAD] Could not create icons dir at module load (dir={_ICONS_DIR}): {_exc}")
else:
    for _p, _b in (
        (_PATH_LINE    + ".dat", _build_line_dat),
        (_PATH_CIRCLE  + ".dat", _build_circle_dat),
        (_PATH_ELLIPSE + ".dat", _build_ellipse_dat),
        (_PATH_POINT   + ".dat", _build_point_dat),
        (_PATH_ARC     + ".dat", _build_arc_dat),
        (_PATH_SPLINE  + ".dat", _build_spline_dat),
        (_PATH_FILLET  + ".dat", _build_fillet_dat),
        (_PATH_CHAMFER + ".dat", _build_chamfer_dat),
        (_PATH_EXTEND  + ".dat", _build_extend_dat),
        (_PATH_RECT    + ".dat", _build_rect_dat),
        (_PATH_SCALE   + ".dat", _build_scale_dat),
        (_PATH_ARRAY   + ".dat", _build_array_dat),
        (_PATH_COPY    + ".dat", _build_copy_dat),
        (_PATH_MIRROR  + ".dat", _build_mirror_dat),
        (_PATH_OFFSET  + ".dat", _build_offset_dat),
    ):
        try:
            with open(_p, "wb") as _f:   # always overwrite, no existence check
                _f.write(_b())
        except Exception as _exc:
            # register() will try again (via _install_icons()) with
            # full error reporting - but log here too rather than
            # swallowing it silently. Each icon is isolated: one
            # failing here (e.g. a bug in a single builder) must never
            # prevent every other icon from being written.
            print(f"[CAD] module-load icon write failed ({_p}): {_exc}")


# ─────────────────────────────────────────────────────────────────────
# ToolDef declarations
#
# All icon= paths use the shared _ICONS_DIR (a guaranteed-writable
# directory - see _get_icons_dir() above), not a path recomputed
# locally per-function. Blender appends .dat automatically when
# loading the icon geometry.
# ─────────────────────────────────────────────────────────────────────

@ToolDef.from_fn
def tool_cad_line():
    return dict(
        idname      = _IDNAME_LINE,
        label       = "CAD Line",
        description = (
            "Draw lines and polylines in CAD Sketch Mode.\n"
            "Click to place points  ·  Enter to finish  ·  Esc to cancel"
        ),
        icon        = os.path.join(_ICONS_DIR, _ICON_LINE),
        widget      = None,
        keymap      = _KM_LINE,
    )


@ToolDef.from_fn
def tool_cad_circle():
    return dict(
        idname      = _IDNAME_CIRCLE,
        label       = "CAD Circle",
        description = (
            "Draw circles in CAD Sketch Mode.\n"
            "Click to set center  ·  move & click to set radius  ·  Esc to cancel"
        ),
        icon        = os.path.join(_ICONS_DIR, _ICON_CIRCLE),
        widget      = None,
        keymap      = _KM_CIRCLE,
    )


@ToolDef.from_fn
def tool_cad_ellipse():
    return dict(
        idname      = _IDNAME_ELLIPSE,
        label       = "CAD Ellipse",
        description = (
            "Draw ellipses in CAD Sketch Mode.\n"
            "Click to set center  ·  move & click to set major axis  ·  move & click to set minor axis  ·  Esc to cancel"
        ),
        icon        = os.path.join(_ICONS_DIR, _ICON_ELLIPSE),
        widget      = None,
        keymap      = _KM_ELLIPSE,
    )


@ToolDef.from_fn
def tool_cad_point():
    return dict(
        idname      = _IDNAME_POINT,
        label       = "CAD Point",
        description = (
            "Place points in CAD Sketch Mode.\n"
            "Click to place a point  ·  type coordinates for precise placement  ·  Esc to cancel"
        ),
        icon        = os.path.join(_ICONS_DIR, _ICON_POINT),
        widget      = None,
        keymap      = _KM_POINT,
    )


@ToolDef.from_fn
def tool_cad_arc():
    return dict(
        idname      = _IDNAME_ARC,
        label       = "CAD Arc",
        description = (
            "Draw 3-point arcs in CAD Sketch Mode.\n"
            "Click start  ·  click end  ·  move & click to bulge through a third point  ·  Esc to cancel"
        ),
        icon        = os.path.join(_ICONS_DIR, _ICON_ARC),
        widget      = None,
        keymap      = _KM_ARC,
    )


@ToolDef.from_fn
def tool_cad_spline():
    return dict(
        idname      = _IDNAME_SPLINE,
        label       = "CAD Spline",
        description = (
            "Draw a Fit-Points spline in CAD Sketch Mode.\n"
            "Click to add fit points  ·  Enter to finish  ·  X/Y=axis  ·  Esc to cancel"
        ),
        icon        = os.path.join(_ICONS_DIR, _ICON_SPLINE),
        widget      = None,
        keymap      = _KM_SPLINE,
    )


@ToolDef.from_fn
def tool_cad_fillet():
    return dict(
        idname      = _IDNAME_FILLET,
        label       = "CAD Fillet",
        description = (
            "Round a corner between two lines/edges with a tangent "
            "circular arc in CAD Sketch Mode.\n"
            "Click first edge  ·  click second edge  ·  type a radius "
            "or scroll to adjust, watching the live preview  ·  "
            "Click/Enter to create  ·  Esc to cancel"
        ),
        icon        = os.path.join(_ICONS_DIR, _ICON_FILLET),
        widget      = None,
        keymap      = _KM_FILLET,
    )


@ToolDef.from_fn
def tool_cad_chamfer():
    return dict(
        idname      = _IDNAME_CHAMFER,
        label       = "CAD Chamfer",
        description = (
            "Replace a sharp corner between two lines/edges with a "
            "straight diagonal segment in CAD Sketch Mode.\n"
            "Enter a distance  ·  click first edge  ·  click second "
            "edge  ·  Esc to cancel"
        ),
        icon        = os.path.join(_ICONS_DIR, _ICON_CHAMFER),
        widget      = None,
        keymap      = _KM_CHAMFER,
    )


@ToolDef.from_fn
def tool_cad_extend():
    return dict(
        idname      = _IDNAME_EXTEND,
        label       = "CAD Extend",
        description = (
            "Extend a line/edge until it meets a chosen boundary edge "
            "in CAD Sketch Mode.\n"
            "Click the boundary edge  ·  click the edge to extend, "
            "near the end to stretch  ·  Esc to cancel"
        ),
        icon        = os.path.join(_ICONS_DIR, _ICON_EXTEND),
        widget      = None,
        keymap      = _KM_EXTEND,
    )




@ToolDef.from_fn
def tool_cad_rect():
    return dict(
        idname      = _IDNAME_RECT,
        label       = "CAD Rectangle",
        description = (
            "Draw rectangles in CAD Sketch Mode.\n"
            "Click first corner  ·  move & click opposite corner  ·  Esc to cancel"
        ),
        icon        = os.path.join(_ICONS_DIR, _ICON_RECT),
        widget      = None,
        keymap      = _KM_RECT,
    )


@ToolDef.from_fn
def tool_cad_move():
    return dict(
        idname      = _IDNAME_MOVE,
        label       = "CAD Move",
        description = (
            "Move whole object(s) in CAD Sketch Mode - never reshapes "
            "geometry.\n"
            "Select object(s)  ·  specify base point  ·  "
            "specify destination  ·  Esc to cancel"
        ),
        icon        = "ops.transform.translate",   # Blender's built-in Move icon
        widget      = None,
        keymap      = _KM_MOVE,
    )


@ToolDef.from_fn
def tool_cad_scale():
    return dict(
        idname      = _IDNAME_SCALE,
        label       = "CAD Scale",
        description = (
            "Scale selected object(s) uniformly about a fixed anchor "
            "point in CAD Sketch Mode.\n"
            "Select object(s)  ·  specify anchor point  ·  "
            "type a scale factor  ·  Enter to apply  ·  Esc to cancel"
        ),
        icon        = os.path.join(_ICONS_DIR, _ICON_SCALE),
        widget      = None,
        keymap      = _KM_SCALE,
    )


@ToolDef.from_fn
def tool_cad_array():
    return dict(
        idname      = _IDNAME_ARRAY,
        label       = "CAD Array",
        description = (
            "Rectangular or Polar array of selected object(s) in CAD "
            "Sketch Mode - all copies of a source are merged into one "
            "new object on their own collection; the original stays "
            "untouched on its own collection.\n"
            "Select object(s)  ·  specify base/center point  ·  "
            "Alt=method menu, Scroll/M=switch Rectangular/Polar  ·  "
            "drag for direction/spacing or sweep angle, or Shift to "
            "type the numeric fields  ·  Click or Enter to apply  ·  "
            "Esc to cancel"
        ),
        icon        = os.path.join(_ICONS_DIR, _ICON_ARRAY),
        widget      = None,
        keymap      = _KM_ARRAY,
    )


@ToolDef.from_fn
def tool_cad_stretch():
    return dict(
        idname      = _IDNAME_STRETCH,
        label       = "CAD Stretch",
        description = (
            "Stretch selected point(s) (endpoints/midpoints/quadrants/"
            "etc) in CAD Sketch Mode.\n"
            "Select point(s)  ·  specify base point  ·  "
            "specify destination  ·  Esc to cancel"
        ),
        icon        = "ops.transform.shear",   # Blender's built-in Shear icon
        widget      = None,
        keymap      = _KM_STRETCH,
    )


@ToolDef.from_fn
def tool_cad_rotate():
    return dict(
        idname      = _IDNAME_ROTATE,
        label       = "CAD Rotate",
        description = (
            "Rotate selected object(s)/point(s) about a pivot in CAD "
            "Sketch Mode.\n"
            "Select object(s)/point(s)  ·  specify pivot point  ·  "
            "specify rotation angle  ·  Esc to cancel"
        ),
        icon        = "ops.transform.rotate",   # Blender's built-in Rotate icon
        widget      = None,
        keymap      = _KM_ROTATE,
    )


@ToolDef.from_fn
def tool_cad_copy():
    return dict(
        idname      = _IDNAME_COPY,
        label       = "CAD Copy",
        description = (
            "Copy/duplicate selected object(s)/point(s) in CAD Sketch "
            "Mode - the original is never modified.\n"
            "Select object(s)/point(s)  ·  specify base point  ·  "
            "click to place one or more copies  ·  Enter/Esc to finish"
        ),
        icon        = os.path.join(_ICONS_DIR, _ICON_COPY),
        widget      = None,
        keymap      = _KM_COPY,
    )


@ToolDef.from_fn
def tool_cad_mirror():
    return dict(
        idname      = _IDNAME_MIRROR,
        label       = "CAD Mirror",
        description = (
            "Mirror selected object(s)/point(s) across a line in CAD "
            "Sketch Mode.\n"
            "Select object(s)/point(s)  ·  draw the mirror line  ·  "
            "choose Keep or Delete Original  ·  Esc to cancel"
        ),
        icon        = os.path.join(_ICONS_DIR, _ICON_MIRROR),
        widget      = None,
        keymap      = _KM_MIRROR,
    )


@ToolDef.from_fn
def tool_cad_trim():
    return dict(
        idname      = _IDNAME_TRIM,
        label       = "CAD Trim",
        description = (
            "Trim geometry against one or more cutting boundaries in "
            "CAD Sketch Mode.\n"
            "Select cutting boundarie(s)  ·  hover to preview  ·  "
            "click to remove that segment  ·  Enter/Esc to finish"
        ),
        icon        = "ops.mesh.bisect",   # Blender's built-in Bisect icon
        widget      = None,
        keymap      = _KM_TRIM,
    )


@ToolDef.from_fn
def tool_cad_offset():
    return dict(
        idname      = _IDNAME_OFFSET,
        label       = "CAD Offset",
        description = (
            "Offset selected geometry to create a parallel copy in CAD "
            "Sketch Mode - the original is never modified.\n"
            "Click a line/arc/rect/circle/spline  ·  move mouse (or type "
            "a number) to set side/distance  ·  click/Enter to confirm  ·  "
            "stays active for more offsets  ·  Esc to cancel"
        ),
        icon        = os.path.join(_ICONS_DIR, _ICON_OFFSET),
        widget      = None,
        keymap      = _KM_OFFSET,
    )


@ToolDef.from_fn
def tool_cad_measure():
    return dict(
        idname      = _IDNAME_MEASURE,
        label       = "CAD Measure",
        description = (
            "Measure the distance between two points in CAD Sketch "
            "Mode, AutoCAD DIST-style.\n"
            "Click the first point  ·  drag & click the second point  ·  "
            "snaps to vertices/edges/curves  ·  press ↑/↓ while placing "
            "to choose which side the dimension line sits on  ·  "
            "results stay on screen until you start a new measurement  ·  "
            "press Tab while drawing to keep it once finished, or "
            "double-click a finished one to select it (Shift+Click to "
            "add more, X/Del=delete, Ctrl+A=select all, tap Shift alone "
            "to flip a selected length's direction)  ·  Enter/Esc to exit"
        ),
        icon        = "ops.view3d.ruler",   # Blender's built-in Measure icon
        widget      = None,
        keymap      = _KM_MEASURE,
    )


# ─────────────────────────────────────────────────────────────────────
# Tool-list helpers
# ─────────────────────────────────────────────────────────────────────

def _get_tool_list(space_type, context_mode):
    from bl_ui.space_toolsystem_common import ToolSelectPanelHelper
    cls = ToolSelectPanelHelper._tool_class_from_space_type(space_type)
    return cls._tools[context_mode]


# ─────────────────────────────────────────────────────────────────────
# Public show / hide — called by sketch_mode.py
# ─────────────────────────────────────────────────────────────────────

def show() -> None:
    """Inject CAD tool buttons into the T-panel. Called on Sketch Mode enter."""
    global _tools_visible
    if _tools_visible:
        return
    try:
        tools = _get_tool_list("VIEW_3D", "OBJECT")
        tools += None, tool_cad_line, tool_cad_circle, tool_cad_ellipse, tool_cad_point, tool_cad_arc, tool_cad_spline, tool_cad_fillet, tool_cad_chamfer, tool_cad_extend, tool_cad_rect, tool_cad_move, tool_cad_scale, tool_cad_array, tool_cad_stretch, tool_cad_rotate, tool_cad_copy, tool_cad_mirror, tool_cad_trim, tool_cad_offset, tool_cad_measure
        _tools_visible = True
    except Exception as exc:
        print(f"[CAD] cad_tpanel_tools.show() failed: {exc}")


def hide() -> None:
    """Remove CAD tool buttons from the T-panel. Called on Sketch Mode exit."""
    global _tools_visible
    if not _tools_visible:
        return
    try:
        tools = _get_tool_list("VIEW_3D", "OBJECT")
        try:
            idx = tools.index(tool_cad_line)
            if idx > 0 and tools[idx - 1] is None:
                tools.pop(idx - 1)
        except (ValueError, IndexError):
            pass
        for t in (tool_cad_line, tool_cad_circle, tool_cad_ellipse, tool_cad_point, tool_cad_arc, tool_cad_spline, tool_cad_fillet, tool_cad_chamfer, tool_cad_extend, tool_cad_rect, tool_cad_move, tool_cad_scale, tool_cad_array, tool_cad_stretch, tool_cad_rotate, tool_cad_copy, tool_cad_mirror, tool_cad_trim, tool_cad_offset, tool_cad_measure):
            try:
                tools.remove(t)
            except (ValueError, AttributeError):
                pass
        _tools_visible = False
    except Exception as exc:
        print(f"[CAD] cad_tpanel_tools.hide() failed: {exc}")


# ─────────────────────────────────────────────────────────────────────
# Keymaps
# ─────────────────────────────────────────────────────────────────────

_KM_DATA = [
    (
        _KM_LINE,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.draw_line",   {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_CIRCLE,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.draw_circle", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_ELLIPSE,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.draw_ellipse", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_POINT,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.draw_point", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_ARC,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.draw_arc", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_SPLINE,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.draw_spline", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_FILLET,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.draw_fillet", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_CHAMFER,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.draw_chamfer", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_EXTEND,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.draw_extend", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_RECT,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.draw_rect",   {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_MOVE,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.sketch_move", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_SCALE,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.sketch_scale", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_ARRAY,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.sketch_array", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_STRETCH,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.sketch_stretch", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_ROTATE,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.sketch_rotate", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_COPY,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.sketch_copy", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_MIRROR,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.sketch_mirror", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_TRIM,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.sketch_trim", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_OFFSET,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.draw_offset", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
    (
        _KM_MEASURE,
        {"space_type": "VIEW_3D", "region_type": "WINDOW"},
        {"items": [("cad.measure", {"type": "LEFTMOUSE", "value": "PRESS"}, {"properties": []})]},
    ),
]


def _register_keymaps() -> None:
    from bl_keymap_utils.io import keyconfig_init_from_data
    kc = bpy.context.window_manager.keyconfigs
    keyconfig_init_from_data(kc.default, _KM_DATA)
    keyconfig_init_from_data(kc.addon,   _KM_DATA)
    wm = bpy.context.window_manager
    for km_name, km_space, _ in _KM_DATA:
        km = wm.keyconfigs.addon.keymaps.find(
            km_name,
            space_type  = km_space["space_type"],
            region_type = km_space["region_type"],
        )
        if km:
            _addon_keymaps.append(km)


def _unregister_keymaps() -> None:
    kc = bpy.context.window_manager.keyconfigs
    for km_name, km_space, _ in _KM_DATA:
        for conf in (kc.default, kc.addon):
            km = conf.keymaps.find(
                km_name,
                space_type  = km_space["space_type"],
                region_type = km_space["region_type"],
            )
            if km:
                try:
                    conf.keymaps.remove(km)
                except Exception:
                    pass
    _addon_keymaps.clear()


# ─────────────────────────────────────────────────────────────────────
# Register / Unregister
# ─────────────────────────────────────────────────────────────────────

def register() -> None:
    _install_icons()
    _register_keymaps()
    print("[CAD v0.92] cad_tpanel_tools registered")


def unregister() -> None:
    hide()
    _unregister_keymaps()
    _remove_icons()
    print("[CAD v0.92] cad_tpanel_tools unregistered")
