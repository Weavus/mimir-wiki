from __future__ import annotations

import json
import shutil
from pathlib import Path

import mimir_wiki.pipeline as pipeline
from mimir_wiki.config import load_config
from mimir_wiki.pipeline import enrich_command, validate_cache_command


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
    predicates = {fact["predicate"] for fact in enrichment["candidate_facts"]}
    assert "owned_by" in predicates
    assert "supported_by" in predicates
    assert "has_diagnostic_step" in predicates
    assert (tmp_path / "knowledge" / "document_index.jsonl").exists()
    assert (tmp_path / "knowledge" / "quality_scores.jsonl").exists()
    assert (tmp_path / "knowledge" / "themes.jsonl").exists()
    assert (tmp_path / "knowledge" / "concepts.jsonl").exists()
    assert (tmp_path / "knowledge" / "candidate_entities.jsonl").exists()
    assert (tmp_path / "knowledge" / "facts.jsonl").exists()
    assert (tmp_path / "knowledge" / "facts.jsonl").read_text(encoding="utf-8").strip()
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
    assert any((tmp_path / "runs").glob("*/summary.json"))


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


def test_oversized_table_rows_get_usability_review_flags(
    tiny_cache: Path, tmp_path: Path
) -> None:
    clean_path = tiny_cache / "pages" / "123" / "clean.md"
    long_cell = "step " * 260
    clean_path.write_text(
        clean_path.read_text(encoding="utf-8")
        + f"\n| Scenario | Investigation |\n| --- | --- |\n| Long row | {long_cell} |\n",
        encoding="utf-8",
    )
    text_path = tiny_cache / "pages" / "123" / "text.txt"
    text_path.write_text(text_path.read_text(encoding="utf-8") + " Long table row.", encoding="utf-8")
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
