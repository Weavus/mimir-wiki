from __future__ import annotations

import os
from pathlib import Path

import pytest

from mimir_wiki.config import load_config
from mimir_wiki.pipeline import enrich_command, validate_cache_command

pytestmark = pytest.mark.smoke


def test_real_cache_smoke(tmp_path: Path) -> None:
    if os.environ.get("MIMIR_WIKI_RUN_SMOKE") != "1":
        pytest.skip("Set MIMIR_WIKI_RUN_SMOKE=1 to run local real-cache smoke tests")
    repo_root = Path(__file__).resolve().parents[1]
    cache = repo_root / "cache" / "carel3support"
    if not cache.exists():
        pytest.skip(f"Real cache not present: {cache}")
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
    validation = validate_cache_command(
        config=config, cache_path=cache, profile=None, dry_run=False, limit=5
    )
    assert validation.exit_code == 0
    enrichment = enrich_command(
        config=config, cache_path=cache, profile=None, dry_run=False, limit=5
    )
    assert enrichment.exit_code == 0
    assert enrichment.summary.counts["pages_processed"] == 5
