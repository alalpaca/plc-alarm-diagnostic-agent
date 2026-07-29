# PLC 智能诊断系统

基于 LangGraph 的三菱 Q 系列 PLC 程序分析与报警诊断 Agent。支持**跨程序因果链回溯**（29个子程序合并分析），自动追溯到物理 I/O 层（X/Y 端子）。

---

## 一、项目背景

在CG 圆柱磨床上，三菱 Q 系列 PLC 负责监控设备状态。当传感器异常、气压不足、安全链断开时，PLC 会触发报警（F 设备）。

**核心痛点**：报警触发后，工程师需要从报警输出一步步往前追溯"为什么会报这个警"——涉及多个子程序的交叉引用，人工翻阅困难。

**本系统的目标**：
- 输入一个报警编号（如 F XX），自动返回跨程序的完整因果链
- 追溯到最终的物理输入（X 端子）或物理输出（Y 端子）
- 附带每个设备的中文/英文注释，帮助理解物理含义
- 通过自然语言对话交互，无需翻阅程序

---

## 二、系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                     用户交互层                                │
│    Gradio UI (run.py ui)  │  CLI (run.py cli)  │  API       │
└───────────────┬───────────────────────────────┬─────────────┘
                │                               │
┌───────────────▼───────────────────────────────▼─────────────┐
│                   LangGraph Agent                             │
│   System Prompt (动态) + Tools (7个) + Memory (SQLite)        │
└───────────────┬──────────────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────────┐
│               知识库层 (PLCKnowledgeBase)                      │
│   rules.jsonl │ device_traces.jsonl │ comments.json │ ...     │
└───────────────┬──────────────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────────┐
│               离线提取层                                       │
│   batch_extract.py ← 29个CSV + COMMENT.csv                   │
└──────────────────────────────────────────────────────────────┘
```

### 两大模块

| 模块 | 职责 | 运行时机 |
|------|------|----------|
| **离线提取** (`batch_extract.py`) | 解析 PLC CSV 文件，生成结构化知识库 | 每次 PLC 程序更新时运行一次 |
| **在线 Agent** (`run.py ui/cli/api`) | 加载知识库，通过 LLM+Tools 回答用户问题 | 日常使用 |

---

## 三、数据来源

### PLC 程序文件

每台 CG 机器有 29 个子程序文件（000.csv ~ 960.csv），从三菱 GX Works2 导出：

| 程序编号 | 功能 | 典型内容 |
|----------|------|----------|
| 000 | 主控制程序 | 模式切换、CYCLE START/STOP、SYSTEM ON |
| 001 | 报警程序 | 654 个 F 设备的 SET/RST 逻辑 |
| 005-013 | 传送/搬运 | Unit 动作控制 |
| 100-120 | 工艺控制 | 磨削参数、伺服指令 |
| 200-260 | 砂轮/刀具 | 修整、换刀逻辑 |
| 700 | 伺服通讯 | Motion CPU 接口 |
| 800-960 | 辅助功能 | 冷却、气压、通讯 |

### COMMENT.csv

设备注释表（62,447 条），为每个 PLC 地址提供英文注释：
```
M4521 → "STA#1 Cycle Start"
X100D → "MAINTENANCE KEY"
F702  → "MAIN AIR PRESSURE ALARM"
```

### 设备地址含义

| 前缀 | 含义 | 举例 |
|------|------|------|
| F | 报警标志位 | F702 = 主气压报警 |
| M | 内部标志位 | M4960 = 维护模式 |
| X | 物理输入 | X33F = 急停按钮 |
| Y | 物理输出 | Y100 = 接触器 |
| T | 定时器 | T0 = 系统ON定时器 |
| D | 数据寄存器 | D7320 = 电流值 |
| L/B | 链接继电器 | L1007 = 来自其他Station |
| SM | 系统特殊继电器 | SM400 = 常ON |

---

## 四、离线提取流程

### 4.1 单文件提取（legacy）

```bash
python plc_extract/71_rule_extract.py
```

处理单个 CSV 文件，生成单文件知识库。已被 `batch_extract.py` 取代。

### 4.2 批量提取（当前使用）

```bash
python plc_extract/batch_extract.py --input plc_extract/plc_file --output plc_knowledge_out_WH201_CG1 --line WH201_CG1
```

**处理流程**：

```
1. 扫描输入目录下所有 *.csv（排除 COMMENT.csv）
2. 解析 COMMENT.csv → 62,447 条设备注释
3. 逐文件解析（复用 71_rule_extract.py 核心函数）：
   - 文件编码检测（UTF-16）
   - Tab 分割 + 续行合并
   - 栈模拟器还原分支逻辑
   - 提取写入规则（SET/RST/OUT/MOV/DMOV/INCP/D+/D-...）
