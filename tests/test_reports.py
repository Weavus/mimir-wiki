from __future__ import annotations

from pathlib import Path

from mimir_wiki.reports import write_duplicate_candidates_report, write_llm_usage_report
from mimir_wiki.schemas import DocumentIndexRow, LLMUsage
from mimir_wiki.writers.artifacts import aggregate_concept_rows, aggregate_theme_rows


def _row(page_id: str, title: str, text_simhash: str, heading_simhash: str) -> DocumentIndexRow:
    return DocumentIndexRow(
        run_id="run-1",
        dataset_name="tiny",
        generated_at="2026-06-17T00:00:00Z",
        document_id=f"confluence:SPACE:{page_id}",
        page_id=page_id,
        space_key="SPACE",
        source_content_hash=f"sha256:{page_id}",
        title=title,
        document_type="runbook",
        document_type_confidence=0.8,
        text_simhash=text_simhash,
        heading_simhash=heading_simhash,
        heading_count=3,
    )


def test_duplicate_report_includes_body_and_heading_simhash(tmp_path: Path) -> None:
    path = write_duplicate_candidates_report(
        out_dir=tmp_path,
        document_rows=[
            _row("1", "ForgeRock Support Runbook", "0000000000000000", "000000000000000f"),
            _row("2", "ForgeRock Support Guide", "0000000000000001", "000000000000000e"),
        ],
    )
    content = path.read_text(encoding="utf-8")
    assert "body_simhash" in content
    assert "heading_simhash" in content


def test_llm_usage_report_includes_cache_hit_rate(tmp_path: Path) -> None:
    path = write_llm_usage_report(
        out_dir=tmp_path,
        usage=[
            LLMUsage(
                run_id="run-1",
                dataset_name="tiny",
                generated_at="2026-06-17T00:00:00Z",
                document_id="confluence:SPACE:1",
                page_id="1",
                space_key="SPACE",
                source_content_hash="sha256:a",
                task="summary",
                provider="mock",
                model="mock-model",
                prompt_version="summary-v1",
                cached=False,
            ),
            LLMUsage(
                run_id="run-1",
                dataset_name="tiny",
                generated_at="2026-06-17T00:00:00Z",
                document_id="confluence:SPACE:2",
                page_id="2",
                space_key="SPACE",
                source_content_hash="sha256:b",
                task="summary",
                provider="mock",
                model="mock-model",
                prompt_version="summary-v1",
                cached=True,
            ),
        ],
    )
    content = path.read_text(encoding="utf-8")
    assert "Cache hit rate" in content
    assert "50%" in content


def test_aggregate_taxonomy_drops_one_off_single_word_noise() -> None:
    from mimir_wiki.schemas import Enrichment, EnrichmentSignature, OnyxMetadata, Quality

    def enrichment(page_id: str, themes: list[str], concepts: list[str]) -> Enrichment:
        return Enrichment(
            run_id="run-1",
            generated_at="2026-06-17T00:00:00Z",
            dataset_name="tiny",
            document_id=f"confluence:SPACE:{page_id}",
            page_id=page_id,
            space_key="SPACE",
            source_content_hash="sha256:a",
            enriched_at="2026-06-17T00:00:00Z",
            ONYX_METADATA=OnyxMetadata(
                link="https://example.com",
                file_display_name="Doc",
                doc_updated_at="2026-06-17T00:00:00Z",
            ),
            document_type="reference",
            document_type_confidence=0.8,
            short_summary="summary",
            detailed_summary="details",
            themes=themes,
            concepts=concepts,
            quality=Quality(
                freshness_score=50,
                authority_score=50,
                completeness_score=50,
                operational_value_score=50,
                ownership_clarity_score=0,
                staleness_risk_score=50,
                contradiction_risk_score=10,
                overall_score=50,
            ),
            quality_band="fair",
            confidence=0.7,
            signatures=EnrichmentSignature(
                source_content_hash="sha256:a",
                prompt_version="v1",
                provider="none",
                model_or_deployment="none",
                tasks=[],
                enrichment_config_hash="hash",
            ),
        )

    enrichments = [
        enrichment("1", ["create", "datadog", "create user"], ["given", "k6"]),
        enrichment("2", ["create"], ["given"]),
    ]
    themes = aggregate_theme_rows(
        enrichments, generated_at="2026-06-17T00:00:00Z", run_id="run-1", dataset_name="tiny"
    )
    concepts = aggregate_concept_rows(
        enrichments, generated_at="2026-06-17T00:00:00Z", run_id="run-1", dataset_name="tiny"
    )
    assert "create" not in {row.normalized_theme for row in themes}
    assert "given" not in {row.normalized_concept for row in concepts}
    assert "datadog" in {row.normalized_theme for row in themes}
    assert "k6" in {row.normalized_concept for row in concepts}
    assert "create user" in {row.normalized_theme for row in themes}
