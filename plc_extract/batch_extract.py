"""
batch_extract.py - Batch PLC Rule Extraction with Cross-Program Global Trace

Processes ALL program CSV files from a single CG machine (e.g., WH201_CG1),
merges them into a unified global knowledge base, and performs cross-program
backward tracing that resolves inter-program device dependencies.

Key improvements over single-file 71_rule_extract.py:
  - Batch processing of all 29+ program files
  - COMMENT.csv integration (device annotations)
  - Global write index enables cross-program backward trace
  - Trace terminates at X/Y (physical I/O) or truly external signals
  - MAX_TRACE_DEPTH = 15 to ensure reaching source

Usage:
  python plc_extract/batch_extract.py --input plc_extract/plc_file --output plc_knowledge_out_WH201_CG1 --line WH201_CG1

Output:
  plc_knowledge_out_<line>/
    summary.json, programs.json, comments.json,
    rules.jsonl, edges.jsonl, devices.jsonl, sections.jsonl, alarm_trace.jsonl
"""

import argparse
import csv
import json
import re
import sys
import io
import copy
import os
from pathlib import Path
from collections import defaultdict
from typing import Optional

# ===========================================================================
# Import core functions from existing extractor
# ===========================================================================

# Add plc_extract to path so we can import
sys.path.insert(0, str(Path(__file__).parent))

# We import key components from 71_rule_extract but NOT the main() or config constants
from importlib import import_module

# Dynamically import to avoid name issues with the "71_" prefix
import importlib.util
_extractor_path = Path(__file__).parent / "71_rule_extract.py"
_spec = importlib.util.spec_from_file_location("rule_extractor", _extractor_path)
_extractor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_extractor)

# Import core functions
detect_encoding = _extractor.detect_encoding
parse_csv_lines = _extractor.parse_csv_lines
merge_continuation_rows = _extractor.merge_continuation_rows
build_instruction_list = _extractor.build_instruction_list
extract_sections = _extractor.extract_sections
get_section_for_step = _extractor.get_section_for_step
extract_rules_with_stack = _extractor.extract_rules_with_stack
build_edges = _extractor.build_edges
parse_device_type = _extractor.parse_device_type
infer_category = _extractor.infer_category
ConditionNode = _extractor.ConditionNode

# ===========================================================================
# Configuration
# ===========================================================================

MAX_TRACE_DEPTH = 15

# Section marker detection patterns
# Different programs use different marker styles (###, ===, --, #, etc.)
SECTION_MARKER_PATTERNS = [
    re.compile(r"^#{1,4}\s*(.+)$"),      # ### SECTION_NAME or # SECTION_NAME
    re.compile(r"^={3,}\s*$"),            # ============= (separator, skip)
    re.compile(r"^-{3,}\s*$"),            # ------------- (separator, skip)
]


# ===========================================================================
# Enhanced Section Detection (handles various marker styles)
# ===========================================================================

def extract_sections_enhanced(merged_rows):
    """
    Extract program sections with enhanced marker detection.
    Handles various commenting styles across different program files:
    - ### markers (original)
    - Single # markers
    - Lines that are purely section names (no instruction, meaningful text)
    """
    sections = []
    
    for row in merged_rows:
        decl = row.get("declaration", "").strip()
        step = row.get("step")
        op = row.get("op", "")
        
        # Only consider rows that have a declaration and no instruction
        if not decl or op:
            continue
        
        # Skip pure separators (=== or ---)
        if re.match(r"^[=\-]{3,}\s*$", decl):
            continue
        
        # Extract section name
        name = None
        
        # Pattern 1: ### or ## or # prefix
        m = re.match(r"^#{1,4}\s*(.+)$", decl)
        if m:
            name = m.group(1).strip()
        
        # Pattern 2: Meaningful text that looks like a section header
        # (at least 4 chars, not just symbols, associated with a step number)
        if not name and step is not None and len(decl) > 3:
            # Check it's not just symbols
            alpha_count = sum(1 for c in decl if c.isalpha())
            if alpha_count >= 3:
                name = decl
        
        if name and step is not None:
            sections.append({
                "name": name,
                "step_start": step,
                "step_end": None,
                "alarms": []
            })
    
    # Remove duplicates at same step
    seen_steps = set()
    unique_sections = []
    for sec in sections:
        if sec["step_start"] not in seen_steps:
            seen_steps.add(sec["step_start"])
            unique_sections.append(sec)
    sections = unique_sections
    
    # Fill step_end
    for i in range(len(sections) - 1):
        sections[i]["step_end"] = sections[i + 1]["step_start"] - 1
    if sections:
        sections[-1]["step_end"] = 99999
    
    return sections


