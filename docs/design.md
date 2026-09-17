# Datara Profile Generator 第一版方案

版本：v1.0 · 日期：2026-09-05 · 状态：设计方案，未开始应用开发

## 1. 目标与设计原则

用一份用户审核后的字段定义，统一生成 Datara Field Mapping、SQL Server 建表脚本、AI 提取提示词及 JSON 结构，解决当前三个文件分别手工修改造成的不一致。

**Field Mapping 是业务字段的唯一基准。** 应用内部保存其完整结构，并补充生成 SQL 和提示词所需的信息。所有导出文件均来自同一个已保存版本，不互相反向猜测、不独立手工维护。

核心关系：

```text
PDF / 图片 + 用户字段清单 / Excel
                  ↓
             AI 建议字段草稿
                  ↓
        用户审核字段、来源、类型、规则
                  ↓
          统一 Profile 定义与校验
                  ↓
       预览 → 测试提取 → 修订 → 导出 ZIP
                  ├─ Field Mapping.xlsx（全部字段）
                  ├─ create_tables.sql（全部字段）
                  ├─ extraction_prompt.txt（仅 Source=AI）
                  └─ output_structure.json（仅 Source=AI）
```

AI 负责理解和建议，确定性代码负责结构、校验和生成。导出时不再调用模型。同一 Profile 版本的预览内容与下载内容保持一致。

## 2. 已确认的规则

本节内容来自逐项确认，优先于生产样本中不一致的历史写法。

### 2.1 字段与来源

- Field Mapping 字段集合与数据库列集合一致，包括必要的 System 字段。
- `Source` 使用 `AI`、`Manual`、`System`。
- 只有 `Source=AI` 的字段进入最终提示词及其 output JSON 结构。
- 公司代码、系统查询值及由 Datara 填充的计算值归为 `System`。2026-09-17 用户确认：`AI_Invoice_Detail.item` 由模型按数组顺序生成字符串行号，归为 `AI`，不得按字段名强制改为 `System`。
- `Manual` 字段由人工填写，不进入 AI 提取内容。
- 字段名强制全小写 snake_case；Field Mapping、SQL、提示词和 JSON 的字段名及大小写完全一致。
- 数据库表名默认使用 `AI_` 前缀，允许编辑，不强制小写。所有产物仍使用同一准确表名。
- 第一版支持一张主表，以及零张或多张直接关联主表的子表；不支持更深层级。

### 2.2 必要的 System 字段

| 字段 | 主表 | 子表 | SQL 类型 | 已确认行为 |
|---|---|---|---|---|
| id | 必须 | 必须 | int | IDENTITY(1,1)，主键 |
| file_id | 必须 | 必须 | int | Datara 填充文件关联值 |
| created_at | 必须 | 必须 | datetime | NOT NULL，DEFAULT (SYSUTCDATETIME()) |
| updated_at | 必须 | 必须 | datetime | 更新时间 |
| is_active | 必须 | 必须 | bit | 有效状态 |
| interface_status | 必须 | 不默认添加 | int | 接口状态 |
| modified_by | 必须 | 不默认添加 | nvarchar(50) | 修改人 |
| head_id | 不添加 | 必须 | int | 外键关联主表 id，ON DELETE CASCADE |

这些字段自动进入 Field Mapping 和 SQL，不进入提示词或 AI JSON。每张表中只添加一次。不得由 AI 删除、重命名或改变来源。

`modified_by` 和 `interface_status` 仅作为主表默认字段；如果未来有明确业务需求，可另行定义子表扩展字段，不将扩展需求误当作现有默认规范。

### 2.3 业务字段默认类型

| Field Mapping DataType | 默认 SQL 类型 | AI JSON 非空值类型 |
|---|---|---|
| String | nvarchar(200) | string |
| Integer | int | integer |
| Decimal | decimal(18,2) | number |
| Date | date | string，YYYYMMDD |
| Boolean | bit | boolean |
| Choice | nvarchar(50) | string，来自声明的选项 |

