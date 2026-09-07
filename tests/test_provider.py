import json

import pytest
import asyncio
import httpx
import ssl

from datara.domain import new_profile
from datara.provider import Connection, completion, parse_draft


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