# ===========================================================================
# Single File Parser (parameterized)
# ===========================================================================

def parse_single_file(filepath: Path, program_no: str, program_name: str = "") -> dict:
    """
    Parse a single PLC program CSV file and return extracted data.
    
    Args:
        filepath: Path to the CSV file
        program_no: Program number (e.g., "000", "001", "005")
        program_name: Program name (auto-detected if empty)
    
    Returns:
        dict with keys: rules, edges, devices, sections, stats
    """
    # Read and parse
    enc, lines = detect_encoding(str(filepath))
    raw_rows = parse_csv_lines(lines)
    merged = merge_continuation_rows(raw_rows)
    instructions = build_instruction_list(merged)
    
    # Auto-detect program name from first line
    if not program_name and raw_rows:
        first_line = raw_rows[0]
        if first_line and first_line[0]:
            program_name = first_line[0]
    
    # Extract sections (use enhanced detection)
    sections = extract_sections_enhanced(merged)
    
    # If no sections found with enhanced, fall back to original
    if not sections:
        sections = extract_sections(merged)
    
    # If still no sections, create a default one
    if not sections:
        sections = [{"name": f"Program {program_no}", "step_start": 0, "step_end": 99999, "alarms": []}]
    
    # We need to temporarily set module-level variables for extract_rules_with_stack
    # since it uses PROGRAM_NO and PROGRAM_NAME internally for rule_id generation
    original_pno = _extractor.PROGRAM_NO
    original_pname = _extractor.PROGRAM_NAME
    _extractor.PROGRAM_NO = program_no
    _extractor.PROGRAM_NAME = program_name
    
    try:
        rules = extract_rules_with_stack(instructions, sections)
    finally:
        # Restore
        _extractor.PROGRAM_NO = original_pno
        _extractor.PROGRAM_NAME = original_pname
    
    # Build edges
    edges = build_edges(rules)
    
    # Stats
    stats = {
        "encoding": enc,
        "total_lines": len(lines),
        "instructions": len(instructions),
        "sections_count": len(sections),
        "rules_count": len(rules),
    }
    
    return {
        "program_no": program_no,
        "program_name": program_name,
        "rules": rules,
        "edges": edges,
        "sections": sections,
        "stats": stats,
    }


# ===========================================================================
# COMMENT.csv Parser
# ===========================================================================

def parse_comments(comment_path: Path) -> dict[str, str]:
    """
    Parse COMMENT.csv into a device → comment mapping.
    
    Returns:
        Dict mapping device name (uppercase) to comment string
    """
    comments = {}
    
    enc, lines = detect_encoding(str(comment_path))
    
    # Parse as tab-separated
    for line in lines[2:]:  # Skip header rows (title + column headers)
        parts = line.rstrip("\n\r").split("\t")
        parts = [p.strip().strip('"') for p in parts]
        if len(parts) >= 2 and parts[0] and parts[1]:
            device = parts[0].strip().upper()
            comment = parts[1].strip()
            if comment:
                comments[device] = comment
    
    return comments


# ===========================================================================
# Global Device Catalog Builder
# ===========================================================================

def build_global_devices(all_rules: list[dict], comments: dict[str, str]) -> list[dict]:
    """
    Build a unified global device catalog from rules across all programs.
    
    Each device record includes:
    - Which programs it appears in
    - Roles per program (target/condition)
    - Global read/write counts
    - Comment from COMMENT.csv
    """
    device_map = {}
    
    def ensure_device(dev, program_no, section, role):
        if not dev or dev.startswith("K") or dev.startswith("H"):
            return
        if dev not in device_map:
            dtype = parse_device_type(dev)
            device_map[dev] = {
                "device": dev,
                "device_type": dtype,
                "category": infer_category(dtype) if dtype else "unknown",
                "comment": comments.get(dev, ""),
                "programs": [],
                "roles_by_program": {},
                "sections": [],
                "written_by_count": 0,
                "read_by_count": 0,
            }
        
        entry = device_map[dev]
        if program_no not in entry["programs"]:
            entry["programs"].append(program_no)
        if program_no not in entry["roles_by_program"]:
            entry["roles_by_program"][program_no] = []
        if role not in entry["roles_by_program"][program_no]:
            entry["roles_by_program"][program_no].append(role)
        if section and section not in entry["sections"]:
            entry["sections"].append(section)
    
    for rule in all_rules:
        program_no = rule["program_no"]
        target = rule["target_device"]
        
        if target and not target.startswith("K") and not target.startswith("H"):
            ensure_device(target, program_no, rule["section"], "target")
            device_map[target]["written_by_count"] += 1
        
        for src in rule.get("upstream_devices", []):
            ensure_device(src, program_no, rule["section"], "condition")
            if src in device_map:
                device_map[src]["read_by_count"] += 1
    
    # Sort and return
    result = sorted(device_map.values(), key=lambda x: x["device"])
    return result