SQL 类型、长度及精度允许在编辑界面调整。JSON 类型由业务类型决定，不因 SQL 存储类型调整而自动变化。

业务字段在 SQL 中允许 `NULL`。`IsRequired` 仅用于业务必填校验，不转换为业务列的 `NOT NULL`。例如必填金额无法识别时仍可返回 `null`，由用户后续补充。

支票号码、银行代码、账号、PO/GR 编号等标识符建议使用 String，保留前导零；这是字段建议，不以“全部字符都是数字”为由强制改成 Integer。

### 2.4 提取结果

- 不符合当前 Profile 单据类型：返回 `{}`。
- 符合单据类型：主表为对象，每张子表为对象数组，表名作为顶层键。
- 没有识别到某张子表的记录：该子表返回 `[]`。
- 无法识别某个 AI 字段：保留键并返回 `null`，包括必填字段。
- 不自动补 `0`、空字符串、当前日期、示例值或默认货币。明确配置为 AI 的行号字段按其生成规则输出。
- Date 输出为 `YYYYMMDD` 字符串，例如 `"20260522"`。
- 输出只包含 JSON，不含解释或额外字段。

### 2.5 界面与交付

- 界面和提取提示词默认简体中文；表名、字段名使用英文。
- 上传优先 PDF，同时兼容图片。
- 第一版通过 AI 推荐字段、勾选、添加、编辑或导入 Excel 选择字段；PDF 框选放到下一版本。
- `HeadDisplay` 是主列表展示次序；为空则不在主列表展示，但仍可在详情或编辑页显示。
- `FieldOrder` 是详情或编辑页字段顺序，每张表从 1 连续编号，支持拖动调整。
- 支持测试提取，展示实际 JSON 并校验字段、类型和格式。
- 支持本地保存、重新打开和修改 Profile 草稿。
- 设置页提供 API 地址、API Key、模型名称，支持外部及公司内部端点。
- 最终 ZIP 包含四个文件：`.xlsx`、`.sql`、`.txt`、`.json`；提示词内的 JSON 结构与独立 JSON 文件相同。

## 3. 样本结论与历史差异处理

### 3.1 样本之间的关系

Cheque 是单表 Profile。生产 mapping 中有 34 个字段：8 个 AI、17 个 Manual、9 个 System；8 个 AI 字段与预期 JSON 字段一致。

Vendor Invoice 是主表加两个子表：`AI_Invoice_Head`、`AI_Invoice_Detail`、`AI_POGR`。子表在 JSON 中为同级数组，通过数据库中的 `head_id` 关联主表。

PDF 是值和布局的证据；预期 JSON 是一份历史认可结果；mapping 是字段注册和展示定义；SQL 是物理存储；prompt 是识别与解释规则。不能从预期 JSON 中没有出现某字段，推断该字段应该从 mapping 删除。

### 3.2 新工具如何解决已发现的不一致

| 历史问题 | 第一版处理 |
|---|---|
| SQL 多出 mapping 没有的列 | 不自动复制；确需保留的列必须先进入统一字段定义 |
| Vendor Invoice 漏提供 AI_POGR SQL | 从 mapping 的子表字段及统一 System 规则生成，无需补旧脚本 |
| AI_POGR 的 file_id/head_id 曾标为 AI | 识别为保留的 System 字段，展示导入修正记录并统一来源 |
| 公司代码标为 AI | 改为 System，并记录由 Datara 填充；item 保留 Mapping 指定的 Source，当前用户需求为 AI |
| mapping 为 AI，但旧 prompt 没有该字段 | 新提示词以当前审核后的 Source=AI 字段集合为准 |
| 旧 prompt 要求 current_date | 未在 mapping 中定义的字段不得输出；系统日期不交给 AI |
| 旧 SQL 日期、金额大量用字符串 | 新字段按确认的默认类型生成；导入旧存储配置作为显式覆盖审核 |
| 重复 FieldOrder | 保留导入行相对次序，再连续编号，并展示变更 |
| Choice 示例不属于选项 | 标记为待修正，不把示例自动当作有效选项 |
| 旧 prompt 缺失值默认 0/HKD | 新规则统一 null，不继承旧默认行为 |
| 旧 prompt 请求额外 MICR 原文但无定义字段 | 不保留无字段对应的输出要求 |
| created_at_cn_local 等 SQL 辅助列 | 不自动生成；需要时先显式纳入 mapping 并设计存储规则 |

