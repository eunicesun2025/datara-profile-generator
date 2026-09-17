# 模型响应整体包裹 Markdown 代码围栏导致解析为空，候选轮被误判“无改进”而提前停止

日期：2026-09-17

触发问题：优化在第二轮后停止，用户反馈“是不是候选提示词有问题”。取证表明候选提示词大概率没有问题：失败样本的候选轮提取中，模型响应原文包含了期望值（税号 `05258865`），但整体被包裹在 ```` ```json … ``` ```` 代码围栏里。`strict_json()` 立即抛出 `Expecting value: line 1 column 1 (char 0)`，调用方回退为空对象 `{}`，正确值被记为 `missing_actual_path`；两轮候选都因此“无改进”，触发 `no_improvement_limit=2` 早停。

受影响范围：所有解析模型原始响应的路径——optimizer 提取结果与候选分析、app“测试提取”任务、provider 字段建议草稿。ground truth、手工结果校验等用户输入不受影响，仍严格拒绝围栏。

测试结果：全量 `uv run pytest -q` 通过（125 passed），新增 5 个测试覆盖 domain 围栏解析规则、provider 草稿、app 提取任务、optimizer 提取评分与基线缓存版本门槛的回归。

## 取证（data/optimizer，2026-09-17 运行 5214731e 与重跑 f83dca18）

