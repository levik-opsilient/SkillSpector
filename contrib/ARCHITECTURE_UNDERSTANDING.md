# SkillSpector 架构理解 — 为什么并发是「长出来的」而不是「塞进去的」

> 作者：Claude (Anthropic)
> 日期：2026-06-18
> 读者：本项目的新开发者、上游 NVIDIA 维护者、Contrib 贡献者
> 目的：理解 SkillSpector 的设计哲学 —— 为什么一个「单 skill 扫描器」的架构天然支持水平并发

---

## 目录

1. [一句话理解](#1-一句话理解)
2. [核心设计模式：函数式分解](#2-核心设计模式函数式分解)
3. [无状态证明：逐层验证](#3-无状态证明逐层验证)
4. [Graph 内部：20 个分析器如何并行](#4-graph-内部20-个分析器如何并行)
5. [LLMAnalyzerBase：Token 感知的并发模型](#5-llmanalyzerbaseToken-感知的并发模型)
6. [Provider 系统：可插拔的 LLM 后端](#6-provider-系统可插拔的-llm-后端)
7. [并行金字塔：从单 skill 到多 skill](#7-并行金字塔从单-skill-到多-skill)
8. [Contrib 如何「长」在架构上](#8-contrib-如何长在架构上)
9. [设计边界：不改什么、为什么](#9-设计边界不改什么为什么)

---

## 1. 一句话理解

SkillSpector 把「扫描一个 skill」做成了一个**无状态的纯函数**：

```python
state → graph.invoke(state) → result
```

如果你接受这个前提，那么「扫描 N 个 skill」就是一个 `map`：

```python
results = map(graph.invoke, states)
```

并行的 `map`：

```python
with ThreadPoolExecutor(max_workers=4) as pool:
    results = pool.map(graph.invoke, states)
```

整个 contrib 的设计，就是给这个 `map` 加上语言检测、API Pool 调度和对比标记。**不改原函数，只改调用方式。**

---

## 2. 核心设计模式：函数式分解

### 2.1 Graph 是纯函数

```python
# graph.py — 模块级单例
graph = create_graph()          # 编译一次，复用所有调用

# 每次调用是独立计算
def scan_one(input_path):
    state = {"input_path": input_path, ...}    # 输入完全自包含
    result = graph.invoke(state)               # 纯计算，无副作用
    cleanup(result["temp_dir_for_cleanup"])   # 副作用外置
    return result
```

**为什么是纯函数**：

- 同一个 state 输入 → 永远得到同一个 result 输出
- `graph.invoke()` 不读写全局变量
- 不依赖调用顺序
- 不修改共享状态
- 唯一的副作用（创建临时目录）被外置给 caller 处理

### 2.2 这是刻意为之

CLI 源码第 18 行的注释揭示了设计意图：

> *"thin wrapper over the LangGraph workflow. No business logic; workflow lives in the graph."*

翻译：CLI 只是薄封装，业务逻辑全在 graph 里。这意味着任何入口（CLI、API、脚本、batch runner）都可以通过 `graph.invoke(state)` 获得完全相同的行为。

### 2.3 与 MapReduce 的类比

```
MapReduce                          SkillSpector
─────────                          ────────────
map(f, docs)                       map(graph.invoke, skills)
  └─ f(doc) 纯函数，无共享状态       └─ invoke(state) 纯函数，无共享状态
reduce(results)                    aggregate(results)
```

区别只在于 SkillSpector 的单个计算单元（`graph.invoke`）比 MapReduce 的 `map` 函数重得多——内部有 20 个并行分析器 + LLM 调用 + AST 解析。但**组合方式完全一样**。

---

## 3. 无状态证明：逐层验证

### 3.1 State 层

```python
# state.py
class SkillspectorState(TypedDict, total=False):
    input_path: str | None
    skill_path: str | None
    temp_dir_for_cleanup: str | None
    components: list[str]
    file_cache: dict[str, str]
    findings: Annotated[list[Finding], operator.add]
    filtered_findings: list[Finding]
    ...
```

**关键观察**：
- `TypedDict(totall=False)` — 所有字段可选，没有构造约束
- 没有 `__init__` — 没有初始化副作用
- `findings` 用 `operator.add` reducer — 但这是 LangGraph 内部的累积机制，不跨 `invoke()` 调用共享
- 每次 `invoke()` 创建一个新的 dict，不引用前一次调用的数据

### 3.2 Provider 层

```python
# providers/chat_models.py
def create_openai_compatible_chat_model(*, model, credentials, max_tokens, timeout):
    api_key, base_url = credentials
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=SecretStr(api_key),
        max_completion_tokens=max_tokens,
        timeout=timeout,
    )
```

**关键观察**：
- 每次调用创建新的 `ChatOpenAI` 实例
- 没有连接池缓存
- 没有全局单例
- 凭证来自参数传入，不从全局状态读取

### 3.3 Analyzer 层

```python
# llm_analyzer_base.py
class LLMAnalyzerBase:
    def __init__(self, base_prompt, model):
        self.base_prompt = base_prompt
        self.model = model
        self._input_budget = get_max_input_tokens(model)
        self._llm = get_chat_model(model=model)              # 新实例
        self._structured_llm = self._llm.with_structured_output(...)  # 新实例
```

**关键观察**：
- 构造函数参数只有 prompt 和 model —— 没有外部状态
- `_llm` 和 `_structured_llm` 是实例变量 —— 每个 analyzer 独立的 LLM 连接
- 没有跨 analyzer 的共享缓存

### 3.4 Graph 层

```python
# graph.py
graph = create_graph()   # 模块加载时编译一次 —— 这是唯一共享的东西

# create_graph() 内部
workflow = StateGraph(SkillspectorState)
workflow.add_node("resolve_input", resolve_input)
workflow.add_node("build_context", build_context)
for analyzer_id in ANALYZER_NODE_IDS:
    workflow.add_node(analyzer_id, ANALYZER_NODES[analyzer_id])
...
return workflow.compile()
```

**关键观察**：
- `create_graph()` 只定义**图的拓扑结构**：节点有哪些、边怎么连
- 编译后的 `graph` 是一个**无状态的执行计划**，不持有任何数据
- 类比：graph = 流水线蓝图，state = 放进流水线的原材料
- 多次 `invoke()` 复用同一个 graph 对象，但 state 是新的

### 3.5 检验：并发安全吗？

```
Thread-1: graph.invoke(state_1)  ──► 读写 state_1，不碰 state_2
Thread-2: graph.invoke(state_2)  ──► 读写 state_2，不碰 state_1
Thread-3: graph.invoke(state_3)  ──► 读写 state_3，不碰 state_1/state_2
```

**安全**。每条线程操作的是完全独立的 dict 和对象引用。唯一共享的 `graph` 对象是只读的编译结果（LangGraph 的 `CompiledGraph` 内部用 asyncio 事件循环，不在多线程间共享可变状态）。

---

## 4. Graph 内部：20 个分析器如何并行

### 4.1 拓扑

```
START
  │
resolve_input       ← 输入归一化：git/zip/url/目录 → 本地临时目录
  │
build_context       ← 遍历文件、读缓存、解析 manifest、注入 model_config
  │
  ├─ static_patterns_*.py  (× 8)  ──┐
  ├─ static_ast.py                  │
  ├─ static_yara.py                 │
  ├─ behavioral_taint_tracking.py   ├─ 20 个节点 fan-out
  ├─ mcp_least_privilege.py         │  LangGraph 自动并行
  ├─ mcp_tool_poisoning.py          │
  ├─ semantic_security_discovery.py │
  ├─ semantic_developer_intent.py   │
  ├─ semantic_quality_policy.py     │
  └─ ...                            ──┘
  │
meta_analyzer       ← fan-in：LLM 二次验证所有 findings
  │
report              ← 风险评分 + 格式化输出
  │
 END
```

### 4.2 为什么 fan-out 是自然的并行

LangGraph 的语义：

```python
workflow.add_edge("build_context", "analyzer_1")
workflow.add_edge("build_context", "analyzer_2")
...
```

当一个节点有多条出边时，目标节点**并行运行**。这是 LangGraph 的默认行为，不需要显式配置线程池。

### 4.3 哪些分析器调 LLM

| 分析器 | 类型 | 是否调 LLM | 并行方式 |
|--------|------|-----------|---------|
| SSD / SDI / SQP | 语义发现 | ✅ | `asyncio.run(analyzer.arun_batches())` |
| TP4 | 工具投毒 | ✅ | 单次 `chat_completion()` |
| meta_analyzer | 验证/过滤 | ✅ | `asyncio.run(analyzer.arun_batches())` |
| 其余 15 个 | 静态/行为 | ❌ | 纯 CPU |

### 4.4 静态与 LLM 的分工哲学

```
静态分析（15 个）          LLM 分析（5 个）
───────────────           ──────────────
解决「已知模式」          解决「未知模式」
快（毫秒级）              慢（秒级）
确定性                    概率性
高精度、低召回            低精度、高召回
不需要 API Key            需要 API Key

两者互补，不是替代。
```

---

## 5. LLMAnalyzerBase：Token 感知的并发模型

### 5.1 三层职责

```
LLMAnalyzerBase
├── Token 预算
│   ├── get_max_input_tokens(model)     → 模型上下文上限
│   ├── estimate_tokens(text)           → 4 char/token 估算
│   └── chunk_file_by_lines(content)    → 超限文件按行拆分 + 50行重叠
│
├── 结构化输出
│   ├── response_schema: Pydantic Model  → 子类可覆盖
│   └── with_structured_output(schema)  → LangChain 自动 JSON → Pydantic
│
└── 并发执行
    ├── run_batches()                   → 同步顺序
    └── arun_batches(sem=10)            → 异步并发 + Semaphore 限流
```

### 5.2 Batch 拆分算法

```
输入：一个 skill 目录的文件列表

对每个文件：
  content_tokens = estimate_tokens(file_content)
  budget = input_budget - base_prompt_overhead - findings_overhead

  if content_tokens <= budget:
      → 一个文件 = 一个 Batch（完整内容发给 LLM）

  else:
      → 按行拆分，每 chunk ≤ budget
      → 相邻 chunk 重叠 50 行（防止边界漏报）
      → 每个 chunk = 一个 Batch

输出：Batch 列表
```

### 5.3 并发控制

```python
# llm_analyzer_base.py:387
sem = asyncio.Semaphore(max_concurrency)  # 默认 10

async def _process(batch):
    async with sem:                        # 同时最多 10 个 API 请求
        response = await self._structured_llm.ainvoke(prompt)
        return self.parse_response(response, batch)

return list(await asyncio.gather(*[_process(b) for b in batches]))
```

**设计思路**：Semaphore 上限写死 10，够覆盖单 skill 的全部 batch。不做复杂的限流算法，因为单 skill 场景下文件数量有限，不需要。

---

## 6. Provider 系统：可插拔的 LLM 后端

### 6.1 三层抽象

```
Protocol 层（base.py）              实现层（各 provider 子包）
─────────────────────              ──────────────────────────
ModelMetadataProvider              openai/
  ├─ get_context_length(model)       ├─ provider.py
  ├─ get_max_output_tokens(model)    └─ model_registry.yaml
  └─ resolve_model(slot)           anthropic/
                                      ├─ provider.py
CredentialsProvider                   └─ model_registry.yaml
  └─ resolve_credentials()
                                    nv_build/
ChatModelProvider                     ├─ provider.py
  └─ create_chat_model(...)           └─ model_registry.yaml
```

Protocol 不是 ABC，是 Python 的结构子类型——任何满足方法签名的对象都能当 Provider 用。添加新 provider 不需要改 base.py。

### 6.2 选择链

```
SKILLSPECTOR_PROVIDER env var
  │
  ├─ "openai"     → OpenAIProvider      → OPENAI_API_KEY
  ├─ "anthropic"  → AnthropicProvider   → ANTHROPIC_API_KEY
  ├─ "nv_build"   → NvBuildProvider     → NVIDIA_INFERENCE_KEY
  └─ unset        → NvInferenceProvider (fallback: NvBuildProvider)
  │
  └─ 凭证回退链：active provider → OpenAI fallback → 报错
```

### 6.3 模型选择

```
SKILLSPECTOR_MODEL env var（最高优先）
  │
  └─ provider 的 SLOT_DEFAULTS（按分析器 slot 细分）
       │  slot="meta_analyzer" → 更大的模型
       │  slot="default"       → 标准模型
       │
       └─ provider 的 DEFAULT_MODEL（兜底）
```

---

## 7. 并行金字塔：从单 skill 到多 skill

```
第 3 层：多 skill 并行     ← Contrib 新增（ThreadPoolExecutor(max_workers=N)）
  │                         每个 worker 跑一个完整的 graph.invoke()
  │
  └─ 第 2 层：多 chunk 并行 ← LLMAnalyzerBase 自带（arun_batches + Semaphore(10)）
  │                         每个 LLM 分析器内部并发处理多个文件 chunk
  │
      └─ 第 1 层：多分析器并行 ← LangGraph 自带（20 个 node fan-out）
                              静态 + LLM 分析器同时运行
```

**关键**：每一层不知道上一层和下一层的存在。

- Graph 不知道自己在被多个 worker 并发调用
- Worker 不知道 graph 内部有 20 个并行分析器
- LLMAnalyzerBase 不知道调用它的是哪个 worker

这是**层级解耦**——每一层只关心自己的职责。

---

## 8. Contrib 如何「长」在架构上

### 8.1 三个新增组件

```
contrib/multilingual/
│
├── detection.py         语言检测：Unicode script ratio，零外部依赖
├── annotation.py        发现标注：rule_id → language_compatible 分类
│
├── gap_fill.py          GapFillAnalyzer(LLMAnalyzerBase)
│   └── 弥补 8 条非英语失效的静态规则（P5/P6-P8/MP1-MP3/RA1-RA2）
│   └── 复用：token 预算、结构化输出、行号模板、Semaphore 并发
│
├── api_pool.py          ApiKeyPool（多 key 调度）
│   └── idle → in_use → rate_limited（退避 30s×2ⁿ）→ 恢复
│   └── 对上层透明，worker 不知道 key 在切换
│
├── batch_scan.py        批量入口（CLI + 并行调度）
├── runner.py            单 skill 编排（graph.invoke + gap_fill + 标注）
└── reports.py           三种输出格式（terminal / json / markdown）
```

### 8.2 不改原项目任何代码

```
src/skillspector/
  graph.py                         ← 不动
  state.py                         ← 不动
  cli.py                           ← 不动
  llm_analyzer_base.py             ← 不动（只作为父类被导入）
  llm_utils.py                     ← 不动（只作为工具函数被调用）
  providers/                       ← 不动
  nodes/analyzers/                 ← 不动
  nodes/meta_analyzer.py           ← 不动
  nodes/report.py                  ← 不动
```

### 8.3 四个设计原则

**① 子类化，不重写**。GapFill 需要 LLM 能力 → 继承 `LLMAnalyzerBase`，不是自己写 token 预算。需要并发 → 用 `arun_batches()`，不是自己写 asyncio。

**② 包一层，不挖洞**。API Pool 需要多 key 调度 → 包一层 `PooledChatModel`，不是改 `ChatOpenAI` 的构造逻辑。Worker 需要并行 → 用 `ThreadPoolExecutor`，不是改 graph 的执行模型。

**③ 加标记，不改输出**。多语言增强 → 在原 Findings 上加 `language_compatible` 字段，不改变 Findings 的结构。对比 → 加 `scan_mode` / `enhancements` 元数据字段，不改变 `risk_assessment` 的算法。

**④ 显式对比，不隐藏差异**。上游开发者跑两条命令就能 diff：`skillspector scan` vs `batch_scan`。报告里有 `scan_mode` 标签，知道自己看的是哪个版本。

---

## 9. 设计边界：不改什么、为什么

| 界限 | 为什么 |
|------|--------|
| **不改 graph.py** | Graph 的拓扑是上游的核心资产。在外部加一层 map 比在内部加节点更安全 |
| **不改 state.py** | 现有字段覆盖了 contrib 的全部需求。加字段 = 上游合并冲突 |
| **不改 providers/** | 上游的 provider 系统是完整的。API Pool 在更上层解决问题 |
| **不改 LLMAnalyzerBase** | 继承就够了。基类的修改会影响所有子类 |
| **不改 analyzer 注册表** | GapFill 不以 graph node 形式存在，不破坏 20-analyzer 的拓扑 |
| **自建 API Pool 而不是自建 provider** | Provider = LLM 后端抽象（已有）。API Pool = 多实例调度（缺失）。互补，不重叠 |

### 什么时候该改上游

如果有一天，批量扫描、多语言支持、API Pool 被证明是广泛需求，那么：

1. API Pool → 提到 `src/skillspector/providers/pool.py`（上游化）
2. 语言检测 → 提到 `build_context` 节点（上游化）
3. GapFill → 注册为第 21 个 analyzer node（上游化）
4. `scan-batch` → 合并进 CLI 的 `scan` 命令（上游化）

但在那一天之前，contrib 保持独立。**先证明价值，再讨论合并。**

---

## 附录 A：关键文件索引

| 文件 | 职责 |
|------|------|
| `src/skillspector/graph.py` | Graph 拓扑定义（7 节点） |
| `src/skillspector/state.py` | State schema（TypedDict） |
| `src/skillspector/llm_analyzer_base.py` | LLM 分析器基类（token 预算 + 并发） |
| `src/skillspector/providers/__init__.py` | Provider 工厂 + 凭证回退链 |
| `src/skillspector/providers/base.py` | Provider 协议定义 |
| `src/skillspector/providers/chat_models.py` | ChatOpenAI 公共构造器 |
| `src/skillspector/llm_utils.py` | LLM 工具函数（chat_completion 等） |
| `src/skillspector/cli.py` | CLI 入口（scan 命令） |
| `src/skillspector/nodes/build_context.py` | 上下文构建（文件发现、缓存、manifest） |
| `src/skillspector/nodes/meta_analyzer.py` | Meta-analyzer（LLM 验证） |
| `src/skillspector/nodes/analyzers/__init__.py` | Analyzer 注册表 |
| `docs/DEVELOPMENT.md` | 开发指南 |
| `docs/LLM_ANALYZER_BASE_GUIDE.md` | LLMAnalyzerBase 使用指南 |

## 附录 B：术语表

| 术语 | 含义 |
|------|------|
| Skill | AI agent 的技能包（目录或 zip） |
| Finding | 一个安全发现（rule_id + severity + line + ...） |
| Batch | 一个 LLM 调用单元（一个文件或一个 chunk） |
| State | 一次 graph 调用的完整输入/输出 |
| Provider | LLM 后端抽象（OpenAI / Anthropic / NVIDIA） |
| Meta-analyzer | LLM 二次验证节点 |
| Fan-out | 一个节点 → 多个节点并行 |
| Fan-in | 多个节点 → 一个节点汇聚 |
| Chunk | 超大文件被按行拆分的片段 |
| Semaphore | asyncio 并发闸门 |
| API Pool | 多 API key 资源调度器 |
