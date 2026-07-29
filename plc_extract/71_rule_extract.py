"""
71_rule_extract.py - PLC Alarm Logic Extractor & Backward Tracer

Parses Mitsubishi Q-series PLC ladder logic exported as CSV (from GX Works2),
extracts alarm rules with proper stack simulation (MPS/MRD/MPP), builds a
device dependency graph, and generates multi-level backward traces for all
alarm (F-device) signals.

Output files (in ./plc_knowledge_out/):
  - rules.jsonl         : Every write-operation rule with full condition tree
  - edges.jsonl         : Causal edges between devices (from -> to)
  - devices.jsonl       : Device catalog with categories and references
  - sections.jsonl      : Program sections (### markers) with step ranges
  - alarm_trace.jsonl   : Recursive backward trace tree for each F-device

Usage:
  python 71_rule_extract.py
"""

import csv
import json
import re
import copy
from pathlib import Path
from collections import defaultdict

# ===========================================================================
# Configuration
# ===========================================================================

INPUT_CSV = "plc_file/000.csv"
PROGRAM_NO = "000"
PROGRAM_NAME = "WH201_CG1_Main"
OUTPUT_DIR = Path("plc_knowledge_out_000")
MAX_TRACE_DEPTH = 10

# ===========================================================================
# Instruction classification
# ===========================================================================

# Instructions that write to a target device
WRITE_OPS = {
    "SET", "RST", "OUT",
    "MOV", "DMOV", "BMOV", "FMOV",
    "DINCP", "DDECP", "INCP", "DECP",
    "D+", "D-", "+", "-",
}

# Instructions that load/start a new condition path
LOAD_OPS = {"LD", "LDI", "LDP", "LDF", "LDPI", "LDFI"}

# Instructions that add series conditions
AND_OPS = {
    "AND", "ANI", "ANDP", "ANDF", "ANDPI", "ANDFI",
    "AND=", "AND<>", "AND>", "AND<", "AND>=", "AND<=",
    "ANDD=", "ANDD<>", "ANDD>", "ANDD<", "ANDD>=", "ANDD<=",
}

# Instructions that add parallel conditions
OR_OPS = {
    "OR", "ORI", "ORP", "ORF", "ORPI", "ORFI",
    "OR=", "OR<>", "OR>", "OR<", "OR>=", "OR<=",
}

# Comparison-style load instructions
LOAD_CMP_OPS = {
    "LD=", "LD<>", "LD>", "LD<", "LD>=", "LD<=",
    "LDD=", "LDD<>", "LDD>", "LDD<", "LDD>=", "LDD<=",
}

# Stack operations
STACK_OPS = {"MPS", "MRD", "MPP"}

# Block logic operations
BLOCK_OPS = {"ANB", "ORB"}

# Inversion
INV_OPS = {"INV"}

# Edge pulse
EDGE_OPS = {"MEP", "MEF"}

# Timer/Counter output (treated as write with constant parameter)
TIMER_COUNTER_OPS = {"OUT"}

# All condition-contributing ops (for upstream device extraction)
ALL_COND_OPS = LOAD_OPS | AND_OPS | OR_OPS | LOAD_CMP_OPS

# ===========================================================================
# CSV Parsing
# ===========================================================================


def detect_encoding(path):
    """Try multiple encodings to read the CSV file."""
    encodings = ["utf-16", "utf-16-le", "utf-8-sig", "utf-8", "shift-jis", "gbk", "cp1252"]
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc) as f:
                lines = f.readlines()
            # Verify we got meaningful content
            if lines and ("步号" in lines[0] or "步号" in "".join(lines[:5]) or
                         "PLC" in "".join(lines[:5]) or "WH" in "".join(lines[:5])):
                return enc, lines
        except (UnicodeDecodeError, UnicodeError):
            continue
    # Fallback: read as binary and decode
    raise RuntimeError(f"Cannot detect encoding for {path}")


def parse_csv_lines(lines):
    """Parse tab-separated CSV lines into structured rows."""
    raw_rows = []
    for line in lines:
        line = line.rstrip("\n").rstrip("\r")
        if not line:
            continue
        # Split by tab, strip surrounding quotes
        parts = line.split("\t")
        parts = [p.strip().strip('"') for p in parts]
        raw_rows.append(parts)
    return raw_rows


