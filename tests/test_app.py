import io
import json
import logging
import os
import time
import hashlib
import httpx
import zipfile

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from PIL import Image

from datara.app import (configure_logging, create_app, load_runtime_environment,
                        resolve_log_level)
from datara.domain import FieldDef, new_profile
from datara.importer import inspect_workbook, parse_fields


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        yield c


@pytest.fixture
def restore_root_logger():
    """Snapshot root logger state so level tests cannot leak into the rest of the suite."""
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    yield root
    root.setLevel(level)
    root.handlers[:] = handlers


def test_project_env_loads_once_without_overriding_environment(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "DATARA_API_KEY=local-project-key\nDATARA_DATA_DIR=local-data\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DATARA_API_KEY", raising=False)
    monkeypatch.setenv("DATARA_DATA_DIR", "deployment-data")

    load_runtime_environment(tmp_path)

    assert os.environ["DATARA_API_KEY"] == "local-project-key"
    assert os.environ["DATARA_DATA_DIR"] == "deployment-data"


@pytest.mark.parametrize("raw,expected", [
    ("DEBUG", "DEBUG"),
    ("info", "INFO"),          # 大小写不敏感，.env 里手写小写也应生效
    ("  Warning  ", "WARNING"),  # 容忍空白
    ("ERROR", "ERROR"),
    ("CRITICAL", "CRITICAL"),
    (None, "WARNING"),         # 未设置 → 默认，保持改动前的行为
    ("", "WARNING"),           # .env 里留空 → 默认
    ("verbose", "WARNING"),    # 拼错 → 默认，而不是启动崩溃
])
def test_resolve_log_level_accepts_known_names_and_falls_back(raw, expected):
    assert resolve_log_level(raw) == expected


def test_configure_logging_keeps_warning_by_default(monkeypatch, restore_root_logger):
    """默认必须是 WARNING：否则本次改动会让所有部署突然开始刷屏。"""
    monkeypatch.delenv("DATARA_LOG_LEVEL", raising=False)
    assert configure_logging() == "WARNING"
    assert restore_root_logger.level == logging.WARNING


def test_configure_logging_enables_provider_diagnostics_at_info(monkeypatch, restore_root_logger):
    """DATARA_LOG_LEVEL=INFO 必须让 datara.provider 的 INFO 记录真正可见。

    回归防护：uvicorn 的 --log-level 只对 uvicorn.error/access/asgi 调 setLevel
    (uvicorn/config.py:413-420)，从不配置 root。缺了 configure_logging() 时这些计时行会
    落到 logging.lastResort(WARNING) 被丢弃 —— 这正是 600 秒代理阻塞当初无法定位的原因。
    """
    monkeypatch.setenv("DATARA_LOG_LEVEL", "INFO")
    assert configure_logging() == "INFO"
    assert logging.getLogger("datara.provider").getEffectiveLevel() == logging.INFO

    seen = []
    handler = logging.Handler()
    handler.emit = seen.append
    restore_root_logger.addHandler(handler)
    try:
        logging.getLogger("datara.provider").info("model_request_started")
        logging.getLogger("datara.provider").debug("below-threshold")
    finally:
        restore_root_logger.removeHandler(handler)

    # 只断言 INFO 那条：DEBUG 仍应低于阈值，证明级别是 INFO 而不是被放成 DEBUG。
    assert [r.getMessage() for r in seen] == ["model_request_started"]


def test_configure_logging_does_not_duplicate_root_handlers(monkeypatch, restore_root_logger):
    """重复调用不得叠加 handler，否则每行日志会被打印多次。"""
    monkeypatch.setenv("DATARA_LOG_LEVEL", "INFO")
    configure_logging()
    first = len(restore_root_logger.handlers)
    configure_logging()
    configure_logging()
    assert len(restore_root_logger.handlers) == first


def test_configure_logging_warns_about_an_unusable_level(monkeypatch, restore_root_logger, caplog):
    """拼错的级别要给出可见提示，而不是静默按 WARNING 运行让人以为设置生效了。"""
    monkeypatch.setenv("DATARA_LOG_LEVEL", "verbose")
    with caplog.at_level(logging.WARNING, logger="datara.app"):
        assert configure_logging() == "WARNING"
    assert any("DATARA_LOG_LEVEL" in r.getMessage() and "verbose" in r.getMessage()
               for r in caplog.records)


