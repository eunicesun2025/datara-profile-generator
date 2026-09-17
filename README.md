# Datara Profile Generator

架构与实现设计：

- [中文架构说明](ARCHITECTURE.zh-CN.md) / [English architecture](ARCHITECTURE.md)
- [中文生成器设计](docs/design-docs/generator-design.zh-CN.md) / [English generator design](docs/design-docs/generator-design.md)
- [Prompt Optimizer V1 implementation design](docs/design-docs/prompt-optimizer-design.md)

本地运行的 Datara Profile 编辑与生成工具。以统一字段定义为基准，生成 Field Mapping、SQL Server 建表脚本、中文提取提示词及 JSON 结构。

## 样张与文件分析（2026-09-08 更新）

- 在样张区拖入 PDF / PNG / JPEG；在参考文件区上传或拖入 XLSX、DOCX、TXT、MD、JSON、CSV 或 SQL。参考文件每份最多 8MB，合计文字最多 60,000 字符。DOCX 只读取正文和表格文字，不读取嵌入图片、页眉/页脚。
- 点击「AI 分析样张与文件」：发送当前样张全部页面、关联的参考文字和已有字段说明，建议单据类型、排除类型、详细识别规则、新增字段，以及已有 AI 字段的类型/规则修改。结果需逐项勾选后应用；不会自动更改 Manual/System 字段。
- 「补全类型与展示」无需 API Key：根据字段名/说明建议日期、金额、计数类型，编号保留 String；主表没有展示设置时为前五个 AI 字段分配 HeadDisplay。已有展示设置保留。AI 分析也会建议列表展示序号。
- 字段名在规范化时强制转为小写 snake_case，例如 `InvoiceDate` → `invoice_date`、`Total Amount` → `total_amount`；重名加后缀，System 名称冲突报错。常见中文字段有英文映射，其他中文名称使用稳定 Unicode 编码名称，原中文保留作说明，建议审核英文命名。
- 生成提示词增加按类型的处理说明和主表/明细定位规则；AI 建议的具体文档/字段规则经审核应用后一起进入最终提示词。样本值只用于 Mapping，不作为提示词固定答案。
- 界面使用深蓝色和提供的 Datara logo。左上角按钮收起导航，样张栏和卡片标题提供收起/展开。HeadDisplay 可直接在字段表格中编辑。

分析目前使用**当前选中的一份样张**（PDF 可多页）及全部已关联参考文件；新增子表仍需先在编辑器创建。AI 请求受模型设置的输出 token 上限限制，复杂字段清单可在设置中增加最大输出长度。结构检查与模拟测试不等于真实模型提取准确率验收。

Mac / Windows 和公司证书/代理配置请先阅读 [跨平台指南](docs/CROSS_PLATFORM.zh-CN.md)。Windows 可双击 `run.cmd`。

第一次使用请阅读 [从 GitHub 到 VS Code 中文指南](docs/VS_CODE_GUIDE.zh-CN.md)。已有 Mapping 直接导入；没有 Mapping 才新建空白 Profile。生成 SQL、提示词和 ZIP 不需要 API Key。

导入前显示结构检查与可选修复：同名表层级以首次定义为候选，子表空白父表只在唯一主表时补齐；字段归属仍需审核，原文件不修改。主表名称在创建时可填，之后通过“修改主表名称”编辑。编辑器提供返回列表按钮及浏览器前进/后退导航。

## 启动

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。当前开发环境已经安装依赖。

