from __future__ import annotations

import json
from pathlib import Path

from mimir_wiki.cache_reader import CacheReader
from mimir_wiki.config import load_config
from mimir_wiki.enrichers.deterministic import enrich_page
from mimir_wiki.enrichers.llm import (
    apply_llm_enrichment,
    enabled_llm_work_items,
    load_prompt_template,
    validate_task_payload,
)
from mimir_wiki.llm.base import LLMRequest, LLMResponse


class JsonProvider:
    provider_name = "mock"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if request.task == "bundle:semantic":
            text = json.dumps(
                {
                    "short_summary": "Bundled short summary.",
                    "detailed_summary": "Bundled detailed summary.",
                    "key_facts": [
                        {
                            "label": "Primary service",
                            "value": "ForgeRock",
                            "confidence": 0.8,
                            "evidence": "ForgeRock Support Runbook",
                        }
                    ],
                    "themes": ["identity operations"],
                }
            )
        elif request.task == "bundle:operational":
            text = json.dumps(
                {
                    "candidate_entities": [
                        {
                            "name": "Identity SRE",
                            "entity_type": "support_group",
                            "aliases": [],
                            "confidence": 0.8,
                            "evidence": "Support group: Identity L3",
                        }
                    ],
                    "operational_signals": {"has_support_group": True},
                    "warnings": ["missing backout steps"],
                    "key_facts": [
                        {
                            "label": "Support group",
                            "value": "Identity SRE",
                            "confidence": 0.8,
                            "evidence": "Support group: Identity L3",
                        }
                    ],
                }
            )
        elif request.task == "summary":
            text = json.dumps(
                {
                    "short_summary": "LLM short summary.",
                    "detailed_summary": "LLM detailed summary.",
                }
            )
        elif request.task == "themes":
            text = json.dumps({"themes": ["identity operations"]})
        elif request.task == "candidate_entities":
            text = json.dumps(
                {
                    "candidate_entities": [
                        {
                            "name": "Identity SRE",
                            "entity_type": "support_group",
                            "aliases": [],
                            "confidence": 0.8,
                            "evidence": "Support group: Identity L3",
                        }
                    ]
                }
            )
        else:
            text = "{}"
        return LLMResponse(text=text, model="mock-model", input_tokens=10, output_tokens=5)


def test_llm_enrichment_merges_outputs_and_uses_cache(tiny_cache: Path, tmp_path: Path) -> None:
    bundle = CacheReader(tiny_cache).iter_pages()[0]
    config = load_config(
        config_path=tmp_path / "missing.yaml",
        cli_overrides={
            "paths": {"llm_cache": str(tmp_path / "llm-cache")},
            "features": {
                "llm": {
                    "enabled": True,
                    "tasks": {
                        "classification": False,
                        "summary": True,
                        "keywords": False,
                        "themes": True,
                        "concepts": False,
                        "candidate_entities": True,
                        "operational_signals": False,
                        "quality_warnings": False,
                    },
                }
            },
            "llm": {
                "provider": "openai",
                "model": "mock-model",
                "costs_usd_per_1k_tokens": {"mock-model": {"input": 0.1, "output": 0.2}},
            },
        },
    )
    enrichment = enrich_page(
        bundle,
        run_id="run-1",
        dataset_name="tiny",
        config=config,
        generated_at="2026-06-17T00:00:00Z",
    )
    provider = JsonProvider()
    progress_events = []
    result = apply_llm_enrichment(
        bundle=bundle,
        enrichment=enrichment,
        config=config,
        run_id="run-1",
        dataset_name="tiny",
        generated_at="2026-06-17T00:00:00Z",
        provider=provider,
        progress_callback=progress_events.append,
    )
    assert result.enrichment.short_summary == "LLM short summary."
    assert "identity operations" in result.enrichment.themes
    assert any(entity.name == "Identity SRE" for entity in result.enrichment.candidate_entities)
    assert len(result.usage) == 3
    assert result.usage[0].estimated_cost_usd == 0.002
    assert [event["event"] for event in progress_events].count("llm_plan") == 1
    assert [event["event"] for event in progress_events].count("llm_call_started") == 3
    assert [event["event"] for event in progress_events].count("llm_call_finished") == 3

    second = apply_llm_enrichment(
        bundle=bundle,
        enrichment=enrichment,
        config=config,
        run_id="run-2",
        dataset_name="tiny",
        generated_at="2026-06-17T00:00:00Z",
        provider=provider,
    )
    assert provider.calls == 3
    assert all(usage.cached for usage in second.usage)


