# Contrib 多语言批量扫描 — 与原项目对齐分析报告

> 日期：2026-06-18
> 范围：`contrib/multilingual/` ↔ `src/skillspector/` 架构对比
> 目标：消除轮子重复、保持上游可比对、推动 Worker 并行化

---

## 1. 原项目架构速览

### 1.1 Graph 是唯一的产品入口

```text
CLI (cli.py)          ← 薄封装，「No business logic; workflow lives in the graph」
  │
  ▼
graph.invoke(state)   ← 模块级单例 (graph.py:55)
  │
  ├─ resolve_input    ← 输入解析（git / zip / url / 目录），创建临时目录
  ├─ build_context    ← 文件遍历、缓存构建、manifest 解析、model_config 注入
  ├─ [20 analyzers]   ← LangGraph 内置并行（branches）
  ├─ meta_analyzer    ← LLM 二次验证 + 过滤 + 丰富（explanation / remediation）
  └─ report           ← 风险评分 + 格式化输出（terminal / json / markdown / sarif）
```

**关键点**：

- `build_context` 是唯一的数据入口——所有分析器从 state 读数据，不自己做 IO。
- `findings` 使用 `Annotated[list[Finding], operator.add]` 自动合并 20 个分析器的输出。
- `meta_analyzer` 是质量守门人——跳过它的发现不会被 report 计入。
- `use_llm: bool` 是全局开关——LLM 节点自己检查，`False` 时直接返回空 findings，管道照跑。

### 1.2 静态 + LLM 的分工

| 层 | 纯静态（15 个） | LLM 驱动（5 个） | 开关机制 |
|---|---|---|---|
| 分析器 | AST、YARA、Pattern、Structure | Semantic × 3 + TP4 | `state["use_llm"]` |
| 验证 | — | meta_analyzer | 同上 |
| 汇总 | report（纯计算） | — | — |

`--no-llm` 时，LLM 节点静默退出，静态分析器继续工作。**复用 graph = 复用整个管线，不需要自己写任何分支逻辑。**

### 1.3 Provider 系统

```
Protocol 层 (base.py)
├── ModelMetadataProvider   →  token 预算 / 模型默认值
├── CredentialsProvider     →  (api_key, base_url)
└── ChatModelProvider       →  create_chat_model() → LangChain BaseChatModel

选择链：SKILLSPECTOR_PROVIDER env → 工厂函数 → 凭证回退链（OpenAI escape hatch）
模型链：SKILLSPECTOR_MODEL env > slot 默认 > provider 默认
```

所有 LLM 分析器通过 `llm_utils.chat_completion()` 或 `LLMAnalyzerBase` 间接使用，不直接接触 provider。

### 1.4 LLMAnalyzerBase — 核心基类

```text
LLMAnalyzerBase(base_prompt, model)
├── token 预算    ← model_info.get_max_input_tokens()，75% / 25% 分割
├── 分批          ← get_batches() — 4 char/token 估算 + 50 行重叠 chunking
├── 结构化输出    ← with_structured_output() + Pydantic response_schema
├── Prompt 模板   ← BASE_ANALYSIS_PROMPT（L<N>: 行号前缀 + 精准优先指令）
├── run_batches   ← 同步顺序
└── arun_batches  ← 异步并发（Semaphore 限流）
```

所有 semantic 分析器 + meta_analyzer 都基于它。

---

## 2. Contrib 现状 vs 原项目 — 逐项对比

### 2.1 正确复用的部分 ✅

