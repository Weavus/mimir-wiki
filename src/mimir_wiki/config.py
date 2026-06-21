from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from dotenv import dotenv_values, load_dotenv
from pydantic import BaseModel, Field

from mimir_wiki.constants import LLM_TASKS, SCHEMA_VERSION
from mimir_wiki.utils import deep_merge, redact_secrets, stable_hash


class PathConfig(BaseModel):
    cache: str | None = None
    knowledge: str = "knowledge"
    vault: str = "vault"
    dist: str = "dist/onyx-approved"
    dist_onyx_enriched: str = "dist/onyx-enriched"
    reports: str = "reports"
    runs: str = "runs"
    llm_cache: str = ".mimir-wiki/llm-cache"


class DeterministicFeatureConfig(BaseModel):
    document_classification: bool = True
    keywords: bool = True
    headings: bool = True
    links: bool = True
    quality_scoring: bool = True
    candidate_entities: bool = True


class LLMFeatureConfig(BaseModel):
    enabled: bool = False
    tasks: dict[str, bool] = Field(
        default_factory=lambda: {
            "classification": False,
            "summary": True,
            "keywords": False,
            "themes": True,
            "concepts": True,
            "candidate_entities": True,
            "operational_signals": True,
            "quality_warnings": True,
        }
    )


class OutputFeatureConfig(BaseModel):
    enrichment_json: bool = True
    document_index: bool = True
    quality_scores: bool = True
    themes: bool = True
    concepts: bool = True
    candidate_entities: bool = True
    onyx_poc_markdown: bool = True
    reports: bool = True


class FeatureConfig(BaseModel):
    deterministic: DeterministicFeatureConfig = Field(default_factory=DeterministicFeatureConfig)
    llm: LLMFeatureConfig = Field(default_factory=LLMFeatureConfig)
    outputs: OutputFeatureConfig = Field(default_factory=OutputFeatureConfig)


class TaskModelConfig(BaseModel):
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None


class TaskBundleConfig(BaseModel):
    tasks: list[str]
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None


class LLMConfig(BaseModel):
    provider: str = "none"
    model: str = "none"
    prompt_version: str = "enrichment-v1"
    task_models: dict[str, TaskModelConfig] = Field(default_factory=dict)
    task_bundles: dict[str, TaskBundleConfig] = Field(default_factory=dict)
    temperature: float = 0
    max_concurrency: int = 4
    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None
    adaptive_concurrency: bool = True
    adaptive_initial_concurrency: int = 4
    adaptive_min_concurrency: int = 1
    adaptive_increase_after_successes: int = 20
    adaptive_decrease_factor_on_429: float = 0.5
    adaptive_cooldown_seconds_on_429: float = 0
    adaptive_max_cooldown_seconds: float = 120
    max_retries: int = 3
    initial_backoff_seconds: float = 1
    max_backoff_seconds: float = 60
    backoff_jitter: bool = True
    respect_retry_after: bool = True
    timeout_seconds: float = 60
    fail_fast: bool = False
    retryable_status_codes: list[int] = Field(
        default_factory=lambda: [408, 409, 429, 500, 502, 503, 504]
    )
    cache_by: list[str] = Field(
        default_factory=lambda: [
            "source_content_hash",
            "prompt_version",
            "provider",
            "model",
            "task",
        ]
    )
    openai: dict[str, Any] = Field(default_factory=lambda: {"api_key_env": "OPENAI_API_KEY"})
    azure_openai: dict[str, Any] = Field(
        default_factory=lambda: {
            "endpoint_env": "AZURE_OPENAI_ENDPOINT",
            "api_key_env": "AZURE_OPENAI_API_KEY",
            "deployment_env": "AZURE_OPENAI_DEPLOYMENT",
            "api_version_env": "AZURE_OPENAI_API_VERSION",
        }
    )
    azure_ai_foundry: dict[str, Any] = Field(
        default_factory=lambda: {
            "endpoint_env": "AZURE_AI_FOUNDRY_ENDPOINT",
            "api_key_env": "AZURE_AI_FOUNDRY_API_KEY",
            "deployment_env": "AZURE_AI_FOUNDRY_DEPLOYMENT",
        }
    )
    openai_compatible: dict[str, Any] = Field(
        default_factory=lambda: {
            "base_url_env": "OPENAI_COMPATIBLE_BASE_URL",
            "api_key_env": "OPENAI_COMPATIBLE_API_KEY",
            "model_env": "OPENAI_COMPATIBLE_MODEL",
        }
    )
    costs_usd_per_1k_tokens: dict[str, dict[str, float]] = Field(default_factory=dict)
    chunking: dict[str, Any] = Field(
        default_factory=lambda: {
            "enabled": True,
            "max_input_tokens_per_chunk": 8000,
            "overlap_tokens": 200,
            "max_chunks_per_document": 20,
        }
    )

    def route_for(self, task: str) -> TaskModelConfig:
        route = self.task_models.get(task)
        if route is None:
            return TaskModelConfig(
                provider=self.provider,
                model=self.model,
                prompt_version=self.prompt_version,
            )
        return TaskModelConfig(
            provider=route.provider or self.provider,
            model=route.model or self.model,
            prompt_version=route.prompt_version or self.prompt_version,
        )


