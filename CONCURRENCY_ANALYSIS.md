# 并发控制与 API 限流分析

> 日期：2026-06-18
> 问题：批量扫描时，多层并行叠加可能导致 API 限流（429 Too Many Requests）
> 目的：分析原项目的限流设计，给出批量层的安全并发策略

---

## 1. 原项目有什么

只有一样东西：**asyncio.Semaphore(10)**。

```python
# llm_analyzer_base.py:372-405

async def arun_batches(self, batches, *, max_concurrency=10, **kwargs):
    sem = asyncio.Semaphore(max_concurrency)   # ← 唯一的限流点

    async def _process(batch):
        async with sem:                         # ← 拿到槽位才能发请求
            response = await self._structured_llm.ainvoke(prompt)
            return (batch, self.parse_response(response, batch))

    return list(await asyncio.gather(*[_process(b) for b in batches]))
```

工作方式：

- 假设有 30 个 batch 要处理，Semaphore(10) 保证**同一时刻最多 10 个请求在空中飞**
- 第 11 个 batch 必须等前面某个完成，释放槽位，才能开始
- 全部 30 个处理完，函数返回

原项目**没有**的东西：重试、退避、429 处理、令牌桶。LangChain 的 `ChatOpenAI` 内部有默认 2 次重试，但那是对网络错误的通用重试，不是针对 API 限流的。

## 2. 为什么单 skill 场景下 10 并发没问题

```
一次 graph.invoke() 调用链路：

graph.invoke(state)
  │
  ├─ SSD 分析器  ── arun_batches(sem=10)  →  最多 10 个请求
  ├─ SDI 分析器  ── arun_batches(sem=10)  →  最多 10 个请求
  ├─ SQP 分析器  ── arun_batches(sem=10)  →  最多 10 个请求
  ├─ TP4 分析器  ── 单个 chat_completion   →  1 个请求
  └─ meta_analyzer ── arun_batches(sem=10) →  最多 10 个请求
```

**但是**，这些不是同时发生的。原因：

1. **Graph 是同步的**。`graph.invoke()` 内部虽然每个分析器可能用 `asyncio.run(analyzer.arun_batches())` 做并发，但分析器之间，LangGraph 的处理方式是 fan-out → 等全部完成 → fan-in。实际时间线上，所有 20 个分析器的 **batch 请求是交错而不是严格同时的**。

2. **单 skill 的文件少**。一个典型 skill 目录 5-15 个文件，大部分文件一个 batch 就装下了。SSD 分析器可能只有 3 个 batch，Semaphore(10) 根本打不满。

3. **非 LLM 分析器不参与**。20 个分析器里有 15 个是纯静态的，不发任何 API 请求。

真实并发峰值：大概 15-25 个同时请求，大多数 API 提供商的免费/基础 tier 都能承受。

## 3. 批量场景下发生了什么变化

```
批量扫描 4 个 skill，完全并行：

skill_1 ─── graph.invoke()
               ├─ SSD  ── arun_batches(sem=10)  →  最多 10
               ├─ SDI  ── arun_batches(sem=10)  →  最多 10
               ├─ SQP  ── arun_batches(sem=10)  →  最多 10
               └─ meta ── arun_batches(sem=10)  →  最多 10

skill_2 ─── graph.invoke()（同上 × 4）

skill_3 ─── graph.invoke()（同上 × 4）

skill_4 ─── graph.invoke()（同上 × 4）
                ↓
        理论上限：4 × 40 = 160 个同时请求
```

**关键问题：每个 `arun_batches` 的 Semaphore 是独立实例，不跨 skill 共享。** 4 个 skill 意味着 4 套独立的 Semaphore(10)，每套都在放行自己的请求，最终全部冲向同一个 API endpoint。

## 4. 方案对比

### 方案 A：全局共享 Semaphore（垂直限流）

在所有 `arun_batches` 之上加一个全局闸门：

```
全局 Semaphore(limit)  ← 新加的这一层
  │
  ├─ skill_1 ─── graph.invoke()
  │               ├─ SSD  ── arun_batches(sem=10)  每个请求都要先过全局闸
  │               └─ ...
  ├─ skill_2 ─── graph.invoke()
  │               └─ ...
  └─ ...
```

**问题**：需要侵入原项目代码。每个 `arun_batches` 调用点都要传这个全局 semaphore，或者 hack `get_chat_model()` / `chat_completion()`。这与「零侵入」原则矛盾。

### 方案 B：限制并行 skill 数量（水平限流）

不碰原项目的任何代码。只在批量调度层控制**同时有几个 skill 在跑**：

