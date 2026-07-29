"""
PLC Agent - New Demo UI (Enterprise Report-style Interface)

A clean, enterprise-grade interface for PLC alarm diagnosis.
Users select Region/Line/Equipment + Error Code, then click "Analyze"
to get a full alarm trace with ladder diagrams.

Design:
  - Full-width responsive layout (no fixed max-width)
  - One-line input bar (search-bar style)
  - Clean card-based output area
  - Blue/white Corning theme
  - Markdown + SVG mixed rendering
"""
import re
import uuid
import gradio as gr
from typing import Optional

from plc_agent.config import PROGRAM_REGISTRY, DEFAULT_PROGRAM_KEY
from plc_agent.knowledge.loader import get_knowledge_base, set_active_kb


# ============================================================
# Configuration
# ============================================================

REGIONS = ["WH"]
LINE_NUMBERS = ["201", "202", "203"]
EQUIPMENTS = ["CG1"]


def _make_program_key(region: str, line_no: str, equipment: str) -> str:
    return f"{region}{line_no}_{equipment}"


# ============================================================
# CSS Styles
# ============================================================

CUSTOM_CSS = """
/* === Global === */
.gradio-container {
    width: 92% !important;
    max-width: none !important;
    margin: 0 auto !important;
    padding: 0 !important;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', 'Noto Sans SC', sans-serif !important;
    background: #f8fafc !important;
    min-height: 100vh;
}
.main-container {
    padding: 0 24px;
}

/* === Header === */
.header-section {
    padding: 40px 0 28px 0;
    text-align: center;
}
.header-section h1 {
    font-size: 30px;
    font-weight: 700;
    color: #0f172a;
    margin: 0 0 6px 0;
    letter-spacing: -0.3px;
}
.header-section .subtitle {
    font-size: 14px;
    color: #64748b;
    margin: 0;
    font-weight: 400;
}

/* === Input Card === */
.input-card {
    background: #ffffff !important;
    border: 1px solid #e2e8f0 !important;
    border-radius: 12px !important;
    padding: 20px 24px !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.03) !important;
    margin-bottom: 24px !important;
}

/* === Output Card === */
.output-card {
    background: #ffffff !important;
    border: 1px solid #e2e8f0 !important;
    border-radius: 12px !important;
    padding: 32px !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.03) !important;
    min-height: 300px;
}

/* === Analyze Button === */
.analyze-btn {
    background: #1a56db !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 8px !important;
    font-size: 14px !important;
    font-weight: 600 !important;
    padding: 10px 24px !important;
    cursor: pointer !important;
    transition: all 0.15s ease !important;
    white-space: nowrap !important;
}
.analyze-btn:hover {
    background: #1e429f !important;
    box-shadow: 0 4px 12px rgba(26, 86, 219, 0.25) !important;
}
.analyze-btn:active {
    background: #1e3a8a !important;
}
.analyze-btn[disabled], .analyze-btn.pending {
    background: #93c5fd !important;
    cursor: not-allowed !important;
    box-shadow: none !important;
}

/* === Form Controls === */
.input-card label {
    font-size: 12px !important;
    font-weight: 500 !important;
    color: #64748b !important;
    margin-bottom: 2px !important;
}

/* === Output Content Styling === */
.output-area {
    font-size: 14px;
    line-height: 1.75;
    color: #334155;
}
.output-area h3 {
    font-size: 16px;
    font-weight: 600;
    margin: 28px 0 12px 0;
    color: #0f172a;
    padding-top: 16px;
    border-top: 1px solid #f1f5f9;
}
.output-area h3:first-child {
    border-top: none;
    margin-top: 0;
    padding-top: 0;
}

/* Trace summary card */
.output-area .trace-summary {
    background: #eff6ff;
    border-left: 4px solid #1a56db;
    padding: 16px 20px;
    border-radius: 0 8px 8px 0;
    margin: 16px 0 24px 0;
}
.output-area .trace-summary h3 {
    margin: 0 0 8px 0;
    padding: 0;
    border: none;
    font-size: 17px;
    color: #1e3a5f;
}
.output-area .trace-summary p {
    margin: 4px 0;
    color: #334155;
    font-size: 13px;
}
.output-area .trace-summary code {
    background: #dbeafe;
    color: #1e40af;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 12px;
}

/* Ladder diagram cards */
.output-area .ladder-section {
    margin: 12px 0;
    padding: 16px;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    overflow-x: auto;
    background: #fafbfd;
}
.output-area .ladder-label {
    font-size: 11px;
    color: #64748b;
    margin-bottom: 8px;
    font-weight: 500;
}
.output-area .ladder-label b {
    color: #334155;
    font-weight: 600;
}

/* === Placeholder === */
.placeholder-box {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 60px 24px;
    color: #94a3b8;
}
.placeholder-box .ph-icon {
    font-size: 40px;
    margin-bottom: 12px;
    opacity: 0.6;
}
.placeholder-box .ph-text {
    font-size: 15px;
    font-weight: 400;
}

/* === Markdown Content === */
.md-content {
    line-height: 1.8;
    color: #334155;
}
.md-content h1, .md-content h2, .md-content h3, .md-content h4 {
    color: #0f172a;
    margin-top: 20px;
    margin-bottom: 8px;
}
.md-content h3 { font-size: 15px; font-weight: 600; }
.md-content h4 { font-size: 14px; font-weight: 600; }
.md-content p {
    margin: 8px 0;
}
.md-content code {
    background: #eff6ff;
    color: #1e40af;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 12.5px;
    font-family: 'SF Mono', 'Consolas', 'Menlo', monospace;
}
.md-content pre {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 14px 18px;
    overflow-x: auto;
    font-size: 12.5px;
    line-height: 1.6;
    margin: 12px 0;
}
.md-content pre code {
    background: none;
    padding: 0;
    color: #334155;
}
.md-content table {
    border-collapse: collapse;
    margin: 12px 0;
    width: 100%;
    font-size: 13px;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    overflow: hidden;
}
.md-content table th, .md-content table td {
    border: 1px solid #e2e8f0;
    padding: 10px 14px;
    text-align: left;
}
.md-content table th {
    background: #f8fafc;
    font-weight: 600;
    color: #374151;
}
.md-content table tr:nth-child(even) {
    background: #fbfcfd;
}
.md-content hr {
    border: none;
    border-top: 1px solid #e2e8f0;
    margin: 20px 0;
}
.md-content ul, .md-content ol {
    padding-left: 20px;
    margin: 8px 0;
}
.md-content li {
    margin: 5px 0;
}
.md-content blockquote {
    border-left: 3px solid #1a56db;
    padding: 8px 16px;
    color: #475569;
    background: #f8fafc;
    border-radius: 0 6px 6px 0;
    margin: 12px 0;
}
"""


