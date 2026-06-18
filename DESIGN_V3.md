# Contrib 多语言批量扫描 — 设计文档 v3

> 日期：2026-06-18
> 状态：待实施
> 原则：零侵入原项目 · 子类化复用 · 可对比 · API Pool 调度

---

## 总览：四层架构

```
┌─────────────────────────────────────────────────────────┐
│                    CLI 层                                │
│  python -m contrib.multilingual.batch_scan ./skills/     │
│  --workers 4 --format json --output report.json          │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│                 调度层（Worker Pool）                     │
│  ThreadPoolExecutor(max_workers=4)                       │
│  控制同时跑几个 skill，不碰底层                            │
└──────────────────────┬──────────────────────────────────┘
                       │ 每个 worker 拿到一个 skill
┌──────────────────────▼──────────────────────────────────┐
│               API Pool 层（新增 ★）                       │
│  ApiKeyPool: 多 key → 调度 → 限流标记 → 换 key 重试       │
│  对上层透明，worker 感知不到 key 切换                      │
└──────────────────────┬──────────────────────────────────┘
                       │ 每次 LLM 调用经过 Pool 分配 key
┌──────────────────────▼──────────────────────────────────┐
│                 执行层（原项目，不改）                      │
│  graph.invoke(state)                                     │
│  ├─ resolve_input → build_context                        │
│  ├─ 15 静态分析器（无 API 调用）                           │
│  ├─ 4 LLM 分析器（经 API Pool）  + GapFillAnalyzer        │
│  ├─ meta_analyzer（经 API Pool）                          │
│  └─ report                                              │
└─────────────────────────────────────────────────────────┘
```

四层各自独立，每一层只跟下一层对话，不知道上一层的存在。

---

## 1. API Pool — 核心创新

### 1.1 问题

```
Worker-1 ──► key_A ──► API ──► 429 (限流) ──► 挂了
Worker-2 ──► key_B ──► API ──► 200 OK
Worker-3 ──► key_C ──► API ──► 200 OK
Worker-4 ──► key_D ──► API ──► 429 (限流) ──► 挂了
```

Semaphore / max_workers 只能减少撞限流的概率，撞上了还是死。

### 1.2 方案

```
                        ┌─────────────┐
                        │  API Pool   │
                        │             │
   Worker-1 ──请求──►  │  Scheduler  │  ──分配──► key_A (空闲) ──► API ✓
   Worker-2 ──请求──►  │             │  ──分配──► key_B (空闲) ──► API ✓
   Worker-3 ──请求──►  │  状态表     │  ──分配──► key_C (空闲) ──► API 429 ✗
   Worker-4 ──请求──►  │             │       │
                        │             │       └──► 标记 key_C 限流 30s
                        └─────────────┘            换 key_D 重试 ──► API ✓
                              │
                              │  30 秒后
                              ▼
                        key_C 恢复为「空闲」
```

### 1.3 核心数据结构

```python
@dataclass
class ApiKey:
    key: str
    base_url: str
    model: str
    status: Literal["idle", "in_use", "rate_limited"]
    rate_limited_until: float = 0.0       # 限流恢复时间戳
    consecutive_429: int = 0              # 连续 429 次数
    total_requests: int = 0               # 总请求数（监控用）


class ApiKeyPool:
    """多 API Key 资源池，K8s-scheduler 风格调度"""

    def __init__(self, keys: list[ApiKey]):
        self._keys = keys
        self._lock = threading.Lock()
        # 默认状态：全部 idle

    def acquire(self) -> ApiKey:
        """获取一个可用的 key。

        优先级：
        1. idle 且未限流的 key
        2. 限流已到期的 key（自动恢复）
        3. 最少使用的 key（负载均衡）
        4. 阻塞等待（所有 key 都限流中）
        """
        with self._lock:
            now = time.monotonic()

            # 恢复限流到期的 key
            for k in self._keys:
                if k.status == "rate_limited" and now >= k.rate_limited_until:
                    k.status = "idle"

            # 找 idle key
            idle = [k for k in self._keys if k.status == "idle"]
            if idle:
                key = min(idle, key=lambda k: k.total_requests)
                key.status = "in_use"
                key.total_requests += 1
                return key

            # 全部 in_use 或 rate_limited → 等恢复
            # 返回恢复最快的 key 的等待时间
            ...

    def release(self, key: ApiKey, success: bool = True):
        """归还 key。success=False 表示遇到 429"""
        with self._lock:
            if success:
                key.status = "idle"
                key.consecutive_429 = 0
            else:
                key.consecutive_429 += 1
                backoff = min(30 * (2 ** key.consecutive_429), 300)  # 30s → 60s → 120s → 300s cap
                key.rate_limited_until = time.monotonic() + backoff
                key.status = "rate_limited"
```