- 两个运行均 `status="completed"`、`stop_reason="no_improvement"`，各含 2 个 `status="rejected"` 迭代（5214731e：`8469442e`/`5bba1c3d`；f83dca18：`e78a80a8`/`eeb22b59`），`decision_reasons=["Failure Set 提升 0.0000，低于阈值 0.0100"]`，`metrics.failure_selected.ratio=0.0`。
- 4 条候选轮提取（qwen3.8-max-0902；`75024164`/`f6b30f32` 属 5214731e，`93d6358e`/`ae93f599` 属 f83dca18）的 `raw_response` 全部以 ```` ```json ```` 开头、整体被围栏包裹：`structure_validation.errors=["Expecting value: line 1 column 1 (char 0)"]`、`parsed_output={}`，所选字段（税号）被判 `missing_actual_path`。
- 其中 3 条围栏原文含与 Ground truth 一致的 `"tax_number": "05258865"`——候选提示词实际已修复该失败样本，只是被解析层拒收。`f6b30f32` 返回的是数字形式 `"tax_number": 5258865`（丢失前导零），属于另一个真实的模型输出质量问题，解析恢复后才会暴露。
- 两次 baseline 提取（`a2d03d66`、`933c0ba4`）未带围栏、解析正常；候选分析模型响应也解析正常（迭代记录含完整 `analyses`/`diffs`），说明围栏是视觉模型在提取阶段的偶发行为。
- 提示词侧已明确写有“只返回 JSON 对象，不要 Markdown”，说明是模型偶发不遵从指令，而非提示词缺少约束；继续加严提示词无法根治，需在解析层容错。

## 改动文件

- `datara/domain.py`：新增 `_CODE_FENCE`、`strip_code_fence()`、`model_json()`；`strict_json()` 保持不变。
- `datara/optimizer.py`：提取结果与候选分析两处解析改用 `model_json()`；新增 `RESPONSE_PARSER_VERSION = 2` 常量，extraction 记录写入 `response_parser_version`，`_cached_baseline()` 拒绝复用旧解析规则产生的缓存记录；导入更新。
- `datara/app.py`：“测试提取”任务结果解析改用 `model_json()`；手工结果校验 `/api/results/validate` 仍用 `strict_json()`。
- `datara/provider.py`：`parse_analysis()`（字段建议草稿）改用 `model_json()`；导入更新。
- `tests/test_domain.py`、`tests/test_provider.py`、`tests/test_app.py`、`tests/test_optimizer.py`：新增测试；原有“strict_json 拒绝围栏”的测试保持不变。
- `README.md`：在“优化耗时与模型设置”补充模型响应围栏容错说明。
- `docs/changes/2026-09-17-model-response-code-fence.zh-CN.md`：本文档。

## 变更内容

- **仅当围栏包裹整个响应时才剥离**：`strip_code_fence()` 用 `re.fullmatch` 匹配 ```` ```[lang]\n…\n``` ````；围栏外有任何前后缀文字、围栏未闭合、或响应本身不是围栏时，原文原样交给 `strict_json()` 拒绝。剥离后仍执行全部严格规则：重复键、NaN/Infinity 等一律 `ValueError`。
- **解析入口区分“模型响应”与“用户输入”**：模型响应（optimizer 提取/候选分析、app 提取任务、provider 草稿）走 `model_json()`；用户输入（ground truth、手工结果校验）继续走 `strict_json()`，不接受围栏——与既有的“用户填写值必须合法 JSON”约定一致。
- **optimizer 行为恢复**：此前围栏响应使 `parsed_output={}`、`structure_validation.errors=["Expecting value…"]`，评分全为 `missing_actual_path`，候选即使修复了失败样本也被拒绝；现在正确值参与比对，`selected_failure_ratio` 反映真实提取质量。
- **app“测试提取”行为恢复**：此前任务状态 `completed` 但结果为空、错误信息显示 `Expecting value: line 1 column 1 (char 0)`；现在围栏响应正常展示结果与校验状态。
- **基线缓存版本门槛**：`_cached_baseline()` 直接复用历史 extraction 存储的 `parsed_output`，被围栏污染的空结果会在 24h TTL 内持续命中。现在每条 extraction 记录写入 `response_parser_version`（当前为 2），缓存匹配要求版本一致；修复前产生的记录（无该字段）自动失效，重跑优化会重新提取一次基线并写入新版缓存。

## 新增测试

- `tests/test_domain.py::test_model_json_accepts_only_a_full_response_code_fence`：带/不带语言标注的围栏、裸 JSON 均可解析；围栏外有文字、未闭合围栏、围栏内重复键、围栏内 NaN 仍抛 `ValueError`。
- `tests/test_provider.py::test_draft_parses_a_fenced_model_response`：围栏包裹的草稿分析响应可产出字段建议。
- `tests/test_app.py::test_extract_job_parses_a_fenced_model_response`：围栏响应使提取任务正常 `completed`，结果与校验状态正确。
- `tests/test_optimizer.py::test_fenced_extraction_response_is_parsed_and_scored`：复现 run 5214731e 场景——围栏内的正确值被完整解析、无 `Expecting value` 错误、选中字段 `matched`、`failure_selected.ratio=1.0`。
- `tests/test_optimizer.py::test_baseline_cache_ignores_records_parsed_by_older_rules`：新记录携带 `response_parser_version=2`；把记录改成旧版本后，基线缓存不再命中并重新调用模型。

## 验证

- `uv run pytest -q` 全量通过。
- 既有测试 `test_duplicate_keys_non_json_and_nonfinite_are_rejected`（strict_json 拒绝围栏）不变，确认 ground truth/手工校验路径行为未受影响。

## 行为变化与风险

- **模型响应解析放宽一处**：整体围栏包裹的响应从“解析失败”变为“剥离围栏后严格解析”。围栏内容仍受重复键/非有限数值等全部严格规则约束，不存在静默接受非法 JSON 的情况。
- **用户输入解析无任何变化**：ground truth 粘贴、手工结果校验、profile 导入仍拒绝围栏。
- 若模型在围栏外还输出了说明文字，仍会解析失败——这是有意保留的严格性；错误会原样呈现在候选分析错误与任务错误里，便于发现模型不遵从指令的其他形态。
- 历史数据不回填：既有 extraction/iteration 记录中的 `parsed_output={}` 保持不变，但通过 `response_parser_version` 门槛不再被基线缓存复用；重新运行优化即可得到正确评分，代价是每个样本多一次基线提取调用。

## 后续待办

- 若其他模型（kimi、glm）出现围栏外的新不遵从形态（如前置说明文字），评估是否在提示词层补充few-shot或在解析层继续按需容错。
