# 变更记录：连接超时被误报为模型请求超时

- 日期：2026-09-16
- 触发问题：优化任务 `7fdd8f21-f2ff-4917-849f-82ea8b7d92c6` 失败，提示
  「候选提示词评估失败：案例「INVOICE_23227.pdf」调用文档提取模型 qwen3.8-max-0902 时出错（单次超时 600 秒）：模型请求超时，可调整超时设置后重试」
- 影响范围：所有模型调用的错误分类与重试策略；优化任务在瞬时网络抖动下会直接失败
- 测试结果：`98 passed`

## 1. 为什么要改

### 1.1 现场证据

服务日志 `%TEMP%\datara_srv.err.log` 中该次运行的关键片段：

```
13:18:05.575 optimizer_case_completed phase=baseline attempts=0 elapsed_seconds=0.0   ← 复用 observed_output，未联网
13:18:05.619 model_request_started  images=3 image_bytes=869976 text_characters=12229 timeout_seconds=600
13:18:42.013 httpx: POST .../compatible-mode/v1/chat/completions "HTTP/1.1 200 OK"
13:18:42.019 model_request_finished elapsed_seconds=36.40                              ← 成功
13:18:42.057 model_request_started  images=3 image_bytes=869976 text_characters=7893  timeout_seconds=600
             （无任何 httpx 响应日志）                                                  ← HTTP 请求从未发出
13:18:57.402 model_request_finished elapsed_seconds=15.34                              ← 仅 15.34 秒
13:18:57.418 optimizer_run_failed …（单次超时 600 秒）：模型请求超时
```

`15.34 ≈ 15`，正好等于 `datara/provider.py` 中 `httpx.Timeout(c.timeout, connect=15)`
里硬编码的 15 秒连接超时；且该次请求没有任何 httpx 响应日志，说明连 TCP/TLS 都没建立成功。
真实异常是 **`httpx.ConnectTimeout`**，与 `Connection.timeout`（600 秒）无关。

### 1.2 三个连锁缺陷

| # | 位置 | 缺陷 |
|---|---|---|
| ① | `datara/provider.py:106` | `except (httpx.TimeoutException, TimeoutError)` 捕获了 `ConnectTimeout`（它是 `TimeoutException` 的子类），把 15 秒建连失败上报为「模型请求超时，可调整超时设置后重试」。该建议是错的：失败的是固定 15 秒连接超时，`Connection` 没有对应配置项，调 `timeout` 完全无效 |
| ② | `datara/optimizer.py:658` | `limit = 1 if "请求超时" in message else 2 if "无法连接" in message else 3`。因缺陷①误标为「请求超时」，本该属于「无法连接」类、可廉价重试的建连失败拿到 `limit=1`，即**零重试**，一次握手抖动就废掉整个优化任务 |
| ③ | `datara/optimizer.py:736-739` | 包装文案无条件插入「（单次超时 {connection.timeout} 秒）」。无论真实原因是 TLS 证书失败还是 15 秒建连失败，都谎称超时。历史上运行 `c1f83651` 的 **TLS 证书验证失败**同样被贴上「（单次超时 1800 秒）」 |

缺陷②的设计初衷（见原注释：一次真实模型超时可能已耗掉 10–30 分钟，重试会让失败样本看起来卡了两倍超时）
对**响应阶段**超时是合理的，但它顺带掐死了**连接阶段**的失败——后者只花 15 秒且极可能重试成功。

### 1.3 为什么不能只改配置

`connect=15` 是硬编码常量，`Connection` 模型无对应字段，`ARCHITECTURE.md` 亦记载
「connect timeout is separately fixed at 15 seconds」。**不存在任何设置项可以规避此故障**，必须改代码。

## 2. 改了哪些文件

| 文件 | 位置 | 改动性质 |
|---|---|---|
| `datara/provider.py` | 原 106–109 行（`completion()` 异常处理） | 新增 `ConnectTimeout` 专用分支 + 网络异常类型日志 |
| `datara/optimizer.py` | 原 736–739 行（`_extract_version()` 内 `extract_case`） | 「单次超时 N 秒」改为条件插入 |
| `tests/test_provider.py` | 文件末尾新增 | 2 个单元测试 |
| `tests/test_optimizer.py` | 原 473 行前、原 542 行前新增 | 2 个单元测试 + 1 个集成回归测试 |
| `README.md` | 「优化耗时与模型设置」新增 1 条 | 说明两个独立超时预算 |
| `ARCHITECTURE.md` | 「Persisted connection settings」新增 1 段 | 英文架构说明 |
| `ARCHITECTURE.zh-CN.md` | `connection.json` 表格后新增 1 段 | 中文架构说明 |
| `docs/changes/2026-09-16-connect-timeout-misclassification.zh-CN.md` | 新建 | 本文件 |

