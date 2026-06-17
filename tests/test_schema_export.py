from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from mimir_wiki.cli import app


def test_export_schema_command_writes_json_schemas(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["export-schema", "--out", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "enrichment.schema.json").exists()
    assert (tmp_path / "document_index_row.schema.json").exists()
