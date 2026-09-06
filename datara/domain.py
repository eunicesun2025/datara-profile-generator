from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def uid() -> str:
    return str(uuid4())


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldDef(Model):
    id: str = Field(default_factory=uid)
    name: str = Field(max_length=128)
    description: str = Field(default="", max_length=2000)
    source: Literal["AI", "Manual", "System"] = "AI"
    data_type: Literal["String", "Integer", "Decimal", "Date", "Boolean", "Choice"] = "String"
    is_required: bool = False
    choice_values: list[str] = Field(default_factory=list, max_length=200)
    sample_data: str | int | float | bool | None = None
    head_display: int | None = Field(default=None, ge=1, le=999)
    field_order: int = 1
    sql_type: str | None = Field(default=None, max_length=80)
    extraction: str = Field(default="", max_length=5000)
    population: str = Field(default="", max_length=1000)
    reviewed: bool = True


class TableDef(Model):
    id: str = Field(default_factory=uid)
    name: str = Field(max_length=100)
    role: Literal["head", "detail"] = "head"
    parent_table_id: str | None = None
    fields: list[FieldDef] = Field(default_factory=list, max_length=200)


class Profile(Model):
    id: str = Field(default_factory=uid)
    schema_version: Literal["1.0"] = "1.0"
    revision: int = Field(default=0, ge=0)
    name: str = Field(default="未命名 Profile", max_length=120)
    description: str = Field(default="", max_length=2000)
    accepted_documents: str = Field(default="", max_length=2000)
    rejected_documents: str = Field(default="", max_length=2000)
    document_rules: str = Field(default="", max_length=5000)
    database_name: str = Field(default="", max_length=128)
    database_schema: str = Field(default="dbo", max_length=128)
    tables: list[TableDef] = Field(default_factory=list, max_length=20)
    sample_ids: list[str] = Field(default_factory=list, max_length=20)
    updated_at: str | None = None


DEFAULT_SQL = {"String": "nvarchar(200)", "Integer": "int", "Decimal": "decimal(18,2)",
               "Date": "date", "Boolean": "bit", "Choice": "nvarchar(50)"}
SYSTEM = {
    "id": ("Integer", "int", "数据库自增主键"),
    "file_id": ("Integer", "int", "文件关联 ID"),
    "created_at": ("Date", "datetime", "UTC 创建时间"),
    "updated_at": ("Date", "datetime", "更新时间"),
    "is_active": ("Boolean", "bit", "有效状态"),
    "interface_status": ("Integer", "int", "接口状态"),
    "modified_by": ("String", "nvarchar(50)", "修改人"),
    "head_id": ("Integer", "int", "主表关联 ID"),
}
BASE_SYSTEM = ["id", "file_id", "created_at", "updated_at", "is_active"]
HEAD_SYSTEM = BASE_SYSTEM + ["interface_status", "modified_by"]
DETAIL_SYSTEM = BASE_SYSTEM + ["head_id"]
COLUMNS = ["TableName", "TableLevel", "ForeignKeyField", "ColumnName", "SampleData", "DataType",
           "ChoiceValues", "IsRequired", "Source", "HeadDisplay", "FieldOrder"]
FIELD_NAME = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def required_names(table: TableDef) -> list[str]:
    return HEAD_SYSTEM if table.role == "head" else DETAIL_SYSTEM


def normalize(profile: Profile, repair_system: bool = False) -> list[str]:
    """Add mandatory fields and derive order. Only imports may repair legacy sources."""
    notes = []
    for table in profile.tables:
        for name in required_names(table):
            existing = next((f for f in table.fields if f.name == name), None)
            dtype, sql, label = SYSTEM[name]
            if not existing:
                table.fields.append(FieldDef(name=name, description=label, source="System",
                                             data_type=dtype, sql_type=sql))
                notes.append(f"{table.name}：补入 System 字段 {name}")
            elif repair_system:
                if existing.source != "System" or existing.data_type != dtype or effective_sql(existing) != sql:
                    notes.append(f"{table.name}.{name}：按统一 System 规则修正来源和类型")
                existing.source, existing.data_type, existing.sql_type = "System", dtype, sql
        for i, field in enumerate(table.fields, 1):
            field.field_order = i
    return notes


def new_profile(name: str = "新建 Profile", table_name: str = "AI_Document") -> Profile:
    p = Profile(name=name, tables=[TableDef(name=table_name)])
    normalize(p)
    return p


def effective_sql(field: FieldDef) -> str:
    return (field.sql_type or DEFAULT_SQL[field.data_type]).lower().replace(" ", "")