def merge_continuation_rows(raw_rows):
    """
    Merge continuation rows (rows with empty step number) into the previous
    instruction as additional arguments.
    """
    merged = []
    for row in raw_rows:
        step_str = row[0] if row else ""
        # Check if this is a continuation line (empty step number, has arg in col 3)
        if step_str == "" and len(row) > 3 and row[3]:
            # This is a continuation - append arg to previous instruction
            if merged:
                merged[-1]["extra_args"].append(row[3])
        else:
            # Try to parse as a normal instruction row
            try:
                step = int(step_str)
            except (ValueError, IndexError):
                # Non-instruction row (header, metadata, etc.)
                merged.append({
                    "step": None,
                    "declaration": row[1] if len(row) > 1 else "",
                    "op": row[2] if len(row) > 2 else "",
                    "arg": row[3] if len(row) > 3 else "",
                    "extra_args": [],
                    "raw": row
                })
                continue

            merged.append({
                "step": step,
                "declaration": row[1].strip() if len(row) > 1 else "",
                "op": row[2].strip().upper() if len(row) > 2 else "",
                "arg": row[3].strip().upper() if len(row) > 3 else "",
                "extra_args": [],
                "raw": row
            })
    return merged


def extract_sections(merged_rows):
    """Extract program sections from ### markers in declarations."""
    sections = []
    for row in merged_rows:
        decl = row.get("declaration", "")
        if decl.startswith("###"):
            name = decl.lstrip("#").strip()
            step = row.get("step")
            sections.append({
                "name": name,
                "step_start": step,
                "step_end": None,  # filled later
                "alarms": []
            })
    # Fill step_end
    for i in range(len(sections) - 1):
        sections[i]["step_end"] = sections[i + 1]["step_start"] - 1
    if sections:
        sections[-1]["step_end"] = 99999  # last section extends to end
    return sections


def get_section_for_step(sections, step):
    """Find which section a given step belongs to."""
    if step is None:
        return None
    for sec in sections:
        if sec["step_start"] is not None and sec["step_end"] is not None:
            if sec["step_start"] <= step <= sec["step_end"]:
                return sec["name"]
    return None


def build_instruction_list(merged_rows):
    """Filter to only valid instruction rows (with step numbers and ops)."""
    instructions = []
    for row in merged_rows:
        if row["step"] is not None and row["op"]:
            instructions.append(row)
    return instructions


# ===========================================================================
# Stack Simulator & Rule Extraction
# ===========================================================================


class ConditionNode:
    """Represents a condition in the logic tree."""

    def __init__(self, node_type, device=None, negated=False, edge_type=None,
                 children=None, op=None, args=None):
        self.node_type = node_type  # "device", "and", "or", "not", "compare"
        self.device = device
        self.negated = negated
        self.edge_type = edge_type  # "rising", "falling", None
        self.children = children or []
        self.op = op  # original op for compare nodes
        self.args = args or []  # for compare: [arg1, arg2]

    def to_dict(self):
        d = {"type": self.node_type}
        if self.device:
            d["device"] = self.device
        if self.negated:
            d["negated"] = True
        if self.edge_type:
            d["edge_type"] = self.edge_type
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        if self.op:
            d["op"] = self.op
        if self.args:
            d["args"] = self.args
        return d

    def get_devices(self):
        """Recursively collect all device references."""
        devices = set()
        if self.device:
            devices.add(self.device)
        for arg in self.args:
            if arg and not arg.startswith("K") and not arg.startswith("H"):
                devices.add(arg)
        for child in self.children:
            devices.update(child.get_devices())
        return devices

    def summarize(self):
        """Generate a human-readable condition summary."""
        if self.node_type == "device":
            prefix = "NOT " if self.negated else ""
            suffix = ""
            if self.edge_type == "rising":
                suffix = "↑"
            elif self.edge_type == "falling":
                suffix = "↓"
            return f"{prefix}{self.device}{suffix}"
        elif self.node_type == "compare":
            return f"({' '.join(self.args)} [{self.op}])"
        elif self.node_type == "and":
            parts = [c.summarize() for c in self.children]
            return " AND ".join(parts)
        elif self.node_type == "or":
            parts = [c.summarize() for c in self.children]
            return "(" + " OR ".join(parts) + ")"
        elif self.node_type == "not":
            if self.children:
                return f"NOT({self.children[0].summarize()})"
            return "NOT(?)"
        return "?"


