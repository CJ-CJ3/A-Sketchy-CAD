# ─────────────────────────────────────────────────────────────────────
#  gfx/primitives.py
#  Low-level GPU drawing helpers:
#    lines, loops, filled rects, circles, dots, text, dashed lines,
#    dashed arcs, arc arrows.
# ─────────────────────────────────────────────────────────────────────

import math
import gpu
import blf
from gpu_extras.batch import batch_for_shader

# ── cached shader ────────────────────────────────────────────────────
_SH = None


def _sh():
    global _SH
    if _SH is None:
        _SH = gpu.shader.from_builtin('UNIFORM_COLOR')
    return _SH


# ════════════════════════════════════════════════════════════════════
#  BASIC SHAPES
# ════════════════════════════════════════════════════════════════════

def gfx_lines(pts, col=(1, 1, 0, 1), w=1.5):
    """Draw a polyline (open) from a list of 2-D or 3-D points."""
    if len(pts) < 2:
        return
    gpu.state.blend_set('ALPHA')
    sh  = _sh()
    idx = [(i, i + 1) for i in range(len(pts) - 1)]
    b   = batch_for_shader(
        sh, 'LINES', {"pos": [list(p) for p in pts]}, indices=idx
    )
    sh.bind()
    sh.uniform_float("color", col)
    gpu.state.line_width_set(w)
    b.draw(sh)
    gpu.state.blend_set('NONE')


def gfx_loop(pts, col=(1, 1, 0, 1), w=1.5):
    """Draw a closed polygon outline."""
    if len(pts) >= 2:
        gfx_lines(list(pts) + [pts[0]], col, w)


def gfx_rect_fill(x, y, w, h, col):
    """Draw a solid filled rectangle (screen space)."""
    gpu.state.blend_set('ALPHA')
    sh  = _sh()
    vtx = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    b   = batch_for_shader(
        sh, 'TRIS', {"pos": vtx}, indices=[(0, 1, 2), (0, 2, 3)]
    )
    sh.bind()
    sh.uniform_float("color", col)
    b.draw(sh)
    gpu.state.blend_set('NONE')


def gfx_circle(cen, r, col=(1, 1, 0, 1), segs=64, w=1.5):
    """Draw a circle outline in screen space."""
    pts = [
        (cen[0] + math.cos(2 * math.pi * i / segs) * r,
         cen[1] + math.sin(2 * math.pi * i / segs) * r)
        for i in range(segs + 1)
    ]
    gfx_lines(pts, col, w)


def gfx_dot(pos, col=(0, 1, 0, 1), size=8.0):
    """Draw a single point (dot) in screen space."""
    gpu.state.blend_set('ALPHA')
    sh = _sh()
    b  = batch_for_shader(sh, 'POINTS', {"pos": [list(pos)]})
    sh.bind()
    sh.uniform_float("color", col)
    gpu.state.point_size_set(size)
    b.draw(sh)
    gpu.state.blend_set('NONE')


def gfx_tri_fill(p1, p2, p3, col):
    """Draw a solid filled triangle in screen space."""
    gpu.state.blend_set('ALPHA')
    sh = _sh()
    b  = batch_for_shader(sh, 'TRIS', {"pos": [list(p1), list(p2), list(p3)]})
    sh.bind()
    sh.uniform_float("color", col)
    b.draw(sh)
    gpu.state.blend_set('NONE')



def gfx_text(txt, x, y, size, col=(1, 1, 1, 1)):
    """Draw a string using BLF at screen-space position (x, y)."""
    f = 0
    try:
        blf.size(f, size, 72)
    except Exception:
        blf.size(f, size)
    blf.color(f, *col)
    blf.position(f, x, y, 0)
    blf.draw(f, txt)


def gfx_text_dims(txt, size):
    """Return (width, height) in pixels that *txt* would occupy at *size*,
    using BLF's own metrics (so callers can center/background it exactly)."""
    f = 0
    try:
        blf.size(f, size, 72)
    except Exception:
        blf.size(f, size)
    return blf.dimensions(f, txt)


def gfx_text_rotated(txt, x, y, size, col=(1, 1, 1, 1), angle=0.0):
    """Draw text at (x, y) rotated *angle* radians CCW around that point.

    Falls back to unrotated drawing if BLF rotation isn't available on
    this Blender build (it always is on 2.8+, but this keeps it safe).
    """
    f = 0
    try:
        blf.size(f, size, 72)
    except Exception:
        blf.size(f, size)
    blf.color(f, *col)
    blf.position(f, x, y, 0)
    try:
        if angle:
            blf.enable(f, blf.ROTATION)
            blf.rotation(f, angle)
        blf.draw(f, txt)
    finally:
        if angle:
            try:
                blf.disable(f, blf.ROTATION)
            except Exception:
                pass


