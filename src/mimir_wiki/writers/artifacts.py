from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from mimir_wiki.cache_reader import PageBundle
from mimir_wiki.enrichers.deterministic import normalize_entity_type
from mimir_wiki.schemas import (
    CandidateEntityRow,
    CandidateFactRow,
    ConceptRow,
    DocumentIndexRow,
    Enrichment,
    QualityScoreRow,
    ThemeRow,
    VisualIndexRow,
)
from mimir_wiki.utils import (
    atomic_write_json,
    atomic_write_jsonl,
    normalize_term,
    simhash64,
    slugify,
)

AGGREGATE_SINGLE_WORD_ALLOWLIST = {
    "datadog",
    "dev",
    "dr",
    "entitlements",
    "gitlab",
    "k6",
    "leanix",
    "ppe",
    "prod",
    "qa",
    "uat",
}


def write_enrichment(path: Path, enrichment: Enrichment) -> None:
    atomic_write_json(path, enrichment.model_dump(mode="json"))


def load_enrichment(path: Path) -> Enrichment:
    from mimir_wiki.utils import load_json

    return Enrichment.model_validate(load_json(path))


def document_index_row(
    bundle: PageBundle, enrichment: Enrichment, *, generated_at: str, run_id: str, dataset_name: str
) -> DocumentIndexRow:
    return DocumentIndexRow(
        run_id=run_id,
        dataset_name=dataset_name,
        document_id=bundle.document_id,
        page_id=bundle.metadata.page_id,
        space_key=bundle.metadata.space_key,
        title=bundle.metadata.title,
        url=bundle.metadata.url,
        source_updated_at=bundle.metadata.updated_at,
        retrieved_at=bundle.metadata.retrieved_at,
        version=bundle.metadata.version,
        source_content_hash=bundle.source_content_hash,
        document_type=enrichment.document_type,
        document_type_confidence=enrichment.document_type_confidence,
        document_subtype=enrichment.document_subtype,
        content_availability=enrichment.content_availability,
        quality_band=enrichment.quality_band,
        status_flags=enrichment.status_flags,
        review_flags=enrichment.review_flags,
        audience=enrichment.audience,
        sensitivity=enrichment.sensitivity,
        labels=bundle.metadata.labels,
        ancestor_titles=bundle.ancestor_titles,
        outbound_link_count=len(bundle.links.links),
        attachment_count=bundle.attachment_reference_count,
        word_count=len(bundle.text.split()),
        heading_count=len(enrichment.headings),
        text_simhash=simhash64(bundle.text),
        heading_simhash=simhash64(" ".join(enrichment.headings)),
        hierarchy_depth=enrichment.hierarchy.depth,
        parent_title=enrichment.hierarchy.parent_title,
        root_title=enrichment.hierarchy.root_title,
        section_path=enrichment.hierarchy.section_path,
        page_role=enrichment.hierarchy.page_role,
        parent_context_type=enrichment.hierarchy.parent_context_type,
        sibling_count=enrichment.hierarchy.sibling_count,
        child_count=enrichment.hierarchy.child_count,
        generated_at=generated_at,
    )


def quality_score_row(
    enrichment: Enrichment, *, generated_at: str, run_id: str, dataset_name: str
) -> QualityScoreRow:
    return QualityScoreRow(
        run_id=run_id,
        dataset_name=dataset_name,
        document_id=enrichment.document_id,
        page_id=enrichment.page_id,
        space_key=enrichment.space_key,
        source_updated_at=enrichment.source_updated_at,
        source_content_hash=enrichment.source_content_hash,
        quality_score=enrichment.quality.overall_score,
        quality_band=enrichment.quality_band,
        dimensions={
            "freshness": enrichment.quality.freshness_score,
            "authority": enrichment.quality.authority_score,
            "completeness": enrichment.quality.completeness_score,
            "operational_value": enrichment.quality.operational_value_score,
            "ownership_clarity": enrichment.quality.ownership_clarity_score,
            "contradiction_penalty": enrichment.quality.contradiction_risk_score,
        },
        warnings=enrichment.warnings,
        generated_at=generated_at,
    )


