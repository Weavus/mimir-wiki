from __future__ import annotations

import asyncio
import base64
import json
import shutil
from pathlib import Path

import httpx

import mimir_wiki.pipeline as pipeline
from mimir_wiki.cache_reader import CacheReader
from mimir_wiki.config import load_config
from mimir_wiki.llm.probe import generate_probe_png
from mimir_wiki.pipeline import (
    enrich_command,
    extract_visuals_command,
    report_command,
    validate_cache_command,
)
from mimir_wiki.writers.onyx_markdown import render_visual_extraction


def test_enrich_provider_none_writes_mvp_artifacts(tiny_cache: Path, tmp_path: Path) -> None:
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )
    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)
    assert result.exit_code == 0
    enrichment_path = tiny_cache / "pages" / "123" / "enrichment.json"
    assert enrichment_path.exists()
    enrichment = json.loads(enrichment_path.read_text(encoding="utf-8"))
    assert enrichment["schema_version"] == "mimir-wiki/v1"
    assert enrichment["document_type"] == "runbook"
    assert enrichment["hierarchy"]["parent_title"] == "Runbooks"
    assert enrichment["hierarchy"]["page_role"] == "runbook_detail"
    assert enrichment["candidate_facts"]
    facts_by_predicate = {fact["predicate"]: fact for fact in enrichment["candidate_facts"]}
    predicates = set(facts_by_predicate)
    assert "owned_by" in predicates
    assert "supported_by" in predicates
    assert "has_diagnostic_step" in predicates
    assert facts_by_predicate["owned_by"]["claim_type"] == "ownership"
    assert facts_by_predicate["supported_by"]["claim_type"] == "support_model"
    assert facts_by_predicate["has_diagnostic_step"]["claim_type"] == "procedure"
    assert (tmp_path / "knowledge" / "document_index.jsonl").exists()
    assert (tmp_path / "knowledge" / "quality_scores.jsonl").exists()
    assert (tmp_path / "knowledge" / "themes.jsonl").exists()
    assert (tmp_path / "knowledge" / "concepts.jsonl").exists()
    assert (tmp_path / "knowledge" / "candidate_entities.jsonl").exists()
    assert (tmp_path / "knowledge" / "facts.jsonl").exists()
    assert (tmp_path / "knowledge" / "visual_index.jsonl").exists()
    fact_rows = [
        json.loads(line)
        for line in (tmp_path / "knowledge" / "facts.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert fact_rows
    assert {row["predicate"]: row for row in fact_rows}["owned_by"]["claim_type"] == "ownership"
    document_index = [
        json.loads(line)
        for line in (tmp_path / "knowledge" / "document_index.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert document_index[0]["parent_title"] == "Runbooks"
    assert document_index[0]["page_role"] == "runbook_detail"
    onyx_files = list((tmp_path / "dist" / "onyx-enriched" / "tiny" / "IDENTITY").glob("*.md"))
    assert len(onyx_files) == 1
    first_line = onyx_files[0].read_text(encoding="utf-8").splitlines()[0]
    onyx_content = onyx_files[0].read_text(encoding="utf-8")
    assert first_line.startswith("#ONYX_METADATA=")
    metadata = json.loads(first_line.removeprefix("#ONYX_METADATA="))
    assert metadata["link"] == "https://confluence.example.com/pages/viewpage.action?pageId=123"
    assert metadata["file_display_name"] == "ForgeRock Support Runbook"
    assert metadata["doc_updated_at"] == "2026-05-01T12:45:00Z"
    assert "run_id" not in metadata
    assert "## Answer Summary" in onyx_content
    assert "## Key Facts" in onyx_content
    assert "- Parent section: Runbooks" in onyx_content
    assert "- Page role: runbook_detail" in onyx_content
    assert onyx_content.index("## Source Content") < onyx_content.index("## Enrichment Details")
    assert (tmp_path / "reports" / "enrichment_summary.md").exists()
    assert (tmp_path / "reports" / "duplicate_candidates.md").exists()
    assert (tmp_path / "reports" / "high_value_subtrees.md").exists()
    assert (tmp_path / "reports" / "llm_usage.md").exists()
    assert (tmp_path / "reports" / "visual_extraction.md").exists()
    assert any((tmp_path / "runs").glob("*/summary.json"))


def test_report_scopes_run_artifacts_to_selected_dataset(tiny_cache: Path, tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    old_selected_run = runs / "20260622T000000Z-enrich-old-selected"
    selected_run = runs / "20260622T010000Z-enrich-selected"
    other_run = runs / "20260622T020000Z-enrich-other"
    for run_dir, dataset, cache, page_id, error_type in (
        (old_selected_run, "tiny", tiny_cache, "122", "OldSelectedError"),
        (selected_run, "tiny", tiny_cache, "123", "SelectedError"),
        (other_run, "other", tmp_path / "cache" / "other", "999", "OtherError"),
    ):
        run_dir.mkdir(parents=True)
        run_id = run_dir.name
        (run_dir / "summary.json").write_text(
            json.dumps(
                {
                    "schema_version": "mimir-wiki/v1",
                    "run_id": run_id,
                    "generated_at": "2026-06-22T00:00:00Z",
                    "generator": "mimir-wiki",
                    "dataset_name": dataset,
                    "command": "enrich",
                    "started_at": "2026-06-22T00:00:00Z",
                    "finished_at": "2026-06-22T00:01:00Z",
                    "elapsed_seconds": 60,
                    "status": "partial_success",
                    "exit_code": 3,
                    "cache_path": str(cache),
                    "counts": {"pages_processed": 1, "pages_failed": 1},
                    "outputs": {},
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "page_failures.jsonl").write_text(
            json.dumps(
                {
                    "schema_version": "mimir-wiki/v1",
                    "run_id": run_id,
                    "generated_at": "2026-06-22T00:00:00Z",
                    "generator": "mimir-wiki",
                    "dataset_name": dataset,
                    "source_system": "confluence",
                    "document_id": f"confluence:IDENTITY:{page_id}",
                    "page_id": page_id,
                    "space_key": "IDENTITY",
                    "source_updated_at": "2026-05-01T12:45:00Z",
                    "source_content_hash": "sha256:a",
                    "stage": "enrich",
                    "error_type": error_type,
                    "message": error_type,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (run_dir / "llm_usage.jsonl").write_text(
            json.dumps(
                {
                    "schema_version": "mimir-wiki/v1",
                    "run_id": run_id,
                    "generated_at": "2026-06-22T00:00:00Z",
                    "generator": "mimir-wiki",
                    "dataset_name": dataset,
                    "source_system": "confluence",
                    "document_id": f"confluence:IDENTITY:{page_id}",
                    "page_id": page_id,
                    "space_key": "IDENTITY",
                    "source_updated_at": "2026-05-01T12:45:00Z",
                    "source_content_hash": "sha256:a",
                    "task": "summary",
                    "provider": "mock",
                    "model": "mock-model",
                    "prompt_version": "summary-v1",
                    "input_tokens": 10,
                    "output_tokens": 5,
                }
            )
            + "\n",
            encoding="utf-8",
        )

    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(runs),
            },
            "llm": {"provider": "none"},
        }
    )

    result = report_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)

    assert result.exit_code == 0
    page_failures = (tmp_path / "reports" / "page_failures.md").read_text(encoding="utf-8")
    assert "SelectedError" in page_failures
    assert "OldSelectedError" not in page_failures
    assert "OtherError" not in page_failures
    llm_usage = (tmp_path / "reports" / "llm_usage.md").read_text(encoding="utf-8")
    assert "summary" in llm_usage
    summary = (tmp_path / "reports" / "enrichment_summary.md").read_text(encoding="utf-8")
    assert "20260622T010000Z-enrich-selected" in summary
    assert "20260622T000000Z-enrich-old-selected" not in summary
    assert "20260622T020000Z-enrich-other" not in summary

    result = report_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=False,
        source_run_ids=["20260622T000000Z-enrich-old-selected"],
    )

    assert result.exit_code == 0
    page_failures = (tmp_path / "reports" / "page_failures.md").read_text(encoding="utf-8")
    assert "OldSelectedError" in page_failures
    assert "| IDENTITY | 123 | enrich | SelectedError |" not in page_failures


def test_onyx_markdown_strips_outline_number_from_display_title(
    tiny_cache: Path, tmp_path: Path
) -> None:
    metadata_path = tiny_cache / "pages" / "123" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["title"] = "1.1.1 Database information"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )
    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)
    assert result.exit_code == 0
    onyx_file = next((tmp_path / "dist" / "onyx-enriched" / "tiny" / "IDENTITY").glob("*.md"))
    content = onyx_file.read_text(encoding="utf-8")
    assert "# Database information" in content
    assert "- Page title: Database information" in content
    assert "- Original source title: 1.1.1 Database information" in content


def test_onyx_markdown_replaces_stale_slug_file(tiny_cache: Path, tmp_path: Path) -> None:
    out_root = tmp_path / "dist" / "onyx-enriched"
    stale_dir = out_root / "tiny" / "IDENTITY"
    stale_dir.mkdir(parents=True)
    stale_file = stale_dir / "123-old-title.md"
    stale_file.write_text("stale", encoding="utf-8")
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(out_root),
            },
            "llm": {"provider": "none"},
        }
    )

    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)

    assert result.exit_code == 0
    onyx_files = sorted(stale_dir.glob("123-*.md"))
    assert len(onyx_files) == 1
    assert onyx_files[0].name == "123-forgerock-support-runbook.md"
    assert not stale_file.exists()


