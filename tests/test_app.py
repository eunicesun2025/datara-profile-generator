import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from PIL import Image

from datara.app import create_app
from datara.domain import FieldDef, new_profile
from datara.importer import inspect_workbook, parse_fields


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        yield c


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
    async def fake_completion(c,key,instructions,images):
        assert key == "test-only-key"
        assert images and images[0].exists()
        assert "file_id" not in instructions
        return '{"AI_Document":{"amount":null}}'
    monkeypatch.setattr("datara.app.completion",fake_completion)
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
