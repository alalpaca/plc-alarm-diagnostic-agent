"""
PLC Agent Tools - LangChain Tool definitions

These tools are the "hands" of the Agent. Each tool wraps a knowledge base
query method and provides a schema that the LLM can understand and invoke.

Tool-level caching: Since the knowledge base is static, identical tool calls
always return the same result. We cache tool outputs to avoid redundant
formatting work (small gain) and to support the query-level cache (big gain).

Multi-program support:
- Tools automatically use the currently active knowledge base (set by UI/API).
- No 'program' parameter is exposed to the LLM; program selection is user-driven.
- Tools are grouped by kb_type (alarm/control/common) for dynamic tool loading.
"""
import json
from typing import Optional
from collections import deque
from langchain_core.tools import tool

from plc_agent.knowledge.loader import get_knowledge_base
from plc_agent.knowledge.cache import get_tool_cache


# ================================================================
# ALARM-SPECIFIC TOOLS (only loaded for alarm-type KBs)
# ================================================================

@tool
def trace_alarm(device: str, max_depth: Optional[int] = None) -> str:
    """反向追溯一个报警(F设备)的触发原因和因果链，输出包含梯形图。

    适用场景：用户问某个报警为什么触发、根本原因是什么、怎么清除。
    输出格式：简短文字摘要 + 回溯路径上所有规则的 SVG 梯形图。
    不适用：查询非F设备的信息（用query_device），或只想看报警列表（用list_alarms）。

    Args:
        device: 报警设备名，如 "F1", "F65", "F701"。
        max_depth: 追溯深度限制(1-15)。省略则返回完整追溯树。
    """
    cache = get_tool_cache()
    
    # Normalize input
    device = device.strip().upper()
    if not device.startswith("F"):
        device = "F" + device
    
    # Check cache
    cached = cache.get("trace_alarm", device=device, max_depth=max_depth)
    if cached is not None:
        return cached
    
    kb = get_knowledge_base()
    
    # Check device exists in device_traces or alarm_traces
    trace = kb.get_alarm_trace(device, max_depth=max_depth)
    
    if trace is None and device not in kb.device_traces:
        # Try to suggest similar devices
        f_devices = [d for d in kb.device_traces if d.startswith("F")]
        suggestions = [d for d in f_devices if device.replace("F", "") in d][:5]
        return (
            f"Alarm {device} not found in the knowledge base.\n"
            f"There are {len(f_devices)} F-devices available.\n"
            f"Did you mean one of: {suggestions if suggestions else 'Use list_alarms to see all available alarms'}"
        )
    
    # Build short text summary
    comment = kb.get_comment(device)
    trace_info = kb.device_traces.get(device, {})
    set_causes = trace_info.get("set_causes", [])
    rst_causes = trace_info.get("reset_causes", [])
    
    summary_lines = [f"**{device}** — {comment}"]
    if set_causes:
        summary_lines.append(f"触发条件 (SET): `{set_causes[0]['condition_summary']}` @程序{set_causes[0].get('program_no','')}")
    if rst_causes:
        summary_lines.append(f"清除条件 (RST): `{rst_causes[0]['condition_summary']}` @程序{rst_causes[0].get('program_no','')}")
    summary = "\n".join(summary_lines)
    
    # Collect all rule_ids from the trace path
    depth_limit = max_depth or 15
    rule_ids = kb.get_trace_rule_ids(device, max_depth=depth_limit)
    
    # Generate SVG for each rule
    from plc_agent.ui.ladder_renderer import render_ladder_svg
    svg_parts = []
    for rule_id in rule_ids:
        rule = kb.get_rule_by_id(rule_id)
        if rule:
            svg = render_ladder_svg(rule, kb.comments)
            svg_parts.append(
                f'<div style="margin:8px 0;padding:6px;border:1px solid #eee;border-radius:3px;">'
                f'<div style="font-size:10px;color:#555;margin-bottom:4px;">'
                f'<b>{rule_id}</b> | {rule["write_type"]} {rule["target_device"]} | {rule["condition_summary"]}</div>'
                f'{svg}</div>'
            )
    
    # Combine: summary + SVG diagrams
    result = summary + "\n\n" + "\n".join(svg_parts)
    
    # Safety valve
    if len(result) > 200000:
        result = summary + "\n\n" + "\n".join(svg_parts[:20]) + "\n\n... (输出过长，已截断部分梯形图)"
    
    cache.put("trace_alarm", result, device=device, max_depth=max_depth)
    return result