def test_onyx_export_filters_are_permissive_by_default(tiny_cache: Path, tmp_path: Path) -> None:
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )

    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)

    assert result.exit_code == 0
    assert list((tmp_path / "dist" / "onyx-enriched" / "tiny" / "IDENTITY").glob("*.md"))


def test_onyx_export_filters_can_exclude_configured_audiences(
    tiny_cache: Path, tmp_path: Path
) -> None:
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8") + "\nCustomer: Example Bank name@example.com\n",
        encoding="utf-8",
    )
    text_path = tiny_cache / "pages" / "123" / "text.txt"
    text_path.write_text(
        text_path.read_text(encoding="utf-8") + " Customer Example Bank name@example.com",
        encoding="utf-8",
    )
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
            "onyx_poc": {"exclude_audiences": ["restricted_internal"]},
        }
    )

    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)

    assert result.exit_code == 0
    assert not list((tmp_path / "dist" / "onyx-enriched" / "tiny" / "IDENTITY").glob("*.md"))


def test_performance_test_subtype_maps_unknown_to_reference(
    tiny_cache: Path, tmp_path: Path
) -> None:
    metadata_path = tiny_cache / "pages" / "123" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["title"] = "2024/09/25 Pre-prod Load testing result (K6)"
    metadata["labels"] = []
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        "# 2024/09/25 Pre-prod Load testing result (K6)\n\nActual throughput was 73 req/s.",
        encoding="utf-8",
    )
    text_path = tiny_cache / "pages" / "123" / "text.txt"
    text_path.write_text("Pre-prod Load testing result K6 throughput report", encoding="utf-8")
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )
    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)
    assert result.exit_code == 0
    enrichment = json.loads((tiny_cache / "pages" / "123" / "enrichment.json").read_text())
    assert enrichment["document_subtype"] == "performance_test_report"
    assert enrichment["document_type"] == "reference"


def test_integration_path_classifies_as_onboarding(tiny_cache: Path, tmp_path: Path) -> None:
    metadata_path = tiny_cache / "pages" / "123" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["title"] = "Tier 2: Identity Management - Group-based provisioning"
    metadata["labels"] = []
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        "# Tier 2: Identity Management - Group-based provisioning\n\n"
        "This flow uses SCIM provisioning via customer-defined group-based rules.",
        encoding="utf-8",
    )
    text_path = tiny_cache / "pages" / "123" / "text.txt"
    text_path.write_text("Tier 2 integration path SCIM provisioning", encoding="utf-8")
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )
    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)
    assert result.exit_code == 0
    enrichment = json.loads((tiny_cache / "pages" / "123" / "enrichment.json").read_text())
    assert enrichment["document_type"] == "onboarding"
    assert enrichment["document_subtype"] == "integration_path"


def test_question_log_classifies_as_reference(tiny_cache: Path, tmp_path: Path) -> None:
    metadata_path = tiny_cache / "pages" / "123" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["title"] = "Open Questions"
    metadata["labels"] = []
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        "# Open Questions\n\n| Question | Status |\n| --- | --- |\n| What is next? | OPEN |\n",
        encoding="utf-8",
    )
    text_path = tiny_cache / "pages" / "123" / "text.txt"
    text_path.write_text("Open Questions Question Log Status OPEN", encoding="utf-8")
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )
    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)
    assert result.exit_code == 0
    enrichment = json.loads((tiny_cache / "pages" / "123" / "enrichment.json").read_text())
    assert enrichment["document_type"] == "reference"
    assert enrichment["document_subtype"] == "question_log"


def test_daily_handover_classifies_as_meeting_notes(tiny_cache: Path, tmp_path: Path) -> None:
    metadata_path = tiny_cache / "pages" / "123" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["title"] = "CIAM AAA 05-2026 RE Daily Handover"
    metadata["labels"] = []
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    (tiny_cache / "pages" / "123" / "text.txt").write_text(
        "Daily handover incident ticket release check support group restart notes",
        encoding="utf-8",
    )
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )

    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)

    assert result.exit_code == 0
    enrichment = json.loads((tiny_cache / "pages" / "123" / "enrichment.json").read_text())
    assert enrichment["document_type"] == "meeting_notes"
    assert enrichment["document_subtype"] == "daily_handover"


def test_service_review_classifies_as_meeting_notes(tiny_cache: Path, tmp_path: Path) -> None:
    metadata_path = tiny_cache / "pages" / "123" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["title"] = "2026-05-14 CIAM Ping Service Review Meeting"
    metadata["labels"] = []
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    (tiny_cache / "pages" / "123" / "text.txt").write_text(
        "Service review meeting work items support group recovery release notes",
        encoding="utf-8",
    )
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )

    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)

    assert result.exit_code == 0
    enrichment = json.loads((tiny_cache / "pages" / "123" / "enrichment.json").read_text())
    assert enrichment["document_type"] == "meeting_notes"
    assert enrichment["document_subtype"] == "service_review_notes"


def test_video_library_classifies_as_reference(tiny_cache: Path, tmp_path: Path) -> None:
    metadata_path = tiny_cache / "pages" / "123" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["title"] = "Entra Video Library"
    metadata["labels"] = []
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    (tiny_cache / "pages" / "123" / "text.txt").write_text(
        "Video library script customer introduction troubleshooting support",
        encoding="utf-8",
    )
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )

    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)

    assert result.exit_code == 0
    enrichment = json.loads((tiny_cache / "pages" / "123" / "enrichment.json").read_text())
    assert enrichment["document_type"] == "reference"
    assert enrichment["document_subtype"] == "video_library"


