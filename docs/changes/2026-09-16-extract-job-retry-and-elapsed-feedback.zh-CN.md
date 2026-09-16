# 变更记录：测试提取「一直 run」——瞬时失败零重试 + 无等待反馈

- 日期：2026-09-16
- 触发问题：用户反馈「优化提示词部分已经可以跑了，但是测试提取这个部分跑不起来了，一直在那边 run」
- 影响范围：所有交互式模型任务（测试提取 / AI 字段分析）的失败恢复能力；测试提取页的等待可观测性
- 前置变更：`docs/changes/2026-09-16-connect-timeout-misclassification.zh-CN.md`
- 测试结果：`108 passed`（原 98，新增 10）

## 1. 现场取证

### 1.1 用户那次「一直 run」到底发生了什么

服务日志 `%TEMP%\datara_srv.err.log`：

```
15:09:48.586 model_request_started  images=3 image_bytes=874702 text_characters=8414  timeout_seconds=600
              （此后每 1.5 秒一次 GET /api/jobs/4073b41e… 全部 200，前端轮询正常）
15:13:27.960 model_request_finished elapsed_seconds=219.37
15:13:27.965 job 4073b41e finished_at, status=cancelled, error=已取消请求
15:13:27.971 POST /api/jobs/4073b41e…/cancel 200
```

任务记录：`kind=extract status=cancelled profile_revision=9 sample=ff9f7447（INVOICE_23238.pdf, 3 页）`。

`model_request_finished` 位于 `provider.py` 的 **`finally:` 块**，成功与异常都会记录；而
`cancel_job` 调用 `tasks[identity].cancel()`，`CancelledError` 会穿过 `await client.post(...)`
触发该 `finally`。access log 在 handler 返回后才写，故 `.971` 晚于 `.960`。
**结论：219.37 秒是「被用户取消」的时刻，不是模型返回的时刻**，前端与后端都没有卡死。

### 1.2 复现：不取消，让它跑完

用同一 Profile（revision 9）与同一样本重跑，全程不取消：

```
15:26:07.168 model_request_started  images=3 image_bytes=874702 text_characters=13094 timeout_seconds=600
              （无任何 httpx 响应日志）
15:36:07.184 model_request_finished elapsed_seconds=600.01   → status=failed
              error=模型请求超时，可调整超时设置后重试
```

`elapsed_seconds=600.01` 精确等于 `asyncio.timeout(c.timeout)`，即 **600 秒响应超时真实触发**；
且**没有 `httpx: HTTP Request … 200 OK` 行**——连接建立了，但响应正文始终没来。

### 1.3 对照：健康时段同一代码路径

| 时刻 | 样本 | images | image_bytes | 提示词字符 | httpx 200 | 耗时 |
|---|---|---|---|---|---|---|
| 14:52:43 | 23227 | 3 | 869976 | 12229 | 有 | 63.14s |
| 14:53:46 | 23227 | 3 | 869976 | 7636 | 有 | 21.80s |
| 14:54:08 | 23227 | 3 | 869976 | 7636 | 有 | 23.70s |
| 15:09:48 | **23238** | 3 | 874702 | 8414 | **无** | 219.37s 被取消 |
| 15:26:07 | **23238** | 3 | 874702 | 13094 | **无** | 600.01s 超时 |

输入规模几乎相同（874702 vs 869976 字节），耗时差 10 倍以上，且失败两次都缺 httpx 响应行。

### 1.4 定位到网络层，并排除 max_tokens

15:38:56 对 23227 的一次请求直接建连失败，**新加的 ConnectTimeout 分支在真实环境首次命中**：

```
15:39:12.023 WARNING model_request_connect_timeout connect_timeout_seconds=15 error=ConnectTimeout:
15:39:12.023 model_request_finished elapsed_seconds=15.87
→ error=无法连接模型端点：建立连接超过 15 秒连接超时（企业代理握手缓慢或网络抖动），可重试
```

网络恢复后（`POST /api/settings/test` 返回 `{"ok":true}`，2.47 秒），**用完全相同的
Profile 与样本重跑 23238**：

```
job 5c1aaed0  started=07:58:12.195  finished=07:58:42.862  → 30.7 秒
status=completed  validation.status=valid  errors=0  warnings=0
result: AI_Invoice_Head / AI_Invoice_Detail / AI_POGR，输出合计约 742 字符
```

