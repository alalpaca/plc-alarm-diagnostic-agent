"""
PLC Ladder Diagram SVG Renderer

Converts a PLC rule's condition_tree into an SVG ladder diagram that closely
resembles the display in Mitsubishi GX Works2/3.

Layout rules:
- AND nodes → horizontal series (contacts left to right)
- OR nodes → vertical parallel (branches stacked with fork/join topology)
- NOT → negated contact (slash through)
- Device leaf → contact element: name above, symbol on wire, comment below
- Coil/output → right-aligned: (S)/(R)/() for SET/RST/OUT, [block] for Timer/MOV

Key rendering principles:
- Main horizontal wire is CONTINUOUS from left rail to right rail
- Contacts are "inserted" onto the wire (wire breaks at || bars only)
- Parallel branches fork DOWN from the main wire and rejoin (NOT closed rectangles)
- All elements snap to a fixed-width cell grid
- Text never overlaps with wire/symbols
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


# ============================================================
# Configuration
# ============================================================

CONTACT_WIDTH = 100      # Width of a contact cell (px)
CONTACT_HEIGHT = 68      # Height of a contact cell (px)
COMPARE_WIDTH = 140      # Width of a compare element (px)
COIL_WIDTH = 130         # Width of the coil/output element (px)
BRANCH_V_GAP = 12        # Vertical gap between parallel branches (px)
RAIL_MARGIN = 25         # Left/right margin for power rails (px)
LINE_Y = 36              # Y-offset of the horizontal wire within a cell
FONT_DEVICE = 10         # Font size for device names
FONT_COMMENT = 7.5       # Font size for comments
FONT_COIL = 10           # Font size for coil text
LINE_WIDTH = 1.0         # Stroke width for all wires/contacts
RAIL_LINE_WIDTH = 1.5    # Stroke width for power rails
CONTACT_VLINE_H = 14     # Height of vertical bars in contacts
CONTACT_GAP = 10         # Half-gap between the two vertical bars (total gap = 20px)
COLOR_LINE = "#000000"
COLOR_CONTACT = "#000000"
COLOR_COMMENT = "#555555"
COLOR_BACKGROUND = "#FFFFFF"
COLOR_RAIL = "#000000"
COLOR_TITLE_BG = "#F7F7F7"
COLOR_INSTR_BG = "#FAFAFA"

# Text positions relative to cell top
TEXT_NAME_Y = 14         # Device name baseline (above wire)
TEXT_COMMENT_Y = 54      # Comment first line baseline (below wire)
TEXT_COMMENT_LINE_H = 9  # Line height for wrapped comments
COMMENT_MAX_CHARS = 14   # Max chars per comment line
COMMENT_MAX_LINES = 2    # Max lines of comment


# ============================================================
# Layout Tree Data Structures
# ============================================================

@dataclass
class LayoutNode:
    """A node in the layout tree."""
    node_type: str  # "contact", "compare", "series", "parallel"
    
    # For contact/compare nodes
    device: str = ""
    comment: str = ""
    negated: bool = False
    edge_type: Optional[str] = None
    compare_op: str = ""
    compare_args: list = field(default_factory=list)
    
    # For series/parallel nodes
    children: list = field(default_factory=list)
    
    # Computed layout
    width: int = 0
    height: int = 0
    x: int = 0
    y: int = 0


@dataclass
class CoilInfo:
    """Information about the output coil/instruction."""
    device: str
    comment: str
    write_type: str
    extra_args: list = field(default_factory=list)
    is_instruction: bool = False  # True for Timer OUT, MOV, etc.


# ============================================================
# Build Layout Tree from condition_tree
# ============================================================

def build_layout_tree(condition_tree: dict, comments: dict) -> LayoutNode:
    """Convert a condition_tree dict into a LayoutNode tree for rendering."""
    if condition_tree is None:
        return LayoutNode(node_type="series", children=[])
    return _build_node(condition_tree, comments)


def _build_node(tree: dict, comments: dict) -> LayoutNode:
    """Recursively build layout nodes."""
    node_type = tree.get("type", "")
    
    if node_type == "device":
        device = tree.get("device", "")
        return LayoutNode(
            node_type="contact",
            device=device,
            comment=comments.get(device, ""),
            negated=tree.get("negated", False),
            edge_type=tree.get("edge_type"),
        )
    
    elif node_type == "compare":
        op = tree.get("op", "")
        args = tree.get("args", [])
        display = f"{' '.join(args)}" if args else op
        return LayoutNode(
            node_type="compare",
            device=display,
            comment=op,
            compare_op=op,
            compare_args=args,
        )
    
    elif node_type == "and":
        children = tree.get("children", [])
        child_nodes = [_build_node(c, comments) for c in children]
        flat = []
        for cn in child_nodes:
            if cn.node_type == "series":
                flat.extend(cn.children)
            else:
                flat.append(cn)
        return LayoutNode(node_type="series", children=flat)
    
    elif node_type == "or":
        children = tree.get("children", [])
        child_nodes = [_build_node(c, comments) for c in children]
        flat = []
        for cn in child_nodes:
            if cn.node_type == "parallel":
                flat.extend(cn.children)
            else:
                flat.append(cn)
        return LayoutNode(node_type="parallel", children=flat)
    
    elif node_type == "not":
        children = tree.get("children", [])
        if children:
            child = _build_node(children[0], comments)
            if child.node_type == "contact":
                child.negated = not child.negated
                return child
            return child
        return LayoutNode(node_type="contact", device="(INV)", comment="Inverted")
    
    else:
        return LayoutNode(node_type="contact", device=f"({node_type})", comment="")


# ============================================================
# Size Computation (Bottom-Up)
# ============================================================

def compute_sizes(node: LayoutNode) -> None:
    """Recursively compute width and height for each node."""
    if node.node_type == "contact":
        node.width = CONTACT_WIDTH
        node.height = CONTACT_HEIGHT
    
    elif node.node_type == "compare":
        node.width = COMPARE_WIDTH
        node.height = CONTACT_HEIGHT
    
    elif node.node_type == "series":
        total_w = 0
        max_h = 0
        for child in node.children:
            compute_sizes(child)
            total_w += child.width
            max_h = max(max_h, child.height)
        node.width = total_w
        node.height = max_h
    
    elif node.node_type == "parallel":
        max_w = 0
        total_h = 0
        for i, child in enumerate(node.children):
            compute_sizes(child)
            max_w = max(max_w, child.width)
            total_h += child.height
            if i > 0:
                total_h += BRANCH_V_GAP
        node.width = max_w
        node.height = total_h


# ============================================================
# Position Assignment (Top-Down)
# ============================================================

def assign_positions(node: LayoutNode, x: int, y: int) -> None:
    """Recursively assign x, y coordinates to each node."""
    node.x = x
    node.y = y
    
    if node.node_type in ("contact", "compare"):
        pass
    
    elif node.node_type == "series":
        cx = x
        for child in node.children:
            cy = y + (node.height - child.height) // 2
            assign_positions(child, cx, cy)
            cx += child.width
    
    elif node.node_type == "parallel":
        cy = y
        for child in node.children:
            assign_positions(child, x, cy)
            cy += child.height + BRANCH_V_GAP


# ============================================================
# SVG Drawing
# ============================================================

def render_ladder_svg(rule: dict, comments: dict) -> str:
    """Main entry point: render a PLC rule as an SVG ladder diagram."""
    condition_tree = rule.get("condition_tree")
    target_device = rule.get("target_device", "?")
    write_type = rule.get("write_type", "OUT")
    extra_args = rule.get("extra_args", [])
    
    # Determine output type
    is_instr = _is_instruction_block(write_type, target_device)
    coil = CoilInfo(
        device=target_device,
        comment=comments.get(target_device, ""),
        write_type=write_type,
        extra_args=extra_args,
        is_instruction=is_instr,
    )
    
    # Build layout tree
    root = build_layout_tree(condition_tree, comments)
    compute_sizes(root)
    
    # Dimensions
    title_h = 20
    pad = 12
    total_width = RAIL_MARGIN + root.width + COIL_WIDTH + RAIL_MARGIN + 20
    total_height = title_h + pad + max(root.height, CONTACT_HEIGHT) + pad
    
    # Assign positions
    assign_positions(root, RAIL_MARGIN + 8, title_h + pad)
    
    # Main wire Y (centered in root)
    wire_y = title_h + pad + root.height // 2 + (LINE_Y - CONTACT_HEIGHT // 2)
    # Simplified: use the first branch's wire Y
    wire_y = title_h + pad + LINE_Y
    
    # Generate SVG elements
    els = []
    
    # Background
    els.append(f'<rect width="{total_width}" height="{total_height}" fill="{COLOR_BACKGROUND}"/>')
    
    # Title bar
    rule_id = rule.get("rule_id", "")
    section = rule.get("section", "")
    step = rule.get("step", "")
    els.append(f'<rect x="0" y="0" width="{total_width}" height="{title_h}" fill="{COLOR_TITLE_BG}"/>')
    els.append(f'<line x1="0" y1="{title_h}" x2="{total_width}" y2="{title_h}" stroke="#DDD" stroke-width="0.5"/>')
    title_text = f"Step {step} | {section} | {rule_id}"
    els.append(f'<text x="{RAIL_MARGIN+2}" y="{title_h-5}" font-size="8" fill="#666">{_escape(title_text)}</text>')
    
    # Left power rail
    els.append(f'<line x1="{RAIL_MARGIN}" y1="{title_h}" x2="{RAIL_MARGIN}" y2="{total_height}" stroke="{COLOR_RAIL}" stroke-width="{RAIL_LINE_WIDTH}"/>')
    
    # Right power rail
    right_rail_x = total_width - RAIL_MARGIN
    els.append(f'<line x1="{right_rail_x}" y1="{title_h}" x2="{right_rail_x}" y2="{total_height}" stroke="{COLOR_RAIL}" stroke-width="{RAIL_LINE_WIDTH}"/>')
    
    # Connect left rail to root entry
    els.append(_svg_hline(RAIL_MARGIN, root.x, wire_y))
    
    # Draw the condition network
    exit_x, exit_y = _draw_node(root, els, wire_y)
    
    # Draw output
    coil_x = total_width - RAIL_MARGIN - COIL_WIDTH
    els.append(_svg_hline(exit_x, coil_x, wire_y))
    _draw_output(coil_x, wire_y, coil, els)
    els.append(_svg_hline(coil_x + COIL_WIDTH - 8, right_rail_x, wire_y))
    
    # Assemble SVG
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{total_width}" height="{total_height}" '
        f'viewBox="0 0 {total_width} {total_height}" '
        f'style="font-family: Consolas, monospace; background: {COLOR_BACKGROUND}; display:block;">\n'
        + "\n".join(els)
        + "\n</svg>"
    )
    return svg


def _is_instruction_block(write_type: str, target: str) -> bool:
    """Determine if output is instruction block vs coil."""
    if write_type == "OUT" and target and target[0] in ("T", "C"):
        return True
    if write_type in ("MOV", "DMOV", "PLS", "PLR", "D+", "D-", "INCP", "DECP",
                      "BIN", "BCD", "FMOV", "XCH", "BMOV"):
        return True
    return False


# ============================================================
# Node Drawing (recursive)
# ============================================================

def _draw_node(node: LayoutNode, elements: list, parent_wire_y: int) -> tuple[int, int]:
    """Draw a layout node, return (exit_x, exit_y)."""
    if node.node_type == "contact":
        return _draw_contact(node, elements, parent_wire_y)
    elif node.node_type == "compare":
        return _draw_compare(node, elements, parent_wire_y)
    elif node.node_type == "series":
        return _draw_series(node, elements, parent_wire_y)
    elif node.node_type == "parallel":
        return _draw_parallel(node, elements, parent_wire_y)
    return (node.x + node.width, parent_wire_y)


def _draw_contact(node: LayoutNode, elements: list, parent_wire_y: int) -> tuple[int, int]:
    """Draw a single contact: wire with bars inserted.
    
    Layout within cell:
      Name (above wire)
      ────┤ ├────  (wire with gap where bars are)
      Comment (below wire, small wrapped)
    """
    x = node.x
    y = node.y
    cx = x + CONTACT_WIDTH // 2   # Cell center x
    wy = parent_wire_y             # Wire Y (from parent, ensures alignment)
    
    # Left bar and right bar positions
    lbar_x = cx - CONTACT_GAP
    rbar_x = cx + CONTACT_GAP
    
    # Wire: left segment (from cell start to left bar)
    elements.append(_svg_hline(x, lbar_x, wy))
    # Wire: right segment (from right bar to cell end)
    elements.append(_svg_hline(rbar_x, x + CONTACT_WIDTH, wy))
    
    # Contact bars (vertical lines)
    bar_top = wy - CONTACT_VLINE_H // 2
    bar_bot = wy + CONTACT_VLINE_H // 2
    
    elements.append(
        f'<line x1="{lbar_x}" y1="{bar_top}" x2="{lbar_x}" y2="{bar_bot}" '
        f'stroke="{COLOR_CONTACT}" stroke-width="{LINE_WIDTH}"/>'
    )
    elements.append(
        f'<line x1="{rbar_x}" y1="{bar_top}" x2="{rbar_x}" y2="{bar_bot}" '
        f'stroke="{COLOR_CONTACT}" stroke-width="{LINE_WIDTH}"/>'
    )
    
    # Negated: slash from left-bottom to right-top ( / direction)
    if node.negated:
        elements.append(
            f'<line x1="{lbar_x+1}" y1="{bar_bot-1}" x2="{rbar_x-1}" y2="{bar_top+1}" '
            f'stroke="{COLOR_CONTACT}" stroke-width="{LINE_WIDTH}"/>'
        )
    
    # Edge detection
    if node.edge_type == "rising":
        elements.append(f'<text x="{cx}" y="{wy+3}" text-anchor="middle" font-size="7" fill="{COLOR_CONTACT}">↑</text>')
    elif node.edge_type == "falling":
        elements.append(f'<text x="{cx}" y="{wy+3}" text-anchor="middle" font-size="7" fill="{COLOR_CONTACT}">↓</text>')
    
    # Device name (above wire, centered in cell)
    elements.append(
        f'<text x="{cx}" y="{wy - CONTACT_VLINE_H//2 - 5}" text-anchor="middle" '
        f'font-size="{FONT_DEVICE}" font-weight="bold" fill="{COLOR_LINE}">{_escape(node.device)}</text>'
    )
    
    # Comment (below wire, wrapped, small)
    if node.comment:
        _draw_comment(elements, cx, wy + CONTACT_VLINE_H//2 + 10, node.comment)
    
    return (x + CONTACT_WIDTH, wy)


def _draw_compare(node: LayoutNode, elements: list, parent_wire_y: int) -> tuple[int, int]:
    """Draw a comparison element: rectangular box on wire."""
    x = node.x
    cx = x + COMPARE_WIDTH // 2
    wy = parent_wire_y
    
    # Wire segments
    bw = 50
    bx = cx - bw // 2
    elements.append(_svg_hline(x, bx, wy))
    elements.append(_svg_hline(bx + bw, x + COMPARE_WIDTH, wy))
    
    # Box
    bh = 14
    by = wy - bh // 2
    elements.append(
        f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" '
        f'fill="none" stroke="{COLOR_CONTACT}" stroke-width="{LINE_WIDTH}"/>'
    )
    
    # Op text inside
    elements.append(
        f'<text x="{cx}" y="{wy+3}" text-anchor="middle" font-size="8" fill="{COLOR_CONTACT}">{_escape(node.compare_op)}</text>'
    )
    
    # Device name above
    elements.append(
        f'<text x="{cx}" y="{wy - bh//2 - 5}" text-anchor="middle" '
        f'font-size="{FONT_DEVICE}" fill="{COLOR_LINE}">{_escape(node.device)}</text>'
    )
    
    return (x + COMPARE_WIDTH, wy)


def _draw_series(node: LayoutNode, elements: list, parent_wire_y: int) -> tuple[int, int]:
    """Draw series (AND): children left to right, all on same wire Y."""
    wy = parent_wire_y
    last_exit_x = node.x
    
    for child in node.children:
        # Connect gap if needed
        if last_exit_x < child.x:
            elements.append(_svg_hline(last_exit_x, child.x, wy))
        
        # Draw child (pass parent wire Y so all align)
        exit_x, _ = _draw_node(child, elements, wy)
        last_exit_x = exit_x
    
    return (last_exit_x, wy)


def _draw_parallel(node: LayoutNode, elements: list, parent_wire_y: int) -> tuple[int, int]:
    """Draw parallel (OR): fork-branch-join topology.
    
    Main wire continues at parent_wire_y.
    Branch lines fork DOWN from parent wire, each branch extends horizontally,
    then rejoins at the right side.
    
    Structure:
      ─────┬──[branch1]──┬─────
           │              │
           ├──[branch2]──┤
           │              │
           └──[branch3]──┘
    """
    fork_x = node.x                 # X where branches fork
    join_x = node.x + node.width    # X where branches rejoin
    
    # Calculate each branch's wire Y
    branch_wire_ys = []
    for child in node.children:
        # Each child's wire Y = its y position + LINE_Y offset
        child_wy = child.y + LINE_Y
        branch_wire_ys.append(child_wy)
    
    # The first (top) branch aligns with the parent wire
    # We need to adjust: first branch should be at parent_wire_y
    # Calculate offset
    if branch_wire_ys:
        offset = parent_wire_y - branch_wire_ys[0]
        branch_wire_ys = [bwy + offset for bwy in branch_wire_ys]
    
    # Draw each branch
    for i, child in enumerate(node.children):
        bwy = branch_wire_ys[i]
        
        # Horizontal wire from fork to child start
        child_start_x = child.x
        elements.append(_svg_hline(fork_x, child_start_x, bwy))
        
        # Draw child elements on this branch's wire Y
        exit_x, _ = _draw_node(child, elements, bwy)
        
        # Horizontal wire from child end to join point
        if exit_x < join_x:
            elements.append(_svg_hline(exit_x, join_x, bwy))
    
    # Vertical fork line (left side)
    if len(branch_wire_ys) > 1:
        y_top = branch_wire_ys[0]
        y_bot = branch_wire_ys[-1]
        elements.append(
            f'<line x1="{fork_x}" y1="{y_top}" x2="{fork_x}" y2="{y_bot}" '
            f'stroke="{COLOR_LINE}" stroke-width="{LINE_WIDTH}"/>'
        )
        # Vertical join line (right side)
        elements.append(
            f'<line x1="{join_x}" y1="{y_top}" x2="{join_x}" y2="{y_bot}" '
            f'stroke="{COLOR_LINE}" stroke-width="{LINE_WIDTH}"/>'
        )
    
    # Output at the first branch (main wire) Y
    return (join_x, parent_wire_y)


# ============================================================
# Output Drawing
# ============================================================

def _draw_output(x: int, wy: int, coil: CoilInfo, elements: list) -> None:
    """Draw right-side output element."""
    if coil.is_instruction:
        _draw_instr_block(x, wy, coil, elements)
    else:
        _draw_coil(x, wy, coil, elements)


def _draw_coil(x: int, wy: int, coil: CoilInfo, elements: list) -> None:
    """Draw coil-style output: ( ), (S), (R).
    
    The coil is two vertical arc lines (parentheses shape):
      Left arc:  )  curving right (open toward center)
      Right arc: (  curving left (open toward center)
    Wire connects to the midpoint of each arc from outside.
    
    Visual:  ─────)(─────
                  ^^ coil symbol (narrow, tall)
    """
    cx = x + COIL_WIDTH // 2
    h = 8   # Half-height of the coil arcs
    gap = 4  # Half-distance between the two arcs
    
    # Arc positions
    left_arc_x = cx - gap
    right_arc_x = cx + gap
    
    # Wire to left arc midpoint
    elements.append(_svg_hline(x, left_arc_x, wy))
    
    # Left arc: ) shape - curves to the RIGHT (opens toward center)
    # SVG arc from (left_arc_x, wy-h) curving right to (left_arc_x, wy+h)
    elements.append(
        f'<path d="M {left_arc_x} {wy-h} C {left_arc_x-5} {wy-h}, {left_arc_x-5} {wy+h}, {left_arc_x} {wy+h}" '
        f'fill="none" stroke="{COLOR_CONTACT}" stroke-width="{LINE_WIDTH}"/>'
    )
    
    # Right arc: ( shape - curves to the LEFT (opens toward center)
    # SVG arc from (right_arc_x, wy-h) curving left to (right_arc_x, wy+h)
    elements.append(
        f'<path d="M {right_arc_x} {wy-h} C {right_arc_x+5} {wy-h}, {right_arc_x+5} {wy+h}, {right_arc_x} {wy+h}" '
        f'fill="none" stroke="{COLOR_CONTACT}" stroke-width="{LINE_WIDTH}"/>'
    )
    
    # Inner label (S/R/empty) between the arcs
    inner = ""
    if coil.write_type == "SET":
        inner = "S"
    elif coil.write_type == "RST":
        inner = "R"
    if inner:
        elements.append(f'<text x="{cx}" y="{wy+3}" text-anchor="middle" font-size="8" font-weight="bold" fill="{COLOR_LINE}">{inner}</text>')
    
    # Wire from right arc midpoint
    elements.append(_svg_hline(right_arc_x, x + COIL_WIDTH - 8, wy))
    
    # Device name above
    elements.append(
        f'<text x="{cx}" y="{wy - h - 4}" text-anchor="middle" '
        f'font-size="{FONT_DEVICE}" font-weight="bold" fill="{COLOR_LINE}">{_escape(coil.device)}</text>'
    )
    
    # Comment below
    if coil.comment:
        _draw_comment(elements, cx, wy + h + 10, coil.comment)


def _draw_instr_block(x: int, wy: int, coil: CoilInfo, elements: list) -> None:
    """Draw instruction block: [OUT T0 K1], [MOV src dst]."""
    cx = x + COIL_WIDTH // 2
    bw = COIL_WIDTH - 24
    bh = 26
    bx = cx - bw // 2
    by = wy - bh // 2
    
    # Wire to block
    elements.append(_svg_hline(x, bx, wy))
    
    # Block rectangle
    elements.append(
        f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" '
        f'fill="{COLOR_INSTR_BG}" stroke="{COLOR_CONTACT}" stroke-width="{LINE_WIDTH}"/>'
    )
    
    # Text lines inside block
    line1 = coil.write_type
    if coil.extra_args:
        line2 = f"{coil.device} {' '.join(coil.extra_args)}"
    else:
        line2 = coil.device
    
    elements.append(f'<text x="{cx}" y="{wy-3}" text-anchor="middle" font-size="8" fill="{COLOR_LINE}">{_escape(line1)}</text>')
    elements.append(f'<text x="{cx}" y="{wy+8}" text-anchor="middle" font-size="{FONT_COIL}" font-weight="bold" fill="{COLOR_LINE}">{_escape(line2)}</text>')
    
    # Wire from block
    elements.append(_svg_hline(bx + bw, x + COIL_WIDTH - 8, wy))
    
    # Comment below block
    if coil.comment:
        _draw_comment(elements, cx, by + bh + 8, coil.comment)


# ============================================================
# Utility Functions
# ============================================================

def _draw_comment(elements: list, cx: int, start_y: int, comment: str) -> None:
    """Draw wrapped comment text (small font, multiple lines)."""
    if not comment:
        return
    lines = []
    remaining = comment
    for _ in range(COMMENT_MAX_LINES):
        if not remaining:
            break
        if len(remaining) <= COMMENT_MAX_CHARS:
            lines.append(remaining)
            break
        lines.append(remaining[:COMMENT_MAX_CHARS])
        remaining = remaining[COMMENT_MAX_CHARS:]
    
    for i, line in enumerate(lines):
        y = start_y + i * TEXT_COMMENT_LINE_H
        elements.append(
            f'<text x="{cx}" y="{y}" text-anchor="middle" '
            f'font-size="{FONT_COMMENT}" fill="{COLOR_COMMENT}">{_escape(line)}</text>'
        )


def _svg_hline(x1: int, x2: int, y: int) -> str:
    """Horizontal wire line."""
    if x2 <= x1:
        return ""
    return (
        f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" '
        f'stroke="{COLOR_LINE}" stroke-width="{LINE_WIDTH}"/>'
    )


def _escape(text: str) -> str:
    """Escape XML special characters."""
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ============================================================
# Convenience
# ============================================================

def render_rule_by_id(rule_id: str, kb) -> str:
    """Render a ladder diagram SVG for a given rule_id."""
    rule = kb.get_rule_by_id(rule_id)
    if not rule:
        return f"<p>Rule '{rule_id}' not found.</p>"
    return render_ladder_svg(rule, kb.comments)
