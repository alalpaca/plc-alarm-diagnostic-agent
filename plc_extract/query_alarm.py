"""
query_alarm.py - PLC Alarm Backward Trace Query Tool

Usage:
  python query_alarm.py F65
  python query_alarm.py F1001
  python query_alarm.py F65 --depth 3
  python query_alarm.py --list              (list all alarms)
  python query_alarm.py --section           (list sections)
"""

import json
import sys
import os
import io

# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DATA_DIR = "plc_knowledge_out"


def load_traces():
    traces = {}
    path = os.path.join(DATA_DIR, "alarm_trace.jsonl")
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            traces[obj["device"]] = obj
    return traces


def load_sections():
    sections = []
    path = os.path.join(DATA_DIR, "sections.jsonl")
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            sections.append(json.loads(line))
    return sections


def print_trace_tree(node, indent=0, max_depth=10):
    """Recursively print a trace node as a tree."""
    prefix = "  " * indent
    device = node.get("device", "?")
    category = node.get("category", "")
    terminal = node.get("terminal", False)
    terminal_reason = node.get("terminal_reason", "")

    # Format device info
    cat_label = f" [{category}]" if category else ""
    if terminal:
        reason_map = {
            "external_input": "外部输入/其他程序写入",
            "max_depth_reached": "达到最大回溯深度",
            "circular_reference": "循环引用",
        }
        reason = reason_map.get(terminal_reason, terminal_reason)
        print(f"{prefix}{'*'} {device}{cat_label} -- 终端: {reason}")
        return

    if indent >= max_depth:
        print(f"{prefix}{'*'} {device}{cat_label} -- (截断: 显示深度限制)")
        return

    # Print SET causes
    for cause in node.get("set_causes", []):
        if indent == 0:
            print(f"{prefix}[SET] 条件: {cause['condition_summary']}")
            print(f"{prefix}      步号: {cause['step']} | 规则: {cause['rule_id']}")
            print(f"{prefix}      上游设备:")
        for trace in cause.get("upstream_traces", []):
            print_trace_tree(trace, indent + 1, max_depth)

    # Print other writes
    for cause in node.get("other_writes", []):
        if indent > 0:
            print(f"{prefix}  {device}{cat_label}")
            print(f"{prefix}    写入方式: {cause['write_type']} | 条件: {cause['condition_summary']}")
            print(f"{prefix}    步号: {cause['step']} | 段落: {cause.get('section', '?')}")
            for trace in cause.get("upstream_traces", []):
                print_trace_tree(trace, indent + 2, max_depth)


def print_alarm_info(alarm, max_depth=10):
    """Print full alarm backward trace."""
    device = alarm["device"]
    section = alarm.get("section", "未知")
    device_type = alarm.get("device_type", "?")
    category = alarm.get("category", "?")

    print("=" * 70)
    print(f"  报警设备: {device}")
    print(f"  设备类型: {device_type} ({category})")
    print(f"  所属段落: {section}")
    print("=" * 70)

    # SET causes (trigger)
    set_causes = alarm.get("set_causes", [])
    if set_causes:
        print(f"\n{'─' * 35}")
        print(f"  触发条件 (SET) - 共 {len(set_causes)} 条路径")
        print(f"{'─' * 35}")
        for i, cause in enumerate(set_causes, 1):
            print(f"\n  路径 {i}:")
            print(f"    条件表达式: {cause['condition_summary']}")
            print(f"    步号: {cause['step']}")
            print(f"    规则ID: {cause['rule_id']}")
            print(f"    直接上游: {', '.join(cause['upstream_devices'])}")

            # Print upstream traces
            if cause.get("upstream_traces"):
                print(f"\n    回溯树:")
                for trace in cause["upstream_traces"]:
                    print_upstream(trace, depth=2, max_depth=max_depth)

    # RST causes (reset)
    rst_causes = alarm.get("reset_causes", [])
    if rst_causes:
        print(f"\n{'─' * 35}")
        print(f"  复位条件 (RST) - 共 {len(rst_causes)} 条路径")
        print(f"{'─' * 35}")
        for i, cause in enumerate(rst_causes, 1):
            print(f"\n  路径 {i}:")
            print(f"    条件表达式: {cause['condition_summary']}")
            print(f"    步号: {cause['step']}")
            print(f"    规则ID: {cause['rule_id']}")
            print(f"    直接上游: {', '.join(cause['upstream_devices'])}")

            if cause.get("upstream_traces"):
                print(f"\n    回溯树:")
                for trace in cause["upstream_traces"]:
                    print_upstream(trace, depth=2, max_depth=max_depth)

    print(f"\n{'=' * 70}")