```
ThreadPoolExecutor(max_workers=4)  ← 只在这里控制
  │
  ├─ skill_1 ── graph.invoke()（原封不动）
  ├─ skill_2 ── graph.invoke()（原封不动）
  ├─ skill_3 ── graph.invoke()（原封不动）
  ├─ skill_4 ── graph.invoke()（原封不动）
  │
  └─ 第 5 个 skill 排队等前面的完成
```

**优点**：
- 零侵入。不改变 `arun_batches`、不改变 graph、不改变任何原项目代码
- `max_workers` 一目了然，理解成本为零
- 实际并发 = `max_workers × (单 skill 内部峰值)`，可控可预测

**缺点**：
- 粒度粗。一个 skill 跑得慢会阻塞队列（即使它大部分时间在等网络）
- 不如方案 A 精细（无法精确到「同时最多 N 个 API 请求」）

### 方案 C：混合方案（水平限流 + 提供选项）

以方案 B 为基础，增加一个用户可调的 `--workers` 参数：

```python
# batch_scan.py

def scan_all(skill_dirs, *, max_workers=4):
    """
    max_workers=4 含义：
    - 同一时刻最多 4 个 skill 在跑 graph.invoke()
    - 每个 skill 内部的 arun_batches(sem=10) 继续正常工作
    - 峰值并发 ≈ 4 × 10-20 = 40-80，大多数 API 可承受

    用户根据 API tier 自行调整：
    - 免费 tier → --workers 1
    - 基础付费 → --workers 4（默认）
    - 企业 tier → --workers 8
    """
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(run_one, d, root, use_llm=use_llm): d
            for d in skill_dirs
        }
        results = []
        for future in as_completed(futures):
            entry, error = future.result()
            results.append(entry)
    return results
```

**这是推荐方案**。理由：

| 维度 | 方案 A（全局 Semaphore） | 方案 B/C（水平限流） |
|------|------------------------|---------------------|
| 侵入性 | 需要改 llm_utils 或 analyzer | **零侵入**，只改 batch_scan.py |
| 可理解性 | 需要理解 Semaphore 在哪生效 | `max_workers` 一个数字，和任何线程池一样 |
| 精细度 | 精确到 API 请求级别 | 精确到 skill 级别 |
| 与上游一致性 | 引入了原项目没有的全局闸门 | 和原项目一样，只加一层不碰底层 |
| 用户可控 | 写死在代码里 | `--workers` CLI flag |

## 5. 推荐方案的并发数估算

```
--workers 4（默认），每个 skill 内部真实情况：

  skill 内部 LLM 调用：
    SSD  ≈ 3 batch   × 1（同步 run_batches）      = 3 并发
    SDI  ≈ 3 batch   × 10（async arun_batches）   = 3 并发（打不满）
    SQP  ≈ 3 batch   × 10                         = 3 并发
    TP4  = 1 请求                                  = 1 并发
    meta ≈ 2 batch   × 10                         = 2 并发
    ─────────────────────────────────────────────────
    单 skill 峰值 ≈ 3+3+3+1+2 = 12 并发请求

  但实际时间线：
    SSD/SDI/SQP/meta 是串行的（每个等前一个 asyncio.run 完成）
    真正同时的只有 arun_batches 内部的 gather
    
  真实并发 = max_workers × (arun_batches 内部并发)
           ≈ 4 × 10 = 40（理论上限，实际 15-25）
```

**结论**：`max_workers=4` 在绝大多数情况下安全。用户如果遇到 429，把 `--workers` 调到 2 或 1 就行。

## 6. CLI 设计

```bash
# 默认 4 并发，适合大多数付费 API
python -m contrib.multilingual.batch_scan ./skills/ --no-llm

# 免费 tier，串行跑
python -m contrib.multilingual.batch_scan ./skills/ --workers 1

# 企业 tier，8 并发
python -m contrib.multilingual.batch_scan ./skills/ --workers 8
```

| --workers | 适用场景 | 预估峰值并发 |
|-----------|---------|------------|
| 1 | 免费 API / 调试 | 10-15 |
| 4（默认）| 基础付费 tier | 25-40 |
| 8 | 企业 tier | 50-80 |

## 7. 为什么不做得更复杂

原项目的限流哲学是「一个 Semaphore 就够」。没有重试、没有退避、没有令牌桶。不是因为他们没想到，而是因为：

1. **LangChain 替你做了重试**。`ChatOpenAI` 默认 `max_retries=2`，网络抖动自动重试。
2. **场景决定复杂度**。单 skill 的文件量和并发需求，一个 Semaphore(10) 全覆盖。
3. **复杂度外包给 provider**。真正的 rate limit 处理在 API 服务端，客户端只需控制并发数。

批量层遵循同样的哲学：一个 `max_workers`，够了。不加额外的重试、退避、令牌桶。保持和原项目一样的设计密度。