| 组件 | 原项目 | Contrib | 方式 |
|---|---|---|---|
| Provider 系统 | `providers/` | 不直接接触 | `chat_completion()` 间接调用 |
| 模型选择 | `MODEL_CONFIG["default"]` | 同样 import | `from skillspector.constants import MODEL_CONFIG` |
| Graph 管线 | `graph.invoke(state)` | `runner.run_one()` 内部调用 | 完全复用 5 个节点 |
| Finding 模型 | `models.Finding` | 同样 import | gap_fill 输出 Finding 对象 |
| 语义分析器 | SSD / SDI / SQP（3 个 LLM 分析器） | 通过 graph 自动调用 | 零重复代码 |
| 静态分析器 | AST / YARA / Pattern 等（15 个） | 通过 graph 自动调用 | 零重复代码 |
| Meta analyzer | 二次验证 + 过滤 | 通过 graph 自动调用 | 零重复代码 |
| Report / 评分 | `report` 节点 | graph 内部执行 | 零重复代码 |
| 输入解析 | `resolve_input` 节点 | graph 内部执行 | 零重复代码 |

### 2.2 不一致的部分 ⚠️

#### 问题 1：Gap-fill 手动 JSON 解析 → 应该用 `with_structured_output()`

```text
原项目主流模式                    Contrib gap_fill
─────────────────────────────────────────────────
LLMAnalyzerBase 子类             裸函数 run_gap_fill()
response_schema = Pydantic       手动 json.loads()
with_structured_output()          手动 strip ``` 前缀
LangChain 自动验证 schema         无 schema 验证
```

**但**：原项目的 TP4 也用手动 JSON 解析（`mcp_tool_poisoning.py`）。所以原项目自身就是分裂的——TP4 是老路，`LLMAnalyzerBase` 是新路。Gap-fill 走了老路。

**结论**：应该走新路。`LLMAnalyzerBase` 是项目明确的未来方向（TP4 是遗留代码，有 TODO 标记）。

#### 问题 2：Gap-fill token 硬截断 → 应该用 `get_batches()`

```text
原项目                                Contrib gap_fill
───────────────────────────────────────────────────────────
estimate_tokens(text)                  content[:3000]   ← 硬截断
get_max_input_tokens(model)            无预算计算
input_budget - prompt_overhead         无预算检查
chunk_file_by_lines(content, max_tokens, overlap=50)    无 chunking
1024 token 保底                         无保底
```

**风险**：大型 skill 目录（10+ 文件）合并后轻松超过 3000 字符，但更重要的是可能超出模型上下文限制。当前硬截断在 3000 字符处一刀切，可能在句子中间切断，LLM 理解出偏差。

#### 问题 3：Gap-fill 运行在 graph 外部 → meta_analyzer 看不见

```text
原项目管线                          Contrib 实际流程
─────────────────────────────────────────────────────────
build_context                      graph.invoke()
  │                                  │
[20 analyzers]                       ├─ build_context
  │                                  ├─ [20 analyzers]
meta_analyzer ← 看见所有 findings     ├─ meta_analyzer  ← 看不见 gap-fill 发现
  │                                  ├─ report          ← 评分不含 gap-fill
report ← 评分包含所有发现             └─ 返回 result
                                       │
                                     runner.run_one() 返回后
                                       │
                                    gap_fill.run_gap_fill() ← 后追加
```

**影响**：

- Gap-fill 发现不会被 meta_analyzer 二次验证（可能假阳性偏高）
- Gap-fill 发现不影响 risk_score（报告评分偏低）
- NVIDIA 开发者可能困惑：为什么某些漏洞没出现在风险评分中

#### 问题 4：Batch 串行 → Graph 内部并行，外层浪费

```text
当前 batch_scan.py 主循环（简化）：

for skill_dir in skill_dirs:          ← 串行，一个一个来
    entry, error = run_one(skill_dir)  ← 每次 graph.invoke()
    results.append(entry)              ← graph 内部 20 个分析器并行，但 skills 之间串行

总耗时 = Σ(每个 skill 的 graph 耗时)
        = N × graph_duration           ← 线性增长
```

应该：

```text
async for skill_dir in skill_dirs:    ← 外层也并行
    async with semaphore(max_workers):
        entry = await run_one(skill_dir)