def valid_sql_type(value: str) -> bool:
    if value in {"int", "bigint", "smallint", "tinyint", "bit", "date", "datetime", "datetime2"}:
        return True
    match = re.fullmatch(r"(nvarchar|varchar|nchar|char)\((max|\d+)\)", value)
    if match:
        kind, length = match.groups()
        return (length == "max" and kind in {"nvarchar", "varchar"}) or (
            length != "max" and 1 <= int(length) <= (4000 if kind.startswith("n") else 8000))
    match = re.fullmatch(r"decimal\((\d+),(\d+)\)", value)
    if match:
        precision, scale = map(int, match.groups())
        return 1 <= precision <= 38 and 0 <= scale <= precision
    return False


def ai_tables(p: Profile):
    return [(t, [f for f in t.fields if f.source == "AI"]) for t in p.tables
            if any(f.source == "AI" for f in t.fields)]


def validate_profile(p: Profile) -> dict:
    errors, warnings = [], []
    heads = [t for t in p.tables if t.role == "head"]
    if len(heads) != 1:
        errors.append("必须有且只有一张主表")
    for name in [p.database_schema] + ([p.database_name] if p.database_name else []):
        if not IDENTIFIER.fullmatch(name):
            errors.append(f"数据库或 schema 名称无效：{name}")
    names, ids = set(), set()
    for table in p.tables:
        if not IDENTIFIER.fullmatch(table.name):
            errors.append(f"表名无效：{table.name}，请使用英文、数字和下划线")
        if table.name.lower() in names:
            errors.append(f"表名重复：{table.name}")
        names.add(table.name.lower())
        if table.id in ids:
            errors.append("表内部 ID 重复")
        ids.add(table.id)
        if table.role == "head" and table.parent_table_id:
            errors.append(f"{table.name}：主表不能有父表")
        if table.role == "detail" and (len(heads) != 1 or table.parent_table_id != heads[0].id):
            errors.append(f"{table.name}：子表必须直接关联主表")
        seen, seen_ids, displays = set(), set(), set()
        for field in table.fields:
            path = f"{table.name}.{field.name}"
            if not FIELD_NAME.fullmatch(field.name):
                errors.append(f"{path}：字段名必须是全小写 snake_case")
            if field.name in seen or field.id in seen_ids:
                errors.append(f"{path}：字段名称或内部 ID 重复")
            seen.add(field.name)
            seen_ids.add(field.id)
            if field.name in SYSTEM:
                dtype, sql, _ = SYSTEM[field.name]
                if field.source != "System" or field.data_type != dtype or effective_sql(field) != sql:
                    errors.append(f"{path}：保留 System 字段的来源和类型不可修改")
                if field.name == "head_id" and table.role == "head":
                    errors.append(f"{path}：主表不能包含 head_id")
            if (field.name in {"company_code", "current_date"} or
                (field.name == "item" and table.name == "AI_Invoice_Detail")) and field.source != "System":
                errors.append(f"{path}：已定义为由 Datara 填充的字段，来源必须为 System")
            if not valid_sql_type(effective_sql(field)):
                errors.append(f"{path}：SQL 类型或长度/精度无效")
            if field.data_type == "Choice":
                if not field.choice_values or any(not x.strip() or "," in x or "，" in x for x in field.choice_values):
                    errors.append(f"{path}：选项不能为空，单个选项不能含逗号")
                if len(set(field.choice_values)) != len(field.choice_values):
                    errors.append(f"{path}：选项重复")
                if field.sample_data is not None and str(field.sample_data) not in field.choice_values:
                    warnings.append(f"{path}：样本值不在选项中")
            if field.head_display is not None:
                if field.head_display in displays:
                    errors.append(f"{table.name}：列表显示次序 {field.head_display} 重复")
                displays.add(field.head_display)
            if not field.reviewed:
                warnings.append(f"{path}：请审核建议字段")
            if field.source == "System" and field.name not in SYSTEM and not field.population:
                warnings.append(f"{path}：尚未说明 Datara 如何填充")
            if field.sql_type and effective_sql(field) != DEFAULT_SQL[field.data_type]:
                if field.name not in SYSTEM:
                    warnings.append(f"{path}：使用自定义 SQL 存储类型，请确认容量与精度")
        for name in required_names(table):
            if name not in seen:
                errors.append(f"{table.name}：缺少 System 字段 {name}")
    if not ai_tables(p):
        errors.append("至少添加一个 Source=AI 的业务字段后才能生成提取包")
    if not p.accepted_documents.strip():
        warnings.append("尚未填写接受的单据类型，建议补充后进行测试")
    # Detect explicit conflicts. Free-form business instructions still need human review.
    rule_text = p.document_rules + "\n" + "\n".join(f.extraction for _, fs in ai_tables(p) for f in fs)
    if re.search(r"(?:缺失|未找到|无法识别|找不到|未识别)[^。\n]{0,35}(?:返回|设为|填[入充]?|默认)[\s:=：]*(?:0(?:\.0)?\b|false\b|HKD\b|空字符串)", rule_text, re.I):
        errors.append("提取规则与统一 null 规则冲突：请删除缺失时补默认值的要求")
    if re.search(r"(?:输出|返回|提取)[^。\n]{0,10}\b(?:current_date|head_id|file_id|modified_by)\b", rule_text):
        errors.append("提取规则要求输出系统字段，请移除")
    return {"errors": errors, "warnings": warnings}


