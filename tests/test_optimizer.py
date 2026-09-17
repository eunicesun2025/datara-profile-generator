import asyncio
import hashlib
import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from datara.app import create_app
from datara.domain import FieldDef, TableDef, new_profile, normalize, validate_profile
from datara.evaluation import compare_values, evaluate_output
from datara.generators import profile_with_field_rules, prompt_components, prompt_fingerprint
from datara.optimizer import (IMPORTED_OVERRIDE_MARKER, RESPONSE_PARSER_VERSION,
                              PromptOptimizerService, candidate_uses_language, create_run_record,
                              ensure_prompt_version, import_prompt_version,
                              imported_prompt_field_rules, prompt_language)
from datara.optimizer_models import CandidateResponse, ComparisonPolicy, OptimizationRunCreate
from datara.provider import Connection
from datara.storage import Store


def image_bytes(color="white"):
    out = io.BytesIO()
    Image.new("RGB", (30, 30), color).save(out, "PNG")
    return out.getvalue()


def test_exact_and_normalized_comparisons():
    assert not compare_values("HKD", "hkd", "String", ComparisonPolicy(mode="exact"))["matched"]
    insensitive = compare_values(
        "Linde (China) investment Co., Ltd. ",
        "Linde (China) Investment Co., Ltd.",
        "String", ComparisonPolicy(mode="case_insensitive"),
    )
    assert insensitive["matched"]
    assert insensitive["normalized_expected"] == "linde (china) investment co., ltd."
    assert not compare_values(
        "Linde (China) Investment Co., Ltd.",
        "Linde China Investment Co Ltd",
        "String", ComparisonPolicy(mode="case_insensitive"),
    )["matched"]
    assert compare_values("2026-09-01", "01/09/2026", "Date",
                          ComparisonPolicy(mode="normalized", date_order="DMY"))["matched"]
    assert compare_values("1,000.00", 1000, "Decimal",
                          ComparisonPolicy(mode="normalized"))["matched"]
    assert not compare_values("01/09/2026", "2026-09-01", "Date",
                              ComparisonPolicy(mode="normalized"))["matched"]


def test_prompt_language_detection_and_candidate_guard():
    assert prompt_language("请读取发票购买方的完整公司名称，并排除开票方和页脚公司名称。") == "zh-CN"
    assert prompt_language("Extract the complete buyer company name from the invoice header.") == "en"
    assert candidate_uses_language("优先读取 BILL TO 标签后的完整公司名称。", "zh-CN")
    assert not candidate_uses_language("Extract the issuer company from the header.", "zh-CN")


def test_published_prompt_rules_are_mapped_to_profile_fields_without_absorbing_next_section(tmp_path):
    store = Store(tmp_path)
    profile = new_profile("Published rules")
    company = FieldDef(name="company_name")
    invoice = FieldDef(name="invoice_number")
    profile.tables[0].fields[:0] = [company, invoice]
    profile = store.save(profile)
    published = """发票提取规则，请返回严格 JSON。

1. 公司名称 (company_name)
识别 Bill To 后的完整购买方名称。
- 排除供应商和页眉 Logo。

2. 供应商名称 (vendor_name)
识别发票开具方；不要当作购买方。

3. 发票号码 (invoice_number)
提取 Invoice No. 后的完整字符串。

提取明细行项目（数组结构）：
以下是其他表的规则。
"""

    rules = imported_prompt_field_rules(profile, published)
    assert rules[company.id] == "识别 Bill To 后的完整购买方名称。\n- 排除供应商和页眉 Logo。"
    assert "识别发票开具方" not in rules[company.id]
    assert rules[invoice.id] == "提取 Invoice No. 后的完整字符串。"

    imported = import_prompt_version(store, profile, published, profile.revision)
    assert set(imported["mapped_field_ids"]) == {company.id, invoice.id}
    assert set(imported["updated_field_ids"]) == {company.id, invoice.id}
    assert imported["profile"]["revision"] == profile.revision + 1
    saved = store.load(profile.id)
    saved_rules = {field.id: field.extraction for field in saved.tables[0].fields}
    assert saved_rules[company.id] == rules[company.id]
    assert next(field for field in imported["field_rules"]
                if field["field_id"] == company.id)["extraction_rule"] == rules[company.id]


def test_imported_baseline_cannot_silently_optimize_an_unmapped_empty_rule(tmp_path):
    store = Store(tmp_path)
    profile = new_profile("Unmapped published rules")
    company = FieldDef(name="company_name")
    profile.tables[0].fields.insert(0, company)
    normalize(profile)
    profile = store.save(profile)
    imported = import_prompt_version(
        store, profile, "这是完整的线上提示词，但没有可明确解析的字段标题或独立字段规则。", profile.revision)
    body = OptimizationRunCreate(
        profile_id=profile.id, profile_revision=profile.revision,
        baseline_prompt_version_id=imported["id"], selected_field_ids=[company.id],
        failure_test_case_ids=["not-reached"],
    )
    with pytest.raises(ValueError, match="尚未同步到 Profile"):
        create_run_record(store, body)


def test_imported_rule_that_breaks_profile_validation_is_reported_but_not_saved(tmp_path):
    store = Store(tmp_path)
    profile = new_profile("Legacy defaults")
    amount = FieldDef(name="tax_amount", data_type="Decimal")
    profile.tables[0].fields.insert(0, amount)
    normalize(profile)
    profile = store.save(profile)
    imported = import_prompt_version(
        store, profile,
        "这是完整的线上发票提示词。\n\ntax_amount:\n如果找不到税额，默认返回 0。",
        profile.revision,
    )
    assert imported["mapped_field_ids"] == []
    assert imported["rejected_field_ids"] == [amount.id]
    assert amount.id in imported["unmapped_field_ids"]
    saved = store.load(profile.id)
    assert next(field for field in saved.tables[0].fields if field.id == amount.id).extraction == ""
    assert validate_profile(saved)["errors"] == []


def test_reimport_repairs_an_invalid_rule_saved_by_an_older_importer(tmp_path):
    store = Store(tmp_path)
    profile = new_profile("Polluted legacy import")
    amount = FieldDef(
        name="tax_amount", data_type="Decimal", extraction="如果找不到税额，默认返回 0。")
    profile.tables[0].fields.insert(0, amount)
    normalize(profile)
    profile = store.save(profile)
    assert validate_profile(profile)["errors"]

    imported = import_prompt_version(
        store, profile,
        "这是完整的线上发票提示词。\n\ntax_amount:\n如果找不到税额，默认返回 0。",
        profile.revision,
    )

    assert imported["mapped_field_ids"] == []
    assert imported["rejected_field_ids"] == [amount.id]
    assert imported["updated_field_ids"] == [amount.id]
    saved = store.load(profile.id)
    assert next(field for field in saved.tables[0].fields if field.id == amount.id).extraction == ""
    assert validate_profile(saved)["errors"] == []


def test_candidate_retries_when_rule_language_differs_from_imported_prompt(tmp_path, monkeypatch):
    calls = 0

    async def fake_completion(connection, key, instructions, images, reference_text=""):
        nonlocal calls
        calls += 1
        assert reference_text == imported["imported_prompt_base"]
        rule = ("Extract the buyer company from the invoice header."
                if calls == 1 else "提取发票购买方的完整公司名称，并排除开票方。")
        return json.dumps({"analyses": [], "candidate_rules": [{
            "field_id": company.id, "old_rule_hash": hashlib.sha256(b"").hexdigest(),
            "new_rule": rule, "reason": "保持原提示词语言",
        }]})

    store = Store(tmp_path)
    profile = new_profile("中文提示词")
    company = FieldDef(name="company_name", description="发票购买方")
    profile.tables[0].fields.insert(0, company)
    profile = store.save(profile)
    imported = import_prompt_version(
        store, profile, "这是当前使用的中文发票提示词。请返回严格 JSON，并提取购买方公司名称。", profile.revision)
    monkeypatch.setattr("datara.optimizer.completion", fake_completion)
    service = PromptOptimizerService(store, lambda: Connection(), lambda: "key")
    response, _ = asyncio.run(service._candidate(
        {"selected_fields": [{"field_id": company.id}], "run_test_cases": []},
        imported, [], []))
    assert calls == 2
    assert response.candidate_rules[0].new_rule.startswith("提取发票购买方")