def test_resource_endpoints_override_runbook(tiny_cache: Path, tmp_path: Path) -> None:
    metadata_path = tiny_cache / "pages" / "123" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["title"] = "IAM SCIM API 1.1.0 Resources & Endpoints"
    metadata["labels"] = []
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    (tiny_cache / "pages" / "123" / "text.txt").write_text(
        "Resources endpoints support group check health api list",
        encoding="utf-8",
    )
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )

    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)

    assert result.exit_code == 0
    enrichment = json.loads((tiny_cache / "pages" / "123" / "enrichment.json").read_text())
    assert enrichment["document_type"] == "reference"
    assert enrichment["document_subtype"] == "resource_endpoint_reference"


def test_no_exportable_body_content_is_flagged_and_capped(tiny_cache: Path, tmp_path: Path) -> None:
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text("# Empty Page\n\n_No exportable body content._\n", encoding="utf-8")
    text_path = tiny_cache / "pages" / "123" / "text.txt"
    text_path.write_text("Empty Page No exportable body content", encoding="utf-8")
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )

    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)

    assert result.exit_code == 0
    enrichment = json.loads((tiny_cache / "pages" / "123" / "enrichment.json").read_text())
    assert enrichment["content_availability"] == "empty"
    assert enrichment["quality"]["overall_score"] <= 25
    assert "no_exportable_body_content" in enrichment["review_flags"]
    onyx_file = next((tmp_path / "dist" / "onyx-enriched" / "tiny" / "IDENTITY").glob("*.md"))
    assert "Content availability: `empty`" in onyx_file.read_text(encoding="utf-8")


def test_onyx_limits_early_links_and_rewrites_images(tiny_cache: Path, tmp_path: Path) -> None:
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + "\n![Architecture](https://example.com/path/diagram.png?x=1)\n",
        encoding="utf-8",
    )
    links_path = tiny_cache / "pages" / "123" / "links.json"
    links = json.loads(links_path.read_text(encoding="utf-8"))
    links["links"] = [
        {
            "type": "external_url",
            "href": f"https://example.com/runbook-{index}",
            "text": f"Runbook {index}",
            "crawlable": False,
            "target_page_id": None,
            "target_space_key": None,
            "target_title": None,
        }
        for index in range(10)
    ]
    links["links"].append(
        {
            "type": "external_url",
            "href": "mailto:test@example.com",
            "text": "test@example.com",
            "crawlable": False,
            "target_page_id": None,
            "target_space_key": None,
            "target_title": None,
        }
    )
    links["links"].append(
        {
            "type": "anchor",
            "href": "#local-heading",
            "text": "Local Heading",
            "crawlable": False,
            "target_page_id": None,
            "target_space_key": None,
            "target_title": None,
        }
    )
    links_path.write_text(json.dumps(links), encoding="utf-8")
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )
    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)
    assert result.exit_code == 0
    onyx_file = next((tmp_path / "dist" / "onyx-enriched" / "tiny" / "IDENTITY").glob("*.md"))
    content = onyx_file.read_text(encoding="utf-8")
    early_links = content.split("## Source Content", maxsplit=1)[0]
    assert early_links.count("https://example.com/runbook-") == 8
    assert "## Additional Source Links" in content
    assert "mailto:test@example.com" in content.split("## Additional Source Links", maxsplit=1)[1]
    assert "#local-heading" not in content
    assert "Image omitted from source export: Architecture" in content
    enrichment = json.loads((tiny_cache / "pages" / "123" / "enrichment.json").read_text())
    assert "visual_content_missing" in enrichment["review_flags"]


def test_extract_visuals_writes_artifact_and_enrichment_marks_extracted(
    tiny_cache: Path, tmp_path: Path, monkeypatch
) -> None:
    image_data = base64.b64encode(generate_probe_png()).decode("ascii")
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + f"\n![Probe](data:image/png;base64,{image_data})\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_FOUNDRY_ENDPOINT", "https://example.services.ai.azure.com/openai/v1")
    monkeypatch.setenv("TEST_FOUNDRY_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["model"] == "gpt-5.4-mini"
        assert payload["input"][0]["content"][1]["type"] == "input_image"
        return httpx.Response(
            200,
            json={
                "model": "gpt-5.4-mini",
                "output_text": json.dumps(
                    {
                        "ocr_text": "MIMIR 42",
                        "caption": "Probe image containing MIMIR 42.",
                        "confidence": 0.99,
                    }
                ),
            },
        )

    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {
                "provider": "none",
                "azure_ai_foundry": {
                    "endpoint_env": "TEST_FOUNDRY_ENDPOINT",
                    "api_key_env": "TEST_FOUNDRY_KEY",
                    "deployment_env": "",
                },
            },
            "visual_extraction": {
                "provider": "azure-ai-foundry",
                "model": "gpt-5.4-mini",
            },
        }
    )
    result = extract_visuals_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=False,
        llm_transport=httpx.MockTransport(handler),
    )
    assert result.exit_code == 0
    artifact = json.loads((tiny_cache / "pages" / "123" / "visual_extraction.json").read_text())
    assert artifact["status"] == "complete"
    assert artifact["model"] == "gpt-5.4-mini"
    assert artifact["images_succeeded"] == 1
    assert artifact["images"][0]["ocr_text"] == "MIMIR 42"
    assert result.summary.counts["llm_calls"] == 1
    usage_path = next((tmp_path / "runs").glob("*/llm_usage.jsonl"))
    usage_rows = [json.loads(line) for line in usage_path.read_text(encoding="utf-8").splitlines()]
    assert usage_rows[0]["task"] == "visual_ocr"
    assert usage_rows[0]["provider"] == "azure-ai-foundry"

    enrich_result = enrich_command(
        config=config, cache_path=tiny_cache, profile=None, dry_run=False
    )
    assert enrich_result.exit_code == 0
    enrichment = json.loads((tiny_cache / "pages" / "123" / "enrichment.json").read_text())
    assert "visual_content_extracted" in enrichment["review_flags"]
    assert "visual_content_missing" not in enrichment["review_flags"]
    visual_rows = [
        json.loads(line)
        for line in (tmp_path / "knowledge" / "visual_index.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(visual_rows) == 1
    assert visual_rows[0]["image_id"] == artifact["images"][0]["image_id"]
    assert visual_rows[0]["ocr_text"] == "MIMIR 42"
    assert visual_rows[0]["visual_run_id"] == artifact["run_id"]
    onyx_file = next((tmp_path / "dist" / "onyx-enriched" / "tiny" / "IDENTITY").glob("*.md"))
    content = onyx_file.read_text(encoding="utf-8")
    assert "## Extracted Visual Content" in content
    assert "MIMIR 42" in content


def test_extract_visuals_reuses_duplicate_image_hashes(
    tiny_cache: Path, tmp_path: Path, monkeypatch
) -> None:
    first_path = tiny_cache / "pages" / "123" / "attachments" / "first.png"
    second_path = tiny_cache / "pages" / "123" / "attachments" / "second.png"
    first_path.write_bytes(generate_probe_png())
    second_path.write_bytes(generate_probe_png())
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + "\n![First](attachments/first.png)\n![Second](attachments/second.png)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_FOUNDRY_ENDPOINT", "https://example.services.ai.azure.com/openai/v1")
    monkeypatch.setenv("TEST_FOUNDRY_KEY", "test-key")

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": "gpt-5.4-mini",
                "output_text": json.dumps(
                    {"ocr_text": "MIMIR 42", "caption": "Duplicate probe", "confidence": 0.99}
                ),
            },
        )

    config = load_config(
        cli_overrides={
            "paths": {"reports": str(tmp_path / "reports"), "runs": str(tmp_path / "runs")},
            "llm": {
                "provider": "none",
                "azure_ai_foundry": {
                    "endpoint_env": "TEST_FOUNDRY_ENDPOINT",
                    "api_key_env": "TEST_FOUNDRY_KEY",
                    "deployment_env": "",
                },
            },
            "visual_extraction": {"provider": "azure-ai-foundry", "model": "gpt-5.4-mini"},
        }
    )
    result = extract_visuals_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=False,
        llm_transport=httpx.MockTransport(handler),
    )

    assert result.exit_code == 0
    assert calls == 1
    assert result.summary.counts["llm_calls"] == 1
    artifact = json.loads((tiny_cache / "pages" / "123" / "visual_extraction.json").read_text())
    assert artifact["images_succeeded"] == 2
    assert artifact["images"][0]["cache_hit"] is False
    assert artifact["images"][1]["cache_hit"] is True