def json_structure(p: Profile) -> dict:
    return {table.name: ({f.name: None for f in fields} if table.role == "head" else
                        [{f.name: None for f in fields}]) for table, fields in ai_tables(p)}


def json_schema(p: Profile) -> dict:
    props = {}
    types = {"String": "string", "Choice": "string", "Date": "string", "Integer": "integer",
             "Decimal": "number", "Boolean": "boolean"}
    for table, fields in ai_tables(p):
        fp = {}
        for f in fields:
            spec = {"type": [types[f.data_type], "null"]}
            if f.data_type == "Date":
                spec["pattern"] = "^[0-9]{8}$"
            if f.data_type == "Choice":
                spec["enum"] = f.choice_values + [None]
            fp[f.name] = spec
        obj = {"type": "object", "properties": fp, "required": list(fp), "additionalProperties": False}
        props[table.name] = obj if table.role == "head" else {"type": "array", "items": obj}
    return {"oneOf": [{"type": "object", "maxProperties": 0},
                      {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}]}


def validate_result(p: Profile, value) -> dict:
    errors, warnings = [], []
    if type(value) is not dict:
        return {"errors": ["根节点必须为 JSON 对象"], "warnings": [], "status": "invalid"}
    if value == {}:
        return {"errors": [], "warnings": [], "status": "not_matched"}
    expected = {t.name for t, _ in ai_tables(p)}
    for name in sorted(expected - value.keys()):
        errors.append(f"缺少表：{name}")
    for name in sorted(value.keys() - expected):
        errors.append(f"未定义的表：{name}")
    for table, fields in ai_tables(p):
        if table.name not in value:
            continue
        data = value[table.name]
        if table.role == "detail" and type(data) is not list:
            errors.append(f"{table.name}：必须是数组")
            continue
        rows = data if table.role == "detail" else [data]
        for i, row in enumerate(rows):
            prefix = table.name + (f"[{i}]" if table.role == "detail" else "")
            if type(row) is not dict:
                errors.append(f"{prefix}：必须是对象")
                continue
            expected_fields = {f.name for f in fields}
            for name in sorted(row.keys() - expected_fields):
                errors.append(f"{prefix}.{name}：额外字段（System/Manual 不得输出）")
            for f in fields:
                path = f"{prefix}.{f.name}"
                if f.name not in row:
                    errors.append(f"{path}：缺少键，无法识别也必须保留并返回 null")
                    continue
                v = row[f.name]
                if v is None:
                    if f.is_required:
                        warnings.append(f"{path}：必填值待人工补充")
                    continue
                dt = f.data_type
                valid = (type(v) is str if dt in {"String", "Choice", "Date"} else
                         type(v) is int if dt == "Integer" else
                         type(v) in {int, float} if dt == "Decimal" else type(v) is bool)
                if not valid:
                    errors.append(f"{path}：类型错误，要求 {dt}")
                    continue
                if v == "":
                    errors.append(f"{path}：未识别请返回 null，不能返回空字符串")
                if dt == "Date":
                    try:
                        if not re.fullmatch(r"[0-9]{8}", v):
                            raise ValueError()
                        datetime.strptime(v, "%Y%m%d")
                    except ValueError:
                        errors.append(f"{path}：需要有效的 YYYYMMDD 日期")
                if dt == "Choice" and v not in f.choice_values:
                    errors.append(f"{path}：值不在选项中")
                if dt == "Decimal":
                    m = re.fullmatch(r"decimal\((\d+),(\d+)\)", effective_sql(f))
                    if m:
                        precision, scale = map(int, m.groups())
                        n = Decimal(str(v))
                        if not n.is_finite() or n.as_tuple().exponent < -scale or abs(n) >= Decimal(10) ** (precision - scale):
                            errors.append(f"{path}：金额超出存储精度或小数位数")
    return {"errors": errors, "warnings": warnings, "status": "invalid" if errors else "valid"}


def strict_json(raw: str):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError(f"重复 JSON 键：{key}")
            out[key] = value
        return out

    def invalid_constant(s):
        raise ValueError(f"非法 JSON 数值：{s}")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