@tool
def list_alarms(section: Optional[str] = None) -> str:
    """列出系统中的报警，可按程序段名称过滤。

    适用场景：用户想知道有多少报警、某个程序段包含哪些报警、浏览报警分布。
    不适用：查某个具体报警的原因（用trace_alarm）。

    Args:
        section: 程序段名称关键词过滤（模糊匹配），如 "SERVO", "WATER", "CV"。省略则显示全部。
    """
    cache = get_tool_cache()
    norm_section = section.strip().upper() if section else None
    
    cached = cache.get("list_alarms", section=norm_section)
    if cached is not None:
        return cached
    
    kb = get_knowledge_base()
    
    result = kb.list_alarms(section)
    
    if "error" in result:
        sections_list = "\n".join(f"  - {s['name']}" for s in kb.sections)
        return f"{result['error']}\n\nAvailable sections:\n{sections_list}"
    
    lines = []
    total = 0
    for sec_name, alarms in result.items():
        lines.append(f"\n[{sec_name}] ({len(alarms)} alarms)")
        total += len(alarms)
        if len(alarms) <= 20:
            lines.append(f"  {', '.join(alarms)}")
        else:
            lines.append(f"  {', '.join(alarms[:15])}, ... (+{len(alarms)-15} more)")
    
    header = f"Total: {total} alarms across {len(result)} sections\n"
    output = header + "\n".join(lines)
    cache.put("list_alarms", output, section=norm_section)
    return output


# ================================================================
# CONTROL-SPECIFIC TOOLS (only loaded for control-type KBs)
# ================================================================

@tool
def trace_control_logic(device: str, direction: str = "backward", max_depth: int = 3) -> str:
    """追踪任意设备的因果链——反向找上游原因或正向找下游影响。

    适用场景：用户想知道某个设备（M/L/T/D等）被什么条件控制、或它影响了什么下游逻辑。
    支持所有设备类型，不限于F报警设备。

    Args:
        device: PLC设备名，如 "L138", "M2127", "T0", "M1920"。
        direction: "backward"=找上游原因（什么控制了它），"forward"=找下游影响（它控制了什么）。默认backward。
        max_depth: 追踪深度(1-15)，默认5层。对于跨程序回溯建议用更大值(如10-15)。
    """
    cache = get_tool_cache()
    device = device.strip().upper()
    direction = direction.strip().lower()
    
    if direction not in ("backward", "forward"):
        return "Error: direction must be 'backward' or 'forward'."
    
    if max_depth < 1:
        max_depth = 1
    elif max_depth > 15:
        max_depth = 15
    
    # Check cache
    cached = cache.get("trace_control_logic", device=device, direction=direction, max_depth=max_depth)
    if cached is not None:
        return cached
    
    kb = get_knowledge_base()
    
    # Verify device exists
    if device not in kb.devices:
        return (
            f"Device {device} not found in the knowledge base.\n"
            f"The knowledge base contains {len(kb.devices)} devices.\n"
            f"Device types available: L(link relay), M(flag), SM(special relay), "
            f"D(register), R(file register), T(timer), X(input), Y(output)"
        )
    
    # BFS traversal along causal edges
    if direction == "backward":
        # For global KB with device_traces: output summary + SVG ladder diagrams
        if kb.has_device_traces and device in kb.device_traces:
            # Build short text summary
            comment = kb.get_comment(device)
            trace_info = kb.device_traces.get(device, {})
            set_causes = trace_info.get("set_causes", [])
            rst_causes = trace_info.get("reset_causes", [])
            other_writes = trace_info.get("other_writes", [])
            
            summary_lines = [f"**{device}** — {comment}"]
            if set_causes:
                summary_lines.append(f"写入条件 (SET): `{set_causes[0]['condition_summary']}` @程序{set_causes[0].get('program_no','')}")
            if other_writes:
                summary_lines.append(f"写入条件 ({other_writes[0].get('write_type','OUT')}): `{other_writes[0]['condition_summary']}` @程序{other_writes[0].get('program_no','')}")
            if rst_causes:
                summary_lines.append(f"复位条件 (RST): `{rst_causes[0]['condition_summary']}` @程序{rst_causes[0].get('program_no','')}")
            summary = "\n".join(summary_lines)
            
            # Collect all rule_ids from trace path
            rule_ids = kb.get_trace_rule_ids(device, max_depth=max_depth)
            
            # Generate SVG for each rule
            from plc_agent.ui.ladder_renderer import render_ladder_svg
            svg_parts = []
            for rule_id in rule_ids:
                rule = kb.get_rule_by_id(rule_id)
                if rule:
                    svg = render_ladder_svg(rule, kb.comments)
                    svg_parts.append(
                        f'<div style="margin:8px 0;padding:6px;border:1px solid #eee;border-radius:3px;">'
                        f'<div style="font-size:10px;color:#555;margin-bottom:4px;">'
                        f'<b>{rule_id}</b> | {rule["write_type"]} {rule["target_device"]} | {rule["condition_summary"]}</div>'
                        f'{svg}</div>'
                    )
            
            result = summary + "\n\n" + "\n".join(svg_parts)
            
            # Safety valve
            if len(result) > 200000:
                result = summary + "\n\n" + "\n".join(svg_parts[:20]) + "\n\n... (输出过长，已截断部分梯形图)"
        else:
            # Fallback: BFS-based trace (text only, for non-global KBs)
            result = _trace_backward(kb, device, max_depth)
            MAX_OUTPUT_CHARS = 15000
            if result and len(result) > MAX_OUTPUT_CHARS:
                result = result[:MAX_OUTPUT_CHARS] + "\n\n... (output truncated)"
    else:
        result = _trace_forward(kb, device, max_depth)
        # Safety valve for forward trace (text only)
        MAX_OUTPUT_CHARS = 15000
        if result and len(result) > MAX_OUTPUT_CHARS:
            result = result[:MAX_OUTPUT_CHARS] + "\n\n... (output truncated)"
    
    cache.put("trace_control_logic", result, device=device, direction=direction, max_depth=max_depth)
    return result