# ===========================================================================
# Global Cross-Program Backward Trace (Flat Format)
# ===========================================================================
# 
# Instead of deeply nested trees (which explode in size when serialized),
# we use a FLAT format:
#   - device_traces.jsonl: one entry per device with its DIRECT causes only
#   - alarm_trace.jsonl: top-level alarm entries pointing to device_traces
#
# This avoids duplicating shared subtrees. The Agent reconstructs the full
# tree on-the-fly by following references.
# ===========================================================================

def build_global_write_index(all_rules: list[dict]) -> dict[str, list[dict]]:
    """Build global write index: device → [rules from any program]."""
    index = defaultdict(list)
    for rule in all_rules:
        index[rule["target_device"]].append(rule)
    return index


def build_device_traces_flat(
    write_index: dict[str, list[dict]],
    comments: dict[str, str],
) -> dict[str, dict]:
    """
    Build a flat device trace index: for each device, store its DIRECT
    write rules and upstream devices (one level only, no recursion).
    
    The Agent will follow the chain by looking up each upstream device
    in this same index.
    
    Returns:
        Dict mapping device name to its trace info
    """
    device_traces = {}
    
    for device, rules in write_index.items():
        # Skip constants - they are not traceable devices
        if device.startswith("K") or device.startswith("H"):
            continue
        
        device_type = parse_device_type(device)
        category = infer_category(device_type) if device_type else "unknown"
        comment = comments.get(device, "")
        
        # Classify terminal type
        terminal = False
        terminal_reason = None
        if device_type in ("X",):
            terminal = True
            terminal_reason = "physical_input"
        elif device_type in ("Y",):
            terminal = True
            terminal_reason = "physical_output"
        elif device_type in ("SM", "SD"):
            terminal = True
            terminal_reason = "system_special_relay"
        
        # Separate by write type
        set_rules = [r for r in rules if r["write_type"] == "SET"]
        rst_rules = [r for r in rules if r["write_type"] == "RST"]
        other_rules = [r for r in rules if r["write_type"] not in ("SET", "RST")]
        
        entry = {
            "device": device,
            "device_type": device_type,
            "category": category,
            "comment": comment,
            "terminal": terminal,
        }
        if terminal_reason:
            entry["terminal_reason"] = terminal_reason
        
        def format_causes(rules_list):
            causes = []
            for rule in rules_list:
                # Filter out K/H constants from upstream_devices
                filtered_upstream = [
                    d for d in rule["upstream_devices"]
                    if not d.startswith("K") and not d.startswith("H")
                ]
                causes.append({
                    "rule_id": rule["rule_id"],
                    "program_no": rule["program_no"],
                    "section": rule["section"],
                    "step": rule["step"],
                    "write_type": rule["write_type"],
                    "condition_summary": rule["condition_summary"],
                    "condition_tree": rule["condition_tree"],
                    "upstream_devices": filtered_upstream,
                })
            return causes
        
        if set_rules:
            entry["set_causes"] = format_causes(set_rules)
        if rst_rules:
            entry["reset_causes"] = format_causes(rst_rules)
        if other_rules:
            entry["other_writes"] = format_causes(other_rules)
        
        device_traces[device] = entry
    
    return device_traces


