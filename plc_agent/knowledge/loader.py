"""
PLC Knowledge Base Loader

Loads all JSONL knowledge files into memory with efficient indexing.
Provides query methods used by Agent tools.

Supports two knowledge base formats:
- Legacy: alarm_trace.jsonl with pre-computed recursive trees
- Global: device_traces.jsonl with flat per-device data (trace computed on-the-fly)
"""
import json
from pathlib import Path
from typing import Optional
from collections import defaultdict

# Max depth for on-the-fly recursive trace
MAX_TRACE_DEPTH = 15


class PLCKnowledgeBase:
    """
    In-memory knowledge base loaded from a knowledge output directory.
    
    Provides indexed access to:
    - Alarm traces (backward causal trees) - pre-computed or on-the-fly
    - Logic rules (SET/RST/OUT operations with conditions)
    - Causal edges (device → device relationships)
    - Device catalog (metadata for all PLC devices)
    - Program sections (functional groupings)
    - Device comments (from COMMENT.csv)
    - Device traces (flat per-device write rules, for on-the-fly recursive trace)
    """
    
    def __init__(self, knowledge_dir: str | Path):
        self.knowledge_dir = Path(knowledge_dir)
        
        # Primary data stores
        self.alarm_traces: dict[str, dict] = {}      # device -> pre-computed trace tree
        self.rules: list[dict] = []                   # all rules
        self.edges: list[dict] = []                   # all edges
        self.devices: dict[str, dict] = {}            # device -> metadata
        self.sections: list[dict] = []                # all sections
        self.summary: dict = {}                       # extraction stats
        
        # New: global KB additions
        self.comments: dict[str, str] = {}            # device -> comment text
        self.programs: list[dict] = []                # program info list
        self.device_traces: dict[str, dict] = {}     # device -> flat trace (direct causes only)
        
        # Trace cache: avoids re-computing the same device trace multiple times
        self._trace_cache: dict[str, dict] = {}
        
        # Indexes (built after loading)
        self.rules_by_target: dict[str, list[dict]] = defaultdict(list)
        self.rules_by_section: dict[str, list[dict]] = defaultdict(list)
        self.edges_from: dict[str, list[dict]] = defaultdict(list)
        self.edges_to: dict[str, list[dict]] = defaultdict(list)
        self.alarms_by_section: dict[str, list[str]] = {}
        self.section_by_alarm: dict[str, str] = {}
        
        # Load everything
        self._load_all()
        self._build_indexes()
    
    def _load_jsonl(self, filename: str) -> list[dict]:
        """Load a JSONL file, return list of dicts."""
        filepath = self.knowledge_dir / filename
        if not filepath.exists():
            return []
        records = []
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records
    
    def _load_json(self, filename: str) -> dict | list:
        """Load a JSON file."""
        filepath = self.knowledge_dir / filename
        if not filepath.exists():
            return {}
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    
    def _load_all(self):
        """Load all knowledge base files."""
        # Alarm traces - indexed by device name (may be empty for global KB)
        for record in self._load_jsonl("alarm_trace.jsonl"):
            self.alarm_traces[record["device"]] = record
        
        # Rules
        self.rules = self._load_jsonl("rules.jsonl")
        
        # Edges
        self.edges = self._load_jsonl("edges.jsonl")
        
        # Devices - indexed by device name
        for record in self._load_jsonl("devices.jsonl"):
            self.devices[record["device"]] = record
        
        # Sections
        self.sections = self._load_jsonl("sections.jsonl")
        
        # Summary
        self.summary = self._load_json("summary.json") or {}
        
        # --- New: Global KB format files ---
        
        # Comments
        self.comments = self._load_json("comments.json") or {}
        
        # Programs
        self.programs = self._load_json("programs.json") or []
        
        # Device traces (flat format) - indexed by device name
        for record in self._load_jsonl("device_traces.jsonl"):
            self.device_traces[record["device"]] = record
    
    def _build_indexes(self):
        """Build secondary indexes for fast lookup."""
        # Rules indexed by target device
        for rule in self.rules:
            self.rules_by_target[rule["target_device"]].append(rule)
            if rule.get("section"):
                self.rules_by_section[rule["section"]].append(rule)
        
        # Edges indexed by from/to
        for edge in self.edges:
            self.edges_from[edge["from"]].append(edge)
            self.edges_to[edge["to"]].append(edge)
        
        # Alarms indexed by section
        for section in self.sections:
            alarms = section.get("alarms", [])
            if alarms:
                self.alarms_by_section[section["name"]] = alarms
                for alarm in alarms:
                    self.section_by_alarm[alarm] = section["name"]
    
    @property
    def has_device_traces(self) -> bool:
        """Check if this KB has device_traces (global format)."""
        return len(self.device_traces) > 0
    
    def get_comment(self, device: str) -> str:
        """Get comment for a device, checking both comments dict and device metadata."""
        device = device.strip().upper()
        # First check global comments
        if device in self.comments:
            return self.comments[device]
        # Then check device metadata (global KB stores comment in device record)
        meta = self.devices.get(device, {})
        return meta.get("comment", "")
    
    # ================================================================
    # Query Methods (used by Agent tools)
    # ================================================================
    
    def get_alarm_trace(self, device: str, max_depth: Optional[int] = None) -> Optional[dict]:
        """
        Get the backward trace tree for an alarm device.
        
        For global KB: formats trace on-the-fly from device_traces (flat lookup).
        For legacy KB: returns pre-computed trace from alarm_trace.jsonl.
        """
        device = device.strip().upper()
        
        # If we have pre-computed traces (legacy format), use them
        if device in self.alarm_traces:
            trace = self.alarm_traces[device]
            if max_depth is not None:
                return self._limit_trace_depth(trace, max_depth)
            return trace
        
        # For global KB: check if device has trace info
        if self.has_device_traces and device in self.device_traces:
            # Return a marker dict - actual formatting is done by format_trace_as_text_live
            return {"device": device, "_live_trace": True}
        
        return None
    
    def _trace_recursive(
        self, device: str, depth: int = 0, visited: Optional[set] = None,
        max_depth: int = MAX_TRACE_DEPTH
    ) -> Optional[dict]:
        """
        Recursively trace backward from a device using device_traces data.
        Computes the full tree on-the-fly (with depth and circular reference limits).
        
        Uses instance-level cache: each device is only fully computed once.
        """
        if visited is None:
            visited = set()
        
        device = device.strip().upper()
        
        # Skip constants (K0, K1, H0, etc.) - not traceable devices
        if device.startswith("K") or device.startswith("H"):
            return None
        
        comment = self.get_comment(device)
        
        # Determine device type
        device_type = self._parse_device_type(device)
        category = self._infer_category(device_type)
        
        # Terminal: physical I/O
        if device_type == "X":
            return {"device": device, "device_type": device_type, "category": category,
                    "comment": comment, "depth": depth, "terminal": True, "terminal_reason": "physical_input"}
        if device_type == "Y":
            return {"device": device, "device_type": device_type, "category": category,
                    "comment": comment, "depth": depth, "terminal": True, "terminal_reason": "physical_output"}
        if device_type in ("SM", "SD"):
            return {"device": device, "device_type": device_type, "category": category,
                    "comment": comment, "depth": depth, "terminal": True, "terminal_reason": "system_special_relay"}
        
        # Terminal: depth limit
        if depth >= max_depth:
            return {"device": device, "device_type": device_type, "category": category,
                    "comment": comment, "depth": depth, "terminal": True, "terminal_reason": "depth_limit"}
        
        # Terminal: circular
        if device in visited:
            return {"device": device, "device_type": device_type, "category": category,
                    "comment": comment, "depth": depth, "terminal": True, "terminal_reason": "circular_reference"}
        
        # Check instance cache (avoid recomputing same device in different branches)
        if device in self._trace_cache:
            return self._trace_cache[device]
        
        # Look up in device_traces
        trace_info = self.device_traces.get(device)
        if not trace_info:
            result = {"device": device, "device_type": device_type, "category": category,
                    "comment": comment, "depth": depth, "terminal": True, "terminal_reason": "truly_external"}
            self._trace_cache[device] = result
            return result
        
        visited = visited | {device}
        
        result = {
            "device": device, "device_type": device_type, "category": category,
            "comment": comment, "depth": depth, "terminal": False,
        }
        
        def trace_causes(causes_list):
            traced = []
            for cause in causes_list:
                traced_cause = {
                    "rule_id": cause["rule_id"],
                    "program_no": cause.get("program_no", ""),
                    "section": cause["section"],
                    "step": cause["step"],
                    "write_type": cause.get("write_type", ""),
                    "condition_summary": cause["condition_summary"],
                    "condition_tree": cause.get("condition_tree"),
                    "upstream_devices": cause["upstream_devices"],
                    "upstream_traces": [],
                }
                for up_dev in cause["upstream_devices"]:
                    up_trace = self._trace_recursive(up_dev, depth + 1, visited, max_depth)
                    if up_trace:
                        traced_cause["upstream_traces"].append(up_trace)
                traced.append(traced_cause)
            return traced
        
        if "set_causes" in trace_info:
            result["set_causes"] = trace_causes(trace_info["set_causes"])
        if "reset_causes" in trace_info:
            result["reset_causes"] = trace_causes(trace_info["reset_causes"])
        if "other_writes" in trace_info:
            result["other_writes"] = trace_causes(trace_info["other_writes"])
        
        # Cache the result
        self._trace_cache[device] = result
        return result
    
    def _parse_device_type(self, device: str) -> str:
        """Extract device type prefix."""
        if not device:
            return ""
        device = device.upper()
        for prefix in ("ZR", "SM", "SD", "SW"):
            if device.startswith(prefix):
                return prefix
        i = 0
        while i < len(device) and device[i].isalpha():
            i += 1
        return device[:i] if i > 0 else ""
    
    def _infer_category(self, device_type: str) -> str:
        """Infer category from device type."""
        categories = {
            "F": "alarm_flag", "X": "field_input", "Y": "field_output",
            "M": "internal_flag", "D": "data_register", "R": "file_register",
            "ZR": "indexed_register", "T": "timer", "C": "counter",
            "SM": "system_special_relay", "SD": "system_special_register",
            "L": "link_relay", "B": "link_relay", "W": "link_register",
        }
        return categories.get(device_type, "unknown")
    
    def _limit_trace_depth(self, trace: dict, max_depth: int, current_depth: int = 0) -> dict:
        """Recursively limit trace tree depth."""
        if current_depth >= max_depth:
            result = {k: v for k, v in trace.items() if k not in ("set_causes", "reset_causes", "other_writes")}
            result["terminal"] = True
            result["terminal_reason"] = "depth_limited_by_query"
            return result
        
        result = dict(trace)
        
        for cause_key in ("set_causes", "reset_causes", "other_writes"):
            if cause_key in result and result[cause_key]:
                new_causes = []
                for cause in result[cause_key]:
                    new_cause = dict(cause)
                    if "upstream_traces" in new_cause:
                        new_cause["upstream_traces"] = [
                            self._limit_trace_depth(ut, max_depth, current_depth + 1)
                            for ut in new_cause["upstream_traces"]
                        ]
                    new_causes.append(new_cause)
                result[cause_key] = new_causes
        
        return result
    
    def list_alarms(self, section: Optional[str] = None) -> dict:
        """List all alarms, optionally filtered by section."""
        if section:
            section_upper = section.strip().upper()
            matched = {}
            for sec_name, alarms in self.alarms_by_section.items():
                if section_upper in sec_name.upper():
                    matched[sec_name] = alarms
            return matched if matched else {"error": f"No section matching '{section}' found"}
        
        return dict(self.alarms_by_section)
    
    def get_device_info(self, device: str) -> Optional[dict]:
        """Get metadata and related rules for a device."""
        device = device.strip().upper()
        
        meta = self.devices.get(device)
        if meta is None:
            return None
        
        # Get rules where this device is the target
        write_rules = self.rules_by_target.get(device, [])
        
        # Get edges
        outgoing = self.edges_from.get(device, [])
        incoming = self.edges_to.get(device, [])
        
        # Get comment
        comment = self.get_comment(device)
        
        return {
            "device": device,
            "comment": comment,
            "metadata": meta,
            "write_rules": [
                {
                    "rule_id": r["rule_id"],
                    "write_type": r["write_type"],
                    "section": r["section"],
                    "step": r["step"],
                    "program_no": r.get("program_no", ""),
                    "condition_summary": r["condition_summary"],
                }
                for r in write_rules
            ],
            "influences": [
                {"target": e["to"], "edge_type": e["edge_type"], "rule_id": e["rule_id"]}
                for e in outgoing[:50]
            ],
            "influenced_by": [
                {"source": e["from"], "edge_type": e["edge_type"], "rule_id": e["rule_id"]}
                for e in incoming[:50]
            ],
        }
    
    def find_rules_by_device(self, device: str) -> dict:
        """Find all rules that involve a given device."""
        device = device.strip().upper()
        
        as_target = self.rules_by_target.get(device, [])
        
        in_condition = []
        for rule in self.rules:
            if device in rule.get("upstream_devices", []):
                in_condition.append(rule)
        
        return {
            "device": device,
            "comment": self.get_comment(device),
            "as_target": [
                {
                    "rule_id": r["rule_id"],
                    "write_type": r["write_type"],
                    "section": r["section"],
                    "step": r["step"],
                    "program_no": r.get("program_no", ""),
                    "condition_summary": r["condition_summary"],
                }
                for r in as_target
            ],
            "in_condition": [
                {
                    "rule_id": r["rule_id"],
                    "target_device": r["target_device"],
                    "write_type": r["write_type"],
                    "section": r["section"],
                    "step": r["step"],
                    "program_no": r.get("program_no", ""),
                    "condition_summary": r["condition_summary"],
                }
                for r in in_condition[:100]
            ],
        }
    
    def get_sections_overview(self) -> list[dict]:
        """Get overview of all program sections."""
        return [
            {
                "name": s["name"],
                "program_no": s.get("program_no", ""),
                "step_range": f"{s['step_start']}-{s['step_end']}",
                "alarm_count": len(s.get("alarms", [])),
                "alarms_preview": s.get("alarms", [])[:10],
            }
            for s in self.sections
        ]
    
    def get_summary(self) -> dict:
        """Get knowledge base statistics."""
        return {
            **self.summary,
            "loaded": {
                "alarm_traces": len(self.alarm_traces),
                "device_traces": len(self.device_traces),
                "rules": len(self.rules),
                "edges": len(self.edges),
                "devices": len(self.devices),
                "sections": len(self.sections),
                "comments": len(self.comments),
                "programs": len(self.programs),
            }
        }
    
    def get_rule_by_id(self, rule_id: str) -> Optional[dict]:
        """Find a specific rule by its rule_id."""
        for rule in self.rules:
            if rule["rule_id"] == rule_id:
                return rule
        return None
    
    def get_trace_rule_ids(self, device: str, max_depth: int = MAX_TRACE_DEPTH,
                           _depth: int = 0, _seen: Optional[set] = None) -> list[str]:
        """
        Recursively collect all rule_ids from the backward trace of a device.
        
        Follows the same logic as _format_live_trace:
        - SET/OUT branches: recurse into upstream devices
        - RST branches: collect rule_id but do NOT recurse upstream
        - Terminal conditions: X/Y/SM/K/H/no_write/circular/depth_limit
        
        Returns:
            Ordered list of rule_ids (shallow → deep, SET/OUT first then RST)
        """
        if _seen is None:
            _seen = set()
        
        device = device.strip().upper()
        
        # Terminal conditions (don't collect anything)
        if device.startswith("K") or device.startswith("H"):
            return []
        device_type = self._parse_device_type(device)
        if device_type in ("X", "Y", "SM", "SD"):
            return []
        if _depth >= max_depth:
            return []
        if device in _seen:
            return []
        
        # Look up device_traces
        trace_info = self.device_traces.get(device)
        if not trace_info:
            return []
        
        _seen.add(device)
        rule_ids = []
        
        # SET and OUT causes: collect rule_id + recurse into upstream
        for cause_type in ("set_causes", "other_writes"):
            for cause in trace_info.get(cause_type, []):
                rid = cause.get("rule_id")
                if rid and rid not in rule_ids:
                    rule_ids.append(rid)
                # Recurse into upstream devices
                for up_dev in cause.get("upstream_devices", []):
                    sub_ids = self.get_trace_rule_ids(up_dev, max_depth, _depth + 1, _seen)
                    for sid in sub_ids:
                        if sid not in rule_ids:
                            rule_ids.append(sid)
        
        # RST causes: collect rule_id only (no recursion into upstream)
        for cause in trace_info.get("reset_causes", []):
            rid = cause.get("rule_id")
            if rid and rid not in rule_ids:
                rule_ids.append(rid)
        
        return rule_ids
    
    def format_trace_as_text(self, trace: dict, indent: int = 0, _seen: Optional[set] = None) -> str:
        """Format a trace tree as human-readable indented text.
        
        If trace has '_live_trace' flag, uses live formatting from device_traces
        (no full tree construction needed).
        """
        if trace.get("_live_trace"):
            return self._format_live_trace(trace["device"], max_depth=MAX_TRACE_DEPTH)
        
        # Legacy format: pre-computed tree
        if _seen is None:
            _seen = set()
        
        lines = []
        prefix = "  " * indent
        device = trace["device"]
        category = trace.get("category", "unknown")
        comment = trace.get("comment", "")
        comment_str = f" | {comment}" if comment else ""
        
        lines.append(f"{prefix}[{device}] ({category}){comment_str}")
        
        if trace.get("terminal"):
            reason = trace.get("terminal_reason", "unknown")
            lines.append(f"{prefix}  └── TERMINAL: {reason}")
            return "\n".join(lines)
        
        if device in _seen:
            lines.append(f"{prefix}  └── (already traced above)")
            return "\n".join(lines)
        
        _seen.add(device)
        
        for cause in trace.get("set_causes", []):
            prog = f" @{cause.get('program_no', '')}" if cause.get("program_no") else ""
            lines.append(f"{prefix}  ├── SET by: {cause['rule_id']}{prog} (Step {cause['step']}, Section: {cause['section']})")
            lines.append(f"{prefix}  │   Condition: {cause['condition_summary']}")
            for ut in cause.get("upstream_traces", []):
                lines.append(self.format_trace_as_text(ut, indent + 2, _seen))
        
        for cause in trace.get("reset_causes", []):
            prog = f" @{cause.get('program_no', '')}" if cause.get("program_no") else ""
            lines.append(f"{prefix}  ├── RST by: {cause['rule_id']}{prog} (Step {cause['step']}, Section: {cause['section']})")
            lines.append(f"{prefix}  │   Condition: {cause['condition_summary']}")
            for ut in cause.get("upstream_traces", []):
                lines.append(self.format_trace_as_text(ut, indent + 2, _seen))
        
        for cause in trace.get("other_writes", []):
            prog = f" @{cause.get('program_no', '')}" if cause.get("program_no") else ""
            wt = cause.get('write_type', 'WRITE')
            lines.append(f"{prefix}  ├── {wt} by: {cause['rule_id']}{prog} (Step {cause['step']}, Section: {cause['section']})")
            lines.append(f"{prefix}  │   Condition: {cause['condition_summary']}")
            for ut in cause.get("upstream_traces", []):
                lines.append(self.format_trace_as_text(ut, indent + 2, _seen))
        
        return "\n".join(lines)
    
    def _format_live_trace(self, device: str, max_depth: int = MAX_TRACE_DEPTH,
                           depth: int = 0, indent: int = 0, _seen: Optional[set] = None) -> str:
        """
        Format trace text LIVE from device_traces, walking the flat index.
        
        Key logic (confirmed with user):
        - Only follow devices that HAVE write rules (SET/RST/OUT/MOV/PLS/PLR)
        - Devices without write rules → terminal (truly_external / physical_input etc.)
        - This produces a near-linear chain (each level typically 1-2 devices with write rules)
        - RST causes: show condition + list upstream devices (NO recursion into them)
        """
        if _seen is None:
            _seen = set()
        
        device = device.strip().upper()
        prefix = "  " * indent
        
        # Skip constants
        if device.startswith("K") or device.startswith("H"):
            return ""
        
        comment = self.get_comment(device)
        device_type = self._parse_device_type(device)
        category = self._infer_category(device_type)
        comment_str = f" | {comment}" if comment else ""
        
        lines = [f"{prefix}[{device}] ({category}){comment_str}"]
        
        # Terminal: physical I/O
        if device_type == "X":
            lines.append(f"{prefix}  └── TERMINAL: physical_input")
            return "\n".join(lines)
        if device_type == "Y":
            lines.append(f"{prefix}  └── TERMINAL: physical_output")
            return "\n".join(lines)
        if device_type in ("SM", "SD"):
            lines.append(f"{prefix}  └── TERMINAL: system_special_relay")
            return "\n".join(lines)
        
        # Terminal: depth limit
        if depth >= max_depth:
            lines.append(f"{prefix}  └── TERMINAL: depth_limit")
            return "\n".join(lines)
        
        # Terminal: circular
        if device in _seen:
            lines.append(f"{prefix}  └── (already traced above)")
            return "\n".join(lines)
        
        # Look up device_traces (does this device have write rules?)
        trace_info = self.device_traces.get(device)
        if not trace_info:
            # No write rules in any program → terminal
            lines.append(f"{prefix}  └── TERMINAL: no_write_rule")
            return "\n".join(lines)
        
        _seen.add(device)
        
        # --- SET and OUT causes: show condition and recurse upstream ---
        for cause_type, label in [("set_causes", "SET"), ("other_writes", None)]:
            for cause in trace_info.get(cause_type, []):
                wt = label or cause.get("write_type", "WRITE")
                prog = f" @{cause.get('program_no', '')}" if cause.get("program_no") else ""
                lines.append(f"{prefix}  ├── {wt} by: {cause['rule_id']}{prog} (Step {cause['step']}, Section: {cause['section']})")
                lines.append(f"{prefix}  │   Condition: {cause['condition_summary']}")
                
                # Recurse into upstream devices
                for up_dev in cause.get("upstream_devices", []):
                    sub = self._format_live_trace(up_dev, max_depth, depth + 1, indent + 2, _seen)
                    if sub:
                        lines.append(sub)
        
        # --- RST causes: show condition + list upstream (NO deep recursion) ---
        for cause in trace_info.get("reset_causes", []):
            prog = f" @{cause.get('program_no', '')}" if cause.get("program_no") else ""
            lines.append(f"{prefix}  ├── RST by: {cause['rule_id']}{prog} (Step {cause['step']}, Section: {cause['section']})")
            lines.append(f"{prefix}  │   Condition: {cause['condition_summary']}")
            lines.append(f"{prefix}  │   Clear condition devices:")
            for up_dev in cause.get("upstream_devices", []):
                up_comment = self.get_comment(up_dev)
                up_type = self._parse_device_type(up_dev)
                up_cat = self._infer_category(up_type)
                cmt = f" | {up_comment}" if up_comment else ""
                lines.append(f"{prefix}  │     [{up_dev}] ({up_cat}){cmt}")
        
        return "\n".join(lines)


