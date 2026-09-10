# Mac / Windows 启动与公司网络排查

在包含 pyproject.toml 的项目目录操作。两种平台使用同一个 uv.lock，但必须各自安装依赖；不要从 Mac 复制 .venv 到 Windows。

## 公司 Windows：已有代码时，最快更新

1. 在运行旧程序的终端按 Ctrl+C。先备份业务数据文件夹（默认 data；设置过 DATARA_DATA_DIR 则备份该目录）。
2. VS Code 打开从 GitHub 克隆的 datara-profile-generator 文件夹，Terminal → New Terminal，选择 PowerShell。
3. 在同一个终端依次运行：

```powershell
git status --short
git branch --show-current
git pull --ff-only
uv sync --frozen
.\run.cmd
```

这份更新发布到 main。若当前不是 main 或 git status 显示自己改过的代码，先保存自己的修改并确认分支，再更新；遇到冲突或分歧不要使用 reset --hard 或强制覆盖。仅有 data 文件夹不会影响拉取，因为它不受 Git 跟踪。

打开 http://127.0.0.1:8765 ，浏览器 Ctrl+F5 刷新。字段列表应有「批量审核当前筛选」。可用 git log -1 --oneline 查看当前提交，并与本次交付邮件中的提交号核对。

如果原来使用的是下载 ZIP、目录内没有 .git，请新建 Git 克隆目录：

```powershell
git clone https://github.com/eunicesun2025/datara-profile-generator.git
cd datara-profile-generator
uv sync --frozen
.\run.cmd
```

若 Git、uv 找不到，先运行 git --version、uv --version 检查。安装后重新打开 VS Code 终端；受公司策略限制时请 IT 安装，不更改公司安全策略。

## 启动

先安装 Git、Python 3.11+ 和 uv（公司电脑按 IT 软件安装政策办理）。uv 安装方式见 https://docs.astral.sh/uv/getting-started/installation/ 。

Windows：在 VS Code 的 PowerShell 终端执行：

```powershell
uv sync --frozen
.\run.cmd
```

也可双击 run.cmd。无需激活 .venv，无需更改 PowerShell 执行策略。

Mac：

```bash
uv sync --frozen
bash run.command
```

通用启动命令（两种平台均可）：

```text
uv run --frozen python -m uvicorn datara.app:app --host 127.0.0.1 --port 8765
```

浏览器打开 http://127.0.0.1:8765 。终端保持开启，Ctrl+C 停止。更新代码后先停止旧服务，再 git pull --ff-only、uv sync --frozen 并重新启动。不要同时启动两个进程写同一 data 目录。

## 持久配置 API Key

项目启动时会自动读取项目根目录的 `.env`，且操作系统、服务器或容器中已有的环境变量优先。`.env` 已被 Git 忽略，不会随代码提交。

Windows：

```bat
copy .env.example .env
run.cmd
```

Mac：

```bash
cp .env.example .env
bash run.command
```

首次复制后，用文本编辑器把 `.env` 中的 `DATARA_API_KEY=` 改成真实密钥。不要把真实密钥写入 `.env.example`，也不要提交或发送 `.env`。

## 公司网络：证书和代理分别处理

旧版关闭了 HTTPX 的环境配置，导致 HTTPS_PROXY 和 SSL_CERT_FILE 不生效。新版启用这些变量并保持 TLS 证书验证。GitHub 能拉代码不代表模型域名也可访问，两者可能使用不同代理、信任库和网络放行规则。

如果提示 TLS 证书验证失败，请 IT 提供可信的 PEM CA 证书链文件（包含需要的企业根/中间 CA；需要访问其他服务时保留相应公共 CA）。不要用 API Key 代替证书，也不要设置 verify=False。Windows 信任库中存在证书，不代表 HTTPX 的默认 CA 库已包含它。

在**启动应用的同一个终端**设置，路径按当前电脑修改：

Windows PowerShell：

```powershell
$env:SSL_CERT_FILE = 'C:\CompanyCerts\ca-bundle.pem'
# 仅在 IT 要求代理时设置，以下是占位示例：
$env:HTTPS_PROXY = 'http://proxy.company.example:8080'
$env:NO_PROXY = '127.0.0.1,localhost'
.\run.cmd
```

Mac：

```bash
export SSL_CERT_FILE='/Users/yourname/CompanyCerts/ca-bundle.pem'
# 仅在 IT 要求代理时设置，以下是占位示例：
export HTTPS_PROXY='http://proxy.company.example:8080'
export NO_PROXY='127.0.0.1,localhost'
bash run.command
```

不需要代理时不要复制代理示例。HTTPX 读取 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY 和 NO_PROXY；不会自动解析公司浏览器的 PAC 脚本，地址请向 IT 获取。环境变量只对该终端启动的子进程生效，修改后重启应用。代理凭据如有需要仅在本机配置，不发送给别人或提交到 Git。

若失败发生在 uv sync 阶段，属于依赖下载网络，尚未进入视觉模型调用；uv 同样支持 SSL_CERT_FILE。按安装版本的 uv --help 检查系统证书选项。不要通过关闭 TLS 校验绕过。

错误分流：证书错误检查 CA 链/系统时钟；代理错误检查代理地址、认证及域名放行；超时检查域名可达性；401 检查 Key/地域/套餐；模型或图片能力错误检查账号支持的模型 ID。先测试连接（文本），再测试提取（图片）。不要把默认模型名当作账号可用性的保证。

