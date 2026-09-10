import asyncio
import hashlib
import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from datara.app import create_app
from datara.domain import FieldDef, TableDef, new_profile, normalize
from datara.evaluation import compare_values, evaluate_output
from datara.generators import profile_with_field_rules, prompt_components
from datara.optimizer import IMPORTED_OVERRIDE_MARKER, PromptOptimizerService, ensure_prompt_version
from datara.optimizer_models import CandidateResponse, ComparisonPolicy
from datara.provider import Connection
from datara.storage import Store


def image_bytes(color="white"):
    out = io.BytesIO()
    Image.new("RGB", (30, 30), color).save(out, "PNG")
    return out.getvalue()


def test_exact_and_normalized_comparisons():
    assert not compare_values("HKD", "hkd", "String", ComparisonPolicy(mode="exact"))["matched"]
    assert compare_values("2026-09-01", "01/09/2026", "Date",
                          ComparisonPolicy(mode="normalized", date_order="DMY"))["matched"]
    assert compare_values("1,000.00", 1000, "Decimal",
                          ComparisonPolicy(mode="normalized"))["matched"]
    assert not compare_values("01/09/2026", "2026-09-01", "Date",
                              ComparisonPolicy(mode="normalized"))["matched"]


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


def test_optimizer_workflow_promote_and_version_history(tmp_path, monkeypatch):
    failure_sample_id = None
    field_id = None

    async def fake_completion(connection, key, instructions, images, reference_text=""):
        assert key == "optimizer-key"
        if not images:
            return json.dumps({
                "analyses": [{
                    "field_id": field_id, "table_name": "AI_Document", "field_name": "amount",
                    "error_type": "wrong_region", "root_cause": "The total label was not prioritized.",
                    "evidence": ["failure case"], "suggested_change": "Prioritize the grand total.",
                }],
                "candidate_rules": [{
                    "field_id": field_id, "old_rule_hash": hashlib.sha256(b"").hexdigest(),
                    "new_rule": "Read the value next to the grand total label.",
                    "reason": "Avoid subtotal values.",
                }],
            })
        is_failure = images[0].parent.name == failure_sample_id
        optimized = "grand total label" in instructions
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
        assert current["extraction"] == "Read the value next to the grand total label."
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
        if not images:
            return json.dumps({"analyses": [], "candidate_rules": [{
                "field_id": field_id, "old_rule_hash": hashlib.sha256(b"").hexdigest(),
                "new_rule": "candidate rule", "reason": "fix failure",
            }]})
        failure = images[0].parent.name == failure_sample_id
        candidate = "candidate rule" in instructions
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


def test_invoice_company_optimizer_runs_with_imported_prompt_and_no_regression_set(tmp_path, monkeypatch):
    sample_expectations = {}
    company_field_id = None

    async def fake_completion(connection, key, instructions, images, reference_text=""):
        if not images:
            return json.dumps({
                "analyses": [{
                    "field_id": company_field_id, "table_name": "AI_Invoice_Head",
                    "field_name": "company_name", "error_type": "label_confusion",
                    "root_cause": "The prompt allowed a brand-keyword fallback to beat the buyer role.",
                    "evidence": ["BILL TO and footer/header contain different entities from the same group"],
                    "suggested_change": "Use the invoiced customer role and ignore seller/header/footer names.",
                }],
                "candidate_rules": [{
                    "field_id": company_field_id,
                    "old_rule_hash": hashlib.sha256(b"buyer company rule").hexdigest(),
                    "new_rule": ("Extract the legal entity that is the invoice recipient or buyer. Prefer BILL TO, "
                                 "BILLED TO, INVOICE TO, BUYER, or CUSTOMER. On self-billing invoices, use the "
                                 "recipient address block. Never choose a seller, issuer, logo, corporate-office "
                                 "header, remittance block, or page footer merely because it shares a brand."),
                    "reason": "Resolve multiple group entities by business role and page region.",
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
        imported_text = "Published supplier invoice prompt with existing JSON and field instructions."
        imported = client.post(f"/api/profiles/{profile['id']}/prompt-versions/import", json={
            "expected_profile_revision": profile["revision"], "prompt_text": imported_text,
        }).json()

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
        saved_fields = promoted.json()["profile"]["tables"][0]["fields"]
        assert next(f for f in saved_fields if f["id"] == invoice_number.id)["extraction"] == "keep invoice number rule"