输出仅约 742 字符，**彻底排除「生成过长撞 `max_tokens=4096` 上限」的猜测**。
同一份文档、同一 Profile，从 >600 秒变成 30.7 秒 → 219s/600s 完全由企业代理阻塞造成。

### 1.5 由此暴露的两个真实代码缺陷

网络抖动无法由代码消除，但它暴露了两处本可自愈/可观测的缺陷：

| # | 位置 | 缺陷 |
|---|---|---|
| ① | `datara/app.py:311` `run_job` | 只调用 `completion()` **一次**。前置变更已把 15 秒建连失败正确归类为「无法连接模型端点…可重试」，优化器据此重试 1 次；但交互式测试提取没有任何重试，一次握手抖动就直接 `failed`——**错误文案自称「可重试」，却没有任何东西去重试** |
| ② | `datara/static/app.js:365` `pollJob` | `if(r.status==='running'){pollTimer=setTimeout(...);return;}` 在 running 时**直接 return，不调用 `render()`**。`renderTest()` 只显示静态的「处理中…」，既无已等待时长也无预期耗时。600 秒的静默等待与死机在界面上完全无法区分——这正是用户在 219 秒时取消的原因 |

另有 ③：重试预算的分类逻辑（瞬时错误词表 + 1/2/3 次数）内联在 `optimizer.py:651-658`，
无法被其他调用方复用，是缺陷①长期存在的结构性原因。

## 2. 改了哪些文件

| 文件 | 位置 | 改动性质 |
|---|---|---|
| `datara/provider.py` | `completion()` 之前新增 | `TRANSIENT_TOKENS` 常量、`classify_transient()`、`completion_retrying()` |
| `datara/optimizer.py` | L18 导入；`_complete_attempts()` 内 | 改为复用 `classify_transient()`，**行为完全等价** |
| `datara/app.py` | L25-26 导入；`run_job()` L310-316 | 两处模型调用改为 `completion_retrying()` |
| `datara/static/app.js` | `renderTest()` L200、toast L357、`pollJob()` L360-377 | 新增 `elapsedText()` / `updateJobElapsed()` 实时计时；提取提示文案补充耗时预期 |
| `tests/test_provider.py` | 导入 + 文件末尾 | 新增 9 个测试（6 个参数化 + 3 个重试行为） |
| `tests/test_app.py` | L123-128 打桩点；L142 后新增 | 修正打桩目标；新增 1 个接线测试 |
| `tests/test_analysis.py` | L171 | 修正打桩目标 |
| `README.md` | 「优化耗时与模型设置」新增 2 条 | 说明重试覆盖范围与等待计时 |
| `ARCHITECTURE.md` | L231、L268 | 重试预算单点定义；修正「there is no retry」的过时陈述 |
| `ARCHITECTURE.zh-CN.md` | L73、L189、L219 | 修正 provider「无重试」与任务「没有自动重试」两处过时陈述 |
| `docs/changes/2026-09-16-extract-job-retry-and-elapsed-feedback.zh-CN.md` | 新建 | 本文件 |

未改动：数据库/存储结构、API 契约、`Connection` 字段、重试次数上限策略（仍为 1/2/3）、超时数值。

## 3. 改动详情

### 3.1 `datara/provider.py` — 重试预算单点定义

```python
TRANSIENT_TOKENS = ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504",
                    "请求超时", "无法连接模型端点")


def classify_transient(message: str) -> tuple[bool, int]:
    """返回 (是否瞬时, 最大尝试次数)；次数含首次，故 1 表示「不重试」。"""
    if not any(token in message for token in TRANSIENT_TOKENS):
        return False, 1
    if "请求超时" in message:
        return True, 1     # 可能已耗尽 10–30 分钟预算，重试等于把卡顿翻倍
    if "无法连接" in message:
        return True, 2     # 只花 15 秒，企业代理重签 TLS 的典型症状
    return True, 3         # 网关 429/5xx
```

词表与 1/2/3 次数**逐字取自** `optimizer.py` 原内联实现，属纯提取，无语义变化。

`completion_retrying()` 是 `completion()` 的重试包装。`asyncio.CancelledError` 派生自
`BaseException` 而非 `ValueError`，因此用户取消仍立即中止，不会被静默重试。

### 3.2 `datara/optimizer.py` — 改为复用，行为等价

