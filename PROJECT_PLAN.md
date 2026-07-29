# PLC Intelligent Diagnostic Agent - Project Plan

> **Project**: PLC 智能诊断 Agent 系统  
> **Owner**: wangy229  
> **Start Date**: 2026-07-02  
> **Status**: Phase 1 - Infrastructure Setup  

---

## 1. Project Overview

### Goal
将已完成的 PLC 报警逻辑提取系统（`71_rule_extract.py` + `query_alarm.py`）升级为一个**对话式智能诊断 Agent**。用户通过自然语言提问，Agent 自动分析意图并调用知识库工具，返回结构化的诊断结果。

### Current Assets
- `71_rule_extract.py` — PLC CSV 解析引擎（929行），生成结构化知识图谱
- `query_alarm.py` — CLI 查询工具（283行）
- `plc_knowledge_out/` — 知识库输出
  - 602 条报警追溯 (`alarm_trace.jsonl`)
  - 1796 条逻辑规则 (`rules.jsonl`)
  - 6820 条因果边 (`edges.jsonl`)
  - 2141 个设备 (`devices.jsonl`)
  - 20 个程序段 (`sections.jsonl`)

### Target Architecture
```
User ──> Gradio UI / API ──> LangGraph Agent ──> Tools ──> Knowledge Base
                                    │
                          Corning AI Platform
                           (GPT/Claude/Qwen)
```

---

## 2. Technology Stack

| Layer | Technology | Version | Purpose |
|-------|-----------|---------|---------|
| LLM Gateway | Corning AI Platform | - | GPT-4o / Claude / Qwen |
| LLM Client | langchain-openai | >=0.2.0 | OpenAI-compatible API calls |
| Agent Framework | LangGraph | >=0.2.0 | Stateful agent graph orchestration |
| API Server | FastAPI | >=0.115.0 | REST API backend |
| Frontend | Gradio | >=5.0.0 | Demo chat interface |
| Config | python-dotenv | >=1.0.0 | Environment management |

---

## 3. Implementation Phases

### Phase 1: Infrastructure Setup ← CURRENT
- [x] Create project directory structure
- [x] Create `requirements.txt`
- [x] Write `.env.example` + `config.py`
- [x] Write API connection test script
- [x] Write this project plan document
- [ ] **USER ACTION**: Install dependencies (`pip install -r requirements.txt`)
- [ ] **USER ACTION**: Copy `.env.example` → `.env`, fill in API key
- [ ] **USER ACTION**: Run `python test_api_connection.py` to verify

### Phase 2: Knowledge Tools Layer ← DONE
- [x] `plc_agent/knowledge/loader.py` — Load JSONL data into memory with indexing
- [x] `plc_agent/tools/plc_tools.py` — Define LangChain Tools with proper schemas
- [ ] Unit tests for tools (deferred to after API verification)

**Tools implemented:**
| Tool | Function | Input |
|------|----------|-------|
| `trace_alarm` | 追溯报警完整因果链 | `device: str`, `max_depth: int` |
| `list_alarms` | 列出报警（可按section过滤） | `section: str` (optional) |
| `query_device` | 查询设备的读写规则 | `device: str` |
| `find_related_rules` | 查找包含某设备的所有规则 | `device: str` |
| `get_system_overview` | 获取系统统计和程序段概览 | (无参数) |

### Phase 3: LangGraph Agent Core ← DONE
- [x] `plc_agent/agent/prompts.py` — System prompt (中英双语 PLC domain expert)
- [x] `plc_agent/agent/graph.py` — ReAct Agent (LangGraph create_react_agent)
- [x] Multi-turn conversation memory (via thread_id)
- [x] CLI interactive mode for testing

### Phase 4: API + Frontend ← DONE
- [x] `plc_agent/api/server.py` — FastAPI backend (`/chat`, `/health`, `/alarms`, `/devices`, `/sections`)
- [x] `plc_agent/ui/app.py` — Gradio chat interface with examples
- [x] `run.py` — Unified startup script (cli/api/ui/test modes)
- [ ] End-to-end testing (after API verification)

---

## 4. Project Structure

```
PLC/
├── plc_agent/                    # Main package
│   ├── __init__.py
│   ├── config.py                 # Configuration (API key, model, paths)
│   ├── knowledge/                # Knowledge base loading & querying
│   │   ├── __init__.py
│   │   └── loader.py            # JSONL data loading with indexes
│   ├── tools/                    # LangChain Tool definitions
│   │   ├── __init__.py
│   │   └── plc_tools.py         # trace_alarm, query_device, etc.
│   ├── agent/                    # LangGraph Agent
│   │   ├── __init__.py
│   │   ├── graph.py             # StateGraph definition
│   │   └── prompts.py           # System prompts
│   ├── api/                      # FastAPI backend
│   │   ├── __init__.py
│   │   └── server.py
│   └── ui/                       # Gradio frontend
│       ├── __init__.py
│       └── app.py
├── plc_knowledge_out/            # Generated knowledge base (existing)
├── tests/                        # Test suite
│   └── __init__.py
├── 71_rule_extract.py            # Existing: PLC CSV parser
├── query_alarm.py                # Existing: CLI query tool
├── test_api_connection.py        # API connectivity test
├── run.py                        # Unified startup entry point
├── requirements.txt              # Python dependencies
├── .env.example                  # Environment template
├── .env                          # Actual config (git-ignored)
└── PROJECT_PLAN.md               # This file
```