def make_device_node(device, negated=False, edge_type=None):
    return ConditionNode("device", device=device, negated=negated, edge_type=edge_type)


def make_compare_node(op, args):
    return ConditionNode("compare", op=op, args=args)


def and_combine(a, b):
    """Combine two condition nodes with AND logic."""
    if a is None:
        return b
    if b is None:
        return a
    # Flatten nested ANDs
    children_a = a.children if a.node_type == "and" else [a]
    children_b = b.children if b.node_type == "and" else [b]
    return ConditionNode("and", children=children_a + children_b)


def or_combine(a, b):
    """Combine two condition nodes with OR logic."""
    if a is None:
        return b
    if b is None:
        return a
    children_a = a.children if a.node_type == "or" else [a]
    children_b = b.children if b.node_type == "or" else [b]
    return ConditionNode("or", children=children_a + children_b)


def extract_rules_with_stack(instructions, sections):
    """
    Main rule extraction engine with proper stack simulation.

    Processes instructions sequentially, maintaining:
    - current_condition: the accumulated condition for the current path
    - operand_stack: for LD/ANB/ORB block operations
    - branch_stack: for MPS/MRD/MPP branching

    When a write operation is encountered, the current_condition is captured
    as the rule's prerequisite.
    """
    rules = []
    current_condition = None
    operand_stack = []    # Stack for LD-block operations (ANB/ORB)
    branch_stack = []     # Stack for MPS/MRD/MPP branching
    network_start_step = None
    inv_flag = False      # INV inverts the next condition check

    for idx, instr in enumerate(instructions):
        op = instr["op"]
        arg = instr["arg"] if instr["arg"] else None
        extra_args = instr.get("extra_args", [])
        step = instr["step"]

        # --- LOAD operations: start a new condition path ---
        if op in LOAD_OPS:
            # If we already have a condition on the operand stack concept,
            # push current to operand_stack (for later ANB/ORB)
            if current_condition is not None:
                operand_stack.append(current_condition)

            negated = op in {"LDI", "LDFI", "LDPI"}
            edge = None
            if op in {"LDP", "LDPI"}:
                edge = "rising"
            elif op in {"LDF", "LDFI"}:
                edge = "falling"

            if arg:
                current_condition = make_device_node(arg, negated=negated, edge_type=edge)
            else:
                current_condition = None

            if network_start_step is None:
                network_start_step = step

        elif op in LOAD_CMP_OPS:
            # Comparison load: LD>, LD<, LD>=, etc.
            if current_condition is not None:
                operand_stack.append(current_condition)

            all_args = [arg] + extra_args if arg else extra_args
            current_condition = make_compare_node(op, all_args)

            if network_start_step is None:
                network_start_step = step

        # --- AND operations: series condition ---
        elif op in AND_OPS:
            negated = op in {"ANI", "ANDFI", "ANDPI"}
            edge = None
            if op in {"ANDP", "ANDPI"}:
                edge = "rising"
            elif op in {"ANDF", "ANDFI"}:
                edge = "falling"

            if op.startswith("AND") and any(c in op for c in "=<>"):
                # Comparison AND: AND<, AND>=, ANDD>, etc.
                all_args = [arg] + extra_args if arg else extra_args
                new_node = make_compare_node(op, all_args)
            else:
                if arg:
                    new_node = make_device_node(arg, negated=negated, edge_type=edge)
                else:
                    new_node = None

            if new_node:
                current_condition = and_combine(current_condition, new_node)

        # --- OR operations: parallel condition ---
        elif op in OR_OPS:
            negated = op in {"ORI", "ORFI", "ORPI"}
            edge = None
            if op in {"ORP", "ORPI"}:
                edge = "rising"
            elif op in {"ORF", "ORFI"}:
                edge = "falling"

            if op.startswith("OR") and any(c in op for c in "=<>"):
                all_args = [arg] + extra_args if arg else extra_args
                new_node = make_compare_node(op, all_args)
            else:
                if arg:
                    new_node = make_device_node(arg, negated=negated, edge_type=edge)
                else:
                    new_node = None

            if new_node:
                current_condition = or_combine(current_condition, new_node)

        # --- Block operations ---
        elif op == "ANB":
            # AND-block: pop top of operand stack, AND with current
            if operand_stack:
                prev = operand_stack.pop()
                current_condition = and_combine(prev, current_condition)

        elif op == "ORB":
            # OR-block: pop top of operand stack, OR with current
            if operand_stack:
                prev = operand_stack.pop()
                current_condition = or_combine(prev, current_condition)

        # --- Stack operations (branching) ---
        elif op == "MPS":
            # Push current condition snapshot to branch stack
            branch_stack.append(copy.deepcopy(current_condition))

        elif op == "MRD":
            # Restore from branch stack top (don't pop)
            if branch_stack:
                current_condition = copy.deepcopy(branch_stack[-1])
                inv_flag = False

        elif op == "MPP":
            # Restore from branch stack top and pop
            if branch_stack:
                current_condition = branch_stack.pop()
                inv_flag = False

        # --- INV: invert current result ---
        elif op == "INV":
            # INV inverts the current accumulated result immediately
            if current_condition:
                current_condition = ConditionNode("not", children=[current_condition])
            inv_flag = False

        # --- Edge pulse detection ---
        elif op in EDGE_OPS:
            # MEP/MEF: modify current condition to be edge-triggered
            # This applies to whatever follows, we handle by setting a flag
            pass

        # --- WRITE operations: capture rule ---
        elif op in WRITE_OPS:
            if arg:
                # For timer OUT (e.g., OUT T641 K50), the arg is the timer device
                # extra_args[0] would be the constant (K50)
                target = arg
                write_type = op

                # Build the condition that was accumulated
                condition = current_condition

                # Extract all upstream devices from the condition tree
                upstream_devices = []
                if condition:
                    upstream_devices = sorted(condition.get_devices())
                    # Remove constants (Kxxx, Hxxx) from upstream
                    upstream_devices = [d for d in upstream_devices
                                        if not d.startswith("K") and not d.startswith("H")]

                section_name = get_section_for_step(sections, step)

                rule = {
                    "rule_id": f"{PROGRAM_NO}-{step}-{write_type}-{target}",
                    "program_no": PROGRAM_NO,
                    "program_name": PROGRAM_NAME,
                    "section": section_name,
                    "target_device": target,
                    "target_type": parse_device_type(target),
                    "write_type": write_type,
                    "step": step,
                    "network_start_step": network_start_step,
                    "condition_tree": condition.to_dict() if condition else None,
                    "condition_summary": condition.summarize() if condition else "",
                    "upstream_devices": upstream_devices,
                    "extra_args": extra_args,
                }
                rules.append(rule)

                # After a write in a branch, DON'T reset the condition;
                # the next MRD/MPP will restore it.
                # But if we're not in a branch context, we might need to continue.
                # The key insight: after SET/RST, the scan continues at the same
                # branch level. The condition doesn't change until MRD/MPP/new LD.

        elif op == "END":
            # Program end
            break

        else:
            # Unknown op - skip
            pass

    return rules


