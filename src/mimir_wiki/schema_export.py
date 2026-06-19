from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from mimir_wiki.schemas import (
    CandidateEntityRow,
    CandidateFactRow,
    ConceptRow,
    DocumentIndexRow,
    Enrichment,
    LLMUsage,
    PageFailure,
    QualityScoreRow,
    RunSummary,
    ThemeRow,
    VisualExtractionArtifact,
    WarningRecord,
)
from mimir_wiki.utils import atomic_write_json

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "enrichment": Enrichment,
    "document_index_row": DocumentIndexRow,
    "quality_score_row": QualityScoreRow,
    "theme_row": ThemeRow,
    "concept_row": ConceptRow,
    "candidate_entity_row": CandidateEntityRow,
    "candidate_fact_row": CandidateFactRow,
    "page_failure": PageFailure,
    "warning_record": WarningRecord,
    "llm_usage": LLMUsage,
    "run_summary": RunSummary,
    "visual_extraction": VisualExtractionArtifact,
}


def export_json_schemas(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name, model in sorted(SCHEMA_MODELS.items()):
        path = out_dir / f"{name}.schema.json"
        atomic_write_json(path, model.model_json_schema())
        paths.append(path)
    return paths
