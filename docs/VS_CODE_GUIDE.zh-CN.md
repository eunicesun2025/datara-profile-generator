# 从 GitHub 到 VS Code：Datara Profile Generator

公司 Windows / 当前 Mac 请先看 [跨平台启动、证书代理与批量审核](CROSS_PLATFORM.zh-CN.md)。

## 先在当前电脑使用

应用地址：http://127.0.0.1:8765/

打开已保存的“现金收款 · 手工 Mapping 实测”。这份本地草稿来自提供的手工 Field Mapping，已经关联 PDF，并加入待审核的识别规则。点击“输出预览”即可查看 SQL、提示词和 JSON；“导出 ZIP”下载四份配置。不需要先连接模型。

“Datara_profile_cash20260904”是另外一次通过浏览器导入的原始字段草稿，用于验证上传和导入操作。推荐继续使用前面的“现金收款 · 手工 Mapping 实测”。

刷新页面可看到新版。应用升级重启后，API Key 需在“模型设置”重新填写，密钥不会保存到磁盘。

## 从 GitHub 克隆到 VS Code

项目仓库：[eunicesun2025/datara-profile-generator](https://github.com/eunicesun2025/datara-profile-generator)。不要把 GitHub 网页当作运行中的应用：GitHub 保存代码，程序仍在本机启动。

1. 安装并打开 [Visual Studio Code](https://code.visualstudio.com/)。
2. 在上述 GitHub 仓库页面点击绿色 **Code**，选择 **HTTPS**，复制 `https://github.com/eunicesun2025/datara-profile-generator.git`。
3. 在 VS Code 按 **Cmd + Shift + P**（Windows 是 Ctrl + Shift + P），输入并选择 **Git: Clone**。
4. 粘贴仓库地址，选择存放项目的文件夹。私有仓库若要求登录，使用拥有仓库权限的 GitHub 账号。
5. 克隆完成后点击 **Open**。确认打开的是包含 `pyproject.toml`、`README.md` 和 `datara` 文件夹的项目目录。

参考：[VS Code 官方克隆指南](https://code.visualstudio.com/docs/sourcecontrol/repos-remotes)。

## 安装依赖并运行

在 VS Code 菜单选择 **Terminal → New Terminal**。先检查 uv 是否已安装：

```bash
uv --version
```

如果找不到命令，macOS 已安装 Homebrew 的环境可以执行：

```bash
brew install uv
```

其他安装方式见 [uv 官方安装指南](https://docs.astral.sh/uv/getting-started/installation/)。安装后重新打开终端。

在项目根目录运行：

```bash
uv sync --frozen
uv run uvicorn datara.app:app --host 127.0.0.1 --port 8765
```

看到 `Uvicorn running on http://127.0.0.1:8765` 后，在浏览器打开这个地址。终端保持运行；停止服务按 **Ctrl + C**。macOS 也可执行 `bash run.command`。

如果提示端口已占用，通常是旧应用仍在运行。可以直接打开原地址，或先停止旧终端中的服务；不要同时运行两个程序写同一个 data 目录。

## 已有 Field Mapping：直接导入

1. 首页点击 **导入已有 Field Mapping**，选择 `.xlsx` 文件。
2. 选择工作表，阅读导入前检查。工具支持选择修复缺失父表、同名表层级冲突；保留原文件，修复记录会显示在新草稿中。
3. 若某行的表名与层级冲突，按该表首次定义修复，并仅将受影响字段标为待审核；明细缺失的父表补为唯一主表。最新 Mapping 的 validity_check 已是主表层级，不必再次修复。
4. 点击 **导入为新草稿**。程序自动保存，并以文件名命名 Profile。
5. 核对 AI、Manual、System 来源。导入器补齐必要 System 字段，`head_id` 归 System。`ForeignKeyField` 在本工具格式中填的是父表名称，不是 `head_id` 字符串。
6. 每个字段的 `⋯` 打开详情，在“提取规则”说明从文档哪里读取。Field Mapping 不包含这些规则，因此单靠列名无法决定日期、金额等业务含义。
7. 点击 **保存草稿 → 输出预览 → 导出 ZIP**。

## 没有 Mapping：新建空白 Profile

首页只有一个 **新建空白 Profile** 入口。填写 Profile 名称和主表名称后，点击 **创建并打开**；新草稿会自动保存。

添加字段后选择来源及类型。需要明细时点击表标签旁的“＋”添加子表。先创建子表，再使用“AI 建议字段”，因为模型只能向已经存在的表建议字段。

修改表名：选中主表或子表标签 → **修改主表名称 / 修改子表名称** → 填写表名 → **保存表名** → **保存草稿**。表名会同步到 Mapping、SQL、提示词和 JSON；子表仍通过内部关联引用主表。

返回列表：点击 **← 返回我的 Profiles**。浏览器后退/前进也支持在本应用页面间切换，后退时有修改会先尝试保存。若保存发生版本冲突，会保留当前编辑内容并提示错误。建议离开前主动保存。刷新已保存的 Profile 地址会重新打开同一份草稿。

## 用 PDF 测试提取

1. 上传 PDF / PNG / JPEG。一次只选一张业务单据，PDF 最多 15 页。
2. 填写接受的单据类型，例如“现金收款单”。
3. 在字段规则明确：日期取运输行还是打印日期，金额取总数还是现金，编号的具体含义。
4. “模型设置”默认地址 `https://dashscope.aliyuncs.com/compatible-mode/v1`，模型 `qwen3.8-max-0902`，均可修改。模型 ID 按用户指定设置，真实账号可用性和图片能力仍待验证。
5. 填 API Key（只粘贴 Key，不加 Bearer），先“测试连接”，再进入“测试提取”。
6. HTTP 401 表示认证失败：检查 Key 是否有效、是否完整，以及 Key 的地域和套餐是否与 Base URL 匹配。通用百炼和 Coding / Token Plan 专属 Key 不能任意混用。参考 [阿里云错误码说明](https://www.alibabacloud.com/help/zh/model-studio/error-code)。
7. 模型返回后检查结构，再逐行对照 PDF。结构通过并不代表金额和日期正确。

当前已完成本地导入、PDF 渲染、保存、预览和导出验证；尚未完成真实模型认证与图片识别验收。也未连接真实 Datara 或执行 SQL。

## 四份输出的用途

|文件|用途|
|---|---|
|field_mapping.xlsx|字段、来源、类型及表关系|
|create_tables.sql|SQL Server 建表脚本，由目标环境人员审核后使用|
|extraction_prompt.txt|从同一份 Profile 生成的提取规则|
|output_structure.json|仅含 AI 字段的预期输出结构|

Manual 与 System 不出现在 AI JSON 中。Manual 只代表由业务系统或人员录入，当前生成器没有交易录入屏幕。缺失值返回 null，不自动补金额、币种或当前日期。SampleData 只是映射示例，不会变成 AI 标准答案或 SQL 默认值。

## 更新代码与保留本地数据

停止运行中的应用后，在项目终端执行：

```bash
git pull --ff-only
uv sync --frozen
uv run uvicorn datara.app:app --host 127.0.0.1 --port 8765
```

如果 `git pull` 提示本地修改或分支分歧，先检查并保存自己的代码，不要强制覆盖。

Profile、样本、测试记录和连接地址保存在项目的 `data/` 目录，未提交到 GitHub。因此新克隆的项目不会自动包含这台电脑上的业务草稿。若要迁移，停止服务后把旧项目的 `data/` 文件夹复制到新项目；不要覆盖新项目已有数据，先备份。API Key 仍需重新填写。

## GitHub 仓库与本地源码

仓库：https://github.com/eunicesun2025/datara-profile-generator

仓库由用户创建，当前为公开仓库。只上传代码、通用测试和指南，不上传 `data/`、真实 PDF、原始业务 Mapping、导出包、`.env` 或 API Key。

建议通过上面的 **Git: Clone** 从仓库创建新的工作目录，之后用 `git pull --ff-only` 获取更新。此前邮件中的 ZIP 也可运行，但 ZIP 解压目录没有 Git 历史，不能直接执行 `git pull`；需要更新时请改用克隆方式。

如果自己修改代码，先检查 VS Code 的 Source Control 列表，确认只包含准备上传的源码，再提交并推送。遇到登录提示时使用仓库所属 GitHub 账号；不要在聊天或代码中粘贴访问令牌。

## 开发验证

```bash
uv run pytest -q
```

回归测试，包含结构修复、表名修改后的主外键与输出一致性、保存重开、模型错误信息不泄露密钥等。模拟接口测试不能代替真实模型或 Datara 导入验收。