def test_extract_visuals_processes_pages_concurrently(
    tiny_cache: Path, tmp_path: Path, monkeypatch
) -> None:
    second_page = tiny_cache / "pages" / "456"
    shutil.copytree(tiny_cache / "pages" / "123", second_page)
    metadata = json.loads((second_page / "metadata.json").read_text(encoding="utf-8"))
    metadata["page_id"] = "456"
    metadata["title"] = "Second Visual Page"
    (second_page / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    links = json.loads((second_page / "links.json").read_text(encoding="utf-8"))
    links["page_id"] = "456"
    (second_page / "links.json").write_text(json.dumps(links), encoding="utf-8")
    image_by_page = {
        "123": base64.b64encode(generate_probe_png()).decode("ascii"),
        "456": base64.b64encode(generate_probe_png() + b"different").decode("ascii"),
    }
    for page_id, image_data in image_by_page.items():
        clean_path = tiny_cache / "pages" / page_id / "clean.md"
        clean_path.write_text(
            clean_path.read_text(encoding="utf-8")
            + f"\n![Probe {page_id}](data:image/png;base64,{image_data})\n",
            encoding="utf-8",
        )
    manifest_path = tiny_cache / "manifest.jsonl"
    manifest_rows = [json.loads(line) for line in manifest_path.read_text().splitlines()]
    second_row = dict(
        manifest_rows[0],
        markdown_path="pages/456/clean.md",
        page_id="456",
        path="pages/456",
        title="Second Visual Page",
    )
    manifest_path.write_text(
        "\n".join(json.dumps(row) for row in [manifest_rows[0], second_row]) + "\n",
        encoding="utf-8",
    )
    summary_path = tiny_cache / "manifest.summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["total_pages"] = 2
    summary["spaces"] = {"IDENTITY": 2}
    summary["statuses"] = {"success": 2}
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    monkeypatch.setenv("TEST_FOUNDRY_ENDPOINT", "https://example.services.ai.azure.com/openai/v1")
    monkeypatch.setenv("TEST_FOUNDRY_KEY", "test-key")

    active = 0
    max_active = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(
            200,
            json={"output_text": json.dumps({"ocr_text": "OK", "caption": "Concurrent"})},
        )

    config = load_config(
        cli_overrides={
            "paths": {"reports": str(tmp_path / "reports"), "runs": str(tmp_path / "runs")},
            "processing": {"page_workers": 2},
            "llm": {
                "provider": "none",
                "max_concurrency": 2,
                "adaptive_initial_concurrency": 2,
                "azure_ai_foundry": {
                    "endpoint_env": "TEST_FOUNDRY_ENDPOINT",
                    "api_key_env": "TEST_FOUNDRY_KEY",
                    "deployment_env": "",
                },
            },
            "visual_extraction": {"provider": "azure-ai-foundry", "model": "gpt-5.4-mini"},
        }
    )
    result = extract_visuals_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=False,
        llm_transport=httpx.MockTransport(handler),
    )

    assert result.exit_code == 0
    assert result.summary.counts["llm_calls"] == 2
    assert max_active == 2


def test_extract_visuals_retries_with_shared_llm_client(
    tiny_cache: Path, tmp_path: Path, monkeypatch
) -> None:
    image_data = base64.b64encode(generate_probe_png()).decode("ascii")
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + f"\n![Probe](data:image/png;base64,{image_data})\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_FOUNDRY_ENDPOINT", "https://example.services.ai.azure.com/openai/v1")
    monkeypatch.setenv("TEST_FOUNDRY_KEY", "test-key")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"message": "try later"}})
        return httpx.Response(
            200,
            json={
                "model": "gpt-5.4-mini",
                "output_text": json.dumps({"ocr_text": "MIMIR 42", "caption": "Probe"}),
                "usage": {"input_tokens": 11, "output_tokens": 7},
            },
        )

    config = load_config(
        cli_overrides={
            "paths": {"reports": str(tmp_path / "reports"), "runs": str(tmp_path / "runs")},
            "llm": {
                "provider": "none",
                "initial_backoff_seconds": 0.001,
                "max_backoff_seconds": 0.001,
                "backoff_jitter": False,
                "azure_ai_foundry": {
                    "endpoint_env": "TEST_FOUNDRY_ENDPOINT",
                    "api_key_env": "TEST_FOUNDRY_KEY",
                    "deployment_env": "",
                },
            },
            "visual_extraction": {"provider": "azure-ai-foundry", "model": "gpt-5.4-mini"},
        }
    )
    result = extract_visuals_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=False,
        llm_transport=httpx.MockTransport(handler),
    )

    assert result.exit_code == 0
    assert calls == 2
    assert result.summary.counts["llm_retries"] == 1
    usage_path = next((tmp_path / "runs").glob("*/llm_usage.jsonl"))
    usage_rows = [json.loads(line) for line in usage_path.read_text(encoding="utf-8").splitlines()]
    assert usage_rows[0]["attempts"] == 2
    assert usage_rows[0]["retries"] == 1
    assert usage_rows[0]["input_tokens"] == 11
    assert usage_rows[0]["output_tokens"] == 7


def test_extract_visuals_resolves_urls_to_local_attachments(
    tiny_cache: Path, tmp_path: Path, monkeypatch
) -> None:
    attachment_path = tiny_cache / "pages" / "123" / "attachments" / "diagram.png"
    attachment_path.write_bytes(generate_probe_png())
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + "\n![Diagram](https://confluence.example.com/download/attachments/123/diagram.png?api=v2)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_FOUNDRY_ENDPOINT", "https://example.services.ai.azure.com/openai/v1")
    monkeypatch.setenv("TEST_FOUNDRY_KEY", "test-key")

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["input"][0]["content"][1]["image_url"].startswith("data:image/png")
        return httpx.Response(
            200,
            json={"output_text": json.dumps({"ocr_text": "LOCAL IMAGE", "caption": "Local"})},
        )

    config = load_config(
        cli_overrides={
            "paths": {"reports": str(tmp_path / "reports"), "runs": str(tmp_path / "runs")},
            "llm": {
                "provider": "none",
                "azure_ai_foundry": {
                    "endpoint_env": "TEST_FOUNDRY_ENDPOINT",
                    "api_key_env": "TEST_FOUNDRY_KEY",
                    "deployment_env": "",
                },
            },
            "visual_extraction": {"provider": "azure-ai-foundry", "model": "gpt-5.4-mini"},
        }
    )
    result = extract_visuals_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=False,
        llm_transport=httpx.MockTransport(handler),
    )
    assert result.exit_code == 0
    artifact = json.loads((tiny_cache / "pages" / "123" / "visual_extraction.json").read_text())
    assert calls == 1
    assert artifact["status"] == "complete"
    assert artifact["images"][0]["source"] == str(attachment_path)
    assert artifact["images"][0]["source_kind"] == "file"