```python
            except ValueError as exc:
                message = str(exc)
                transient, limit = classify_transient(message)
                if not transient or attempts >= limit:
                    raise
```

### 3.3 `datara/app.py:run_job` — 交互任务获得同等恢复能力

```python
            if body.kind == "draft" and body.profile.reference_ids:
                raw = await completion_retrying(c, key, instructions, images,
                                                reference_text=analysis_references(body.profile))
            else:
                # 此前只调用一次，企业代理一次 15 秒建连超时就会让整个测试提取失败，
                # 尽管错误文案自称「可重试」。
                raw = await completion_retrying(c, key, instructions, images)
```

保持「仅在 `reference_text` 非空时才传该关键字参数」的原有调用形态，与既有测试替身签名兼容。

### 3.4 `datara/static/app.js` — 让等待可见

```js
function elapsedText(iso) {
  const s=Math.max(0,Math.round((Date.now()-new Date(iso).getTime())/1000));
  return s<60?`已等待 ${s} 秒`:`已等待 ${Math.floor(s/60)} 分 ${s%60} 秒`;
}
function updateJobElapsed(startedAt) {
  const el=document.getElementById('job-elapsed');
  if(el&&startedAt)el.textContent=elapsedText(startedAt);
}
```

`renderTest()` 的状态徽标在 running 时渲染 `处理中… <span id="job-elapsed"></span>`，
`pollJob` 每 1.5 秒只更新该节点文本。

**刻意不调用 `render()`**：整页重绘会闪烁，并抢走用户正在操作控件的焦点。
`started_at` 形如 `2026-09-16T07:58:12.195539+00:00`，`renderTest()` 原本就用
`new Date(r.started_at).toLocaleString('zh-CN')` 解析同一字段，故解析行为已有先例。

toast 文案由「AI 已开始提取」改为「AI 已开始提取；多页发票可能需要数分钟，计时器走动即表示仍在等待模型」。

## 4. 新增/调整测试

### 4.1 `tests/test_provider.py`

| 测试 | 断言 |
|---|---|
| `test_classify_transient_matches_the_optimizer_retry_budget`（6 组参数化） | 两类连接失败消息 → `(True,2)`；`模型请求超时…` → `(True,1)`；`HTTP 503` → `(True,3)`；`HTTP 401` 与「输出达到长度上限」→ `(False,1)`，确保永久性错误不消耗重试预算、不延迟真实报错 |
| `test_completion_retrying_recovers_from_a_transient_connect_timeout` | 首次 `ConnectTimeout`、第二次成功 → 返回内容且 `calls == 2` |
| `test_completion_retrying_does_not_repeat_a_full_response_timeout` | `ReadTimeout` → 抛「模型请求超时」且 `calls == 1` |
| `test_completion_retrying_gives_up_after_the_connection_budget` | 持续 `ConnectTimeout` → 抛「无法连接模型端点」且 `calls == 2`（预算有界） |

参数化用例中特意纳入本次新增的连接超时文案，断言其命中「无法连接」而非「请求超时」——
这正是前置变更修复的分类，两条变更由此互相锁定。

### 4.2 `tests/test_app.py`

新增 `test_extract_job_survives_a_transient_connection_failure`：**接线测试**，
打桩 `datara.provider.completion` 使其首次抛连接失败，经 `POST /api/jobs` 真实走一遍，
断言 `calls == 2` 且 `status == "completed"`。仅单测 `completion_retrying` 无法证明
`run_job` 真的用了它，故必须有此端到端断言。

### 4.3 两处打桩点必须同步修正（重要）

`run_job` 改调 `completion_retrying` 后，原有 `monkeypatch.setattr("datara.app.completion", …)`
**不再被命中**，测试替身被绕过、真的去解析 `example.test` 而抛 `ConnectError`，导致
`test_app.py::test_async_vision_job_records_validation_without_credentials` 与
`test_analysis.py::test_reference_analysis_job_end_to_end` 失败（`assert 'running' == 'completed'`）。

修法是把打桩目标改为 `datara.app.completion_retrying`，并给 `test_app.py` 的替身补上
`reference_text=""` 形参（`test_analysis.py` 的替身本就有）。**这是本次改动真实的接口位移，
必须同步；放宽断言或延长轮询都会掩盖问题。**

`datara.app.completion` 仍被 `test_connection()`（`POST /api/settings/test`）使用，故导入保留。