`AI_POGR` 可按 `po_number`、`gr_number` 两个业务字段，加子表 System 字段生成。编号建议 String；样本中的 JSON 数字表示不应成为编号必须用数字的规范。

旧文件用于验证兼容性和识别业务规则，不能作为新输出逐字一致的目标。明确要求模型生成的 item 行号属于有效 AI 输出。默认税额或默认数量需依据审核后的规则形成测试基准，保留原文件不覆盖。

## 4. 用户流程

### 4.1 创建或打开 Profile

填写 Profile 名称、业务场景说明、接受及排除的单据类型。新建时添加主表；按需添加子表。也可打开本地草稿。

### 4.2 上传样本和导入字段

上传 PDF 或图片，在页面预览。可输入业务字段清单，或导入 Excel。

Excel 有两种入口：

1. 现有 Datara mapping：按完整的 11 列识别并导入。
2. 普通业务字段清单：用户选择字段名、说明、类型等对应列；缺少的信息由 AI 建议并留待审核。

普通 Excel 列名和工作表不固定，不能只依靠文件名或第一张表自动决定。类型和来源不明确时标记待审核，不擅自将所有列设为 AI。

PDF 先在本地逐页渲染，供预览及视觉模型使用；已有文本层可辅助理解，但扫描件必须保留图像输入。超过端点容量时明确提示缩小范围或分批，不静默丢页。

### 4.3 AI 生成草稿

AI 根据文档、用户选定范围及可用字段清单，建议字段名称、类型、主子表归属、描述、提取线索和示例值。可以建议额外字段，但默认不作为已选字段自动导出。

建议附带样本页码和简短依据。没有依据时标注不确定。AI 不决定强制系统字段，也不直接生成最终 SQL 或 Excel。

重新生成草稿时展示变更供选择，不覆盖已审核字段。字段名变更采用稳定内部 ID 追踪，避免误创建重复字段。

### 4.4 字段审核与编辑

按表分组展示字段，常用列包括：名称、中文说明、来源、业务类型、必填、选项、列表次序、样本值、提取规则。

高级区域编辑 SQL 类型、长度、精度，以及 System 字段的填充责任说明。必要 System 字段可见，但核心属性锁定。

拖动字段自动更新 FieldOrder。修改 Source 后立即更新 AI JSON 预览；改为 System/Manual 后，相关提取规则不再进入模型请求。

### 4.5 预览与测试提取

预览四份输出；所有预览来自同一份当前定义。修改字段后，提示旧测试结果已不对应当前版本。

“测试提取”使用当前生成的最终提示词与所选样本调用配置模型，展示：

- 原始响应与可解析 JSON；
- 缺少、额外或大小写不一致的字段；
- 类型、真实日期、Choice 取值等错误；
- 必填字段为 null 的业务待补项；
- 文档不匹配或无明细的正常结果。

结果结构有效不代表提取值一定正确。用户对照样本确认内容；如有审核后的预期 JSON，可做字段级比较。

### 4.6 保存与导出

草稿按版本保存。导出前完成结构校验，生成不可变导出快照。ZIP 四份文件使用相同 Profile 版本。

建议允许未执行模型测试时导出，但清楚显示“尚未测试”；结构错误则阻止导出。测试不是偷偷修复字段或改变提示词的入口。