def test_candidate_falls_back_to_text_when_optimizer_model_rejects_images(tmp_path, monkeypatch):
    image_counts = []

    async def fake_completion(connection, key, instructions, images, reference_text=""):
        image_counts.append(len(images))
        assert reference_text == imported["imported_prompt_base"]
        if images:
            raise ValueError("模型接口返回 HTTP 400：包含参考资料的请求被网关拒绝")
        return json.dumps({"analyses": [], "candidate_rules": [{
            "field_id": company.id, "old_rule_hash": hashlib.sha256(b"").hexdigest(),
            "new_rule": "提取发票购买方的完整公司名称。", "reason": "按购买方角色定位",
        }]})

    store = Store(tmp_path)
    profile = new_profile("图片降级")
    company = FieldDef(name="company_name", description="发票购买方")
    profile.tables[0].fields.insert(0, company)
    profile = store.save(profile)
    imported = import_prompt_version(
        store, profile, "这是当前使用的中文发票提示词。请返回严格 JSON，并提取购买方公司名称。", profile.revision)
    sample_folder = store.path("samples", "failure-sample", "")
    sample_folder.mkdir(parents=True)
    (sample_folder / "meta.json").write_text(json.dumps({"pages": 1}), encoding="utf-8")
    Image.new("RGB", (30, 30), "white").save(sample_folder / "1.jpg", "JPEG")
    run = {
        "selected_fields": [{"field_id": company.id}],
        "run_test_cases": [{"test_case_id": "failure-case", "dataset_role": "failure",
                            "sample_id": "failure-sample", "name": "failure"}],
    }
    evaluations = [{"test_case_id": "failure-case", "dataset_role": "failure",
                    "field_id": company.id, "matched": False}]
    monkeypatch.setattr("datara.optimizer.completion", fake_completion)
    service = PromptOptimizerService(store, lambda: Connection(), lambda: "key")
    response, _ = asyncio.run(service._candidate(run, imported, evaluations, []))
    assert image_counts == [1, 0]
    assert response.candidate_rules[0].new_rule.startswith("提取发票购买方")


def test_repeated_run_reuses_recent_valid_baseline_extraction(tmp_path, monkeypatch):
    calls = 0

    async def fake_completion(connection, key, instructions, images, reference_text=""):
        nonlocal calls
        calls += 1
        return json.dumps({"AI_Document": {"invoice_number": "INV-1"}})

    monkeypatch.setattr("datara.optimizer.completion", fake_completion)
    with TestClient(create_app(tmp_path)) as client:
        profile = new_profile("Baseline cache")
        invoice = FieldDef(name="invoice_number")
        profile.tables[0].fields.insert(0, invoice)
        profile = client.post("/api/profiles/save", json=profile.model_dump()).json()
        sample = client.post("/api/samples", files={"file": ("invoice.png", image_bytes())}).json()
        case = client.post(f"/api/optimizer/profiles/{profile['id']}/test-cases", json={
            "sample_id": sample["id"], "name": "invoice", "dataset_role": "failure",
            "ground_truth": {"AI_Document": {"invoice_number": "INV-1"}},
        }).json()
        store = client.app.state.store
        body = OptimizationRunCreate(
            profile_id=profile["id"], profile_revision=profile["revision"],
            selected_field_ids=[invoice.id], failure_test_case_ids=[case["id"]],
        )
        service = PromptOptimizerService(store, lambda: Connection(base_url="https://cache.test/v1",
                                                                   model="qwen-vision"), lambda: "key")
        first = create_run_record(store, body)
        version = store.read_json("optimizer/prompt_versions", first["baseline_version_id"])
        asyncio.run(service._extract_version(first, version, "baseline", None))
        second = create_run_record(store, body)
        asyncio.run(service._extract_version(second, version, "baseline", None))
        assert calls == 1
        assert second["cache_hits"] == 1
        latest = store.read_json("optimizer/extractions", second["extraction_ids"][0])
        assert latest["cache_hit"] is True
        assert latest["cached_from_extraction_id"] == first["extraction_ids"][0]


def test_baseline_cache_reuses_selected_fields_when_unselected_structure_is_invalid(tmp_path, monkeypatch):
    calls = 0

    async def fake_completion(connection, key, instructions, images, reference_text=""):
        nonlocal calls
        calls += 1
        # The selected field is usable, while an unrelated required field is missing.
        return json.dumps({"AI_Document": {"company_name": "Linde Huizhou"}})

    monkeypatch.setattr("datara.optimizer.completion", fake_completion)
    with TestClient(create_app(tmp_path)) as client:
        profile = new_profile("Selected field baseline cache")
        company = FieldDef(name="company_name")
        invoice = FieldDef(name="invoice_number")
        profile.tables[0].fields[:0] = [company, invoice]
        profile = client.post("/api/profiles/save", json=profile.model_dump()).json()
        sample = client.post("/api/samples", files={"file": ("invoice.png", image_bytes())}).json()
        case = client.post(f"/api/optimizer/profiles/{profile['id']}/test-cases", json={
            "sample_id": sample["id"], "name": "invoice", "dataset_role": "failure",
            "ground_truth": {"AI_Document": {"company_name": "Linde Huizhou"}},
        }).json()
        store = client.app.state.store
        body = OptimizationRunCreate(
            profile_id=profile["id"], profile_revision=profile["revision"],
            selected_field_ids=[company.id], failure_test_case_ids=[case["id"]],
        )
        service = PromptOptimizerService(
            store, lambda: Connection(base_url="https://cache.test/v1", model="qwen-vision"),
            lambda: "key")
        first = create_run_record(store, body)
        version = store.read_json("optimizer/prompt_versions", first["baseline_version_id"])
        asyncio.run(service._extract_version(first, version, "baseline", None))
        first_extraction = store.read_json("optimizer/extractions", first["extraction_ids"][0])
        assert first_extraction["structure_validation"]["status"] == "invalid"

        second = create_run_record(store, body)
        asyncio.run(service._extract_version(second, version, "baseline", None))
        assert calls == 1
        assert second["cache_hits"] == 1


def test_fenced_extraction_response_is_parsed_and_scored(tmp_path, monkeypatch):
    """A correct value wrapped in a ```json fence used to be scored as missing.

    Regression from runs 5214731e / f83dca18 (2026-09-17): candidate extractions
    returned the expected value inside a Markdown fence, strict_json raised
    'Expecting value: line 1 column 1 (char 0)', parsed_output fell back to {}, and
    both runs early-stopped as no_improvement after two rejected rounds that had
    actually fixed the case.
    """
    async def fenced_completion(connection, key, instructions, images, reference_text=""):
        return '```json\n{"AI_Document": {"company_name": "Linde Huizhou"}}\n```'

    monkeypatch.setattr("datara.optimizer.completion", fenced_completion)
    with TestClient(create_app(tmp_path)) as client:
        profile = new_profile("Fenced extraction response")
        company = FieldDef(name="company_name")
        profile.tables[0].fields[:0] = [company]
        profile = client.post("/api/profiles/save", json=profile.model_dump()).json()
        sample = client.post("/api/samples", files={"file": ("invoice.png", image_bytes())}).json()
        case = client.post(f"/api/optimizer/profiles/{profile['id']}/test-cases", json={
            "sample_id": sample["id"], "name": "invoice", "dataset_role": "failure",
            "ground_truth": {"AI_Document": {"company_name": "Linde Huizhou"}},
        }).json()
        store = client.app.state.store
        body = OptimizationRunCreate(
            profile_id=profile["id"], profile_revision=profile["revision"],
            selected_field_ids=[company.id], failure_test_case_ids=[case["id"]],
        )
        service = PromptOptimizerService(
            store, lambda: Connection(base_url="https://cache.test/v1", model="qwen-vision"),
            lambda: "key")
        run = create_run_record(store, body)
        version = store.read_json("optimizer/prompt_versions", run["baseline_version_id"])
        metrics, failure_rows, _ = asyncio.run(
            service._extract_version(run, version, "baseline", None))
        extraction = store.read_json("optimizer/extractions", run["extraction_ids"][0])
        assert extraction["parsed_output"] == {"AI_Document": {"company_name": "Linde Huizhou"}}
        assert not any("Expecting value" in e for e in extraction["structure_validation"]["errors"])
        selected = [row for row in failure_rows if row["selected"]]
        assert selected and all(row["matched"] for row in selected)
        assert metrics["failure_selected"]["ratio"] == 1.0