# ============================================================
# Core Analysis Functions
# ============================================================

def get_f_devices_for_program(program_key: str) -> list[str]:
    """Get list of available F-devices for dropdown suggestions."""
    try:
        kb = get_knowledge_base(program_key)
        f_devices = sorted(
            [d for d in kb.device_traces if d.startswith("F")],
            key=lambda x: int(re.sub(r"[^0-9]", "", x) or "0")
        )
        choices = []
        for f in f_devices:
            comment = kb.get_comment(f)
            label = f"{f} — {comment}" if comment else f
            choices.append(label)
        return choices
    except Exception:
        return []


def generate_svg_diagrams(program_key: str, error_code: str) -> tuple[str, list[str]]:
    """Generate SVG ladder diagrams for an alarm trace (instant, no LLM)."""
    kb = get_knowledge_base(program_key)
    
    device = error_code.strip().upper()
    if not device.startswith("F"):
        device = "F" + device
    
    if device not in kb.device_traces:
        return (f'<div class="placeholder-box"><span class="ph-icon">⚠️</span><span class="ph-text">报警设备 <b>{device}</b> 在当前知识库中未找到</span></div>', [])
    
    comment = kb.get_comment(device)
    trace_info = kb.device_traces.get(device, {})
    set_causes = trace_info.get("set_causes", [])
    rst_causes = trace_info.get("reset_causes", [])
    
    summary_parts = ['<div class="trace-summary">']
    summary_parts.append(f'<h3>{device} — {comment}</h3>')
    if set_causes:
        summary_parts.append(f'<p><b>触发条件 (SET):</b> <code>{set_causes[0]["condition_summary"]}</code> &nbsp; @程序{set_causes[0].get("program_no", "")}</p>')
    if rst_causes:
        summary_parts.append(f'<p><b>清除条件 (RST):</b> <code>{rst_causes[0]["condition_summary"]}</code> &nbsp; @程序{rst_causes[0].get("program_no", "")}</p>')
    summary_parts.append('</div>')
    summary_html = "\n".join(summary_parts)
    
    rule_ids = kb.get_trace_rule_ids(device)
    
    from plc_agent.ui.ladder_renderer import render_ladder_svg
    svg_htmls = []
    for rule_id in rule_ids:
        rule = kb.get_rule_by_id(rule_id)
        if rule:
            svg = render_ladder_svg(rule, kb.comments)
            svg_html = (
                f'<div class="ladder-section">'
                f'<div class="ladder-label">'
                f'<b>{rule_id}</b> &nbsp;|&nbsp; {rule["write_type"]} {rule["target_device"]} '
                f'&nbsp;|&nbsp; {rule["condition_summary"]}</div>'
                f'{svg}</div>'
            )
            svg_htmls.append(svg_html)
    
    return (summary_html, svg_htmls)


