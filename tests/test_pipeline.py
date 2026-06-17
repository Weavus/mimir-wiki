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
    onyx_files = list((tmp_path / "dist" / "onyx-enriched" / "tiny" / "IDENTITY").glob("*.md"))
    assert len(onyx_files) == 1
    first_line = onyx_files[0].read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#ONYX_METADATA=")
    metadata = json.loads(first_line.removeprefix("#ONYX_METADATA="))
    assert metadata["link"] == "https://confluence.example.com/pages/viewpage.action?pageId=123"
    assert metadata["file_display_name"] == "ForgeRock Support Runbook"
    assert metadata["doc_updated_at"] == "2026-05-01T12:45:00Z"
    assert "run_id" not in metadata
    assert (tmp_path / "reports" / "enrichment_summary.md").exists()
    assert (tmp_path / "reports" / "duplicate_candidates.md").exists()
    assert (tmp_path / "reports" / "llm_usage.md").exists()
    assert any((tmp_path / "runs").glob("*/summary.json"))


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