## 5. 统一内部数据模型

### 5.1 设计取舍

不采用互相竞争的 `value_origin` 与 `Source`。**`field.source` 是唯一提取开关**，直接对应 mapping 的 Source。额外 `population` 仅说明 System/Manual 的值由谁填写，不改变 prompt 是否包含字段。

内部模型是对 Field Mapping 的扩展：现有 Excel 缺少 SQL 长度、精度和完整提取规则，因此 Excel 单独导入不能完整恢复整个 Profile。继续编辑应打开本地 Profile 草稿。

### 5.2 对象结构

| 对象 | 核心属性 |
|---|---|
| Profile | id、name、schema_version、convention_version、revision、description、language、document_rules、database、tables、sample_refs |
| Table | id、name、role(head/detail)、parent_table_id、fields |
| Field | id、name、description、source、data_type、is_required、choice_values、sample_data、head_display、field_order、sql、extraction、population、review |
| SQL 配置 | type、length、precision、scale；身份列、主键、外键及系统默认值由已确认模板生成 |
| Extraction 配置 | 字段含义、寻找位置、区分规则、标准化规则；仅 AI 字段参与提示词 |
| Population 配置 | 填充方与说明，例如 Datara 查询公司代码、按明细顺序生成行号 |
| Review | imported/ai_suggested/user_confirmed、来源文件/单元格/页码、待确认项 |
| ModelConnection | id、label、adapter、base_url、model、credential_ref、capabilities、timeouts |
| TestRun | profile_revision、prompt_hash、connection_id、实际 model、sample_refs、原始响应、校验结果、时间 |

TableLevel、ForeignKeyField、JSON 对象/数组形式均从 table.role 与 parent_table_id 计算，不另外保存一套可冲突的关系。

字段集合以 `fields` 为准；JSON 键等于字段名，不支持额外别名。SQL 类型覆盖显式保存，避免调整业务类型时悄悄丢失覆盖值。

简化示例：

```json
{
  "schema_version": "1.0",
  "convention_version": "datara-v1",
  "name": "Cheque Payment",
  "revision": 1,
  "database": {"schema": "dbo", "name": null},
  "tables": [
    {
      "id": "table-cheque",
      "name": "AI_ChequePay",
      "role": "head",
      "parent_table_id": null,
      "fields": [
        {
          "id": "field-cheque-date",
          "name": "cheque_date",
          "description": "支票票面日期",
          "source": "AI",
          "data_type": "Date",
          "is_required": true,
          "choice_values": [],
          "head_display": null,
          "field_order": 1,
          "sample_data": "20251001",
          "sql": {"type": "date"},
          "extraction": {"instructions": "读取支票票面日期，按日/月/年标签理解。"},
          "population": null,
          "review": {"status": "user_confirmed"}
        }
      ]
    }
  ]
}
```

示例仅展示一个业务字段；完整保存数据必须包含所有自动补入的 System 字段。样本值只用于审核或 mapping 的 SampleData，不自动注入最终提示词作为答案。

## 6. 确定性生成规则

### 6.1 Field Mapping

保持生产格式和列顺序：

```text
TableName, TableLevel, ForeignKeyField, ColumnName, SampleData,
DataType, ChoiceValues, IsRequired, Source, HeadDisplay, FieldOrder
```

- 主表 TableLevel=1，ForeignKeyField 空。
- 子表 TableLevel=2，ForeignKeyField 填父表名称。这是两个 invoice mapping 样本中的格式，作为第一版兼容约定。
- Source 直接导出 field.source；不使用另一个“加入 AI”复选框。
- ChoiceValues 内部为数组，导出为逗号分隔文本。
- FieldOrder 按表排序后连续编号。
- 系统 datetime 在 mapping 中仍映射为 `Date`，SQL 保留 `datetime`。
- Excel 数字型标识符不能恢复已经丢失的前导零；导入时提示用户核对。

### 6.2 SQL