def generate_llm_explanation(program_key: str, error_code: str) -> str:
    """
    Generate detailed text explanation by calling LLM directly (no Agent/tools).
    
    This is faster than going through the Agent framework because:
    - No tool calls (LLM won't call trace_alarm which returns large SVG)
    - Single LLM API roundtrip (~3-5s)
    - All needed information is pre-extracted from KB and fed into the prompt
    """
    from langchain_openai import ChatOpenAI
    from plc_agent.config import OPENAI_API_BASE, OPENAI_API_KEY, MODEL_NAME
    
    kb = get_knowledge_base(program_key)
    
    device = error_code.strip().upper()
    if not device.startswith("F"):
        device = "F" + device
    
    # Extract structured info from knowledge base
    comment = kb.get_comment(device)
    trace_info = kb.device_traces.get(device, {})
    set_causes = trace_info.get("set_causes", [])
    rst_causes = trace_info.get("reset_causes", [])
    other_writes = trace_info.get("other_writes", [])
    
    # Build context: SET condition details
    set_context = ""
    if set_causes:
        cause = set_causes[0]
        upstream_details = []
        for up_dev in cause.get("upstream_devices", []):
            up_comment = kb.get_comment(up_dev)
            has_write = up_dev in kb.device_traces
            # Get write info for upstream device if available
            up_trace = kb.device_traces.get(up_dev, {})
            up_write_info = ""
            if up_trace:
                up_set = up_trace.get("set_causes", [])
                up_out = up_trace.get("other_writes", [])
                if up_set:
                    up_write_info = f"，其 SET 条件: {up_set[0]['condition_summary']} @程序{up_set[0].get('program_no','')}"
                elif up_out:
                    up_write_info = f"，其 {up_out[0].get('write_type','OUT')} 条件: {up_out[0]['condition_summary']} @程序{up_out[0].get('program_no','')}"
            status = f"有写入规则{up_write_info}" if has_write else "无写入规则（外部/其他程序信号）"
            upstream_details.append(f"  - {up_dev} ({up_comment}): {status}")
        
        set_context = (
            f"SET 规则: {cause['rule_id']} @程序{cause.get('program_no','')}\n"
            f"SET 条件表达式: {cause['condition_summary']}\n"
            f"SET 上游设备:\n" + "\n".join(upstream_details)
        )
    
    # Build context: RST condition details
    rst_context = ""
    if rst_causes:
        cause = rst_causes[0]
        upstream_details = []
        for up_dev in cause.get("upstream_devices", []):
            up_comment = kb.get_comment(up_dev)
            upstream_details.append(f"  - {up_dev} ({up_comment})")
        
        rst_context = (
            f"RST 规则: {cause['rule_id']} @程序{cause.get('program_no','')}\n"
            f"RST 条件表达式: {cause['condition_summary']}\n"
            f"RST 涉及设备:\n" + "\n".join(upstream_details)
        )
    
    # Build the prompt with all context
    system_prompt = (
        "你是康宁（Corning）CG圆柱磨床的PLC程序分析专家。"
        "请基于以下信息详细分析报警原因。使用中文回答。"
        "回答要专业、详细、有层次。"
    )
    
    user_prompt = f"""请详细分析以下 PLC 报警：

## 报警信息
- 报警设备: {device}
- 报警注释: {comment}

## SET 触发条件
{set_context if set_context else "（无 SET 规则）"}

## RST 清除条件
{rst_context if rst_context else "（无 RST 规则）"}

## 分析要求
1. 先说明报警本质（这个报警代表什么物理故障/状态）
2. 详细解释 SET 触发条件中每个设备的物理含义，为什么这些条件组合会触发报警
3. 详细解释 RST 清除条件的含义，说明如何清除这个报警
4. 给出现场排查结论和建议

重点突出，层次清晰。
"""
    
    try:
        llm = ChatOpenAI(
            model=MODEL_NAME,
            base_url=OPENAI_API_BASE,
            api_key=OPENAI_API_KEY,
            temperature=1.0,
        )
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        
        response = llm.invoke(messages)
        return response.content if response.content else "LLM 未返回有效回答。"
    
    except Exception as e:
        return f"文字分析生成失败: {type(e).__name__}: {str(e)}"