def _trace_backward(kb, root_device: str, max_depth: int) -> str:
    """Backward trace: find what controls this device (BFS along incoming edges)."""
    lines = [f"=== Backward Trace: {root_device} ==="]
    lines.append(f"(What controls/writes to {root_device}?)\n")
    
    # First show direct write rules for root device
    write_rules = kb.rules_by_target.get(root_device, [])
    if write_rules:
        lines.append(f"--- Direct Write Rules for {root_device} ({len(write_rules)}) ---")
        for r in write_rules:
            lines.append(f"  [{r['write_type']}] {r['rule_id']} (Step {r['step']}, {r['section']})")
            lines.append(f"       Condition: {r['condition_summary']}")
            lines.append(f"       Upstream: {', '.join(r.get('upstream_devices', []))}")
    else:
        lines.append(f"--- {root_device}: No write rules (external input / not written in this program) ---")
    
    # BFS to find upstream chain
    if max_depth > 1 and write_rules:
        lines.append(f"\n--- Upstream Trace (depth {max_depth}) ---")
        visited = {root_device}
        queue = deque()  # (device, depth, path)
        
        # Seed with upstream devices from write rules
        for r in write_rules:
            for up_dev in r.get("upstream_devices", []):
                if up_dev not in visited and not up_dev.startswith("SM"):  # Skip special relays
                    queue.append((up_dev, 1, [root_device]))
                    visited.add(up_dev)
        
        while queue:
            dev, depth, path = queue.popleft()
            indent = "  " * depth
            
            # Get device info
            dev_meta = kb.devices.get(dev, {})
            dev_category = dev_meta.get("category", "unknown")
            
            # Get write rules for this upstream device
            dev_rules = kb.rules_by_target.get(dev, [])
            
            if dev_rules:
                for r in dev_rules[:3]:  # Limit rules shown per device
                    lines.append(f"{indent}[{dev}] ({dev_category}) <- [{r['write_type']}] {r['condition_summary']} ({r['section']})")
            else:
                lines.append(f"{indent}[{dev}] ({dev_category}) <- EXTERNAL/NOT_WRITTEN")
            
            # Continue BFS if depth allows
            if depth < max_depth:
                for r in dev_rules:
                    for up_dev in r.get("upstream_devices", []):
                        if up_dev not in visited and not up_dev.startswith("SM"):
                            queue.append((up_dev, depth + 1, path + [dev]))
                            visited.add(up_dev)
    
    return "\n".join(lines)