4. 合并全局数据（rules/edges/devices/sections）
5. 构建 device_traces（每设备的直接写入规则，扁平格式）
6. 过滤 K/H 常量设备（不可追溯）
7. 输出到指定目录
```

### 4.3 跨程序回溯算法

**核心设计**（与 user 确认的方案）：

> 对于每个设备的前溯，只寻找"写入"指令（OUT/SET/RST/MOV/PLS/PLR），由此构建前溯主链，直到追溯到 X/Y 等 I/O 设备。

**回溯逻辑**：

```
给定目标设备 D:
1. 查 device_traces[D] → 找到所有写入 D 的规则
2. 规则中的条件引用了哪些设备（upstream_devices）
3. 对每个 upstream 设备:
   - 如果它有写入规则 → 继续递归追溯
   - 如果它没有写入规则 → 标记为终端 (no_write_rule)
   - 如果是 X 设备 → 终止 (physical_input) ✓
   - 如果是 Y 设备 → 终止 (physical_output) ✓
   - 如果是 SM/SD → 终止 (system_special_relay)
   - 如果是 K/H → 跳过（常量）
4. RST（清除条件）只展示一层，不深入递归
```

**性能优化**：
- 实例级缓存：同一设备只计算一次
- K/H 常量过滤：不进入递归
- `_seen` 去重：防止循环展开
- 安全阀：输出超过 15,000 字符自动截断

### 4.4 输出文件

```
plc_knowledge_out_XX/
├── summary.json              # 全局统计
├── programs.json             # 程序元信息
├── comments.json             # 设备注释
├── rules.jsonl               # 规则（含 program_no 字段）
├── edges.jsonl               # 因果边
├── devices.jsonl             # 设备
├── device_traces.jsonl       # 设备的直接写入规则（扁平格式）
├── sections.jsonl            # 程序段
└── alarm_trace.jsonl         # 空（回溯由 Agent 运行时按需计算）
```

---

## 五、Agent 智能问答系统

### 5.1 架构

- **框架**：LangGraph ReAct Agent
- **LLM**：GPT-4o（通过 Corning AI Platform Gateway）
- **记忆**：SQLite 持久化（多轮对话上下文）
- **多产线**：通过 `PROGRAM_REGISTRY` 注册，UI Dropdown 切换

### 5.2 工具列表（7个）

| Tool | 用途 | 适用 KB 类型 |
|------|------|-------------|
| `trace_alarm` | 报警反向追溯（跨程序，到 X/Y） | global, alarm |
| `list_alarms` | 列出报警，按段落过滤 | global, alarm |
| `trace_control_logic` | 任意设备因果链（backward/forward） | global, control |
| `query_device` | 设备元信息+注释+关联 | all |
| `find_related_rules` | 查设备的所有相关规则 | all |
| `query_section` | 程序段逻辑概览 | all |
| `get_system_overview` | 系统全局统计 | all |

### 5.3 System Prompt 策略

根据知识库类型动态生成 prompt：
- `"global"` 类型：跨程序回溯 + 设备注释 + 报警分析 + 控制逻辑
- `"alarm"` 类型：专注报警诊断
- `"control"` 类型：专注控制逻辑分析

### 5.4 多产线支持

```python
# plc_agent/config.py
PROGRAM_REGISTRY = {
    "WH201_CG1": {
        "name": "XX产线名 全局 (XX个程序合并)",
        "path": PROJECT_ROOT / "plc_knowledge_out_WH201_CG1",
        "type": "global",
    },
    # 未来新产线在此添加
}
```

---

## 六、使用指南

### 6.1 环境准备

```bash
# 安装依赖
pip install -r requirements.txt

# 配置 .env 文件
cp .env.example .env
# 编辑 .env，填入以下值：
#   OPENAI_API_BASE  你的 OpenAI 兼容 API 网关地址
#   OPENAI_API_KEY   你的 API Key
#   MODEL_NAME       使用的模型（如 gpt5.5）
```

> **数据准备说明**：本仓库**不包含** PLC 源程序文件与生成的知识库（属专有数据，已在 `.gitignore` 中排除）。
> 克隆后请先自行准备数据：
> 1. 将 PLC 源文件（`000.csv ~ 960.csv` + `COMMENT.csv`）放入 `plc_extract/plc_file/`
> 2. 运行 6.2 的批量提取命令生成知识库到 `plc_knowledge_out_<LINE>/`
> 3. 在 `plc_agent/config.py` 的 `PROGRAM_REGISTRY` 中注册该知识库路径
>
> 完成上述步骤后才能正常启动系统（6.3）。

### 6.2 生成知识库（离线，仅需运行一次）

```bash
python plc_extract/batch_extract.py \
  --input plc_extract/plc_file \
  --output plc_knowledge_out_WH201_CG1 \
  --line WH201_CG1
```

### 6.3 启动系统

```bash
python run.py ui           # Gradio 网页界面（推荐）
python run.py cli          # 终端交互模式
python run.py api          # FastAPI REST API
python run.py test         # 系统自检
python run.py clear-cache  # 清除缓存和对话历史
```

### 6.4 典型问法

**报警诊断**：
- "F701报警是什么原因？"
- "F702是怎么触发的？帮我追溯到物理层"
- "F703安全链报警的根本原因是什么？"
- "列出所有SERVO相关的报警"

**控制逻辑**：
- "CYCLE START的启动条件是什么？"
- "M4521被什么控制？"
- "T0是怎么产生的？"

**设备查询**：
- "X100D是什么设备？"
- "M7在哪些规则中被使用？"
- "系统概览"

### 6.5 API 接口

```bash
# 健康检查
GET http://localhost:8000/health