def test_baseline_cache_ignores_records_parsed_by_older_rules(tmp_path, monkeypatch):
    """Extractions cached before the code-fence parser fix must not be reused.

    Older records stored fenced responses as parsed_output={} with 'Expecting value'
    validation errors. Reusing them would keep scoring new runs against an empty
    baseline until the 24h TTL expires.
    """
    calls = 0

    async def fake_completion(connection, key, instructions, images, reference_text=""):
        nonlocal calls
        calls += 1
        return json.dumps({"AI_Document": {"company_name": "Linde Huizhou"}})

    monkeypatch.setattr("datara.optimizer.completion", fake_completion)
    with TestClient(create_app(tmp_path)) as client:
        profile = new_profile("Stale parser cache")
        company = FieldDef(name="company_name")
        profile.tables[0].fields[:0] = [company]
        profile = client.post("/api/profiles/save", json=profile.model_dump()).json()
        sample = client.post("/api/samples", files={"file": ("invoice.png", image_bytes())}).json()
        case = client.post(f"/api/optimizer/profiles/{profile['id']}/test-cases", json={
            "sample_id": sample["id"], "name": "invoice", "dataset_role": "failure",
            "ground_truth": {"AI_Document": {"company_name": "Linde Huizhou"}},
        }).json()
        store = client.app.state.store
        body = OptimizationRunCreate(
            profile_id=profile["id"], profile_revision=profile["revision"],
            selected_field_ids=[company.id], failure_test_case_ids=[case["id"]],
        )
        service = PromptOptimizerService(
            store, lambda: Connection(base_url="https://cache.test/v1", model="qwen-vision"),
            lambda: "key")
        first = create_run_record(store, body)
        version = store.read_json("optimizer/prompt_versions", first["baseline_version_id"])
        asyncio.run(service._extract_version(first, version, "baseline", None))
        assert calls == 1
        extraction = store.read_json("optimizer/extractions", first["extraction_ids"][0])
        assert extraction["response_parser_version"] == RESPONSE_PARSER_VERSION
        # Simulate a record written by the pre-fix parser.
        extraction["response_parser_version"] = RESPONSE_PARSER_VERSION - 1
        store.write_json(store.path("optimizer/extractions", extraction["id"]), extraction)
        second = create_run_record(store, body)
        asyncio.run(service._extract_version(second, version, "baseline", None))
        assert calls == 2
        assert second["cache_hits"] == 0


@pytest.mark.parametrize("repeats", [1, 3])
def test_known_observed_failure_drives_candidate_when_current_baseline_is_lucky(tmp_path, monkeypatch, repeats):
    field_id = None
    analysis_calls = 0

    async def fake_completion(connection, key, instructions, images, reference_text=""):
        nonlocal analysis_calls
        if "Datara 字段级提取提示词优化器" in instructions:
            analysis_calls += 1
            assert '"actual": "Linde GmbH"' in instructions
            return json.dumps({
                "analyses": [{
                    "field_id": field_id, "table_name": "AI_Document",
                    "field_name": "company_name", "error_type": "wrong_region",
                    "root_cause": "历史运行误取了页眉开票方。",
                    "evidence": ["已知错误输出为 Linde GmbH"],
                    "suggested_change": "优先购买方地址块并排除 Corporate Office。",
                }],
                "candidate_rules": [{
                    "field_id": field_id, "old_rule_hash": hashlib.sha256(b"").hexdigest(),
                    "new_rule": "提取购买方地址块的完整公司名称，并排除页眉 Corporate Office 开票方。",
                    "reason": "修复已确认的历史错误输出。",
                }],
            })
        # The live rerun happens to be correct even before optimization.
        return json.dumps({"AI_Document": {
            "company_name": "Linde (Huizhou) Industrial Gas Co., Ltd.",
        }})

    monkeypatch.setattr("datara.optimizer.completion", fake_completion)
    with TestClient(create_app(tmp_path)) as client:
        client.post("/api/settings", json={
            "base_url": "https://example.test/v1", "model": "test", "api_key": "optimizer-key",
        })
        profile = new_profile("Observed production failure")
        company = FieldDef(name="company_name")
        profile.tables[0].fields.insert(0, company)
        field_id = company.id
        profile = client.post("/api/profiles/save", json=profile.model_dump()).json()
        sample = client.post("/api/samples", files={
            "file": ("invoice.png", image_bytes()),
        }).json()
        case = client.post(f"/api/optimizer/profiles/{profile['id']}/test-cases", json={
            "sample_id": sample["id"], "name": "intermittent failure",
            "dataset_role": "failure",
            "ground_truth": {"AI_Document": {
                "company_name": "Linde (Huizhou) Industrial Gas Co., Ltd.",
            }},
            "observed_output": {"AI_Document": {"company_name": "Linde GmbH"}},
        }).json()
        assert case["observed_output"]["AI_Document"]["company_name"] == "Linde GmbH"

        run = client.post("/api/optimizer/runs", json={
            "profile_id": profile["id"], "profile_revision": profile["revision"],
            "selected_field_ids": [company.id], "failure_test_case_ids": [case["id"]],
            "settings": {"max_iterations": 1, "evaluation_repeats": repeats},
        }).json()
        for _ in range(100):
            result = client.get("/api/optimizer/runs/" + run["id"]).json()
            if result["status"] not in {"queued", "baselining", "optimizing", "validating"}:
                break
            time.sleep(0.01)

        assert result["status"] == "completed", result
        assert result["observed_failure_case_ids"] == [case["id"]]
        assert result["baseline_metrics"]["failure_selected"]["ratio"] == 1
        assert result["best_metrics"]["failure_selected"]["ratio"] == 1
        assert result["promotion_eligible"] is False
        assert analysis_calls == 1
        iteration = client.get(f"/api/optimizer/runs/{run['id']}/iterations").json()[0]
        assert iteration["diffs"][0]["failure_accuracy_before"]["ratio"] == 1
        assert iteration["candidate_version_id"]  # Candidate remains inspectable despite no proven gain.
        assert iteration["accepted"] is False
        assert result["baseline_metrics"]["case_results"][0]["total"] == repeats
        bypass = client.post(f"/api/prompt-versions/{iteration['candidate_version_id']}/rollback", json={
            "expected_profile_revision": profile["revision"],
            "expected_active_version_id": result["baseline_version_id"],
        })
        assert bypass.status_code == 400
        assert "未发布候选" in bypass.json()["detail"]
        latest = client.get(f"/api/optimizer/profiles/{profile['id']}/runs/latest").json()
        assert latest["id"] == run["id"]


def test_field_rule_override_is_copy_only_and_ai_only():
    profile = new_profile()
    selected = FieldDef(name="invoice_number", extraction="old")
    untouched = FieldDef(name="po_number", extraction="keep")
    profile.tables[0].fields[:0] = [selected, untouched]
    before = prompt_components(profile)
    changed = profile_with_field_rules(profile, {selected.id: "new"})
    after = prompt_components(changed)
    assert selected.extraction == "old"
    assert next(f for f in changed.tables[0].fields if f.id == selected.id).extraction == "new"
    assert before["global_rules_hash"] == after["global_rules_hash"]
    assert before["profile_rules_hash"] == after["profile_rules_hash"]
    assert before["json_structure_hash"] == after["json_structure_hash"]
    old_by_id = {f["field_id"]: f for f in before["field_rules"]}
    new_by_id = {f["field_id"]: f for f in after["field_rules"]}
    assert old_by_id[untouched.id]["component_hash"] == new_by_id[untouched.id]["component_hash"]


def test_detail_evaluation_penalizes_missing_and_unexpected_rows():
    profile = new_profile()
    detail_field = FieldDef(name="po_number")
    profile.tables.append(TableDef(name="AI_Detail", role="detail",
                                   parent_table_id=profile.tables[0].id, fields=[detail_field]))
    normalize(profile)
    selected = {detail_field.id}
    missing = evaluate_output(profile, {"AI_Detail": [{"po_number": None}]},
                              {"AI_Detail": []}, selected, {})
    assert missing[0]["matched"] is False and missing[0]["reason"] == "missing_actual_path"
    extra = evaluate_output(profile, {"AI_Detail": []},
                            {"AI_Detail": [{"po_number": "PO-1"}]}, selected, {})
    assert extra[0]["matched"] is False and extra[0]["reason"] == "unexpected_row"


