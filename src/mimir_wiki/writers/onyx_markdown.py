from __future__ import annotations

import re
from pathlib import Path

from mimir_wiki.cache_reader import PageBundle
from mimir_wiki.config import AppConfig
from mimir_wiki.schemas import CandidateEntity, Enrichment, WarningRecord
from mimir_wiki.utils import atomic_write_text, json_dumps, slugify, strip_front_matter
from mimir_wiki.visual_extraction import load_visual_extraction

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

GENERIC_ENTITY_NAMES = {
    "api",
    "business",
    "business document",
    "business documents",
    "configuration",
    "database",
    "database configuration",
    "document",
    "documents",
    "here",
    "information",
    "open",
    "performance",
    "technical",
    "technical document",
    "wip internal",
}
GENERIC_KEY_FACT_TERMS = GENERIC_ENTITY_NAMES | {
    "account",
    "configuration",
    "failed",
    "guide",
    "here",
    "information",
    "installation",
    "linking",
    "previous",
    "service",
    "verify",
}


def onyx_output_path(root: Path, dataset_name: str, bundle: PageBundle, config: AppConfig) -> Path:
    slug = slugify(bundle.metadata.title, config.onyx_poc.slug_max_chars)
    return root / dataset_name / bundle.metadata.space_key / f"{bundle.metadata.page_id}-{slug}.md"


def remove_onyx_markdown_for_page(
    root: Path,
    dataset_name: str,
    bundle: PageBundle,
    config: AppConfig,
    *,
    keep_path: Path | None = None,
) -> int:
    directory = root / dataset_name / bundle.metadata.space_key
    if not directory.exists():
        return 0
    removed = 0
    keep_resolved = keep_path.resolve(strict=False) if keep_path is not None else None
    for path in directory.glob(f"{bundle.metadata.page_id}-*.md"):
        if keep_resolved is not None and path.resolve(strict=False) == keep_resolved:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def onyx_export_exclusion_reasons(enrichment: Enrichment, config: AppConfig) -> list[str]:
    reasons: list[str] = []
    if enrichment.audience in config.onyx_poc.exclude_audiences:
        reasons.append(f"audience:{enrichment.audience}")
    if enrichment.sensitivity in config.onyx_poc.exclude_sensitivities:
        reasons.append(f"sensitivity:{enrichment.sensitivity}")
    excluded_flags = sorted(
        set(enrichment.review_flags) & set(config.onyx_poc.exclude_review_flags)
    )
    reasons.extend(f"review_flag:{flag}" for flag in excluded_flags)
    return reasons


def should_emit_onyx_markdown(enrichment: Enrichment, config: AppConfig) -> bool:
    return not onyx_export_exclusion_reasons(enrichment, config)


def display_title(title: str) -> str:
    cleaned = re.sub(r"^\s*\d+(?:\.\d+)*\.?\s+", "", title).strip()
    return cleaned or title


