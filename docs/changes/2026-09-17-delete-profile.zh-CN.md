# 新增 Profile 删除入口：级联清理独有记录、保留共享上传、任务运行时拒绝

日期：2026-09-17

触发问题：界面上没有删除按钮，用户无法移除不再需要的 Profile。此前 `ARCHITECTURE.md` 明确写着“没有 Profile 或上传对象的删除接口”，唯一手段是停止服务后手工删除 `data/` 目录，容易误删共享样张或留下孤立记录。

受影响范围：`data/` 下该 Profile 独有的记录与其导出文件；共享工作区对象（样张、参考文件、导入的 Excel）不受影响。

测试结果：全量 `uv run pytest -q` 通过（130 passed），新增 2 个测试覆盖级联删除/共享上传保留与三种拒绝路径（404、400、409）。

## 设计取舍

- **级联范围**：Profile 是 canonical 定义，它的派生记录只对它有意义，因此一起删除，避免留下指向不存在 Profile 的孤立数据（提示词版本、优化任务、提取证据、测试记录、导出快照）。
- **保留共享上传**：`samples`、`references`、`imports` 是工作区级对象，可被多个 Profile 引用（`sample_ids`/`reference_ids`），删除后不可恢复，因此刻意保留。孤立文件的清理留待后续单独入口。
- **运行中拒绝**：模型任务（内存 `jobs`）与优化 run 一旦仍在写记录，删除会产生“写入已消失的 Profile”的孤立文件，所以先检查再删除，返回 `409`；检查失败时不删除任何数据。
- **不引入数据库**：仍是本地文件存储，删除即 `unlink`，通过 `Store` 的进程内可重入写锁串行化，避免与保存/优化写入交错。

## 改动文件

- `datara/storage.py`：新增模块级常量 `PROFILE_OWNED_FOLDERS` 与 `Store.delete_profile(identity)`，返回每个目录删除的文件数。
- `datara/app.py`：新增 `DELETE /api/profiles/{identity}`，含 live 任务/优化 run 守卫。
- `datara/static/app.js`：Profile 卡片外层包 `.profile-card-wrap`，新增「删除」按钮与 `delete-profile`、`confirm-delete-profile` 两个 action（确认弹窗 + 删除 + 列表刷新 + 清理明细 toast）。
- `datara/static/style.css`：`.profile-card-wrap`、`.profile-card-delete`（hover/focus 显示，触屏常显）。
- `tests/test_app.py`：新增 2 个测试。
- `README.md`、`ARCHITECTURE.md`、`ARCHITECTURE.zh-CN.md`：更新已实现清单、本地数据说明、模块职责、API 表、删除语义段落与需求映射（A-26 / A-16）。
- `docs/changes/2026-09-17-delete-profile.zh-CN.md`：本文档。

## 变更内容

- **`Store.delete_profile(identity)`**：在写锁内先读取该 Profile 的 run/case/version/iteration 标识，再按 `PROFILE_OWNED_FOLDERS` 逐个目录删除属于它的文件——`profiles`、`tests`（模型任务记录）、`exports`（JSON 元数据与 ZIP）、`optimizer/prompt_states`、`optimizer/prompt_versions`、`optimizer/test_cases`、`optimizer/ground_truth`、`optimizer/runs`、`optimizer/iterations`、`optimizer/extractions`、`optimizer/promotions`。返回 `dict[folder, count]` 供界面汇总。单条记录 JSON 损坏不阻断删除；`identity` 仍走既有 `Store.path()` 的标识符安全校验。
- **`DELETE /api/profiles/{identity}`**：先 `store.load(identity)`（未知 ID → 404，不安全 ID → 400，由既有异常映射处理）；再检查内存 `jobs` 中 `status="running"` 且属于该 Profile 的任务，以及 `optimizer/runs` 中状态属于 `queued/baselining/optimizing/validating` 的 run；命中任一则 `409` 并提示等待完成或先取消；否则删除并返回 `{"ok", "id", "name", "removed"}`。
- **界面**：卡片 hover 显示「删除」；确认弹窗写明“此操作不可撤销”、列出会删除的内容与保留的样张/参考文件、显示当前版本与表/字段数量；删除成功后关闭弹窗、重新拉取 Profile 列表并重绘；若删除的正是当前打开的 Profile，则停止轮询并清空编辑器状态。toast 汇总清理明细（提示词版本、优化任务、优化案例、测试记录、导出文件）。

## 新增测试

- `tests/test_app.py::test_delete_profile_removes_profile_and_derived_records`：以发票示例 Profile 为基础，导入一个提示词版本、挂上共享样张、执行一次导出并写入一条 `tests/job-1.json` 任务记录；删除后断言 `removed` 计数（`profiles==1`、`optimizer/prompt_versions>=2`、`optimizer/prompt_states==1`、`tests==1`、`exports==1`）、Profile JSON 与导出 ZIP 已消失、样张 `meta.json` 仍在、列表不再包含该 Profile、`GET /api/profiles/{id}` 与 `GET /api/profiles/{id}/prompt-versions` 均为 404。
- `tests/test_app.py::test_delete_profile_guards_unknown_unsafe_and_running`：未知 ID → 404；含空格的 URL 编码 ID（`bad%20id`）→ 400；该 Profile 有 `status="optimizing"` 的优化 run 时 → 409 且 Profile 文件仍在；把 run 改成 `completed` 后再删 → 200、`removed["optimizer/runs"]==1`、run 文件已删除。运行中的内存模型任务走同一守卫分支。

## 验证

- `uv run pytest -q` 全量通过（130 passed）；`node --check datara/static/app.js` 通过。
- 用真实 `data/` 的**副本**跑端到端删除（未触碰生产数据）：删除 `VendorInvoiceTW06_PRD` 清理了 12 个提示词版本、1 个 prompt state、2 个优化案例、2 份 ground truth、3 个 run、6 个 iteration、9 条 extraction、3 条模型任务记录与 1 个 Profile；`samples 82→82`、`references 6→6`、`imports 5→5`、`exports 2→2`（其余 Profile 的导出）全部不变；其余 7 个 Profile 及其版本记录完好，被删 Profile 返回 404。临时副本已清理。

## 行为变化与风险

- **删除不可撤销**：本地文件直接 `unlink`，没有回收站或备份。界面已用红色确认弹窗二次确认并明示范围。
- **无认证**：与既有接口一致，本地单进程使用；多用户部署前必须先加认证，否则任何人都能删除他人 Profile。
- **共享上传会留下孤立文件**：删除 Profile 不会清理只被它引用的样张/参考文件，磁盘占用不会立刻下降；这是有意保守的选择。
- **运行中任务的竞态**：守卫检查与删除之间没有跨进程锁，若另一个进程恰好启动新任务仍可能写入已删除的 Profile 记录；单进程本地部署下不会出现。
- **前端列表过期**：确认删除时若列表已被其他标签页改动，后端仍以 `identity` 为准删除；action 在找不到对应卡片数据时会提示返回列表刷新，而不是静默失败。

## 后续待办

- 增加“清理不再被任何 Profile 引用的样张/参考文件/导入 Excel”的独立入口（含预览与计数），解决孤立上传文件。
- 若引入认证，删除接口需要加权限校验与审计日志（谁在何时删了哪个 Profile）。