def test_candidate_attempt_to_modify_unselected_field_is_rejected(tmp_path):
    store = Store(tmp_path)
    profile = new_profile()
    selected = FieldDef(name="invoice_number", extraction="selected old")
    unselected = FieldDef(name="po_number", extraction="must stay")
    profile.tables[0].fields[:0] = [selected, unselected]
    profile = store.save(profile)
    baseline = ensure_prompt_version(store, profile)
    service = PromptOptimizerService(store, lambda: Connection(), lambda: "key")
    response = CandidateResponse.model_validate({
        "analyses": [],
        "candidate_rules": [{
            "field_id": unselected.id,
            "old_rule_hash": hashlib.sha256(b"must stay").hexdigest(),
            "new_rule": "changed", "reason": "not authorized",
        }],
    })
    with pytest.raises(ValueError, match="未选择"):
        service._build_candidate_version({
            "id": "run", "selected_fields": [{"field_id": selected.id}],
        }, baseline, response, "{}", "iteration")
    assert store.load(profile.id).tables[0].fields[1].extraction == "must stay"
    assert len(store.list_json("optimizer/prompt_versions")) == 1


def test_optimizer_retries_transient_qwen_error(tmp_path, monkeypatch):
    calls = 0

    async def flaky(connection, key, instructions, images):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("模型接口返回 HTTP 429。请检查地址、模型、图片能力及账号额度。")
        return "{}"

    async def no_wait(_):
        return None

    monkeypatch.setattr("datara.optimizer.completion", flaky)
    monkeypatch.setattr("datara.optimizer.asyncio.sleep", no_wait)
    service = PromptOptimizerService(Store(tmp_path), lambda: Connection(), lambda: "key")
    raw, attempts = asyncio.run(service._complete(Connection(), "task", []))
    assert raw == "{}" and attempts == 2


def test_optimizer_does_not_repeat_a_full_model_timeout(tmp_path, monkeypatch):
    calls = 0

    async def timeout(connection, key, instructions, images):
        nonlocal calls
        calls += 1
        raise ValueError("模型请求超时，可调整超时设置后重试")

    monkeypatch.setattr("datara.optimizer.completion", timeout)
    service = PromptOptimizerService(Store(tmp_path), lambda: Connection(), lambda: "key")
    with pytest.raises(ValueError, match="模型请求超时"):
        asyncio.run(service._complete(Connection(timeout=1800), "task", []))
    assert calls == 1


def test_optimizer_retries_a_connection_setup_failure(tmp_path, monkeypatch):
    """A connect timeout costs 15 seconds, not the whole budget, so it gets one retry."""
    calls = 0

    async def connect_timeout(connection, key, instructions, images, reference_text=""):
        nonlocal calls
        calls += 1
        raise ValueError("无法连接模型端点：建立连接超过 15 秒连接超时（企业代理握手缓慢或网络抖动），可重试")

    async def no_wait(_):
        return None

    monkeypatch.setattr("datara.optimizer.completion", connect_timeout)
    monkeypatch.setattr("datara.optimizer.asyncio.sleep", no_wait)
    service = PromptOptimizerService(Store(tmp_path), lambda: Connection(), lambda: "key")
    with pytest.raises(ValueError, match="无法连接模型端点"):
        asyncio.run(service._complete(Connection(timeout=600), "task", []))
    assert calls == 2


def test_optimizer_recovers_when_the_second_connection_attempt_works(tmp_path, monkeypatch):
    calls = 0

    async def flaky_connect(connection, key, instructions, images, reference_text=""):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("无法连接模型端点：建立连接超过 15 秒连接超时（企业代理握手缓慢或网络抖动），可重试")
        return "{}"

    async def no_wait(_):
        return None

    monkeypatch.setattr("datara.optimizer.completion", flaky_connect)
    monkeypatch.setattr("datara.optimizer.asyncio.sleep", no_wait)
    service = PromptOptimizerService(Store(tmp_path), lambda: Connection(), lambda: "key")
    raw, attempts = asyncio.run(service._complete(Connection(timeout=600), "task", []))
    assert raw == "{}" and attempts == 2



def test_candidate_evaluation_timeout_preserves_candidate_and_fails_iteration(
        tmp_path, monkeypatch, caplog):
    field_id = None
    timeout_calls = 0

    async def fake_completion(connection, key, instructions, images, reference_text=""):
        nonlocal timeout_calls
        if "Datara 字段级提取提示词优化器" in instructions:
            return json.dumps({
                "analyses": [],
                "candidate_rules": [{
                    "field_id": field_id,
                    "old_rule_hash": hashlib.sha256(b"").hexdigest(),
                    "new_rule": "提取购买方完整公司名称，并排除页眉中的开票方。",
                    "reason": "修复公司角色混淆。",
                }],
            })
        if "排除页眉中的开票方" in instructions:
            timeout_calls += 1
            raise ValueError("模型请求超时，可调整超时设置后重试")
        return json.dumps({"AI_Document": {"company_name": "正确公司"}})

    monkeypatch.setattr("datara.optimizer.completion", fake_completion)
    caplog.set_level("INFO", logger="datara.optimizer")
    with TestClient(create_app(tmp_path)) as client:
        client.post("/api/settings", json={
            "base_url": "https://example.test/v1", "model": "extract-model",
            "optimizer_model": "optimizer-model", "extraction_model": "extract-model",
            "timeout": 1800, "api_key": "optimizer-key",
        })
        profile = new_profile("Candidate timeout")
        company = FieldDef(name="company_name")
        profile.tables[0].fields.insert(0, company)
        field_id = company.id
        profile = client.post("/api/profiles/save", json=profile.model_dump()).json()
        sample = client.post(
            "/api/samples", files={"file": ("invoice.png", image_bytes())}).json()
        case = client.post(f"/api/optimizer/profiles/{profile['id']}/test-cases", json={
            "sample_id": sample["id"], "name": "Huizhou invoice",
            "dataset_role": "failure",
            "ground_truth": {"AI_Document": {"company_name": "正确公司"}},
            "observed_output": {"AI_Document": {"company_name": "错误公司"}},
        }).json()
        run = client.post("/api/optimizer/runs", json={
            "profile_id": profile["id"], "profile_revision": profile["revision"],
            "selected_field_ids": [field_id], "failure_test_case_ids": [case["id"]],
            "settings": {"max_iterations": 1},
        }).json()
        for _ in range(100):
            result = client.get("/api/optimizer/runs/" + run["id"]).json()
            if result["status"] not in {"queued", "baselining", "optimizing", "validating"}:
                break
            time.sleep(0.01)

        assert result["status"] == "failed"
        assert result["connection_snapshot"]["timeout_seconds"] == 1800
        assert "候选提示词评估失败" in result["blocking_reasons"][0]
        assert "单次超时 1800 秒" in result["blocking_reasons"][0]
        iteration = client.get(f"/api/optimizer/runs/{run['id']}/iterations").json()[0]
        assert iteration["status"] == "failed"
        assert iteration["candidate_version_id"]
        assert timeout_calls == 1
        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "optimizer_run_started" in messages
        assert "optimizer_candidate_completed" in messages
        assert "optimizer_run_failed" in messages
        assert "optimizer-key" not in messages


