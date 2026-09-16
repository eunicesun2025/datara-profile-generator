# 可配置日志级别：让模型请求计时行不再被静默丢弃

- 日期：2026-09-16
- 类型：可观测性 / 配置（独立于同日 `2026-09-16-extract-job-retry-and-elapsed-feedback.zh-CN.md` 的缺陷修复）
- 影响面：`datara/app.py`、`.env.example`、`README.md`、`ARCHITECTURE.md`、`ARCHITECTURE.zh-CN.md`、`tests/test_app.py`
- 风险：低。默认级别仍为 `WARNING`，实测 stderr 输出与改动前逐行一致

## 1. 问题

排查「测试提取一直在 run」时，最关键的一条证据是 `datara.provider` 的
`model_request_started` / `model_request_finished` 计时行——只有它能区分
「模型响应慢」与「连接被企业代理挂住」。但在按 `run.cmd` / `run.command` 正常启动时，
**这些 INFO 行根本不会出现**，我当时只能靠一个临时放在 `%TEMP%`、未纳入版本控制的
包装器脚本才拿到日志。

根因是全仓库没有任何 logging 配置。只有两处取 logger：

```
datara/provider.py:15   logger = logging.getLogger(__name__)
datara/optimizer.py:25  logger = logging.getLogger(__name__)
```

没有 `basicConfig`、没有 `dictConfig`、没有级别设置。

## 2. 为什么 `--log-level info` 解决不了

这是最直觉的修法，但无效。`uvicorn/config.py:413-420`：

```python
if self.log_level is not None:
    ...
    logging.getLogger("uvicorn.error").setLevel(log_level)
    logging.getLogger("uvicorn.access").setLevel(log_level)
    logging.getLogger("uvicorn.asgi").setLevel(log_level)
```

**`--log-level` 只改这三个 uvicorn 自己的 logger，从不碰 root，也从不碰 `datara.*`。**
而 uvicorn 默认的 `LOGGING_CONFIG` 里没有 `root` 条目，所以 root 始终无 handler。
子 logger 无 handler → 向 root 传播 → root 也无 handler → Python 退回
`logging.lastResort`，其级别为 `WARNING` → INFO 记录被丢弃。

实测探针（`uvicorn.config.LOGGING_CONFIG` 经 `dictConfig` 应用后）：

```
datara.provider effective level : WARNING
root logger handlers            : (none)
lastResort level                : WARNING
--- only WARN-VISIBLE should appear below ---
WARNING:datara.provider:WARN-VISIBLE      ← INFO-VISIBLE 未出现，已丢弃
```

## 3. 方案

在 `.env` 增加 `DATARA_LOG_LEVEL`，`app.py` 导入时据此配置根日志器，默认 `WARNING`。
选这个方案是因为它符合项目规范中「所有配置项集中在单一配置文件」与「按环境自动切换」两条要求：
生产保持 `WARNING`，排障时改 `.env` 重启即可，不需要改代码或换启动脚本。

```python
# Accepted DATARA_LOG_LEVEL values. An unrecognised value falls back to WARNING instead of
# aborting startup: a typo in a logging setting must never take the service down.
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_LOG_LEVEL = "WARNING"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def resolve_log_level(raw: str | None) -> str:
    """Map a raw DATARA_LOG_LEVEL value to a known level name, defaulting to WARNING."""
    name = (raw or "").strip().upper()
    return name if name in LOG_LEVELS else DEFAULT_LOG_LEVEL


def configure_logging() -> str:
    raw = os.environ.get("DATARA_LOG_LEVEL")
    level_name = resolve_log_level(raw)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level_name))
    if not root.handlers:                      # 不叠加、不夺走 pytest 已装的 handler
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
    requested = (raw or "").strip()
    if requested and requested.upper() not in LOG_LEVELS:
        logging.getLogger(__name__).warning(
            "DATARA_LOG_LEVEL=%r 无法识别，已回退到 %s", raw, DEFAULT_LOG_LEVEL)
    return level_name


load_runtime_environment()
configure_logging()
```

三个刻意的设计取舍：

1. **调用点在 `load_runtime_environment()` 之后**。`.env` 必须先加载，否则读不到该变量。
   uvicorn 的 `configure_logging()` 在 `Config.__init__` 就执行，早于导入 `datara.app`，
   所以我们不会被它覆盖。
2. **只在 root 无 handler 时添加 handler**。重复调用不会叠加（否则每行日志打印多次），
   也不会顶掉 pytest logging 插件已安装的 handler。级别则每次都设，使函数可重入、可测试。
