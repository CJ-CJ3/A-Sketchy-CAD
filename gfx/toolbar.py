# ─────────────────────────────────────────────────────────────────────
#  gfx/toolbar.py
#  Sketch-mode TOOLS-region draw callback + geometric tool icons
# ─────────────────────────────────────────────────────────────────────

import math
import bpy
from .primitives import (
    gfx_rect_fill, gfx_tri_fill, gfx_loop, gfx_lines, gfx_circle,
    gfx_dot, gfx_text, gfx_dashed_line,
    gfx_rounded_rect_fill, gfx_rounded_rect_loop,
)

# ── toolbar geometry constants ───────────────────────────────────────
_TB_W    = 96    # total toolbar width
_TB_BTN  = 80    # square button size  (matches reference photo)
_TB_PAD  = (_TB_W - _TB_BTN) // 2   # side padding = 8
_TB_SEP  = 6     # separator half-space
_TOP_PAD = 14    # gap at the very top of the panel
_BTN_R   = 10    # button corner radius (rounded look)

# ── tool definitions (order = top-to-bottom render order) ───────────
# "id"        = internal draw/icon key
# "blender_id"= workspace tool idname used for active-check & tool_set_by_id
_SKETCH_TOOLS = [
    {"id": "SELECT_BOX", "label": "Select Box",  "blender_id": "builtin.select_box"},
    {"id": "---"},
    {"id": "MOVE",       "label": "Move",        "blender_id": "builtin.move"},
    {"id": "SCALE",      "label": "Scale",       "blender_id": "builtin.scale"},
    {"id": "TRANSFORM",  "label": "Transform",   "blender_id": "builtin.transform"},
]

# ── colours ──────────────────────────────────────────────────────────
_BTN_BG      = (0.15, 0.15, 0.15, 1.0)   # idle  (#262626)
_BTN_BG_HOV  = (0.23, 0.23, 0.23, 1.0)
_BTN_BG_ACT  = (0.18, 0.18, 0.18, 1.0)   # active: subtle lift, no blue
_TOOLBAR_BG  = (0.14, 0.14, 0.14, 1.0)
_SEP_COL     = (0.27, 0.27, 0.27, 1.0)
_BTN_BORDER     = (0.23, 0.23, 0.23, 0.9)
_BTN_BORDER_ACT = (0.24, 0.24, 0.24, 1.0)


# ── Blender-style mint green for all icons ───────────────────────────
_ICON_COL       = (0.37, 0.92, 0.67, 1.0)   # active  (#5EEAAA)
_ICON_COL_IDLE  = (0.48, 0.48, 0.48, 1.0)   # idle    (#7a7a7a) — grey, not green


def _sq(pos, col, half=3):
    """Draw a small filled square vertex node (Blender Edit Mode style)."""
    gfx_rect_fill(int(pos[0]) - half, int(pos[1]) - half,
                  half * 2, half * 2, col)




# ════════════════════════════════════════════════════════════════════
#  TOOL ICONS  (geometric GPU drawings, one per tool)
# ════════════════════════════════════════════════════════════════════

