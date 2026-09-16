from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from .domain import (DEFAULT_SQL, SYSTEM, FieldDef, Model, Profile, TableDef, json_schema,
                     new_profile, normalize, strict_json, uid, validate_profile, validate_result,
                     infer_type, suggest_displays)
from .generators import export_zip, fingerprint, preview, prompt
from .importer import inspect_workbook, parse_fields
from .media import render_pages
from .provider import (Connection, ConnectionUpdate, completion, completion_retrying, draft_prompt,
                       parse_analysis, validate_connection)
from .references import reference_text, MAX_TEXT
from .storage import Conflict, Store
from .optimizer import ensure_prompt_version
from .optimizer_routes import register_optimizer_routes

def load_runtime_environment(root: Path | None = None) -> None:
    """Load local project secrets without overriding deployment environment variables."""
    load_dotenv((root or Path.cwd()) / ".env", override=False, encoding="utf-8")


load_runtime_environment()

STATIC = Path(__file__).parent / "static"


class ImportRequest(Model):
    import_id: str
    sheet: str
    columns: dict[str, str] | None = None
    repair_structure: bool = False


class AIRequest(Model):
    profile: Profile
    sample_id: str
    kind: str = "extract"
    instructions: str = ""


class ResultRequest(Model):
    profile: Profile
    raw: str