- 每张映射表生成一个 CREATE TABLE，全部字段一次且仅一次出现。
- 先主表后子表；显式生成 id 主键、created_at 默认约束及 head_id 外键。
- 业务列全部允许 NULL；不从 IsRequired 推导 NOT NULL。
- 只允许白名单类型及合法参数；表名和列名校验后正确引用。
- 不生成 DROP、自动迁移或执行数据库操作。
- 不添加 mapping 以外的辅助列或历史常量。
- head_id、file_id 等的值由运行时 Datara 填充；建表脚本不执行这类业务逻辑。
- 重复执行遇到已有表应明确失败，不静默认为现有表结构正确。

### 6.3 Prompt 与 JSON

先由代码得到 AI 字段投影，再从该投影生成 JSON 结构与字段说明。模型收到的最终提取请求只使用这份投影，不附带完整 mapping 或非 AI 字段定义。

提示词顺序：任务及单据判定 → AI JSON 结构 → AI 字段解释 → 已审核的单据/明细识别规则 → null/日期/空数组/JSON-only 通用要求。

用户编辑字段级或文档级规则，JSON 结构区由程序生成，不允许单独修改。检测自由文本中与全局规则冲突的要求，例如“缺失金额返回 0”或“额外输出 current_date”，要求修正后导出。

独立 `.json` 是结构示例，不是测试结果，也不是 JSON Schema。推荐用 null 表示未知标量，用一个全 null 对象展示子表字段：

```json
{
  "AI_Invoice_Head": {
    "invoice_number": null,
    "invoice_date": null,
    "invoice_amount": null
  },
  "AI_POGR": [
    {"po_number": null, "gr_number": null}
  ]
}
```

提示词明确该对象只展示结构，不能照抄为记录；实际无明细必须返回 `[]`。JSON Schema 在应用内部另行生成用于校验，不额外加入本次约定的四文件 ZIP。

对于没有 AI 字段的表，建议不进入 AI JSON 投影；它仍存在于 mapping 和 SQL。若整个 Profile 没有 AI 字段，则可保存草稿，但应补充 AI 字段后再生成提取包，避免 `{}` 同时被解释为正常结果和文档不匹配。

## 7. 校验与一致性保证

### 7.1 结构错误：阻止导出

- 缺少主表、多个主表、子表无有效父表，或出现第二层子表。
- 字段名不符合全小写 snake_case，同表重名，或表名在目标数据库可能冲突。
- 保留 System 字段缺失、类型不正确或 Source 被改为 AI/Manual。
- SQL 类型参数无效，或明显不能承载定义的业务值。
- Choice 无选项、存在无法无损导出的分隔内容。
- FieldOrder 不连续；由编辑器自动规范化。
- JSON/prompt 字段投影与 Source=AI 字段集合不一致。
- SQL 列集合与 mapping 字段集合不一致。
- 明确的缺失值、日期和额外输出规则冲突。

### 7.2 需审核的提示

- AI 建议未确认、字段来源不确定。
- 示例值与类型/选项不符、前导零可能已在 Excel 中丢失。
- 必填 AI 字段在测试中为 null。
- System 业务字段没有 Datara 填充说明。
- 自定义 SQL 类型可能缩短长度或丢失精度。
- 当前测试属于旧 Profile 版本，或尚未测试。

### 7.3 测试结果处理

将 `{}` 文档不匹配、合法空数组、必填值缺失、JSON 结构错误、API 超时分开呈现。JSON Schema 允许 AI 字段为 null，但要求有效文档结果包含所有应有键；拒绝未定义键及 System/Manual 字段。

日期除八位格式外还检查实际日历有效性。整数、金额和字符串按定义校验；不悄悄将账号转数字或将缺失值补零。`25.0` 与 `25.00` 是相同 JSON 数值，金额精度在数值验证和 SQL 存储层控制。

## 8. 第一版本地架构

