# Datara Profile Generator

本地运行的 Datara Profile 编辑与生成工具。以统一字段定义为基准，生成 Field Mapping、SQL Server 建表脚本、中文提取提示词及 JSON 结构。

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
6. 点击「AI 建议字段」，勾选要加入的建议；或者点击「测试提取」查看实际 JSON 和校验结果。

示例 Profile 使用通用业务字段，未将用户生产样本、账户号码或真实提取值加入仓库。可从原有文件夹手动导入生产 Excel，并上传样本。

## 已实现

- 主表、直接关联的多张子表；自动补齐并保护必要 System 字段。
- 字段列表编辑、来源筛选、拖动排序、SQL 类型覆盖、主列表显示次序。
- 完整 Datara mapping 的 XLSX 导入；普通字段清单可选择工作表与列映射。
- 来源修正与导入说明；旧公司代码、关联 ID 和发票自动行号归为 System。
- 本地版本化保存、重新打开、过期写入冲突检测。
- 四份输出预览；导出已保存的同一版本，保留本地导出快照。
- 可配置的 OpenAI-compatible Chat Completions 视觉接口。
- AI 新增字段建议，不自动覆盖当前字段；请求可取消。
- 测试结果保留原始响应、定义指纹、模型名称及校验结果；旧结果提示过期。
- 不需要模型连接也能粘贴 JSON，检查键名、来源、类型、日期与 Choice。

## 固定规则

只有 `Source=AI` 的字段进入提示词和 AI JSON。Manual/System 只进入映射和数据库。所有表字段使用小写 snake_case。

每表 System：`id`、`file_id`、`created_at`、`updated_at`、`is_active`。主表额外包含 `interface_status`、`modified_by`；子表额外包含 `head_id`。

`id` 为自增主键。`created_at` 为 `datetime NOT NULL DEFAULT (SYSUTCDATETIME())`。子表外键级联删除。业务字段数据库允许 NULL，业务必填不转成 SQL NOT NULL。

无法识别的字段保留键并返回 null；日期为 YYYYMMDD；无明细为 []；不匹配文档为 {}。

## 模型设置

Base URL 是基础地址，例如 `https://your-endpoint.example/v1`，应用追加 `/chat/completions`。PDF 默认逐页转换为 JPEG 后发送，不要求端点原生支持 PDF。

首次启动默认 Base URL 为 `https://dashscope.aliyuncs.com/compatible-mode/v1`，模型 ID 为 `qwen3.8-max-0902`（按用户指定配置），API Key 留空。已有保存的连接配置优先，仍可在「模型设置」修改。

模型必须支持图片输入和 OpenAI-compatible 消息结构。默认 ID 的账号可用性与图片能力尚未通过真实接口验证，请以账号实际可调用的 ID 为准。「测试连接」验证文本请求，「测试提取」验证图片请求。

API Key 只保留在当前服务进程中，重启后需重新填写。也可在启动前配置环境变量：

```bash
export DATARA_API_KEY='your-key'
export DATARA_DATA_DIR='/your/private/data-directory'
./run.command
```

不要把真实密钥提交到 Git。`.env.example` 仅是环境变量说明，应用不会隐式读取 `.env` 文件。设置持久化文件只包含端点、模型及超时等非密钥配置。

该版本请求不使用系统代理；需要通过企业代理连接的环境需增加明确的代理配置。模型的图片数量、容量与 token 限制由实际端点决定；应用另有 18MB 图片总量限制，会报错而非丢页。

## 本地数据

默认在工作目录 `data/` 下保存，已加入 `.gitignore`：

```text
data/
  profiles/       Profile JSON 草稿及版本
  samples/        原始文档、页面图像和元数据
  imports/        用户上传的 Excel
  tests/          模型任务、响应与校验记录
  exports/        ZIP 和对应 Profile 快照
  connection.json  不含 API Key 的连接配置
```

修改数据目录请通过 `DATARA_DATA_DIR`。本版本不自动清理样本与测试记录，可在停止服务后备份或删除对应数据目录。多用户和多副本部署前需加入认证与共享存储；当前使用单进程本地文件存储。

## 仓库结构

```text
datara/
  domain.py       统一模型、System 模板和校验
  generators.py   mapping / SQL / prompt / JSON / ZIP
  importer.py     Excel 读取和导入修正
  media.py        PDF 与图片页面准备
  provider.py     可配置视觉模型适配器
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

测试中的模型使用隔离的模拟响应。尚未使用用户真实 Qwen/Kimi 密钥进行外部端点验收，也没有连接真实 Datara 或 SQL Server 执行建表/导入。运行验证与静态/模拟测试需区分。

## 第一版边界

- 支持核心本地闭环；普通清单的字段类型/来源需要审核。
- AI 草稿当前仅向已建表建议新增字段；先建立子表后再建议其字段。
- SQL 只生成 CREATE TABLE，不自动执行、不迁移已有生产数据库。
- 系统查询、公司代码映射、自动行号等业务填充由 Datara 实现。
- 自由文本识别规则做常见冲突检测，但仍需人工审核其业务含义。
- 暂未实现 PDF 框选、深层子表、用户登录、模型供应商非兼容协议、多实例部署和持久化任务队列。

架构保持端点配置、存储与生成器分离，可后续容器化；目前不包含未经验证的部署配置。