总耗时 ≈ N / max_workers × graph_duration   ← 可控并发
```

#### 问题 5：结果无可比性标记

当前 batch 报告和原项目 `skillspector scan` 的报告格式不同，没有标记说明差异来源。上游开发者无法快速对比「标准版 vs 多语言增强版」。

---

## 3. 改进方案

### 3.1 核心原则

1. **零侵入**：不修改 `src/skillspector/` 中任何文件
2. **子类化复用**：gap_fill 改为 `LLMAnalyzerBase` 子类
3. **Graph 完整复用**：不绕过 graph，不改 graph 内部逻辑
4. **并行外层调度**：`asyncio` + semaphore 控制并发度
5. **显式对比标记**：每条结果带 `scan_mode`，报告头部打印模式标签

### 3.2 改动清单

```
contrib/multilingual/
├── gap_fill.py      ★ 重写：GapFillAnalyzer(LLMAnalyzerBase) 子类
├── batch_scan.py    ★ 重写：asyncio 并行调度 + CLI 对齐
├── reports.py       ▲ 修改：头部加 scan_mode 标记
├── runner.py        ▲ 修改：entry 加 scan_mode / gap_fill_findings 字段
├── detection.py     ✓ 不改
├── annotation.py    ✓ 不改
└── __init__.py      ▲ 修改：导出新符号
```

★ = 重写　▲ = 修改　✓ = 不动

### 3.3 gap_fill.py — 从裸函数到 LLMAnalyzerBase 子类

**改造前**（现状）：

```python
# 模块级字符串 prompt
GAP_FILL_PROMPT = """...{language}...{file_contents}..."""

# 硬截断
content[:3000]

# 手动解析
json.loads(text.strip("```").strip())

# 裸调用
chat_completion(prompt, model=model)
```

**改造后**（目标）：

```python
from pydantic import BaseModel, Field
from skillspector.llm_analyzer_base import LLMAnalyzerBase, BASE_ANALYSIS_PROMPT

class GapFillFinding(BaseModel):
    rule_id: str
    message: str
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    confidence: float
    explanation: str
    remediation: str

class GapFillResult(BaseModel):
    findings: list[GapFillFinding]

class GapFillAnalyzer(LLMAnalyzerBase):
    response_schema = GapFillResult

    def __init__(self, language: str, model: str | None = None):
        self.language = language
        prompt = GAP_FILL_ANALYZER_PROMPT  # 分析器专用提示词
        super().__init__(base_prompt=prompt, model=model)

    def build_prompt(self, batch, **kwargs):
        # 复用 BASE_ANALYSIS_PROMPT 的 L<N>: 行号模板
        # language 通过 kwargs 注入到 prompt 的 {language} 占位符
        return super().build_prompt(batch, language=self.language, **kwargs)

    def parse_response(self, response, batch):
        # 自动获得 Pydantic 验证 + 类型安全
        return [f.to_finding(batch.file_path) for f in response.findings
                if f.confidence >= 0.7]


def run_gap_fill(file_cache, language, model=None):
    """对外接口保持兼容"""
    analyzer = GapFillAnalyzer(language=language, model=model)
    batches = analyzer.get_batches(
        file_paths=list(file_cache.keys()),
        file_cache=file_cache,
    )
    return analyzer.run_batches(batches, language=language)
```

**获得的能力**（全部继承自 `LLMAnalyzerBase`）：

- `get_batches()` — token 感知的智能分批（4 char/token 估算 + 50 行重叠 + 1024 token 保底）
- `with_structured_output()` — LangChain 原生结构化输出，Pydantic 自动验证
- `BASE_ANALYSIS_PROMPT` — 统一的行号前缀（`L<N>:`）+ 精准优先指令
- `arun_batches()` — 异步并发（Semaphore 限流），为外层并行打下基础
- 错误处理 — `ValueError` 传播、其他异常静默降级（与原项目分析器一致）

### 3.4 batch_scan.py — asyncio 并行调度

```python
import asyncio
from concurrent.futures import ProcessPoolExecutor