# 聊天
POST http://localhost:8000/chat
{
  "message": "F702报警是什么原因？",
  "program_key": "WH201_CG1",
  "thread_id": "session-1"
}

# 列出产线
GET http://localhost:8000/programs

# 设备查询
GET http://localhost:8000/devices/M4321?program_key=WH201_CG1
```

---

## 七、新增产线操作流程

当需要接入新产线时，只需 3 步：

### Step 1：准备文件

将新产线的所有 CSV 文件（000.csv ~ 960.csv + COMMENT.csv）放入一个目录：
```
plc_extract/plc_file_XX/
├── 000.csv
├── 001.csv
├── ...
├── 960.csv
└── COMMENT.csv
```

### Step 2：运行批量提取

```bash
python plc_extract/batch_extract.py \
  --input plc_extract/plc_file_XX \
  --output plc_knowledge_out_XX \
  --line XX
```

### Step 3：注册到 config

在 `plc_agent/config.py` 的 `PROGRAM_REGISTRY` 中添加：

```python
"XX": {
    "name": "XX 全局 (XX个程序合并)",
    "path": PROJECT_ROOT / "plc_knowledge_out_XX",
    "type": "global",
},
```

重启系统后，UI Dropdown 自动出现新选项。

---

## 八、性能优化记录

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| 回溯产生 959MB 输出 | 条件中所有设备都递归展开（指数爆炸） | 只沿"有写入规则"的设备递归，无写入的直接终止 |
| K0/K1 有 952 条写入规则 | 常量值被误认为可追溯设备 | 过滤 K/H 开头设备，不进入递归 |
| RST 分支展开过深 | 清除条件引入整个控制逻辑链 | RST 只列出条件设备名+注释，不深入递归 |
| 同一设备重复计算 | 缓存的 dict 对象序列化时重复展开 | 改为直接生成文本（`_format_live_trace`），不构建嵌套 dict |
| tool cache 存有旧的巨大结果 | SQLite 缓存了之前的 959MB 输出 | 加安全阀 (15000 chars) + 提供 `clear-cache` 命令 |

---

## 九、项目文件结构

> 注：以下标注 **[专有数据·不在仓库]** 的目录含康宁真实 PLC 数据，已被 `.gitignore` 排除，需按 6.1 说明自行准备。

```
PLC/
├── run.py                         # 统一入口
├── requirements.txt               # Python 依赖
├── .env                           # API Key 配置（不上传）
├── .env.example                   # 配置模板（占位符）
├── README.md                      # 本文档
│
├── plc_extract/                   # 离线提取模块
│   ├── 71_rule_extract.py         # 单文件提取（legacy）
│   ├── batch_extract.py           # 批量提取（当前使用）
│   ├── query_alarm.py             # 命令行查询工具（legacy）
│   └── plc_file/                  # PLC 源文件（CSV）[专有数据·不在仓库]
│       ├── 000.csv ~ 960.csv      # 29个程序文件
│       └── COMMENT.csv            # 设备注释表
│
├── plc_knowledge_out_WH201_CG1/   # 全局知识库输出 [专有数据·不在仓库]
│   ├── summary.json
│   ├── programs.json
│   ├── comments.json
│   ├── rules.jsonl
│   ├── edges.jsonl
│   ├── devices.jsonl
│   ├── device_traces.jsonl
│   ├── sections.jsonl
│   └── alarm_trace.jsonl
│
├── plc_agent/                     # Agent 模块
│   ├── config.py                  # 配置（LLM、产线注册）
│   ├── knowledge/
│   │   ├── loader.py              # 知识库加载 + 动态回溯
│   │   └── cache.py              # Tool/Query 缓存
│   ├── tools/
│   │   └── plc_tools.py          # 7 个 LangChain Tools
│   ├── agent/
│   │   ├── graph.py              # LangGraph Agent 定义
│   │   └── prompts.py            # 动态 System Prompt
│   ├── api/
│   │   └── server.py             # FastAPI 后端
│   └── ui/
│       └── app.py                # Gradio 前端
│
├── data/                          # 运行时数据（对话历史、缓存）
└── venv/                          # Python 虚拟环境
```

---

## 十、后续规划

| 优先级 | 任务 | 状态 |
|--------|------|------|
| P0 | 跨程序全局回溯 + COMMENT 整合 | ✅ 已完成 |
| P1 | Agent 多产线支持 + UI Dropdown | ✅ 已完成 |
| P2 | 梯形图渲染（condition_tree → SVG） | 待开发 |
| P3 | 更多产线接入（WH202, WH203...） | 待数据 |
| P4 | 回溯终端类型细化（区分 Motion CPU / FIMC / 硬接线） | 待设计 |
| P5 | 实时数据对接（在线监控 + 诊断建议） | 待规划 |