```bash
uv sync --frozen
uv run uvicorn datara.app:app --host 127.0.0.1 --port 8765
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。macOS 也可双击 `run.command`，或执行：

```bash
./run.command
```

已有 `.venv` 时，启动器直接使用该环境；首次使用时通过 uv 安装锁定依赖。服务只监听本机。停止服务：终端按 Ctrl+C。

## 第一次体验

1. 打开「支票示例」或「供应商发票示例」，无需 API 即可编辑、保存、预览和导出。
2. 审核字段来源、类型、必填、显示次序。点击每行 `⋯` 编辑提取规则、Choice、SQL 类型和审核状态。
3. 保存草稿，切换「输出预览」，下载 ZIP。
4. 上传样本 PDF / PNG / JPEG。PDF 最多 15 页，文件最多 20MB；图片在本机准备，不静默截页。
5. 在「模型设置」填写视觉端点 Base URL、准确模型 ID 和 API Key。
6. 点击「AI 分析样张与文件」，审核单据类型、提取规则和字段建议；或者点击「测试提取」查看实际 JSON 和校验结果。

示例 Profile 使用通用业务字段，未将用户生产样本、账户号码或真实提取值加入仓库。可从原有文件夹手动导入生产 Excel，并上传样本。

## 已实现

- 主表、直接关联的多张子表；自动补齐并保护必要 System 字段。
- 字段列表编辑、来源筛选、拖动排序、SQL 类型覆盖、主列表显示次序。
- 完整 Datara mapping 的 XLSX 导入；普通字段清单可选择工作表与列映射。
- 来源修正与导入说明；旧公司代码、关联 ID 和发票自动行号归为 System。
- 本地版本化保存、重新打开、过期写入冲突检测。
- 四份输出预览；导出已保存的同一版本，保留本地导出快照。
- 可配置的 OpenAI-compatible Chat Completions 视觉接口。
- AI 单据类型、提取规则、新增及已有 AI 字段修改建议，审核后应用；请求可取消。
- 测试结果保留原始响应、定义指纹、模型名称及校验结果；旧结果提示过期。
- 不需要模型连接也能粘贴 JSON，检查键名、来源、类型、日期与 Choice。
- Prompt Optimizer：可粘贴或上传 TXT/MD，把已发布提示词设为优化基线；可直接上传失败 PDF、填写所选字段的 Ground truth，并只优化勾选的 AI 字段规则。Regression 案例可选，添加后会作为防退化门禁。
- 应用按固定流程运行 baseline、Qwen 错误分析、候选提取、精确/归一化/可选语义比较、回归保护、提前停止与最终验证。错误分析会同时使用完整原提示词、字段定义、失败差异及有界失败页面；候选字段规则必须沿用原提示词的主要语言。
- 导入提示词的正文会原样保留，候选只附加所选字段的高优先级覆盖规则。Profile 中没有单独字段规则时，界面会明确提示以导入全文为准，不再错误显示成“原规则为空”。
- 导入基线不会被后续改动清除：挂样张、选参考文件、改库名等不影响提示词内容的改动不会重建版本；改动字段定义或提取规则时保留导入原文，只刷新其后的覆盖块并新建 `imported_prompt_refresh` 版本（来源仍为 imported），不会静默退回生成提示词。
- 优化器默认使用 3 轮/连续 2 轮无提升即停止；24 小时内相同端点、模型、提示词、JSON 结构和文档会复用结构有效的 Baseline 结果，诊断图片只在第一轮发送。最终验证始终真实重跑。
- 提示词版本历史、字段级 Diff、最佳版本选择、人工发布和前向回滚；发布和回滚都会创建新的 Profile revision，不会覆盖无关字段或改写历史。

## 固定规则

只有 `Source=AI` 的字段进入提示词和 AI JSON。Manual/System 只进入映射和数据库。所有表字段使用小写 snake_case。

每表 System：`id`、`file_id`、`created_at`、`updated_at`、`is_active`。主表额外包含 `interface_status`、`modified_by`；子表额外包含 `head_id`。

`id` 为自增主键。`created_at` 为 `datetime NOT NULL DEFAULT (SYSUTCDATETIME())`。子表外键级联删除。业务字段数据库允许 NULL，业务必填不转成 SQL NOT NULL。

无法识别的字段保留键并返回 null；日期为 YYYYMMDD；无明细为 []；不匹配文档为 {}。

## 模型设置

Base URL 是基础地址，例如 `https://your-endpoint.example/v1`，应用追加 `/chat/completions`。PDF 默认逐页转换为 JPEG 后发送，不要求端点原生支持 PDF。

首次启动默认 Base URL 为 `https://dashscope.aliyuncs.com/compatible-mode/v1`，模型 ID 为 `qwen3.8-max-0902`（按用户指定配置），API Key 留空。已有保存的连接配置优先，仍可在「模型设置」修改。

模型必须支持图片输入和 OpenAI-compatible 消息结构。默认 ID 的账号可用性与图片能力尚未通过真实接口验证，请以账号实际可调用的 ID 为准。「测试连接」验证文本请求，「测试提取」验证图片请求。