async def scan_all(
    skill_dirs: list[Path],
    root: Path,
    use_llm: bool,
    max_workers: int = 4,
) -> list[dict]:
    """并行调度：每个 skill 在独立线程中跑完整 graph."""
    semaphore = asyncio.Semaphore(max_workers)

    async def scan_one(skill_dir: Path) -> dict:
        async with semaphore:
            lang = detect_skill_language(skill_dir)
            loop = asyncio.get_running_loop()
            # graph.invoke() 是同步的，用 to_thread 避免阻塞事件循环
            entry, error = await loop.run_in_executor(
                None, run_one, skill_dir, root,
                use_llm=use_llm, detected_language=lang
            )
            # gap_fill 也在 executor 中运行
            if lang != "en" and use_llm and not error:
                gap_findings = await loop.run_in_executor(
                    None, run_gap_fill, entry["_file_cache"], lang
                )
                entry["issues"].extend(
                    annotate_findings([f.to_dict() for f in gap_findings], lang)
                )
                entry["enhancements"]["gap_fill_applied"] = True
                entry["enhancements"]["gap_fill_findings"] = len(gap_findings)
            return entry

    return await asyncio.gather(*[scan_one(d) for d in skill_dirs])
```

**并发层级**：

```text
外层：asyncio (max_workers 个 skill 并行)
  └─ 中层：graph 内置 (20 个 analyzer 并行)
       └─ 内层：LLMAnalyzerBase.arun_batches (Semaphore(10) 个 batch 并行)

总并发 = max_workers × 20 × 10（理论上限，实际受限于 CPU/API rate limit）
```

### 3.5 对比标记 — 让上游能 diff

每条 entry 增加字段：

```json
{
  "skill": { "...": "..." },
  "scan_mode": "multilingual-enhanced",
  "enhancements": {
    "language_detected": "zh",
    "language_detection_method": "unicode-script-ratio",
    "gap_fill_applied": true,
    "gap_fill_rules_covered": ["P5", "P6", "P7", "P8", "MP1", "MP2", "MP3", "RA1", "RA2"],
    "gap_fill_findings": 2,
    "english_keyword_rules_skipped": ["P1-P4", "E1-E4", "PE1-PE3", "EA1-EA4", "OH1-OH3", "TR1-TR3"]
  },
  "risk_assessment": { "...": "..." },
  "issues": [ "...": "..." ]
}
```

报告头部添加：

```markdown
# SkillSpector Batch Scan Report

**Scan mode**: Multilingual Enhanced (v2.1.0)
**Compare with**: Run `skillspector scan <skill> -f json` for standard mode
**Enhancements applied**:
  - Language detection (Unicode script ratio)
  - Gap-fill LLM pass for 8 non-semantic rules
  - 25 English-keyword rules skipped for non-English skills
```

上游对比命令：

```bash
# 标准模式（原项目，不动任何代码）
skillspector scan ./skills/my-zh-skill/ -f json -o standard.json

# 多语言增强模式（contrib 提供）
python -m contrib.multilingual.batch_scan ./skills/ --lang zh -f json -o enhanced.json

# diff 对比
diff <(jq -S . standard.json) <(jq -S '.skills[0]' enhanced.json)
```

### 3.6 CLI 对齐

原项目 CLI 的 flag 设计：

```text
skillspector scan <input> [-f terminal|json|markdown|sarif] [-o output] [--no-llm] [--verbose]
```

Contrib CLI 复用已有 flag + 增加多语言专属：

```text
python -m contrib.multilingual.batch_scan <input_dir> \
    [-f terminal|json|markdown] \    ← 与原项目相同的 -f 语义
    [-o output] \                    ← 与原项目相同的 -o 语义
    [--no-llm] \                     ← 与原项目相同的 flag
    [-V|--verbose] \                 ← 与原项目相同的 -V 语义
    [--lang auto|en|zh|ja|ko] \     ← contrib 专属
    [--workers 4]                    ← contrib 专属（并行度）
