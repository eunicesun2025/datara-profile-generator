# 导入的提示词基线被生成提示词覆盖：挂样张等工作区改动即触发重建，用户正文丢失

日期：2026-09-17

触发问题：用户把已发布的外部提示词导入为优化基线后，只要在编辑器里保存一次 Profile（例如挂上样张、选择参考文件、修改数据库名），活动基线就会被换成按 Profile 重新渲染的生成提示词，导入的正文不再出现在活动版本里。

根因：`ensure_prompt_version()` 用 `fingerprint(profile)`（整个 Profile 的哈希）判断导入基线是否过期。`fingerprint` 覆盖 `sample_ids`、`reference_ids`、`database_name`、示例值等与提示词无关的工作区状态，所以这些改动会让判定失败并直接走 `create_prompt_version(..., make_active=True)` 生成新版本；而生成器无法重建用户手写的外部提示词，导入正文因此被覆盖。

受影响范围：所有 `prompt_source="imported"` 的活动基线。生成提示词的 Profile 行为不变（本来就应该按 Profile 重新渲染）。

测试结果：全量 `uv run pytest -q` 通过（130 passed），新增 3 个测试覆盖提示词投影、挂样张不重建、字段规则变更后保留导入正文。

## 取证（本地 data/optimizer，只读扫描）

- Profile `31793218-96de-4bf7-af6f-17a3ac2fe9f3`：10 个版本的 `prompt_source/origin` 序列为 `1:generated/manual_edit, 2:generated/manual_edit, 3:imported/imported_prompt, 4:generated/manual_edit, 5:generated/manual_edit, 6:generated/optimizer_candidate, 7:generated/optimizer_candidate, 8:imported/imported_prompt, 9:imported/imported_prompt, 10:generated/manual_edit`；活动版本是 `10`，即 `generated`——导入版本 `8`/`9` 之后又被生成版本覆盖并成为活动基线。
- Profile `65d98a73-4233-4efe-9da8-3ada21348e95`：12 个版本中，导入版本 `8` 之后出现 `9:generated/manual_edit`、`10-11:generated/optimizer_candidate`，同样是一次导入正文被生成提示词接管；该 Profile 后来重新导入（版本 `12`，当前活动且为 `imported`）才恢复。
- 两个序列里 `generated/manual_edit` 都紧跟在导入版本之后，与“保存 Profile 触发整体 fingerprint 变化 → 重建为生成版本”的代码路径一致。

## 改动文件

- `datara/generators.py`：新增 `prompt_fingerprint()`，只对 `prompt_components(profile)` 这一提示词相关投影取哈希。
- `datara/optimizer.py`：重写 `ensure_prompt_version()` 的导入基线分支；新增 `_imported_baseline_is_current()` 与 `carry_imported_baseline()`；导入 `pydantic.ValidationError` 与 `prompt_fingerprint`。
- `tests/test_optimizer.py`：新增 3 个测试，并导入 `prompt_fingerprint`。
- `docs/design-docs/prompt-optimizer-design.md`：`origin` 枚举补充 `imported_prompt`、`imported_prompt_refresh`；“编辑器在优化器之外修改规则”一节写清生成基线与导入基线的不同处理，并明确“导入基线不会被生成提示词替换”。
- `README.md`、`ARCHITECTURE.md`、`ARCHITECTURE.zh-CN.md`：补充导入基线保留与提示词投影判定的说明。
- `docs/changes/2026-09-17-imported-prompt-preserved.zh-CN.md`：本文档。

## 变更内容

- **只有提示词相关内容变化才可能重建版本**：`prompt_fingerprint()` 基于 `prompt_components()`（表名、`prompt_order` 字段顺序、提示词标题、字段属性与 AI 提取规则）。挂样张、选参考文件、改库名、改示例值都不改变该投影，因此不会再触发版本重建。
- **导入基线按投影判定是否过期**：`_imported_baseline_is_current()` 用活动版本保存的 `profile_snapshot` 与当前 Profile 比较 `prompt_fingerprint`；快照缺失或形状过旧（`ValidationError`）时视为已变化，交给携带逻辑处理而不是直接生成。
- **导入正文原样携带**：`carry_imported_baseline()` 以 `imported_prompt_base` 为准（缺失时退回 `rendered_prompt` 中 `IMPORTED_OVERRIDE_MARKER` 之前的部分），把与该版本同步时不同的 AI 字段规则（含新增字段）重新追加为覆盖块，同时保留此前候选轮累积且仍然有效的覆盖；随后创建 `origin="imported_prompt_refresh"`、`prompt_source="imported"`、`parent_version_id=活动版本`、`lifecycle="active"` 的新版本。正文找不到时返回 `None`，调用方才回退到生成提示词。
- **生成基线行为不变**：非导入的活动版本仍按 Profile 重新渲染并创建 `manual_edit` 版本。

## 新增测试

- `tests/test_optimizer.py::test_prompt_fingerprint_ignores_workspace_state_but_tracks_prompt_content`：修改 `sample_ids`/`reference_ids`/`database_name` 后投影哈希不变；修改字段 `extraction` 规则后哈希变化。
- `tests/test_optimizer.py::test_attaching_a_sample_keeps_the_imported_prompt_baseline`：导入提示词后再保存带样张与参考文件的 Profile，活动版本 ID 与版本数量都不变，活动版本仍是 `prompt_source="imported"` 且 `rendered_prompt` 与导入正文逐字一致。
- `tests/test_optimizer.py::test_field_rule_edit_after_import_preserves_the_imported_base`：修改字段 AI 规则后新建 `imported_prompt_refresh` 版本，`imported_prompt_base` 与导入正文一致、`rendered_prompt` 以其开头且包含覆盖标记与新规则、`field_rule_overrides` 只含被改字段；重复打开版本历史不再增加版本；后续候选轮在该版本之上仍然保留导入正文与已有覆盖。

## 验证

- `uv run pytest -q` 全量通过（130 passed）。
- 只读扫描本地 `data/optimizer/prompt_versions` 与 `prompt_states`，确认既有历史版本未被改写（本次修复不回溯修改历史记录）。

## 行为变化与风险

- **版本数量减少**：工作区改动不再产生 `manual_edit` 版本，历史里不会再出现“导入版本后面紧跟生成版本”的覆盖序列。
- **导入正文只增不覆盖**：字段规则变化时以覆盖块形式追加，正文本身保持逐字不变。如果用户确实想改回生成提示词，需要显式重新生成或替换正文。
- **历史数据不回填**：已经被覆盖的导入正文无法从生成版本恢复；受影响 Profile 需要重新导入一次提示词（取证中的 `65d98a73` 已通过重新导入恢复）。
- `profile_snapshot` 形状过旧时按“已变化”处理并携带正文，最坏情况是多产生一个 `imported_prompt_refresh` 版本，不会丢失正文。

## 后续待办

- 若需要在版本历史里直观区分“用户导入原文”和“携带后的刷新版本”，可以在前端为 `imported_prompt_refresh` 增加标识与到父版本的 Diff 入口。