def gfx_rounded_rect_fill(x, y, w, h, r, col):
    """Draw a solid filled rounded rectangle with no corner gaps.

    Tessellation: centre rect + 4 edge rects + 4 corner arc fans.
    This guarantees every pixel is covered even with large corner radii.
    """
    r = min(r, w / 2, h / 2)
    gpu.state.blend_set('ALPHA')
    sh = _sh()
    sh.bind()
    sh.uniform_float("color", col)

    def _draw_tris(verts, indices):
        b = batch_for_shader(sh, 'TRIS', {"pos": verts}, indices=indices)
        b.draw(sh)

    # ── 3 solid rectangles that cover the interior without overlap ───
    # Vertical centre strip  (full height, inner width)
    _draw_tris(
        [(x + r, y), (x + w - r, y), (x + w - r, y + h), (x + r, y + h)],
        [(0, 1, 2), (0, 2, 3)],
    )
    # Left edge strip  (inner height, left margin)
    _draw_tris(
        [(x, y + r), (x + r, y + r), (x + r, y + h - r), (x, y + h - r)],
        [(0, 1, 2), (0, 2, 3)],
    )
    # Right edge strip
    _draw_tris(
        [(x + w - r, y + r), (x + w, y + r),
         (x + w,     y + h - r), (x + w - r, y + h - r)],
        [(0, 1, 2), (0, 2, 3)],
    )

    # ── 4 corner arc fans ────────────────────────────────────────────
    segs = max(20, int(r * 1.5))
    corner_data = [
        (x + r,     y + r,     math.pi,          3 * math.pi / 2),  # bottom-left
        (x + w - r, y + r,     3 * math.pi / 2,  2 * math.pi    ),  # bottom-right
        (x + w - r, y + h - r, 0,                math.pi / 2    ),  # top-right
        (x + r,     y + h - r, math.pi / 2,      math.pi        ),  # top-left
    ]
    for cx, cy, a0, a1 in corner_data:
        arc = [(cx + math.cos(a0 + (a1 - a0) * i / segs) * r,
                cy + math.sin(a0 + (a1 - a0) * i / segs) * r)
               for i in range(segs + 1)]
        verts   = [[cx, cy]] + [list(p) for p in arc]
        indices = [(0, i, i + 1) for i in range(1, len(verts) - 1)]
        _draw_tris(verts, indices)

    gpu.state.blend_set('NONE')


