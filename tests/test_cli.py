from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from mimir_wiki.cli import app


def test_cli_json_mode_outputs_machine_summary(tiny_cache: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    config_file = tmp_path / "mimir-wiki.yaml"
    config_file.write_text(
        f"""
paths:
  reports: {tmp_path / "reports"}
  runs: {tmp_path / "runs"}
""",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "enrich",
            "--config",
            str(config_file),
            "--cache",
            str(tiny_cache),
            "--out",
            str(tmp_path / "knowledge"),
            "--onyx-out",
            str(tmp_path / "dist"),
            "--json",
            "--quiet",
            "--provider",
            "none",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "enrich"
    assert payload["status"] == "success"
    assert payload["counts"]["pages_processed"] == 1


def test_cli_log_file_writes_jsonl(tiny_cache: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    log_file = tmp_path / "logs" / "mimir-wiki.jsonl"
    config_file = tmp_path / "mimir-wiki.yaml"
    config_file.write_text(
        f"""
paths:
  runs: {tmp_path / "runs"}
""",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "validate-cache",
            "--config",
            str(config_file),
            "--cache",
            str(tiny_cache),
            "--out",
            str(tmp_path / "reports"),
            "--log-file",
            str(log_file),
            "--quiet",
        ],
    )
    assert result.exit_code == 0, result.output
    assert log_file.exists()
    assert "command_finished" in log_file.read_text(encoding="utf-8")


def test_cli_enrich_log_file_includes_page_events(tiny_cache: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    log_file = tmp_path / "logs" / "enrich.jsonl"
    config_file = tmp_path / "mimir-wiki.yaml"
    config_file.write_text(
        f"""
paths:
  knowledge: {tmp_path / "knowledge"}
  reports: {tmp_path / "reports"}
  runs: {tmp_path / "runs"}
  dist_onyx_enriched: {tmp_path / "dist"}
""",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "enrich",
            "--config",
            str(config_file),
            "--cache",
            str(tiny_cache),
            "--provider",
            "none",
            "--log-file",
            str(log_file),
            "--quiet",
        ],
    )
    assert result.exit_code == 0, result.output
    log_text = log_file.read_text(encoding="utf-8")
    assert "page_started" in log_text
    assert "page_finished" in log_text
    assert "artifact_written" in log_text


def test_cli_enrich_uses_config_provider_when_provider_flag_omitted(
    tiny_cache: Path, tmp_path: Path
) -> None:
    runner = CliRunner()
    config_file = tmp_path / "mimir-wiki.yaml"
    config_file.write_text(
        f"""
paths:
  knowledge: {tmp_path / "knowledge"}
  reports: {tmp_path / "reports"}
  runs: {tmp_path / "runs"}
  dist_onyx_enriched: {tmp_path / "dist"}
llm:
  provider: azure-ai-foundry
  model: gpt-5.5
  azure_ai_foundry:
    endpoint_env: TEST_MISSING_FOUNDRY_ENDPOINT
    api_key_env: TEST_MISSING_FOUNDRY_KEY
    deployment_env: TEST_MISSING_FOUNDRY_DEPLOYMENT
features:
  llm:
    enabled: true
    tasks:
      summary: true
""",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "enrich",
            "--config",
            str(config_file),
            "--cache",
            str(tiny_cache),
            "--json",
            "--quiet",
            "--limit",
            "1",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "missing_credentials" in result.output