未改动：数据库/存储结构、API 契约、`Connection` 字段、前端、重试次数上限策略（仍为 1/2/3）。

## 3. 改动详情

### 3.1 `datara/provider.py` — 区分连接阶段与响应阶段超时

改动前（原 106–109 行）：

```python
    except (httpx.TimeoutException, TimeoutError) as e:
        raise ValueError("模型请求超时，可调整超时设置后重试") from e
    except httpx.HTTPError as e:
        cause = e
```

改动后（现 106–124 行）：

```python
    except httpx.ConnectTimeout as e:
        # Connection setup is bounded by the fixed 15 second connect timeout above,
        # not by Connection.timeout. Transparent corporate proxies that re-sign TLS
        # can exceed it once and succeed immediately afterwards. httpx reports it as
        # a TimeoutException subclass, so without this branch it was labelled as a
        # model response timeout: that advises the wrong setting and, through the
        # "请求超时" retry rule in the optimizer, cancels the retry budget entirely.
        logger.warning("model_request_connect_timeout model=%s connect_timeout_seconds=15 error=%s: %s",
                       c.model, type(e).__name__, e)
        raise ValueError("无法连接模型端点：建立连接超过 15 秒连接超时（企业代理握手缓慢或网络抖动），可重试") from e
    except (httpx.TimeoutException, TimeoutError) as e:
        raise ValueError("模型请求超时，可调整超时设置后重试") from e
    except httpx.HTTPError as e:
        # Keep the transport-level type visible. The user-facing messages below are
        # deliberately generic, which made ConnectError and RemoteProtocolError
        # indistinguishable when diagnosing failures in the field.
        logger.warning("model_request_network_error model=%s error=%s: %s",
                       c.model, type(e).__name__, e)
        cause = e
```

关键点：

- `except httpx.ConnectTimeout` **必须排在** `except (httpx.TimeoutException, TimeoutError)` 之前，
  因为它是后者的子类；顺序颠倒则新分支永不生效。
- 新文案刻意包含 `无法连接模型端点`、且**不含** `请求超时`。这两个子串正好对应
  `optimizer.py:653` 的 `transient` 白名单与 `optimizer.py:658` 的 `limit` 判定，
  因此无需改动 optimizer 的重试规则即可自动获得 `limit=2`（一次重试）。
- 两条 `logger.warning` 记录 HTTPX 原始异常类型，修复此前无法区分
  `ConnectError` / `ConnectTimeout` / `RemoteProtocolError` 的诊断盲区。不记录 API Key 或请求正文。

### 3.2 `datara/optimizer.py` — 停止无条件谎称超时

改动前（原 736–739 行）：

```python
                    raise ValueError(
                        f"{phase_name}失败：案例「{case['name']}」调用文档提取模型 "
                        f"{connection.model} 时出错（单次超时 {connection.timeout} 秒）：{exc}"
                    ) from exc
```

改动后（现 736–747 行）：

```python
                    # This wrapper used to claim a per-request timeout for every
                    # failure, including TLS and connection-setup errors, which sent
                    # users to the wrong setting. Only add the budget note when the
                    # cause is a provider timeout that does not already state its
                    # own bound (the retry-inclusive message from _complete does).
                    message = str(exc)
                    budget = (f"（单次超时 {connection.timeout} 秒）"
                              if "请求超时" in message and "秒" not in message else "")
                    raise ValueError(
                        f"{phase_name}失败：案例「{case['name']}」调用文档提取模型 "
                        f"{connection.model} 时出错{budget}：{message}"
                    ) from exc
```

判定表（条件为 `"请求超时" in message and "秒" not in message`）：

| 内层消息 | 含「请求超时」 | 含「秒」 | 加「单次超时 N 秒」 |
|---|---|---|---|
| `模型请求超时，可调整超时设置后重试`（provider 响应超时） | 是 | 否 | ✅ 加（自身未说明预算） |
| `模型请求超时：总等待超过 600 秒（含重试）`（`_complete` 总预算） | 是 | 是 | ❌ 不加（已自述预算，且「单次」措辞不准） |
| `无法连接模型端点：建立连接超过 15 秒连接超时…`（本次修复） | 否 | 是 | ❌ 不加 |
| `TLS 证书验证失败：…` | 否 | 否 | ❌ 不加（修正 `c1f83651` 那类误标） |
| `无法连接模型端点，请检查网络和 API 地址` | 否 | 否 | ❌ 不加 |

## 4. 新增测试

### 4.1 `tests/test_provider.py`（文件末尾）