def gfx_rounded_rect_loop(x, y, w, h, r, col, lw=1.0):
    """Draw a rounded rectangle outline."""
    r = min(r, w // 2, h // 2)
    segs = max(24, int(r * 1.5))
    corners = [
        (x + r,     y + r    ),
        (x + w - r, y + r    ),
        (x + w - r, y + h - r),
        (x + r,     y + h - r),
    ]
    arc_ranges = [
        (math.pi,          3 * math.pi / 2),
        (3 * math.pi / 2,  2 * math.pi     ),
        (0,                math.pi / 2     ),
        (math.pi / 2,      math.pi         ),
    ]
    pts = []
    for (cx, cy), (a0, a1) in zip(corners, arc_ranges):
        for i in range(segs + 1):
            a = a0 + (a1 - a0) * i / segs
            pts.append((cx + math.cos(a) * r, cy + math.sin(a) * r))
    gfx_loop(pts, col, lw)


# ════════════════════════════════════════════════════════════════════
#  DASHED / ANNOTATED
# ════════════════════════════════════════════════════════════════════

_DASH = 14
_GAP  = 9


def gfx_dashed_line(p1, p2,
                    col=(0.2, 0.9, 0.2, 0.6),
                    dash_len=_DASH,
                    space_len=_GAP,
                    w=1.5):
    """Draw a dashed line between two 2-D points.

    Builds explicit non-consecutive index pairs so each dash is its
    own disconnected segment - passing the flat point list straight to
    gfx_lines() would connect every consecutive point, including the
    *gaps* between dashes, which renders as a solid line.
    """
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    dist   = math.hypot(dx, dy)
    if dist < 1e-4:
        return
    ux, uy = dx / dist, dy / dist
    pts, idx, cur = [], [], 0
    while cur < dist:
        s   = (p1[0] + ux * cur,   p1[1] + uy * cur)
        nxt = min(cur + dash_len, dist)
        e   = (p1[0] + ux * nxt,   p1[1] + uy * nxt)
        i = len(pts)
        pts.extend([s, e])
        idx.append((i, i + 1))
        cur += dash_len + space_len
    if not pts:
        return
    gpu.state.blend_set('ALPHA')
    sh = _sh()
    b  = batch_for_shader(sh, 'LINES', {"pos": [list(p) for p in pts]}, indices=idx)
    sh.bind()
    sh.uniform_float("color", col)
    gpu.state.line_width_set(w)
    b.draw(sh)
    gpu.state.blend_set('NONE')


def gfx_dashed_polyline(points,
                        col=(0.2, 0.9, 0.2, 0.6),
                        dash_len=_DASH,
                        space_len=_GAP,
                        w=1.5):
    """Draw a dashed line that follows an arbitrary open chain of 2-D
    points (e.g. a tessellated arc projected to screen space), rather
    than a single straight segment.

    Walks cumulative distance along the whole chain and steps
    dash/gap lengths across it exactly like gfx_dashed_line does for
    a single segment - a dash (or a gap) can span a vertex of the
    chain without a visible seam. Same disconnected-index-pairs
    technique as gfx_dashed_line/gfx_dashed_arc, for the same reason:
    a flat point list fed straight to gfx_lines() would also connect
    the gaps.
    """
    if len(points) < 2:
        return

    # Precompute each segment's endpoints/length/unit direction once.
    segs = []
    for a, b in zip(points, points[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        seg_len = math.hypot(dx, dy)
        if seg_len < 1e-6:
            continue
        segs.append((a, dx / seg_len, dy / seg_len, seg_len))
    if not segs:
        return

    def point_at(dist):
        """World-along-chain distance -> (x, y), clamped to the chain."""
        remaining = dist
        for a, ux, uy, seg_len in segs:
            if remaining <= seg_len:
                return (a[0] + ux * remaining, a[1] + uy * remaining)
            remaining -= seg_len
        last_a, ux, uy, seg_len = segs[-1]
        return (last_a[0] + ux * seg_len, last_a[1] + uy * seg_len)

    total = sum(seg_len for _a, _ux, _uy, seg_len in segs)
    pts, idx, cur = [], [], 0.0
    while cur < total:
        nxt = min(cur + dash_len, total)
        i = len(pts)
        pts.append(point_at(cur))
        pts.append(point_at(nxt))
        idx.append((i, i + 1))
        cur += dash_len + space_len
    if not pts:
        return

    gpu.state.blend_set('ALPHA')
    sh = _sh()
    b  = batch_for_shader(sh, 'LINES', {"pos": [list(p) for p in pts]}, indices=idx)
    sh.bind()
    sh.uniform_float("color", col)
    gpu.state.line_width_set(w)
    b.draw(sh)
    gpu.state.blend_set('NONE')



def gfx_dashed_arc(center, r, ang_start, ang_end,
                   col=(0.2, 0.9, 0.2, 0.6), w=1.5,
                   dash_len=4.0, space_len=4.0):
    """Draw a dashed arc in screen space.

    dash_len/space_len are in pixels, matching gfx_dashed_line's
    convention, so an arc's dash pattern reads consistently regardless
    of its radius instead of a fixed angular step blurring into a
    near-solid line at small radii.

    Builds explicit non-consecutive index pairs, same technique as
    gfx_dashed_line - passing a flat dash-point list straight to
    gfx_lines() connects every consecutive point (including the gaps
    between dashes), which is what silently rendered this as a solid
    arc rather than a dashed one.
    """
    diff = ang_end - ang_start
    if r <= 0 or abs(diff) < 1e-6:
        return
    arc_len = abs(diff) * r
    if arc_len < 1:
        return
    sign = 1.0 if diff >= 0 else -1.0
    unit = max(dash_len + space_len, 0.5)

    pts, idx, s = [], [], 0.0
    while s < arc_len:
        e = min(s + dash_len, arc_len)
        a0 = ang_start + sign * (s / r)
        a1 = ang_start + sign * (e / r)
        i = len(pts)
        pts.append((center[0] + math.cos(a0) * r, center[1] + math.sin(a0) * r))
        pts.append((center[0] + math.cos(a1) * r, center[1] + math.sin(a1) * r))
        idx.append((i, i + 1))
        s += unit
    if not pts:
        return

    gpu.state.blend_set('ALPHA')
    sh = _sh()
    b  = batch_for_shader(sh, 'LINES', {"pos": [list(p) for p in pts]}, indices=idx)
    sh.bind()
    sh.uniform_float("color", col)
    gpu.state.line_width_set(w)
    b.draw(sh)
    gpu.state.blend_set('NONE')


def gfx_arc_arrow(center, r, tip_angle, sweep_ccw,
                  col=(0.2, 0.95, 0.2, 0.85), w=1.5):
    """Draw an arrowhead at the tip of an arc annotation."""
    tip     = (center[0] + math.cos(tip_angle) * r,
               center[1] + math.sin(tip_angle) * r)
    tangent = tip_angle + (math.pi / 2 if sweep_ccw else -math.pi / 2)
    base    = tangent + math.pi
    spread  = math.radians(28)
    alen    = 7
    a1 = (tip[0] + math.cos(base + spread) * alen,
          tip[1] + math.sin(base + spread) * alen)
    a2 = (tip[0] + math.cos(base - spread) * alen,
          tip[1] + math.sin(base - spread) * alen)
    gfx_lines([a1, tip, a2], col, w)