def test_candidate_connection_failure_is_retried_and_not_reported_as_a_timeout(
        tmp_path, monkeypatch, caplog):
    """Guards the field bug: a 15 second connect timeout surfaced as
    "（单次超时 600 秒）：模型请求超时" and, being classified as a timeout, was never
    retried, so one transient proxy handshake failed the whole optimization run."""
    field_id = None
    evaluation_calls = 0

    async def fake_completion(connection, key, instructions, images, reference_text=""):
        nonlocal evaluation_calls
        if "Datara 字段级提取提示词优化器" in instructions:
            return json.dumps({
                "analyses": [],
                "candidate_rules": [{
                    "field_id": field_id,
                    "old_rule_hash": hashlib.sha256(b"").hexdigest(),
                    "new_rule": "提取购买方完整公司名称，并排除页眉中的开票方。",
                    "reason": "修复公司角色混淆。",
                }],
            })
        if "排除页眉中的开票方" in instructions:
            evaluation_calls += 1
            raise ValueError("无法连接模型端点：建立连接超过 15 秒连接超时（企业代理握手缓慢或网络抖动），可重试")
        return json.dumps({"AI_Document": {"company_name": "正确公司"}})

    monkeypatch.setattr("datara.optimizer.completion", fake_completion)
    caplog.set_level("INFO", logger="datara.optimizer")
    with TestClient(create_app(tmp_path)) as client:
        client.post("/api/settings", json={
            "base_url": "https://example.test/v1", "model": "extract-model",
            "optimizer_model": "optimizer-model", "extraction_model": "extract-model",
            "timeout": 600, "api_key": "optimizer-key",
        })
        profile = new_profile("Candidate connect failure")
        company = FieldDef(name="company_name")
        profile.tables[0].fields.insert(0, company)
        field_id = company.id
        profile = client.post("/api/profiles/save", json=profile.model_dump()).json()
        sample = client.post(
            "/api/samples", files={"file": ("invoice.png", image_bytes())}).json()
        case = client.post(f"/api/optimizer/profiles/{profile['id']}/test-cases", json={
            "sample_id": sample["id"], "name": "Huizhou invoice",
            "dataset_role": "failure",
            "ground_truth": {"AI_Document": {"company_name": "正确公司"}},
            "observed_output": {"AI_Document": {"company_name": "错误公司"}},
        }).json()
        run = client.post("/api/optimizer/runs", json={
            "profile_id": profile["id"], "profile_revision": profile["revision"],
            "selected_field_ids": [field_id], "failure_test_case_ids": [case["id"]],
            "settings": {"max_iterations": 1},
        }).json()
        for _ in range(200):
            result = client.get("/api/optimizer/runs/" + run["id"]).json()
            if result["status"] not in {"queued", "baselining", "optimizing", "validating"}:
                break
            time.sleep(0.01)

        assert result["status"] == "failed"
        reason = result["blocking_reasons"][0]
        assert "候选提示词评估失败" in reason
        assert "无法连接模型端点" in reason
        assert "单次超时" not in reason
        assert evaluation_calls == 2
        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "optimizer_model_retry" in messages
        assert "optimizer-key" not in messages



def test_optimizer_workflow_promote_and_version_history(tmp_path, monkeypatch):
    failure_sample_id = None
    field_id = None

    async def fake_completion(connection, key, instructions, images, reference_text=""):
        assert key == "optimizer-key"
        if "Datara 字段级提取提示词优化器" in instructions:
            return json.dumps({
                "analyses": [{
                    "field_id": field_id, "table_name": "AI_Document", "field_name": "amount",
                    "error_type": "wrong_region", "root_cause": "The total label was not prioritized.",
                    "evidence": ["failure case"], "suggested_change": "Prioritize the grand total.",
                }],
                "candidate_rules": [{
                    "field_id": field_id, "old_rule_hash": hashlib.sha256(b"").hexdigest(),
                    "new_rule": "读取大写总计标签旁的金额，并排除小计金额。",
                    "reason": "避免误取小计金额。",
                }],
            })
        is_failure = images[0].parent.name == failure_sample_id
        optimized = "大写总计标签" in instructions
        value = 100 if is_failure and optimized else 90 if is_failure else 50
        return json.dumps({"AI_Document": {"amount": value}})

    monkeypatch.setattr("datara.optimizer.completion", fake_completion)
    with TestClient(create_app(tmp_path)) as client:
        client.post("/api/settings", json={"base_url": "https://example.test/v1", "model": "test",
                                           "api_key": "optimizer-key"})
        profile = new_profile("Optimizer")
        amount = FieldDef(name="amount", data_type="Decimal")
        profile.tables[0].fields.insert(0, amount)
        field_id = amount.id
        profile = client.post("/api/profiles/save", json=profile.model_dump()).json()

        failure = client.post("/api/samples", files={"file": ("failure.png", image_bytes("red"))}).json()
        regression = client.post("/api/samples", files={"file": ("regression.png", image_bytes("blue"))}).json()
        failure_sample_id = failure["id"]
        failure_case = client.post(f"/api/optimizer/profiles/{profile['id']}/test-cases", json={
            "sample_id": failure["id"], "name": "failure", "dataset_role": "failure",
            "ground_truth": {"AI_Document": {"amount": 100}},
        }).json()
        regression_case = client.post(f"/api/optimizer/profiles/{profile['id']}/test-cases", json={
            "sample_id": regression["id"], "name": "regression", "dataset_role": "regression",
            "ground_truth": {"AI_Document": {"amount": 50}},
        }).json()
        versions = client.get(f"/api/profiles/{profile['id']}/prompt-versions").json()
        run = client.post("/api/optimizer/runs", json={
            "profile_id": profile["id"], "profile_revision": profile["revision"],
            "baseline_prompt_version_id": versions["active_prompt_version_id"],
            "selected_field_ids": [field_id], "failure_test_case_ids": [failure_case["id"]],
            "regression_test_case_ids": [regression_case["id"]],
            "settings": {"max_iterations": 3},
        })
        assert run.status_code == 200, run.text
        run = run.json()
        for _ in range(100):
            result = client.get("/api/optimizer/runs/" + run["id"]).json()
            if result["status"] not in {"queued", "baselining", "optimizing", "validating"}:
                break
            time.sleep(0.01)
        assert result["status"] == "completed", result
        assert result["promotion_eligible"] is True
        assert result["best_iteration_number"] == 1
        assert result["best_metrics"]["failure_selected"]["ratio"] == 1
        iterations = client.get(f"/api/optimizer/runs/{run['id']}/iterations").json()
        assert iterations[0]["accepted"] is True

        promoted = client.post(f"/api/prompt-versions/{result['best_version_id']}/promote", json={
            "expected_profile_revision": profile["revision"],
            "expected_active_version_id": versions["active_prompt_version_id"],
            "optimization_run_id": run["id"],
        })
        assert promoted.status_code == 200, promoted.text
        promoted = promoted.json()
        assert promoted["profile"]["revision"] == profile["revision"] + 1
        current = next(f for f in promoted["profile"]["tables"][0]["fields"] if f["id"] == field_id)
        assert current["extraction"] == "读取大写总计标签旁的金额，并排除小计金额。"
        history = client.get(f"/api/profiles/{profile['id']}/prompt-versions").json()
        assert history["active_prompt_version_id"] == promoted["prompt_version"]["id"]
        assert len(history["versions"]) >= 3
        comparison = client.get("/api/prompt-versions/compare", params={
            "from_id": versions["active_prompt_version_id"], "to_id": result["best_version_id"],
        })
        assert comparison.status_code == 200, comparison.text
        assert comparison.json()["diffs"][0]["field_id"] == field_id

        rolled = client.post(f"/api/prompt-versions/{versions['active_prompt_version_id']}/rollback", json={
            "expected_profile_revision": promoted["profile"]["revision"],
            "expected_active_version_id": promoted["prompt_version"]["id"],
        })
        assert rolled.status_code == 200, rolled.text
        rolled = rolled.json()
        restored = next(f for f in rolled["profile"]["tables"][0]["fields"] if f["id"] == field_id)
        assert restored["extraction"] == ""
        assert rolled["prompt_version"]["origin"] == "rollback"