def create_app(data_dir: Path | None = None):
    store = Store(data_dir or Path(os.environ.get("DATARA_DATA_DIR", "data")))
    settings_path = store.root / "connection.json"
    connection = Connection.model_validate_json(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else Connection()
    jobs, tasks = {}, {}

    @asynccontextmanager
    async def lifespan(app):
        for path in (store.root / "tests").glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("status") == "running":
                record.update(status="interrupted", error="应用重启，请重新发起请求")
                store.write_json(path, record)
        for path in (store.root / "optimizer" / "runs").glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("status") in {"queued", "baselining", "optimizing", "validating"}:
                record.update(status="interrupted", stop_reason="interrupted", promotion_eligible=False,
                              blocking_reasons=["应用重启，请重新发起优化任务"],
                              finished_at=datetime.now(timezone.utc).isoformat())
                store.write_json(path, record)
        yield
        pending = list(tasks.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    app = FastAPI(title="Datara Profile Generator", lifespan=lifespan)
    app.state.store = store
    app.state.connection = connection
    app.state.api_key = os.environ.get("DATARA_API_KEY", "")

    @app.middleware("http")
    async def local_guard(request: Request, call_next):
        allowed = {"localhost", "127.0.0.1", "::1", "testserver"}
        allowed.update(x.strip() for x in os.getenv("DATARA_ALLOWED_HOSTS", "").split(",") if x.strip())
        if request.url.hostname not in allowed:
            return JSONResponse({"detail": "访问主机未授权"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.url.netloc:
                return JSONResponse({"detail": "仅允许从本应用页面发起修改请求"}, status_code=403)
        if int(request.headers.get("content-length", "0") or "0") > 25 * 1024 * 1024:
            return JSONResponse({"detail": "请求超过 25MB 上限"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(FileNotFoundError)
    async def missing(request, exc):
        return JSONResponse({"detail": "文件或草稿不存在"}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def malformed(request, exc):
        # Do not echo request bodies: settings may contain API credentials.
        messages = [".".join(map(str, e["loc"])) + ": " + e["msg"] for e in exc.errors()]
        return JSONResponse({"detail": "；".join(messages)}, status_code=422)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/meta")
    def metadata():
        return {"default_sql": DEFAULT_SQL, "system": SYSTEM, "version": "0.1.0"}

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/api/profiles")
    def profiles():
        return store.list_profiles()

    @app.post("/api/profiles/new")
    def create_profile():
        return new_profile()

    @app.get("/api/profiles/{identity}")
    def get_profile(identity: str):
        return store.load(identity)

    @app.post("/api/profiles/save")
    def save_profile(profile: Profile):
        normalize(profile)
        saved = store.save(profile)
        ensure_prompt_version(store, saved)
        return saved

    @app.post("/api/normalize")
    def normalize_profile(profile: Profile):
        notes = normalize(profile)
        return {"profile": profile.model_dump(), "notes": notes}

    @app.post("/api/fields/suggest")
    def suggest_fields(profile: Profile):
        notes = normalize(profile)
        for table in profile.tables:
            for field in table.fields:
                if field.source != "AI":
                    continue
                inferred = infer_type(field.name, field.description, field.data_type)
                if inferred != field.data_type:
                    notes.append(f"{table.name}.{field.name}：{field.data_type} → {inferred}")
                    field.data_type, field.reviewed = inferred, False
        suggest_displays(profile)
        return {"profile": profile.model_dump(), "notes": notes}

    @app.post("/api/preview")
    def get_preview(profile: Profile):
        normalize(profile)
        return preview(profile)

    @app.post("/api/export")
    def export(profile: Profile):
        normalize(profile)
        # Export must refer to a persisted revision, not a different unsaved payload.
        saved = store.load(profile.id)
        if saved.revision != profile.revision or fingerprint(saved) != fingerprint(profile):
            raise Conflict("请先保存当前修改，再导出相同版本")
        archive = export_zip(saved)
        identity = uid()
        store.path("exports", identity, ".zip").write_bytes(archive)
        store.write_json(store.path("exports", identity), {"profile": saved.model_dump(), "fingerprint": fingerprint(saved)})
        return Response(archive, media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="datara-profile.zip"'})

    @app.post("/api/results/validate")
    def validate_manual_result(body: ResultRequest):
        return validate_result(body.profile, strict_json(body.raw))

    @app.post("/api/schema")
    def schema(profile: Profile):
        return json_schema(profile)

    @app.post("/api/samples")
    async def upload_sample(file: UploadFile = File(...)):
        content = await file.read(20 * 1024 * 1024 + 1)
        if len(content) > 20 * 1024 * 1024:
            raise ValueError("文件超过 20MB，请压缩或拆分后上传")
        suffix = Path(file.filename or "").suffix.lower()
        identity = uid()
        folder = store.root / "samples" / identity
        try:
            pages = await asyncio.to_thread(render_pages, content, suffix, folder)
            (folder / ("original" + suffix)).write_bytes(content)
            meta = {"id": identity, "name": Path(file.filename or "sample").name, "pages": pages, "suffix": suffix}
            store.write_json(folder / "meta.json", meta)
            return meta
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def sample_meta(identity: str):
        folder = store.path("samples", identity, "")
        return folder, json.loads((folder / "meta.json").read_text(encoding="utf-8"))

    @app.get("/api/samples/{identity}")
    def sample(identity: str):
        return sample_meta(identity)[1]

    @app.get("/api/samples/{identity}/pages/{page}")
    def sample_page(identity: str, page: int):
        folder, meta = sample_meta(identity)
        if page < 1 or page > meta["pages"]:
            raise HTTPException(404, "页面不存在")
        return FileResponse(folder / f"{page}.jpg")

    @app.post("/api/import/inspect")
    async def inspect_excel(file: UploadFile = File(...)):
        content = await file.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            raise ValueError("Excel 文件超过 8MB")
        result = await asyncio.to_thread(inspect_workbook, content)
        identity = uid()
        store.path("imports", identity, ".xlsx").write_bytes(content)
        return {"import_id": identity, **result}

    @app.post("/api/import/apply")
    def import_excel(body: ImportRequest):
        profile, notes = parse_fields(store.path("imports", body.import_id, ".xlsx").read_bytes(), body.sheet, body.columns, body.repair_structure)
        return {"profile": profile.model_dump(), "notes": notes}

    @app.post("/api/references")
    async def upload_reference(file: UploadFile = File(...)):
        content = await file.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            raise ValueError("参考文件超过 8MB")
        name = Path(file.filename or "reference.txt").name
        text = await asyncio.to_thread(reference_text, content, name)
        identity = uid()
        record = {"id": identity, "name": name, "text": text, "characters": len(text)}
        store.write_json(store.path("references", identity), record)
        return {k: record[k] for k in ("id", "name", "characters")}

    @app.get("/api/references/{identity}")
    def reference_meta(identity: str):
        record = json.loads(store.path("references", identity).read_text(encoding="utf-8"))
        return {k: record[k] for k in ("id", "name", "characters")}

    def analysis_references(profile: Profile):
        records = [json.loads(store.path("references", identity).read_text(encoding="utf-8"))
                   for identity in profile.reference_ids]
        text = json.dumps([{"filename": r["name"], "content": r["text"]} for r in records], ensure_ascii=False) if records else ""
        if len(text) > MAX_TEXT:
            raise ValueError("本次参考资料合计超过 60,000 字符，请移除部分资料后重试")
        return text

    def public_connection():
        return {**app.state.connection.model_dump(), "has_key": bool(app.state.api_key)}

    @app.get("/api/settings")
    def settings():
        return public_connection()

    @app.post("/api/settings")
    def update_settings(body: ConnectionUpdate):
        c = Connection(**body.model_dump(exclude={"api_key", "clear_key"}))
        validate_connection(c)
        app.state.connection = c
        if body.clear_key:
            app.state.api_key = ""
        elif body.api_key:
            app.state.api_key = body.api_key
        store.write_json(settings_path, c.model_dump())
        return public_connection()

    @app.post("/api/settings/test")
    async def test_connection():
        raw = await completion(app.state.connection, app.state.api_key, '仅回复 JSON：{"ok":true}', [])
        return {"ok": True, "message": "文本连接成功；图片能力请使用样本进行测试提取。", "response": raw[:200]}

    async def run_job(identity, body, c, key):
        record = jobs[identity]
        try:
            folder, meta = sample_meta(body.sample_id)
            images = [folder / f"{i}.jpg" for i in range(1, meta["pages"] + 1)]
            instructions = prompt(body.profile) if body.kind == "extract" else draft_prompt(body.profile, body.instructions)
            if body.kind == "draft" and body.profile.reference_ids:
                raw = await completion_retrying(c, key, instructions, images,
                                                reference_text=analysis_references(body.profile))
            else:
                # Interactive extract/draft jobs used to make a single call, so one
                # transient 15 second connect timeout from a corporate proxy failed the
                # whole 测试提取 run even though the error text said it was retryable.
                raw = await completion_retrying(c, key, instructions, images)
            record["raw"] = raw
            if body.kind == "draft":
                record.update(parse_analysis(raw, body.profile))
            else:
                try:
                    value = strict_json(raw)
                    record["result"] = value
                    record["validation"] = validate_result(body.profile, value)
                except ValueError as e:
                    record["validation"] = {"errors": [str(e)], "warnings": [], "status": "invalid"}
            record["status"] = "completed"
        except asyncio.CancelledError:
            record.update(status="cancelled", error="已取消请求")
        except Exception as e:
            # Errors from third-party libraries must not expose credentials.
            safe = str(e).replace(key, "[redacted]") if key else str(e)
            record.update(status="failed", error=safe[:1000])
        finally:
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            store.write_json(store.path("tests", identity), record)
            tasks.pop(identity, None)

    @app.post("/api/jobs")
    async def start_job(body: AIRequest):
        if body.kind not in {"draft", "extract"}:
            raise ValueError("未知的任务类型")
        if len(tasks) >= 2:
            raise ValueError("已有两个模型任务进行中，请等待或取消后重试")
        normalize(body.profile)
        if body.kind == "extract":
            errors = validate_profile(body.profile)["errors"]
            if errors:
                raise ValueError("；".join(errors))
        sample_meta(body.sample_id)
        if body.kind == "draft":
            analysis_references(body.profile)
        c = app.state.connection.model_copy(deep=True)
        key = app.state.api_key
        validate_connection(c)
        if not key:
            raise ValueError("请先在模型设置中填写 API Key")
        identity = uid()
        record = {"id": identity, "kind": body.kind, "status": "running", "profile_id": body.profile.id,
                  "profile_revision": body.profile.revision, "fingerprint": fingerprint(body.profile),
                  "sample_id": body.sample_id, "model": c.model, "started_at": datetime.now(timezone.utc).isoformat()}
        if body.kind == "extract":
            record["prompt_hash"] = hashlib.sha256(prompt(body.profile).encode()).hexdigest()
        jobs[identity] = record
        store.write_json(store.path("tests", identity), record)
        tasks[identity] = asyncio.create_task(run_job(identity, body, c, key))
        return record

    @app.get("/api/jobs/{identity}")
    def get_job(identity: str):
        return jobs.get(identity) or json.loads(store.path("tests", identity).read_text(encoding="utf-8"))

    @app.post("/api/jobs/{identity}/cancel")
    async def cancel_job(identity: str):
        if identity in tasks:
            tasks[identity].cancel()
            await asyncio.sleep(0)
        return {"ok": True}

    @app.get("/api/profiles/{identity}/tests")
    def tests(identity: str):
        store.path("profiles", identity)
        records = [json.loads(p.read_text(encoding="utf-8")) for p in (store.root / "tests").glob("*.json")]
        return sorted([r for r in records if r["profile_id"] == identity], key=lambda r: r["started_at"], reverse=True)[:10]

    @app.post("/api/demo/{kind}")
    def demo(kind: str):
        if kind not in {"cheque", "invoice"}:
            raise ValueError("未知示例")
        p = new_profile("支票收款" if kind == "cheque" else "供应商发票", "AI_ChequePay" if kind == "cheque" else "AI_Invoice_Head")
        p.accepted_documents = "支票支付单据" if kind == "cheque" else "供应商发票或收据"
        p.rejected_documents = "采购单、运货单、报关单"
        if kind == "cheque":
            defs = [("cheque_date", "支票日期", "Date"), ("cheque_number", "支票号码", "String"),
                    ("customer_name", "出票人名称", "String"), ("bank_sort_code", "银行代码", "String"),
                    ("account_no", "银行账号", "String"), ("amount", "支票金额", "Decimal"),
                    ("company_name", "收款公司名称", "String"), ("currency", "币种", "String")]
        else:
            defs = [("company_name", "购买方公司名称", "String"), ("invoice_number", "发票号码", "String"),
                    ("invoice_date", "发票日期", "Date"), ("invoice_amount", "发票总额", "Decimal"),
                    ("currency", "币种", "String"), ("tax_amount", "税额", "Decimal"),
                    ("vendor_name_chinese", "供应商中文名称", "String"), ("vendor_name_english", "供应商英文名称", "String")]
            for name, fields in [("AI_Invoice_Detail", [("description", "项目说明", "String"), ("quantity", "数量", "Decimal"),
                                                        ("unit", "单位", "String"), ("unit_price", "单价", "Decimal"),
                                                        ("line_amount", "行金额", "Decimal"), ("tax_rate", "税率", "Decimal")]),
                                 ("AI_POGR", [("po_number", "采购单号", "String"), ("gr_number", "收货单号", "String")])]:
                t = TableDef(name=name, role="detail", parent_table_id=p.tables[0].id,
                             fields=[FieldDef(name=n, description=d, data_type=dt) for n, d, dt in fields])
                if name == "AI_Invoice_Detail":
                    t.fields.append(FieldDef(name="item", description="明细行号", source="System", population="Datara 按数组顺序从 1 递增"))
                p.tables.append(t)
        p.tables[0].fields = [FieldDef(name=n, description=d, data_type=dt, is_required=n in {"invoice_number", "amount"}) for n, d, dt in defs] + p.tables[0].fields
        p.tables[0].fields.append(FieldDef(name="company_code", description="公司代码", source="System", population="Datara 根据公司名称查询"))
        if kind == "cheque":
            p.tables[0].fields.append(FieldDef(name="comment", description="人工备注", source="Manual"))
        normalize(p)
        return p

    register_optimizer_routes(app, store, tasks)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
