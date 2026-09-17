import json

import pytest
import asyncio
import httpx
import ssl
from pydantic import ValidationError

from datara.domain import new_profile
from datara.provider import (Connection, classify_transient, completion, completion_retrying,
                             parse_draft)


def test_connection_allows_long_vision_timeout():
    assert Connection(timeout=1800).timeout == 1800
    with pytest.raises(ValidationError):
        Connection(timeout=1801)


def test_draft_is_additive_and_filters_system():
    p = new_profile()
    raw = json.dumps({"fields": [
        {"table_name": "AI_Document", "name": "amount", "description": "金额", "data_type": "Decimal", "evidence": "第1页"},
        {"table_name": "AI_Document", "name": "file_id", "data_type": "Integer"},
        {"table_name": "AI_Document", "name": "company_code"},
    ]})
    suggestions = parse_draft(raw, p)
    assert len(suggestions) == 1
    assert suggestions[0]["field"]["source"] == "AI"
    assert suggestions[0]["field"]["reviewed"] is False
    assert len(p.tables[0].fields) == 7


def test_draft_unknown_table_rejected():
    with pytest.raises(ValueError):
        parse_draft('{"fields":[{"table_name":"missing","name":"amount"}]}', new_profile())


def test_draft_parses_a_fenced_model_response():
    """The analysis model may wrap its JSON in a fence despite the no-Markdown instruction."""
    p = new_profile()
    raw = '```json\n' + json.dumps({"fields": [
        {"table_name": "AI_Document", "name": "amount", "description": "金额",
         "data_type": "Decimal", "evidence": "第1页"},
    ]}) + '\n```'
    suggestions = parse_draft(raw, p)
    assert len(suggestions) == 1
    assert suggestions[0]["field"]["name"] == "amount"


def test_compatible_http_request_contains_visual_input(tmp_path, monkeypatch):
    image = tmp_path / "1.jpg"
    image.write_bytes(b"example-jpeg-bytes")
    real_client = httpx.AsyncClient
    def handler(request):
        assert str(request.url) == "https://vision.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer local-test-key"
        payload = json.loads(request.content)
        assert payload["model"] == "company-vision-model"
        assert payload["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
    monkeypatch.setattr("datara.provider.httpx.AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    result = asyncio.run(completion(Connection(base_url="https://vision.test/v1",model="company-vision-model"), "local-test-key", "任务", [image]))
    assert result == "{}"


def test_reference_uses_one_text_part_for_enterprise_gateway_compatibility(tmp_path, monkeypatch):
    image = tmp_path / "1.jpg"
    image.write_bytes(b"example-jpeg-bytes")
    real_client = httpx.AsyncClient
    def handler(request):
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        assert [part["type"] for part in content] == ["text", "image_url"]
        assert "REFERENCE_ONLY" in content[0]["text"]
        assert "BEGIN USER REFERENCE DATA" in content[0]["text"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
    monkeypatch.setattr("datara.provider.httpx.AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    result = asyncio.run(completion(Connection(base_url="https://vision.test/v1", model="company-model"),
                                    "local-test-key", "任务", [image], "REFERENCE_ONLY"))
    assert result == "{}"


def test_reference_gateway_error_is_actionable_without_echoing_response(monkeypatch):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda r: httpx.Response(400, text="secret provider request details"))
    monkeypatch.setattr("datara.provider.httpx.AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    with pytest.raises(ValueError, match="参考文字 14 字符") as error:
        asyncio.run(completion(Connection(base_url="https://vision.test/v1", model="test"),
                               "private-key", "任务", [], "REFERENCE_ONLY"))
    assert "secret provider" not in str(error.value)


@pytest.mark.parametrize("status", [401, 429, 500])
def test_provider_errors_do_not_echo_sensitive_response(monkeypatch, status):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda r: httpx.Response(status, text="secret provider request details"))
    monkeypatch.setattr("datara.provider.httpx.AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    with pytest.raises(ValueError, match=f"HTTP {status}") as error:
        asyncio.run(completion(Connection(base_url="https://vision.test/v1", model="test"), "private-key", "任务", []))
    assert "secret provider" not in str(error.value)


def test_certificate_failure_is_actionable_and_private(monkeypatch):
    real_client = httpx.AsyncClient
    def handler(request):
        try:
            raise ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED private detail")
        except ssl.SSLCertVerificationError as cause:
            raise httpx.ConnectError("private proxy detail") from cause
    def client(**kwargs):
        assert kwargs['trust_env'] is True
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr('datara.provider.httpx.AsyncClient', client)
    with pytest.raises(ValueError, match='SSL_CERT_FILE') as error:
        asyncio.run(completion(Connection(), 'private-key', '任务', []))
    assert 'private' not in str(error.value)


def test_invalid_ca_file_has_actionable_error(monkeypatch, tmp_path):
    monkeypatch.setenv('SSL_CERT_FILE', str(tmp_path / 'missing.pem'))
    with pytest.raises(ValueError, match='无法加载 TLS 配置'):
        asyncio.run(completion(Connection(), 'private-key', '任务', []))


@pytest.mark.parametrize('model,host,explicit,expected', [
    ('qwen3.8-max-0902', 'dashscope.aliyuncs.com', None, False),
    ('qwen3.8-max-0902', 'dashscope.aliyuncs.com', True, True),
    ('qwen3.8-max-0902', 'enterprise.test', None, None),
    ('other-model', 'dashscope.aliyuncs.com', None, None),
])
def test_thinking_switch_is_scoped_to_supported_provider(monkeypatch, model, host, explicit, expected):
    real_client = httpx.AsyncClient
    def handler(request):
        payload = json.loads(request.content)
        if expected is None:
            assert 'enable_thinking' not in payload
        else:
            assert payload['enable_thinking'] is expected
        return httpx.Response(200, json={'choices': [{'message': {'content': '{}'}}]})
    monkeypatch.setattr('datara.provider.httpx.AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    asyncio.run(completion(Connection(base_url=f'https://{host}/v1', model=model, enable_thinking=explicit), 'test-key', 'task', []))


def test_total_deadline_stops_a_response_that_keeps_sending_chunks(monkeypatch):
    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.005)
                yield b' '
    real_client = httpx.AsyncClient
    monkeypatch.setattr('datara.provider.httpx.AsyncClient', lambda **kw: real_client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Trickle())), **kw))
    connection = Connection().model_copy(update={'timeout': 0.03})
    with pytest.raises(ValueError, match='请求超时'):
        asyncio.run(completion(connection, 'test-key', 'task', []))


