from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mimir_wiki.constants import DOCUMENT_TYPES, GENERATOR, SCHEMA_VERSION


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class Dataset(FlexibleModel):
    source: str = "confluence"
    dataset_name: str
    base_url: str | None = None
    api_root: str | None = None
    crawl_type: str | None = None
    crawl_config: dict[str, Any] = Field(default_factory=dict)
    tool_version: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ManifestRow(FlexibleModel):
    markdown_path: str
    page_id: str
    path: str
    space_key: str
    status: str
    title: str
    updated_at: str | None = None
    version: int | None = None


class ManifestSummary(FlexibleModel):
    spaces: dict[str, int] = Field(default_factory=dict)
    status: str | None = None
    statuses: dict[str, int] = Field(default_factory=dict)
    total_pages: int | None = None


class ExportError(FlexibleModel):
    operation: str | None = None
    page_id: str | None = None
    error: str
    timestamp: str | None = None


class Author(FlexibleModel):
    display_name: str | None = None
    username: str | None = None


class Ancestor(FlexibleModel):
    id: str | None = None
    title: str


class ContentHashes(FlexibleModel):
    storage_sha256: str | None = None
    export_view_sha256: str | None = None
    markdown_sha256: str | None = None
    text_sha256: str | None = None

    def as_source_mapping(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "storage_sha256": self.storage_sha256,
                "export_view_sha256": self.export_view_sha256,
                "markdown_sha256": self.markdown_sha256,
                "text_sha256": self.text_sha256,
            }.items()
            if value
        }


class PageMetadata(FlexibleModel):
    ancestors: list[Ancestor] = Field(default_factory=list)
    author: Author | None = None
    content_hashes: ContentHashes = Field(default_factory=ContentHashes)
    conversion_status: str | None = None
    created_at: str | None = None
    download_status: str | None = None
    labels: list[str] = Field(default_factory=list)
    page_id: str
    space_key: str
    space_name: str | None = None
    status: str | None = None
    title: str
    url: str | None = None
    version: int | None = None
    updated_at: str | None = None
    retrieved_at: str | None = None


class Link(FlexibleModel):
    type: str | None = None
    href: str | None = None
    text: str | None = None
    crawlable: bool | None = None
    target_page_id: str | None = None
    target_space_key: str | None = None
    target_title: str | None = None


class LinksFile(FlexibleModel):
    page_id: str | None = None
    links: list[Link] = Field(default_factory=list)


class Conversion(FlexibleModel):
    converter: str | None = None
    converter_version: str | None = None
    converted_at: str | None = None
    markdown_sha256: str | None = None
    text_sha256: str | None = None
    warnings: list[str] = Field(default_factory=list)


class OnyxMetadata(BaseModel):
    link: str
    file_display_name: str
    doc_updated_at: str
    dataset_name: str | None = None
    source_system: str | None = None
    space_key: str | None = None
    document_type: str | None = None
    document_subtype: str | None = None
    quality_band: str | None = None
    approval_status: str = "unreviewed"
    historical: bool | None = None
    currentness: str | None = None
    audience: str | None = None
    sensitivity: str | None = None
    review_flags: list[str] = Field(default_factory=list)


class OperationalSignals(BaseModel):
    has_owner: bool = False
    has_support_group: bool = False
    has_escalation_path: bool = False
    has_runbook_steps: bool = False
    has_diagnostic_steps: bool = False
    has_recovery_steps: bool = False
    has_validation_steps: bool = False
    has_backout_steps: bool = False
    has_monitoring_links: bool = False
    has_dependencies: bool = False
    has_known_errors: bool = False
    has_architecture_description: bool = False
    has_environment_details: bool = False
    has_impact_summary: bool = False
    has_incident_timeline: bool = False
    has_root_cause: bool = False
    has_contributing_factors: bool = False
    has_detection_gap: bool = False
    has_monitoring_gap: bool = False
    has_runbook_gap: bool = False
    has_corrective_actions: bool = False
    has_preventive_actions: bool = False
    has_action_owners: bool = False
    has_due_dates: bool = False
    links_to_incident_or_change: bool = False