### 1.4 调度流程（一图说清）

```
acquire()
  │
  ├─ Step 1: 扫描所有 key，恢复限流到期的
  │     rate_limited + now >= rate_limited_until  →  idle
  │
  ├─ Step 2: 有 idle key？
  │     YES → 选 total_requests 最少的（负载均衡）→ 标记 in_use → 返回
  │     NO  → 下一步
  │
  ├─ Step 3: 全都在用 / 全限流？
  │     计算最早恢复时间 → 阻塞等待 → 回到 Step 1
  │
  └─ 返回 ApiKey


release(key, success)
  │
  ├─ success=True  → key 标记 idle，consecutive_429 归零
  │
  └─ success=False → consecutive_429++
                     退避 = min(30 × 2^n, 300) 秒
                     标记 rate_limited，记录恢复时间
```

### 1.5 与 LangChain 集成

Pool 对上层透明，通过一个薄 wrapper 注入：

```python
class PooledChatModel:
    """包装 LangChain ChatModel，每次 invoke 前从 Pool 获取 key"""

    def __init__(self, pool: ApiKeyPool, model_label: str):
        self._pool = pool
        self._model_label = model_label

    def invoke(self, prompt):
        key = self._pool.acquire()
        try:
            llm = self._build_llm(key)      # 用这个 key 创建 ChatOpenAI
            result = llm.invoke(prompt)
            self._pool.release(key, success=True)
            return result
        except RateLimitError:               # 429
            self._pool.release(key, success=False)
            return self.invoke(prompt)       # 递归重试 → acquire 会换 key
```

这样原项目的 `graph.invoke()` 内部完全不用改——它调 `_structured_llm.invoke(prompt)`，PooledChatModel 透明接管 key 的获取和归还。

### 1.6 配置方式

```bash
# 环境变量方式（推荐）
export SKILLSPECTOR_API_KEYS="
  sk-or-xxx1|https://api.openai.com/v1|gpt-5.4
  sk-or-xxx2|https://api.openai.com/v1|gpt-5.4
  sk-or-xxx3|https://api.openai.com/v1|gpt-5.4
"

# 或者每个 key 单独配置（和原项目兼容）
export OPENAI_API_KEY=sk-or-xxx1
export OPENAI_API_KEY_2=sk-or-xxx2
export OPENAI_API_KEY_3=sk-or-xxx3
```

不配置多 key 时退化为原项目默认行为（单 key，无 pool）。

---

## 2. 完整架构图

