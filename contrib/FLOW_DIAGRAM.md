# Contrib 整体架构流程图

```
CLI
 │  python -m contrib.multilingual.batch_scan ./skills/ --workers 4 [--no-llm]
 │
 ▼
┌──────────────────────────────────────────────────────────────────────┐
│ batch_scan.py :: main()                                              │
│                                                                      │
│  ① discovery.discover_skills(root)                                   │
│     └─ rglob("SKILL.md") → [Path, Path, ...]  排序                  │
│                                                                      │
│  ② detection.detect_skill_language(file_cache)  每 skill 一次        │
│     └─ 主线程预读文件 → Unicode 脚本比例 → zh/ja/ko/en               │
│                                                                      │
│  ③ api_pool.create_api_key_pool_from_env()  可选                     │
│     └─ SKILLSPECTOR_API_KEYS → ApiKeyPool(10 keys)                  │
│                                                                      │
│  ④ ThreadPoolExecutor(max_workers=4)                                 │
│     ┌─────────────┬─────────────┬─────────────┬─────────────┐       │
│     │  Thread A   │  Thread B   │  Thread C   │  Thread D   │       │
│     │  skill_1    │  skill_2    │  skill_3    │  skill_4    │       │
│     │     │       │     │       │     │       │     │       │       │
│     │     ▼       │     ▼       │     ▼       │     ▼       │       │
│     │  _scan_skill() 并行执行，300s 超时，RuntimeError 重试   │       │
│     └─────────────┴─────────────┴─────────────┴─────────────┘       │
│                                                                      │
│  ⑤ 收集结果，按 risk_score 降序排列                                  │
│  ⑥ reports._format_terminal / _format_json / _format_markdown       │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 单个 skill 扫描流程 (`_scan_skill`)

```
_scan_skill(skill_dir, root, use_llm, lang)
│
│  ┌─── ① runner.run_one(skill_dir, root, use_llm, lang) ────────────┐
│  │                                                                   │
│  │   ⚠️ MONKEY-PATCH ZONE (当前实现，有竞态)                       │
│  │   ┌─────────────────────────────────────────────────────┐        │
│  │   │ _saved = _Base.response_schema                      │        │
│  │   │ _Base.response_schema = None    ← 改全局类属性       │        │
│  │   │ _Meta.response_schema = None    ← 同上              │        │
│  │   │                                                     │        │
│  │   │   graph.invoke(state)  ←── 同步阻塞                 │        │
│  │   │   │                                                 │        │
│  │   │   │  ┌──────────────────────────────────────────┐   │        │
│  │   │   │  │         LangGraph Pipeline               │   │        │
│  │   │   │  │                                         │   │        │
│  │   │   │  │  build_context                          │   │        │
│  │   │   │  │    └─ 下载/解压/构建文件缓存              │   │        │
│  │   │   │  │       temp_dir_for_cleanup ← 临时目录     │   │        │
│  │   │   │  │                                         │   │        │
│  │   │   │  │  ┌─── 20 Analyzers 并行扇出 ─────────┐  │   │        │
│  │   │   │  │  │                                    │  │   │        │
│  │   │   │  │  │  静态规则 (不调 LLM):              │  │   │        │
│  │   │   │  │  │  AST1-8  代码注入检测              │  │   │        │
│  │   │   │  │  │  TT1-5   工具使用检测              │  │   │        │
│  │   │   │  │  │  YR1-4   YARA 规则                 │  │   │        │
│  │   │   │  │  │  SC1-6   供应链检测                │  │   │        │
│  │   │   │  │  │  LP1-4   循环/递归检测             │  │   │        │
│  │   │   │  │  │  TP1-3   工具投毒检测              │  │   │        │
│  │   │   │  │  │  TM1-3   工具滥用检测              │  │   │        │
│  │   │   │  │  │                                    │  │   │        │
│  │   │   │  │  │  LLM 语义规则 (调 LLM):            │  │   │        │
│  │   │   │  │  │  SSD1-4  敏感数据泄露    ──┐      │  │   │        │
│  │   │   │  │  │  SDI1-4  直接注入        │      │  │   │        │
│  │   │   │  │  │  SQP1-3  可疑权限提升    │      │  │   │        │
│  │   │   │  │  │                          │      │  │   │        │
│  │   │   │  │  │  每个 Analyzer 创建时:   │      │  │   │        │
│  │   │   │  │  │    LLMAnalyzerBase.__init__()    │  │   │        │
│  │   │   │  │  │      │                  │      │  │   │        │
│  │   │   │  │  │      ▼                  │      │  │   │        │
│  │   │   │  │  │  self.response_schema ──┘      │  │   │        │
│  │   │   │  │  │    ├─ 类属性 ≠ None             │  │   │        │
│  │   │   │  │  │    │  → with_structured_output  │  │   │        │
│  │   │   │  │  │    │  → DeepSeek 400 ❌         │  │   │        │
│  │   │   │  │  │    │                             │  │   │        │
│  │   │   │  │  │    └─ 类属性 = None (被 patch)  │  │   │        │
│  │   │   │  │  │       → 原始文本模式             │  │   │        │
│  │   │   │  │  │       → parse_response 抛        │  │   │        │
│  │   │   │  │  │         NotImplementedError     │  │   │        │
│  │   │   │  │  │       → fallback 空 findings     │  │   │        │
│  │   │   │  │  └────────────────────────────────┘  │   │        │
│  │   │   │  │                                         │   │        │
│  │   │   │  │  meta_analyzer (扇出结果汇总后执行)      │   │        │
│  │   │   │  │    └─ LLMMetaAnalyzer.__init__()       │   │        │
│  │   │   │  │         self.response_schema ── 同上   │   │        │
│  │   │   │  │                                         │   │        │
│  │   │   │  │  结果汇总 → filter → risk_score         │   │        │
│  │   │   │  └─────────────────────────────────────────┘   │        │
│  │   │   │                                                 │        │
│  │   │   result = {                                        │        │
│  │   │     findings, filtered_findings,                    │        │
│  │   │     risk_score, risk_severity,                      │        │
│  │   │     manifest, component_metadata,                   │        │
│  │   │     temp_dir_for_cleanup                            │        │
│  │   │   }                                                 │        │
│  │   │                                                     │        │
│  │   entry_from_result(result)                             │        │
│  │     └─ 提取字段 → annotation.annotate_findings          │        │
│  │                                                     │        │
│  │   finally:                                          │        │
│  │     _Base.response_schema = _saved   ← 恢复         │        │
│  │     _Meta.response_schema = _saved                  │        │
│  │     cleanup_result(result)          ← 删临时目录      │        │
│  │       └─ shutil.rmtree(temp_dir)   ← ⚠️ 可能卡死    │        │
│  │   ┌─────────────────────────────────────────────────────┐        │
│  │   └─────────────────────────────────────────────────────┘        │
│  │                                                                   │
│  └── ② 返回 (entry, error_msg, rel_name) ─────────────────────────┘
│
│  ┌─── ③ 非英语 + use_llm → gap_fill ─────────────────────────┐
│  │                                                             │
│  │   _read_skill_files(skill_dir)      ← 再次读文件 (重复IO)   │
│  │     │                                                       │
│  │     ▼                                                       │
│  │   run_gap_fill(file_cache, lang, model)                     │
│  │     └─ GapFillAnalyzer(language, model)                     │
│  │          └─ response_schema = None   ← 类属性，设计正确     │
│  │          └─ parse_response() 手动 JSON 解析 + Pydantic      │
│  │     │                                                       │
│  │     ▼                                                       │
│  │   8 规则: P5, P6-P8, MP1-MP3, RA1-RA2                      │
│  │   只有原项目英文关键词静态规则覆盖不到的部分                  │
│  │                                                             │
│  │   entry["issues"] += annotate_findings(gap_findings)        │
│  │   entry["enhancements"]["gap_fill_applied"] = True          │
│  └─────────────────────────────────────────────────────────────┘
│
│  返回 entry (批量结果的一条)
```

---

## 当前问题的三条关键链路

```
链路 1 —— --no-llm 正常 (你的日常):
───────────────────────────────────
  use_llm=False → graph 跳过 SSD/SDI/SQP/meta
  → monkey-patch 被触发但不影响任何东西
  → 无 LLM 调用 → 无 400 → 无连接泄漏
  → cleanup_result 正常完成 ✅


