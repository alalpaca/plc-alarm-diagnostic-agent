"""
PLC Ladder Diagram SVG Renderer

Converts a PLC rule's condition_tree into an SVG ladder diagram that closely
resembles the display in Mitsubishi GX Works2/3.

Layout rules:
- AND nodes → horizontal series (contacts left to right)
- OR nodes → vertical parallel (branches stacked)
- NOT → negated contact (slash through)
- Device leaf → contact element with device name above, comment below
- Coil/output → right-aligned, format: (OUT T0 K1), (SET F67), (RST M4960)

SVG elements:
- Solid lines for all connections (no dashed lines)
- Left/right power rails (vertical lines)
- Contacts: ─┤ ├─ (NO) or ─┤/├─ (NC)
- Coils: ─( )─ with text inside
- Branch points: T-junctions with solid lines
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


# ============================================================
# Configuration
# ============================================================

CONTACT_WIDTH = 110      # Width of a contact element (px)
CONTACT_HEIGHT = 60      # Height of a contact element (px)
COMPARE_WIDTH = 150      # Width of a compare element (px)
COIL_WIDTH = 140         # Width of the coil/output element (px)
BRANCH_V_GAP = 8         # Vertical gap between parallel branches (px)
RAIL_MARGIN = 30         # Left/right margin for power rails (px)
LINE_Y = 30              # Y-offset of the horizontal wire within an element
FONT_DEVICE = 11         # Font size for device names
FONT_COMMENT = 9         # Font size for comments
FONT_COIL = 11           # Font size for coil text
LINE_WIDTH = 1.5         # Stroke width for wires
CONTACT_VLINE_H = 16     # Height of vertical bars in contacts
COLOR_LINE = "#000000"
COLOR_CONTACT = "#000000"
COLOR_COMMENT = "#666666"
COLOR_BACKGROUND = "#FFFFFF"
COLOR_RAIL = "#000000"


# ============================================================
# Layout Tree Data Structures
# ============================================================

@dataclass
class LayoutNode:
    """A node in the layout tree. Represents either a contact, a series
    group (AND), or a parallel group (OR)."""
    
    node_type: str  # "contact", "compare", "series", "parallel"
    
    # For contact/compare nodes
    device: str = ""
    comment: str = ""
    negated: bool = False
    edge_type: Optional[str] = None  # "rising", "falling"
    compare_op: str = ""       # For compare: ">", ">=", "<", etc.
    compare_args: list = field(default_factory=list)
    
    # For series/parallel nodes
    children: list = field(default_factory=list)
    
    # Computed layout (set during size computation)
    width: int = 0
    height: int = 0
    x: int = 0
    y: int = 0


@dataclass
class CoilInfo:
    """Information about the output coil."""
    device: str
    comment: str
    write_type: str       # OUT, SET, RST, MOV, DMOV, etc.
    extra_args: list = field(default_factory=list)


# ============================================================
# Build Layout Tree from condition_tree
# ============================================================

def build_layout_tree(condition_tree: dict, comments: dict) -> LayoutNode:
    """Convert a condition_tree dict into a LayoutNode tree for rendering."""
    if condition_tree is None:
        # No condition (unconditional write) - just a wire
        return LayoutNode(node_type="contact", device="(always)", comment="")
    
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
        # Format: "D7320 < R3612"
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
        # Flatten nested AND (AND(AND(a,b), c) → AND(a,b,c))
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
        # Flatten nested OR
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
            # If child is a single contact, just negate it
            if child.node_type == "contact":
                child.negated = not child.negated
                return child
            # Otherwise, wrap in a special "NOT block" - render as inverted contact group
            # For simplicity: mark first contact in series as negated indicator
            # In real GX Works this would be INV instruction
            # We'll render it as a series/parallel with a NOT label
            return child  # Simplified: just show the inner structure
        return LayoutNode(node_type="contact", device="(INV)", comment="Inverted")
    
    else:
        # Unknown type - render as generic contact
        return LayoutNode(node_type="contact", device=f"({node_type})", comment="")


# ============================================================
# Size Computation (Bottom-Up)
# ============================================================

def compute_sizes(node: LayoutNode) -> None:
    """Recursively compute width and height for each node."""
    if node.node_type in ("contact",):
        node.width = CONTACT_WIDTH
        node.height = CONTACT_HEIGHT
    
    elif node.node_type == "compare":
        node.width = COMPARE_WIDTH
        node.height = CONTACT_HEIGHT
    
    elif node.node_type == "series":
        # Horizontal: sum widths, max height
        total_w = 0
        max_h = 0
        for child in node.children:
            compute_sizes(child)
            total_w += child.width
            max_h = max(max_h, child.height)
        node.width = total_w
        node.height = max_h
    
    elif node.node_type == "parallel":
        # Vertical: max width, sum heights + gaps
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
        # Leaf node - position already set
        pass
    
    elif node.node_type == "series":
        # Children laid out left to right
        cx = x
        for child in node.children:
            # Vertically center child within parent's height
            cy = y + (node.height - child.height) // 2
            assign_positions(child, cx, cy)
            cx += child.width
    
    elif node.node_type == "parallel":
        # Children laid out top to bottom
        cy = y
        for child in node.children:
            assign_positions(child, x, cy)
            cy += child.height + BRANCH_V_GAP


# ============================================================
# SVG Drawing
# ============================================================

def render_ladder_svg(rule: dict, comments: dict) -> str:
    """
    Main entry point: render a PLC rule as an SVG ladder diagram.
    
    Args:
        rule: A rule dict with condition_tree, target_device, write_type, extra_args
        comments: Device → comment mapping
    
    Returns:
        Complete SVG string (embeddable in HTML)
    """
    condition_tree = rule.get("condition_tree")
    target_device = rule.get("target_device", "?")
    write_type = rule.get("write_type", "OUT")
    extra_args = rule.get("extra_args", [])
    
    # Build coil info
    coil = CoilInfo(
        device=target_device,
        comment=comments.get(target_device, ""),
        write_type=write_type,
        extra_args=extra_args,
    )
    
    # Build layout tree
    root = build_layout_tree(condition_tree, comments)
    
    # Compute sizes
    compute_sizes(root)
    
    # Total diagram dimensions
    total_width = RAIL_MARGIN + root.width + COIL_WIDTH + RAIL_MARGIN + 20
    total_height = max(root.height, CONTACT_HEIGHT) + 40  # Add padding top/bottom
    
    # Assign positions (start after left rail)
    assign_positions(root, RAIL_MARGIN + 10, 20)
    
    # Generate SVG
    svg_elements = []
    
    # Background
    svg_elements.append(
        f'<rect width="{total_width}" height="{total_height}" fill="{COLOR_BACKGROUND}"/>'
    )
    
    # Left power rail
    svg_elements.append(
        f'<line x1="{RAIL_MARGIN}" y1="0" x2="{RAIL_MARGIN}" y2="{total_height}" '
        f'stroke="{COLOR_RAIL}" stroke-width="2.5"/>'
    )
    
    # Right power rail
    right_rail_x = total_width - RAIL_MARGIN
    svg_elements.append(
        f'<line x1="{right_rail_x}" y1="0" x2="{right_rail_x}" y2="{total_height}" '
        f'stroke="{COLOR_RAIL}" stroke-width="2.5"/>'
    )
    
    # Draw the condition network
    # The wire enters from left rail and exits to coil
    wire_y = 20 + root.height // 2  # Main wire Y position (centered in root)
    
    # Connect left rail to root
    root_entry_x = root.x
    svg_elements.append(_svg_hline(RAIL_MARGIN, root_entry_x, wire_y))
    
    # Draw the root network
    exit_x, exit_y = _draw_node(root, svg_elements, wire_y)
    
    # Draw coil
    coil_x = total_width - RAIL_MARGIN - COIL_WIDTH
    
    # Connect network exit to coil
    svg_elements.append(_svg_hline(exit_x, coil_x, wire_y))
    
    # Draw the coil element
    _draw_coil(coil_x, wire_y, coil, svg_elements)
    
    # Connect coil to right rail
    svg_elements.append(_svg_hline(coil_x + COIL_WIDTH - 10, right_rail_x, wire_y))
    
    # Rule info label (top-left corner)
    rule_id = rule.get("rule_id", "")
    section = rule.get("section", "")
    step = rule.get("step", "")
    info_text = f"Step {step} | {section} | {rule_id}"
    svg_elements.append(
        f'<text x="{RAIL_MARGIN + 5}" y="{total_height - 5}" '
        f'font-size="8" fill="#999" font-family="monospace">{_escape(info_text)}</text>'
    )
    
    # Assemble SVG
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{total_width}" height="{total_height}" '
        f'viewBox="0 0 {total_width} {total_height}" '
        f'style="font-family: Consolas, monospace; background: {COLOR_BACKGROUND};">\n'
        + "\n".join(svg_elements)
        + "\n</svg>"
    )
    
    return svg


def _draw_node(node: LayoutNode, elements: list, parent_wire_y: int) -> tuple[int, int]:
    """
    Draw a layout node and return (exit_x, exit_y) where the wire comes out.
    """
    if node.node_type == "contact":
        return _draw_contact(node, elements)
    
    elif node.node_type == "compare":
        return _draw_compare(node, elements)
    
    elif node.node_type == "series":
        return _draw_series(node, elements, parent_wire_y)
    
    elif node.node_type == "parallel":
        return _draw_parallel(node, elements, parent_wire_y)
    
    return (node.x + node.width, node.y + LINE_Y)


def _draw_contact(node: LayoutNode, elements: list) -> tuple[int, int]:
    """Draw a single contact element (NO or NC)."""
    x = node.x
    y = node.y
    mid_y = y + LINE_Y  # Wire line Y
    
    # Left wire segment
    elements.append(_svg_hline(x, x + 25, mid_y))
    
    # Contact vertical bars
    bar_top = mid_y - CONTACT_VLINE_H // 2
    bar_bot = mid_y + CONTACT_VLINE_H // 2
    
    # Left bar
    elements.append(
        f'<line x1="{x+25}" y1="{bar_top}" x2="{x+25}" y2="{bar_bot}" '
        f'stroke="{COLOR_CONTACT}" stroke-width="{LINE_WIDTH}"/>'
    )
    # Right bar
    elements.append(
        f'<line x1="{x+75}" y1="{bar_top}" x2="{x+75}" y2="{bar_bot}" '
        f'stroke="{COLOR_CONTACT}" stroke-width="{LINE_WIDTH}"/>'
    )
    
    # Negated: diagonal slash
    if node.negated:
        elements.append(
            f'<line x1="{x+35}" y1="{bar_bot}" x2="{x+65}" y2="{bar_top}" '
            f'stroke="{COLOR_CONTACT}" stroke-width="{LINE_WIDTH}"/>'
        )
    
    # Edge detection markers
    if node.edge_type == "rising":
        # Small up arrow
        elements.append(
            f'<text x="{x+50}" y="{mid_y+4}" text-anchor="middle" '
            f'font-size="10" fill="{COLOR_CONTACT}">↑</text>'
        )
    elif node.edge_type == "falling":
        elements.append(
            f'<text x="{x+50}" y="{mid_y+4}" text-anchor="middle" '
            f'font-size="10" fill="{COLOR_CONTACT}">↓</text>'
        )
    
    # Right wire segment
    elements.append(_svg_hline(x + 75, x + CONTACT_WIDTH, mid_y))
    
    # Device name (above)
    elements.append(
        f'<text x="{x+50}" y="{mid_y - CONTACT_VLINE_H//2 - 4}" text-anchor="middle" '
        f'font-size="{FONT_DEVICE}" fill="{COLOR_LINE}" font-weight="bold">'
        f'{_escape(node.device)}</text>'
    )
    
    # Comment (below)
    if node.comment:
        # Truncate long comments
        cmt = node.comment[:18] + ".." if len(node.comment) > 20 else node.comment
        elements.append(
            f'<text x="{x+50}" y="{mid_y + CONTACT_VLINE_H//2 + 12}" text-anchor="middle" '
            f'font-size="{FONT_COMMENT}" fill="{COLOR_COMMENT}">'
            f'{_escape(cmt)}</text>'
        )
    
    return (x + CONTACT_WIDTH, mid_y)


def _draw_compare(node: LayoutNode, elements: list) -> tuple[int, int]:
    """Draw a comparison contact (e.g., D7320 >= R3612)."""
    x = node.x
    y = node.y
    mid_y = y + LINE_Y
    w = COMPARE_WIDTH
    
    # Left wire
    elements.append(_svg_hline(x, x + 20, mid_y))
    
    # Square bracket contact [ >= ]
    bx1, bx2 = x + 20, x + w - 20
    bar_top = mid_y - CONTACT_VLINE_H // 2
    bar_bot = mid_y + CONTACT_VLINE_H // 2
    
    elements.append(
        f'<rect x="{bx1}" y="{bar_top}" width="{bx2-bx1}" height="{bar_bot-bar_top}" '
        f'fill="none" stroke="{COLOR_CONTACT}" stroke-width="{LINE_WIDTH}"/>'
    )
    
    # Op text inside
    elements.append(
        f'<text x="{(bx1+bx2)//2}" y="{mid_y+4}" text-anchor="middle" '
        f'font-size="10" fill="{COLOR_CONTACT}">{_escape(node.compare_op)}</text>'
    )
    
    # Right wire
    elements.append(_svg_hline(bx2, x + w, mid_y))
    
    # Device/args above
    elements.append(
        f'<text x="{(bx1+bx2)//2}" y="{bar_top - 4}" text-anchor="middle" '
        f'font-size="{FONT_DEVICE}" fill="{COLOR_LINE}">{_escape(node.device)}</text>'
    )
    
    return (x + w, mid_y)


def _draw_series(node: LayoutNode, elements: list, parent_wire_y: int) -> tuple[int, int]:
    """Draw a series (AND) group: children left to right on same wire."""
    wire_y = node.y + node.height // 2
    
    last_exit_x = node.x
    last_exit_y = wire_y
    
    for child in node.children:
        # Connect from last exit to child entry
        child_entry_x = child.x
        child_wire_y = child.y + child.height // 2
        
        # If wire levels differ (due to parallel children having different centers)
        if last_exit_y != child_wire_y:
            # Draw connecting line
            elements.append(_svg_hline(last_exit_x, child_entry_x, wire_y))
        elif last_exit_x < child_entry_x:
            elements.append(_svg_hline(last_exit_x, child_entry_x, wire_y))
        
        # Draw the child
        exit_x, exit_y = _draw_node(child, elements, wire_y)
        last_exit_x = exit_x
        last_exit_y = exit_y
    
    return (last_exit_x, wire_y)


def _draw_parallel(node: LayoutNode, elements: list, parent_wire_y: int) -> tuple[int, int]:
    """Draw a parallel (OR) group: children stacked with branch lines."""
    x = node.x
    y = node.y
    
    # Branch entry/exit X positions
    branch_entry_x = x
    branch_exit_x = x + node.width
    
    # Collect wire Y positions for each branch
    branch_wires = []
    
    for child in node.children:
        child_wire_y = child.y + child.height // 2
        branch_wires.append(child_wire_y)
        
        # Draw connecting line from branch entry to child
        child_entry_x = child.x
        if branch_entry_x < child_entry_x:
            elements.append(_svg_hline(branch_entry_x, child_entry_x, child_wire_y))
        
        # Draw child
        exit_x, exit_y = _draw_node(child, elements, child_wire_y)
        
        # Draw connecting line from child exit to branch exit
        if exit_x < branch_exit_x:
            elements.append(_svg_hline(exit_x, branch_exit_x, child_wire_y))
    
    # Draw vertical branch lines (left side: entry junction)
    if len(branch_wires) > 1:
        min_y = min(branch_wires)
        max_y = max(branch_wires)
        # Left vertical
        elements.append(
            f'<line x1="{branch_entry_x}" y1="{min_y}" '
            f'x2="{branch_entry_x}" y2="{max_y}" '
            f'stroke="{COLOR_LINE}" stroke-width="{LINE_WIDTH}"/>'
        )
        # Right vertical
        elements.append(
            f'<line x1="{branch_exit_x}" y1="{min_y}" '
            f'x2="{branch_exit_x}" y2="{max_y}" '
            f'stroke="{COLOR_LINE}" stroke-width="{LINE_WIDTH}"/>'
        )
    
    # The output wire Y is the center of the first branch (top branch = main path)
    output_wire_y = branch_wires[0] if branch_wires else (y + node.height // 2)
    
    return (branch_exit_x, output_wire_y)


def _draw_coil(x: int, wire_y: int, coil: CoilInfo, elements: list) -> None:
    """Draw the output coil element."""
    # Coil circle/arc
    cx = x + COIL_WIDTH // 2
    cy = wire_y
    r = 12
    
    # Left wire to coil
    elements.append(_svg_hline(x, cx - r - 5, cy))
    
    # Draw parentheses as arcs
    # Left paren
    elements.append(
        f'<path d="M {cx-r} {cy-r} A {r} {r} 0 0 0 {cx-r} {cy+r}" '
        f'fill="none" stroke="{COLOR_CONTACT}" stroke-width="{LINE_WIDTH}"/>'
    )
    # Right paren
    elements.append(
        f'<path d="M {cx+r} {cy-r} A {r} {r} 0 0 1 {cx+r} {cy+r}" '
        f'fill="none" stroke="{COLOR_CONTACT}" stroke-width="{LINE_WIDTH}"/>'
    )
    
    # Coil text inside
    if coil.write_type == "OUT":
        if coil.extra_args:
            coil_text = f"{coil.write_type} {coil.device} {' '.join(coil.extra_args)}"
        else:
            coil_text = f"{coil.write_type} {coil.device}"
    else:
        coil_text = f"{coil.write_type} {coil.device}"
    
    elements.append(
        f'<text x="{cx}" y="{cy + 4}" text-anchor="middle" '
        f'font-size="{FONT_COIL}" fill="{COLOR_LINE}" font-weight="bold">'
        f'{_escape(coil_text)}</text>'
    )
    
    # Comment below coil
    if coil.comment:
        cmt = coil.comment[:22] + ".." if len(coil.comment) > 24 else coil.comment
        elements.append(
            f'<text x="{cx}" y="{cy + r + 14}" text-anchor="middle" '
            f'font-size="{FONT_COMMENT}" fill="{COLOR_COMMENT}">'
            f'{_escape(cmt)}</text>'
        )
    
    # Right wire from coil
    elements.append(_svg_hline(cx + r + 5, x + COIL_WIDTH - 10, cy))


# ============================================================
# SVG Primitives
# ============================================================

def _svg_hline(x1: int, x2: int, y: int) -> str:
    """Horizontal line."""
    if x2 <= x1:
        return ""
    return (
        f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" '
        f'stroke="{COLOR_LINE}" stroke-width="{LINE_WIDTH}"/>'
    )


def _escape(text: str) -> str:
    """Escape special XML characters."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


# ============================================================
# Convenience: render from rule_id
# ============================================================

def render_rule_by_id(rule_id: str, kb) -> str:
    """Render a ladder diagram SVG for a given rule_id."""
    rule = kb.get_rule_by_id(rule_id)
    if not rule:
        return f"<p>Rule '{rule_id}' not found.</p>"
    return render_ladder_svg(rule, kb.comments)