```
┌────────────────────────────────────────────────────────────────────┐
│                         用户命令                                    │
│  python -m contrib.multilingual.batch_scan ./skills/               │
│    --workers 4 --format json -o report.json --lang auto            │
└─────────────────────────────┬──────────────────────────────────────┘
                              │
┌─────────────────────────────▼──────────────────────────────────────┐
│                     batch_scan.py 主循环                            │
│                                                                    │
│  1. discover_skills(root) → [skill_1, skill_2, ..., skill_N]       │
│  2. detect_language()     → 每个 skill 的语言标记                   │
│  3. ThreadPoolExecutor(max_workers=4)                              │
│       │                                                            │
│       ├─ Worker-1: scan_one(skill_1, lang=zh) ─┐                  │
│       ├─ Worker-2: scan_one(skill_2, lang=ja)  │                  │
│       ├─ Worker-3: scan_one(skill_3, lang=en)  ├─ 并行             │
│       └─ Worker-4: scan_one(skill_4, lang=ko) ─┘                  │
│  4. aggregate results → report formatter                           │
└─────────────────────────────┬──────────────────────────────────────┘
                              │ 每个 Worker 内部
                              ▼
┌────────────────────────────────────────────────────────────────────┐
│                    scan_one() — 单 skill 流程                       │
│                                                                    │
│  ┌─────────────┐                                                  │
│  │ graph.invoke│──► resolve_input → build_context                  │
│  │   (state)   │    ├─ 15 静态分析器（纯 CPU，不调 API）             │
│  │             │    ├─ SSD / SDI / SQP / TP4 ──┐                  │
│  │             │    └─ meta_analyzer ──────────┤                  │
│  └─────────────┘                               │ LLM 调用          │
│                                                 ▼                  │
│  ┌─────────────┐                    ┌──────────────────┐          │
│  │ GapFill     │──► LLM 调用 ──────►│   API Key Pool   │          │
│  │ Analyzer    │                    │                  │          │
│  │ (LLMAnalyzer│                    │ key_A ──► API    │          │
│  │  Base子类)  │                    │ key_B ──► API    │          │
│  └─────────────┘                    │ key_C ──► API    │          │
│                                     │ key_D ──► API    │          │
│  ┌─────────────┐                    └──────────────────┘          │
│  │ annotation  │──► 标记 language_compatible                       │
│  └─────────────┘                                                  │
│                                                                     │
│  输出: { skill, risk_assessment, components, issues,               │
│          scan_mode: "multilingual-enhanced", enhancements: {...} }  │
└────────────────────────────────────────────────────────────────────┘
```

---

## 3. 改动清单

### 3.1 新建文件

| 文件 | 内容 | 行数 |
|------|------|------|
| `contrib/multilingual/api_pool.py` | `ApiKey`, `ApiKeyPool`, `PooledChatModel` | ~120 |
| `contrib/multilingual/gap_fill.py` | **重写**：`GapFillAnalyzer(LLMAnalyzerBase)` | ~100 |
| `contrib/multilingual/batch_scan.py` | **重写**：asyncio/ThreadPool 并行 + API Pool | ~200 |

### 3.2 修改文件

| 文件 | 改动 | 说明 |
|------|------|------|
| `contrib/multilingual/runner.py` | entry 加 `scan_mode` / `enhancements` | 对比标记 |
| `contrib/multilingual/reports.py` | 报告头加模式标签 + API Pool 统计 | 可见标记 |
| `contrib/multilingual/__init__.py` | 导出新符号 | API 兼容 |

### 3.3 不改的文件（零侵入）

```
src/skillspector/graph.py
src/skillspector/state.py
src/skillspector/cli.py
src/skillspector/llm_analyzer_base.py
src/skillspector/llm_utils.py
src/skillspector/providers/*
src/skillspector/nodes/analyzers/*
src/skillspector/nodes/meta_analyzer.py
src/skillspector/nodes/report.py
contrib/multilingual/detection.py
contrib/multilingual/annotation.py
```

---

## 4. GapFill 改造：从裸函数到 LLMAnalyzerBase 子类

### 4.1 改造前

