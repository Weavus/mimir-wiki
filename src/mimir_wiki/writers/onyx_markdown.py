from __future__ import annotations

import re
from pathlib import Path

from mimir_wiki.cache_reader import PageBundle
from mimir_wiki.config import AppConfig
from mimir_wiki.schemas import Enrichment, WarningRecord
from mimir_wiki.utils import atomic_write_text, json_dumps, slugify, strip_front_matter

SECRET_PATTERNS = {
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "azure_openai_key_assignment": re.compile(
        r"(?i)\b(azure[_ -]?openai[_ -]?api[_ -]?key)\s*[:=]\s*[^\s`]+"
    ),
    "generic_api_key_assignment": re.compile(r"(?i)\b(api[_ -]?key)\s*[:=]\s*[^\s`]+"),
    "bearer_token": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    "connection_string": re.compile(
        r"(?i)\b(Server|Host|Endpoint)=[^;\n]+;(?:[^\n;]+=[^;\n]+;?){2,}"
    ),
    "private_key_block": re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    "password_assignment": re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*[^\s`]+"),
}


def onyx_output_path(root: Path, dataset_name: str, bundle: PageBundle, config: AppConfig) -> Path:
    slug = slugify(bundle.metadata.title, config.onyx_poc.slug_max_chars)
    return root / dataset_name / bundle.metadata.space_key / f"{bundle.metadata.page_id}-{slug}.md"


def render_markdown(
    *,
    bundle: PageBundle,
    enrichment: Enrichment,
    config: AppConfig,
    include_source_content: bool,
) -> tuple[str, list[str]]:
    metadata = enrichment.ONYX_METADATA.model_dump(mode="json", exclude_none=True)
    first_line = f"#ONYX_METADATA={json_dumps(metadata)}"
    truncation_warnings: list[str] = []
    source = strip_front_matter(bundle.clean_markdown)
    if len(source) > config.onyx_poc.max_source_content_chars:
        source = source[: config.onyx_poc.max_source_content_chars]
        truncation_warnings.append("source_content_truncated_for_onyx")
    keyword_lines = "\n".join(f"- {keyword}" for keyword in enrichment.keywords) or "- none"
    theme_lines = "\n".join(f"- {theme}" for theme in enrichment.themes) or "- none"
    concept_lines = "\n".join(f"- {concept}" for concept in enrichment.concepts) or "- none"
    entity_lines = (
        "\n".join(
            f"- {entity.name} ({entity.entity_type}, confidence {entity.confidence:.2f})"
            for entity in enrichment.candidate_entities[:40]
        )
        or "- none"
    )
    warning_lines = "\n".join(f"- {warning}" for warning in enrichment.warnings) or "- none"
    source_section = f"\n## Source Content\n\n{source}\n" if include_source_content else ""
    body = f"""

# {bundle.metadata.title}

> Source-enriched Confluence document. Not approved curated knowledge.

## Enrichment Summary

{enrichment.short_summary}

{enrichment.detailed_summary}

Document type: `{enrichment.document_type}` ({enrichment.document_type_confidence:.2f})
Quality band: `{enrichment.quality_band}` ({enrichment.quality.overall_score}/100)
Currentness: `{enrichment.currentness}`

## Keywords

{keyword_lines}

## Themes

{theme_lines}

## Concepts

{concept_lines}

## Candidate Entities

{entity_lines}

## Quality Signals

- Freshness: {enrichment.quality.freshness_score}
- Authority: {enrichment.quality.authority_score}
- Completeness: {enrichment.quality.completeness_score}
- Operational value: {enrichment.quality.operational_value_score}
- Ownership clarity: {enrichment.quality.ownership_clarity_score}

Warnings:

{warning_lines}

## Source Metadata

Schema version: {enrichment.schema_version}
Run ID: {enrichment.run_id}
Document ID: {enrichment.document_id}
Page ID: {enrichment.page_id}
Space: {enrichment.space_key}
Source updated at: {enrichment.source_updated_at or "unknown"}
Source content hash: {enrichment.source_content_hash}
Source URL: {bundle.metadata.url or "unknown"}
Attachment count: {len(bundle.attachment_names)}
{source_section}"""
    return first_line + body, truncation_warnings


def apply_redaction(content: str, config: AppConfig) -> tuple[str, list[str]]:
    if not config.redaction.enabled or config.redaction.action == "off":
        return content, []
    warnings: list[str] = []
    redacted = content
    for pattern_name in config.redaction.patterns:
        pattern = SECRET_PATTERNS.get(pattern_name)
        if pattern is None:
            continue
        matches = list(pattern.finditer(redacted))
        if not matches:
            continue
        warnings.append(f"redaction_match:{pattern_name}:{len(matches)}")
        if config.redaction.action == "fail":
            continue
        redacted = pattern.sub(config.redaction.replacement, redacted)
    return redacted, warnings


def write_onyx_markdown(
    *,
    root: Path,
    dataset_name: str,
    bundle: PageBundle,
    enrichment: Enrichment,
    config: AppConfig,
    generated_at: str,
    run_id: str,
) -> tuple[Path, list[WarningRecord]]:
    content, render_warnings = render_markdown(
        bundle=bundle,
        enrichment=enrichment,
        config=config,
        include_source_content=config.onyx_poc.include_source_content,
    )
    content, redaction_warnings = apply_redaction(content, config)
    all_warnings = render_warnings + redaction_warnings
    if config.redaction.enabled and config.redaction.action == "fail" and redaction_warnings:
        raise ValueError(
            "Redaction policy failed for "
            f"{bundle.metadata.page_id}: {', '.join(redaction_warnings)}"
        )
    path = onyx_output_path(root, dataset_name, bundle, config)
    atomic_write_text(path, content)
    warning_records = [
        WarningRecord(
            run_id=run_id,
            dataset_name=dataset_name,
            generated_at=generated_at,
            document_id=bundle.document_id,
            page_id=bundle.metadata.page_id,
            space_key=bundle.metadata.space_key,
            title=bundle.metadata.title,
            warning_type=warning,
            message=warning,
            stage="onyx_markdown",
        )
        for warning in all_warnings
    ]
    return path, warning_records