class ProcessingConfig(BaseModel):
    page_workers: int = 8
    llm_workers: int = 4
    writer_workers: int = 1
    use_threads_for_blocking_io: bool = False
    fail_fast: bool = False


class CLIConfig(BaseModel):
    color: Literal["auto", "always", "never"] = "auto"
    progress: Literal["auto", "always", "never"] = "auto"
    default_output: Literal["human", "json"] = "human"
    summary: bool = True
    log_level: str = "info"
    show_tracebacks: bool = False
    quiet: bool = False


class ScoringConfig(BaseModel):
    freshness_days: dict[str, int] = Field(
        default_factory=lambda: {"excellent": 90, "good": 180, "acceptable": 365, "stale": 730}
    )
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "freshness": 0.20,
            "authority": 0.20,
            "completeness": 0.20,
            "operational_value": 0.25,
            "ownership_clarity": 0.10,
            "contradiction_penalty": 0.05,
        }
    )
    document_type_weights: dict[str, int] = Field(
        default_factory=lambda: {
            "approved_runbook": 95,
            "runbook": 90,
            "knowledge_article": 85,
            "rca": 85,
            "architecture": 85,
            "known_error": 80,
            "support_model": 80,
            "incident": 75,
            "change_record": 70,
            "reference": 50,
            "project_plan": 40,
            "meeting_notes": 30,
            "archive": 20,
            "unknown": 10,
            "design": 75,
            "migration": 65,
            "onboarding": 65,
        }
    )
    rca: dict[str, Any] = Field(default_factory=lambda: {"treat_old_documents_as_historical": True})
    claim_type_authority: dict[str, Any] = Field(default_factory=dict)


class OnyxPocConfig(BaseModel):
    emit_enriched_markdown: bool = True
    include_source_content: bool = True
    max_source_content_chars: int = 200000
    slug_max_chars: int = 90
    chunked_output: bool = False
    metadata_field: str = "ONYX_METADATA"
    metadata_line_format: Literal["hash_prefix", "html_comment"] = "hash_prefix"
    metadata_policy: Literal["lean_filters", "extended_debug"] = "lean_filters"
    dedupe_visual_content: bool = True
    max_visual_images: int = 20
    max_visual_ocr_chars: int = 2000


class VisualExtractionConfig(BaseModel):
    enabled: bool = False
    provider: str = "azure-ai-foundry"
    model: str = "gpt-5.4-mini"
    prompt_version: str = "visual-ocr-v1"
    max_images_per_page: int = 20
    skip_low_value_images: bool = True
    min_image_pixels: int = 4096
    adaptive_page_caps: bool = True
    report_page_max_images: int = 12
    representative_group_sampling: bool = True
    max_images_per_representative_group: int = 3