```python
# 现状：模块级字符串 prompt，手动 json.loads，硬截断
GAP_FILL_PROMPT = """...{language}...{file_contents}..."""
content[:3000]                                 # ← 硬截断
json.loads(text.strip("```").strip())          # ← 手动解析
chat_completion(prompt, model=model)           # ← 裸调用
```

### 4.2 改造后

```python
from pydantic import BaseModel
from skillspector.llm_analyzer_base import LLMAnalyzerBase

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
    response_schema = GapFillResult          # ← 自动 with_structured_output()

    def __init__(self, language: str, model: str | None = None):
        self.language = language
        super().__init__(base_prompt=GAP_FILL_ANALYZER_PROMPT, model=model)

    def build_prompt(self, batch, **kwargs):
        # 复用 BASE_ANALYSIS_PROMPT 的 L<N>: 行号 + 精准优先指令
        return super().build_prompt(batch, language=self.language, **kwargs)
```

### 4.3 自动获得的能力

```
继承自 LLMAnalyzerBase             之前手动做的
──────────────────────────────     ──────────
get_batches()     token 感知分批     content[:3000] 硬截断
chunk_file_by_lines  50行重叠       无
with_structured_output  Pydantic    json.loads() + strip```
arun_batches      Semaphore(10)     无并发控制
BASE_ANALYSIS_PROMPT  L<N>: 行号   无行号
日志 + 错误处理                     无
```

---

## 5. 对比标记

### 5.1 输出结构

```json
{
  "batch": {
    "scanned_at": "2026-06-18T10:00:00+00:00",
    "total_skills": 150,
    "scan_mode": "multilingual-enhanced",
    "enhancements": {
      "language_detection": "unicode-script-ratio",
      "languages_detected": {"zh": 45, "ja": 30, "ko": 25, "en": 50},
      "gap_fill_applied": 100,
      "api_pool": {
        "keys_configured": 4,
        "keys_active": 3,
        "rate_limits_hit": 2,
        "retry_successes": 2
      }
    }
  },
  "skills": [
    {
      "skill": { "name": "...", "language": "zh", "scanned_at": "..." },
      "scan_mode": "multilingual-enhanced",
      "enhancements": {
        "gap_fill_applied": true,
        "gap_fill_findings": 2,
        "english_keyword_rules_skipped": 25
      },
      "risk_assessment": { "score": 45, "severity": "MEDIUM" },
      "issues": [
        {
          "rule_id": "P5",
          "language_compatible": true,
          "source": "gap_fill"
        }
      ]
    }
  ]
}
```

### 5.2 上游对比命令

```bash
# 标准模式（原项目，不动）
skillspector scan ./skills/my-zh-skill/ -f json -o standard.json

# 多语言增强（contrib）
python -m contrib.multilingual.batch_scan ./skills/ -f json -o enhanced.json

# 对比
diff <(jq -S . standard.json) <(jq -S '.skills[] | select(.skill.name=="my-zh-skill")' enhanced.json)
```

---

## 6. CLI

```bash
python -m contrib.multilingual.batch_scan <input_dir> [OPTIONS]

Options:
  -f, --format      terminal | json | markdown       (default: terminal)
  -o, --output      输出文件路径                       (default: stdout)
  --no-llm          跳过 LLM 分析                      (default: False)
  --workers N       并发 worker 数                    (default: 4)
  --lang            auto | en | zh | ja | ko         (default: auto)
  -V, --verbose     DEBUG 日志                        (default: False)
```

所有 flag 语义与原项目 `skillspector scan` 保持一致，新增 `--workers` 和 `--lang`。

---

## 7. 任务清单

### Phase 1：GapFill 子类化（核心改造）

