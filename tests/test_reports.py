from __future__ import annotations

from pathlib import Path

from mimir_wiki.reports import (
    VisualReportPage,
    write_duplicate_candidates_report,
    write_high_value_subtrees_report,
    write_llm_usage_report,
    write_visual_extraction_report,
)
from mimir_wiki.schemas import (
    DocumentIndexRow,
    Enrichment,
    EnrichmentSignature,
    HierarchyContext,
    LLMUsage,
    OnyxMetadata,
    Quality,
    VisualExtractionArtifact,
    VisualExtractionImage,
)
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


def test_visual_extraction_report_includes_operational_triage_sections(
    tmp_path: Path,
) -> None:
    artifact = VisualExtractionArtifact(
        run_id="visual-run-1",
        dataset_name="tiny",
        generated_at="2026-06-17T00:00:00Z",
        document_id="confluence:SPACE:1",
        page_id="1",
        space_key="SPACE",
        source_content_hash="sha256:a",
        extracted_at="2026-06-17T00:00:00Z",
        status="partial",
        provider="mock",
        model="mock-model",
        image_count=3,
        images_succeeded=2,
        images_failed=1,
        images_skipped=1,
        images=[
            VisualExtractionImage(
                image_id="image-001",
                source="https://cdn.example.com/one.png",
                source_kind="url",
                status="skipped",
                error_type="remote_source_not_in_cache",
            ),
            VisualExtractionImage(
                image_id="image-002",
                source="attachments/fail.png",
                source_kind="file",
                status="failed",
                error_type="invalid_image",
                content_sha256="hash-failed",
            ),
            VisualExtractionImage(
                image_id="image-003",
                source="attachments/low.png",
                source_kind="file",
                status="success",
                confidence=0.42,
                content_sha256="duplicate-hash",
            ),
        ],
    )
    duplicate_artifact = VisualExtractionArtifact(
        run_id="visual-run-1",
        dataset_name="tiny",
        generated_at="2026-06-17T00:00:00Z",
        document_id="confluence:SPACE:2",
        page_id="2",
        space_key="SPACE",
        source_content_hash="sha256:b",
        extracted_at="2026-06-17T00:00:00Z",
        status="complete",
        provider="mock",
        model="mock-model",
        image_count=1,
        images_succeeded=1,
        images=[
            VisualExtractionImage(
                image_id="image-001",
                source="attachments/dupe.png",
                source_kind="file",
                status="success",
                confidence=0.91,
                content_sha256="duplicate-hash",
            )
        ],
    )
    path = write_visual_extraction_report(
        out_dir=tmp_path,
        dataset_name="tiny",
        pages=[
            VisualReportPage(
                artifact=artifact,
                title="Partial page",
                url="https://example.com/1",
                discovered_image_count=5,
            ),
            VisualReportPage(
                artifact=duplicate_artifact,
                title="Complete page",
                discovered_image_count=1,
            ),
        ],
    )

    content = path.read_text(encoding="utf-8")
    assert "# Visual Extraction" in content
    assert "| partial | 1 |" in content
    assert "| success | 2 |" in content
    assert "| SPACE | 1 | 5 | 3 | 2 | Partial page | https://example.com/1 |" in content
    assert "invalid_image" in content
    assert "cdn.example.com" in content
    assert "| 0.42 | SPACE | 1 | image-003 | file | attachments/low.png |" in content
    assert "duplicate-hash" in content


def test_aggregate_taxonomy_drops_one_off_single_word_noise() -> None:
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


def test_high_value_subtrees_report_groups_by_parent(tmp_path: Path) -> None:
    enrichment = Enrichment(
        run_id="run-1",
        generated_at="2026-06-17T00:00:00Z",
        dataset_name="tiny",
        document_id="confluence:SPACE:1",
        page_id="1",
        space_key="SPACE",
        source_content_hash="sha256:a",
        enriched_at="2026-06-17T00:00:00Z",
        ONYX_METADATA=OnyxMetadata(
            link="https://example.com",
            file_display_name="Doc",
            doc_updated_at="2026-06-17T00:00:00Z",
        ),
        document_type="runbook",
        document_type_confidence=0.8,
        short_summary="summary",
        detailed_summary="details",
        hierarchy=HierarchyContext(
            parent_title="IAM SCIM API - Runbook", page_role="runbook_detail"
        ),
        quality=Quality(
            freshness_score=80,
            authority_score=90,
            completeness_score=70,
            operational_value_score=90,
            ownership_clarity_score=0,
            staleness_risk_score=20,
            contradiction_risk_score=10,
            overall_score=75,
        ),
        quality_band="good",
        confidence=0.8,
        signatures=EnrichmentSignature(
            source_content_hash="sha256:a",
            prompt_version="v1",
            provider="none",
            model_or_deployment="none",
            tasks=[],
            enrichment_config_hash="hash",
        ),
    )
    path = write_high_value_subtrees_report(out_dir=tmp_path, enrichments=[enrichment])
    content = path.read_text(encoding="utf-8")
    assert "IAM SCIM API - Runbook" in content
    assert "runbook_detail" in content