def render_markdown(
    *,
    bundle: PageBundle,
    enrichment: Enrichment,
    config: AppConfig,
    include_source_content: bool,
) -> tuple[str, list[str]]:
    metadata = enrichment.ONYX_METADATA.model_dump(mode="json", exclude_none=True)
    first_line = f"#ONYX_METADATA={json_dumps(metadata)}"
    title = display_title(bundle.metadata.title)
    truncation_warnings: list[str] = []
    source = rewrite_source_images(strip_front_matter(bundle.clean_markdown))
    if len(source) > config.onyx_poc.max_source_content_chars:
        source = source[: config.onyx_poc.max_source_content_chars]
        truncation_warnings.append("source_content_truncated_for_onyx")
    key_fact_lines = render_key_facts(bundle, enrichment)
    source_link_lines = render_source_links(bundle, limit=8, include_low_value=False)
    additional_source_link_lines = render_additional_source_links(bundle, source_link_lines)
    keyword_lines = "\n".join(f"- {keyword}" for keyword in enrichment.keywords) or "- none"
    theme_lines = "\n".join(f"- {theme}" for theme in enrichment.themes) or "- none"
    concept_lines = "\n".join(f"- {concept}" for concept in enrichment.concepts) or "- none"
    entity_lines = (
        "\n".join(
            f"- {entity.name} ({entity.entity_type}, confidence {entity.confidence:.2f})"
            for entity in display_candidate_entities(enrichment.candidate_entities)[:30]
        )
        or "- none"
    )
    warning_lines = "\n".join(f"- {warning}" for warning in enrichment.warnings) or "- none"
    review_flag_lines = "\n".join(f"- {flag}" for flag in enrichment.review_flags) or "- none"
    warning_heading = (
        "Operational Gaps"
        if enrichment.document_type in {"runbook", "support_model"}
        else "Documentation Quality Notes"
    )
    visual_section = render_visual_extraction(bundle, config)
    source_section = f"\n## Source Content\n\n{source}\n" if include_source_content else ""
    body = f"""

# {title}

> Source-enriched Confluence document. Not approved curated knowledge.

## Answer Summary

{enrichment.short_summary}

{enrichment.detailed_summary}

Document type: `{enrichment.document_type}` ({enrichment.document_type_confidence:.2f})
{document_subtype_line(enrichment)}
Quality band: `{enrichment.quality_band}` ({enrichment.quality.overall_score}/100)
Currentness: `{enrichment.currentness}`
Audience: `{enrichment.audience}`
Sensitivity: `{enrichment.sensitivity}`

Review flags: `{", ".join(enrichment.review_flags) if enrichment.review_flags else "none"}`

## Key Facts

{key_fact_lines}

## Source Links

{source_link_lines}

{source_section}

{visual_section}

{additional_source_link_lines}

## Enrichment Details

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

## Review Flags

{review_flag_lines}

{warning_heading}:

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
Attachment count: {bundle.attachment_reference_count}
Missing attachment content count: {len(bundle.missing_attachment_names)}
Hierarchy depth: {enrichment.hierarchy.depth}
Parent section: {enrichment.hierarchy.parent_title or "none"}
Page role: {enrichment.hierarchy.page_role}
Section path: {enrichment.hierarchy.section_path or "unknown"}
"""
    return first_line + body, truncation_warnings