推荐单个 Python 应用：FastAPI + 服务端页面模板 + 少量 JavaScript。字段表格支持拖动和即时预览，不先引入独立前端服务。

| 模块 | 职责 |
|---|---|
| 浏览器 UI | Profile 列表、上传预览、字段编辑、测试、导出、模型设置 |
| Profile 服务 | 草稿版本、系统字段补齐、审核状态、变更追踪 |
| 统一模型与校验 | 类型验证、来源规则、关系规则、产物一致性 |
| 文件输入 | PDF 渲染、图片读取、Excel 解析 |
| 模型适配器 | 草稿建议、测试提取、能力检测、请求/响应适配 |
| 生成器 | XLSX、SQL、中文 prompt、JSON 结构、ZIP |
| 本地存储 | Profile JSON、样本、测试记录、导出快照 |

实现可采用 Pydantic 定义模型与约束、成熟的 PDF 渲染库处理页面、Excel 库处理模板。具体依赖版本在开发时固定。设计不依赖 AI 供应商特定 SDK 才能完成本地导出。

本地草稿使用 JSON 文件与原子写入；版本号用于检测过期修改。文件放在可配置的数据目录，通过存储接口访问。第一版单用户、单服务进程，不需要 SQL Server、Redis、向量数据库或消息队列。

模型调用提供进行中状态和取消操作。第一版可用进程内任务执行；应用重启后未完成任务标记中断，可重试，不假设后台任务跨重启继续。

## 9. 模型与端点配置

用户候选模型为 Qwen3.8 Max 或 Kimi3。产品名称不直接硬编码为 API model ID；实际 ID、图片能力、结构化输出能力及上下文限制，通过用户账号和端点验证。

设置页包括连接名称、协议适配器、Base URL、API Key、模型 ID，以及测试连接/测试图片功能。默认优先支持 OpenAI-compatible 接口，公司端点不同协议时增加适配器，字段和生成逻辑无需改变。

PDF 是用户输入格式，不假定所有模型原生接受 PDF。默认在本地转换为页面图片，通过视觉请求发送；图片输入复用同一路径。供应商原生 PDF 能力可作为以后优化。

API Key 仅在后端使用，不写入 Profile、提示词、日志或 ZIP。建议界面输入的 Key 默认只在当前会话保留；需跨重启保存时使用系统凭据存储，部署时使用环境变量或 Secret。业务样本仅在用户发起草稿生成/测试时发送至所选端点。

官方参考：