def _trace_forward(kb, root_device: str, max_depth: int) -> str:
    """Forward trace: find what this device influences (BFS along outgoing edges)."""
    lines = [f"=== Forward Trace: {root_device} ==="]
    lines.append(f"(What does {root_device} influence/control?)\n")
    
    # BFS along outgoing edges
    visited = {root_device}
    queue = deque()  # (device, depth)
    
    # Seed with direct downstream targets
    outgoing = kb.edges_from.get(root_device, [])
    if not outgoing:
        lines.append(f"{root_device} does not directly influence any other device in this program.")
        return "\n".join(lines)
    
    lines.append(f"--- Direct Influences ({len(outgoing)} edges) ---")
    targets_at_depth = {}
    for edge in outgoing:
        target = edge["to"]
        edge_type = edge["edge_type"]
        if target not in targets_at_depth:
            targets_at_depth[target] = []
        targets_at_depth[target].append(edge_type)
        if target not in visited:
            visited.add(target)
            queue.append((target, 1))
    
    for target, types in targets_at_depth.items():
        dev_meta = kb.devices.get(target, {})
        dev_category = dev_meta.get("category", "unknown")
        lines.append(f"  -> [{target}] ({dev_category}) via {', '.join(set(types))}")
    
    # Continue BFS for deeper levels
    current_depth = 1
    while queue and current_depth < max_depth:
        next_level = []
        depth_targets = {}
        
        while queue and queue[0][1] == current_depth:
            dev, depth = queue.popleft()
            dev_outgoing = kb.edges_from.get(dev, [])
            for edge in dev_outgoing:
                target = edge["to"]
                if target not in visited:
                    visited.add(target)
                    next_level.append((target, depth + 1))
                    if target not in depth_targets:
                        depth_targets[target] = {"from": [], "types": []}
                    depth_targets[target]["from"].append(dev)
                    depth_targets[target]["types"].append(edge["edge_type"])
        
        if depth_targets:
            lines.append(f"\n--- Depth {current_depth + 1} ({len(depth_targets)} devices) ---")
            for target, info in list(depth_targets.items())[:20]:
                dev_meta = kb.devices.get(target, {})
                dev_category = dev_meta.get("category", "unknown")
                lines.append(f"  {'  ' * current_depth}-> [{target}] ({dev_category}) from {', '.join(info['from'][:3])}")
            if len(depth_targets) > 20:
                lines.append(f"  {'  ' * current_depth}... +{len(depth_targets) - 20} more")
        
        for item in next_level:
            queue.append(item)
        current_depth += 1
    
    lines.append(f"\nTotal devices in influence chain: {len(visited) - 1}")
    return "\n".join(lines)