# ============================================================
# Main Analysis Handler
# ============================================================

def run_analysis(region, line_no, equipment, error_code):
    """Main handler when user clicks Analyze."""
    if not error_code or not error_code.strip():
        return '<div class="placeholder-box"><span class="ph-icon">⚠️</span><span class="ph-text">请输入报警代码（如 F67）</span></div>'
    
    device = error_code.strip().upper()
    if "—" in device:
        device = device.split("—")[0].strip()
    if " " in device:
        device = device.split(" ")[0].strip()
    if not device.startswith("F"):
        device = "F" + device
    
    program_key = _make_program_key(region, line_no, equipment)
    if program_key not in PROGRAM_REGISTRY:
        return f'<div class="placeholder-box"><span class="ph-icon">⚠️</span><span class="ph-text">产线 {program_key} 尚未接入系统</span></div>'
    
    set_active_kb(program_key)
    summary_html, svg_htmls = generate_svg_diagrams(program_key, device)
    
    if not svg_htmls:
        return summary_html
    
    # LLM explanation
    llm_explanation = generate_llm_explanation(program_key, device)
    explanation_html = _markdown_to_html(llm_explanation)
    
    # Assemble output: summary → ladder diagrams → text analysis
    output_parts = [
        '<div class="output-area">',
        summary_html,
        '<h3>回溯梯形图</h3>',
    ]
    output_parts.extend(svg_htmls)
    output_parts.append('<h3>详细分析</h3>')
    output_parts.append('<div style="margin: 16px 0;">')
    output_parts.append(explanation_html)
    output_parts.append('</div>')
    output_parts.append('</div>')
    
    return "\n".join(output_parts)


