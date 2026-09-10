from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .domain import Profile
from .optimizer_models import ComparisonPolicy


LEGAL_SUFFIXES = {
    "co", "company", "limited", "ltd", "inc", "incorporated", "corp", "corporation",
    "llc", "plc", "有限公司", "有限责任公司", "股份有限公司",
}


def default_policy(data_type: str) -> ComparisonPolicy:
    return ComparisonPolicy(mode="normalized" if data_type in {"Date", "Integer", "Decimal"} else "exact")


def _date(value: Any, order: str | None) -> str:
    if not isinstance(value, str):
        raise ValueError("date must be a string")
    text = unicodedata.normalize("NFKC", value).strip()
    formats = ["%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"]
    if order == "DMY":
        formats += ["%d/%m/%Y", "%d-%m-%Y"]
    elif order == "MDY":
        formats += ["%m/%d/%Y", "%m-%d-%Y"]
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).strftime("%Y%m%d")
        except ValueError:
            pass
    raise ValueError("unsupported or ambiguous date")


def _number(value: Any, policy: ComparisonPolicy, integer: bool) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError("not a number")
    text = unicodedata.normalize("NFKC", str(value)).strip()
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    for token in policy.currency_tokens:
        text = text.replace(token, "")
    for separator in policy.grouping_separators:
        text = text.replace(separator, "")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("invalid number") from exc
    if not number.is_finite() or (integer and number != number.to_integral_value()):
        raise ValueError("invalid integer" if integer else "invalid number")
    if negative:
        number = -number
    return str(number.normalize())


def _company_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("semantic comparison requires strings")
    text = unicodedata.normalize("NFKC", value).casefold()
    words = re.findall(r"[\w\u3400-\u9fff]+", text)
    return "".join(word for word in words if word not in LEGAL_SUFFIXES)


def compare_values(expected: Any, actual: Any, data_type: str,
                   policy: ComparisonPolicy) -> dict:
    result = {
        "comparison_mode": policy.mode,
        "expected": expected,
        "actual": actual,
        "normalized_expected": None,
        "normalized_actual": None,
        "matched": False,
        "indeterminate": False,
        "reason": "value_mismatch",
    }
    if expected is None or actual is None:
        result["matched"] = expected is None and actual is None
        result["reason"] = "matched" if result["matched"] else "null_mismatch"
        return result
    if policy.mode == "exact":
        result["matched"] = type(expected) is type(actual) and expected == actual
        result["reason"] = "matched" if result["matched"] else "exact_mismatch"
        return result
    try:
        if policy.mode == "semantic":
            if data_type != "String":
                raise ValueError("semantic comparison is only valid for String fields")
            left, right = _company_name(expected), _company_name(actual)
        elif data_type == "Date":
            left, right = _date(expected, policy.date_order), _date(actual, policy.date_order)
        elif data_type in {"Integer", "Decimal"}:
            left = _number(expected, policy, data_type == "Integer")
            right = _number(actual, policy, data_type == "Integer")
        else:
            left = unicodedata.normalize("NFKC", str(expected))
            right = unicodedata.normalize("NFKC", str(actual))
        result["normalized_expected"], result["normalized_actual"] = left, right
        result["matched"] = bool(left) and left == right
        result["reason"] = "matched" if result["matched"] else f"{policy.mode}_mismatch"
    except ValueError as exc:
        result["reason"] = "normalization_failed: " + str(exc)
    return result


def evaluate_output(profile: Profile, expected: dict, actual: Any,
                    selected_field_ids: set[str], policies: dict[str, ComparisonPolicy]) -> list[dict]:
    """Evaluate only paths present in ground truth; missing actual paths are mismatches."""
    evaluations = []
    actual_root = actual if isinstance(actual, dict) else {}
    for table in profile.tables:
        expected_table = expected.get(table.name)
        if expected_table is None:
            continue
        actual_table = actual_root.get(table.name)
        if table.role == "head":
            expected_rows = [expected_table] if isinstance(expected_table, dict) else []
            actual_rows = [actual_table] if isinstance(actual_table, dict) else []
        else:
            expected_rows = expected_table if isinstance(expected_table, list) else []
            actual_rows = actual_table if isinstance(actual_table, list) else []
        comparable_fields = [field for field in table.fields if field.source == "AI" and
                             (table.role == "head" or not expected_rows or
                              any(isinstance(row, dict) and field.name in row for row in expected_rows))]
        for row_index, expected_row in enumerate(expected_rows):
            if not isinstance(expected_row, dict):
                continue
            actual_row_present = row_index < len(actual_rows) and isinstance(actual_rows[row_index], dict)
            actual_row = actual_rows[row_index] if actual_row_present else {}
            for field in table.fields:
                if field.source != "AI" or field.name not in expected_row:
                    continue
                policy = policies.get(field.id) or default_policy(field.data_type)
                if not actual_row_present or field.name not in actual_row:
                    comparison = compare_values(expected_row[field.name], None, field.data_type, policy)
                    comparison.update(matched=False, reason="missing_actual_path")
                else:
                    comparison = compare_values(expected_row[field.name], actual_row[field.name], field.data_type, policy)
                comparison.update({
                    "table_id": table.id,
                    "table_name": table.name,
                    "field_id": field.id,
                    "field_name": field.name,
                    "row_index": None if table.role == "head" else row_index,
                    "path": f"{table.name}.{field.name}" if table.role == "head" else f"{table.name}[{row_index}].{field.name}",
                    "selected": field.id in selected_field_ids,
                    "policy_version": "v1",
                })
                evaluations.append(comparison)
        if table.role == "detail" and len(actual_rows) > len(expected_rows):
            for row_index in range(len(expected_rows), len(actual_rows)):
                actual_row = actual_rows[row_index] if isinstance(actual_rows[row_index], dict) else {}
                for field in comparable_fields:
                    policy = policies.get(field.id) or default_policy(field.data_type)
                    comparison = compare_values(None, actual_row.get(field.name), field.data_type, policy)
                    comparison.update({
                        "matched": False, "reason": "unexpected_row", "table_id": table.id,
                        "table_name": table.name, "field_id": field.id, "field_name": field.name,
                        "row_index": row_index, "path": f"{table.name}[{row_index}].{field.name}",
                        "selected": field.id in selected_field_ids, "policy_version": "v1",
                    })
                    evaluations.append(comparison)
    return evaluations


def accuracy(evaluations: list[dict], *, selected_only: bool = False) -> dict:
    rows = [row for row in evaluations if not selected_only or row["selected"]]
    comparable = [row for row in rows if not row.get("indeterminate")]
    matched = sum(bool(row["matched"]) for row in comparable)
    total = len(comparable)
    return {"matched": matched, "total": total, "ratio": matched / total if total else None}


def summarize_metrics(failure_evaluations: list[dict], regression_evaluations: list[dict]) -> dict:
    return {
        "failure_selected": accuracy(failure_evaluations, selected_only=True),
        "failure_all": accuracy(failure_evaluations),
        "regression_selected": accuracy(regression_evaluations, selected_only=True),
        "regression_all": accuracy(regression_evaluations),
    }