@tool
def query_section(section: str) -> str:
    """查询某个程序段的完整逻辑概览：包含所有规则摘要、涉及设备、输入/输出信号分类。

    适用场景：用户想了解某个功能段落（如 CYCLE START、AUTO MODE、MANUAL MODE）的整体逻辑结构。
    不适用：只查某一个具体设备的详情（用query_device）。

    Args:
        section: 程序段名称或关键词，如 "CYCLE START", "AUTO", "MANUAL", "READY"。支持模糊匹配。
    """
    cache = get_tool_cache()
    norm_section = section.strip().upper()
    
    cached = cache.get("query_section", section=norm_section)
    if cached is not None:
        return cached
    
    kb = get_knowledge_base()
    
    # Fuzzy match section name
    matched_sections = []
    for sec in kb.sections:
        if norm_section in sec["name"].upper():
            matched_sections.append(sec)
    
    if not matched_sections:
        sections_list = "\n".join(f"  - {s['name']} (Steps {s['step_start']}-{s['step_end']})" for s in kb.sections)
        return f"No section matching '{section}' found.\n\nAvailable sections:\n{sections_list}"
    
    lines = []
    for sec in matched_sections:
        sec_name = sec["name"]
        step_range = f"{sec['step_start']}-{sec['step_end']}"
        
        # Get all rules in this section
        sec_rules = kb.rules_by_section.get(sec_name, [])
        
        lines.append(f"=== Section: {sec_name} ===")
        lines.append(f"Step range: {step_range}")
        lines.append(f"Total rules: {len(sec_rules)}")
        
        if not sec_rules:
            lines.append("  (No rules in this section)")
            lines.append("")
            continue
        
        # Categorize rules by write type
        by_type = {}
        for r in sec_rules:
            wt = r["write_type"]
            if wt not in by_type:
                by_type[wt] = []
            by_type[wt].append(r)
        
        lines.append(f"Rule types: {', '.join(f'{t}:{len(rs)}' for t, rs in by_type.items())}")
        
        # Identify target devices (outputs of this section)
        targets = set()
        # Identify upstream devices (inputs to this section)
        upstreams = set()
        for r in sec_rules:
            targets.add(r["target_device"])
            for up in r.get("upstream_devices", []):
                upstreams.add(up)
        
        # Inputs = devices that appear in conditions but are not targets in this section
        pure_inputs = upstreams - targets
        
        lines.append(f"\nOutputs (written devices): {len(targets)}")
        target_list = sorted(targets)
        if len(target_list) <= 30:
            lines.append(f"  {', '.join(target_list)}")
        else:
            lines.append(f"  {', '.join(target_list[:25])}, ... (+{len(target_list)-25} more)")
        
        lines.append(f"\nInputs (condition-only devices): {len(pure_inputs)}")
        input_list = sorted(pure_inputs)
        if len(input_list) <= 30:
            lines.append(f"  {', '.join(input_list)}")
        else:
            lines.append(f"  {', '.join(input_list[:25])}, ... (+{len(input_list)-25} more)")
        
        # Show rules (limited)
        lines.append(f"\n--- Rules (showing up to 20) ---")
        for r in sec_rules[:20]:
            lines.append(f"  [{r['write_type']}] {r['target_device']} <- {r['condition_summary']} (Step {r['step']})")
        if len(sec_rules) > 20:
            lines.append(f"  ... +{len(sec_rules) - 20} more rules")
        
        lines.append("")
    
    output = "\n".join(lines)
    cache.put("query_section", output, section=norm_section)
    return output


# ================================================================
# COMMON TOOLS (loaded for all KB types)
# ================================================================

@tool
def query_device(device: str) -> str:
    """查询任意PLC设备的元信息、写入规则和上下游关系概览。

    适用场景：用户想了解某个设备（M、X、T、D、L、B等）的基本信息和关联。
    不适用：想知道设备参与的所有规则详情（用find_related_rules），或报警追溯（用trace_alarm）。

    Args:
        device: PLC设备名，如 "M7", "T0", "X1A", "D500", "L138", "B16FD"。
    """
    cache = get_tool_cache()
    device = device.strip().upper()
    
    cached = cache.get("query_device", device=device)
    if cached is not None:
        return cached
    
    kb = get_knowledge_base()
    info = kb.get_device_info(device)
    
    if info is None:
        return (
            f"Device {device} not found in the knowledge base.\n"
            f"The knowledge base contains {len(kb.devices)} devices.\n"
            f"Device types: F(alarm), M(flag), X(input), Y(output), "
            f"T(timer), C(counter), D(register), B/L(link relay), W(link register)"
        )
    
    meta = info["metadata"]
    comment = info.get("comment", "")
    comment_str = f"\nComment: {comment}" if comment else ""
    lines = [
        f"=== Device: {device} ==={comment_str}",
        f"Type: {meta.get('device_type', '?')} ({meta.get('category', '?')})",
        f"Sections: {', '.join(meta.get('sections', [])[:10])}",
        f"Roles: {', '.join(meta.get('roles', []) if isinstance(meta.get('roles'), list) else list(meta.get('roles_by_program', {}).keys())[:5])}",
        f"Written by {meta.get('written_by_count', 0)} rule(s), Read by {meta.get('read_by_count', 0)} rule(s)",
        "",
    ]
    
    # Write rules
    if info["write_rules"]:
        lines.append(f"--- Write Rules ({len(info['write_rules'])}) ---")
        for r in info["write_rules"]:
            lines.append(f"  [{r['write_type']}] {r['rule_id']} (Step {r['step']}, {r['section']})")
            lines.append(f"       Condition: {r['condition_summary']}")
    else:
        lines.append("--- No write rules (external input / not written in this program) ---")
    
    # Influences (outgoing) - compact
    if info["influences"]:
        targets = {}
        for edge in info["influences"]:
            t = edge["target"]
            if t not in targets:
                targets[t] = []
            targets[t].append(edge["edge_type"])
        lines.append(f"\n--- Influences ({len(targets)} devices) ---")
        for t, types in list(targets.items())[:15]:
            lines.append(f"  -> {t} ({', '.join(set(types))})")
        if len(targets) > 15:
            lines.append(f"  ... +{len(targets)-15} more")
    
    # Influenced by (incoming) - compact
    if info["influenced_by"]:
        sources = {}
        for edge in info["influenced_by"]:
            s = edge["source"]
            if s not in sources:
                sources[s] = []
            sources[s].append(edge["edge_type"])
        lines.append(f"\n--- Influenced by ({len(sources)} devices) ---")
        for s, types in list(sources.items())[:15]:
            lines.append(f"  <- {s} ({', '.join(set(types))})")
        if len(sources) > 15:
            lines.append(f"  ... +{len(sources)-15} more")
    
    output = "\n".join(lines)
    cache.put("query_device", output, device=device)
    return output