def test_llm_task_bundles_reduce_calls_and_merge_outputs(tiny_cache: Path, tmp_path: Path) -> None:
    bundle = CacheReader(tiny_cache).iter_pages()[0]
    config = load_config(
        config_path=tmp_path / "missing.yaml",
        cli_overrides={
            "paths": {"llm_cache": str(tmp_path / "llm-cache")},
            "features": {
                "llm": {
                    "enabled": True,
                    "tasks": {
                        "classification": False,
                        "summary": True,
                        "keywords": False,
                        "themes": True,
                        "concepts": False,
                        "candidate_entities": True,
                        "operational_signals": True,
                        "quality_warnings": True,
                    },
                }
            },
            "llm": {
                "provider": "openai",
                "model": "mock-model",
                "task_bundles": {
                    "semantic": {
                        "tasks": ["summary", "themes"],
                        "model": "mock-model",
                        "prompt_version": "semantic-v1",
                    },
                    "operational": {
                        "tasks": [
                            "candidate_entities",
                            "operational_signals",
                            "quality_warnings",
                        ],
                        "model": "mock-model",
                        "prompt_version": "operational-v1",
                    },
                },
            },
        },
    )
    work_items = enabled_llm_work_items(config)
    assert [item.name for item in work_items] == ["bundle:operational", "bundle:semantic"]
    enrichment = enrich_page(
        bundle,
        run_id="run-1",
        dataset_name="tiny",
        config=config,
        generated_at="2026-06-17T00:00:00Z",
    )
    provider = JsonProvider()
    result = apply_llm_enrichment(
        bundle=bundle,
        enrichment=enrichment,
        config=config,
        run_id="run-1",
        dataset_name="tiny",
        generated_at="2026-06-17T00:00:00Z",
        provider=provider,
    )
    assert provider.calls == 2
    assert {usage.task for usage in result.usage} == {"bundle:operational", "bundle:semantic"}
    assert result.enrichment.short_summary == "Bundled short summary."
    assert "identity operations" in result.enrichment.themes
    assert result.enrichment.operational_signals.has_support_group is True
    assert "missing_backout_steps" in result.enrichment.warnings
    assert {fact.label for fact in result.enrichment.key_facts} == {
        "Primary service",
        "Support group",
    }
    assert any(entity.name == "Identity SRE" for entity in result.enrichment.candidate_entities)


def test_prompt_template_is_loaded_from_versioned_file() -> None:
    template = load_prompt_template("summary", "summary-v1")
    assert "Task: summary" in template
    assert "{contract}" in template


def test_task_payload_schema_rejects_invalid_classification() -> None:
    try:
        validate_task_payload("classification", {"document_type": "nonsense", "confidence": 1.2})
    except ValueError as exc:
        assert "document_type" in str(exc) or "confidence" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected validation failure")


def test_classification_schema_normalizes_common_aliases() -> None:
    assert (
        validate_task_payload("classification", {"document_type": "faq", "confidence": 0.8})[
            "document_type"
        ]
        == "knowledge_article"
    )
    assert (
        validate_task_payload(
            "classification", {"document_type": "release_notes", "confidence": 0.8}
        )["document_type"]
        == "change_record"
    )
    assert (
        validate_task_payload(
            "classification", {"document_type": "test report", "confidence": 0.8}
        )["document_type"]
        == "reference"
    )