def print_upstream(node, depth=0, max_depth=10):
    """Print upstream trace as indented tree."""
    indent = "    " + "  │ " * depth
    device = node.get("device", "?")
    category = node.get("category", "")
    terminal = node.get("terminal", False)
    terminal_reason = node.get("terminal_reason", "")

    cat_short = {
        "alarm_flag": "报警",
        "field_input": "物理输入",
        "field_output": "物理输出",
        "internal_flag": "内部标志",
        "data_register": "数据寄存器",
        "file_register": "文件寄存器",
        "indexed_register": "变址寄存器",
        "timer": "定时器",
        "counter": "计数器",
        "system_special_relay": "系统特殊",
        "link_relay": "链接继电器",
        "link_register": "链接寄存器",
    }.get(category, category)

    if terminal:
        reason_map = {
            "external_input": "外部/其他程序",
            "max_depth_reached": "达到最大深度",
            "circular_reference": "循环引用",
        }
        reason = reason_map.get(terminal_reason, terminal_reason)
        print(f"{indent}└─ {device} [{cat_short}] ← {reason}")
        return

    if depth >= max_depth:
        print(f"{indent}└─ {device} [{cat_short}] ← (截断)")
        return

    # Has writes - show them
    has_content = False

    for cause in node.get("set_causes", []):
        has_content = True
        print(f"{indent}├─ {device} [{cat_short}] ← SET: {cause['condition_summary']}")
        print(f"{indent}│  (步号:{cause['step']} 段落:{cause.get('section','?')})")
        for trace in cause.get("upstream_traces", []):
            print_upstream(trace, depth + 1, max_depth)

    for cause in node.get("other_writes", []):
        has_content = True
        print(f"{indent}├─ {device} [{cat_short}] ← {cause['write_type']}: {cause['condition_summary']}")
        print(f"{indent}│  (步号:{cause['step']} 段落:{cause.get('section','?')})")
        for trace in cause.get("upstream_traces", []):
            print_upstream(trace, depth + 1, max_depth)

    for cause in node.get("reset_causes", []):
        has_content = True
        print(f"{indent}├─ {device} [{cat_short}] ← RST: {cause['condition_summary']}")
        print(f"{indent}│  (步号:{cause['step']} 段落:{cause.get('section','?')})")
        for trace in cause.get("upstream_traces", []):
            print_upstream(trace, depth + 1, max_depth)

    if not has_content:
        print(f"{indent}└─ {device} [{cat_short}]")


def list_all_alarms(traces):
    """List all alarm devices grouped by section."""
    by_section = {}
    for dev, trace in sorted(traces.items(), key=lambda x: int(x[0][1:]) if x[0][1:].isdigit() else 0):
        sec = trace.get("section", "未知")
        if sec not in by_section:
            by_section[sec] = []
        by_section[sec].append(dev)

    print(f"\n所有报警设备 (共 {len(traces)} 个):\n")
    for sec, devs in by_section.items():
        print(f"  [{sec}] ({len(devs)}个)")
        # Show first/last if too many
        if len(devs) > 10:
            print(f"    {', '.join(devs[:5])}, ..., {', '.join(devs[-3:])}")
        else:
            print(f"    {', '.join(devs)}")
    print()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    arg = sys.argv[1].upper()

    if arg == "--LIST":
        traces = load_traces()
        list_all_alarms(traces)
        return

    if arg == "--SECTION":
        sections = load_sections()
        print("\n程序段落清单:\n")
        for sec in sections:
            alarm_count = len(sec.get("alarms", []))
            print(f"  {sec['name']}")
            print(f"    步号范围: {sec['step_start']} ~ {sec['step_end']}")
            print(f"    报警数量: {alarm_count}")
            if alarm_count > 0 and alarm_count <= 8:
                print(f"    报警列表: {', '.join(sec['alarms'])}")
            elif alarm_count > 8:
                alarms = sec['alarms']
                print(f"    报警列表: {', '.join(alarms[:4])}, ..., {', '.join(alarms[-2:])}")
            print()
        return

    # Query specific alarm
    device = arg if arg.startswith("F") else f"F{arg}"

    # Parse optional --depth
    max_depth = 10
    if "--depth" in [a.lower() for a in sys.argv]:
        idx = [a.lower() for a in sys.argv].index("--depth")
        if idx + 1 < len(sys.argv):
            try:
                max_depth = int(sys.argv[idx + 1])
            except ValueError:
                pass

    traces = load_traces()

    if device not in traces:
        print(f"\n  未找到报警设备 {device}")
        print(f"  可用的报警设备范围: F1 ~ F{max(int(d[1:]) for d in traces.keys() if d[1:].isdigit())}")

        # Suggest similar
        prefix = device[:2]
        similar = [d for d in sorted(traces.keys()) if d.startswith(prefix)][:10]
        if similar:
            print(f"  以 {prefix} 开头的设备: {', '.join(similar)}")
        return

    alarm = traces[device]
    print_alarm_info(alarm, max_depth=max_depth)


if __name__ == "__main__":
    main()