class Quality(BaseModel):
    freshness_score: int = Field(ge=0, le=100)
    authority_score: int = Field(ge=0, le=100)
    completeness_score: int = Field(ge=0, le=100)
    operational_value_score: int = Field(ge=0, le=100)
    ownership_clarity_score: int = Field(ge=0, le=100)
    staleness_risk_score: int = Field(ge=0, le=100)
    contradiction_risk_score: int = Field(ge=0, le=100)
    overall_score: int = Field(ge=0, le=100)


class HierarchyContext(BaseModel):
    ancestor_titles: list[str] = Field(default_factory=list)
    depth: int = 0
    root_title: str | None = None
    parent_title: str | None = None
    section_path: str | None = None
    is_root_page: bool = False
    is_index_page: bool = False
    is_leaf_page: bool = True
    sibling_count: int = 0
    child_count: int = 0
    page_role: str = "unknown"
    parent_context_type: str | None = None
    ancestor_context_types: list[str] = Field(default_factory=list)


class CandidateFact(BaseModel):
    subject: str
    predicate: str
    object: str
    confidence: float = Field(ge=0, le=1)
    evidence_text: str
    extraction_method: str = "deterministic"
    source_document_id: str | None = None
    source_section: str | None = None
    extracted_at: str | None = None


class CandidateMention(BaseModel):
    document_id: str
    page_id: str
    evidence: str
    source_field: str


class CandidateEntity(BaseModel):
    name: str
    normalized_name: str
    entity_type: str
    aliases: list[str] = Field(default_factory=list)
    mentions: list[CandidateMention] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1, default=0.7)
    method: str = "deterministic"


class KeyFact(BaseModel):
    label: str
    value: str
    confidence: float = Field(ge=0, le=1, default=0.7)
    evidence: str | None = None
    method: str = "llm"


class EnrichmentSignature(BaseModel):
    source_content_hash: str
    schema_version: str = SCHEMA_VERSION
    prompt_version: str
    provider: str
    model_or_deployment: str
    tasks: list[str]
    enrichment_config_hash: str


class Enrichment(FlexibleModel):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    generated_at: str
    generator: str = GENERATOR
    dataset_name: str
    source_system: str = "confluence"
    document_id: str
    page_id: str
    space_key: str
    source_updated_at: str | None = None
    source_content_hash: str
    enriched_at: str
    ONYX_METADATA: OnyxMetadata
    document_type: str
    document_type_confidence: float = Field(ge=0, le=1)
    document_subtype: str | None = None
    hierarchy: HierarchyContext = Field(default_factory=HierarchyContext)
    short_summary: str
    detailed_summary: str
    key_facts: list[KeyFact] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)
    candidate_entities: list[CandidateEntity] = Field(default_factory=list)
    entities: dict[str, list[str]] = Field(default_factory=dict)
    operational_signals: OperationalSignals = Field(default_factory=OperationalSignals)
    quality: Quality
    quality_band: str
    warnings: list[str] = Field(default_factory=list)
    review_flags: list[str] = Field(default_factory=list)
    audience: str = "internal"
    sensitivity: str = "internal"
    candidate_facts: list[CandidateFact] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    historical: bool = False
    currentness: str = "current"
    headings: list[str] = Field(default_factory=list)
    status_flags: list[str] = Field(default_factory=list)
    signatures: EnrichmentSignature
    chunk_count: int = 0
    llm_failures: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("document_type")
    @classmethod
    def document_type_is_supported(cls, value: str) -> str:
        if value not in DOCUMENT_TYPES:
            raise ValueError(f"unsupported document_type: {value}")
        return value


class CommonArtifact(BaseModel):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    dataset_name: str
    generated_at: str
    generator: str = GENERATOR


class PageScopedArtifact(CommonArtifact):
    source_system: str = "confluence"
    document_id: str
    page_id: str
    space_key: str
    source_updated_at: str | None = None
    source_content_hash: str


class VisualExtractionImage(FlexibleModel):
    image_id: str
    source: str
    source_kind: Literal["data_url", "file", "url"]
    mime_type: str | None = None
    content_sha256: str | None = None
    status: Literal["success", "skipped", "failed"]
    ocr_text: str = ""
    caption: str = ""
    confidence: float | None = Field(default=None, ge=0, le=1)
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    error_type: str | None = None
    error: str | None = None