class RedactionConfig(BaseModel):
    enabled: bool = True
    action: Literal["redact", "fail", "off"] = "redact"
    fail_on_high_confidence_secret: bool = True
    replacement: str = "[REDACTED]"
    patterns: list[str] = Field(
        default_factory=lambda: [
            "aws_access_key",
            "github_token",
            "slack_token",
            "openai_key",
            "azure_openai_key_assignment",
            "generic_api_key_assignment",
            "bearer_token",
            "connection_string",
            "private_key_block",
            "jwt",
            "password_assignment",
        ]
    )


class ArtifactConfig(BaseModel):
    schema_version: str = SCHEMA_VERSION
    atomic_writes: bool = True
    stable_sort_order: list[str] = Field(default_factory=lambda: ["space_key", "page_id"])


class AppConfig(BaseModel):
    project_name: str = "mimir-wiki"
    paths: PathConfig = Field(default_factory=PathConfig)
    features: FeatureConfig = Field(default_factory=FeatureConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)
    cli: CLIConfig = Field(default_factory=CLIConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    onyx_poc: OnyxPocConfig = Field(default_factory=OnyxPocConfig)
    visual_extraction: VisualExtractionConfig = Field(default_factory=VisualExtractionConfig)
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)
    artifacts: ArtifactConfig = Field(default_factory=ArtifactConfig)

    def non_secret_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], redact_secrets(self.model_dump(mode="json")))

    def enrichment_config_hash(self) -> str:
        subset = {
            "features": self.features.model_dump(mode="json"),
            "llm": {
                "provider": self.llm.provider,
                "model": self.llm.model,
                "prompt_version": self.llm.prompt_version,
                "task_models": {
                    key: value.model_dump(mode="json")
                    for key, value in sorted(self.llm.task_models.items())
                },
                "task_bundles": {
                    key: value.model_dump(mode="json")
                    for key, value in sorted(self.llm.task_bundles.items())
                },
            },
            "scoring": self.scoring.model_dump(mode="json"),
            "onyx_poc": self.onyx_poc.model_dump(mode="json"),
            "redaction": self.redaction.model_dump(mode="json"),
        }
        return stable_hash(subset)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        parsed = yaml.safe_load(handle) or {}
    if not isinstance(parsed, dict):
        raise ValueError(f"Config file must contain a mapping: {path}")
    return cast(dict[str, Any], parsed)