```

---

## 4. 实施路径

### Phase 1：GapFill 子类化（核心改造）

| 步骤 | 文件 | 内容 |
|---|---|---|
| 1.1 | `gap_fill.py` | 定义 `GapFillFinding` / `GapFillResult` Pydantic 模型 |
| 1.2 | `gap_fill.py` | 实现 `GapFillAnalyzer(LLMAnalyzerBase)` 子类 |
| 1.3 | `gap_fill.py` | 保留 `run_gap_fill()` 作为对外兼容接口 |
| 1.4 | 验证 | 用原项目测试 skill 跑一遍，确认输出格式一致 |

### Phase 2：并行调度

| 步骤 | 文件 | 内容 |
|---|---|---|
| 2.1 | `batch_scan.py` | `asyncio` + `run_in_executor` 并行化主循环 |
| 2.2 | `batch_scan.py` | `--workers` CLI flag |
| 2.3 | `batch_scan.py` | 进度输出（`tqdm` 或 Rich progress bar） |

### Phase 3：对比标记 + 报告

| 步骤 | 文件 | 内容 |
|---|---|---|
| 3.1 | `runner.py` | entry 增加 `scan_mode` / `enhancements` 字段 |
| 3.2 | `reports.py` | 所有格式（terminal / json / markdown）头部加模式标记 |
| 3.3 | `reports.py` | Markdown 报告中标注哪些规则因语言被跳过 |

### Phase 4：文档 + 示例

| 步骤 | 文件 | 内容 |
|---|---|---|
| 4.1 | `README.md` | 对比命令示例（标准 vs 增强） |
| 4.2 | `README.md` | 架构说明（Graph 复用关系图） |

---

## 5. 不做什么

以下事情**不做**，原因是违背「最小改动、最大复用」原则：

| 不做的事 | 原因 |
|---|---|
| 修改 `graph.py` 添加新节点 | 上游 graph 的结构不是 contrib 该动的 |
| 修改 `state.py` 添加新字段 | 同上，现有字段已覆盖所有需求 |
| 把 gap_fill 注册为 graph node | 需要改 `ANALYZER_NODES` 注册表，侵入上游 |
| 在 graph 外部重写分析管线 | 已有 20 个分析器 + meta_analyzer，无需重复 |
| 自建 provider / 凭证系统 | 原项目 provider 已完美覆盖 openai / anthropic / nv_build |
| 自建 token 估算 | `LLMAnalyzerBase.estimate_tokens()` 已存在 |
| 自建 batch 分批 | `LLMAnalyzerBase.get_batches()` 已存在 |

---

## 6. 收益总结

| 维度 | 改造前 | 改造后 |
|---|---|---|
| 轮子重复 | gap_fill 手工 JSON 解析、硬截断 | 继承 `LLMAnalyzerBase`，零重复 |
| Token 安全 | 3000 字符硬截断，无预算检查 | `get_batches()` 自动分批 + 重叠 |
| 结构化输出 | `json.loads()` + `strip("```")` | LangChain `with_structured_output()` + Pydantic 验证 |
| 并行度 | 串行 for 循环 | `asyncio` 外层并行 + Graph 内部并行 |
| 上游比对 | 无法对比标准 vs 增强 | `scan_mode` 标记 + 相同 JSON schema + diff 就绪 |
| 理解负担 | 自创 prompt 模板、解析逻辑 | 统一 `BASE_ANALYSIS_PROMPT` + `LLMAnalyzerBase` 模式 |
| 侵入性 | 无（当前已不侵入） | 无（继续保持零侵入） |
| 上游可合并性 | 完全独立 contrib | 完全独立 contrib，随时可提 PR |