def _markdown_to_html(text: str) -> str:
    """Convert markdown text to HTML."""
    import markdown
    html = markdown.markdown(
        text,
        extensions=['tables', 'fenced_code', 'nl2br', 'sane_lists'],
    )
    return f'<div class="md-content">{html}</div>'


# ============================================================
# UI Builder
# ============================================================

def build_ui() -> gr.Blocks:
    """Build the enterprise-grade demo interface."""
    
    with gr.Blocks(title="PLC 智能诊断 Agent") as demo:
        
        # Header
        gr.HTML('''
            <div class="header-section">
                <h1>PLC 智能诊断 Agent</h1>
                <p class="subtitle">AI 驱动的报警回溯分析 · 梯形图可视化 · 自动追溯至物理 I/O 层</p>
            </div>
        ''')
        
        # Input card — one-line search bar style
        with gr.Group(elem_classes="input-card"):
            with gr.Row():
                region_dd = gr.Dropdown(
                    choices=REGIONS,
                    value="WH",
                    label="Region",
                    scale=1,
                    interactive=True,
                    min_width=80,
                )
                line_dd = gr.Dropdown(
                    choices=LINE_NUMBERS,
                    value="201",
                    label="产线",
                    scale=1,
                    interactive=True,
                    min_width=80,
                )
                equip_dd = gr.Dropdown(
                    choices=EQUIPMENTS,
                    value="CG1",
                    label="设备",
                    scale=1,
                    interactive=True,
                    min_width=80,
                )
                
                initial_program = _make_program_key("WH", "201", "CG1")
                initial_choices = get_f_devices_for_program(initial_program)
                
                error_input = gr.Dropdown(
                    choices=initial_choices,
                    value=None,
                    label="Error Code",
                    allow_custom_value=True,
                    scale=4,
                    min_width=200,
                    info="输入或选择 F 设备",
                )
                
                analyze_btn = gr.Button(
                    "开始分析",
                    variant="primary",
                    scale=1,
                    min_width=120,
                    elem_classes="analyze-btn",
                )
        
        # Output area
        output_html = gr.HTML(
            value='''
                <div class="placeholder-box">
                    <span class="ph-icon">📊</span>
                    <span class="ph-text">选择产线并输入报警代码，点击「开始分析」</span>
                </div>
            ''',
            elem_classes="output-card",
        )
        
        # --- Event Handlers ---
        
        def update_error_suggestions(region, line_no, equipment):
            program_key = _make_program_key(region, line_no, equipment)
            if program_key in PROGRAM_REGISTRY:
                choices = get_f_devices_for_program(program_key)
                return gr.update(choices=choices, value=None)
            return gr.update(choices=[], value=None)
        
        def on_analyze_click(region, line_no, equipment, error_code):
            if not error_code:
                return '<div class="placeholder-box"><span class="ph-icon">⚠️</span><span class="ph-text">请输入报警代码</span></div>'
            return run_analysis(region, line_no, equipment, error_code)
        
        for dd in [region_dd, line_dd, equip_dd]:
            dd.change(
                fn=update_error_suggestions,
                inputs=[region_dd, line_dd, equip_dd],
                outputs=[error_input],
            )
        
        analyze_btn.click(
            fn=on_analyze_click,
            inputs=[region_dd, line_dd, equip_dd, error_input],
            outputs=[output_html],
        )
    
    return demo


# ============================================================
# Launch
# ============================================================

def launch_ui(share: bool = False, server_port: int = 7860):
    """Launch the Gradio UI."""
    print("Loading knowledge base...")
    get_knowledge_base(DEFAULT_PROGRAM_KEY)
    
    print("Launching PLC Diagnostic Agent UI...")
    demo = build_ui()
    demo.launch(
        share=share,
        server_port=server_port,
        server_name="0.0.0.0",
        css=CUSTOM_CSS,
    )


if __name__ == "__main__":
    launch_ui()
