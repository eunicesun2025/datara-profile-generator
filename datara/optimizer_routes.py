from __future__ import annotations

import asyncio
import difflib

from fastapi import FastAPI

from .optimizer import (PromptOptimizerService, create_ground_truth, create_run_record,
                        create_test_case, ensure_prompt_version, import_prompt_version, list_prompt_versions,
                        promote_version, prompt_language, rollback_version)
from .optimizer_models import (GroundTruthCreate, OptimizationRunCreate, PromotionRequest,
                               PromptImportRequest, TestCaseCreate, TestCaseUpdate)
from .storage import Store


def register_optimizer_routes(app: FastAPI, store: Store, tasks: dict):
    service = PromptOptimizerService(store, lambda: app.state.connection, lambda: app.state.api_key)
    app.state.optimizer_service = service

    @app.get("/api/optimizer/profiles/{profile_id}/test-cases")
    def list_test_cases(profile_id: str):
        store.load(profile_id)
        cases = [c for c in store.list_json("optimizer/test_cases") if c["profile_id"] == profile_id]
        for case in cases:
            truth_id = case.get("active_ground_truth_id")
            case["ground_truth"] = store.read_json("optimizer/ground_truth", truth_id) if truth_id else None
        return sorted(cases, key=lambda c: (c["dataset_role"], c["created_at"]))

    @app.post("/api/optimizer/profiles/{profile_id}/test-cases")
    def add_test_case(profile_id: str, body: TestCaseCreate):
        return create_test_case(store, store.load(profile_id), body)

    @app.patch("/api/optimizer/test-cases/{identity}")
    def update_test_case(identity: str, body: TestCaseUpdate):
        record = store.read_json("optimizer/test_cases", identity)
        updates = body.model_dump(exclude_none=True)
        record.update(updates)
        from .optimizer import now
        record["updated_at"] = now()
        store.write_json(store.path("optimizer/test_cases", identity), record)
        return record

    @app.get("/api/optimizer/test-cases/{identity}/ground-truth")
    def get_ground_truth(identity: str):
        case = store.read_json("optimizer/test_cases", identity)
        return store.read_json("optimizer/ground_truth", case["active_ground_truth_id"])

    @app.post("/api/optimizer/test-cases/{identity}/ground-truth")
    def add_ground_truth(identity: str, body: GroundTruthCreate):
        case = store.read_json("optimizer/test_cases", identity)
        profile = store.load(case["profile_id"])
        truth = create_ground_truth(store, profile, identity, body.expected_output)
        from .optimizer import now
        case.update(active_ground_truth_id=truth["id"], updated_at=now())
        store.write_json(store.path("optimizer/test_cases", identity), case)
        return truth

    @app.post("/api/optimizer/runs")
    async def start_optimizer_run(body: OptimizationRunCreate):
        if len(tasks) >= 2:
            raise ValueError("已有两个模型任务进行中，请等待或取消后重试")
        if not app.state.api_key:
            raise ValueError("请先在模型设置中填写 API Key")
        run = create_run_record(store, body)

        async def execute():
            try:
                await service.run(run["id"])
            finally:
                tasks.pop(run["id"], None)

        tasks[run["id"]] = asyncio.create_task(execute())
        return run

    @app.get("/api/optimizer/runs/{identity}")
    def get_optimizer_run(identity: str):
        return store.read_json("optimizer/runs", identity)

    @app.get("/api/optimizer/runs/{identity}/iterations")
    def get_optimizer_iterations(identity: str):
        run = store.read_json("optimizer/runs", identity)
        return [store.read_json("optimizer/iterations", item) for item in run["iteration_ids"]]

    @app.get("/api/optimizer/runs/{identity}/iterations/{number}")
    def get_optimizer_iteration(identity: str, number: int):
        run = store.read_json("optimizer/runs", identity)
        for item in run["iteration_ids"]:
            record = store.read_json("optimizer/iterations", item)
            if record["number"] == number:
                return record
        raise FileNotFoundError()

    @app.post("/api/optimizer/runs/{identity}/cancel")
    async def cancel_optimizer_run(identity: str):
        run = store.read_json("optimizer/runs", identity)
        from .optimizer import now
        run["cancel_requested_at"] = now()
        store.write_json(store.path("optimizer/runs", identity), run)
        task = tasks.get(identity)
        if task:
            task.cancel()
            await asyncio.sleep(0)
        return {"ok": True}

    @app.get("/api/profiles/{profile_id}/prompt-versions")
    def prompt_versions(profile_id: str):
        profile = store.load(profile_id)
        active = ensure_prompt_version(store, profile)
        versions = list_prompt_versions(store, profile_id)
        for version in versions:
            version["prompt_language"] = version.get("prompt_language") or prompt_language(
                version.get("imported_prompt_base") or version["rendered_prompt"])
        return {"active_prompt_version_id": active["id"], "versions": [
            {k: v.get(k) for k in ("id", "version_number", "parent_version_id", "origin", "lifecycle",
                                    "profile_revision", "prompt_hash", "prompt_source", "prompt_language",
                                    "selected_field_ids",
                                    "optimization_run_id", "iteration_id", "metrics", "created_at")}
            for v in versions
        ]}

    @app.post("/api/profiles/{profile_id}/prompt-versions/import")
    def import_existing_prompt(profile_id: str, body: PromptImportRequest):
        profile = store.load(profile_id)
        version = import_prompt_version(store, profile, body.prompt_text, body.expected_profile_revision)
        response = {k: version.get(k) for k in (
            "id", "version_number", "origin", "lifecycle", "prompt_source", "profile_revision",
            "prompt_language", "prompt_hash", "created_at",
        )}
        response.update({k: version.get(k) for k in (
            "profile", "mapped_field_ids", "updated_field_ids", "unmapped_field_ids",
            "rejected_field_ids",
        )})
        return response

    @app.get("/api/prompt-versions/compare")
    def compare_prompt_versions(from_id: str, to_id: str):
        before = store.read_json("optimizer/prompt_versions", from_id)
        after = store.read_json("optimizer/prompt_versions", to_id)
        if before["profile_id"] != after["profile_id"]:
            raise ValueError("只能比较同一 Profile 的提示词版本")
        left = {f["field_id"]: f for f in before["field_rules"]}
        right = {f["field_id"]: f for f in after["field_rules"]}
        diffs = []
        for identity in left.keys() | right.keys():
            old, new = left.get(identity), right.get(identity)
            if old and new and old["extraction_rule"] == new["extraction_rule"]:
                continue
            old_text, new_text = (old or {}).get("extraction_rule", ""), (new or {}).get("extraction_rule", "")
            diffs.append({
                "field_id": identity, "table_name": (new or old)["table_name"],
                "field_name": (new or old)["field_name"], "old_rule": old_text, "new_rule": new_text,
                "diff": "\n".join(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(),
                                                        fromfile=f"v{before['version_number']}",
                                                        tofile=f"v{after['version_number']}", lineterm="")),
            })
        return {"from": from_id, "to": to_id, "diffs": diffs,
                "metrics_before": before.get("metrics"), "metrics_after": after.get("metrics")}

    @app.get("/api/prompt-versions/{identity}")
    def prompt_version(identity: str):
        return store.read_json("optimizer/prompt_versions", identity)

    @app.post("/api/prompt-versions/{identity}/promote")
    def promote(identity: str, body: PromotionRequest):
        profile, version = promote_version(store, identity, body.expected_profile_revision,
                                           body.expected_active_version_id, body.optimization_run_id)
        return {"profile": profile.model_dump(), "prompt_version": version}

    @app.post("/api/prompt-versions/{identity}/rollback")
    def rollback(identity: str, body: PromotionRequest):
        profile, version = rollback_version(store, identity, body.expected_profile_revision,
                                            body.expected_active_version_id)
        return {"profile": profile.model_dump(), "prompt_version": version}