def test_extract_visuals_resolves_cross_page_attachment_urls(
    tiny_cache: Path, tmp_path: Path, monkeypatch
) -> None:
    other_attachment_path = tiny_cache / "pages" / "456" / "attachments" / "shared diagram.png"
    other_attachment_path.parent.mkdir(parents=True)
    other_attachment_path.write_bytes(generate_probe_png())
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + "\n![Shared](https://confluence.example.com/download/attachments/456/shared%20diagram.png?api=v2)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_FOUNDRY_ENDPOINT", "https://example.services.ai.azure.com/openai/v1")
    monkeypatch.setenv("TEST_FOUNDRY_KEY", "test-key")

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["input"][0]["content"][1]["image_url"].startswith("data:image/png")
        return httpx.Response(
            200,
            json={"output_text": json.dumps({"ocr_text": "CROSS PAGE", "caption": "Shared"})},
        )

    config = load_config(
        cli_overrides={
            "paths": {"reports": str(tmp_path / "reports"), "runs": str(tmp_path / "runs")},
            "llm": {
                "provider": "none",
                "azure_ai_foundry": {
                    "endpoint_env": "TEST_FOUNDRY_ENDPOINT",
                    "api_key_env": "TEST_FOUNDRY_KEY",
                    "deployment_env": "",
                },
            },
            "visual_extraction": {"provider": "azure-ai-foundry", "model": "gpt-5.4-mini"},
        }
    )
    result = extract_visuals_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=False,
        llm_transport=httpx.MockTransport(handler),
    )

    assert result.exit_code == 0
    artifact = json.loads((tiny_cache / "pages" / "123" / "visual_extraction.json").read_text())
    assert calls == 1
    assert artifact["status"] == "complete"
    assert artifact["images"][0]["source"] == str(other_attachment_path)
    assert artifact["images"][0]["source_kind"] == "file"


def test_extract_visuals_skips_remote_images_not_in_cache(
    tiny_cache: Path, tmp_path: Path, monkeypatch
) -> None:
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + "\n![Missing](https://confluence.example.com/download/attachments/123/missing.png?api=v2)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_FOUNDRY_ENDPOINT", "https://example.services.ai.azure.com/openai/v1")
    monkeypatch.setenv("TEST_FOUNDRY_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("mimir-wiki must not fetch or send remote-only image references")

    config = load_config(
        cli_overrides={
            "paths": {"reports": str(tmp_path / "reports"), "runs": str(tmp_path / "runs")},
            "llm": {
                "provider": "none",
                "azure_ai_foundry": {
                    "endpoint_env": "TEST_FOUNDRY_ENDPOINT",
                    "api_key_env": "TEST_FOUNDRY_KEY",
                    "deployment_env": "",
                },
            },
            "visual_extraction": {"provider": "azure-ai-foundry", "model": "gpt-5.4-mini"},
        }
    )
    result = extract_visuals_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=False,
        llm_transport=httpx.MockTransport(handler),
    )
    assert result.exit_code == 0
    artifact = json.loads((tiny_cache / "pages" / "123" / "visual_extraction.json").read_text())
    assert artifact["status"] == "skipped"
    assert artifact["images"][0]["status"] == "skipped"
    assert artifact["images"][0]["error_type"] == "remote_source_not_in_cache"


def test_extract_visuals_skips_missing_cross_page_attachment_urls(
    tiny_cache: Path, tmp_path: Path, monkeypatch
) -> None:
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + "\n![Missing](https://confluence.example.com/download/attachments/456/missing.png?api=v2)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_FOUNDRY_ENDPOINT", "https://example.services.ai.azure.com/openai/v1")
    monkeypatch.setenv("TEST_FOUNDRY_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("missing cross-page attachment should not be sent to the provider")

    config = load_config(
        cli_overrides={
            "paths": {"reports": str(tmp_path / "reports"), "runs": str(tmp_path / "runs")},
            "llm": {
                "provider": "none",
                "azure_ai_foundry": {
                    "endpoint_env": "TEST_FOUNDRY_ENDPOINT",
                    "api_key_env": "TEST_FOUNDRY_KEY",
                    "deployment_env": "",
                },
            },
            "visual_extraction": {"provider": "azure-ai-foundry", "model": "gpt-5.4-mini"},
        }
    )
    result = extract_visuals_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=False,
        llm_transport=httpx.MockTransport(handler),
    )

    assert result.exit_code == 0
    artifact = json.loads((tiny_cache / "pages" / "123" / "visual_extraction.json").read_text())
    assert artifact["status"] == "skipped"
    assert artifact["images"][0]["status"] == "skipped"
    assert artifact["images"][0]["source_kind"] == "url"
    assert artifact["images"][0]["error_type"] == "remote_source_not_in_cache"


def test_extract_visuals_ignores_confluence_icon_references(
    tiny_cache: Path, tmp_path: Path
) -> None:
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8") + "\n![External link](/images/icons/linkext7.gif)\n",
        encoding="utf-8",
    )
    config = load_config(
        cli_overrides={
            "paths": {"reports": str(tmp_path / "reports"), "runs": str(tmp_path / "runs")},
            "llm": {"provider": "none"},
        }
    )
    result = extract_visuals_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=True,
    )

    assert result.exit_code == 0
    assert result.summary.counts["pages_considered"] == 0
    assert result.summary.counts["visual_images_discovered"] == 0


def test_extract_visuals_emits_progress_snapshots(tiny_cache: Path, tmp_path: Path) -> None:
    image_data = base64.b64encode(generate_probe_png()).decode("ascii")
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + f"\n![Probe](data:image/png;base64,{image_data})\n",
        encoding="utf-8",
    )
    config = load_config(
        cli_overrides={
            "paths": {"reports": str(tmp_path / "reports"), "runs": str(tmp_path / "runs")},
            "llm": {"provider": "none"},
        }
    )
    snapshots: list[dict[str, object]] = []
    result = extract_visuals_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=True,
        progress_callback=snapshots.append,
    )
    assert result.exit_code == 0
    assert snapshots
    assert snapshots[-1]["current_status"] == "done"
    assert snapshots[-1]["scanned"] == 1
    assert snapshots[-1]["considered"] == 1
    assert snapshots[-1]["processed"] == 1
    assert snapshots[-1]["images_discovered"] == 1


def test_extract_visuals_reports_images_omitted_by_page_cap(
    tiny_cache: Path, tmp_path: Path
) -> None:
    image_data = base64.b64encode(generate_probe_png()).decode("ascii")
    attachment_path = tiny_cache / "pages" / "123" / "attachments" / "second.png"
    attachment_path.write_bytes(generate_probe_png())
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + f"\n![Probe 1](data:image/png;base64,{image_data})\n"
        + "\n![Probe 2](attachments/second.png)\n",
        encoding="utf-8",
    )
    config = load_config(
        cli_overrides={
            "paths": {"reports": str(tmp_path / "reports"), "runs": str(tmp_path / "runs")},
            "llm": {"provider": "none"},
            "visual_extraction": {"max_images_per_page": 1},
        }
    )
    result = extract_visuals_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=True,
    )

    assert result.exit_code == 0
    assert result.summary.counts["visual_images_discovered"] == 2
    assert result.summary.counts["visual_images_considered"] == 1
    assert result.summary.counts["visual_images_omitted_by_page_cap"] == 1
    assert result.summary.counts["visual_pages_capped"] == 1
    assert result.warnings[0].warning_type == "visual_images_omitted_by_page_cap"