链路 2 —— use_llm=True 竞态中奖 → 400 → 卡死 (上次遇到的):
────────────────────────────────────────────
  Thread A: save → set None → graph.invoke()
  Thread B: save → set None → graph.invoke()
  Thread A: graph 执行完毕 → restore 原始值
  Thread B: meta_analyzer 此时才创建实例
    → 读到 Thread A 刚恢复的原始 schema
    → with_structured_output() → DeepSeek 400
    → httpx 连接池损坏
    → cleanup_result 时 shutil.rmtree 阻塞 🔴


链路 3 —— use_llm=True 竞态躲过 → 运行但不完整:
────────────────────────────────
  Thread A: save → set None → graph 执行 → restore None (被污染)
  Thread B: 始终看到 None → raw text 模式
    → parse_response → NotImplementedError
    → 所有 LLM 分析器空返回 → findings 全空
    → 不报错、不卡死，但结果不完整 🟡
```

---

## Monkey-patch 的正确位置

```
当前:  改类属性 response_schema ──→ 所有实例共享，竞态问题
      LLMAnalyzerBase.response_schema = None


目标:  改实例属性 response_schema ──→ 每个实例独立，无竞态
      在 __init__ 入口处 self.response_schema = None


怎么做:
  _original_init = LLMAnalyzerBase.__init__

  def _patched_init(self, base_prompt, model):
      self.response_schema = None    ← 写入 self.__dict__
      _original_init(self, base_prompt, model)
        └─ self._llm.with_structured_output(self.response_schema)
             ↑ MRO 在 self.__dict__ 找到 None → 停止查找 → 不走类属性
             从此每个实例自己有一个 None，谁也碰不到谁

  LLMAnalyzerBase.__init__ = _patched_init   ← 模块加载时一次，不加锁
```