def visual_index_rows(
    bundle: PageBundle, *, generated_at: str, run_id: str, dataset_name: str
) -> list[VisualIndexRow]:
    from mimir_wiki.visual_extraction import load_visual_extraction

    artifact = load_visual_extraction(bundle)
    if artifact is None:
        return []
    return [
        VisualIndexRow(
            run_id=run_id,
            dataset_name=dataset_name,
            generated_at=generated_at,
            document_id=bundle.document_id,
            page_id=bundle.metadata.page_id,
            space_key=bundle.metadata.space_key,
            source_updated_at=bundle.metadata.updated_at,
            source_content_hash=bundle.source_content_hash,
            image_id=image.image_id,
            status=image.status,
            source=image.source,
            source_kind=image.source_kind,
            mime_type=image.mime_type,
            content_sha256=image.content_sha256,
            confidence=image.confidence,
            caption=image.caption,
            ocr_text=image.ocr_text,
            provider=image.provider,
            model=image.model,
            prompt_version=image.prompt_version,
            error_type=image.error_type,
            error=image.error,
            visual_run_id=artifact.run_id,
            visual_extracted_at=artifact.extracted_at,
        )
        for image in artifact.images
    ]


def aggregate_theme_rows(
    enrichments: list[Enrichment], *, generated_at: str, run_id: str, dataset_name: str
) -> list[ThemeRow]:
    documents_by_theme: dict[str, set[str]] = defaultdict(set)
    display_by_theme: dict[str, str] = {}
    for enrichment in enrichments:
        for theme in enrichment.themes:
            normalized = normalize_term(theme)
            if not normalized:
                continue
            display_by_theme.setdefault(normalized, theme)
            documents_by_theme[normalized].add(enrichment.document_id)
    rows = []
    for normalized, documents in documents_by_theme.items():
        if should_drop_aggregate_taxonomy(normalized, len(documents)):
            continue
        rows.append(
            ThemeRow(
                run_id=run_id,
                dataset_name=dataset_name,
                theme_id=f"theme:{slugify(normalized, 120)}",
                theme=display_by_theme[normalized],
                normalized_theme=normalized,
                document_count=len(documents),
                documents=sorted(documents),
                confidence=0.65,
                method="deterministic",
                generated_at=generated_at,
            )
        )
    return sorted(rows, key=lambda row: (row.normalized_theme, row.theme_id))


def aggregate_concept_rows(
    enrichments: list[Enrichment], *, generated_at: str, run_id: str, dataset_name: str
) -> list[ConceptRow]:
    documents_by_concept: dict[str, set[str]] = defaultdict(set)
    display_by_concept: dict[str, str] = {}
    for enrichment in enrichments:
        for concept in enrichment.concepts:
            normalized = normalize_term(concept)
            if not normalized:
                continue
            display_by_concept.setdefault(normalized, concept)
            documents_by_concept[normalized].add(enrichment.document_id)
    rows = []
    for normalized, documents in documents_by_concept.items():
        if should_drop_aggregate_taxonomy(normalized, len(documents)):
            continue
        rows.append(
            ConceptRow(
                run_id=run_id,
                dataset_name=dataset_name,
                concept_id=f"concept:{slugify(normalized, 120)}",
                concept=display_by_concept[normalized],
                normalized_concept=normalized,
                description=(
                    "Deterministic concept extracted from headings or keywords: "
                    f"{display_by_concept[normalized]}"
                ),
                document_count=len(documents),
                documents=sorted(documents),
                confidence=0.58,
                method="deterministic",
                generated_at=generated_at,
            )
        )
    return sorted(rows, key=lambda row: (row.normalized_concept, row.concept_id))


def should_drop_aggregate_taxonomy(normalized: str, document_count: int) -> bool:
    words = normalized.split()
    if len(words) != 1:
        return False
    if normalized in AGGREGATE_SINGLE_WORD_ALLOWLIST:
        return False
    return document_count < 3