def test_extract_visuals_ranks_sources_before_applying_page_cap(
    tiny_cache: Path, tmp_path: Path, monkeypatch
) -> None:
    logo_path = tiny_cache / "pages" / "123" / "attachments" / "logo.png"
    architecture_path = tiny_cache / "pages" / "123" / "attachments" / "architecture.png"
    logo_path.write_bytes(generate_probe_png())
    architecture_path.write_bytes(generate_probe_png())
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + "\n![Logo icon](attachments/logo.png)\n"
        + "\n## Architecture\n![Service topology](attachments/architecture.png)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_FOUNDRY_ENDPOINT", "https://example.services.ai.azure.com/openai/v1")
    monkeypatch.setenv("TEST_FOUNDRY_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"output_text": json.dumps({"ocr_text": "ARCH", "caption": "Architecture"})},
        )

    config = load_config(
        cli_overrides={
            "paths": {"reports": str(tmp_path / "reports"), "runs": str(tmp_path / "runs")},
            "llm": {
                "provider": "none",
                "azure_ai_foundry": {
                    "endpoint_env": "TEST_FOUNDRY_ENDPOINT",
                    "api_key_env": "TEST_FOUNDRY_KEY",
                    "deployment_env": "",
                },
            },
            "visual_extraction": {
                "provider": "azure-ai-foundry",
                "model": "gpt-5.4-mini",
                "max_images_per_page": 1,
            },
        }
    )
    result = extract_visuals_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=False,
        llm_transport=httpx.MockTransport(handler),
    )

    assert result.exit_code == 0
    artifact = json.loads((tiny_cache / "pages" / "123" / "visual_extraction.json").read_text())
    assert artifact["images"][0]["source"] == str(architecture_path)


def test_extract_visuals_writes_omitted_image_inventory(
    tiny_cache: Path, tmp_path: Path, monkeypatch
) -> None:
    first_path = tiny_cache / "pages" / "123" / "attachments" / "first.png"
    second_path = tiny_cache / "pages" / "123" / "attachments" / "second.png"
    first_path.write_bytes(generate_probe_png())
    second_path.write_bytes(generate_probe_png())
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + "\n![First](attachments/first.png)\n![Second](attachments/second.png)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_FOUNDRY_ENDPOINT", "https://example.services.ai.azure.com/openai/v1")
    monkeypatch.setenv("TEST_FOUNDRY_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output_text": json.dumps({"caption": "Selected"})})

    config = load_config(
        cli_overrides={
            "paths": {"reports": str(tmp_path / "reports"), "runs": str(tmp_path / "runs")},
            "llm": {
                "provider": "none",
                "azure_ai_foundry": {
                    "endpoint_env": "TEST_FOUNDRY_ENDPOINT",
                    "api_key_env": "TEST_FOUNDRY_KEY",
                    "deployment_env": "",
                },
            },
            "visual_extraction": {
                "provider": "azure-ai-foundry",
                "model": "gpt-5.4-mini",
                "max_images_per_page": 1,
            },
        }
    )
    result = extract_visuals_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=False,
        llm_transport=httpx.MockTransport(handler),
    )

    assert result.exit_code == 0
    assert result.summary.counts["visual_omitted_inventory_records"] == 1
    omitted_path = next((tmp_path / "runs").glob("*/visual_omitted_images.jsonl"))
    omitted = [json.loads(line) for line in omitted_path.read_text(encoding="utf-8").splitlines()]
    assert omitted[0]["page_id"] == "123"
    assert omitted[0]["source"] == str(second_path)
    assert omitted[0]["omitted_reason"] == "page_cap"
    assert omitted[0]["content_sha256"]
    assert omitted[0]["selection_score"] >= 0


def test_extract_visuals_applies_report_page_cap(tiny_cache: Path, tmp_path: Path) -> None:
    manifest_path = tiny_cache / "manifest.jsonl"
    manifest = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines()]
    manifest[0]["title"] = "Weekly Operations Report"
    manifest_path.write_text(
        "\n".join(json.dumps(row) for row in manifest) + "\n", encoding="utf-8"
    )
    metadata_path = tiny_cache / "pages" / "123" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["title"] = "Weekly Operations Report"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    for index in range(3):
        (tiny_cache / "pages" / "123" / "attachments" / f"chart-{index}.png").write_bytes(
            generate_probe_png()
        )
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + "\n".join(f"![Chart {index}](attachments/chart-{index}.png)" for index in range(3)),
        encoding="utf-8",
    )
    config = load_config(
        cli_overrides={
            "paths": {"reports": str(tmp_path / "reports"), "runs": str(tmp_path / "runs")},
            "llm": {"provider": "none"},
            "visual_extraction": {"max_images_per_page": 20, "report_page_max_images": 2},
        }
    )
    result = extract_visuals_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=True,
    )

    assert result.exit_code == 0
    assert result.summary.counts["visual_images_discovered"] == 3
    assert result.summary.counts["visual_images_considered"] == 2
    assert result.summary.counts["visual_pages_adaptive_capped"] == 1


def test_extract_visuals_samples_representative_visual_groups(
    tiny_cache: Path, tmp_path: Path
) -> None:
    metadata_path = tiny_cache / "pages" / "123" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["title"] = "Weekly Operations Report"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    for index in range(5):
        (tiny_cache / "pages" / "123" / "attachments" / f"nginx-chart-{index}.png").write_bytes(
            generate_probe_png()
        )
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + "\n"
        + "\n".join(
            f"![Nginx dashboard chart {index}](attachments/nginx-chart-{index}.png)"
            for index in range(5)
        ),
        encoding="utf-8",
    )
    config = load_config(
        cli_overrides={
            "paths": {"reports": str(tmp_path / "reports"), "runs": str(tmp_path / "runs")},
            "llm": {"provider": "none"},
            "visual_extraction": {
                "max_images_per_page": 20,
                "report_page_max_images": 20,
                "max_images_per_representative_group": 2,
            },
        }
    )
    result = extract_visuals_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=True,
    )

    assert result.exit_code == 0
    assert result.summary.counts["visual_images_discovered"] == 5
    assert result.summary.counts["visual_images_considered"] == 2
    assert result.summary.counts["visual_images_omitted_by_grouping"] == 3
    assert result.summary.counts["visual_images_omitted_by_page_cap"] == 0


def test_extract_visuals_skips_obvious_low_value_images(
    tiny_cache: Path, tmp_path: Path, monkeypatch
) -> None:
    logo_path = tiny_cache / "pages" / "123" / "attachments" / "product-logo.png"
    logo_path.write_bytes(generate_probe_png())
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8") + "\n![Logo](attachments/product-logo.png)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_FOUNDRY_ENDPOINT", "https://example.services.ai.azure.com/openai/v1")
    monkeypatch.setenv("TEST_FOUNDRY_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("low-value images should not be sent to the provider")

    config = load_config(
        cli_overrides={
            "paths": {"reports": str(tmp_path / "reports"), "runs": str(tmp_path / "runs")},
            "llm": {
                "provider": "none",
                "azure_ai_foundry": {
                    "endpoint_env": "TEST_FOUNDRY_ENDPOINT",
                    "api_key_env": "TEST_FOUNDRY_KEY",
                    "deployment_env": "",
                },
            },
            "visual_extraction": {"provider": "azure-ai-foundry", "model": "gpt-5.4-mini"},
        }
    )
    result = extract_visuals_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=False,
        llm_transport=httpx.MockTransport(handler),
    )

    assert result.exit_code == 0
    assert result.summary.counts["llm_calls"] == 0
    artifact = json.loads((tiny_cache / "pages" / "123" / "visual_extraction.json").read_text())
    assert artifact["status"] == "skipped"
    assert artifact["images"][0]["status"] == "skipped"
    assert artifact["images"][0]["error_type"] == "low_value_visual"