def trace_alarm_recursive(
    device: str,
    device_traces: dict[str, dict],
    comments: dict[str, str],
    depth: int = 0,
    visited: Optional[set] = None,
    max_depth: int = MAX_TRACE_DEPTH,
) -> dict:
    """
    Recursively build a full trace tree for display purposes.
    Uses device_traces (flat) as the data source.
    
    This is only called for the top-level alarm device and produces
    the complete nested tree for that specific alarm.
    """
    if visited is None:
        visited = set()
    
    device_type = parse_device_type(device)
    category = infer_category(device_type) if device_type else "unknown"
    comment = comments.get(device, "")
    
    # Terminal conditions
    if device_type in ("X",):
        return {"device": device, "device_type": device_type, "category": category,
                "comment": comment, "depth": depth, "terminal": True, "terminal_reason": "physical_input"}
    if device_type in ("Y",):
        return {"device": device, "device_type": device_type, "category": category,
                "comment": comment, "depth": depth, "terminal": True, "terminal_reason": "physical_output"}
    if device_type in ("SM", "SD"):
        return {"device": device, "device_type": device_type, "category": category,
                "comment": comment, "depth": depth, "terminal": True, "terminal_reason": "system_special_relay"}
    if depth >= max_depth:
        return {"device": device, "device_type": device_type, "category": category,
                "comment": comment, "depth": depth, "terminal": True, "terminal_reason": "depth_limit"}
    if device in visited:
        return {"device": device, "device_type": device_type, "category": category,
                "comment": comment, "depth": depth, "terminal": True, "terminal_reason": "circular_reference"}
    
    # Look up in flat index
    trace_info = device_traces.get(device)
    if not trace_info:
        return {"device": device, "device_type": device_type, "category": category,
                "comment": comment, "depth": depth, "terminal": True, "terminal_reason": "truly_external"}
    
    visited = visited | {device}
    
    result = {
        "device": device, "device_type": device_type, "category": category,
        "comment": comment, "depth": depth, "terminal": False,
    }
    
    def trace_causes(causes_list):
        traced_causes = []
        for cause in causes_list:
            traced_cause = {
                "rule_id": cause["rule_id"],
                "program_no": cause["program_no"],
                "section": cause["section"],
                "step": cause["step"],
                "write_type": cause["write_type"],
                "condition_summary": cause["condition_summary"],
                "condition_tree": cause["condition_tree"],
                "upstream_devices": cause["upstream_devices"],
                "upstream_traces": [],
            }
            for up_dev in cause["upstream_devices"]:
                up_trace = trace_alarm_recursive(
                    up_dev, device_traces, comments,
                    depth + 1, visited, max_depth
                )
                traced_cause["upstream_traces"].append(up_trace)
            traced_causes.append(traced_cause)
        return traced_causes
    
    if "set_causes" in trace_info:
        result["set_causes"] = trace_causes(trace_info["set_causes"])
    if "reset_causes" in trace_info:
        result["reset_causes"] = trace_causes(trace_info["reset_causes"])
    if "other_writes" in trace_info:
        result["other_writes"] = trace_causes(trace_info["other_writes"])
    
    return result


def build_global_alarm_traces(
    all_rules: list[dict],
    write_index: dict[str, list[dict]],
    comments: dict[str, str],
    device_traces: dict[str, dict],
) -> list[dict]:
    """Build backward traces for all F-device alarms across all programs."""
    # Find all F-devices that are SET anywhere
    alarm_devices = sorted(set(
        r["target_device"] for r in all_rules
        if r["target_device"].startswith("F") and r["write_type"] == "SET"
    ), key=lambda x: int(re.sub(r"[^0-9]", "", x) or "0"))
    
    print(f"  Found {len(alarm_devices)} alarm devices to trace...")
    
    traces = []
    for i, alarm_dev in enumerate(alarm_devices):
        if (i + 1) % 100 == 0:
            print(f"    Tracing {i+1}/{len(alarm_devices)}...")
        
        trace = trace_alarm_recursive(alarm_dev, device_traces, comments)
        
        # Add section info from the SET rule
        set_rules = [r for r in all_rules 
                     if r["target_device"] == alarm_dev and r["write_type"] == "SET"]
        if set_rules:
            trace["section"] = set_rules[0]["section"]
            trace["source_program"] = set_rules[0]["program_no"]
        
        traces.append(trace)
    
    return traces


# ===========================================================================
# Output Utilities
# ===========================================================================