def render_visual_extraction(bundle: PageBundle, config: AppConfig) -> str:
    artifact = load_visual_extraction(bundle)
    if artifact is None or artifact.images_succeeded == 0:
        return ""
    seen_hashes: set[str] = set()
    rendered_count = 0
    duplicate_count = 0
    truncated_count = 0
    lines = [
        "## Extracted Visual Content",
        "",
        (
            "Source-derived OCR/caption extraction from visual source artifacts. "
            "Use as evidence for retrieval and review, not approved curated knowledge. "
            "OCR may contain recognition errors."
        ),
        "",
        f"Extraction status: `{artifact.status}`",
        f"Provider/model: `{artifact.provider}` / `{artifact.model}`",
        "",
    ]
    for image in artifact.images:
        if image.status != "success":
            continue
        if config.onyx_poc.dedupe_visual_content and image.content_sha256:
            if image.content_sha256 in seen_hashes:
                duplicate_count += 1
                continue
            seen_hashes.add(image.content_sha256)
        if rendered_count >= config.onyx_poc.max_visual_images:
            truncated_count += 1
            continue
        rendered_count += 1
        lines.append(f"### {image.image_id}")
        lines.append("")
        lines.append(f"Source: {image.source}")
        if image.caption:
            lines.append("")
            lines.append(f"Caption: {image.caption}")
        if image.ocr_text:
            ocr_text = image.ocr_text
            if len(ocr_text) > config.onyx_poc.max_visual_ocr_chars:
                ocr_text = ocr_text[: config.onyx_poc.max_visual_ocr_chars].rstrip()
                truncated_count += 1
            lines.append("")
            lines.append("OCR text:")
            lines.append("")
            lines.append("```text")
            lines.append(ocr_text)
            if len(ocr_text) < len(image.ocr_text):
                lines.append("[truncated]")
            lines.append("```")
        lines.append("")
    if duplicate_count:
        lines.append(f"Duplicate visual images omitted from this section: {duplicate_count}")
        lines.append("")
    if truncated_count:
        lines.append(f"Visual OCR/images truncated for Onyx output: {truncated_count}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def display_candidate_entities(entities: list[CandidateEntity]) -> list[CandidateEntity]:
    selected: list[CandidateEntity] = []
    seen: set[tuple[str, str]] = set()
    for entity in sorted(
        entities,
        key=lambda item: (
            item.method != "llm",
            -item.confidence,
            item.entity_type,
            item.name.lower(),
        ),
    ):
        normalized = entity.normalized_name.lower()
        if normalized in GENERIC_ENTITY_NAMES:
            continue
        if entity.method != "llm" and entity.confidence < 0.7:
            continue
        key = (entity.entity_type, normalized)
        if key in seen:
            continue
        seen.add(key)
        selected.append(entity)
    return selected


def document_subtype_line(enrichment: Enrichment) -> str:
    if not enrichment.document_subtype:
        return "Document subtype: `not classified`"
    return f"Document subtype: `{enrichment.document_subtype}`"


def render_key_facts(bundle: PageBundle, enrichment: Enrichment) -> str:
    facts: list[str] = []
    facts.extend(render_llm_key_facts(enrichment))
    title = display_title(bundle.metadata.title)
    facts.append(f"- Page title: {title}")
    if title != bundle.metadata.title:
        facts.append(f"- Original source title: {bundle.metadata.title}")
    facts.append(f"- Source space: {bundle.metadata.space_key}")
    if enrichment.hierarchy.parent_title:
        facts.append(f"- Parent section: {enrichment.hierarchy.parent_title}")
    if enrichment.hierarchy.section_path:
        facts.append(f"- Section path: {enrichment.hierarchy.section_path}")
    facts.append(f"- Page role: {enrichment.hierarchy.page_role}")
    if enrichment.hierarchy.parent_context_type:
        facts.append(f"- Parent context: {enrichment.hierarchy.parent_context_type}")
    facts.append(f"- Document type: {enrichment.document_type}")
    if enrichment.document_subtype:
        facts.append(f"- Document subtype: {enrichment.document_subtype}")
    facts.append(f"- Currentness: {enrichment.currentness}")
    if bundle.metadata.updated_at:
        facts.append(f"- Source updated at: {bundle.metadata.updated_at}")
    for label, values in (
        ("Applications", enrichment.entities.get("applications", [])),
        ("Databases", enrichment.entities.get("databases", [])),
        ("Queues", enrichment.entities.get("queues", [])),
        ("Dashboards", enrichment.entities.get("dashboards", [])),
        ("Support groups", enrichment.entities.get("support_groups", [])),
        ("Teams", enrichment.entities.get("teams", [])),
    ):
        cleaned = filter_key_fact_values(label, values)
        if cleaned:
            facts.append(f"- {label}: {', '.join(cleaned)}")
    for label, entity_types in (
        ("Database services", {"database_service"}),
        ("Database instances", {"database_instance"}),
        ("Regions", {"aws_region", "region"}),
        ("APIs", {"api"}),
        ("Platforms", {"platform"}),
    ):
        values = high_confidence_entities_by_type(enrichment, entity_types)
        if values:
            facts.append(f"- {label}: {', '.join(values)}")
    if enrichment.keywords:
        terms = [
            keyword
            for keyword in enrichment.keywords
            if keyword.lower() not in GENERIC_KEY_FACT_TERMS
        ][:10]
        if terms:
            facts.append(f"- Important terms: {', '.join(terms)}")
    if enrichment.operational_signals.has_owner:
        facts.append("- Owner signal: present")
    if enrichment.operational_signals.has_support_group:
        facts.append("- Support group signal: present")
    if enrichment.operational_signals.has_dependencies:
        facts.append("- Dependency signal: present")
    if enrichment.operational_signals.has_recovery_steps:
        facts.append("- Recovery/failover signal: present")
    if bundle.attachment_reference_names:
        facts.append(f"- Attachments: {', '.join(bundle.attachment_reference_names[:10])}")
    return "\n".join(facts) if facts else "- none"


def render_llm_key_facts(enrichment: Enrichment) -> list[str]:
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for fact in enrichment.key_facts[:12]:
        label = fact.label.strip()
        value = fact.value.strip()
        if not label or not value:
            continue
        if is_question_like(value) or is_noisy_key_fact_value(value):
            continue
        key = (label.lower(), value.lower())
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- {label}: {value}")
    return lines


def filter_key_fact_values(label: str, values: list[str]) -> list[str]:
    filtered: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.lower().strip()
        if not normalized or normalized in seen:
            continue
        if (
            normalized in GENERIC_ENTITY_NAMES
            or is_question_like(value)
            or is_noisy_key_fact_value(value)
        ):
            continue
        if label in {"Support groups", "Teams"} and not is_stable_support_value(value):
            continue
        seen.add(normalized)
        filtered.append(value)
    return filtered[:10]


def is_question_like(value: str) -> bool:
    normalized = value.strip().lower()
    return "?" in value or normalized.startswith(
        (
            "what ",
            "who ",
            "why ",
            "how ",
            "when ",
            "where ",
            "do ",
            "does ",
            "can ",
            "could ",
            "please clarify",
        )
    )


def is_noisy_key_fact_value(value: str) -> bool:
    normalized = value.strip().lower()
    if len(value) > 100:
        return True
    return normalized in GENERIC_KEY_FACT_TERMS


def is_stable_support_value(value: str) -> bool:
    normalized = value.lower()
    return any(
        marker in normalized
        for marker in (
            "sre",
            "support",
            "l1",
            "l2",
            "l3",
            "team",
            "assignment",
            "resolver",
            "techops",
            "app-",
        )
    )


def high_confidence_entities_by_type(enrichment: Enrichment, entity_types: set[str]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for entity in sorted(
        enrichment.candidate_entities,
        key=lambda item: (item.method != "llm", -item.confidence, item.name.lower()),
    ):
        if entity.entity_type not in entity_types or entity.confidence < 0.75:
            continue
        normalized = entity.normalized_name.lower()
        if normalized in seen or normalized in GENERIC_ENTITY_NAMES:
            continue
        seen.add(normalized)
        values.append(entity.name)
    return values


def render_source_links(bundle: PageBundle, *, limit: int, include_low_value: bool) -> str:
    if not bundle.links.links:
        return "- none"
    lines: list[str] = []
    seen: set[str] = set()
    ranked_links = sorted(bundle.links.links, key=source_link_rank)
    for index, link in enumerate(ranked_links, start=1):
        href = link.href or ""
        if not href or href in seen:
            continue
        if is_relative_anchor_href(href):
            continue
        if not include_low_value and is_low_value_source_link(link.text or "", href):
            continue
        seen.add(href)
        label = (link.text or link.target_title or f"Source link {index}").strip()
        if label.lower() in {"here", "link", "click here"}:
            label = f"Referenced source link {index}"
        lines.append(f"- {label}: {href}")
        if len(lines) >= limit:
            break
    return "\n".join(lines) if lines else "- none"


def render_additional_source_links(bundle: PageBundle, early_lines: str) -> str:
    all_links = render_source_links(bundle, limit=100, include_low_value=True)
    if all_links == "- none" or all_links == early_lines:
        return ""
    return f"\n## Additional Source Links\n\n{all_links}\n"


def source_link_rank(link: object) -> tuple[int, str]:
    href = getattr(link, "href", "") or ""
    text = getattr(link, "text", "") or ""
    lower = f"{text} {href}".lower()
    if is_low_value_source_link(text, href):
        return (5, lower)
    if any(
        marker in lower
        for marker in ("runbook", "procedure", "rollback", "failover", "troubleshoot")
    ):
        return (0, lower)
    if any(marker in lower for marker in ("jira", "browse/", "service-now", "change_request")):
        return (1, lower)
    if "confluence" in lower:
        return (2, lower)
    return (3, lower)


def is_low_value_source_link(text: str, href: str) -> bool:
    lower = f"{text} {href}".lower()
    if href.startswith("mailto:") or is_relative_anchor_href(href):
        return True
    return any(
        marker in lower
        for marker in (
            "/display/~",
            "diffpagesbyversion",
            "templateid=",
            "newspacekey=",
            "personal/",
            "stream.aspx",
        )
    )


def is_relative_anchor_href(href: str) -> bool:
    return href.startswith("#")


def rewrite_source_images(markdown: str) -> str:
    def replace(match: re.Match[str]) -> str:
        alt = match.group(1).strip()
        url = match.group(2).strip()
        filename = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or "image"
        label = alt or filename
        return f"> Image omitted from source export: {label}"

    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", replace, markdown)


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
    exclusion_reasons = onyx_export_exclusion_reasons(enrichment, config)
    if exclusion_reasons:
        raise ValueError(
            f"Onyx export excluded for {bundle.metadata.page_id}: {', '.join(exclusion_reasons)}"
        )
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
    remove_onyx_markdown_for_page(root, dataset_name, bundle, config, keep_path=path)
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