def test_onyx_visual_section_dedupes_and_truncates(tiny_cache: Path, tmp_path: Path) -> None:
    visual_path = tiny_cache / "pages" / "123" / "visual_extraction.json"
    long_ocr = "visible text " * 40
    visual_path.write_text(
        json.dumps(
            {
                "schema_version": "mimir-wiki/v1",
                "run_id": "visual-run",
                "dataset_name": "tiny",
                "generated_at": "2026-06-17T00:00:00Z",
                "generator": "mimir-wiki",
                "source_system": "confluence",
                "document_id": "confluence:IDENTITY:123",
                "page_id": "123",
                "space_key": "IDENTITY",
                "source_updated_at": "2026-05-01T12:45:00Z",
                "source_content_hash": "sha256:test",
                "extracted_at": "2026-06-17T00:00:00Z",
                "status": "complete",
                "method": "multimodal_ocr",
                "provider": "mock",
                "model": "mock-model",
                "prompt_version": "visual-ocr-v1",
                "image_count": 2,
                "images_succeeded": 2,
                "images_failed": 0,
                "images_skipped": 0,
                "images": [
                    {
                        "image_id": "image-001",
                        "source": "attachments/one.png",
                        "source_kind": "file",
                        "content_sha256": "same-hash",
                        "status": "success",
                        "ocr_text": long_ocr,
                        "caption": "First visual",
                    },
                    {
                        "image_id": "image-002",
                        "source": "attachments/two.png",
                        "source_kind": "file",
                        "content_sha256": "same-hash",
                        "status": "success",
                        "ocr_text": "duplicate",
                        "caption": "Duplicate visual",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    config = load_config(
        cli_overrides={
            "paths": {"reports": str(tmp_path / "reports"), "runs": str(tmp_path / "runs")},
            "llm": {"provider": "none"},
            "onyx_poc": {"max_visual_ocr_chars": 30},
        }
    )
    bundle = CacheReader(tiny_cache).iter_pages()[0]

    section = render_visual_extraction(bundle, config)

    assert "image-001" in section
    assert "image-002" not in section
    assert "[truncated]" in section
    assert "Duplicate visual images omitted from this section: 1" in section


def test_oversized_table_rows_get_usability_review_flags(tiny_cache: Path, tmp_path: Path) -> None:
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    long_cell = "step " * 260
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + f"\n| Scenario | Investigation |\n| --- | --- |\n| Long row | {long_cell} |\n",
        encoding="utf-8",
    )
    text_path = tiny_cache / "pages" / "123" / "text.txt"
    text_path.write_text(
        text_path.read_text(encoding="utf-8") + " Long table row.", encoding="utf-8"
    )
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )
    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)
    assert result.exit_code == 0
    enrichment = json.loads((tiny_cache / "pages" / "123" / "enrichment.json").read_text())
    assert "source_contains_oversized_table_rows" in enrichment["review_flags"]
    assert "manual_review_required" in enrichment["review_flags"]


def test_linked_failover_procedure_suppresses_missing_backout_warning(
    tiny_cache: Path, tmp_path: Path
) -> None:
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + "\nFailover procedures are documented and available [here](https://example.com/failover).\n",
        encoding="utf-8",
    )
    text_path = tiny_cache / "pages" / "123" / "text.txt"
    text_path.write_text(
        text_path.read_text(encoding="utf-8")
        + " Failover procedures are documented and available here.",
        encoding="utf-8",
    )
    links_path = tiny_cache / "pages" / "123" / "links.json"
    links = json.loads(links_path.read_text(encoding="utf-8"))
    links["links"].append(
        {
            "type": "external_url",
            "href": "https://example.com/failover",
            "text": "here",
            "crawlable": False,
            "target_page_id": None,
            "target_space_key": None,
            "target_title": None,
        }
    )
    links_path.write_text(json.dumps(links), encoding="utf-8")
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )
    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)
    assert result.exit_code == 0
    enrichment = json.loads((tiny_cache / "pages" / "123" / "enrichment.json").read_text())
    assert "linked_procedure_not_expanded" in enrichment["warnings"]
    assert "missing_backout_steps" not in enrichment["warnings"]


def test_missing_attachment_links_get_review_flags(tiny_cache: Path, tmp_path: Path) -> None:
    metadata_path = tiny_cache / "pages" / "123" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["title"] = "SCIM Admin API - Open API Specification"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    links_path = tiny_cache / "pages" / "123" / "links.json"
    links = json.loads(links_path.read_text(encoding="utf-8"))
    links["links"].append(
        {
            "type": "confluence_attachment",
            "href": "https://confluence.example.com/download/attachments/123/swagger.yaml?api=v2",
            "text": "swagger.yaml",
            "crawlable": False,
            "target_page_id": None,
            "target_space_key": None,
            "target_title": None,
        }
    )
    links_path.write_text(json.dumps(links), encoding="utf-8")
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )
    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)
    assert result.exit_code == 0
    enrichment = json.loads((tiny_cache / "pages" / "123" / "enrichment.json").read_text())
    assert enrichment["document_type"] == "reference"
    assert enrichment["document_subtype"] == "api_specification"
    assert "attachment_content_missing" in enrichment["review_flags"]
    assert "attachment_content_review_recommended" in enrichment["review_flags"]
    document_row = json.loads((tmp_path / "knowledge" / "document_index.jsonl").read_text())
    assert document_row["attachment_count"] == 1
    onyx_file = next((tmp_path / "dist" / "onyx-enriched" / "tiny" / "IDENTITY").glob("*.md"))
    content = onyx_file.read_text(encoding="utf-8")
    assert "Missing attachment content count: 1" in content


def test_customer_case_content_gets_restricted_review_flags(
    tiny_cache: Path, tmp_path: Path
) -> None:
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + "\nBain user jane.customer@bain.com (GEDTC-1107515) failed in DataDog logs.\n",
        encoding="utf-8",
    )
    text_path = tiny_cache / "pages" / "123" / "text.txt"
    text_path.write_text(
        text_path.read_text(encoding="utf-8")
        + " Bain user jane.customer@bain.com GEDTC-1107515 failed in DataDog logs.",
        encoding="utf-8",
    )
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )
    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)
    assert result.exit_code == 0
    enrichment = json.loads((tiny_cache / "pages" / "123" / "enrichment.json").read_text())
    assert enrichment["audience"] == "restricted_internal"
    assert enrichment["sensitivity"] == "customer_confidential"
    assert "contains_customer_case_data" in enrichment["review_flags"]
    assert "requires_restricted_audience" in enrichment["review_flags"]
    onyx_file = next((tmp_path / "dist" / "onyx-enriched" / "tiny" / "IDENTITY").glob("*.md"))
    metadata = json.loads(onyx_file.read_text(encoding="utf-8").splitlines()[0].split("=", 1)[1])
    assert metadata["audience"] == "restricted_internal"
    assert metadata["sensitivity"] == "customer_confidential"
    document_row = json.loads((tmp_path / "knowledge" / "document_index.jsonl").read_text())
    assert document_row["audience"] == "restricted_internal"


def test_draft_future_runbook_gets_not_for_execution_flags(
    tiny_cache: Path, tmp_path: Path
) -> None:
    metadata_path = tiny_cache / "pages" / "123" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["title"] = "Draft [PROD] SCIM API 9.9.0 Installation Guide"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        "# Draft [PROD] SCIM API 9.9.0 Installation Guide\n\n"
        "Change Request: to-be-modified\n\n"
        "| Environment | Install Date |\n| --- | --- |\n| Production | 31 Dec 2099 |\n",
        encoding="utf-8",
    )
    text_path = tiny_cache / "pages" / "123" / "text.txt"
    text_path.write_text(
        "Draft PROD SCIM API 9.9.0 Installation Guide Change Request to-be-modified "
        "Production 31 Dec 2099",
        encoding="utf-8",
    )
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )
    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)
    assert result.exit_code == 0
    enrichment = json.loads((tiny_cache / "pages" / "123" / "enrichment.json").read_text())
    assert "draft" in enrichment["review_flags"]
    assert "contains_unresolved_items" in enrichment["review_flags"]
    assert "future_dated" in enrichment["review_flags"]
    assert "manual_review_required" in enrichment["review_flags"]
    assert "not_for_execution" in enrichment["review_flags"]
    assert "not_for_execution_until_verified" in enrichment["review_flags"]
    assert "versioned_operational_document" in enrichment["review_flags"]