## 一步诊断与常见问题

在项目根目录、启动应用所用的同一个终端运行：

```powershell
uv run --frozen python scripts/diagnose.py
uv run --frozen python scripts/diagnose.py --network
```

第一条检查系统、依赖、数据是否存在、代理/证书变量是否设置及本地端口。第二条另外对当前模型端点发一个不含 Key 和业务文档的请求。输出隐藏代理变量值，不打印密钥。网络检查收到 HTTP 401/403，说明已收到 HTTP 响应，不代表 Key 无效，也不代表图片能力已经通过。

|现象|先检查|处理|
|git pull 失败|是否在 Git 克隆目录；当前分支；本地修改|保存修改；核对 GitHub 登录与公司网络，不强制覆盖|
|uv sync 失败|这是下载依赖阶段，不是模型调用|记录第一条错误，核对下载域名、IT 代理及 CA；本机未安装 Python 时 uv 还需下载 Python|
|SSL / CERTIFICATE_VERIFY_FAILED|SSL_CERT_FILE 是否存在、是否为 PEM CA 链；是否在同一终端启动|请 IT 提供可信证书链；重启，不能关闭 TLS 验证|
|ProxyError / 超时 / 无法连接|代理是否要求认证、目标域名是否放行|请 IT 确認端点网络策略；不要复制占位代理地址|
|HTTP 401|Key、端点、套餐和地域是否匹配|在模型设置重新输入正确 Key；不要把 Key 发在诊断截图中|
|文本成功、图片失败|模型是否支持图片、请求大小、额度及响应时间|用一张小 PDF 测试；在模型设置将超时调到 600 秒后仅重试失败样例（上限 1800 秒）|
|提取测试成功，但添加参考文件后 AI 字段分析失败|先确认版本是否包含单文本块兼容修复；记录 HTTP 状态码与参考字符数|更新并重启服务；新版本把参考文字合并进唯一文本块。若仍为 HTTP 400/413/422，缩小参考文件并让网关管理员确认支持 `text + image_url` 的 Chat Completions 多模态请求|
|端口被占用 / WinError 10048|旧终端是否仍运行|先打开原地址确认；需要重启时 Ctrl+C 停旧服务，不同时写同一数据目录|
|WinError 10013 / 权限拒绝|公司端点防护、端口限制|请 IT 放行本机程序；也可使用允许的本机端口，例如 8766|
|页面打不开|终端是否显示 Uvicorn running；代理是否绕过 localhost|保持终端开启；NO_PROXY 包含 127.0.0.1,localhost|
|更新后仍是旧页面|旧服务、浏览器缓存、打开了另一份代码|停止旧服务，从正确项目启动，Ctrl+F5 刷新|
|没有现金 Profile|业务文件不在 GitHub；数据目录不一致|按交付包 README 设置 DATARA_DATA_DIR；诊断脚本检查 Profile count|
|JSON 校验通过但内容不对|日期来源、现金/支票列、手写内容、参考答案质量|对照原图和测试报告，不能把结构通过当作识别正确|

若端口必须改用 8766，在停止旧服务后运行 uv run --frozen python -m uvicorn datara.app:app --host 127.0.0.1 --port 8766，并打开 http://127.0.0.1:8766 。诊断脚本的默认端口检查仍为 8765。

求助时提供：git log -1 --oneline、诊断脚本输出、发生错误的阶段、状态码和经过脱敏的报错。不要提供 API Key、代理密码、完整环境变量列表或业务文档原文。

参考：[HTTPX 环境变量](https://www.python-httpx.org/environment_variables/)、[HTTPX TLS](https://www.python-httpx.org/advanced/ssl/)、[uv TLS](https://docs.astral.sh/uv/concepts/authentication/certificates/)。

## 减少审核操作

审核标记是字段定义的提醒，不是每次识别一张单据都要重复确认，也不阻止导出。明确的完整 Mapping 保持已审核；来源/类型缺失、AI 建议、字段层级修复或子表关联修复才标为待审核。新加的必要 System 字段无需逐项确认。

选择主表或子表及来源筛选，点击「批量审核当前筛选」，取消勾选仍有疑问的字段，再点击「确认所选字段」和「保存草稿」。批量确认只修改审核状态，不能消除非法字段名、Choice 空选项等真正的校验错误。不同表分别确认，避免误选其他表。

## 数据迁移与验证边界

停止两端应用，先备份，再迁移 data 文件夹到新电脑；已有 data 不要直接覆盖。只迁移业务数据，不迁移 .venv。API Key 重启后重新输入，证书路径和代理也按目标电脑配置。应用的 JSON 读写明确使用 UTF-8，避免 Windows 中文系统默认编码造成草稿打不开。

Mac 本地回归通过后仍需 Windows 验收。仓库提供 docs/cross-platform-tests.yml.example，包含 macOS / Windows / Linux 测试矩阵。当前 GitHub 凭据没有 workflow 权限，因此未启用远程工作流；这三个平台的 Actions 结果均不能视作已通过。拥有相应权限的仓库维护者可另行启用。公司网络和真实视觉模型仍须在公司电脑完成「测试连接 → 上传 PDF → 测试提取 → 对照单据」验收。