# ===========================================================================
# Device Parsing Utilities
# ===========================================================================

DEVICE_RE = re.compile(r"^([A-Z]+)(\d+(?:Z\d+)?)$", re.IGNORECASE)


def parse_device_type(dev):
    """Extract device type prefix from device string."""
    if not dev:
        return None
    dev = dev.upper()
    m = DEVICE_RE.match(dev)
    if m:
        return m.group(1)
    # Handle special cases like ZR110660
    for prefix in ["ZR", "SM", "SD", "SW"]:
        if dev.startswith(prefix):
            return prefix
    # Single letter prefix
    if dev and dev[0].isalpha():
        i = 0
        while i < len(dev) and dev[i].isalpha():
            i += 1
        return dev[:i] if i > 0 else None
    return None


def infer_category(device_type):
    """Infer device category from its type prefix."""
    categories = {
        "F": "alarm_flag",
        "X": "field_input",
        "Y": "field_output",
        "M": "internal_flag",
        "D": "data_register",
        "R": "file_register",
        "ZR": "indexed_register",
        "T": "timer",
        "C": "counter",
        "SM": "system_special_relay",
        "SD": "system_special_register",
        "L": "link_relay",
        "B": "link_relay",
        "W": "link_register",
        "K": "constant",
        "H": "hex_constant",
        "Z": "index_register",
    }
    return categories.get(device_type, "unknown")