def write_jsonl(path: Path, items: list):
    """Write items as JSON Lines."""
    with open(path, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def write_json(path: Path, data):
    """Write data as formatted JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ===========================================================================
# Main
# ===========================================================================

def main():
    # Force UTF-8 output on Windows
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    
    parser = argparse.ArgumentParser(description="Batch PLC Rule Extraction with Cross-Program Trace")
    parser.add_argument("--input", required=True, help="Input directory containing PLC CSV files")
    parser.add_argument("--output", required=True, help="Output directory for knowledge base")
    parser.add_argument("--line", required=True, help="Production line identifier (e.g., WH201_CG1)")
    parser.add_argument("--max-depth", type=int, default=MAX_TRACE_DEPTH, help=f"Max trace depth (default {MAX_TRACE_DEPTH})")
    args = parser.parse_args()
    
    input_dir = Path(args.input)
    output_dir = Path(args.output)
    line_name = args.line
    max_depth = args.max_depth
    
    if not input_dir.exists():
        print(f"ERROR: Input directory not found: {input_dir}")
        sys.exit(1)
    
    print("=" * 70)
    print(f"  PLC Batch Extraction - {line_name}")
    print(f"  Input:  {input_dir}")
    print(f"  Output: {output_dir}")
    print(f"  Max trace depth: {max_depth}")
    print("=" * 70)
    
    # ================================================================
    # Step 1: Discover CSV files
    # ================================================================
    print(f"\n[1/7] Discovering CSV files in {input_dir}...")
    
    csv_files = sorted([
        f for f in input_dir.glob("*.csv")
        if f.name.upper() != "COMMENT.CSV" and f.stem.isdigit()
    ], key=lambda f: int(f.stem))
    
    # Also include non-numeric CSV files that aren't COMMENT
    other_csv = sorted([
        f for f in input_dir.glob("*.csv")
        if f.name.upper() != "COMMENT.CSV" and not f.stem.isdigit()
    ])
    # Only include numeric-named ones for now (000.csv, 001.csv, etc.)
    
    comment_file = input_dir / "COMMENT.csv"
    has_comments = comment_file.exists()
    
    print(f"  Found {len(csv_files)} program files")
    print(f"  COMMENT.csv: {'found' if has_comments else 'NOT found'}")
    for f in csv_files:
        print(f"    - {f.name}")
    
    # ================================================================
    # Step 2: Parse COMMENT.csv
    # ================================================================
    print(f"\n[2/7] Parsing COMMENT.csv...")
    comments = {}
    if has_comments:
        comments = parse_comments(comment_file)
        print(f"  Loaded {len(comments)} device comments")
        # Show distribution
        type_counts = defaultdict(int)
        for dev in comments:
            dt = parse_device_type(dev)
            type_counts[dt or "?"] += 1
        top_types = sorted(type_counts.items(), key=lambda x: -x[1])[:8]
        print(f"  Top types: {', '.join(f'{t}:{c}' for t,c in top_types)}")
    else:
        print("  No COMMENT.csv found, proceeding without comments")
    
    # ================================================================
    # Step 3: Parse each program file
    # ================================================================
    print(f"\n[3/7] Parsing {len(csv_files)} program files...")
    
    all_rules = []
    all_edges = []
    all_sections = []
    program_infos = []
    
    for filepath in csv_files:
        program_no = filepath.stem  # e.g., "000", "001", "005"
        print(f"  [{program_no}] {filepath.name}...", end=" ")
        
        try:
            result = parse_single_file(filepath, program_no)
            
            rules = result["rules"]
            edges = result["edges"]
            sections = result["sections"]
            stats = result["stats"]
            
            # Add program_no to sections
            for sec in sections:
                sec["program_no"] = program_no
            
            all_rules.extend(rules)
            all_edges.extend(edges)
            all_sections.extend(sections)
            
            # Determine program type
            has_f_set = any(r["target_device"].startswith("F") and r["write_type"] == "SET" for r in rules)
            prog_type = "alarm" if has_f_set else "control"
            
            program_infos.append({
                "program_no": program_no,
                "program_name": result["program_name"],
                "type": prog_type,
                "instructions": stats["instructions"],
                "rules": stats["rules_count"],
                "sections": stats["sections_count"],
            })
            
            print(f"{stats['instructions']} instr, {stats['rules_count']} rules, {len(sections)} sections")
            
        except Exception as e:
            print(f"ERROR: {e}")
            import traceback
            traceback.print_exc()
            continue
    
    print(f"\n  Total: {len(all_rules)} rules, {len(all_edges)} edges from {len(program_infos)} programs")
    
    # ================================================================
    # Step 4: Build global device catalog
    # ================================================================
    print(f"\n[4/7] Building global device catalog...")
    
    all_devices = build_global_devices(all_rules, comments)
    print(f"  Total devices: {len(all_devices)}")
    
    # Show category distribution
    cat_counts = defaultdict(int)
    for d in all_devices:
        cat_counts[d["category"]] += 1
    for cat, cnt in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"    {cat}: {cnt}")
    
    commented_count = sum(1 for d in all_devices if d.get("comment"))
    print(f"  Devices with comments: {commented_count}/{len(all_devices)}")
    
    # ================================================================
    # Step 5: Build global write index and flat device traces
    # ================================================================
    print(f"\n[5/7] Building global write index and device trace index...")
    
    write_index = build_global_write_index(all_rules)
    print(f"  Indexed {len(write_index)} target devices")
    
    # Build flat device traces (one entry per device, no recursion)
    device_traces = build_device_traces_flat(write_index, comments)
    print(f"  Device traces built: {len(device_traces)}")
    
    # Stats on cross-program devices
    cross_program = 0
    for dev, rules in write_index.items():
        programs = set(r["program_no"] for r in rules)
        if len(programs) > 1:
            cross_program += 1
    print(f"  Devices written by multiple programs: {cross_program}")
    
    # ================================================================
    # Step 6: Global cross-program alarm traces
    # ================================================================
    print(f"\n[6/7] Skipping pre-computed alarm traces (will be computed on-the-fly by Agent)...")
    print(f"  Device traces (flat) will be used for on-demand recursive trace")
    print(f"  Total F-devices with SET rules: {sum(1 for d in device_traces if d.startswith('F') and any(c.get('write_type')=='SET' for c in device_traces[d].get('set_causes',[])))}")
    
    # We DO NOT pre-generate alarm_trace.jsonl because with cross-program
    # links the fully expanded trees are enormous (GBs). Instead:
    # - device_traces.jsonl provides flat per-device data
    # - The Agent's trace_alarm tool does recursive expansion on-the-fly
    alarm_traces = []  # Empty - computed on demand by Agent
    
    # ================================================================
    # Step 7: Write output
    # ================================================================
    print(f"\n[7/7] Writing output to {output_dir}/...")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Enrich sections with alarm lists
    for sec in all_sections:
        prog = sec.get("program_no", "")
        sec["alarms"] = sorted(set(
            r["target_device"] for r in all_rules
            if r["section"] == sec["name"] and r["program_no"] == prog
            and r.get("target_type") == "F" and r["write_type"] == "SET"
        ), key=lambda x: int(re.sub(r"[^0-9]", "", x) or "0"))
    
    # Write all output files
    write_jsonl(output_dir / "rules.jsonl", all_rules)
    write_jsonl(output_dir / "edges.jsonl", all_edges)
    write_jsonl(output_dir / "devices.jsonl", all_devices)
    write_jsonl(output_dir / "sections.jsonl", all_sections)
    write_jsonl(output_dir / "alarm_trace.jsonl", alarm_traces)
    write_jsonl(output_dir / "device_traces.jsonl", list(device_traces.values()))
    write_json(output_dir / "comments.json", comments)
    write_json(output_dir / "programs.json", program_infos)
    
    # Summary
    rules_breakdown = defaultdict(int)
    for r in all_rules:
        wt = r["write_type"]
        if wt in ("SET", "RST", "OUT"):
            rules_breakdown[wt] += 1
        else:
            rules_breakdown["other"] += 1
    
    summary = {
        "line_name": line_name,
        "input_dir": str(input_dir),
        "program_count": len(program_infos),
        "total_rules": len(all_rules),
        "total_edges": len(all_edges),
        "total_devices": len(all_devices),
        "total_sections": len(all_sections),
        "total_alarm_traces": len(alarm_traces),
        "comments_loaded": len(comments),
        "max_trace_depth": max_depth,
        "rules_breakdown": dict(rules_breakdown),
        "programs": [p["program_no"] for p in program_infos],
    }
    write_json(output_dir / "summary.json", summary)
    
    # Print final summary
    print(f"\n{'=' * 70}")
    print(f"  COMPLETE - {line_name}")
    print(f"{'=' * 70}")
    print(f"  Programs:     {len(program_infos)}")
    print(f"  Rules:        {len(all_rules)} (SET:{rules_breakdown['SET']}, RST:{rules_breakdown['RST']}, OUT:{rules_breakdown['OUT']}, other:{rules_breakdown['other']})")
    print(f"  Edges:        {len(all_edges)}")
    print(f"  Devices:      {len(all_devices)} ({commented_count} with comments)")
    print(f"  Sections:     {len(all_sections)}")
    print(f"  Alarm traces: {len(alarm_traces)}")
    print(f"\n  Output: {output_dir}/")
    for f in sorted(output_dir.iterdir()):
        size_kb = f.stat().st_size / 1024
        print(f"    {f.name:25s} {size_kb:8.1f} KB")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