| # | 任务 | 文件 | 输入 | 输出 | 验收标准 |
|---|------|------|------|------|---------|
| 1.1 | 定义 Pydantic 响应模型 | `gap_fill.py` | `_GAP_FILL_RULE_IDS` (现有常量) | `GapFillFinding(BaseModel)`, `GapFillResult(BaseModel)` | 字段完整：rule_id / message / severity / confidence / explanation / remediation |
| 1.2 | 实现 `GapFillAnalyzer(LLMAnalyzerBase)` | `gap_fill.py` | `GAP_FILL_PROMPT` 重构为 `GAP_FILL_ANALYZER_PROMPT` | `class GapFillAnalyzer`，覆盖 `response_schema` / `__init__` / `build_prompt` / `parse_response` | 继承 `get_batches()` token 预算；继承 `arun_batches()` 并发；继承 `BASE_ANALYSIS_PROMPT` L&lt;N&gt;: 行号模板 |
| 1.3 | 保留 `run_gap_fill()` 兼容接口 | `gap_fill.py` | `file_cache: dict`, `language: str`, `model: str \| None` | `list[Finding]` | 签名不变，内部改为实例化 `GapFillAnalyzer` + 调 `run_batches()` |
| 1.4 | 删除旧的手动解析代码 | `gap_fill.py` | `_build_file_contents_section()`, `_parse_gap_fill_response()` | — | 不再有 `content[:3000]` 硬截断、不再有 `json.loads()` + strip fence |
| 1.5 | 单 skill 回归验证 | `batch_scan.py` | `./tests/fixtures/ssd/` | gap-fill findings 列表 | 用原项目 fixture 跑，对比改造前后的 gap-fill 输出一致 |

### Phase 2：API Pool（多 key 调度）

| # | 任务 | 文件 | 输入 | 输出 | 验收标准 |
|---|------|------|------|------|---------|
| 2.1 | 定义 `ApiKey` 数据类 | `api_pool.py` | — | `@dataclass ApiKey`：key / base_url / model / status / rate_limited_until / consecutive_429 / total_requests | status 三态：idle / in_use / rate_limited |
| 2.2 | 实现 `ApiKeyPool` 调度器 | `api_pool.py` | `list[ApiKey]` | `acquire()` → ApiKey / `release(key, success)` | acquire 优先级：idle > 限流到期 > 最少使用；release 失败时 30s × 2ⁿ 退避，上限 300s；线程安全（`threading.Lock`） |
| 2.3 | 实现 `PooledChatModel` 包装器 | `api_pool.py` | `ApiKeyPool`, model_label | LangChain `BaseChatModel` 兼容对象 | `.invoke(prompt)` 和 `.ainvoke(prompt)` 透明切换 key；429 自动 retry 换 key |
| 2.4 | 多 key 配置解析 | `api_pool.py` | `SKILLSPECTOR_API_KEYS` env var | `list[ApiKey]` | 支持 `key\|url\|model` 格式，支持 `OPENAI_API_KEY_2/3` 格式，不配置时退化为单 key |
| 2.5 | 单元测试：模拟 429 | `tests/test_api_pool.py` | mock key 列表 | test pass | key_A 429 → 标记限流 → 换 key_B 成功；key_A 限流到期后自动恢复；全部限流时阻塞等待 |
| 2.6 | 集成：注入 graph 调用路径 | `api_pool.py` + `batch_scan.py` | — | — | GapFill 和 graph 内 LLM 调用经过 `PooledChatModel` |

### Phase 3：并行调度

| # | 任务 | 文件 | 输入 | 输出 | 验收标准 |
|---|------|------|------|------|---------|
| 3.1 | ThreadPoolExecutor 主循环 | `batch_scan.py` | `list[Path]` (skill 目录列表) | `list[dict]` (entry 列表) | `max_workers` 可配，默认 4；单个 skill 失败不阻塞其他 |
| 3.2 | `--workers` CLI flag | `batch_scan.py` | 命令行参数 | — | 和原项目 flag 风格一致（Annotated + typer.Option） |
| 3.3 | 进度输出 | `batch_scan.py` | — | Rich 进度条 或 `[3/150] name → 45 MEDIUM` | 每完成一个 skill 打印一行 |
| 3.4 | 退出码逻辑 | `batch_scan.py` | 扫描结果 | 0 / 1 / 2 | 有 skill > 50 → 1；有运行错误 → 2；全绿 → 0 |
| 3.5 | 并发压测 | `batch_scan.py` | `./tests/fixtures/` (已知安全) | 无死锁、无丢失结果 | `--workers 1/2/4/8` 全部通过，结果一致 |