def aggregate_candidate_entity_rows(
    enrichments: list[Enrichment], *, generated_at: str, run_id: str, dataset_name: str
) -> list[CandidateEntityRow]:
    grouped: dict[tuple[str, str], CandidateEntityRow] = {}
    document_sets: dict[tuple[str, str], set[str]] = defaultdict(set)
    confidence_totals: dict[tuple[str, str], list[float]] = defaultdict(list)
    for enrichment in enrichments:
        for entity in enrichment.candidate_entities:
            entity_type = normalize_entity_type(entity.entity_type, name=entity.name)
            key = (entity_type, entity.normalized_name)
            document_sets[key].add(enrichment.document_id)
            confidence_totals[key].append(entity.confidence)
            if key not in grouped:
                grouped[key] = CandidateEntityRow(
                    run_id=run_id,
                    dataset_name=dataset_name,
                    entity_id=(f"candidate:{entity_type}:{slugify(entity.normalized_name, 120)}"),
                    name=entity.name,
                    normalized_name=entity.normalized_name,
                    entity_type=entity_type,
                    aliases=entity.aliases,
                    document_count=0,
                    mentions=[],
                    confidence=entity.confidence,
                    method=entity.method,
                    generated_at=generated_at,
                )
            grouped[key].mentions.extend(entity.mentions)
    rows = []
    for key, row in grouped.items():
        data = row.model_dump(mode="python")
        data["document_count"] = len(document_sets[key])
        data["confidence"] = round(sum(confidence_totals[key]) / len(confidence_totals[key]), 2)
        data["mentions"] = sorted(
            data["mentions"],
            key=lambda item: (item["document_id"], item["source_field"], item["evidence"]),
        )[:100]
        rows.append(CandidateEntityRow.model_validate(data))
    return sorted(rows, key=lambda row: (row.entity_type, row.normalized_name))


def aggregate_candidate_fact_rows(
    enrichments: list[Enrichment], *, generated_at: str, run_id: str, dataset_name: str
) -> list[CandidateFactRow]:
    rows: list[CandidateFactRow] = []
    for enrichment in enrichments:
        for index, fact in enumerate(enrichment.candidate_facts, start=1):
            rows.append(
                CandidateFactRow(
                    run_id=run_id,
                    dataset_name=dataset_name,
                    generated_at=generated_at,
                    document_id=enrichment.document_id,
                    page_id=enrichment.page_id,
                    space_key=enrichment.space_key,
                    source_updated_at=enrichment.source_updated_at,
                    source_content_hash=enrichment.source_content_hash,
                    fact_id=f"fact:{enrichment.space_key}:{enrichment.page_id}:{index}",
                    subject=fact.subject,
                    predicate=fact.predicate,
                    claim_type=fact.claim_type,
                    object=fact.object,
                    evidence_text=fact.evidence_text,
                    confidence=fact.confidence,
                    extraction_method=fact.extraction_method,
                    source_section=fact.source_section,
                    extracted_at=fact.extracted_at,
                )
            )
    return sorted(rows, key=lambda row: (row.space_key, row.page_id, row.fact_id))


def write_global_jsonl(
    *,
    knowledge_dir: Path,
    document_rows: list[DocumentIndexRow],
    quality_rows: list[QualityScoreRow],
    theme_rows: list[ThemeRow],
    concept_rows: list[ConceptRow],
    candidate_entity_rows: list[CandidateEntityRow],
    candidate_fact_rows: list[CandidateFactRow],
    visual_rows: list[VisualIndexRow],
) -> int:
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    document_rows = sorted(document_rows, key=lambda row: (row.space_key, row.page_id))
    quality_rows = sorted(quality_rows, key=lambda row: (row.space_key, row.page_id))
    visual_rows = sorted(visual_rows, key=lambda row: (row.space_key, row.page_id, row.image_id))
    outputs = {
        "document_index.jsonl": [row.model_dump(mode="json") for row in document_rows],
        "quality_scores.jsonl": [row.model_dump(mode="json") for row in quality_rows],
        "themes.jsonl": [row.model_dump(mode="json") for row in theme_rows],
        "concepts.jsonl": [row.model_dump(mode="json") for row in concept_rows],
        "candidate_entities.jsonl": [row.model_dump(mode="json") for row in candidate_entity_rows],
        "facts.jsonl": [
            row.model_dump(mode="json") for row in candidate_fact_rows if row.confidence >= 0.7
        ],
        "evidence_hints.jsonl": [
            row.model_dump(mode="json") for row in candidate_fact_rows if row.confidence < 0.7
        ],
        "visual_index.jsonl": [row.model_dump(mode="json") for row in visual_rows],
    }
    for filename, rows in outputs.items():
        atomic_write_jsonl(knowledge_dir / filename, rows)
    return len(outputs)