3. **无法识别的值回退 `WARNING` 并记一条警告**，而不是抛异常。日志配置的拼写错误
   不应该让服务起不来；但也不能静默回退，否则使用者会以为设置生效了。

未使用 `basicConfig(force=True)`：`force` 会移除既有 root handler，在 pytest 下会破坏捕获。

## 4. 测试（`tests/test_app.py`，+12 例）

新增 `restore_root_logger` fixture 快照并还原 root 的级别与 handler 列表，避免这些用例
把全局日志状态泄漏给套件里其他测试。

| 测试 | 断言 |
|---|---|
| `test_resolve_log_level_accepts_known_names_and_falls_back`（8 组参数化） | 五个合法级别（含小写 `info`、带空白 `  Warning  `）正确解析；`None`、`""`、`"verbose"` 均回退 `WARNING` |
| `test_configure_logging_keeps_warning_by_default` | 未设置该变量时 root 级别为 `WARNING`——锁定「默认行为不变」这条主要风险 |
| `test_configure_logging_enables_provider_diagnostics_at_info` | `INFO` 时 `datara.provider` 有效级别为 `INFO`；挂一个捕获 handler 后 `info("model_request_started")` **确实到达**，而 `debug(...)` 不到达（证明级别是 INFO 而非被放成 DEBUG） |
| `test_configure_logging_does_not_duplicate_root_handlers` | 连续调用三次，handler 数量不变 |
| `test_configure_logging_warns_about_an_unusable_level` | `DATARA_LOG_LEVEL=verbose` 时通过 `caplog` 断言发出含该变量名与原值的警告 |

第三个用例是重点：只断言「级别数字变成 20」不足以证明诊断行真的可见——
必须让记录穿过 handler。这正是当初缺口存在却无人发现的原因：没有任何测试覆盖它。

## 5. 验证

单元与套件：

```
.\.venv\Scripts\python.exe -m pytest -q        → 120 passed, 2 warnings in 14.61s   (exit=0)
.\.venv\Scripts\python.exe -m pytest -q tests/test_app.py
                                               → 22 passed
```

真实进程验证（**普通 uvicorn，不使用任何包装器**，仅靠 `.env`）：

`DATARA_LOG_LEVEL=INFO`：

```
started PID=32604 / listeners on 8765: 1
POST /api/settings/test → ok=True
2026-09-16 16:45:25,489 INFO datara.provider model_request_started model=qwen3.8-max-0902 images=0 image_bytes=0 text_characters=35 timeout_seconds=600
2026-09-16 16:45:27,584 INFO datara.provider model_request_finished model=qwen3.8-max-0902 elapsed_seconds=2.09
```

`DATARA_LOG_LEVEL=WARNING`（默认）：

```
started PID=22540
POST /api/settings/test → ok=True
provider INFO lines (expect 0): 0
total stderr lines: 4        ← 与改动前逐行一致，仅 uvicorn 自身四行
```

即默认路径零行为变化，不存在「升级后突然刷屏」的风险。

最终服务以 `DATARA_LOG_LEVEL=INFO` 运行（PID `16108`，`http://127.0.0.1:8765`），
Profile revision 9 保持。`%TEMP%\datara_serve.py` 包装器已不再需要。

## 6. 顺带修正的过期文档

`ARCHITECTURE.md` 原第 218 行写着：

> The application does not load `.env` files.

这与代码不符——`app.py` 一直在导入时调用 `load_runtime_environment()` → `load_dotenv(...)`，
且 `tests/test_app.py::test_project_env_loads_once_without_overriding_environment` 正是为此而写，
`README.md:102` 也早已正确说明「应用启动时会自动读取当前工作目录下的 `.env`」。
已改为准确描述，并补充 `override=False` 的优先级语义与 `configure_logging()` 的必要性说明。

两份 ARCHITECTURE 与 README 均已登记 `DATARA_LOG_LEVEL`；`.env.example` 增加该项并注明
「uvicorn `--log-level` 对此无效」，避免后来者重走弯路。

## 7. 已知限制

- 级别在**导入时**读取一次，运行期改 `.env` 需重启进程。本项目为单进程本地优先应用，
  未提供热重载或运行时调级接口。
- 只配置 root logger，不接管 uvicorn 自身的日志格式；因此 stderr 中 uvicorn 行仍是
  `INFO:     Started server process [...]` 风格，应用行是 `2026-09-16 16:45:25,489 INFO datara.provider ...`
  风格。刻意不做统一，以免干扰 uvicorn 既有输出与 access log 解析。
- 无日志轮转/落盘。输出到 stderr，重定向由启动方负责（如 `run.cmd` 或容器运行时）。