def test_connect_timeout_is_reported_as_a_connection_failure(monkeypatch):
    """The fixed 15 second connect timeout must not be labelled a model response timeout.

    The optimizer grants a retry only for messages containing "无法连接" and suppresses
    it for "请求超时", so both assertions below guard the retry budget as well as the
    wording shown to the user.
    """
    real_client = httpx.AsyncClient

    def handler(request):
        raise httpx.ConnectTimeout('timed out')

    monkeypatch.setattr('datara.provider.httpx.AsyncClient', lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(ValueError) as error:
        asyncio.run(completion(Connection(), 'test-key', 'task', []))
    message = str(error.value)
    assert '无法连接模型端点' in message
    assert '15 秒连接超时' in message
    assert '请求超时' not in message


def test_read_timeout_is_still_reported_as_a_request_timeout(monkeypatch):
    """Only the connect phase is reclassified; a slow response keeps the timeout wording."""
    real_client = httpx.AsyncClient

    def handler(request):
        raise httpx.ReadTimeout('timed out')

    monkeypatch.setattr('datara.provider.httpx.AsyncClient', lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(ValueError, match='模型请求超时'):
        asyncio.run(completion(Connection(), 'test-key', 'task', []))


@pytest.mark.parametrize("message,expected", [
    # The reclassified 15 second connect failure: cheap to retry, so one extra attempt.
    ("无法连接模型端点：建立连接超过 15 秒连接超时（企业代理握手缓慢或网络抖动），可重试", (True, 2)),
    ("无法连接模型端点，请检查网络和 API 地址", (True, 2)),
    # A response timeout already burned the whole configured timeout; never repeat it.
    ("模型请求超时，可调整超时设置后重试", (True, 1)),
    ("模型接口返回 HTTP 503。请检查地址、模型、图片能力及账号额度。", (True, 3)),
    # Permanent problems must not consume the retry budget or delay the real error.
    ("模型接口返回 HTTP 401：API Key 认证失败。", (False, 1)),
    ("模型输出达到长度上限；请增加最大输出长度或减少样本范围", (False, 1)),
])
def test_classify_transient_matches_the_optimizer_retry_budget(message, expected):
    """The shared classifier must reproduce the budget the optimizer always used."""
    assert classify_transient(message) == expected


def test_completion_retrying_recovers_from_a_transient_connect_timeout(monkeypatch):
    """One proxy blip must not fail an interactive 测试提取 run.

    run_job used to call completion() exactly once, so a 15 second ConnectTimeout
    aborted the job even though its own error text told the user it was retryable.
    """
    real_client = httpx.AsyncClient
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout('timed out')
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                      "message": {"content": '{"ok":true}'}}]})

    monkeypatch.setattr('datara.provider.httpx.AsyncClient', lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    assert asyncio.run(completion_retrying(Connection(), 'test-key', 'task', [])) == '{"ok":true}'
    assert calls["n"] == 2


def test_completion_retrying_does_not_repeat_a_full_response_timeout(monkeypatch):
    """Retrying a read timeout would make one sample look stuck for twice the timeout."""
    real_client = httpx.AsyncClient
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ReadTimeout('timed out')

    monkeypatch.setattr('datara.provider.httpx.AsyncClient', lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(ValueError, match='模型请求超时'):
        asyncio.run(completion_retrying(Connection(), 'test-key', 'task', []))
    assert calls["n"] == 1


def test_completion_retrying_gives_up_after_the_connection_budget(monkeypatch):
    """Connection failures are bounded: two attempts total, then the real error surfaces."""
    real_client = httpx.AsyncClient
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectTimeout('timed out')

    monkeypatch.setattr('datara.provider.httpx.AsyncClient', lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(ValueError, match='无法连接模型端点'):
        asyncio.run(completion_retrying(Connection(), 'test-key', 'task', []))
    assert calls["n"] == 2


def test_interactive_deadline_includes_retry_backoff(monkeypatch):
    async def busy(*args, **kwargs):
        raise ValueError("模型接口返回 HTTP 429")
    monkeypatch.setattr("datara.provider.completion", busy)
    with pytest.raises(ValueError, match="含重试"):
        asyncio.run(completion_retrying(Connection().model_copy(update={"timeout": 0.03}),
                                        "key", "task", []))


def test_cancellation_is_never_retried(monkeypatch):
    calls = []
    async def cancelled(*args, **kwargs):
        calls.append(1)
        raise asyncio.CancelledError()
    monkeypatch.setattr("datara.provider.completion", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(completion_retrying(Connection(), "key", "task", []))
    assert len(calls) == 1


@pytest.mark.parametrize("ending,expected_error", [
    ('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n', None),
    ('', "流式响应中断"),
    ('data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n', "长度上限"),
])
def test_thinking_stream_reassembles_answer_without_reasoning(monkeypatch, ending, expected_error):
    real_client = httpx.AsyncClient
    def handler(request):
        payload = json.loads(request.content)
        assert payload["enable_thinking"] is True
        assert payload["stream"] is True
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "private reasoning"}}]},
            {"choices": [{"delta": {"content": '{"ok":'}}]},
            {"choices": [{"delta": {"content": 'true}'}}]},
        ]
        text = ": heartbeat\n\n" + "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + ending
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
    monkeypatch.setattr("datara.provider.httpx.AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    call = completion(Connection(enable_thinking=True), "key", "task", [])
    if expected_error:
        with pytest.raises(ValueError, match=expected_error):
            asyncio.run(call)
    else:
        assert asyncio.run(call) == '{"ok":true}'


@pytest.mark.parametrize("body", [None, [], {"choices": None}, {"choices": [{"message": None}]},
                                  {"choices": [{"message": {"content": ""}}]}])
def test_malformed_success_response_has_actionable_error(monkeypatch, body):
    real_client = httpx.AsyncClient
    monkeypatch.setattr("datara.provider.httpx.AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=json.dumps(body))), **kw))
    with pytest.raises(ValueError, match="模型"):
        asyncio.run(completion(Connection(), "key", "task", []))


def test_network_log_does_not_include_proxy_credentials(monkeypatch, caplog):
    real_client = httpx.AsyncClient
    def handler(request):
        raise httpx.ConnectError("http://user:secret@proxy.test private-key")
    monkeypatch.setattr("datara.provider.httpx.AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(ValueError):
        asyncio.run(completion(Connection(), "private-key", "task", []))
    assert "secret" not in caplog.text and "private-key" not in caplog.text


def test_reasoning_only_stream_is_not_a_success(monkeypatch):
    real_client = httpx.AsyncClient
    chunk = {"choices": [{"delta": {"reasoning_content": "reasoning only"}, "finish_reason": "stop"}]}
    text = "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n"
    monkeypatch.setattr("datara.provider.httpx.AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda r: httpx.Response(
            200, text=text, headers={"content-type": "text/event-stream"})), **kw))
    with pytest.raises(ValueError, match="未返回最终答案"):
        asyncio.run(completion(Connection(enable_thinking=True), "key", "task", []))