def _env_overrides(env: Mapping[str, str | None]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    provider = env.get("MIMIR_WIKI_PROVIDER")
    if provider:
        overrides = deep_merge(overrides, {"llm": {"provider": provider}})
    llm_enabled = env.get("MIMIR_WIKI_LLM_ENABLED")
    if llm_enabled is not None and llm_enabled.strip():
        enabled = llm_enabled.strip().lower() in {"1", "true", "yes", "on"}
        overrides = deep_merge(overrides, {"features": {"llm": {"enabled": enabled}}})
    for env_name, path_key in (
        ("MIMIR_WIKI_KNOWLEDGE_PATH", "knowledge"),
        ("MIMIR_WIKI_REPORTS_PATH", "reports"),
        ("MIMIR_WIKI_RUNS_PATH", "runs"),
        ("MIMIR_WIKI_ONYX_ENRICHED_PATH", "dist_onyx_enriched"),
    ):
        value = env.get(env_name)
        if value:
            overrides = deep_merge(overrides, {"paths": {path_key: value}})
    return overrides


def _normalize_llm_tasks(config: AppConfig, explicit_tasks: list[str] | None) -> AppConfig:
    if explicit_tasks:
        validate_llm_task_names(explicit_tasks, context="--llm-task")
        tasks = {task: task in explicit_tasks for task in LLM_TASKS}
        data = config.model_dump(mode="python")
        data["features"]["llm"]["tasks"] = tasks
        return AppConfig.model_validate(data)
    return config


def validate_llm_task_names(tasks: list[str] | set[str], *, context: str) -> None:
    unknown = sorted(set(tasks) - LLM_TASKS)
    if unknown:
        allowed = ", ".join(sorted(LLM_TASKS))
        raise ValueError(
            f"Unknown LLM task(s) in {context}: {', '.join(unknown)}. Allowed tasks: {allowed}."
        )


def validate_llm_task_config(config: AppConfig) -> None:
    validate_llm_task_names(set(config.features.llm.tasks), context="features.llm.tasks")
    validate_llm_task_names(set(config.llm.task_models), context="llm.task_models")
    for bundle_name, bundle in sorted(config.llm.task_bundles.items()):
        validate_llm_task_names(bundle.tasks, context=f"llm.task_bundles.{bundle_name}.tasks")


def load_config(
    *,
    config_path: Path | None = None,
    profile: str | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    env_file: Path | None = None,
) -> AppConfig:
    data: dict[str, Any] = AppConfig().model_dump(mode="python")

    if config_path is None:
        default_path = Path("mimir-wiki.yaml")
        config_path = default_path if default_path.exists() else None
    file_data = _load_yaml(config_path) if config_path else {}
    profiles = file_data.pop("profiles", {}) if isinstance(file_data.get("profiles"), dict) else {}
    data = deep_merge(data, file_data)
    if profile:
        selected = profiles.get(profile)
        if not isinstance(selected, dict):
            raise ValueError(f"Config profile not found: {profile}")
        data = deep_merge(data, selected)

    dotenv_path = env_file or Path(".env")
    dotenv_data = dotenv_values(dotenv_path) if dotenv_path.exists() else {}
    if dotenv_path.exists():
        # Make provider credentials available to provider factories without
        # committing secret values to YAML. Real environment variables still win.
        load_dotenv(dotenv_path, override=False)
    data = deep_merge(data, _env_overrides(dotenv_data))
    data = deep_merge(data, _env_overrides(os.environ))
    if cli_overrides:
        data = deep_merge(data, cli_overrides)

    config = AppConfig.model_validate(data)
    if config.llm.provider == "none":
        config_data = config.model_dump(mode="python")
        config_data["features"]["llm"]["enabled"] = False
        config = AppConfig.model_validate(config_data)
    validate_llm_task_config(config)
    return config


def apply_runtime_overrides(
    *,
    provider: str | None = None,
    enable_llm: bool | None = None,
    llm_tasks: list[str] | None = None,
    emit_onyx_markdown: bool | None = None,
    include_source_content: bool | None = None,
    redaction: str | None = None,
    cache: Path | None = None,
    out: Path | None = None,
    onyx_out: Path | None = None,
    reports_out: Path | None = None,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if provider is not None:
        overrides = deep_merge(overrides, {"llm": {"provider": provider}})
    if enable_llm is not None:
        overrides = deep_merge(overrides, {"features": {"llm": {"enabled": enable_llm}}})
    if emit_onyx_markdown is not None:
        overrides = deep_merge(
            overrides,
            {
                "features": {"outputs": {"onyx_poc_markdown": emit_onyx_markdown}},
                "onyx_poc": {"emit_enriched_markdown": emit_onyx_markdown},
            },
        )
    if include_source_content is not None:
        overrides = deep_merge(
            overrides, {"onyx_poc": {"include_source_content": include_source_content}}
        )
    if redaction is not None:
        if redaction == "off":
            overrides = deep_merge(overrides, {"redaction": {"enabled": False, "action": "off"}})
        else:
            overrides = deep_merge(overrides, {"redaction": {"enabled": True, "action": redaction}})
    if cache is not None:
        overrides = deep_merge(overrides, {"paths": {"cache": str(cache)}})
    if out is not None:
        overrides = deep_merge(overrides, {"paths": {"knowledge": str(out)}})
    if onyx_out is not None:
        overrides = deep_merge(overrides, {"paths": {"dist_onyx_enriched": str(onyx_out)}})
    if reports_out is not None:
        overrides = deep_merge(overrides, {"paths": {"reports": str(reports_out)}})
    if llm_tasks:
        validate_llm_task_names(llm_tasks, context="--llm-task")
        overrides = deep_merge(
            overrides,
            {"features": {"llm": {"tasks": {task: task in llm_tasks for task in LLM_TASKS}}}},
        )
    return overrides
