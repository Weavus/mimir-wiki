from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mimir_wiki.cache_reader import PageBundle
from mimir_wiki.config import AppConfig
from mimir_wiki.constants import DOCUMENT_TYPES
from mimir_wiki.enrichers.deterministic import (
    categories_for,
    document_type_for_subtype,
    entity_bucket,
    filter_taxonomy_terms,
    has_linked_procedure,
    infer_document_subtype,
    is_procedural_runbook,
    warnings_for,
)
from mimir_wiki.hierarchy import adjust_quality_for_hierarchy
from mimir_wiki.llm.base import (
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    RateLimitedLLMClient,
)
from mimir_wiki.schemas import (
    CandidateEntity,
    CandidateMention,
    Enrichment,
    KeyFact,
    LLMUsage,
    OperationalSignals,
    PageFailure,
    WarningRecord,
)
from mimir_wiki.scoring import build_quality, currentness, quality_band
from mimir_wiki.utils import atomic_write_json, load_json, normalize_term, stable_hash, word_count

DETERMINISTIC_WARNING_TYPES = {
    "source_is_archived_or_deprecated",
    "low_quality_score",
    "missing_explicit_owner",
    "missing_validation_steps",
    "missing_backout_steps",
    "linked_procedure_not_expanded",
    "attachments_present_not_parsed",
}


class LLMTaskModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClassificationResponse(LLMTaskModel):
    document_type: str
    confidence: float = Field(ge=0, le=1)

    @field_validator("document_type")
    @classmethod
    def document_type_supported(cls, value: str) -> str:
        value = normalize_document_type(value)
        if value not in DOCUMENT_TYPES:
            raise ValueError(f"unsupported document_type: {value}")
        return value


DOCUMENT_TYPE_ALIASES = {
    "adr": "design",
    "architecture decision record": "design",
    "architecture_decision_record": "design",
    "faq": "knowledge_article",
    "frequently asked questions": "knowledge_article",
    "howto": "knowledge_article",
    "how to": "knowledge_article",
    "how-to": "knowledge_article",
    "installation guide": "runbook",
    "installation_guide": "runbook",
    "performance test": "reference",
    "performance_test": "reference",
    "procedure page": "runbook",
    "procedure_page": "runbook",
    "report": "reference",
    "release note": "change_record",
    "release notes": "change_record",
    "release-notes": "change_record",
    "release_note": "change_record",
    "release_notes": "change_record",
    "release": "change_record",
    "requirements": "design",
    "requirement document": "design",
    "readme": "knowledge_article",
    "runbook detail": "runbook",
    "runbook_detail": "runbook",
    "runbook_index": "runbook",
    "service review": "meeting_notes",
    "service_review": "meeting_notes",
    "template": "reference",
    "test report": "reference",
    "test_report": "reference",
}


def normalize_document_type(value: str) -> str:
    normalized = normalize_term(value).replace(" ", "_")
    alias_key = normalize_term(value)
    if value in DOCUMENT_TYPE_ALIASES:
        return DOCUMENT_TYPE_ALIASES[value]
    if normalized in DOCUMENT_TYPE_ALIASES:
        return DOCUMENT_TYPE_ALIASES[normalized]
    if alias_key in DOCUMENT_TYPE_ALIASES:
        return DOCUMENT_TYPE_ALIASES[alias_key]
    return normalized


class SummaryResponse(LLMTaskModel):
    short_summary: str = Field(min_length=1, max_length=1000)
    detailed_summary: str = Field(min_length=1, max_length=6000)


class KeywordsResponse(LLMTaskModel):
    keywords: list[str] = Field(default_factory=list, max_length=50)


class ThemesResponse(LLMTaskModel):
    themes: list[str] = Field(default_factory=list, max_length=50)


class ConceptsResponse(LLMTaskModel):
    concepts: list[str] = Field(default_factory=list, max_length=80)


class LLMCandidateEntity(LLMTaskModel):
    name: str = Field(min_length=1, max_length=120)
    entity_type: str = Field(min_length=1, max_length=80)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1, default=0.65)
    evidence: str = Field(min_length=1, max_length=8000)


class CandidateEntitiesResponse(LLMTaskModel):
    candidate_entities: list[LLMCandidateEntity] = Field(default_factory=list, max_length=100)