# ===========================================================================
# Graph Building
# ===========================================================================


def build_edges(rules):
    """Build causal edges from rules: each upstream device -> target device."""
    edges = []
    seen = set()

    for rule in rules:
        target = rule["target_device"]
        write_type = rule["write_type"]

        edge_type_map = {
            "SET": "sets",
            "RST": "resets",
            "OUT": "drives",
            "MOV": "writes_data",
            "DMOV": "writes_data",
            "BMOV": "writes_data",
            "FMOV": "writes_data",
            "DINCP": "increments",
            "INCP": "increments",
            "D+": "adds_to",
            "D-": "subtracts_from",
        }
        edge_type = edge_type_map.get(write_type, "writes")

        for src in rule["upstream_devices"]:
            key = (src, target, rule["rule_id"])
            if key not in seen:
                seen.add(key)
                edges.append({
                    "from": src,
                    "to": target,
                    "edge_type": edge_type,
                    "write_op": write_type,
                    "rule_id": rule["rule_id"],
                    "section": rule["section"],
                    "step": rule["step"],
                })
    return edges


def build_devices(rules, sections):
    """Build device catalog from all rules."""
    device_map = {}

    def ensure_device(dev, section, role):
        if not dev or dev.startswith("K") or dev.startswith("H"):
            return
        if dev not in device_map:
            dtype = parse_device_type(dev)
            device_map[dev] = {
                "device": dev,
                "device_type": dtype,
                "category": infer_category(dtype) if dtype else "unknown",
                "sections": [],
                "roles": set(),
                "written_by_count": 0,
                "read_by_count": 0,
            }
        if section and section not in device_map[dev]["sections"]:
            device_map[dev]["sections"].append(section)
        device_map[dev]["roles"].add(role)

    for rule in rules:
        target = rule["target_device"]
        if target and not target.startswith("K") and not target.startswith("H"):
            ensure_device(target, rule["section"], "target")
            device_map[target]["written_by_count"] += 1
        for src in rule["upstream_devices"]:
            ensure_device(src, rule["section"], "condition")
            if src in device_map:
                device_map[src]["read_by_count"] += 1

    # Convert sets to lists for JSON serialization
    result = []
    for dev_info in device_map.values():
        dev_info["roles"] = sorted(dev_info["roles"])
        result.append(dev_info)

    return sorted(result, key=lambda x: x["device"])


# ===========================================================================
# Alarm Backward Trace
# ===========================================================================


def build_write_index(rules):
    """Build index: device -> list of rules that write to it."""
    index = defaultdict(list)
    for rule in rules:
        index[rule["target_device"]].append(rule)
    return index