# ================================================================
# Knowledge Base Registry (multi-program support)
# ================================================================

_kb_registry: dict[str, "PLCKnowledgeBase"] = {}
_active_kb_key: Optional[str] = None


def get_knowledge_base(program_key: Optional[str] = None) -> PLCKnowledgeBase:
    """Get the knowledge base for a specific program, or the currently active one."""
    global _active_kb_key
    
    key = program_key or _active_kb_key
    
    if key is None:
        from plc_agent.config import DEFAULT_PROGRAM_KEY
        key = DEFAULT_PROGRAM_KEY
        _active_kb_key = key
    
    if key not in _kb_registry:
        from plc_agent.config import PROGRAM_REGISTRY
        if key not in PROGRAM_REGISTRY:
            available = list(PROGRAM_REGISTRY.keys())
            raise KeyError(f"Program '{key}' not found in registry. Available: {available}")
        cfg = PROGRAM_REGISTRY[key]
        _kb_registry[key] = PLCKnowledgeBase(cfg["path"])
    
    return _kb_registry[key]


def set_active_kb(program_key: str) -> PLCKnowledgeBase:
    """Set the active knowledge base by program key."""
    global _active_kb_key
    _active_kb_key = program_key
    return get_knowledge_base(program_key)


def get_active_kb_key() -> Optional[str]:
    """Get the current active knowledge base key."""
    return _active_kb_key