def test_enrich_uses_page_workers_for_multiple_pages(tiny_cache: Path, tmp_path: Path) -> None:
    page_123 = tiny_cache / "pages" / "123"
    page_456 = tiny_cache / "pages" / "456"
    shutil.copytree(page_123, page_456)
    manifest_path = tiny_cache / "manifest.jsonl"
    rows = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines()]
    rows.append(
        {
            **rows[0],
            "page_id": "456",
            "path": "pages/456",
            "markdown_path": "pages/456/clean.md",
            "title": "ForgeRock Support Runbook Copy",
        }
    )
    manifest_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    metadata_path = page_456 / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["page_id"] = "456"
    metadata["title"] = "ForgeRock Support Runbook Copy"
    metadata["content_hashes"]["markdown_sha256"] = "e" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    links_path = page_456 / "links.json"
    links = json.loads(links_path.read_text(encoding="utf-8"))
    links["page_id"] = "456"
    links_path.write_text(json.dumps(links), encoding="utf-8")
    summary_path = tiny_cache / "manifest.summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["total_pages"] = 2
    summary["spaces"]["IDENTITY"] = 2
    summary["statuses"]["success"] = 2
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    events = []
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
            "processing": {"page_workers": 2},
        }
    )
    result = enrich_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=False,
        event_callback=events.append,
    )
    assert result.exit_code == 0
    assert result.summary.counts["pages_processed"] == 2
    assert sum(1 for event in events if event["event"] == "artifact_written") >= 10


def test_enrich_cancels_pending_pages_and_writes_partial_run(
    tiny_cache: Path, tmp_path: Path, monkeypatch
) -> None:
    page_123 = tiny_cache / "pages" / "123"
    page_456 = tiny_cache / "pages" / "456"
    shutil.copytree(page_123, page_456)
    manifest_path = tiny_cache / "manifest.jsonl"
    first_row = json.loads(manifest_path.read_text(encoding="utf-8").splitlines()[0])
    second_row = {
        **first_row,
        "page_id": "456",
        "path": "pages/456",
        "markdown_path": "pages/456/clean.md",
        "title": "Cancelled Page",
    }
    manifest_path.write_text(
        json.dumps(first_row) + "\n" + json.dumps(second_row) + "\n", encoding="utf-8"
    )
    metadata_path = page_456 / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["page_id"] = "456"
    metadata["title"] = "Cancelled Page"
    metadata["content_hashes"]["markdown_sha256"] = "f" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    links_path = page_456 / "links.json"
    links = json.loads(links_path.read_text(encoding="utf-8"))
    links["page_id"] = "456"
    links_path.write_text(json.dumps(links), encoding="utf-8")
    summary_path = tiny_cache / "manifest.summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["total_pages"] = 2
    summary["spaces"]["IDENTITY"] = 2
    summary["statuses"]["success"] = 2
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    def interrupting_process_page(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "_process_page", interrupting_process_page)
    events = []
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
            "processing": {"page_workers": 1},
        }
    )
    result = pipeline.enrich_command(
        config=config,
        cache_path=tiny_cache,
        profile=None,
        dry_run=False,
        event_callback=events.append,
    )
    assert result.exit_code == 3
    assert result.summary.status == "partial_success"
    assert result.summary.counts["pages_cancelled"] == 2
    assert any(warning.warning_type == "run_cancelled" for warning in result.warnings)
    assert any(event["event"] == "run_cancelled" for event in events)
    assert any((tmp_path / "runs").glob("*/summary.json"))


def test_enrich_reports_attachments_and_rca_historical(tiny_cache: Path, tmp_path: Path) -> None:
    (tiny_cache / "pages" / "123" / "attachments" / "evidence.pdf").write_text(
        "x", encoding="utf-8"
    )
    metadata_path = tiny_cache / "pages" / "123" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["title"] = "RCA - ForgeRock Login Outage"
    metadata["labels"] = ["rca", "incident", "deprecated"]
    metadata["status"] = "archived"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + "\nImpact: login failures\nRoot cause: LDAP timeout\nAction items: improve alerting\n",
        encoding="utf-8",
    )
    text_path = tiny_cache / "pages" / "123" / "text.txt"
    text_path.write_text(
        text_path.read_text(encoding="utf-8")
        + " RCA root cause LDAP timeout impact login failures action items improve alerting",
        encoding="utf-8",
    )
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
        }
    )
    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)
    assert result.exit_code == 0
    enrichment = json.loads(
        (tiny_cache / "pages" / "123" / "enrichment.json").read_text(encoding="utf-8")
    )
    assert enrichment["document_type"] == "rca"
    assert enrichment["currentness"] == "deprecated"
    assert enrichment["historical"] is True
    assert "attachments_present_not_parsed" in enrichment["warnings"]
    assert "had_root_cause" in {fact["predicate"] for fact in enrichment["candidate_facts"]}


def test_changed_only_skips_unchanged_pages(tiny_cache: Path, tmp_path: Path) -> None:
    overrides = {
        "paths": {
            "knowledge": str(tmp_path / "knowledge"),
            "reports": str(tmp_path / "reports"),
            "runs": str(tmp_path / "runs"),
            "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
        },
        "llm": {"provider": "none"},
    }
    config = load_config(cli_overrides=overrides)
    first = enrich_command(
        config=config, cache_path=tiny_cache, profile=None, dry_run=False, changed_only=True
    )
    assert first.summary.counts["pages_processed"] == 1
    second = enrich_command(
        config=config, cache_path=tiny_cache, profile=None, dry_run=False, changed_only=True
    )
    assert second.summary.counts["pages_processed"] == 0
    assert second.summary.counts["pages_skipped_unchanged"] == 1
    config_changed = load_config(
        cli_overrides={**overrides, "llm": {"provider": "none", "prompt_version": "enrichment-v2"}}
    )
    third = enrich_command(
        config=config_changed, cache_path=tiny_cache, profile=None, dry_run=False, changed_only=True
    )
    assert third.summary.counts["pages_processed"] == 1


def test_dry_run_does_not_write(tiny_cache: Path, tmp_path: Path) -> None:
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
            },
            "llm": {"provider": "none"},
        }
    )
    result = validate_cache_command(
        config=config, cache_path=tiny_cache, profile=None, dry_run=True
    )
    assert result.exit_code == 0
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "reports").exists()


def test_redaction_fail_records_page_failure(tiny_cache: Path, tmp_path: Path) -> None:
    clean_md = tiny_cache / "pages" / "123" / "clean.md"
    clean_md.write_text(
        clean_md.read_text(encoding="utf-8")
        + "\nToken: "
        + "ghp_"
        + "abcdefghijklmnopqrstuvwxyz123456\n",
        encoding="utf-8",
    )
    config = load_config(
        cli_overrides={
            "paths": {
                "knowledge": str(tmp_path / "knowledge"),
                "reports": str(tmp_path / "reports"),
                "runs": str(tmp_path / "runs"),
                "dist_onyx_enriched": str(tmp_path / "dist" / "onyx-enriched"),
            },
            "llm": {"provider": "none"},
            "redaction": {"enabled": True, "action": "fail"},
        }
    )
    result = enrich_command(config=config, cache_path=tiny_cache, profile=None, dry_run=False)
    assert result.exit_code == 3
    assert result.failures[0].stage == "enrich"
    assert "Redaction policy failed" in result.failures[0].message