class OperationalSignalsResponse(LLMTaskModel):
    operational_signals: OperationalSignals = Field(default_factory=OperationalSignals)


class QualityWarningsResponse(LLMTaskModel):
    warnings: list[str] = Field(default_factory=list, max_length=100)


class LLMKeyFact(LLMTaskModel):
    label: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=300)
    confidence: float = Field(ge=0, le=1, default=0.75)
    evidence: str | None = Field(default=None, max_length=8000)


class BundleResponse(LLMTaskModel):
    document_type: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    short_summary: str | None = Field(default=None, max_length=1000)
    detailed_summary: str | None = Field(default=None, max_length=6000)
    keywords: list[str] | None = Field(default=None, max_length=50)
    themes: list[str] | None = Field(default=None, max_length=50)
    concepts: list[str] | None = Field(default=None, max_length=80)
    candidate_entities: list[LLMCandidateEntity] | None = Field(default=None, max_length=100)
    operational_signals: OperationalSignals | None = None
    warnings: list[str] | None = Field(default=None, max_length=100)
    key_facts: list[LLMKeyFact] | None = Field(default=None, max_length=200)

    @field_validator("document_type")
    @classmethod
    def optional_document_type_supported(cls, value: str | None) -> str | None:
        if value is not None:
            value = normalize_document_type(value)
        if value is not None and value not in DOCUMENT_TYPES:
            raise ValueError(f"unsupported document_type: {value}")
        return value


TASK_RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "classification": ClassificationResponse,
    "summary": SummaryResponse,
    "keywords": KeywordsResponse,
    "themes": ThemesResponse,
    "concepts": ConceptsResponse,
    "candidate_entities": CandidateEntitiesResponse,
    "operational_signals": OperationalSignalsResponse,
    "quality_warnings": QualityWarningsResponse,
}

FIELD_STRING_LIMITS = {
    "short_summary": 1000,
    "detailed_summary": 6000,
    "name": 120,
    "entity_type": 80,
    "evidence": 8000,
    "label": 80,
    "value": 300,
}
FIELD_LIST_LIMITS = {
    "keywords": 50,
    "themes": 50,
    "concepts": 80,
    "candidate_entities": 100,
    "warnings": 100,
    "key_facts": 200,
    "aliases": 20,
}


@dataclass(frozen=True)
class LLMWorkItem:
    name: str
    prompt_task: str
    tasks: list[str]
    provider: str
    model: str
    prompt_version: str


PROMPT_CONTRACTS = {
    "classification": '{"document_type":"runbook","confidence":0.0}',
    "summary": '{"short_summary":"...","detailed_summary":"..."}',
    "keywords": '{"keywords":["term"]}',
    "themes": '{"themes":["theme"]}',
    "concepts": '{"concepts":["concept"]}',
    "candidate_entities": (
        '{"candidate_entities":[{"name":"ForgeRock","entity_type":"application",'
        '"aliases":[],"confidence":0.8,"evidence":"text"}]}'
    ),
    "operational_signals": '{"operational_signals":{"has_owner":true}}',
    "quality_warnings": '{"warnings":["missing explicit owner"]}',
    "key_facts": (
        '{"key_facts":[{"label":"Primary service","value":"ForgeRock",'
        '"confidence":0.8,"evidence":"..."}]}'
    ),
}


@dataclass
class LLMEnrichmentResult:
    enrichment: Enrichment
    usage: list[LLMUsage] = field(default_factory=list)
    failures: list[PageFailure] = field(default_factory=list)
    warnings: list[WarningRecord] = field(default_factory=list)
    retries: int = 0


def enabled_llm_tasks(config: AppConfig) -> list[str]:
    if not config.features.llm.enabled or config.llm.provider == "none":
        return []
    return sorted(task for task, enabled in config.features.llm.tasks.items() if enabled)