### Phase 4：对比标记 + 报告

| # | 任务 | 文件 | 输入 | 输出 | 验收标准 |
|---|------|------|------|------|---------|
| 4.1 | entry 增加 `scan_mode` / `enhancements` | `runner.py` | `result: dict` | `entry: dict` | `scan_mode: "multilingual-enhanced"`；`enhancements.gap_fill_applied`；`enhancements.english_keyword_rules_skipped: 25` |
| 4.2 | batch 外壳增加 API Pool 统计 | `reports.py` | `list[entry]` | 报告头部 | `api_pool.keys_configured / keys_active / rate_limits_hit / retry_successes` |
| 4.3 | terminal 报告加模式标签 | `reports.py` | `list[entry]` | Rich Panel | 头部显示 `Scan mode: Multilingual Enhanced` + 语言分布表 |
| 4.4 | JSON 报告结构对齐 | `reports.py` | `list[entry]` | JSON 字符串 | 每个 skill entry 含完整 `enhancements` 元数据 |
| 4.5 | Markdown 报告加模式标签 | `reports.py` | `list[entry]` | .md 文件 | 头部说明 enhancement 内容 + 对比命令示例 |
| 4.6 | 对比验证 | `reports.py` + 手动 | 同一 skill 的标准报告 vs 增强报告 | diff 输出 | `jq -S` 后 diff 可见差异来源（language_compatible / gap_fill findings / scan_mode） |

### Phase 5：文档 + 清理

| # | 任务 | 文件 | 输出 | 验收标准 |
|---|------|------|------|---------|
| 5.1 | 更新 `__init__.py` 导出 | `__init__.py` | 导出 `ApiKeyPool`, `GapFillAnalyzer`, `PooledChatModel` | `from contrib.multilingual import ApiKeyPool` 可用 |
| 5.2 | `ARCHITECTURE_UNDERSTANDING.md` | contrib/ | 架构理解文档 | 新开发者 10 分钟看懂设计哲学 |
| 5.3 | `DESIGN_V3.md` | 项目根 | 本文件 | 移除「待实施」标记 |

---

### 依赖关系

```
Phase 1  ──────┐
               ├──► Phase 3  ──► Phase 4  ──► Phase 5
Phase 2  ──────┘
```

- Phase 1 和 Phase 2 互不依赖，可并行开工
- Phase 3 依赖 Phase 1 (GapFill) 和 Phase 2 (API Pool) 都完成
- Phase 4 依赖 Phase 3（需要完整 entry 结构）
- Phase 5 在 Phase 4 完成后收尾

### 工作量估算

| Phase | 任务数 | 新建/重写行数 | 预计耗时 |
|-------|--------|-------------|---------|
| Phase 1 | 5 | ~100 | 2-3 小时 |
| Phase 2 | 6 | ~120 + ~100 测试 | 3-4 小时 |
| Phase 3 | 5 | ~200 | 2-3 小时 |
| Phase 4 | 6 | ~80 | 1-2 小时 |
| Phase 5 | 3 | ~20 | 0.5 小时 |
| **合计** | **25** | **~620** | **9-13 小时** |

---

## 8. 不做什么

| 不做 | 原因 |
|------|------|
| 改 `graph.py` | 原项目的图结构不动 |
| 改 `state.py` | 现有字段够用 |
| 在 graph 里注册 GapFill 节点 | 需要改 ANALYZER_NODES，侵入上游 |
| 自建 provider | 原项目 provider 已覆盖 |
| 自建 token 预算 / chunking | LLMAnalyzerBase 已提供 |
| 复杂限流算法（令牌桶、滑动窗口） | API Pool + 退避 够用 |