def trace_alarm(alarm_device, write_index, depth=0, visited=None, max_depth=MAX_TRACE_DEPTH):
    """
    Recursively trace backward from an alarm device to find all upstream causes.

    Returns a trace tree structure showing the full causal chain.
    """
    if visited is None:
        visited = set()

    if depth >= max_depth:
        return {
            "device": alarm_device,
            "depth": depth,
            "terminal": True,
            "terminal_reason": "max_depth_reached",
        }

    if alarm_device in visited:
        return {
            "device": alarm_device,
            "depth": depth,
            "terminal": True,
            "terminal_reason": "circular_reference",
        }

    visited = visited | {alarm_device}  # New set to allow branching

    # Find all rules that write to this device
    writing_rules = write_index.get(alarm_device, [])

    if not writing_rules:
        # No rules write to this device - it's an external input
        return {
            "device": alarm_device,
            "device_type": parse_device_type(alarm_device),
            "category": infer_category(parse_device_type(alarm_device) or ""),
            "depth": depth,
            "terminal": True,
            "terminal_reason": "external_input",
        }

    # Separate SET and RST rules
    set_rules = [r for r in writing_rules if r["write_type"] == "SET"]
    rst_rules = [r for r in writing_rules if r["write_type"] == "RST"]
    other_rules = [r for r in writing_rules if r["write_type"] not in ("SET", "RST")]

    result = {
        "device": alarm_device,
        "device_type": parse_device_type(alarm_device),
        "category": infer_category(parse_device_type(alarm_device) or ""),
        "depth": depth,
        "terminal": False,
    }

    # Process SET (trigger) rules
    if set_rules:
        result["set_causes"] = []
        for rule in set_rules:
            cause = {
                "rule_id": rule["rule_id"],
                "section": rule["section"],
                "step": rule["step"],
                "condition_summary": rule["condition_summary"],
                "condition_tree": rule["condition_tree"],
                "upstream_devices": rule["upstream_devices"],
                "upstream_traces": []
            }
            # Recursively trace each upstream device
            for up_dev in rule["upstream_devices"]:
                up_trace = trace_alarm(up_dev, write_index, depth + 1, visited, max_depth)
                cause["upstream_traces"].append(up_trace)
            result["set_causes"].append(cause)

    # Process RST (reset) rules
    if rst_rules:
        result["reset_causes"] = []
        for rule in rst_rules:
            cause = {
                "rule_id": rule["rule_id"],
                "section": rule["section"],
                "step": rule["step"],
                "condition_summary": rule["condition_summary"],
                "condition_tree": rule["condition_tree"],
                "upstream_devices": rule["upstream_devices"],
                "upstream_traces": []
            }
            for up_dev in rule["upstream_devices"]:
                up_trace = trace_alarm(up_dev, write_index, depth + 1, visited, max_depth)
                cause["upstream_traces"].append(up_trace)
            result["reset_causes"].append(cause)

    # Process other write rules (OUT, MOV, etc.)
    if other_rules:
        result["other_writes"] = []
        for rule in other_rules:
            cause = {
                "rule_id": rule["rule_id"],
                "section": rule["section"],
                "step": rule["step"],
                "write_type": rule["write_type"],
                "condition_summary": rule["condition_summary"],
                "condition_tree": rule["condition_tree"],
                "upstream_devices": rule["upstream_devices"],
                "upstream_traces": []
            }
            for up_dev in rule["upstream_devices"]:
                up_trace = trace_alarm(up_dev, write_index, depth + 1, visited, max_depth)
                cause["upstream_traces"].append(up_trace)
            result["other_writes"].append(cause)

    return result


def build_alarm_traces(rules, write_index):
    """Build backward traces for all F-device alarms."""
    # Find all unique F-devices that are written to
    alarm_devices = sorted(set(
        r["target_device"] for r in rules
        if r["target_device"].startswith("F") and r["write_type"] == "SET"
    ), key=lambda x: int(re.sub(r"[^0-9]", "", x) or "0"))

    traces = []
    for alarm_dev in alarm_devices:
        trace = trace_alarm(alarm_dev, write_index)
        # Add section info
        set_rules = [r for r in rules if r["target_device"] == alarm_dev and r["write_type"] == "SET"]
        if set_rules:
            trace["section"] = set_rules[0]["section"]
        traces.append(trace)

    return traces


# ===========================================================================
# Output
# ===========================================================================