def _draw_tool_icon(tool_id: str, bx: int, by: int,
                    btn_size: int, col: tuple):
    """Render a Blender Edit Mode-style geometric icon inside button (bx,by)."""
    cx = bx + btn_size // 2
    cy = by + btn_size // 2
    m  = btn_size // 2 - 4

    dim = (*col[:3], col[3] * 0.38)

    if tool_id == 'SELECT_BOX':
        # ── Dashed marquee rectangle (selection box) ──────────────────────────
        pad = 9
        x1, y1 = bx + pad,            by + pad
        x2, y2 = bx + btn_size - pad, by + btn_size - pad
        gfx_dashed_line((x1, y1), (x2, y1), col, dash_len=5, space_len=3, w=1.6)
        gfx_dashed_line((x2, y1), (x2, y2), col, dash_len=5, space_len=3, w=1.6)
        gfx_dashed_line((x2, y2), (x1, y2), col, dash_len=5, space_len=3, w=1.6)
        gfx_dashed_line((x1, y2), (x1, y1), col, dash_len=5, space_len=3, w=1.6)
        # small square nodes at two opposite corners
        _sq((x1, y1), col, 3)
        _sq((x2, y2), col, 3)

    elif tool_id == 'MOVE':
        # ── Four-arrow translate icon ──────────────────────────────────────────
        shaft = m - 2
        hw_   = 5
        hl_   = 7

        for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
            tip_x  = cx + dx * shaft
            tip_y  = cy + dy * shaft
            base_x = cx + dx * (shaft - hl_)
            base_y = cy + dy * (shaft - hl_)
            gfx_lines([(cx, cy), (base_x, base_y)], col, 2.5)
            px_, py_ = -dy, dx
            a1 = (base_x + px_ * hw_, base_y + py_ * hw_)
            a2 = (base_x - px_ * hw_, base_y - py_ * hw_)
            gfx_loop([(tip_x, tip_y), a1, a2], col, 2.2)

        _sq((cx, cy), col, 3)

    elif tool_id == 'SCALE':
        # ── Four diagonal outward arrows (scale icon) ─────────────────────────
        hw_ = 4
        hl_ = 6
        shaft = m - 3

        for dx, dy in [(1, 1), (-1, 1), (1, -1), (-1, -1)]:
            length = shaft / math.sqrt(2)
            tip_x = cx + dx * length
            tip_y = cy + dy * length
            base_x = cx + dx * (length - hl_)
            base_y = cy + dy * (length - hl_)
            gfx_lines([(cx, cy), (base_x, base_y)], col, 2.0)
            # perpendicular to diagonal direction
            px_, py_ = -dy / math.sqrt(2), dx / math.sqrt(2)
            a1 = (base_x + px_ * hw_, base_y + py_ * hw_)
            a2 = (base_x - px_ * hw_, base_y - py_ * hw_)
            gfx_loop([(tip_x, tip_y), a1, a2], col, 1.8)

        _sq((cx, cy), col, 3)

    elif tool_id == 'TRANSFORM':
        # ── Axis widget: bold X/Y arrows + arc hint (move+rotate+scale) ───────
        shaft = m - 2
        hw_   = 5
        hl_   = 7

        # Horizontal (X) arrow — right only, bright
        tip_x = cx + shaft
        base_x = cx + shaft - hl_
        gfx_lines([(cx, cy), (base_x, cy)], col, 2.5)
        gfx_loop([(tip_x, cy), (base_x, cy + hw_), (base_x, cy - hw_)], col, 2.0)

        # Vertical (Y) arrow — up only, bright
        tip_y = cy + shaft
        base_y = cy + shaft - hl_
        gfx_lines([(cx, cy), (cx, base_y)], col, 2.5)
        gfx_loop([(cx, tip_y), (cx - hw_, base_y), (cx + hw_, base_y)], col, 2.0)

        # Dim negative-axis stubs
        gfx_lines([(cx, cy), (cx - shaft // 2, cy)], dim, 1.5)
        gfx_lines([(cx, cy), (cx, cy - shaft // 2)], dim, 1.5)

        # Small arc to hint at rotation component
        gfx_circle((cx, cy), shaft // 2 + 2, dim, segs=24, w=1.2)

        _sq((cx, cy), col, 3)



# ════════════════════════════════════════════════════════════════════
#  HIT-TEST  (toolbar button y-ranges for mouse-over / click)
# ════════════════════════════════════════════════════════════════════

def sketch_toolbar_tool_y_ranges(region_h: int):
    """
    Return a list of (tool_dict, y_bottom, y_top) for mouse hit-testing.
    Separators are skipped; their vertical space is consumed.
    """
    result = []
    y = region_h - _TOP_PAD
    for tool in _SKETCH_TOOLS:
        if tool["id"] == "---":
            y -= (_TB_SEP * 2 + 1)
            continue
        y -= _TB_BTN
        result.append((tool, y, y + _TB_BTN))
        y -= 3
    return result


# ════════════════════════════════════════════════════════════════════
#  DRAW CALLBACK  (registered as POST_PIXEL in the TOOLS region)
# ════════════════════════════════════════════════════════════════════

def draw_sketch_toolbar_callback():
    """POST_PIXEL draw callback for the TOOLS region."""
    import bpy
    from .. import state as _st

    context = bpy.context
    s = getattr(context.scene, 'cad_settings', None)
    if not s or not s.sketch_mode:
        return
    if _st.sketch_area_ref and context.area != _st.sketch_area_ref:
        return

    W = context.region.width
    H = context.region.height

    # solid background (covers default Blender tools)
    gfx_rect_fill(0, 0, W, H, _TOOLBAR_BG)

    # "SKETCH" label at the very top
    gfx_text("SKETCH", 5, H - _TOP_PAD + 2, 9, (0.55, 0.55, 0.55, 0.9))

    # active tool from Blender's workspace tool system
    try:
        active_blender_id = context.workspace.tools.from_space_view3d_mode(
            context.mode
        ).idname
    except Exception:
        active_blender_id = ""

    y = H - _TOP_PAD - 12

    for tool in _SKETCH_TOOLS:
        if tool["id"] == "---":
            mid = y - _TB_SEP
            gfx_lines([(4, mid), (W - 4, mid)], _SEP_COL, 1.0)
            y -= (_TB_SEP * 2 + 1)
            continue

        bx = _TB_PAD
        by = y - _TB_BTN

        is_active  = (active_blender_id == tool.get("blender_id", ""))
        bg         = _BTN_BG_ACT if is_active else _BTN_BG
        border_col = _BTN_BORDER_ACT if is_active else _BTN_BORDER

        gfx_rounded_rect_fill(bx, by, _TB_BTN, _TB_BTN, _BTN_R, bg)
        gfx_rounded_rect_loop(bx, by, _TB_BTN, _TB_BTN, _BTN_R, border_col, 1.0)

        icon_col = (_ICON_COL if is_active else _ICON_COL_IDLE)
        _draw_tool_icon(tool["id"], bx, by, _TB_BTN, icon_col)

        y -= (_TB_BTN + 3)