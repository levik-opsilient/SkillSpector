# Batch Scan Feature for SkillSpector

## Context

SkillSpector 当前 `scan` 命令一次只能扫一个 skill。用户需要批量审核包含数百个 skill 的仓库。项目刚开源一个月（2026-05-11），36 commit，批量扫描是自然的功能延伸。

## Design Principles

1. **像从项目里长出来的，不是硬塞进去的**——复用全部现有模式
2. **只动 CLI 层**——不动 graph、不动 report 节点、不动 analyzer
3. **输出一个大文件**——不做零碎文件，方便集中查看和后续 LLM 筛选

---

## Output Format

### 一个大 JSON 文件，结构复用现有单 scan 报告

内部 skill 条目完全复用 `report.py:_format_json()` 的 `skill` / `risk_assessment` / `components` / `issues` 四个块，外面套 batch 外壳：

```json
{
  "batch": {
    "scanned_at": "2026-06-17T19:31:29+00:00",
    "total_skills": 150
  },
  "skills": [
    {
      "skill": { "name": "evil-skill", "source": "./skills/evil-skill", "scanned_at": "..." },
      "risk_assessment": { "score": 100, "severity": "CRITICAL", "recommendation": "DO_NOT_INSTALL" },
      "components": [
        { "path": "SKILL.md", "type": "markdown", "lines": 53, "executable": false, "size_bytes": 1234 },
        { "path": "scripts/helper.py", "type": "python", "lines": 31, "executable": true, "size_bytes": 567 }
      ],
      "issues": [
        { "id": "E1", "category": "数据外泄", "severity": "HIGH", "confidence": 0.89, ... }
      ]
    }
  ],
  "metadata": {
    "skillspector_version": "2.2.3",
    "llm_requested": false,
    "llm_available": false
  }
}
```

终端的汇总表样式复用 `report.py:_format_terminal()` 的 Rich Panel/Table/配色。

---

## CLI

### 新命令：`scan-batch`

```bash
# 终端打印汇总表
skillspector scan-batch ./all-skills/

# 落地一个大 JSON（绝对路径随便写）
skillspector scan-batch ./skills/ --format json -o /Users/me/Desktop/batch-report.json

# Markdown 报告
skillspector scan-batch ./skills/ --format markdown -o batch-report.md
```

### 参数设计（完全复用 `scan` 的模式，不发明新参数）

| 参数 | 类型 | 说明 |
|------|------|------|
| `input_dir` | Argument（Path） | 包含多个 skill 子目录的目录 |
| `--format` / `-f` | Option | terminal / json / markdown（无 sarif，batch 不适合） |
| `--output` / `-o` | Option（Path） | 输出文件路径，不指定则 stdout |
| `--no-llm` | Option（bool） | batch 模式建议默认不开 LLM |
| `--verbose` / `-V` | Option（bool） | 显示详细进度 |

不引入 `--summary-only`、`--parallel` 等新参数——保持 CLI 表面跟 `scan` 一致。

### 运行流程

1. **发现**：遍历 input_dir，找到所有含 `SKILL.md` 的直接子目录，按名称排序
2. **逐个扫描**：每个 skill 调用 `graph.invoke()`，复用 `_scan_state()` 构建初始 state
3. **进度输出**：每扫完一个打印 `[3/150] my-skill → 23/100 MEDIUM (2 issues)`
4. **汇总输出**：所有结果按风险分降序，生成终端汇总表或 JSON/Markdown 文件
5. **失败不阻塞**：单个 skill 报错打印 `[WARN]` 继续下一个
6. **退出码**：有 skill > 50 分 → 1，运行错误 → 2，全绿 → 0

### 代码风格匹配

- 复用 `_scan_state()`、`_write_result()`、`_cleanup_result()` 三个已有 helper
- 新增 `_discover_skills(root: Path) -> list[Path]`
- 新增 `_format_batch_json(results) -> str` / `_format_batch_terminal(results) -> str`
- `scan_batch` 命令函数完全模仿 `scan` 的结构：Annotated 参数 → try/except/typer.Exit → finally cleanup
- Rich 配色用 report.py 同款 severity_colors

---

## Files to Modify

| File | Change | Lines |
|------|--------|-------|
| `src/skillspector/cli.py` | 新增 `_discover_skills()` + `_format_batch_json()` + `_format_batch_terminal()` + `scan_batch` 命令 | ~120 |
| `tests/unit/test_cli.py` | 新增 4 个测试 | ~60 |

### 不改的文件

`graph.py` · `state.py` · `models.py` · `report.py` · 所有 analyzer · `input_handler.py`

---

## Verification

```bash
# 用项目自带 fixtures 测试（目录里有多个 skill）
skillspector scan-batch ./tests/fixtures/

# 落地 JSON 验证结构
skillspector scan-batch ./tests/fixtures/ --format json -o /tmp/batch-test.json

# 单元测试
pytest tests/unit/test_cli.py -v

# 全量回归
make test-unit && make lint
```