def write_jsonl(path, items):
    """Write items as JSON Lines."""
    with open(path, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def write_json(path, data):
    """Write data as formatted JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ===========================================================================
# Main
# ===========================================================================


def main():
    import sys
    import io
    # Force UTF-8 output on Windows
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    print("=" * 60)
    print("PLC 报警逻辑提取器 v2.0")
    print("=" * 60)

    # Step 1: Read and parse CSV
    print(f"\n[1/7] 读取CSV文件: {INPUT_CSV}")
    enc, lines = detect_encoding(INPUT_CSV)
    print(f"  检测到编码: {enc}")
    print(f"  总行数: {len(lines)}")

    # Step 2: Parse and merge
    print("\n[2/7] 解析指令行并合并续行...")
    raw_rows = parse_csv_lines(lines)
    merged = merge_continuation_rows(raw_rows)
    instructions = build_instruction_list(merged)
    print(f"  有效指令数: {len(instructions)}")

    # Step 3: Extract sections
    print("\n[3/7] 识别程序段落...")
    sections = extract_sections(merged)
    print(f"  段落数: {len(sections)}")
    for sec in sections:
        print(f"    - {sec['name']} (步号 {sec['step_start']}~{sec['step_end']})")

    # Step 4: Extract rules with stack simulation
    print("\n[4/7] 栈模拟提取规则...")
    rules = extract_rules_with_stack(instructions, sections)
    print(f"  提取规则数: {len(rules)}")

    set_count = sum(1 for r in rules if r["write_type"] == "SET")
    rst_count = sum(1 for r in rules if r["write_type"] == "RST")
    out_count = sum(1 for r in rules if r["write_type"] == "OUT")
    other_count = len(rules) - set_count - rst_count - out_count
    print(f"    SET规则: {set_count}")
    print(f"    RST规则: {rst_count}")
    print(f"    OUT规则: {out_count}")
    print(f"    其他写入: {other_count}")

    # Step 5: Build graph
    print("\n[5/7] 构建设备依赖图...")
    edges = build_edges(rules)
    devices = build_devices(rules, sections)
    print(f"  设备总数: {len(devices)}")
    print(f"  因果边数: {len(edges)}")

    # Categorize devices
    cat_counts = defaultdict(int)
    for d in devices:
        cat_counts[d["category"]] += 1
    for cat, cnt in sorted(cat_counts.items()):
        print(f"    {cat}: {cnt}")

    # Step 6: Build alarm traces
    print("\n[6/7] 生成报警回溯树...")
    write_index = build_write_index(rules)
    alarm_traces = build_alarm_traces(rules, write_index)
    print(f"  报警回溯数: {len(alarm_traces)}")

    # Step 7: Write output
    print(f"\n[7/7] 输出到 {OUTPUT_DIR}/")
    OUTPUT_DIR.mkdir(exist_ok=True)

    # Enrich sections with alarm device lists
    for sec in sections:
        sec["alarms"] = sorted(set(
            r["target_device"] for r in rules
            if r["section"] == sec["name"] and r["target_type"] == "F" and r["write_type"] == "SET"
        ), key=lambda x: int(re.sub(r"[^0-9]", "", x) or "0"))

    write_jsonl(OUTPUT_DIR / "rules.jsonl", rules)
    write_jsonl(OUTPUT_DIR / "edges.jsonl", edges)
    write_jsonl(OUTPUT_DIR / "devices.jsonl", devices)
    write_jsonl(OUTPUT_DIR / "sections.jsonl", sections)
    write_jsonl(OUTPUT_DIR / "alarm_trace.jsonl", alarm_traces)

    # Summary
    summary = {
        "input_file": INPUT_CSV,
        "encoding": enc,
        "total_lines": len(lines),
        "instructions": len(instructions),
        "sections": len(sections),
        "rules": len(rules),
        "devices": len(devices),
        "edges": len(edges),
        "alarm_traces": len(alarm_traces),
        "rules_breakdown": {
            "SET": set_count,
            "RST": rst_count,
            "OUT": out_count,
            "other": other_count,
        }
    }
    write_json(OUTPUT_DIR / "summary.json", summary)

    print("\n" + "=" * 60)
    print("完成! 输出文件:")
    print(f"  {OUTPUT_DIR / 'rules.jsonl'}")
    print(f"  {OUTPUT_DIR / 'edges.jsonl'}")
    print(f"  {OUTPUT_DIR / 'devices.jsonl'}")
    print(f"  {OUTPUT_DIR / 'sections.jsonl'}")
    print(f"  {OUTPUT_DIR / 'alarm_trace.jsonl'}")
    print(f"  {OUTPUT_DIR / 'summary.json'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