在页面填写的 API Key 只保留在当前服务进程中。若不想每次重启后重新填写，可复制项目根目录的 `.env.example` 为 `.env`，只填写一次：

```bash
cp .env.example .env
# 使用文本编辑器打开 .env，将 DATARA_API_KEY= 后面改为真实密钥
./run.command
```

Windows 可在项目目录执行 `copy .env.example .env`，编辑 `.env` 后双击或运行 `run.cmd`。

应用启动时会自动读取当前工作目录下的 `.env`。`.env` 已被 Git 忽略；操作系统、服务器、容器或 CI 中已经设置的环境变量优先，不会被 `.env` 覆盖。也可继续直接配置环境变量：

```bash
export DATARA_API_KEY='your-key'
export DATARA_DATA_DIR='/your/private/data-directory'
./run.command
```

不要把真实密钥提交到 Git，也不要把真实值写进 `.env.example`。设置持久化文件只包含端点、模型及超时等非密钥配置。

模型请求读取 HTTPS_PROXY / HTTP_PROXY / NO_PROXY 和 SSL_CERT_FILE / SSL_CERT_DIR，保持 TLS 验证；详见跨平台指南。模型的图片数量、容量与 token 限制由实际端点决定；应用另有 18MB 图片总量限制，会报错而非丢页。

排查模型调用缓慢或疑似卡死时，在 `.env` 中设置 `DATARA_LOG_LEVEL=INFO` 后重启，日志会输出每次模型请求的 `model_request_started`（模型、图片数、字节数、字符数、超时）与 `model_request_finished`（实际耗时）。有 `finished` 行说明请求已返回，只有 `started` 而长时间没有 `finished` 说明连接被代理或网络挂住。默认 `WARNING` 不输出这些行。注意 uvicorn 的 `--log-level` 参数对本应用日志无效，它只影响 uvicorn 自身的日志器。

## 本地数据

默认在工作目录 `data/` 下保存，已加入 `.gitignore`：

```text
data/
  profiles/       Profile JSON 草稿及版本
  samples/        原始文档、页面图像和元数据
  imports/        用户上传的 Excel
  references/     AI 分析参考文字及文件名（本地 JSON）
  tests/          模型任务、响应与校验记录
  exports/        ZIP 和对应 Profile 快照
  connection.json  不含 API Key 的连接配置
  optimizer/        测试案例、Ground truth、优化任务、提取证据及提示词版本
```

修改数据目录请通过 `DATARA_DATA_DIR`。本版本不自动清理样本与测试记录，可在停止服务后备份或删除对应数据目录。多用户和多副本部署前需加入认证与共享存储；当前使用单进程本地文件存储。

## 仓库结构

```text
datara/
  domain.py       统一模型、System 模板和校验
  generators.py   mapping / SQL / prompt / JSON / ZIP
  importer.py     Excel 读取和导入修正
  media.py        PDF 与图片页面准备
  provider.py     可配置视觉模型适配器和完整分析建议解析
  references.py   参考文件文字解析与容量检查
  storage.py      原子保存、版本冲突检测
  app.py          本地 API、任务和页面服务
  static/         原生 HTML / CSS / JavaScript 界面
tests/            自动化测试
docs/design.md    已确认的完整方案
```

最小版本将 SQL 覆盖保存为 `sql_type`（如 `decimal(23,2)`），并在后端按白名单解析验证，不接受任意 SQL 片段。它对应方案中的 SQL 类型、长度和精度配置，后续可迁移为分立属性。映射 XLSX 本身不包含提取规则和 SQL 覆盖，继续编辑应打开 Profile 草稿。

## 验证

```bash
uv run pytest -q
```

测试覆盖跨产物字段一致性、System 隔离、SQL 主键/外键/默认值、日期与金额验证、空值规则、Excel 导入、前导零保留、公式文本安全、版本冲突、上传预览及模型任务流程。

自动测试中的模型使用隔离的模拟响应。2026-09-08 另以用户配置的 DashScope 视觉模型，对电费账单 JPEG 与 XLSX 参考文件完成一次人工端到端验收：字段分析成功，随后提取结果通过结构校验；这不替代供应商可用性监控或更多版式回归。系统仍未连接真实 Datara 或 SQL Server 执行建表/导入。

## 第一版边界

