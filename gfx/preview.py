# ─────────────────────────────────────────────────────────────────────
#  gfx/preview.py
#  Shared "anchor -> cursor" preview primitives.
#
#  Originally built for CAD_OT_DrawLine, and reused verbatim (not
#  reimplemented) by the Move, Stretch, and Rotate tools so every
#  transform tool's live preview looks and behaves identically: same
#  dashed reference ray, same dimension-line/label system, same green
#  Polar Tracking direction guide. There is exactly one implementation
#  of each; every tool calls into this module rather than keeping its
#  own copy.
# ─────────────────────────────────────────────────────────────────────

import math
from mathutils import Vector

from .primitives import gfx_dashed_line, gfx_dashed_arc
from .markers    import draw_vertex_square
from .dims       import draw_float_dim


def draw_reference_ray(base_2d, cur_2d, col=(0.90, 0.90, 0.90, 0.90), w=1.1):
    """Thin dashed line from the anchor/base point to the cursor -
    and *only* to the cursor, never extended beyond either end. Same
    dash pattern CAD_OT_DrawLine's dimension-preview extension lines
    use, so the anchor->cursor reference reads consistently across
    every tool that has one."""
    if base_2d is None or cur_2d is None:
        return
    gfx_dashed_line(base_2d, cur_2d, col=col, dash_len=1.5, space_len=3.5, w=w)


def draw_direction_guide(context, world_to_screen_fn, base_3d, cur_3d, cur_2d):
    """Green dashed ray starting at the cursor and extending onward
    toward infinity, away from the anchor point - CAD_OT_DrawLine's
    Polar Tracking guide, reused as-is by every other transform tool.

    The caller is responsible for only invoking this while Polar
    Tracking is actually engaged (i.e. its own polar_angle is not
    None) - the same gating the Line tool itself uses, so the guide
    only appears once the cursor has snapped to a configured
    increment.
    """
    if cur_2d is None:
        return
    delta = cur_3d - base_3d
    if delta.length <= 1e-4:
        return
    far_2d = world_to_screen_fn(context, cur_3d + delta.normalized() * 1000)
    if far_2d:
        gfx_dashed_line(
            cur_2d, far_2d,
            col=(0.2, 0.95, 0.2, 0.65),
            dash_len=6, space_len=5, w=1.2,
        )


def draw_dim_preview(base_2d, cur_2d, label_text, active=True, locked=False):
    """AutoCAD-style live dimension preview: dotted extension lines
    drop from the anchor and the cursor out to a dotted dimension line
    running parallel to the anchor->cursor segment, with small square
    end markers and the label centred on it. The offset scales with
    the on-screen distance (clamped for legibility on very short/long
    spans) exactly like CAD_OT_DrawLine's segment-length preview,
    which this *is* - Move/Stretch call it for their live distance
    readout rather than keeping a separate implementation.
    """
    if base_2d is None or cur_2d is None:
        return
    dx_s   = cur_2d[0] - base_2d[0]
    dy_s   = cur_2d[1] - base_2d[1]
    dist_s = math.hypot(dx_s, dy_s)
    if dist_s <= 1:
        return
    ux, uy = dx_s / dist_s, dy_s / dist_s
    perp_x, perp_y = -uy, ux

    # Offset scales with the on-screen distance instead of a fixed
    # pixel gap, clamped so it stays legible for very short/long spans.
    dim_off = max(24.0, min(dist_s * 0.18, 90.0))
    d1 = (base_2d[0] + perp_x * dim_off, base_2d[1] + perp_y * dim_off)
    d2 = (cur_2d[0]  + perp_x * dim_off, cur_2d[1]  + perp_y * dim_off)

    dim_col = (0.90, 0.90, 0.90, 0.90)
    # Extension lines: endpoint → dimension line
    gfx_dashed_line(base_2d, d1, col=dim_col, dash_len=1.5, space_len=3.5, w=1.1)
    gfx_dashed_line(cur_2d,  d2, col=dim_col, dash_len=1.5, space_len=3.5, w=1.1)
    # Dimension line itself, parallel to the anchor->cursor segment
    gfx_dashed_line(d1, d2,      col=dim_col, dash_len=1.5, space_len=3.5, w=1.1)
    # Small hollow square markers at each end, matching AutoCAD's
    # tracking-point glyphs
    draw_vertex_square(d1, 3, fill_col=None, outline_col=(1, 1, 1, 0.9), outline_w=1.3)
    draw_vertex_square(d2, 3, fill_col=None, outline_col=(1, 1, 1, 0.9), outline_w=1.3)

    mid_x = (d1[0] + d2[0]) / 2
    mid_y = (d1[1] + d2[1]) / 2
    draw_float_dim(mid_x, mid_y, label_text, active=active, locked=locked)


def draw_angle_guide(context, world_to_screen_fn, anchor_2d, anchor_3d, cur_2d,
                      angle_text, arc_col=(0.90, 0.90, 0.90, 0.85)):
    """AutoCAD-style live angle preview: a dashed 0°-reference baseline
    from the anchor point (world +X direction), a dashed arc sweeping
    from that baseline to the current cursor direction, and the live
    angle value in a box positioned along the arc's own bisector.

    Extracted from CAD_OT_DrawLine's angle annotation so every tool
    with an angle to show (currently Line and Mirror) calls the same
    implementation rather than keeping separate copies. Uses
    draw_float_dim for the value box - proven to render reliably,
    unlike a from-scratch tooltip implementation.
    """
    if anchor_2d is None or cur_2d is None:
        return

    ref_3d     = anchor_3d + Vector((1, 0, 0))
    ref_2d     = world_to_screen_fn(context, ref_3d)
    ref_screen = (
        math.atan2(ref_2d[1] - anchor_2d[1], ref_2d[0] - anchor_2d[0])
        if ref_2d else 0.0
    )
    tip_screen = math.atan2(cur_2d[1] - anchor_2d[1], cur_2d[0] - anchor_2d[0])

    dist_s = math.hypot(cur_2d[0] - anchor_2d[0], cur_2d[1] - anchor_2d[1])
    guide_len = max(60.0, min(dist_s, 260.0))
    ARC_R     = max(30.0, min(dist_s * 0.35, guide_len - 10.0))

    baseline_2d = (
        anchor_2d[0] + math.cos(ref_screen) * guide_len,
        anchor_2d[1] + math.sin(ref_screen) * guide_len,
    )
    draw_reference_ray(anchor_2d, baseline_2d)

    gfx_dashed_arc(
        anchor_2d, ARC_R, ref_screen, tip_screen,
        col=arc_col, w=1.3, dash_len=4.0, space_len=4.0,
    )

    # shortest-path bisector, robust to the ±π wrap
    diff = tip_screen - ref_screen
    diff = math.atan2(math.sin(diff), math.cos(diff))
    bisector = ref_screen + diff * 0.5
    label_r  = ARC_R + 18.0
    label_pos = (
        anchor_2d[0] + math.cos(bisector) * label_r,
        anchor_2d[1] + math.sin(bisector) * label_r,
    )
    draw_float_dim(label_pos[0], label_pos[1] + 11, angle_text, active=True)
