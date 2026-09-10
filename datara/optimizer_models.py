from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from .domain import Model


class ComparisonPolicy(Model):
    mode: Literal["exact", "case_insensitive", "normalized", "semantic"] = "exact"
    date_order: Literal["YMD", "DMY", "MDY"] | None = None
    grouping_separators: list[str] = Field(default_factory=lambda: [",", " ", "\u00a0"], max_length=5)
    currency_tokens: list[str] = Field(default_factory=list, max_length=10)
    semantic_threshold: float = Field(default=0.90, ge=0.5, le=1.0)


class OptimizationSettings(Model):
    max_iterations: int = Field(default=3, ge=1, le=20)
    early_stop_enabled: bool = True
    no_improvement_limit: int = Field(default=2, ge=1, le=10)
    minimum_improvement: float = Field(default=0.01, ge=0.0, le=1.0)
    target_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)
    max_regression_drop: float = Field(default=0.0, ge=0.0, le=0.05)
    final_validation_rerun: bool = True
    reuse_baseline_results: bool = True


class TestCaseCreate(Model):
    sample_id: str
    name: str = Field(default="Test case", min_length=1, max_length=200)
    dataset_role: Literal["failure", "regression"]
    ground_truth: dict[str, Any]
    observed_output: dict[str, Any] | None = None


class TestCaseUpdate(Model):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    dataset_role: Literal["failure", "regression"] | None = None
    enabled: bool | None = None


class GroundTruthCreate(Model):
    expected_output: dict[str, Any]


class PromptImportRequest(Model):
    expected_profile_revision: int = Field(ge=1)
    prompt_text: str = Field(min_length=20, max_length=100_000)


class OptimizationRunCreate(Model):
    profile_id: str
    profile_revision: int = Field(ge=1)
    baseline_prompt_version_id: str | None = None
    selected_field_ids: list[str] = Field(min_length=1, max_length=20)
    failure_test_case_ids: list[str] = Field(min_length=1, max_length=50)
    regression_test_case_ids: list[str] = Field(default_factory=list, max_length=50)
    evaluation_policies: dict[str, ComparisonPolicy] = Field(default_factory=dict)
    settings: OptimizationSettings = Field(default_factory=OptimizationSettings)

    @model_validator(mode="after")
    def unique_ids(self):
        for label, values in (("selected_field_ids", self.selected_field_ids),
                              ("failure_test_case_ids", self.failure_test_case_ids),
                              ("regression_test_case_ids", self.regression_test_case_ids)):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} contains duplicate IDs")
        if set(self.failure_test_case_ids) & set(self.regression_test_case_ids):
            raise ValueError("A test case cannot be in both datasets")
        return self


class PromotionRequest(Model):
    expected_profile_revision: int = Field(ge=1)
    expected_active_version_id: str
    optimization_run_id: str | None = None


class CandidateAnalysis(Model):
    field_id: str
    table_name: str = Field(max_length=100)
    field_name: str = Field(max_length=128)
    error_type: Literal[
        "missing_value", "wrong_region", "label_confusion", "formatting",
        "multiple_candidates", "table_alignment", "document_boundary",
        "unsupported_pattern", "unknown",
    ] = "unknown"
    root_cause: str = Field(max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=20)
    suggested_change: str = Field(max_length=2000)


class CandidateRule(Model):
    field_id: str
    old_rule_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    new_rule: str = Field(min_length=1, max_length=5000)
    reason: str = Field(max_length=2000)


class CandidateResponse(Model):
    analyses: list[CandidateAnalysis] = Field(default_factory=list, max_length=20)
    candidate_rules: list[CandidateRule] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_rules(self):
        ids = [rule.field_id for rule in self.candidate_rules]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate_rules contains duplicate field IDs")
        return self