- 支持核心本地闭环；普通清单的字段类型/来源需要审核。
- AI 分析可建议单据规则及已有 AI 字段修改；先建立子表后再建议其字段。
- SQL 只生成 CREATE TABLE，不自动执行、不迁移已有生产数据库。
- 系统查询、公司代码映射、自动行号等业务填充由 Datara 实现。
- 自由文本识别规则做常见冲突检测，但仍需人工审核其业务含义。
- 暂未实现 PDF 框选、深层子表、用户登录、模型供应商非兼容协议、多实例部署和持久化任务队列。

架构保持端点配置、存储与生成器分离，可后续容器化；目前不包含未经验证的部署配置。

### 优化耗时与模型设置

- 已填写 `observed_output` 的失败案例：如果该输出覆盖全部 Ground truth 字段且存在所选字段错误，直接将其作为历史基线，省去重复提取。候选评估和最终复验仍调用模型。历史结果不作为真实模型缓存复用。
- `OptimizationSettings.extraction_concurrency` 默认 2，可通过优化任务 API 设为 1–4。只并发独立样本；优化轮次仍有先后依赖。失败或取消会取消同批未完成请求。
- 模型超时同时约束完整响应时间；优化请求的重试与退避共用该次请求的时间预算。HTTPX 的分块读取超时不再是唯一限制。
- 连接建立另有固定 15 秒连接超时，与上面的模型超时是两个独立预算，且无对应配置项。两者刻意分开上报：连接超时上报为「无法连接模型端点：建立连接超过 15 秒连接超时…」并保留一次重试（约 1 秒退避）；响应超时才上报为「模型请求超时，可调整超时设置后重试」且不重试。因此调高设置里的超时无法解决连接阶段失败，企业代理握手抖动会被自动重试一次。
- 上述重试预算只在 `datara/provider.py` 的 `classify_transient()` 中定义一次，**测试提取与 AI 分析任务同样适用**：`run_job` 通过 `completion_retrying()` 调用模型，连接失败自动重试 1 次、网关 429/5xx 重试 2 次、响应超时不重试。此前这些交互任务只调用模型一次，一次 15 秒握手抖动就会让整个测试提取失败。
- 模型响应按严格 JSON 解析（重复键、NaN 等一律拒绝），但容忍整体包裹单个 Markdown 代码围栏的响应（```json … ```）：qwen3.8-max 等视觉模型偶发在提示词已写明「只返回 JSON 对象，不要 Markdown」时仍附加围栏。此前这会让完全正确的提取结果被判为空输出（`Expecting value: line 1 column 1 (char 0)`），optimizer 把实际已修复失败样本的候选轮误判为「无改进」，连续两轮后 `no_improvement` 早停；「测试提取」也会显示解析错误和空结果。现在 optimizer 提取/候选分析、「测试提取」与字段建议草稿统一走 `model_json()`（仅当围栏包裹整个响应时剥离，围栏内仍执行全部严格规则）；Ground truth 与手工结果校验等用户输入仍走 `strict_json()`，不接受围栏。基线缓存记录携带 `response_parser_version`，旧解析规则产生的缓存（含被围栏污染的空结果）自动失效。
- 视觉提取可能需要数十秒到数分钟，且请求是非流式的，期间没有增量输出。测试提取页的「处理中…」旁会显示实时等待计时（`已等待 N 秒` / `已等待 M 分 N 秒`），**计时器走动即表示仍在等待模型，不是卡死**；请勿在计时器仍在走动时取消。
- 百炼官方 DashScope 接口上的 `qwen3.8-max*` / `qwen3.8-flash*` 默认发送 `enable_thinking=false`，减少额外思考。其他模型或企业网关默认不发送此专用参数。可通过设置 API 的 `enable_thinking` 显式设为 `true` / `false`，`null` 恢复上述自动策略。请用业务样本核验切换后的准确率。参数依据：[百炼视觉推理文档](https://www.alibabacloud.com/help/en/model-studio/visual-reasoning)。
- 开启 `datara.provider` 和 `datara.optimizer` 的 INFO 日志可看到每次调用的模型、输入文字长度、图片数量/字节数、开始及耗时；不会记录 API Key 或请求正文。
- 模拟接口测试只验证流程、并发及超时行为，不代表真实模型响应速度或识别准确率。