def test_optimizer_rejects_incomplete_regression_ground_truth(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        client.post("/api/settings", json={"base_url": "https://example.test/v1", "model": "test",
                                           "api_key": "optimizer-key"})
        profile = new_profile("Coverage")
        first, second = FieldDef(name="invoice_number"), FieldDef(name="po_number")
        profile.tables[0].fields[:0] = [first, second]
        profile = client.post("/api/profiles/save", json=profile.model_dump()).json()
        sample = client.post("/api/samples", files={"file": ("one.png", image_bytes())}).json()
        failure = client.post(f"/api/optimizer/profiles/{profile['id']}/test-cases", json={
            "sample_id": sample["id"], "name": "failure", "dataset_role": "failure",
            "ground_truth": {"AI_Document": {"invoice_number": "A"}},
        }).json()
        regression = client.post(f"/api/optimizer/profiles/{profile['id']}/test-cases", json={
            "sample_id": sample["id"], "name": "regression", "dataset_role": "regression",
            "ground_truth": {"AI_Document": {"invoice_number": "A"}},
        }).json()
        response = client.post("/api/optimizer/runs", json={
            "profile_id": profile["id"], "profile_revision": profile["revision"],
            "selected_field_ids": [first.id], "failure_test_case_ids": [failure["id"]],
            "regression_test_case_ids": [regression["id"]],
        })
        assert response.status_code == 400
        assert "Regression Ground truth 不完整" in response.text


def test_regression_guard_rejects_failure_only_improvement(tmp_path, monkeypatch):
    failure_sample_id = None
    field_id = None

    async def fake_completion(connection, key, instructions, images, reference_text=""):
        if "Datara 字段级提取提示词优化器" in instructions:
            return json.dumps({"analyses": [], "candidate_rules": [{
                "field_id": field_id, "old_rule_hash": hashlib.sha256(b"").hexdigest(),
                "new_rule": "候选规则：读取最终金额。", "reason": "修复待修复案例",
            }]})
        failure = images[0].parent.name == failure_sample_id
        candidate = "候选规则" in instructions
        value = 10 if failure and candidate else 9 if failure else 19 if candidate else 20
        return json.dumps({"AI_Document": {"amount": value}})

    monkeypatch.setattr("datara.optimizer.completion", fake_completion)
    with TestClient(create_app(tmp_path)) as client:
        client.post("/api/settings", json={"base_url": "https://example.test/v1", "model": "test",
                                           "api_key": "optimizer-key"})
        profile = new_profile("Guard")
        amount = FieldDef(name="amount", data_type="Decimal")
        profile.tables[0].fields.insert(0, amount)
        field_id = amount.id
        profile = client.post("/api/profiles/save", json=profile.model_dump()).json()
        failure_sample = client.post("/api/samples", files={"file": ("f.png", image_bytes("red"))}).json()
        regression_sample = client.post("/api/samples", files={"file": ("r.png", image_bytes("blue"))}).json()
        failure_sample_id = failure_sample["id"]
        cases = []
        for sample, role, expected in ((failure_sample, "failure", 10), (regression_sample, "regression", 20)):
            cases.append(client.post(f"/api/optimizer/profiles/{profile['id']}/test-cases", json={
                "sample_id": sample["id"], "name": role, "dataset_role": role,
                "ground_truth": {"AI_Document": {"amount": expected}},
            }).json())
        run = client.post("/api/optimizer/runs", json={
            "profile_id": profile["id"], "profile_revision": profile["revision"],
            "selected_field_ids": [field_id], "failure_test_case_ids": [cases[0]["id"]],
            "regression_test_case_ids": [cases[1]["id"]], "settings": {"max_iterations": 1},
        }).json()
        for _ in range(100):
            result = client.get("/api/optimizer/runs/" + run["id"]).json()
            if result["status"] not in {"queued", "baselining", "optimizing", "validating"}:
                break
            time.sleep(0.01)
        assert result["status"] == "completed"
        assert result["promotion_eligible"] is False
        assert result["best_version_id"] == result["baseline_version_id"]
        iteration = client.get(f"/api/optimizer/runs/{run['id']}/iterations").json()[0]
        assert iteration["accepted"] is False
        assert "Regression Set" in " ".join(iteration["decision_reasons"])


def test_imported_prompt_is_active_baseline_and_candidate_only_appends_selected_override(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        profile = new_profile("Published invoice profile")
        company = FieldDef(name="company_name", extraction="old company rule")
        invoice_number = FieldDef(name="invoice_number", extraction="keep invoice rule")
        profile.tables[0].fields[:0] = [company, invoice_number]
        profile = client.post("/api/profiles/save", json=profile.model_dump()).json()
        imported_text = "Existing production invoice prompt.\nReturn strict JSON and preserve every unrelated rule."
        imported = client.post(f"/api/profiles/{profile['id']}/prompt-versions/import", json={
            "expected_profile_revision": profile["revision"], "prompt_text": imported_text,
        })
        assert imported.status_code == 200, imported.text
        imported = imported.json()
        assert imported["prompt_source"] == "imported"
        versions = client.get(f"/api/profiles/{profile['id']}/prompt-versions").json()
        assert versions["active_prompt_version_id"] == imported["id"]
        baseline = client.get(f"/api/prompt-versions/{imported['id']}").json()
        assert baseline["rendered_prompt"].strip() == imported_text

        response = CandidateResponse.model_validate({
            "analyses": [],
            "candidate_rules": [{
                "field_id": company.id,
                "old_rule_hash": hashlib.sha256(b"old company rule").hexdigest(),
                "new_rule": "Choose the entity adjacent to BILL TO and ignore seller names.",
                "reason": "The seller and buyer were confused.",
            }],
        })
        service = client.app.state.optimizer_service
        candidate, _ = service._build_candidate_version({
            "id": "import-test-run", "selected_fields": [{"field_id": company.id}],
        }, baseline, response, "{}", "import-test-iteration")
        assert candidate["prompt_source"] == "imported"
        assert candidate["rendered_prompt"].startswith(imported_text)
        assert IMPORTED_OVERRIDE_MARKER in candidate["rendered_prompt"]
        assert "Choose the entity adjacent to BILL TO" in candidate["rendered_prompt"]
        untouched = next(f for f in candidate["profile_snapshot"]["tables"][0]["fields"]
                         if f["id"] == invoice_number.id)
        assert untouched["extraction"] == "keep invoice rule"


def test_prompt_fingerprint_ignores_workspace_state_but_tracks_prompt_content():
    profile = new_profile("Fingerprint profile")
    field = FieldDef(name="company_name", extraction="rule one")
    profile.tables[0].fields[:0] = [field]
    normalize(profile)
    baseline = prompt_fingerprint(profile)
    attached = profile.model_copy(deep=True)
    attached.sample_ids = ["sample-1"]
    attached.reference_ids = ["reference-1"]
    attached.database_name = "OtherDb"
    assert prompt_fingerprint(attached) == baseline
    edited = profile.model_copy(deep=True)
    edited.tables[0].fields[0].extraction = "rule two"
    assert prompt_fingerprint(edited) != baseline


def test_attaching_a_sample_keeps_the_imported_prompt_baseline(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        profile = new_profile("Published invoice profile")
        company = FieldDef(name="company_name", extraction="old company rule")
        profile.tables[0].fields[:0] = [company]
        profile = client.post("/api/profiles/save", json=profile.model_dump()).json()
        imported_text = "现有生产发票提示词，含未映射字段的全局规则。\ncompany_name:\n识别买方公司名称。"
        imported = client.post(f"/api/profiles/{profile['id']}/prompt-versions/import", json={
            "expected_profile_revision": profile["revision"], "prompt_text": imported_text,
        }).json()
        after_import = client.get(f"/api/profiles/{profile['id']}/prompt-versions").json()
        assert after_import["active_prompt_version_id"] == imported["id"]

        workspace = imported["profile"]
        workspace["sample_ids"] = ["sample-1"]
        workspace["reference_ids"] = ["reference-1"]
        saved = client.post("/api/profiles/save", json=workspace).json()
        assert saved["sample_ids"] == ["sample-1"]

        versions = client.get(f"/api/profiles/{profile['id']}/prompt-versions").json()
        assert versions["active_prompt_version_id"] == imported["id"]
        assert len(versions["versions"]) == len(after_import["versions"])
        active = client.get(f"/api/prompt-versions/{imported['id']}").json()
        assert active["prompt_source"] == "imported"
        assert active["rendered_prompt"].strip() == imported_text


def test_field_rule_edit_after_import_preserves_the_imported_base(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        profile = new_profile("Published invoice profile")
        company = FieldDef(name="company_name", extraction="old company rule")
        invoice_number = FieldDef(name="invoice_number", extraction="keep invoice rule")
        profile.tables[0].fields[:0] = [company, invoice_number]
        profile = client.post("/api/profiles/save", json=profile.model_dump()).json()
        imported_text = "现有生产发票提示词，含未映射字段的全局规则。\ncompany_name:\n识别买方公司名称。"
        imported = client.post(f"/api/profiles/{profile['id']}/prompt-versions/import", json={
            "expected_profile_revision": profile["revision"], "prompt_text": imported_text,
        }).json()

        edited = imported["profile"]
        new_rule = "改为只从 Consignee 区域提取买方公司名称。"
        for field in edited["tables"][0]["fields"]:
            if field["id"] == company.id:
                field["extraction"] = new_rule
        client.post("/api/profiles/save", json=edited)

        versions = client.get(f"/api/profiles/{profile['id']}/prompt-versions").json()
        refreshed_id = versions["active_prompt_version_id"]
        assert refreshed_id != imported["id"]
        refreshed = client.get(f"/api/prompt-versions/{refreshed_id}").json()
        assert refreshed["origin"] == "imported_prompt_refresh"
        assert refreshed["prompt_source"] == "imported"
        assert refreshed["parent_version_id"] == imported["id"]
        assert refreshed["imported_prompt_base"].strip() == imported_text
        assert refreshed["rendered_prompt"].startswith(imported_text)
        assert IMPORTED_OVERRIDE_MARKER in refreshed["rendered_prompt"]
        assert new_rule in refreshed["rendered_prompt"]
        assert refreshed["field_rule_overrides"] == {company.id: new_rule}

        again = client.get(f"/api/profiles/{profile['id']}/prompt-versions").json()
        assert again["active_prompt_version_id"] == refreshed_id
        assert len(again["versions"]) == len(versions["versions"])

        service = client.app.state.optimizer_service
        response = CandidateResponse.model_validate({
            "analyses": [],
            "candidate_rules": [{
                "field_id": invoice_number.id,
                "old_rule_hash": hashlib.sha256("keep invoice rule".encode("utf-8")).hexdigest(),
                "new_rule": "Read the invoice number from the top-right stamp.",
                "reason": "The number was missed.",
            }],
        })
        candidate, _ = service._build_candidate_version({
            "id": "refresh-test-run", "selected_fields": [{"field_id": invoice_number.id}],
        }, refreshed, response, "{}", "refresh-test-iteration")
        assert candidate["prompt_source"] == "imported"
        assert candidate["rendered_prompt"].startswith(imported_text)
        assert candidate["field_rule_overrides"][company.id] == new_rule
        assert "Read the invoice number from the top-right stamp." in candidate["rendered_prompt"]


def test_invoice_company_optimizer_runs_with_imported_prompt_and_no_regression_set(tmp_path, monkeypatch):
    sample_expectations = {}
    company_field_id = None
    imported_text = ""

    async def fake_completion(connection, key, instructions, images, reference_text=""):
        if "Datara 字段级提取提示词优化器" in instructions:
            assert reference_text == imported_text
            assert len(images) == 2
            assert '"original_prompt_language": "zh-CN"' in instructions
            return json.dumps({
                "analyses": [{
                    "field_id": company_field_id, "table_name": "AI_Invoice_Head",
                    "field_name": "company_name", "error_type": "label_confusion",
                    "root_cause": "原规则没有明确多个集团公司同时出现时的业务角色优先级。",
                    "evidence": ["BILL TO 与页眉、页脚出现同集团的不同公司"],
                    "suggested_change": "优先按购买方或收件方角色取值，并排除卖方与页脚。",
                }],
                "candidate_rules": [{
                    "field_id": company_field_id,
                    "old_rule_hash": hashlib.sha256(b"buyer company rule").hexdigest(),
                    "new_rule": ("提取作为发票购买方、付款方或收件方的完整法定公司名称。优先读取 BILL TO、"
                                 "BILLED TO、INVOICE TO、BUYER 或 CUSTOMER 标签后的公司；自开票单据读取"
                                 "收件方地址块。排除卖方、开票方、Logo、Corporate Office、汇款信息和页脚公司。"),
                    "reason": "按业务角色和页面区域区分同集团的多个公司。",
                }],
            })
        expected = sample_expectations[images[0].parent.name]
        company = expected if IMPORTED_OVERRIDE_MARKER in instructions else "Supplier Holdings GmbH"
        return json.dumps({"AI_Invoice_Head": {"company_name": company}})

    monkeypatch.setattr("datara.optimizer.completion", fake_completion)
    with TestClient(create_app(tmp_path)) as client:
        client.post("/api/settings", json={"base_url": "https://example.test/v1", "model": "qwen-test",
                                           "api_key": "optimizer-key"})
        profile = new_profile("Supplier invoice")
        profile.tables[0].name = "AI_Invoice_Head"
        company = FieldDef(name="company_name", description="发票购买方/付款方", extraction="buyer company rule")
        invoice_number = FieldDef(name="invoice_number", extraction="keep invoice number rule")
        profile.tables[0].fields[:0] = [company, invoice_number]
        company_field_id = company.id
        profile = client.post("/api/profiles/save", json=profile.model_dump()).json()
        imported_text = "这是当前已发布的供应商发票提示词。请按既有 JSON 结构提取字段，并保留所有未选择字段的规则。"
        imported = client.post(f"/api/profiles/{profile['id']}/prompt-versions/import", json={
            "expected_profile_revision": profile["revision"], "prompt_text": imported_text,
        }).json()
        assert imported["prompt_language"] == "zh-CN"

        cases = []
        for filename, color, expected in (
            ("failure-a.pdf", "red", "Buyer China Co., Ltd."),
            ("failure-b.pdf", "blue", "Buyer Huizhou Co., Ltd."),
        ):
            sample = client.post("/api/samples", files={"file": (filename.replace(".pdf", ".png"), image_bytes(color))}).json()
            sample_expectations[sample["id"]] = expected
            cases.append(client.post(f"/api/optimizer/profiles/{profile['id']}/test-cases", json={
                "sample_id": sample["id"], "name": filename, "dataset_role": "failure",
                "ground_truth": {"AI_Invoice_Head": {"company_name": expected}},
            }).json())

        run = client.post("/api/optimizer/runs", json={
            "profile_id": profile["id"], "profile_revision": profile["revision"],
            "baseline_prompt_version_id": imported["id"], "selected_field_ids": [company.id],
            "failure_test_case_ids": [case["id"] for case in cases], "regression_test_case_ids": [],
            "settings": {"max_iterations": 2},
        })
        assert run.status_code == 200, run.text
        run = run.json()
        for _ in range(100):
            result = client.get("/api/optimizer/runs/" + run["id"]).json()
            if result["status"] not in {"queued", "baselining", "optimizing", "validating"}:
                break
            time.sleep(0.01)
        assert result["status"] == "completed", result
        assert result["baseline_metrics"]["failure_selected"]["ratio"] == 0
        assert result["best_metrics"]["failure_selected"]["ratio"] == 1
        assert result["promotion_eligible"] is True
        promoted = client.post(f"/api/prompt-versions/{result['best_version_id']}/promote", json={
            "expected_profile_revision": profile["revision"],
            "expected_active_version_id": imported["id"], "optimization_run_id": run["id"],
        })
        assert promoted.status_code == 200, promoted.text
        promoted_version = promoted.json()["prompt_version"]
        assert promoted_version["prompt_source"] == "imported"
        assert promoted_version["rendered_prompt"].startswith(imported_text)
        assert IMPORTED_OVERRIDE_MARKER in promoted_version["rendered_prompt"]
        iteration = client.get(f"/api/optimizer/runs/{run['id']}/iterations").json()[0]
        assert iteration["diffs"][0]["old_rule_source"] == "imported_prompt"
        saved_fields = promoted.json()["profile"]["tables"][0]["fields"]
        assert next(f for f in saved_fields if f["id"] == invoice_number.id)["extraction"] == "keep invoice number rule"


def make_speed_case(client, count=1, observed=False):
    profile = new_profile('Speed fixture')
    company = FieldDef(name='company_name')
    profile.tables[0].fields.insert(0, company)
    profile = client.post('/api/profiles/save', json=profile.model_dump()).json()
    ids = []
    for i in range(count):
        sample = client.post('/api/samples', files={'file': ('invoice.png', image_bytes())}).json()
        case = client.post(f"/api/optimizer/profiles/{profile['id']}/test-cases", json={
            'sample_id': sample['id'], 'name': f'case-{i}', 'dataset_role': 'failure',
            'ground_truth': {'AI_Document': {'company_name': 'Billing Entity Ltd.'}},
            'observed_output': {'AI_Document': {'company_name': 'Delivery Entity Ltd.'}} if observed else None,
        }).json()
        ids.append(case['id'])
    body = OptimizationRunCreate(profile_id=profile['id'], profile_revision=profile['revision'],
                                selected_field_ids=[company.id], failure_test_case_ids=ids)
    store = client.app.state.store
    run = create_run_record(store, body)
    return run, store.read_json('optimizer/prompt_versions', run['baseline_version_id'])


def test_historical_output_never_replaces_measured_baseline(tmp_path, monkeypatch):
    calls = 0
    async def fake(*args, **kwargs):
        nonlocal calls
        calls += 1
        return json.dumps({'AI_Document': {'company_name': 'Billing Entity Ltd.'}})
    monkeypatch.setattr('datara.optimizer.completion', fake)
    with TestClient(create_app(tmp_path)) as client:
        run, version = make_speed_case(client, observed=True)
        service = client.app.state.optimizer_service
        metrics, _, _ = asyncio.run(service._extract_version(run, version, 'baseline', None))
        assert calls == 1 and metrics['failure_selected']['ratio'] == 1
        record = client.app.state.store.read_json('optimizer/extractions', run['extraction_ids'][0])
        assert record['evidence_source'] == 'model'
        for phase in ['candidate', 'final_validation']:
            metrics, _, _ = asyncio.run(service._extract_version(run, version, phase, None))
            assert metrics['failure_selected']['ratio'] == 1
        assert calls == 3


def test_extractions_overlap_but_respect_concurrency_limit(tmp_path, monkeypatch):
    active = peak = calls = 0
    async def fake(*args, **kwargs):
        nonlocal active, peak, calls
        calls += 1; active += 1; peak = max(peak, active)
        try:
            await asyncio.sleep(0.015)
            return json.dumps({'AI_Document': {'company_name': 'Billing Entity Ltd.'}})
        finally:
            active -= 1
    monkeypatch.setattr('datara.optimizer.completion', fake)
    with TestClient(create_app(tmp_path)) as client:
        run, version = make_speed_case(client, count=5)
        metrics, rows, _ = asyncio.run(client.app.state.optimizer_service._extract_version(run, version, 'candidate', None))
        assert peak == 2 and active == 0 and calls == 5
        assert metrics['failure_selected']['ratio'] == 1
        assert [r['test_case_name'] for r in rows] == [f'case-{i}' for i in range(5)]
        assert run['progress']['completed_cases'] == 5


def test_failed_parallel_case_cancels_inflight_requests(tmp_path, monkeypatch):
    active = 0
    started = None
    async def scenario(service, run, version):
        nonlocal started
        started = asyncio.Event()
        with pytest.raises(ValueError, match='HTTP 401'):
            await service._extract_version(run, version, 'candidate', None)
        assert active == 0
    async def fake(connection, key, instructions, images, **kwargs):
        nonlocal active
        active += 1
        try:
            if active == 1:
                await started.wait()
                raise ValueError('HTTP 401')
            started.set()
            await asyncio.Event().wait()
        finally:
            active -= 1
    monkeypatch.setattr('datara.optimizer.completion', fake)
    with TestClient(create_app(tmp_path)) as client:
        run, version = make_speed_case(client, count=2)
        asyncio.run(scenario(client.app.state.optimizer_service, run, version))


def test_optimizer_deadline_includes_retry_backoff(tmp_path, monkeypatch):
    async def fake(*args, **kwargs):
        raise ValueError('HTTP 429')
    monkeypatch.setattr('datara.optimizer.completion', fake)
    service = PromptOptimizerService(Store(tmp_path), lambda: Connection(), lambda: 'key')
    with pytest.raises(ValueError, match='总等待超过'):
        asyncio.run(service._complete(Connection().model_copy(update={'timeout': 0.03}), 'task', []))


@pytest.mark.parametrize("final_degrades", [False, True])
def test_repeated_baseline_catches_intermittent_failure_and_final_is_fresh(tmp_path, monkeypatch, final_degrades):
    baseline_calls = candidate_calls = 0
    field_id = None

    async def model(connection, key, instructions, images, reference_text=""):
        nonlocal baseline_calls, candidate_calls
        if "Datara 字段级提取提示词优化器" in instructions:
            return json.dumps({"analyses": [], "candidate_rules": [{
                "field_id": field_id, "old_rule_hash": hashlib.sha256(b"").hexdigest(),
                "new_rule": "提取购买方完整名称，排除送货公司。", "reason": "区分购买与送货角色。",
            }]})
        if "排除送货公司" in instructions:
            candidate_calls += 1
            value = ("Delivery Entity Ltd." if final_degrades and candidate_calls in {4, 5}
                     else "Billing Entity Ltd.")
        else:
            baseline_calls += 1
            value = "Delivery Entity Ltd." if baseline_calls == 2 else "Billing Entity Ltd."
        return json.dumps({"AI_Document": {"company_name": value}})

    monkeypatch.setattr("datara.optimizer.completion", model)
    with TestClient(create_app(tmp_path)) as client:
        run, version = make_speed_case(client)
        store = client.app.state.store
        field_id = run["selected_fields"][0]["field_id"]
        run["settings"].update(evaluation_repeats=3, max_iterations=1, reuse_baseline_results=True)
        store.write_json(store.path("optimizer/runs", run["id"]), run)
        asyncio.run(client.app.state.optimizer_service.run(run["id"]))
        result = store.read_json("optimizer/runs", run["id"])
        assert result["status"] == "completed", result
        assert result["promotion_eligible"] is (not final_degrades)
        assert baseline_calls == 3 and candidate_calls == 6
        assert result["baseline_metrics"]["failure_selected"]["ratio"] == pytest.approx(2 / 3)
        assert result["baseline_metrics"]["case_results"][0]["passed"] == 2
        assert result["final_validation_metrics"]["case_results"][0]["passed"] == (1 if final_degrades else 3)
        assert len(result["result_summary"]["fixed"]) == (0 if final_degrades else 1)
        assert len(result["result_summary"]["remaining_failures"]) == (1 if final_degrades else 0)
        assert len(result["extraction_ids"]) == 9
        records = [store.read_json("optimizer/extractions", i) for i in result["extraction_ids"]]
        for phase in ("baseline", "candidate", "final_validation"):
            assert {r["repetition"] for r in records if r["phase"] == phase} == {1, 2, 3}
        assert all(not r["cache_hit"] for r in records)


def test_repeated_mode_ignores_existing_cache_and_does_not_invent_failure(tmp_path, monkeypatch):
    calls = 0
    async def correct(*args, **kwargs):
        nonlocal calls
        calls += 1
        return json.dumps({"AI_Document": {"company_name": "Billing Entity Ltd."}})
    monkeypatch.setattr("datara.optimizer.completion", correct)
    with TestClient(create_app(tmp_path)) as client:
        run, version = make_speed_case(client)
        store = client.app.state.store
        service = client.app.state.optimizer_service
        asyncio.run(service._extract_version(run, version, "baseline", None))  # Seed a valid cache entry.
        run["settings"]["evaluation_repeats"] = 3
        run["extraction_ids"] = []
        store.write_json(store.path("optimizer/runs", run["id"]), run)
        asyncio.run(service.run(run["id"]))
        result = store.read_json("optimizer/runs", run["id"])
        assert calls == 4  # All three trials were real calls, despite the seed cache.
        assert result["cache_hits"] == 0
        assert result["iteration_ids"] == []
        assert result["promotion_eligible"] is False
        assert "本次通过不代表长期稳定" in result["blocking_reasons"][0]


def test_edit_historical_error_validates_clears_and_preserves_run_snapshot(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        run, _ = make_speed_case(client, observed=True)
        case = run["run_test_cases"][0]
        path = "/api/optimizer/test-cases/" + case["test_case_id"]
        replacement = {"AI_Document": {"company_name": "Another wrong entity"}}
        response = client.patch(path, json={"observed_output": replacement})
        assert response.status_code == 200
        assert response.json()["observed_output"] == replacement
        snapshot = client.app.state.store.read_json("optimizer/runs", run["id"])
        assert snapshot["run_test_cases"][0]["observed_output"] == case["observed_output"]
        assert client.patch(path, json={"observed_output": {"unknown_table": {}}}).status_code == 400
        assert client.patch(path, json={"observed_output": None}).json()["observed_output"] is None


@pytest.mark.parametrize("repeats", [0, 11])
def test_repeat_count_is_bounded(repeats):
    from datara.optimizer_models import OptimizationSettings
    with pytest.raises(ValueError):
        OptimizationSettings(evaluation_repeats=repeats)