- [阿里云视觉理解文档](https://docs.modelstudio.console.alibabacloud.com/zh/model-studio/vision)：支持通过兼容接口传图，具体容量限制按实际模型配置。
- Kimi3 的准确 model ID 与目标端点能力在接入时验证，本方案不将其列为已验证连接。

## 10. Docker / Kubernetes 演进

第一版应用逻辑不依赖固定本机路径，配置和数据与应用代码分离。

1. 本地：单进程，JSON 草稿和本地样本目录。
2. Docker：同一应用打包镜像，挂载数据目录，以环境变量或 Secret 注入端点配置。
3. 多用户/Kubernetes：加入认证，草稿迁移到共享数据库，样本迁移到共享存储；需要长任务可靠运行时再加入独立任务队列。

不将本地 JSON 文件存储直接当作多副本并发方案。通过替换存储/任务接口演进，保留统一字段模型与生成器。

[FastAPI 容器部署官方文档](https://fastapi.tiangolo.com/deployment/docker/)可作为后续部署依据。

## 11. 建议默认值：尚未逐项确认

以下为可调整的第一版建议，不应标记成已确认 Datara 规范。

| 项目 | 建议默认 |
|---|---|
| 数据库/schema | schema 为 dbo；数据库名可选，未填时不输出 USE，不硬编码 CHN_WORKFLOW |
| 其他 System 字段 NULL | 除 id、created_at 外允许 NULL；head_id 不默认 NOT NULL |
| 其他 System 字段默认值 | 不自动设置 is_active、interface_status、updated_at、modified_by 默认值 |
| updated_at 更新 | 由 Datara 更新；不加数据库触发器 |
| HeadDisplay | 主表设置；空值不展示，非空正整数建议不重复，允许间隔 |
| 新 Profile 展示字段 | 用户选择，不直接复制旧 Profile 的 1/2/3/5/6 |
| IsRequired 空白导入 | 暂作未声明必填处理，界面可明确设为是/否 |
| ChoiceValues 序列化 | 沿用逗号分隔；第一版禁止选项自身包含逗号，直到确认转义规范 |
| 无 AI 字段的表 | 从 AI JSON 投影中省略；mapping/SQL 保留 |
| JSON 结构示例 | 标量使用 null，子表以一条对象展示字段；真实无记录返回 [] |
| 测试是否必须 | 不强制先调用 API 才导出，但显示测试状态；结构校验必须通过 |
| 普通图片 | 首先支持 PNG/JPEG，其他格式按依赖能力扩展 |
| API Key 保存 | 默认当前会话；持久保存采用系统凭据存储 |

Datara 是否已实现新增 System 业务字段的查询/计算，需要在具体 Profile 上核对。本工具负责定义与生成，不自动实现 Datara 的业务运行逻辑。

## 12. 开发阶段与验收

### 阶段一：统一字段模型和生成器

实现 System 模板、来源投影、mapping/SQL/prompt/JSON 生成、结构校验。先用人工建立的 Profile 数据验证，避免被模型接入阻塞。

验收：Cheque 和 Vendor Invoice 都能表达；AI_POGR 可由规则补出；非 AI 字段不会泄漏；mapping 与 SQL 列集合完全一致；四份预览同源。

### 阶段二：本地编辑流程

实现上传预览、Excel 导入、字段表格、主子表、拖动排序、保存/打开草稿及 ZIP 导出。

验收：一次字段重命名或来源调整同步影响所有输出；重新打开草稿保留 SQL 覆盖和提取规则；原始样本不被覆盖。

### 阶段三：视觉模型和测试

实现连接设置、AI 草稿、用户审核、测试提取和结果校验；通过一条实际可用的视觉端点完成闭环。

验收：PDF/图片均可用；未识别字段为 null；无明细为 []；非目标单据为 {}；错误响应和过期测试明确显示；可替换端点与模型而不修改生成器。

### 阶段四：兼容性与本地交付

按新的确认规则建立测试基准，验证生成 SQL 在独立测试数据库中可建表、主键自增及级联删除有效；验证 Field Mapping 能被真实 Datara 导入。若暂时无法访问测试环境，应明确标为待执行，不能把静态检查称作运行验证。

补充本地启动说明、依赖锁定及配置说明。第一版无需完成 Kubernetes 部署。

### 明确延期

PDF 区域框选、深层子表、多用户权限、自动执行 SQL、现有数据库结构迁移、自动发布到 Datara，以及 Kubernetes 部署实施，不属于第一版。

## 13. 证据来源

确认规则来源：本次逐项问答，覆盖来源、系统字段、SQL 默认类型、空值、日期、关系、展示、界面与导出要求。

生产参考：

- Datara Sample Cheque：样本 PDF/PNG、Field Mapping、sql.txt、prompts.txt、outputjson.txt。
- Datara Sample Vendor Invoice：样本 PDF/PNG、Field Mapping、sql.txt、prompts.txt、outputjson.txt。
- Datara_table_template.xlsx、Datara_sql_template.txt、Datara 提示词模板.docx。
- AI_Cash_Cheque_Table_Structure_20260729.xlsx 与 AI_Cash_Head_Detail_20260729.sql：辅助核对，不覆盖本次确认规则。

命名说明网页只有资源目录，未读取到完整规范正文。因此仅采用用户已确认的 snake_case 与表名规则，不推断额外 Profile 命名限制。