@tool
def find_related_rules(device: str) -> str:
    """查找涉及某设备的所有逻辑规则——包括它作为写入目标的规则，和它出现在条件中的规则。

    适用场景：用户想全面了解一个设备在哪里被使用、影响了哪些下游设备。
    比query_device更详细，返回完整的规则列表。

    Args:
        device: PLC设备名，如 "M7", "T0", "X1A", "F65", "L138"。
    """
    cache = get_tool_cache()
    device = device.strip().upper()
    
    cached = cache.get("find_related_rules", device=device)
    if cached is not None:
        return cached
    
    kb = get_knowledge_base()
    result = kb.find_rules_by_device(device)
    
    lines = [f"=== Rules involving {device} ===\n"]
    
    # As target
    as_target = result["as_target"]
    lines.append(f"--- As write target ({len(as_target)} rules) ---")
    if as_target:
        for r in as_target:
            lines.append(f"  [{r['write_type']}] {r['rule_id']} ({r['section']}, Step {r['step']})")
            lines.append(f"       When: {r['condition_summary']}")
    else:
        lines.append("  (none - not written in this program)")
    
    # In conditions
    in_condition = result["in_condition"]
    lines.append(f"\n--- Appears in condition of ({len(in_condition)} rules) ---")
    if in_condition:
        for r in in_condition[:30]:
            lines.append(f"  -> [{r['write_type']}] {r['target_device']} | {r['rule_id']} ({r['section']})")
        if len(in_condition) > 30:
            lines.append(f"  ... +{len(in_condition)-30} more rules")
    else:
        lines.append("  (not found in any rule condition)")
    
    output = "\n".join(lines)
    cache.put("find_related_rules", output, device=device)
    return output


@tool
def get_system_overview() -> str:
    """获取当前PLC程序的整体统计和程序段概览。

    适用场景：用户想了解系统全貌、有多少规则/设备/程序段、系统的整体结构。
    不适用：查具体报警或设备详情。
    """
    kb = get_knowledge_base()
    
    summary = kb.get_summary()
    sections = kb.get_sections_overview()
    
    # Determine program type for display
    has_alarms = summary['loaded']['alarm_traces'] > 0 or summary['loaded'].get('device_traces', 0) > 0
    has_programs = summary['loaded'].get('programs', 0) > 0
    
    lines = [
        "=== PLC Program Overview ===",
        f"Source: {summary.get('input_file', summary.get('line_name', 'Unknown'))}",
        f"",
        f"Statistics:",
        f"  Rules:   {summary['loaded']['rules']} (SET:{summary.get('rules_breakdown',{}).get('SET','?')}, RST:{summary.get('rules_breakdown',{}).get('RST','?')}, OUT:{summary.get('rules_breakdown',{}).get('OUT','?')}, Other:{summary.get('rules_breakdown',{}).get('other','?')})",
        f"  Edges:   {summary['loaded']['edges']}",
        f"  Devices: {summary['loaded']['devices']} ({summary['loaded'].get('comments', 0)} with comments)",
        f"  Sections:{summary['loaded']['sections']}",
    ]
    
    if has_programs:
        lines.append(f"  Programs:{summary['loaded']['programs']}")
        lines.append(f"  Device traces (for cross-program trace): {summary['loaded'].get('device_traces', 0)}")
    
    if summary['loaded'].get('alarm_traces', 0) > 0:
        lines.append(f"  Pre-computed alarm traces: {summary['loaded']['alarm_traces']}")
    
    # Show programs if available
    if kb.programs:
        lines.append(f"\nPrograms ({len(kb.programs)}):")
        for prog in kb.programs:
            lines.append(f"  [{prog['program_no']}] {prog.get('program_name', '')} ({prog['type']}, {prog['rules']} rules)")
    
    # Show sections (grouped by program_no if available)
    lines.append(f"\nProgram Sections ({len(sections)}):")
    # Show first 30 sections
    for sec in sections[:30]:
        prog_prefix = f"[{sec['program_no']}] " if sec.get('program_no') else ""
        alarm_info = f" | {sec['alarm_count']} alarms" if sec['alarm_count'] > 0 else ""
        lines.append(f"  {prog_prefix}{sec['name']} (Steps {sec['step_range']}{alarm_info})")
    if len(sections) > 30:
        lines.append(f"  ... +{len(sections) - 30} more sections")
    
    return "\n".join(lines)