---

## 5. Key Design Decisions

### Why LangGraph (not raw LangChain AgentExecutor)?
- **Explicit control flow**: Can define exactly when to call tools vs. respond
- **State management**: Built-in conversation state, extensible to multi-agent
- **Streaming**: Native support for streaming responses
- **Debuggability**: Clear graph visualization, step-by-step execution trace
- **Future-proof**: Easy to add conditional routing, human-in-the-loop, etc.

### Why NOT deepagents?
- Not a mainstream/well-maintained library
- LangGraph alone covers all needed functionality
- Reduces dependency risk and debugging complexity

### Knowledge Base Strategy
- **In-memory loading**: JSONL files are small (~2MB total), load everything at startup
- **Pre-built indexes**: Dict-based lookup by device name, section, rule_id
- **No vector DB needed** (for now): Structured lookup is more reliable than semantic search for PLC device queries

---

## 6. Example User Interactions (Target)

```
User: F65报警是什么原因触发的？
Agent: [calls trace_alarm(device="F65")]
       F65 报警位于 "COMMON HEAVY ALARM" 段。
       
       触发条件 (SET):
       ├── 规则 001-2580-SET-F65 (Step 2580)
       │   条件: M500 AND X1A (M500激活 且 外部输入X1A为ON)
       │   ├── M500 ← 由 ... 驱动
       │   └── X1A ← 外部输入信号（无上游程序写入）
       ...

User: 这个报警涉及到哪些外部输入信号？
Agent: [uses previous context + calls trace_alarm with analysis]
       F65 报警链中涉及的外部输入信号(X设备)有:
       - X1A: 终端信号（无上游，来自现场传感器）
       - X2B: ...

User: M500 是在哪里被置位的？
Agent: [calls query_device(device="M500")]
       M500 相关规则:
       - SET by rule 001-xxx (条件: ...)
       - RST by rule 001-yyy (条件: ...)
       - 被读取于: F65, F66, F67 的触发条件中
```

---

## 7. Risk & Mitigation

| Risk | Impact | Mitigation |
|------|--------|------------|
| API Gateway 不完全兼容 OpenAI format | 高 | test_api_connection.py 提前验证; 备选用 httpx 直接调用 |
| Function Calling 不支持 | 高 | 降级方案: 用 prompt engineering 实现伪工具调用 |
| 模型中文理解不够好 | 中 | 可切换到 Qwen; 或 prompt 用英文+中文混合 |
| 知识库数据量增长 | 低 | 当前2MB，内存足够; 未来可加 SQLite |

---

## 8. Changelog

| Date | Change |
|------|--------|
| 2026-07-02 | Project initiated. All 4 phases code skeleton created in one session. |
| 2026-07-02 | Fixed: gpt-5.5 doesn't support temperature!=1. Removed temperature param. |
| 2026-07-02 | Fixed: chat_with_agent message extraction logic (tool_calls is always present as empty list). |
| 2026-07-02 | **E2E verified**: Agent correctly calls tools and generates structured Chinese responses. |
| 2026-07-02 | **Multi-turn memory**: Added MemorySaver checkpointer. Agent now remembers conversation context. |
| 2026-07-02 | CLI: Added `/new` (new session) and `/history` commands. |
| 2026-07-02 | UI: Per-session thread_id + "新建对话" button. |
| 2026-07-02 | API: Added `/chat/history` endpoint. |
| 2026-07-03 | **Prompt optimized**: Full trace tree preserved, structured templates, multi-turn conciseness. |
| 2026-07-03 | **Cache system**: Two-level cache (Tool + Query). Same/similar questions return in 0ms vs ~10s. |
| 2026-07-03 | Cache: Query normalization handles Chinese text + intent detection + device extraction. |
| 2026-07-03 | API: Added `/cache/stats` and `/cache/clear` endpoints. |

---

## 9. Current Status: FULLY OPERATIONAL

All core functionality is working. Available commands:

```bash
venv\Scripts\activate
python run.py cli            # Terminal interactive chat
python run.py ui             # Gradio web UI (port 7860)
python run.py api            # FastAPI REST server (port 8000)
```

## 10. Troubleshooting

| Problem | Solution |
|---------|----------|
| `Import "dotenv" could not be resolved` | Run `pip install -r requirements.txt` in venv |
| `ModuleNotFoundError` | Make sure venv is activated: `venv\Scripts\activate` |
| API: `temperature does not support 0` | Already fixed - using default temperature |
| API connection test fails | Check VPN, verify `OPENAI_API_BASE` in .env |
| Function calling not supported | Switch MODEL_NAME to gpt-4o or gpt-5.5 |
| Agent returns raw tool output | Fixed in chat_with_agent message extraction |
| Gradio UI doesn't load | Check port 7860 is not in use |
| Windows encoding error in CLI | Use `python -X utf8 run.py cli` |