### 4.4 未打桩 `asyncio.sleep`

与前置变更同理：`datara.provider.asyncio` 即全局 `asyncio` 模块，在 `TestClient` 场景下打桩会波及
anyio 内部。真实退避仅 1 秒，可接受。

## 5. 验证

```
node --check datara\static\app.js                     → app.js syntax OK

.\.venv\Scripts\python.exe -m pytest -q               → 108 passed, 2 warnings in 20.07s
```

首次运行曾为 `2 failed, 105 passed`，即 4.3 所述两处打桩点；修正后全绿。
本机 `uv` 不在 PATH，故直接用 `.venv` 内 Python 调用 pytest，等价于 AGENTS.md 要求的 `uv run pytest -q`。

真实端点验证（非模拟）：

```
POST /api/settings/test                               → {"ok":true,"response":"{\"ok\":true}"}  2.47s
POST /api/jobs (23238.pdf, revision 9)                → job 5c1aaed0
   30.7 秒后 status=completed, validation.status=valid, errors=0, warnings=0
   结果覆盖 AI_Invoice_Head / AI_Invoice_Detail / AI_POGR
```

服务已重启加载新代码（PID `30880`，`http://127.0.0.1:8765`）；`has_key=True`、
`revision=9` 均在重启后保持（API Key 与 `DATARA_DATA_DIR` 来自 `.env`）。

## 6. 行为变化与风险

| 场景 | 改动前 | 改动后 |
|---|---|---|
| 测试提取遇到 15 秒建连抖动 | 立即 `failed`，零重试 | 自动重试 1 次（约 1 秒退避），第二次成功则任务完成 |
| 测试提取遇到网关 429/5xx | 立即 `failed` | 最多 3 次尝试 |
| 测试提取遇到响应阶段超时 | `failed`，零重试 | **不变**（仍不重试，避免把卡顿翻倍） |
| 用户点「取消」 | 立即中止 | **不变**（`CancelledError` 不是 `ValueError`，不进重试） |
| 优化器重试策略 | 内联 1/2/3 | **行为完全等价**，改为复用共享分类器 |
| 测试提取等待期间 | 静态「处理中…」，与死机无法区分 | 显示「已等待 N 秒 / M 分 N 秒」并持续走动 |

风险：低。重试仅覆盖瞬时错误且预算有界，最坏情况多花约 16 秒（15+1）或两次退避；
永久性错误（401、TLS 证书、输出超长）仍立即失败。前端仅新增一个只读文本节点，
不改变任何请求、状态机或数据契约。

## 7. 本次未解决 / 后续待办

1. **企业代理阻塞是根因，代码无法根治。** 23238 在健康网络下 30.7 秒完成，阻塞时 >600 秒且
   始终没有 httpx 响应行。建议向 IT 申请将 `*.aliyuncs.com` 加入 Zscaler bypass/exclusion 列表。
2. **`provider.py` 的 `"stream": False`**：改为流式是根治「长时间静默连接被中间设备掐断」的手段，
   也能提供真实增量进度，但属协议层改动，需单独评估与测试。
3. **前端发布条件与后端不一致**（沿袭前次）：前端要求 `run.baseline_version_id === activeVersionId`，
   后端只校验 `revision + fingerprint`。本次导入生产提示词后 Profile 推进到 revision 9，
   `ensure_prompt_version`（`optimizer.py:140-141`）因 imported 版本的 `profile_fingerprint`
   与新指纹失配，新建了 generated 版本 `fb8480f6` 顶替导入版本 `531c2ea6` 成为活动版本——
   **测试提取用的并不是导入的生产提示词**。这属产品决策，未在本次改动。
4. **`app.js` 的 `persistSettings()` 未发送 `enable_thinking`**（沿袭前次待办），每次保存设置会静默重置为 `null`。
5. **日志配置不在仓库内**：`run.command` 直接 `uvicorn` 启动时根 logger 停在 WARNING，
   `model_request_started/finished` 等 INFO 计时行会被丢弃，而这正是本次定位「600.01 秒且无 httpx
   响应行」所依赖的证据。当前由 `%TEMP%\datara_serve.py` 包装器（`logging.basicConfig(level=INFO)`
   + `log_config=None`）恢复，**该文件不在版本控制内**。建议后续把日志级别纳入应用或启动脚本。