# ================================================================
# LADDER DIAGRAM TOOL
# ================================================================

@tool
def render_ladder(rule_id: str) -> str:
    """将指定规则渲染为梯形图（Ladder Diagram）SVG图形。

    当用户想以图形化方式查看某条规则的逻辑结构时使用。
    rule_id 可以从 trace_alarm、trace_control_logic、find_related_rules 等工具的输出中获取。

    Args:
        rule_id: 规则ID，如 "000-68-OUT-T0", "001-691-SET-F67", "011-128-RST-M4960"。
    """
    kb = get_knowledge_base()
    rule = kb.get_rule_by_id(rule_id.strip())
    
    if not rule:
        # Try fuzzy match
        available_samples = [r["rule_id"] for r in kb.rules[:20]]
        return (
            f"Rule '{rule_id}' not found in the knowledge base.\n"
            f"Rule ID format: <program_no>-<step>-<write_type>-<target_device>\n"
            f"Examples: {available_samples[:5]}"
        )
    
    from plc_agent.ui.ladder_renderer import render_ladder_svg
    svg = render_ladder_svg(rule, kb.comments)
    
    # Wrap in a div for proper HTML rendering
    target = rule.get("target_device", "?")
    write_type = rule.get("write_type", "?")
    condition = rule.get("condition_summary", "")
    
    html = (
        f'<div style="margin: 10px 0; padding: 10px; border: 1px solid #ddd; border-radius: 4px; background: #fff;">'
        f'<div style="font-size: 12px; color: #666; margin-bottom: 5px;">'
        f'<b>{rule_id}</b> | {write_type} {target} | Condition: {condition}</div>'
        f'{svg}'
        f'</div>'
    )
    
    return html


# ================================================================
# TOOL GROUPING (used by graph.py to select tools per KB type)
# ================================================================

# Alarm-type KB: full alarm analysis tools
ALARM_TOOLS = [
    trace_alarm,
    list_alarms,
    query_device,
    find_related_rules,
    query_section,
    get_system_overview,
    render_ladder,
]

# Control-type KB: control logic analysis tools
CONTROL_TOOLS = [
    trace_control_logic,
    query_device,
    find_related_rules,
    query_section,
    get_system_overview,
    render_ladder,
]

# Global-type KB: all tools (cross-program, has both alarms and control logic)
GLOBAL_TOOLS = [
    trace_alarm,
    list_alarms,
    trace_control_logic,
    query_device,
    find_related_rules,
    query_section,
    get_system_overview,
    render_ladder,
]

# All tools (for backward compatibility)
ALL_TOOLS = GLOBAL_TOOLS


def get_tools_for_type(kb_type: str) -> list:
    """
    Get the appropriate tool set for a given knowledge base type.
    
    Args:
        kb_type: "alarm", "control", or "global"
    
    Returns:
        List of LangChain tools appropriate for this KB type.
    """
    if kb_type == "alarm":
        return ALARM_TOOLS
    elif kb_type == "control":
        return CONTROL_TOOLS
    elif kb_type == "global":
        return GLOBAL_TOOLS
    else:
        return ALL_TOOLS