def test_save_reload_conflict_and_export(client):
    p = client.post("/api/demo/invoice").json()
    saved = client.post("/api/profiles/save", json=p).json()
    assert saved["revision"] == 1
    loaded = client.get("/api/profiles/" + saved["id"]).json()
    assert loaded == saved
    assert client.post("/api/profiles/save", json=p).status_code == 409
    preview = client.post("/api/preview", json=saved).json()
    assert preview["errors"] == []
    assert client.post("/api/export", json=saved).content.startswith(b"PK")
    saved["tables"][0]["fields"][0]["name"] = "renamed_field"
    assert client.post("/api/export", json=saved).status_code == 409


def test_delete_profile_removes_profile_and_derived_records(client, tmp_path):
    """删除 Profile 必须级联清理它独有的记录，同时保留共享的样张上传。"""
    saved = client.post("/api/demo/invoice").json()      # 示例 Profile 可直接导出，便于验证 exports 清理
    saved = client.post("/api/profiles/save", json=saved).json()
    identity = saved["id"]
    # 导入提示词会额外产生一个 imported 版本，正好验证 prompt_versions 被一并清理
    imported = client.post(f"/api/profiles/{identity}/prompt-versions/import", json={
        "expected_profile_revision": saved["revision"],
        "prompt_text": "旧系统的提示词正文，用于确认导入版本会随 Profile 一起被清理。"})
    assert imported.status_code == 200, imported.text
    # 样张是共享工作区对象：删除 Profile 后仍必须留在磁盘上
    sample_dir = tmp_path / "samples" / "kept-sample"
    sample_dir.mkdir(parents=True)
    (sample_dir / "meta.json").write_text(
        json.dumps({"id": "kept-sample", "name": "样本.pdf", "pages": 1, "suffix": ".pdf"}), encoding="utf-8")
    saved["sample_ids"] = ["kept-sample"]
    saved = client.post("/api/profiles/save", json=saved).json()
    assert client.post("/api/export", json=saved).status_code == 200
    (tmp_path / "tests" / "job-1.json").write_text(json.dumps(
        {"id": "job-1", "profile_id": identity, "status": "done", "started_at": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8")

    response = client.delete(f"/api/profiles/{identity}")
    assert response.status_code == 200, response.text
    removed = response.json()["removed"]
    assert removed["profiles"] == 1
    assert removed["optimizer/prompt_versions"] >= 2      # generated_baseline + imported
    assert removed["optimizer/prompt_states"] == 1
    assert removed["tests"] == 1
    assert removed["exports"] == 1
    assert not (tmp_path / "profiles" / f"{identity}.json").exists()
    assert not list((tmp_path / "exports").glob("*.zip"))  # 导出压缩包一并清理
    assert (sample_dir / "meta.json").exists()            # 上传的样张不被删除
    assert identity not in [p["id"] for p in client.get("/api/profiles").json()]
    assert client.get(f"/api/profiles/{identity}").status_code == 404
    assert client.get(f"/api/profiles/{identity}/prompt-versions").status_code == 404


def test_delete_profile_guards_unknown_unsafe_and_running(client, tmp_path):
    """未知 ID → 404，不安全 ID → 400，仍有优化任务在跑 → 409 且不得删掉任何数据。"""
    assert client.delete("/api/profiles/does-not-exist").status_code == 404
    assert client.delete("/api/profiles/bad%20id").status_code == 400

    saved = client.post("/api/profiles/save", json=new_profile("运行中", "AI_Busy").model_dump()).json()
    run_path = tmp_path / "optimizer" / "runs" / "run-1.json"
    run_path.write_text(json.dumps({"id": "run-1", "profile_id": saved["id"], "status": "optimizing",
                                    "created_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
    assert client.delete(f"/api/profiles/{saved['id']}").status_code == 409
    assert (tmp_path / "profiles" / f"{saved['id']}.json").exists()

    run_path.write_text(json.dumps({"id": "run-1", "profile_id": saved["id"], "status": "completed",
                                    "created_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
    response = client.delete(f"/api/profiles/{saved['id']}")
    assert response.status_code == 200, response.text
    assert response.json()["removed"]["optimizer/runs"] == 1
    assert not run_path.exists()


def test_settings_never_persist_or_return_api_key(client, tmp_path):
    data = {"base_url": "https://example.test/v1", "model": "test-vision", "api_key": "test-secret-token"}
    response = client.post("/api/settings", json=data)
    assert response.status_code == 200
    assert "test-secret-token" not in response.text
    assert "test-secret-token" not in (client.app.state.store.root / "connection.json").read_text()
    assert client.get("/api/settings").json()["has_key"] is True
    assert client.post("/api/settings", json={**data, "clear_key": True}).json()["has_key"] is False


def test_reject_cross_origin_and_path_traversal(client):
    assert client.post("/api/profiles/new", headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.get("/api/profiles/not.valid.id").status_code == 400
    assert client.get("/api/health", headers={"Host": "evil.test"}).status_code == 403


def image_bytes():
    out = io.BytesIO()
    Image.new("RGB", (120, 100), "white").save(out, "PNG")
    return out.getvalue()


def test_image_upload_preview_and_invalid_file(client):
    response = client.post("/api/samples", files={"file": ("sample.png", image_bytes(), "image/png")})
    assert response.status_code == 200
    sample = response.json()
    assert sample["pages"] == 1
    assert client.get(f"/api/samples/{sample['id']}/pages/1").headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/samples/{sample['id']}/pages/2").status_code == 404
    assert client.post("/api/samples", files={"file": ("bad.pdf", b"not a pdf", "application/pdf")}).status_code == 400


def spreadsheet():
    w = Workbook(); s = w.active
    s.title = "Mapping"
    s.append(["TableName", "TableLevel", "ForeignKeyField", "ColumnName", "SampleData", "DataType", "ChoiceValues", "IsRequired", "Source", "HeadDisplay", "FieldOrder"])
    s.append(["AI_Invoice_Head", 1, None, "invoice_number", "0012", "String", None, "true", "AI", 1, 2])
    s.append(["AI_Invoice_Head", 1, None, "company_code", None, "String", None, None, "AI", None, 2])
    s.append(["AI_POGR", 2, "AI_Invoice_Head", "po_number", "09300000000", "String", None, None, "AI", None, 1])
    s.append(["AI_POGR", 2, "AI_Invoice_Head", "head_id", None, "Integer", None, None, "AI", None, 2])
    out=io.BytesIO(); w.save(out); return out.getvalue()


def test_mapping_import_repairs_legacy_and_preserves_identifiers(client):
    info=client.post("/api/import/inspect", files={"file": ("mapping.xlsx", spreadsheet())}).json()
    result=client.post("/api/import/apply", json={"import_id":info["import_id"],"sheet":"Mapping"}).json()
    p=result["profile"]
    assert result["notes"]
    assert p["tables"][0]["fields"][0]["sample_data"] == "0012"
    assert p["tables"][0]["fields"][1]["source"] == "System"
    assert next(f for f in p["tables"][1]["fields"] if f["name"] == "head_id")["source"] == "System"
    assert client.post("/api/preview", json=p).json()["errors"] == []


def test_invoice_item_is_ai_in_demo_outputs_and_validation(client):
    from datara.domain import Profile, json_structure, validate_profile, validate_result
    from datara.generators import mapping_rows, prompt
    from datara.provider import parse_analysis

    profile = Profile.model_validate(client.post("/api/demo/invoice").json())
    detail = next(t for t in profile.tables if t.name == "AI_Invoice_Detail")
    item = next(f for f in detail.fields if f.name == "item")
    assert item.source == "AI" and item.data_type == "String"
    assert not validate_profile(profile)["errors"]
    assert any(r[0] == detail.name and r[3] == "item" and r[8] == "AI" for r in mapping_rows(profile))
    assert item.extraction in prompt(profile)
    value = json_structure(profile)
    value[detail.name][0]["item"] = "1"
    assert validate_result(profile, value)["status"] == "valid"
    value[detail.name][0]["file_id"] = 1
    assert any("file_id：额外字段" in e for e in validate_result(profile, value)["errors"])
    proposal = parse_analysis(json.dumps({"fields": [{"table_name": detail.name,
        "name": "item", "data_type": "String", "extraction": item.extraction}]}), profile)
    assert proposal["suggestions"][0]["field"]["name"] == "item"


@pytest.mark.parametrize("source", ["AI", "Manual", "System"])
def test_import_preserves_invoice_item_source(source):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["TableName", "TableLevel", "ForeignKeyField", "ColumnName", "DataType", "Source"])
    sheet.append(["AI_Invoice_Head", 1, None, "invoice_number", "String", "AI"])
    sheet.append(["AI_Invoice_Detail", 2, "AI_Invoice_Head", "item", "String", source])
    output = io.BytesIO()
    workbook.save(output)
    profile, notes = parse_fields(output.getvalue(), sheet.title)
    item = next(f for t in profile.tables if t.name == "AI_Invoice_Detail" for f in t.fields if f.name == "item")
    assert item.source == source
    assert not any("item" in note and "System" in note for note in notes)


def test_plain_excel_column_selection():
    w=Workbook();s=w.active;s.title="业务清单";s.append(["名称","说明"]);s.append(["account_no","账号"])
    out=io.BytesIO();w.save(out)
    p, notes=parse_fields(out.getvalue(),"业务清单",{"name":"名称","description":"说明"})
    assert p.tables[0].fields[0].description == "账号"
    assert not p.tables[0].fields[0].reviewed


def test_unknown_mapping_source_is_not_marked_reviewed():
    from openpyxl import load_workbook
    w = load_workbook(io.BytesIO(spreadsheet()))
    w.active["I2"] = "Unknown"
    out = io.BytesIO(); w.save(out)
    p, notes = parse_fields(out.getvalue(), "Mapping")
    assert p.tables[0].fields[0].reviewed is False
    assert any("请审核" in note for note in notes)


def test_async_vision_job_records_validation_without_credentials(client, monkeypatch):
    async def fake_completion(c,key,instructions,images,reference_text=""):
        assert key == "test-only-key"
        assert images and images[0].exists()
        assert "file_id" not in instructions
        return '{"AI_Document":{"amount":null}}'
    monkeypatch.setattr("datara.app.completion_retrying",fake_completion)
    client.post("/api/settings",json={"base_url":"https://example.test/v1","model":"test","api_key":"test-only-key"})
    sample=client.post("/api/samples",files={"file":("a.png",image_bytes())}).json()
    p=new_profile();p.tables[0].fields.append(FieldDef(name="amount",data_type="Decimal",is_required=True))
    job=client.post("/api/jobs",json={"profile":p.model_dump(),"sample_id":sample["id"],"kind":"extract"}).json()
    for _ in range(30):
        result=client.get("/api/jobs/"+job["id"]).json()
        if result["status"] != "running": break
        time.sleep(.01)
    assert result["status"] == "completed"
    assert result["validation"]["status"] == "valid"
    assert result["validation"]["warnings"]
    assert "test-only-key" not in client.app.state.store.path("tests",job["id"]).read_text()


def test_extract_job_survives_a_transient_connection_failure(client, monkeypatch):
    """Wiring check: run_job must call the model through the retrying wrapper.

    A single 15 second connect timeout from a corporate proxy used to fail the whole
    测试提取 run, even though the message shown to the user said it was retryable.
    """
    calls={"n":0}
    async def flaky(c,key,instructions,images,reference_text=""):
        calls["n"]+=1
        if calls["n"]==1:
            raise ValueError("无法连接模型端点：建立连接超过 15 秒连接超时（企业代理握手缓慢或网络抖动），可重试")
        return '{"AI_Document":{"amount":null}}'
    monkeypatch.setattr("datara.provider.completion",flaky)
    client.post("/api/settings",json={"base_url":"https://example.test/v1","model":"test","api_key":"test-only-key"})
    sample=client.post("/api/samples",files={"file":("a.png",image_bytes())}).json()
    p=new_profile();p.tables[0].fields.append(FieldDef(name="amount",data_type="Decimal",is_required=True))
    job=client.post("/api/jobs",json={"profile":p.model_dump(),"sample_id":sample["id"],"kind":"extract"}).json()
    result={}
    for _ in range(120):
        result=client.get("/api/jobs/"+job["id"]).json()
        if result["status"] != "running": break
        time.sleep(.05)
    assert calls["n"] == 2
    assert result["status"] == "completed"


@pytest.mark.parametrize("thinking", [True, False, None])
def test_model_routes_share_transport_retries_and_thinking(client, monkeypatch, thinking):
    """Exercise the real provider through jobs, optimizer phases and connection diagnostics."""
    profile = new_profile("Transport regression")
    field = FieldDef(name="amount", data_type="Decimal")
    profile.tables[0].fields.append(field)
    profile = client.post("/api/profiles/save", json=profile.model_dump()).json()
    sample = client.post("/api/samples", files={"file": ("sample.png", image_bytes())}).json()
    seen, failures = [], set()
    real_client = httpx.AsyncClient

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert request.headers["authorization"] == "Bearer regression-key"
        if thinking is None:
            assert "enable_thinking" not in payload
        else:
            assert payload["enable_thinking"] is thinking
        assert payload["stream"] is (thinking is True)
        instructions = payload["messages"][0]["content"]
        identity = (payload["model"], instructions)
        if identity not in failures:
            failures.add(identity)
            raise httpx.ConnectTimeout("proxy handshake")
        if "Datara 字段级提取提示词优化器" in instructions:
            assert payload["model"] == "analysis-model"
            # Editing settings during analysis must not change later evaluation calls.
            client.app.state.api_key = "replacement-key"
            client.app.state.connection.enable_thinking = not thinking
            answer = {"analyses": [], "candidate_rules": [{
                "field_id": field.id, "old_rule_hash": hashlib.sha256(b"").hexdigest(),
                "new_rule": "提取总金额，排除小计金额。", "reason": "区分总计与小计。",
            }]}
        elif "Datara 单据分析与提取配置专家" in instructions:
            assert payload["model"] == "analysis-model"
            answer = {"fields": []}
        elif "仅回复 JSON" in instructions:
            answer = {"ok": True}
        else:
            assert payload["model"] == "extract-model"
            answer = {"AI_Document": {"amount": 100 if "排除小计金额" in instructions else 90}}
        raw = json.dumps(answer, ensure_ascii=False)
        if thinking:
            chunk = {"choices": [{"delta": {"content": raw}, "finish_reason": "stop"}]}
            return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": raw}}]})

    monkeypatch.setattr("datara.provider.httpx.AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    settings = {"base_url": "https://example.test/v1", "model": "default-model",
                "optimizer_model": "analysis-model", "extraction_model": "extract-model",
                "enable_thinking": thinking, "api_key": "regression-key"}
    assert client.post("/api/settings", json=settings).json()["enable_thinking"] is thinking
    assert client.get("/api/settings").json()["enable_thinking"] is thinking
    stored = client.app.state.store.root / "connection.json"
    assert json.loads(stored.read_text())["enable_thinking"] is thinking
    assert "regression-key" not in stored.read_text()

    for kind in ("extract", "draft"):
        job = client.post("/api/jobs", json={"profile": profile, "sample_id": sample["id"], "kind": kind}).json()
        for _ in range(300):
            result = client.get("/api/jobs/" + job["id"]).json()
            if result["status"] != "running":
                break
            time.sleep(.02)
        assert result["status"] == "completed", result
        assert result["model"] == ("extract-model" if kind == "extract" else "analysis-model")
        assert result["enable_thinking"] is thinking

    case = client.post(f"/api/optimizer/profiles/{profile['id']}/test-cases", json={
        "sample_id": sample["id"], "name": "total", "dataset_role": "failure",
        "ground_truth": {"AI_Document": {"amount": 100}},
    }).json()
    run = client.post("/api/optimizer/runs", json={
        "profile_id": profile["id"], "profile_revision": profile["revision"],
        "selected_field_ids": [field.id], "failure_test_case_ids": [case["id"]],
        "settings": {"max_iterations": 1},
    }).json()
    for _ in range(500):
        result = client.get("/api/optimizer/runs/" + run["id"]).json()
        if result["status"] not in {"queued", "baselining", "optimizing", "validating"}:
            break
        time.sleep(.02)
    assert result["status"] == "completed", result
    records = [client.app.state.store.read_json("optimizer/extractions", identity)
               for identity in result["extraction_ids"]]
    assert {r["phase"] for r in records} == {"baseline", "candidate", "final_validation"}
    assert all(r["model_id"] == "extract-model" and r["enable_thinking"] is thinking for r in records)
    client.post("/api/settings", json=settings)
    diagnostic = client.post("/api/settings/test").json()
    assert diagnostic["ok"] is True
    assert {r["model"] for r in diagnostic["results"]} == {"default-model", "analysis-model", "extract-model"}
    assert len(seen) > len(failures)  # Every distinct request survived a transport failure.


def test_interactive_extract_uses_the_active_imported_prompt(client, monkeypatch):
    text = 'Published prompt: preserve this exact instruction. Return JSON {"AI_Document":{"amount":null}}.'
    profile = new_profile()
    profile.tables[0].fields.append(FieldDef(name="amount", data_type="Decimal"))
    profile = client.post("/api/profiles/save", json=profile.model_dump()).json()
    imported = client.post(f"/api/profiles/{profile['id']}/prompt-versions/import", json={
        "prompt_text": text, "expected_profile_revision": profile["revision"],
    }).json()
    profile = imported["profile"]
    version = client.get("/api/prompt-versions/" + imported["id"]).json()
    async def check(c, key, instructions, images):
        assert instructions == version["rendered_prompt"]
        return '{"AI_Document":{"amount":null}}'
    monkeypatch.setattr("datara.app.completion_retrying", check)
    client.post("/api/settings", json={"api_key": "test-key"})
    sample = client.post("/api/samples", files={"file": ("sample.png", image_bytes())}).json()
    job = client.post("/api/jobs", json={"profile": profile, "sample_id": sample["id"], "kind": "extract"}).json()
    for _ in range(100):
        result = client.get("/api/jobs/" + job["id"]).json()
        if result["status"] != "running":
            break
        time.sleep(.01)
    assert result["status"] == "completed", result
    assert result["prompt_version_id"] == version["id"]
    assert result["prompt_hash"] == version["prompt_hash"]
    preview = client.post("/api/preview", json=profile).json()
    assert preview["prompt"] == version["rendered_prompt"]
    archive = client.post("/api/export", json=profile)
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as exported:
        assert exported.read("extraction_prompt.txt").decode() == preview["prompt"]
    profile["document_rules"] = "未保存的新规则"
    response = client.post("/api/jobs", json={"profile": profile, "sample_id": sample["id"], "kind": "extract"})
    assert response.status_code == 409


def test_extract_job_parses_a_fenced_model_response(client, monkeypatch):
    """A model response wrapped in a Markdown fence must not fail 测试提取.

    Regression: fenced responses parsed to {}, the page showed
    'Expecting value: line 1 column 1 (char 0)' and an empty result even though the
    extraction itself had succeeded.
    """
    async def fenced(c,key,instructions,images,reference_text=""):
        return '```json\n{"AI_Document":{"amount":null}}\n```'
    monkeypatch.setattr("datara.app.completion_retrying",fenced)
    client.post("/api/settings",json={"base_url":"https://example.test/v1","model":"test","api_key":"test-only-key"})
    sample=client.post("/api/samples",files={"file":("a.png",image_bytes())}).json()
    p=new_profile();p.tables[0].fields.append(FieldDef(name="amount",data_type="Decimal",is_required=True))
    job=client.post("/api/jobs",json={"profile":p.model_dump(),"sample_id":sample["id"],"kind":"extract"}).json()
    result={}
    for _ in range(30):
        result=client.get("/api/jobs/"+job["id"]).json()
        if result["status"] != "running": break
        time.sleep(.01)
    assert result["status"] == "completed"
    assert result["result"] == {"AI_Document": {"amount": None}}
    assert result["validation"]["status"] == "valid"
