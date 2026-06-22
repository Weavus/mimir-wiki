from __future__ import annotations

from pathlib import Path

from mimir_wiki.reports import (
    VisualReportPage,
    audit_onyx_exports,
    write_duplicate_candidates_report,
    write_high_value_sources_report,
    write_high_value_subtrees_report,
    write_llm_usage_report,
    write_onyx_export_integrity_report,
    write_onyx_export_risk_report,
    write_page_failures_report,
    write_review_queue_report,
    write_visual_extraction_report,
)
from mimir_wiki.schemas import (
    DocumentIndexRow,
    Enrichment,
    EnrichmentSignature,
    HierarchyContext,
    LLMUsage,
    OnyxMetadata,
    PageFailure,
    Quality,
    QualityScoreRow,
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


def test_duplicate_report_includes_cluster_recommendation(tmp_path: Path) -> None:
    path = write_duplicate_candidates_report(
        out_dir=tmp_path,
        document_rows=[
            _row("1", "ForgeRock Support Runbook", "0000000000000000", "000000000000000f"),
            _row("2", "ForgeRock Support Runbook", "0000000000000001", "000000000000000e"),
        ],
    )
    content = path.read_text(encoding="utf-8")
    assert "## Clusters" in content
    assert "Recommended keeper" in content
    assert "SPACE:1" in content


def test_review_queue_report_prioritizes_manual_review(tmp_path: Path) -> None:
    row = _row("1", "Restricted Runbook", "0000000000000001", "0000000000000001")
    row.review_flags = ["manual_review_required", "visual_content_missing"]
    row.sensitivity = "restricted"
    quality = QualityScoreRow(
        run_id="run-1",
        dataset_name="tiny",
        generated_at="2026-06-17T00:00:00Z",
        document_id=row.document_id,
        page_id=row.page_id,
        space_key=row.space_key,
        source_content_hash=row.source_content_hash,
        quality_score=55,
        quality_band="fair",
        dimensions={},
    )

    path = write_review_queue_report(out_dir=tmp_path, document_rows=[row], quality_rows=[quality])
    content = path.read_text(encoding="utf-8")
    assert "Restricted Runbook" in content
    assert "manual_review_required" in content
    assert "sensitive_content" in content


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


def test_page_failures_report_summarizes_failures(tmp_path: Path) -> None:
    path = write_page_failures_report(
        out_dir=tmp_path,
        failures=[
            PageFailure(
                run_id="run-1",
                dataset_name="tiny",
                generated_at="2026-06-17T00:00:00Z",
                document_id="confluence:SPACE:1",
                page_id="1",
                space_key="SPACE",
                source_content_hash="sha256:a",
                stage="enrich",
                error_type="SSLError",
                message="ssl failed",
            ),
            PageFailure(
                run_id="run-1",
                dataset_name="tiny",
                generated_at="2026-06-17T00:00:00Z",
                document_id="confluence:SPACE:2",
                page_id="2",
                space_key="SPACE",
                source_content_hash="sha256:b",
                stage="enrich",
                error_type="SSLError",
                message="ssl failed",
            ),
        ],
    )
    content = path.read_text(encoding="utf-8")
    assert "## Summary" in content
    assert "| enrich | SSLError | 2 |" in content
    assert "## Details" in content


def test_high_value_sources_penalizes_dated_operational_records(tmp_path: Path) -> None:
    canonical = _row("1", "ForgeRock Support Runbook", "0000000000000001", "0000000000000001")
    handover = _row(
        "2",
        "2026-05-14 CIAM - Ping Service Review Meeting",
        "0000000000000002",
        "0000000000000002",
    )
    handover.document_type = "meeting_notes"
    rows = [handover, canonical]
    quality_rows = [
        QualityScoreRow(
            run_id="run-1",
            dataset_name="tiny",
            generated_at="2026-06-17T00:00:00Z",
            document_id=handover.document_id,
            page_id=handover.page_id,
            space_key=handover.space_key,
            source_content_hash=handover.source_content_hash,
            quality_score=95,
            quality_band="excellent",
            dimensions={},
        ),
        QualityScoreRow(
            run_id="run-1",
            dataset_name="tiny",
            generated_at="2026-06-17T00:00:00Z",
            document_id=canonical.document_id,
            page_id=canonical.page_id,
            space_key=canonical.space_key,
            source_content_hash=canonical.source_content_hash,
            quality_score=80,
            quality_band="good",
            dimensions={},
        ),
    ]

    path = write_high_value_sources_report(
        out_dir=tmp_path, document_rows=rows, quality_rows=quality_rows
    )
    content = path.read_text(encoding="utf-8")
    assert "Priority" in content
    assert content.index("ForgeRock Support Runbook") < content.index("Service Review Meeting")
    assert "dated_operational_record" in content


def test_onyx_export_risk_report_flags_restricted_content(tmp_path: Path) -> None:
    risky = _row("1", "Customer Incident Runbook", "0000000000000001", "0000000000000001")
    risky.audience = "restricted_internal"
    risky.sensitivity = "customer_confidential"
    risky.review_flags = ["contains_customer_case_data", "requires_restricted_audience"]
    safe = _row("2", "Public Overview", "0000000000000002", "0000000000000002")

    path = write_onyx_export_risk_report(out_dir=tmp_path, document_rows=[safe, risky])
    content = path.read_text(encoding="utf-8")

    assert "Customer Incident Runbook" in content
    assert "customer_confidential" in content
    assert "contains_customer_case_data" in content
    assert "Public Overview" not in content


def test_onyx_export_integrity_report_flags_and_reconciles_files(tmp_path: Path) -> None:
    current = _row("1", "Current Runbook", "0000000000000001", "0000000000000001")
    dataset_dir = tmp_path / "dist" / "tiny" / "SPACE"
    dataset_dir.mkdir(parents=True)
    keep = dataset_dir / "1-current-runbook.md"
    duplicate = dataset_dir / "1-old-current-runbook.md"
    stale = dataset_dir / "999-stale-runbook.md"
    unparseable = dataset_dir / "not-a-page.md"
    for path in (keep, duplicate, stale, unparseable):
        path.write_text("# doc", encoding="utf-8")

    report_path = write_onyx_export_integrity_report(
        out_dir=tmp_path / "reports",
        onyx_root=tmp_path / "dist",
        dataset_name="tiny",
        document_rows=[current],
    )
    content = report_path.read_text(encoding="utf-8")
    assert "stale" in content
    assert "duplicate" in content
    assert "unparseable" in content

    audit = audit_onyx_exports(
        onyx_root=tmp_path / "dist",
        dataset_name="tiny",
        document_rows=[current],
        reconcile=True,
    )
    assert stale in audit.removed_files
    assert len(audit.duplicate_files) == 1
    assert not stale.exists()
    assert sum(path.exists() for path in (keep, duplicate)) == 1


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
        total_pages=3,
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
    assert "## Coverage Summary" in content
    assert "| Cache pages | 3 |" in content
    assert "| Pages without visual artifacts | 1 |" in content
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