class VisualExtractionArtifact(PageScopedArtifact):
    extracted_at: str
    status: Literal["complete", "partial", "failed", "skipped"]
    method: str = "multimodal_ocr"
    provider: str
    model: str
    prompt_version: str = "visual-ocr-v1"
    image_count: int = 0
    images_succeeded: int = 0
    images_failed: int = 0
    images_skipped: int = 0
    images: list[VisualExtractionImage] = Field(default_factory=list)


class VisualIndexRow(PageScopedArtifact):
    image_id: str
    status: Literal["success", "skipped", "failed"]
    source: str
    source_kind: Literal["data_url", "file", "url"]
    mime_type: str | None = None
    content_sha256: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    caption: str = ""
    ocr_text: str = ""
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    error_type: str | None = None
    error: str | None = None
    visual_run_id: str
    visual_extracted_at: str


class DocumentIndexRow(PageScopedArtifact):
    title: str
    url: str | None = None
    retrieved_at: str | None = None
    version: int | None = None
    document_type: str
    document_type_confidence: float = Field(ge=0, le=1)
    status_flags: list[str] = Field(default_factory=list)
    review_flags: list[str] = Field(default_factory=list)
    audience: str = "internal"
    sensitivity: str = "internal"
    labels: list[str] = Field(default_factory=list)
    ancestor_titles: list[str] = Field(default_factory=list)
    outbound_link_count: int = 0
    attachment_count: int = 0
    word_count: int = 0
    heading_count: int = 0
    text_simhash: str | None = None
    heading_simhash: str | None = None
    hierarchy_depth: int = 0
    parent_title: str | None = None
    root_title: str | None = None
    section_path: str | None = None
    page_role: str = "unknown"
    parent_context_type: str | None = None
    sibling_count: int = 0
    child_count: int = 0


class QualityScoreRow(PageScopedArtifact):
    quality_score: int = Field(ge=0, le=100)
    quality_band: str
    dimensions: dict[str, int]
    warnings: list[str] = Field(default_factory=list)


class ThemeRow(CommonArtifact):
    source_system: str = "confluence"
    theme_id: str
    theme: str
    normalized_theme: str
    document_count: int
    documents: list[str]
    confidence: float = Field(ge=0, le=1)
    method: str


class ConceptRow(CommonArtifact):
    source_system: str = "confluence"
    concept_id: str
    concept: str
    normalized_concept: str
    description: str
    document_count: int
    documents: list[str]
    confidence: float = Field(ge=0, le=1)
    method: str


class CandidateEntityRow(CommonArtifact):
    source_system: str = "confluence"
    entity_id: str
    name: str
    normalized_name: str
    entity_type: str
    aliases: list[str] = Field(default_factory=list)
    document_count: int
    mentions: list[CandidateMention] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    method: str


class CandidateFactRow(PageScopedArtifact):
    fact_id: str
    subject: str
    predicate: str
    object: str
    evidence_text: str
    confidence: float = Field(ge=0, le=1)
    extraction_method: str
    source_section: str | None = None
    extracted_at: str | None = None


class PageFailure(PageScopedArtifact):
    title: str | None = None
    stage: str
    error_type: str
    message: str
    retryable: bool = False
    attempts: int = 1
    suggested_action: str | None = None


class WarningRecord(CommonArtifact):
    source_system: str = "confluence"
    document_id: str | None = None
    page_id: str | None = None
    space_key: str | None = None
    title: str | None = None
    warning_type: str
    message: str
    stage: str


class LLMUsage(PageScopedArtifact):
    task: str
    provider: str
    model: str
    prompt_version: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached: bool = False
    attempts: int = 1
    retries: int = 0
    elapsed_ms: int | None = None
    estimated_cost_usd: float | None = None


class RunSummary(CommonArtifact):
    command: str
    started_at: str
    finished_at: str
    elapsed_seconds: float
    status: Literal["success", "partial_success", "failed"]
    exit_code: int
    cache_path: str | None = None
    config_profile: str | None = None
    resolved_config: dict[str, Any] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def status_matches_exit_code(self) -> RunSummary:
        if self.exit_code == 0 and self.status != "success":
            raise ValueError("exit_code 0 requires success status")
        return self