| 测试 | 断言 |
|---|---|
| `test_connect_timeout_is_reported_as_a_connection_failure` | 用 `MockTransport` 抛 `httpx.ConnectTimeout`，验证消息含 `无法连接模型端点`、含 `15 秒连接超时`、**不含** `请求超时`（后两条共同守住 optimizer 的重试预算） |
| `test_read_timeout_is_still_reported_as_a_request_timeout` | 抛 `httpx.ReadTimeout`，验证仍匹配 `模型请求超时`，确保只重分类连接阶段 |

### 4.2 `tests/test_optimizer.py`

| 测试 | 断言 |
|---|---|
| `test_optimizer_retries_a_connection_setup_failure` | `_complete()` 对连接失败调用 `completion` **恰好 2 次**（`limit=2`），最终抛出 `无法连接模型端点` |
| `test_optimizer_recovers_when_the_second_connection_attempt_works` | 首次连接失败、第二次成功，返回 `("{}", attempts == 2)` |
| `test_candidate_connection_failure_is_retried_and_not_reported_as_a_timeout` | 端到端跑一次优化任务：`status == "failed"`、blocking reason 含 `候选提示词评估失败` 与 `无法连接模型端点`、**不含 `单次超时`**、`evaluation_calls == 2`、日志出现 `optimizer_model_retry`、且日志不含 `optimizer-key` |

集成测试刻意**不**打桩 `asyncio.sleep`：`datara.optimizer.asyncio` 就是全局 `asyncio` 模块，
在 `TestClient` 场景下打桩会波及 anyio 内部。真实退避仅 1 秒，可接受。

### 4.3 既有测试的兼容性

`test_candidate_evaluation_timeout_preserves_candidate_and_fails_iteration`（原 530 行）断言
`"单次超时 1800 秒" in blocking_reasons[0]`，其打桩消息为 `模型请求超时，可调整超时设置后重试`
（含「请求超时」、不含「秒」）→ 条件成立 → 仍加该注记 → **测试无需修改即通过**。
同测试的 `timeout_calls == 1` 也保持成立，因为「请求超时」类的 `limit` 仍是 1。

## 5. 验证

```
.\.venv\Scripts\python.exe -m pytest tests/test_provider.py tests/test_optimizer.py -q -k "connect or timeout or retr"
→ 11 passed, 36 deselected, 2 warnings in 6.03s

.\.venv\Scripts\python.exe -m pytest -q
→ 98 passed, 2 warnings in 11.13s
```

注：本机 `uv` 不在 PATH，故直接用 `.venv` 内的 Python 调用 pytest；等价于 AGENTS.md 要求的 `uv run pytest -q`。

## 6. 行为变化与风险

| 场景 | 改动前 | 改动后 |
|---|---|---|
| 建连超过 15 秒（Zscaler 握手抖动） | 报「模型请求超时」，0 次重试，整个优化任务失败 | 报「无法连接模型端点：…15 秒连接超时…」，**自动重试 1 次**（退避 1 秒），第二次成功则任务继续 |
| 建连持续失败 | 约 15 秒后失败 | 约 31 秒后失败（15 + 1 + 15），仍被 `_complete` 的 `asyncio.timeout(600)` 包住 |
| 响应阶段真实超时 | 报「模型请求超时」，0 次重试 | **不变** |
| TLS 证书失败 | 谎称「（单次超时 1800 秒）」 | 不再附加超时注记 |
| 其他网络错误 | 无原始异常类型日志 | 新增 `model_request_network_error` WARNING |

风险：极低。未改动重试上限策略、超时数值、API 契约与存储结构；唯一的行为增量是
「连接阶段失败多一次重试」，最坏情况多花约 16 秒。

## 7. 后续待办（本次未做）

1. **`provider.py:64` 的 `"stream": False`**：改为流式是根治「长时间静默连接被中间设备掐断」的手段，
   但属于协议层改动，需单独评估与测试。本次故障是**建连阶段**失败，流式对其无帮助，故未一并改。
2. **`optimizer.py:688-694` 的 `progress`**：`request_started_at` 每个案例只写一次，UI 无法区分
   「第 1 次尝试」与「重试」，也看不出已失败过一次。可在 `_complete_attempts` 中按 attempt 更新。
3. **`app.js:319` 的 `persistSettings()`**：未发送 `enable_thinking`，导致每次点「保存设置」都会把该字段
   静默重置为 `null`。当前 `null` 恰好等于自动策略，无实际损害，但显式设置会被 UI 覆盖。
4. **Zscaler**：可申请将 `*.aliyuncs.com` 加入 bypass / exclusion 列表，从环境层减少握手抖动。