def get_active_kb_type() -> str:
    """Get the type of the currently active knowledge base."""
    from plc_agent.config import PROGRAM_REGISTRY, DEFAULT_PROGRAM_KEY
    key = _active_kb_key or DEFAULT_PROGRAM_KEY
    return PROGRAM_REGISTRY.get(key, {}).get("type", "alarm")


def list_available_programs() -> dict[str, dict]:
    """List all registered programs with their metadata."""
    from plc_agent.config import PROGRAM_REGISTRY
    return {
        key: {
            "name": cfg["name"],
            "type": cfg["type"],
            "loaded": key in _kb_registry,
        }
        for key, cfg in PROGRAM_REGISTRY.items()
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    
    print("=== Testing Multi-Program Knowledge Base Registry ===\n")
    
    # Test loading global KB
    print("Loading WH201_CG1 (global)...")
    kb = get_knowledge_base("WH201_CG1")
    print(f"  Rules:         {len(kb.rules)}")
    print(f"  Edges:         {len(kb.edges)}")
    print(f"  Devices:       {len(kb.devices)}")
    print(f"  Sections:      {len(kb.sections)}")
    print(f"  Comments:      {len(kb.comments)}")
    print(f"  Programs:      {len(kb.programs)}")
    print(f"  Device traces: {len(kb.device_traces)}")
    print(f"  Alarm traces:  {len(kb.alarm_traces)} (pre-computed)")
    print()
    
    # Test on-the-fly trace
    print("Testing on-the-fly trace for F1...")
    trace = kb.get_alarm_trace("F1", max_depth=3)
    if trace:
        print(kb.format_trace_as_text(trace))
    else:
        print("  F1 not found")
    print()
    
    # Test device info with comment
    print("Testing device info for M7...")
    info = kb.get_device_info("M7")
    if info:
        print(f"  Comment: {info['comment']}")
        print(f"  Write rules: {len(info['write_rules'])}")
    
    print("\nAvailable programs:")
    for k, v in list_available_programs().items():
        print(f"  [{k}] type={v['type']}, loaded={v['loaded']}")