def enabled_llm_work_items(config: AppConfig) -> list[LLMWorkItem]:
    tasks = enabled_llm_tasks(config)
    if not tasks:
        return []
    bundled_tasks: set[str] = set()
    work_items: list[LLMWorkItem] = []
    for bundle_name, bundle in sorted(config.llm.task_bundles.items()):
        bundle_tasks = [task for task in bundle.tasks if task in tasks]
        if not bundle_tasks:
            continue
        bundled_tasks.update(bundle_tasks)
        work_items.append(
            LLMWorkItem(
                name=f"bundle:{bundle_name}",
                prompt_task=bundle_name,
                tasks=bundle_tasks,
                provider=bundle.provider or config.llm.provider,
                model=bundle.model or config.llm.model,
                prompt_version=bundle.prompt_version or f"{bundle_name}-v1",
            )
        )
    for task in tasks:
        if task in bundled_tasks:
            continue
        route = config.llm.route_for(task)
        work_items.append(
            LLMWorkItem(
                name=task,
                prompt_task=task,
                tasks=[task],
                provider=route.provider or config.llm.provider,
                model=route.model or config.llm.model,
                prompt_version=route.prompt_version or config.llm.prompt_version,
            )
        )
    return work_items


def apply_llm_enrichment(
    *,
    bundle: PageBundle,
    enrichment: Enrichment,
    config: AppConfig,
    run_id: str,
    dataset_name: str,
    generated_at: str,
    provider: LLMProvider,
    event_callback: Callable[[dict[str, Any]], None] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> LLMEnrichmentResult:
    work_items = enabled_llm_work_items(config)
    if not work_items:
        return LLMEnrichmentResult(enrichment=enrichment)
    return asyncio.run(
        _apply_llm_enrichment_async(
            bundle=bundle,
            enrichment=enrichment,
            config=config,
            run_id=run_id,
            dataset_name=dataset_name,
            generated_at=generated_at,
            provider=provider,
            client=None,
            work_items=work_items,
            event_callback=event_callback,
            progress_callback=progress_callback,
        )
    )


async def apply_llm_enrichment_async(
    *,
    bundle: PageBundle,
    enrichment: Enrichment,
    config: AppConfig,
    run_id: str,
    dataset_name: str,
    generated_at: str,
    provider: LLMProvider,
    client: RateLimitedLLMClient | None = None,
    event_callback: Callable[[dict[str, Any]], None] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> LLMEnrichmentResult:
    work_items = enabled_llm_work_items(config)
    if not work_items:
        return LLMEnrichmentResult(enrichment=enrichment)
    return await _apply_llm_enrichment_async(
        bundle=bundle,
        enrichment=enrichment,
        config=config,
        run_id=run_id,
        dataset_name=dataset_name,
        generated_at=generated_at,
        provider=provider,
        client=client,
        work_items=work_items,
        event_callback=event_callback,
        progress_callback=progress_callback,
    )


async def _apply_llm_enrichment_async(
    *,
    bundle: PageBundle,
    enrichment: Enrichment,
    config: AppConfig,
    run_id: str,
    dataset_name: str,
    generated_at: str,
    provider: LLMProvider,
    client: RateLimitedLLMClient | None,
    work_items: list[LLMWorkItem],
    event_callback: Callable[[dict[str, Any]], None] | None,
    progress_callback: Callable[[dict[str, Any]], None] | None,
) -> LLMEnrichmentResult:
    def llm_client_event(event: dict[str, Any]) -> None:
        if event_callback is not None:
            event_callback(event)
        if progress_callback is not None:
            progress_callback(event)

    client = client or RateLimitedLLMClient(provider, config.llm, retry_callback=llm_client_event)
    result = LLMEnrichmentResult(enrichment=enrichment)
    chunks, truncated = chunk_text(bundle.text, config)
    if progress_callback:
        progress_callback(
            {
                "event": "llm_plan",
                "page_id": bundle.metadata.page_id,
                "space_key": bundle.metadata.space_key,
                "calls_planned": len(work_items) * len(chunks),
                "chunk_count": len(chunks),
            }
        )
    if truncated:
        result.warnings.append(
            _warning(
                run_id,
                dataset_name,
                generated_at,
                bundle,
                "llm_input_truncated_to_max_chunks",
                "LLM input exceeded max_chunks_per_document and was truncated.",
            )
        )
    for work_item in work_items:
        task_payloads: list[dict[str, Any]] = []
        for chunk_index, chunk in enumerate(chunks, start=1):
            prompt = build_prompt(
                task=work_item.prompt_task,
                prompt_version=work_item.prompt_version,
                requested_tasks=work_item.tasks,
                bundle=bundle,
                enrichment=result.enrichment,
                chunk=chunk,
                chunk_index=chunk_index,
                chunk_count=len(chunks),
            )
            request = LLMRequest(
                task=work_item.name,
                prompt=prompt,
                document_id=bundle.document_id,
                model=work_item.model,
                prompt_version=work_item.prompt_version,
            )
            started = time.monotonic()
            if progress_callback:
                progress_callback(
                    {
                        "event": "llm_call_started",
                        "page_id": bundle.metadata.page_id,
                        "space_key": bundle.metadata.space_key,
                        "task": work_item.name,
                        "chunk_index": chunk_index,
                        "chunk_count": len(chunks),
                    }
                )
            try:
                response, attempts, retries = await complete_with_cache(
                    client=client,
                    request=request,
                    config=config,
                    route_provider=work_item.provider,
                    source_content_hash=bundle.source_content_hash,
                )
                result.retries += retries
                usage = LLMUsage(
                    run_id=run_id,
                    dataset_name=dataset_name,
                    document_id=bundle.document_id,
                    page_id=bundle.metadata.page_id,
                    space_key=bundle.metadata.space_key,
                    source_updated_at=bundle.metadata.updated_at,
                    source_content_hash=bundle.source_content_hash,
                    task=work_item.name,
                    provider=work_item.provider,
                    model=response.model,
                    prompt_version=work_item.prompt_version,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    cached=response.cached,
                    attempts=attempts,
                    retries=retries,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                    estimated_cost_usd=estimate_llm_cost(
                        provider=work_item.provider,
                        model=response.model,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        config=config,
                    ),
                    generated_at=generated_at,
                )
                result.usage.append(usage)
                parsed, parse_warnings = parse_json_response_with_warnings(response.text)
                payload, validation_warnings = validate_work_item_payload_with_warnings(
                    work_item, parsed
                )
                for warning_type in parse_warnings + validation_warnings:
                    result.warnings.append(
                        _warning(
                            run_id,
                            dataset_name,
                            generated_at,
                            bundle,
                            f"llm_response_{warning_type}:{work_item.name}",
                            f"LLM response {warning_type} for {work_item.name}.",
                        )
                    )
                task_payloads.append(payload)
                if progress_callback:
                    progress_callback(
                        {
                            "event": "llm_call_finished",
                            "page_id": bundle.metadata.page_id,
                            "space_key": bundle.metadata.space_key,
                            "task": work_item.name,
                            "chunk_index": chunk_index,
                            "chunk_count": len(chunks),
                            "cached": response.cached,
                            "retries": retries,
                            "input_tokens": response.input_tokens,
                            "output_tokens": response.output_tokens,
                        }
                    )
            except (LLMError, ValueError, ValidationError) as exc:
                failure = _failure(
                    run_id=run_id,
                    dataset_name=dataset_name,
                    generated_at=generated_at,
                    bundle=bundle,
                    stage=f"llm.{work_item.name}",
                    exc=exc,
                    attempts=getattr(exc, "attempts", 1),
                )
                result.enrichment.llm_failures.append(failure.model_dump(mode="json"))
                result.warnings.append(
                    _warning(
                        run_id,
                        dataset_name,
                        generated_at,
                        bundle,
                        f"llm_task_failed:{work_item.name}",
                        f"{failure.error_type}: {failure.message}",
                    )
                )
                if progress_callback:
                    progress_callback(
                        {
                            "event": "llm_call_failed",
                            "page_id": bundle.metadata.page_id,
                            "space_key": bundle.metadata.space_key,
                            "task": work_item.name,
                            "chunk_index": chunk_index,
                            "chunk_count": len(chunks),
                            "error_type": failure.error_type,
                        }
                    )
                if config.llm.fail_fast or config.processing.fail_fast:
                    result.failures.append(failure)
                    return result
        if task_payloads:
            merge_task_payload(result.enrichment, "key_facts", task_payloads, bundle)
            for task in work_item.tasks:
                merge_task_payload(result.enrichment, task, task_payloads, bundle)
    finalize_llm_enrichment(result.enrichment, bundle, config)
    result.enrichment.chunk_count = max(result.enrichment.chunk_count, len(chunks))
    return result


def finalize_llm_enrichment(enrichment: Enrichment, bundle: PageBundle, config: AppConfig) -> None:
    enrichment.document_subtype = infer_document_subtype(bundle, enrichment.document_type)
    enrichment.document_type = document_type_for_subtype(
        enrichment.document_type, enrichment.document_subtype
    )
    historical, current = currentness(
        enrichment.document_type, enrichment.status_flags, bundle.metadata.updated_at
    )
    enrichment.historical = historical
    enrichment.currentness = current
    enrichment.ONYX_METADATA.document_type = enrichment.document_type
    enrichment.ONYX_METADATA.document_subtype = enrichment.document_subtype
    enrichment.quality = build_quality(
        document_type=enrichment.document_type,
        updated_at=bundle.metadata.updated_at,
        status_flags=enrichment.status_flags,
        word_count=word_count(bundle.text),
        heading_count=len(enrichment.headings),
        signals=enrichment.operational_signals,
        outbound_link_count=len(bundle.links.links),
        config=config.scoring,
    )
    enrichment.quality = adjust_quality_for_hierarchy(enrichment.quality, enrichment.hierarchy)
    enrichment.quality_band = quality_band(enrichment.quality.overall_score)
    enrichment.ONYX_METADATA.quality_band = enrichment.quality_band
    enrichment.ONYX_METADATA.historical = historical
    enrichment.ONYX_METADATA.currentness = current
    enrichment.entities = entity_bucket(enrichment.candidate_entities)
    enrichment.keywords = filter_taxonomy_terms(enrichment.keywords, max_terms=30)
    enrichment.themes = filter_taxonomy_terms(enrichment.themes, max_terms=30)
    enrichment.concepts = filter_taxonomy_terms(enrichment.concepts, max_terms=40)
    enrichment.categories = categories_for(bundle, enrichment.document_type, enrichment.keywords)
    custom_warnings = [
        warning for warning in enrichment.warnings if warning not in DETERMINISTIC_WARNING_TYPES
    ]
    recalculated = warnings_for(
        document_type=enrichment.document_type,
        quality_score=enrichment.quality.overall_score,
        signals=enrichment.operational_signals,
        status_flags=enrichment.status_flags,
        attachment_count=len(bundle.attachment_names),
        has_linked_procedure=has_linked_procedure(bundle),
        is_procedural=is_procedural_runbook(bundle),
    )
    enrichment.warnings = _merge_strings(custom_warnings, recalculated, 60)
    enrichment.confidence = min(
        0.95, (enrichment.document_type_confidence + enrichment.quality.overall_score / 100) / 2
    )


def chunk_text(text: str, config: AppConfig) -> tuple[list[str], bool]:
    chunking = config.llm.chunking
    if not chunking.get("enabled", True):
        return [text], False
    max_tokens = int(chunking.get("max_input_tokens_per_chunk") or 8000)
    max_chars = max(1000, max_tokens * 4)
    overlap_tokens = int(chunking.get("overlap_tokens") or 0)
    overlap_chars = max(0, overlap_tokens * 4)
    max_chunks = int(chunking.get("max_chunks_per_document") or 20)
    if len(text) <= max_chars:
        return [text], False
    chunks: list[str] = []
    start = 0
    while start < len(text) and len(chunks) < max_chunks:
        end = min(len(text), start + max_chars)
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = max(end - overlap_chars, start + 1)
    return chunks, chunks[-1][-1:] != text[-1:]


def build_prompt(
    *,
    task: str,
    prompt_version: str,
    requested_tasks: list[str],
    bundle: PageBundle,
    enrichment: Enrichment,
    chunk: str,
    chunk_index: int,
    chunk_count: int,
) -> str:
    base = {
        "document_id": enrichment.document_id,
        "page_id": enrichment.page_id,
        "space_key": enrichment.space_key,
        "title": bundle.metadata.title,
        "labels": bundle.metadata.labels,
        "deterministic_document_type": enrichment.document_type,
        "deterministic_keywords": enrichment.keywords,
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "requested_tasks": requested_tasks,
        "ancestor_titles": bundle.ancestor_titles,
        "hierarchy": enrichment.hierarchy.model_dump(mode="json"),
    }
    template = load_prompt_template(task, prompt_version)
    return template.format(
        task=task,
        contract=prompt_contract_for_tasks(requested_tasks),
        metadata_json=json.dumps(base, sort_keys=True),
        chunk=chunk,
    )


def prompt_contract_for_tasks(tasks: list[str]) -> str:
    if len(tasks) == 1:
        return PROMPT_CONTRACTS.get(tasks[0], "{}")
    parts = []
    for task in tasks:
        contract = PROMPT_CONTRACTS.get(task)
        if contract:
            parts.append(contract.strip("{}"))
    parts.append(PROMPT_CONTRACTS["key_facts"].strip("{}"))
    return "{" + ",".join(part for part in parts if part) + "}"


def load_prompt_template(task: str, prompt_version: str) -> str:
    prompts_dir = Path(__file__).parent / "prompts"
    candidates = [
        prompts_dir / f"{task}-{prompt_version}.md",
        prompts_dir / f"{prompt_version}.md",
        prompts_dir / f"{task}-v1.md",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    return (
        "Task: {task}\n"
        "Return JSON matching this shape: {contract}\n"
        "Use only supplied text. If evidence is absent, omit the value or return an empty list.\n"
        "Metadata JSON: {metadata_json}\n\n"
        "Document text chunk:\n{chunk}"
    )


async def complete_with_cache(
    *,
    client: RateLimitedLLMClient,
    request: LLMRequest,
    config: AppConfig,
    route_provider: str,
    source_content_hash: str,
) -> tuple[LLMResponse, int, int]:
    cache_dir = Path(config.paths.llm_cache)
    key = stable_hash(
        {
            "source_content_hash": source_content_hash,
            "task": request.task,
            "prompt": request.prompt,
            "prompt_version": request.prompt_version,
            "provider": route_provider,
            "model": request.model,
            "config_hash": config.enrichment_config_hash(),
        }
    )
    cache_path = cache_dir / f"{key}.json"
    if cache_path.exists():
        cached = load_json(cache_path)
        return (
            LLMResponse(
                text=str(cached["text"]),
                model=str(cached["model"]),
                input_tokens=cached.get("input_tokens"),
                output_tokens=cached.get("output_tokens"),
                cached=True,
            ),
            0,
            0,
        )
    response, attempts, retries = await client.complete(request)
    atomic_write_json(
        cache_path,
        {
            "text": response.text,
            "model": response.model,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
        },
    )
    return response, attempts, retries


def parse_json_response(text: str) -> dict[str, Any]:
    parsed, _ = parse_json_response_with_warnings(text)
    return parsed


def parse_json_response_with_warnings(text: str) -> tuple[dict[str, Any], list[str]]:
    stripped = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, flags=re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    try:
        parsed = json.loads(stripped)
        warnings: list[str] = []
    except json.JSONDecodeError:
        repaired = repair_json_text(stripped)
        if repaired == stripped:
            raise
        parsed = json.loads(repaired)
        warnings = ["repaired_json"]
    if not isinstance(parsed, dict):
        raise ValueError("LLM response must be a JSON object")
    return parsed, warnings


def repair_json_text(text: str) -> str:
    repaired = text.strip()
    start = repaired.find("{")
    end = repaired.rfind("}")
    if start >= 0 and end > start:
        repaired = repaired[start : end + 1]
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    return repaired


def trim_payload_for_validation(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    changed = False

    def trim_value(key: str, value: Any) -> Any:
        nonlocal changed
        if isinstance(value, str) and key in FIELD_STRING_LIMITS:
            limit = FIELD_STRING_LIMITS[key]
            if len(value) > limit:
                changed = True
                return value[:limit]
        if isinstance(value, list):
            limit = FIELD_LIST_LIMITS.get(key)
            items = value
            if limit is not None and len(items) > limit:
                changed = True
                items = items[:limit]
            return [trim_value("", item) for item in items]
        if isinstance(value, dict):
            return {
                nested_key: trim_value(nested_key, nested_value)
                for nested_key, nested_value in value.items()
            }
        return value

    trimmed = {key: trim_value(key, value) for key, value in payload.items()}
    return trimmed, changed


def validate_task_payload(task: str, payload: dict[str, Any]) -> dict[str, Any]:
    validated, _ = validate_task_payload_with_warnings(task, payload)
    return validated


def validate_task_payload_with_warnings(
    task: str, payload: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    payload, trimmed = trim_payload_for_validation(payload)
    model = TASK_RESPONSE_MODELS.get(task)
    if model is None:
        return payload, ["trimmed_fields"] if trimmed else []
    return (
        model.model_validate(payload).model_dump(mode="python", exclude_none=True),
        ["trimmed_fields"] if trimmed else [],
    )


def validate_work_item_payload(work_item: LLMWorkItem, payload: dict[str, Any]) -> dict[str, Any]:
    validated, _ = validate_work_item_payload_with_warnings(work_item, payload)
    return validated


def validate_work_item_payload_with_warnings(
    work_item: LLMWorkItem, payload: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    payload, trimmed = trim_payload_for_validation(payload)
    warnings = ["trimmed_fields"] if trimmed else []
    if len(work_item.tasks) == 1:
        validated, task_warnings = validate_task_payload_with_warnings(work_item.tasks[0], payload)
        return validated, sorted(set(warnings + task_warnings))
    validated = BundleResponse.model_validate(payload).model_dump(mode="python", exclude_none=True)
    for task in work_item.tasks:
        task_payload = {key: validated[key] for key in _task_payload_keys(task) if key in validated}
        if task_payload:
            _, task_warnings = validate_task_payload_with_warnings(task, task_payload)
            warnings.extend(task_warnings)
    return validated, sorted(set(warnings))


def _task_payload_keys(task: str) -> tuple[str, ...]:
    return {
        "classification": ("document_type", "confidence"),
        "summary": ("short_summary", "detailed_summary"),
        "keywords": ("keywords",),
        "themes": ("themes",),
        "concepts": ("concepts",),
        "candidate_entities": ("candidate_entities",),
        "operational_signals": ("operational_signals",),
        "quality_warnings": ("warnings",),
        "key_facts": ("key_facts",),
    }.get(task, tuple())


def estimate_llm_cost(
    *,
    provider: str,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    config: AppConfig,
) -> float | None:
    costs = config.llm.costs_usd_per_1k_tokens
    keys = [f"{provider}:{model}", model, provider]
    selected: dict[str, float] | None = None
    for key in keys:
        value = costs.get(key)
        if value is not None:
            selected = value
            break
    if selected is None:
        return None
    input_rate = selected.get("input")
    output_rate = selected.get("output")
    if input_rate is None or output_rate is None:
        return None
    return round(
        ((input_tokens or 0) / 1000 * input_rate) + ((output_tokens or 0) / 1000 * output_rate), 8
    )


def merge_task_payload(
    enrichment: Enrichment, task: str, payloads: list[dict[str, Any]], bundle: PageBundle
) -> None:
    if task == "classification":
        for payload in payloads:
            document_type = payload.get("document_type")
            confidence = payload.get("confidence")
            if (
                document_type in DOCUMENT_TYPES
                and isinstance(confidence, int | float)
                and float(confidence) >= enrichment.document_type_confidence
            ):
                enrichment.document_type = document_type
                enrichment.document_type_confidence = max(0, min(1, float(confidence)))
                enrichment.ONYX_METADATA.document_type = document_type
        return
    if task == "summary":
        for payload in payloads:
            if isinstance(payload.get("short_summary"), str):
                enrichment.short_summary = payload["short_summary"][:1000]
            if isinstance(payload.get("detailed_summary"), str):
                enrichment.detailed_summary = payload["detailed_summary"][:6000]
        return
    if task == "keywords":
        enrichment.keywords = _merge_strings(
            enrichment.keywords, _collect_strings(payloads, "keywords"), 30
        )
        return
    if task == "themes":
        enrichment.themes = _merge_strings(
            enrichment.themes, _collect_strings(payloads, "themes"), 30
        )
        return
    if task == "concepts":
        enrichment.concepts = _merge_strings(
            enrichment.concepts, _collect_strings(payloads, "concepts"), 40
        )
        return
    if task == "quality_warnings":
        enrichment.warnings = _merge_strings(
            enrichment.warnings, _collect_strings(payloads, "warnings"), 60
        )
        return
    if task == "operational_signals":
        merged = enrichment.operational_signals.model_dump()
        for payload in payloads:
            signals = payload.get("operational_signals")
            if isinstance(signals, dict):
                for key, value in signals.items():
                    if key in merged and isinstance(value, bool):
                        merged[key] = bool(merged[key] or value)
        enrichment.operational_signals = OperationalSignals.model_validate(merged)
        return
    if task == "candidate_entities":
        entities = list(enrichment.candidate_entities)
        seen = {(entity.entity_type, entity.normalized_name) for entity in entities}
        for payload in payloads:
            raw_entities = payload.get("candidate_entities")
            if not isinstance(raw_entities, list):
                continue
            for raw in raw_entities:
                if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                    continue
                name = raw["name"].strip()[:120]
                normalized = normalize_term(name)
                entity_type = str(raw.get("entity_type") or "technology")
                key = (entity_type, normalized)
                if not normalized or key in seen:
                    continue
                confidence = raw.get("confidence", 0.65)
                evidence = str(raw.get("evidence") or name)[:240]
                entities.append(
                    CandidateEntity(
                        name=name,
                        normalized_name=normalized,
                        entity_type=entity_type,
                        aliases=[
                            str(alias) for alias in raw.get("aliases", []) if isinstance(alias, str)
                        ],
                        mentions=[
                            CandidateMention(
                                document_id=bundle.document_id,
                                page_id=bundle.metadata.page_id,
                                evidence=evidence,
                                source_field="llm",
                            )
                        ],
                        confidence=max(0, min(1, float(confidence))),
                        method="llm",
                    )
                )
                seen.add(key)
        enrichment.candidate_entities = entities[:80]
        return
    if task == "key_facts":
        facts = list(enrichment.key_facts)
        seen = {(fact.label.lower(), fact.value.lower()) for fact in facts}
        for payload in payloads:
            raw_facts = payload.get("key_facts")
            if not isinstance(raw_facts, list):
                continue
            for raw in raw_facts:
                if not isinstance(raw, dict):
                    continue
                label = str(raw.get("label") or "").strip()[:80]
                value = str(raw.get("value") or "").strip()[:300]
                if not label or not value:
                    continue
                key = (label.lower(), value.lower())
                if key in seen:
                    continue
                seen.add(key)
                confidence = raw.get("confidence", 0.75)
                facts.append(
                    KeyFact(
                        label=label,
                        value=value,
                        confidence=max(0, min(1, float(confidence))),
                        evidence=str(raw.get("evidence"))[:500] if raw.get("evidence") else None,
                        method="llm",
                    )
                )
        enrichment.key_facts = facts[:40]


def _collect_strings(payloads: list[dict[str, Any]], key: str) -> list[str]:
    values: list[str] = []
    for payload in payloads:
        raw_values = payload.get(key)
        if isinstance(raw_values, list):
            for value in raw_values:
                if isinstance(value, str):
                    values.append(value)
                elif isinstance(value, dict) and isinstance(value.get("name"), str):
                    values.append(value["name"])
                elif isinstance(value, dict) and isinstance(value.get(key[:-1]), str):
                    values.append(value[key[:-1]])
    return values


def _merge_strings(existing: list[str], added: list[str], limit: int) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in [*existing, *added]:
        normalized = normalize_term(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(value.strip())
        if len(merged) >= limit:
            break
    return merged


def _failure(
    *,
    run_id: str,
    dataset_name: str,
    generated_at: str,
    bundle: PageBundle,
    stage: str,
    exc: Exception,
    attempts: int,
) -> PageFailure:
    retryable = bool(getattr(exc, "retryable", False))
    error_type = str(getattr(exc, "error_type", type(exc).__name__))
    suggested = "Inspect provider configuration or rerun with --provider none."
    if error_type == "context_length_exceeded":
        suggested = "Reduce LLM input size or adjust llm.chunking settings."
    return PageFailure(
        run_id=run_id,
        dataset_name=dataset_name,
        generated_at=generated_at,
        document_id=bundle.document_id,
        page_id=bundle.metadata.page_id,
        space_key=bundle.metadata.space_key,
        title=bundle.metadata.title,
        source_updated_at=bundle.metadata.updated_at,
        source_content_hash=bundle.source_content_hash,
        stage=stage,
        error_type=error_type,
        message=str(exc),
        retryable=retryable,
        attempts=attempts,
        suggested_action=suggested,
    )


def _warning(
    run_id: str,
    dataset_name: str,
    generated_at: str,
    bundle: PageBundle,
    warning_type: str,
    message: str,
) -> WarningRecord:
    return WarningRecord(
        run_id=run_id,
        dataset_name=dataset_name,
        generated_at=generated_at,
        document_id=bundle.document_id,
        page_id=bundle.metadata.page_id,
        space_key=bundle.metadata.space_key,
        title=bundle.metadata.title,
        warning_type=warning_type,
        message=message,
        stage="llm",
    )
