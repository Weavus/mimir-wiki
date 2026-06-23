from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from mimir_wiki.cache_reader import ValidationResult
from mimir_wiki.config import AppConfig
from mimir_wiki.schemas import (
    CandidateEntityRow,
    CandidateFactRow,
    DocumentIndexRow,
    Enrichment,
    LLMUsage,
    PageFailure,
    QualityScoreRow,
    RunSummary,
    VisualExtractionArtifact,
)
from mimir_wiki.utils import atomic_write_text, hamming_distance_hex, normalize_term


@dataclass(frozen=True)
class VisualReportPage:
    artifact: VisualExtractionArtifact
    title: str
    url: str | None = None
    discovered_image_count: int | None = None


@dataclass(frozen=True)
class OnyxExportAudit:
    current_files: int
    stale_files: list[Path]
    duplicate_files: list[Path]
    unparseable_files: list[Path]
    removed_files: list[Path]

    @property
    def has_issues(self) -> bool:
        return bool(self.stale_files or self.duplicate_files or self.unparseable_files)


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(cell.replace("\n", " ") for cell in row) + " |")
    return "\n".join(lines)


def audit_onyx_exports(
    *,
    onyx_root: Path,
    dataset_name: str,
    document_rows: list[DocumentIndexRow],
    reconcile: bool = False,
) -> OnyxExportAudit:
    dataset_root = onyx_root / dataset_name
    if not dataset_root.exists():
        return OnyxExportAudit(0, [], [], [], [])
    current_page_ids = {row.page_id for row in document_rows}
    files_by_page_id: dict[str, list[Path]] = defaultdict(list)
    unparseable: list[Path] = []
    for path in sorted(dataset_root.glob("**/*.md")):
        match = re.match(r"^(\d+)-.+\.md$", path.name)
        if match is None:
            unparseable.append(path)
            continue
        files_by_page_id[match.group(1)].append(path)
    stale = [
        path
        for page_id, paths in files_by_page_id.items()
        if page_id not in current_page_ids
        for path in paths
    ]
    duplicate_files: list[Path] = []
    for page_id, paths in files_by_page_id.items():
        if page_id not in current_page_ids or len(paths) < 2:
            continue
        keep = sorted(paths, key=lambda path: (path.stat().st_mtime_ns, str(path)), reverse=True)[0]
        duplicate_files.extend(path for path in paths if path != keep)
    removed: list[Path] = []
    if reconcile:
        for path in sorted(stale + duplicate_files):
            path.unlink(missing_ok=True)
            removed.append(path)
    current_files = sum(
        1
        for page_id, paths in files_by_page_id.items()
        if page_id in current_page_ids
        for _ in paths
    )
    if reconcile:
        current_files = max(0, current_files - len(duplicate_files))
    return OnyxExportAudit(current_files, stale, duplicate_files, unparseable, removed)


def write_cache_validation_report(result: ValidationResult, out_dir: Path) -> Path:
    issue_rows = [
        [issue.level, issue.code, issue.page_id or "", issue.path or "", issue.message]
        for issue in result.issues[:200]
    ]
    issues = (
        markdown_table(["Level", "Code", "Page", "Path", "Message"], issue_rows)
        if issue_rows
        else "No issues found."
    )
    content = f"""# Cache Validation

Dataset: {result.dataset_name or "unknown"}
Cache path: `{result.cache_path}`

## Summary

- Valid: {result.ok}
- Pages total: {result.pages_total}
- Pages valid: {result.pages_valid}
- Pages failed: {result.pages_failed}
- Export errors: {result.export_errors}

## Issues

{issues}
"""
    path = out_dir / "cache_validation.md"
    atomic_write_text(path, content)
    return path


def write_enrichment_summary(
    *,
    out_dir: Path,
    dataset_name: str,
    document_rows: list[DocumentIndexRow],
    quality_rows: list[QualityScoreRow],
    source_run_summaries: list[RunSummary] | None = None,
    page_failures: list[PageFailure] | None = None,
) -> Path:
    average_quality = 0
    if quality_rows:
        average_quality = round(sum(row.quality_score for row in quality_rows) / len(quality_rows))
    stale_count = sum(
        1
        for row in document_rows
        if any(flag in row.status_flags for flag in ("stale", "deprecated", "archived"))
    )
    source_run_summaries = source_run_summaries or []
    page_failures = page_failures or []
    source_run_lines = (
        "\n".join(
            f"- `{summary.run_id}`: {summary.command} {summary.status} "
            f"(exit {summary.exit_code}, processed "
            f"{summary.counts.get('pages_processed', 0)}, failed "
            f"{summary.counts.get('pages_failed', 0)})"
            for summary in source_run_summaries
        )
        or "- No matching enrich or extract-visuals runs found for this dataset."
    )
    failure_summary = "No page failures recorded for the selected source runs."
    if page_failures:
        grouped = Counter((failure.stage, failure.error_type) for failure in page_failures)
        rows = [
            [stage, error_type, str(count)]
            for (stage, error_type), count in grouped.most_common(20)
        ]
        failure_summary = markdown_table(["Stage", "Error type", "Count"], rows)
    content = f"""# Enrichment Summary

Dataset: {dataset_name}

## Summary

- Documents: {len(document_rows)}
- Average quality score: {average_quality}
- Good or better documents: {sum(1 for row in quality_rows if row.quality_score >= 70)}
- Stale/deprecated/archive documents: {stale_count}

## Source Runs

{source_run_lines}

## Current Page Failures

{failure_summary}
"""
    path = out_dir / "enrichment_summary.md"
    atomic_write_text(path, content)
    return path


def write_document_types_report(*, out_dir: Path, document_rows: list[DocumentIndexRow]) -> Path:
    counts = Counter(row.document_type for row in document_rows)
    rows = [[document_type, str(count)] for document_type, count in sorted(counts.items())]
    table = markdown_table(["Document type", "Count"], rows)
    availability_counts = Counter(row.content_availability for row in document_rows)
    availability_table = markdown_table(
        ["Content availability", "Count"],
        [[availability, str(count)] for availability, count in sorted(availability_counts.items())],
    )
    content = f"""# Document Types

{table}

## Content Availability

{availability_table}
"""
    path = out_dir / "document_types.md"
    atomic_write_text(path, content)
    return path


def write_stale_or_deprecated_report(
    *, out_dir: Path, document_rows: list[DocumentIndexRow]
) -> Path:
    selected = [
        row
        for row in document_rows
        if any(flag in row.status_flags for flag in ("stale", "deprecated", "archived"))
    ]
    rows = [
        [row.space_key, row.page_id, row.title, ", ".join(row.status_flags), row.url or ""]
        for row in selected[:200]
    ]
    table = (
        markdown_table(["Space", "Page ID", "Title", "Flags", "URL"], rows)
        if rows
        else "No stale, deprecated, or archived documents found."
    )
    content = f"# Stale Or Deprecated\n\n{table}\n"
    path = out_dir / "stale_or_deprecated.md"
    atomic_write_text(path, content)
    return path


def write_high_value_sources_report(
    *, out_dir: Path, document_rows: list[DocumentIndexRow], quality_rows: list[QualityScoreRow]
) -> Path:
    quality_by_doc = {row.document_id: row.quality_score for row in quality_rows}

    def review_priority(row: DocumentIndexRow) -> tuple[int, int, int]:
        quality = quality_by_doc.get(row.document_id, 0)
        return (
            quality - source_review_penalty(row),
            row.outbound_link_count,
            row.word_count,
        )

    high_value = sorted(
        document_rows,
        key=review_priority,
        reverse=True,
    )[:100]
    rows = [
        [
            row.space_key,
            row.page_id,
            str(quality_by_doc.get(row.document_id, 0)),
            str(review_priority(row)[0]),
            row.document_type,
            source_review_reason(row),
            row.title,
            row.url or "",
        ]
        for row in high_value
    ]
    table = markdown_table(
        ["Space", "Page ID", "Quality", "Priority", "Type", "Reason", "Title", "URL"],
        rows,
    )
    content = f"# High Value Sources\n\n{table}\n"
    path = out_dir / "high_value_sources.md"
    atomic_write_text(path, content)
    return path


def write_review_queue_report(
    *, out_dir: Path, document_rows: list[DocumentIndexRow], quality_rows: list[QualityScoreRow]
) -> Path:
    quality_by_doc = {row.document_id: row.quality_score for row in quality_rows}
    queue_rows: list[list[str]] = []
    for row in sorted(
        document_rows,
        key=lambda item: review_queue_priority(item, quality_by_doc.get(item.document_id, 0)),
        reverse=True,
    ):
        priority = review_queue_priority(row, quality_by_doc.get(row.document_id, 0))
        reasons = review_queue_reasons(row)
        if priority < 40 and "manual_review_required" not in row.review_flags:
            continue
        queue_rows.append(
            [
                str(priority),
                row.space_key,
                row.page_id,
                row.document_type,
                row.document_subtype or "",
                str(quality_by_doc.get(row.document_id, 0)),
                ", ".join(reasons),
                row.title,
                row.url or "",
            ]
        )
    table = (
        markdown_table(
            [
                "Priority",
                "Space",
                "Page ID",
                "Type",
                "Subtype",
                "Quality",
                "Reasons",
                "Title",
                "URL",
            ],
            queue_rows[:200],
        )
        if queue_rows
        else "No review candidates found."
    )
    content = f"# Review Queue\n\n{table}\n"
    path = out_dir / "review_queue.md"
    atomic_write_text(path, content)
    return path


def review_queue_priority(row: DocumentIndexRow, quality_score: int) -> int:
    priority = quality_score - source_review_penalty(row)
    if "manual_review_required" in row.review_flags:
        priority += 30
    if row.document_type in {"runbook", "support_model", "architecture"}:
        priority += 15
    if any(
        flag in row.review_flags
        for flag in ("visual_content_missing", "attachment_content_missing")
    ):
        priority += 10
    if row.sensitivity not in {"internal", "public"}:
        priority += 10
    return max(0, priority)


def review_queue_reasons(row: DocumentIndexRow) -> list[str]:
    reasons = source_review_reason(row).split(", ")
    if "manual_review_required" in row.review_flags:
        reasons.append("manual_review_required")
    if row.sensitivity not in {"internal", "public"}:
        reasons.append("sensitive_content")
    for flag in ("visual_content_missing", "attachment_content_missing"):
        if flag in row.review_flags:
            reasons.append(flag)
    return [reason for reason in dict.fromkeys(reasons) if reason]


def source_review_penalty(row: DocumentIndexRow) -> int:
    title = normalize_term(row.title)
    penalty = 0
    if row.document_type in {"meeting_notes", "project_plan", "change_record"}:
        penalty += 20
    if row.document_type in {"archive", "unknown"}:
        penalty += 35
    if any(flag in row.status_flags for flag in ("archived", "deprecated")):
        penalty += 30
    if "stale" in row.status_flags:
        penalty += 10
    if any(term in title for term in ("daily handover", "handover", "service review meeting")):
        penalty += 25
    if any(term in title for term in ("release", "rollback", "report")):
        penalty += 15
    if any(term in title for term in ("draft", "rejected", "template")):
        penalty += 25
    if "future_dated" in row.review_flags:
        penalty += 20
    return penalty


def source_review_reason(row: DocumentIndexRow) -> str:
    reasons: list[str] = []
    title = normalize_term(row.title)
    if row.document_type in {"runbook", "support_model", "architecture"}:
        reasons.append("operational_source")
    if row.document_type in {"meeting_notes", "project_plan", "change_record"}:
        reasons.append("lower_authority_type")
    if any(flag in row.status_flags for flag in ("stale", "archived", "deprecated")):
        reasons.append("currentness_risk")
    if any(term in title for term in ("handover", "service review meeting", "release", "rollback")):
        reasons.append("dated_operational_record")
    if "future_dated" in row.review_flags:
        reasons.append("future_dated")
    return ", ".join(reasons) or "candidate_canonical_source"


def write_missing_owners_report(*, out_dir: Path, enrichments: list[Enrichment]) -> Path:
    rows = [
        [
            enrichment.space_key,
            enrichment.page_id,
            enrichment.document_type,
            enrichment.ONYX_METADATA.file_display_name,
        ]
        for enrichment in enrichments
        if not enrichment.operational_signals.has_owner and enrichment.quality.overall_score >= 50
    ][:200]
    table = (
        markdown_table(["Space", "Page ID", "Type", "Title"], rows)
        if rows
        else "No missing-owner candidates found."
    )
    content = f"# Missing Owners\n\n{table}\n"
    path = out_dir / "missing_owners.md"
    atomic_write_text(path, content)
    return path


def write_high_value_subtrees_report(*, out_dir: Path, enrichments: list[Enrichment]) -> Path:
    grouped: dict[str, list[Enrichment]] = defaultdict(list)
    for enrichment in enrichments:
        key = enrichment.hierarchy.parent_title or enrichment.hierarchy.root_title or "unknown"
        grouped[key].append(enrichment)
    rows: list[list[str]] = []
    for section, items in grouped.items():
        average_quality = round(sum(item.quality.overall_score for item in items) / len(items))
        operational_pages = sum(
            1
            for item in items
            if item.document_type in {"runbook", "support_model", "architecture"}
        )
        roles = ", ".join(sorted({item.hierarchy.page_role for item in items})[:8])
        rows.append([section, str(len(items)), str(average_quality), str(operational_pages), roles])
    rows.sort(key=lambda row: (int(row[2]), int(row[1])), reverse=True)
    table = (
        markdown_table(
            ["Section", "Pages", "Avg quality", "Operational pages", "Roles"], rows[:100]
        )
        if rows
        else "No hierarchy sections found."
    )
    content = f"# High Value Subtrees\n\n{table}\n"
    path = out_dir / "high_value_subtrees.md"
    atomic_write_text(path, content)
    return path


def write_attachment_followups_report(
    *, out_dir: Path, document_rows: list[DocumentIndexRow]
) -> Path:
    rows = [
        [row.space_key, row.page_id, str(row.attachment_count), row.title, row.url or ""]
        for row in sorted(document_rows, key=lambda item: item.attachment_count, reverse=True)
        if row.attachment_count > 0
    ][:200]
    table = (
        markdown_table(["Space", "Page ID", "Attachments", "Title", "URL"], rows)
        if rows
        else "No pages with attachments found."
    )
    content = f"# Attachment Followups\n\n{table}\n"
    path = out_dir / "attachment_followups.md"
    atomic_write_text(path, content)
    return path


def write_onyx_export_risk_report(
    *, out_dir: Path, document_rows: list[DocumentIndexRow], config: AppConfig | None = None
) -> Path:
    risk_rows: list[list[str]] = []
    for row in sorted(document_rows, key=onyx_export_risk_score, reverse=True):
        score = onyx_export_risk_score(row)
        if score <= 0:
            continue
        risk_rows.append(
            [
                row.space_key,
                row.page_id,
                str(score),
                row.audience,
                row.sensitivity,
                ", ".join(onyx_export_risk_reasons(row)),
                row.title,
                row.url or "",
            ]
        )
    table = (
        markdown_table(
            ["Space", "Page ID", "Risk", "Audience", "Sensitivity", "Reasons", "Title", "URL"],
            risk_rows[:200],
        )
        if risk_rows
        else "No elevated Onyx export risks found."
    )
    gate_note = onyx_risk_gate_note(document_rows, config)
    content = f"""# Onyx Export Risk

Pages listed here should be reviewed before uploading enriched source Markdown to a broad
Onyx connector.

{gate_note}

{table}
"""
    path = out_dir / "onyx_export_risk.md"
    atomic_write_text(path, content)
    return path


def onyx_risk_gate_note(document_rows: list[DocumentIndexRow], config: AppConfig | None) -> str:
    if config is None or config.onyx_poc.risk_gate_action == "off":
        return "Risk gate: off."
    threshold = config.onyx_poc.risk_gate_threshold
    if threshold is None:
        return f"Risk gate: {config.onyx_poc.risk_gate_action}, no threshold configured."
    risky_count = sum(1 for row in document_rows if onyx_export_risk_score(row) >= threshold)
    note = (
        f"Risk gate: {config.onyx_poc.risk_gate_action} at threshold {threshold}; "
        f"{risky_count} page(s) meet or exceed the threshold."
    )
    if config.onyx_poc.risk_gate_action == "fail" and risky_count:
        raise ValueError(note)
    return note


def write_onyx_export_integrity_report(
    *,
    out_dir: Path,
    onyx_root: Path,
    dataset_name: str,
    document_rows: list[DocumentIndexRow],
    cache_pages_total: int | None = None,
    page_failures: list[PageFailure] | None = None,
    reconcile: bool = False,
) -> Path:
    audit = audit_onyx_exports(
        onyx_root=onyx_root,
        dataset_name=dataset_name,
        document_rows=document_rows,
        reconcile=reconcile,
    )
    summary_rows = [
        ["Cache pages", str(cache_pages_total if cache_pages_total is not None else "unknown")],
        ["Document index pages", str(len(document_rows))],
        [
            "Cache pages missing from document index",
            str(max(0, cache_pages_total - len(document_rows)))
            if cache_pages_total is not None
            else "unknown",
        ],
        ["Current page files", str(audit.current_files)],
        ["Stale files", str(len(audit.stale_files))],
        ["Duplicate page files", str(len(audit.duplicate_files))],
        ["Unparseable files", str(len(audit.unparseable_files))],
        ["Removed files", str(len(audit.removed_files))],
    ]
    issue_rows = (
        [["stale", str(path)] for path in audit.stale_files[:100]]
        + [["duplicate", str(path)] for path in audit.duplicate_files[:100]]
        + [["unparseable", str(path)] for path in audit.unparseable_files[:100]]
    )
    issues = (
        markdown_table(["Issue", "Path"], issue_rows)
        if issue_rows
        else "No Onyx export integrity issues found."
    )
    removed = (
        markdown_table(["Removed path"], [[str(path)] for path in audit.removed_files[:200]])
        if audit.removed_files
        else "No files removed."
    )
    document_page_ids = {row.page_id for row in document_rows}
    missing_failure_rows = [
        [
            failure.space_key,
            failure.page_id,
            failure.stage,
            failure.error_type,
            failure.title or "",
        ]
        for failure in page_failures or []
        if failure.page_id not in document_page_ids
    ]
    missing_failure_table = (
        markdown_table(
            ["Space", "Page ID", "Stage", "Error type", "Title"], missing_failure_rows[:200]
        )
        if missing_failure_rows
        else "No missing pages were linked to selected source-run failures."
    )
    mode = "reconciled" if reconcile else "audit only"
    content = f"""# Onyx Export Integrity

Dataset: {dataset_name}

Onyx root: `{onyx_root}`

Mode: {mode}

## Summary

{markdown_table(["Metric", "Count"], summary_rows)}

## Issues

{issues}

## Missing Pages From Source Failures

{missing_failure_table}

## Reconciliation

{removed}
"""
    path = out_dir / "onyx_export_integrity.md"
    atomic_write_text(path, content)
    return path


def write_entity_quality_report(*, out_dir: Path, entity_rows: list[CandidateEntityRow]) -> Path:
    type_counts = Counter(row.entity_type for row in entity_rows)
    tier_counts = Counter(entity_quality_tier(row) for row in entity_rows)
    noisy = [row for row in entity_rows if row.entity_type in {"url", "contact", "ticket"}]
    low_confidence = [row for row in entity_rows if row.confidence < 0.5]
    summary = markdown_table(
        ["Metric", "Count"],
        [
            ["Candidate entities", str(len(entity_rows))],
            ["Canonical candidates", str(tier_counts.get("canonical_candidate", 0))],
            ["Weak signals", str(tier_counts.get("weak_signal", 0))],
            ["Contact/link records", str(tier_counts.get("contact_link_record", 0))],
            ["URL/contact/ticket entities", str(len(noisy))],
            ["Low confidence entities", str(len(low_confidence))],
        ],
    )
    by_type = markdown_table(
        ["Entity type", "Count"],
        [[entity_type, str(count)] for entity_type, count in sorted(type_counts.items())],
    )
    by_tier = markdown_table(
        ["Quality tier", "Count"],
        [[tier, str(count)] for tier, count in sorted(tier_counts.items())],
    )
    noisy_rows = [
        [
            entity_quality_tier(row),
            row.entity_type,
            row.name,
            str(row.document_count),
            f"{row.confidence:.2f}",
        ]
        for row in sorted(noisy + low_confidence, key=lambda item: (item.entity_type, item.name))[
            :100
        ]
    ]
    noisy_table = (
        markdown_table(["Tier", "Type", "Name", "Documents", "Confidence"], noisy_rows)
        if noisy_rows
        else "No noisy or low-confidence candidate entities found."
    )
    content = f"""# Entity Quality

## Summary

{summary}

## Entity Types

{by_type}

## Quality Tiers

{by_tier}

## Follow-Up Candidates

{noisy_table}
"""
    path = out_dir / "entity_quality.md"
    atomic_write_text(path, content)
    return path


def write_llm_failures_report(*, out_dir: Path, enrichments: list[Enrichment]) -> Path:
    rows: list[list[str]] = []
    for enrichment in sorted(enrichments, key=lambda item: (item.space_key, item.page_id)):
        for failure in enrichment.llm_failures:
            context = failure.get("error_context")
            context = context if isinstance(context, dict) else {}
            rows.append(
                [
                    enrichment.space_key,
                    enrichment.page_id,
                    enrichment.ONYX_METADATA.file_display_name,
                    str(context.get("task") or failure.get("stage") or "unknown"),
                    str(context.get("provider") or "unknown"),
                    str(context.get("model") or "unknown"),
                    str(context.get("prompt_version") or "unknown"),
                    str(failure.get("error_type") or "unknown"),
                    str(failure.get("message") or "")[:300],
                    "deterministic_fallback_retained",
                ]
            )
    table = (
        markdown_table(
            [
                "Space",
                "Page ID",
                "Title",
                "Task",
                "Provider",
                "Model",
                "Prompt version",
                "Error type",
                "Message",
                "Fallback",
            ],
            rows[:300],
        )
        if rows
        else "No LLM task failures recorded in current enrichment artifacts."
    )
    content = f"""# LLM Failures

Pages listed here were still emitted with deterministic enrichment fallback, but one or more
LLM tasks failed.

{table}
"""
    path = out_dir / "llm_failures.md"
    atomic_write_text(path, content)
    return path


def entity_quality_tier(row: CandidateEntityRow) -> str:
    if row.entity_type in {"url", "contact", "ticket"}:
        return "contact_link_record"
    if row.confidence >= 0.7 and row.document_count >= 2 and row.entity_type != "technology":
        return "canonical_candidate"
    return "weak_signal"


def write_fact_quality_report(*, out_dir: Path, fact_rows: list[CandidateFactRow]) -> Path:
    predicate_counts = Counter(row.predicate for row in fact_rows)
    claim_counts = Counter(row.claim_type for row in fact_rows)
    downstream_usable = [row for row in fact_rows if row.confidence >= 0.7]
    evidence_hints = [row for row in fact_rows if row.confidence < 0.7]
    summary = markdown_table(
        ["Metric", "Count"],
        [
            ["Candidate facts", str(len(fact_rows))],
            ["Downstream-usable facts (confidence >= 0.70)", str(len(downstream_usable))],
            ["Evidence hints (confidence < 0.70)", str(len(evidence_hints))],
            ["Predicates", str(len(predicate_counts))],
            ["Claim types", str(len(claim_counts))],
        ],
    )
    predicates = markdown_table(
        ["Predicate", "Count"],
        [[predicate, str(count)] for predicate, count in predicate_counts.most_common(50)],
    )
    low_rows = [
        [
            row.space_key,
            row.page_id,
            row.predicate,
            f"{row.confidence:.2f}",
            row.subject,
            row.object,
        ]
        for row in evidence_hints[:100]
    ]
    low_table = (
        markdown_table(
            ["Space", "Page ID", "Predicate", "Confidence", "Subject", "Object"], low_rows
        )
        if low_rows
        else "No evidence hints found."
    )
    content = f"""# Evidence Hint Quality

Rows in `facts.jsonl` are downstream-usable facts. Rows in `evidence_hints.jsonl` are low
confidence candidate evidence hints and are not trusted structured facts until they meet
downstream confidence and validation gates.

## Summary

{summary}

## Predicate Counts

{predicates}

## Evidence Hints

Rows below confidence 0.70 should be treated as retrieval hints, not trusted structured facts.

{low_table}
"""
    path = out_dir / "fact_quality.md"
    atomic_write_text(path, content)
    return path


def onyx_export_risk_score(row: DocumentIndexRow) -> int:
    score = 0
    if row.audience == "restricted_internal":
        score += 30
    if row.sensitivity not in {"internal", "public"}:
        score += 30
    high_risk_flags = {
        "contains_customer_case_data",
        "contains_email_addresses",
        "contains_log_links",
        "manual_review_required",
        "requires_restricted_audience",
        "not_for_execution_until_verified",
    }
    score += 10 * len(high_risk_flags & set(row.review_flags))
    return score


def onyx_export_risk_reasons(row: DocumentIndexRow) -> list[str]:
    reasons: list[str] = []
    if row.audience == "restricted_internal":
        reasons.append("restricted_audience")
    if row.sensitivity not in {"internal", "public"}:
        reasons.append(row.sensitivity)
    for flag in sorted(row.review_flags):
        if flag in {
            "contains_customer_case_data",
            "contains_email_addresses",
            "contains_log_links",
            "manual_review_required",
            "requires_restricted_audience",
            "not_for_execution_until_verified",
        }:
            reasons.append(flag)
    return reasons


def write_duplicate_candidates_report(
    *, out_dir: Path, document_rows: list[DocumentIndexRow]
) -> Path:
    cluster_rows = duplicate_cluster_rows(document_rows)
    by_hash: dict[str, list[DocumentIndexRow]] = defaultdict(list)
    by_title: dict[str, list[DocumentIndexRow]] = defaultdict(list)
    for row in document_rows:
        by_hash[row.source_content_hash].append(row)
        by_title[normalize_term(row.title)].append(row)
    rows: list[list[str]] = []
    for group_type, groups in (("content_hash", by_hash), ("normalized_title", by_title)):
        for key, group in groups.items():
            if len(group) < 2 or not key:
                continue
            rows.append(
                [
                    group_type,
                    key[:80],
                    str(len(group)),
                    ", ".join(f"{row.space_key}:{row.page_id}" for row in group[:10]),
                ]
            )
    title_tokens = [(row, set(normalize_term(row.title).split())) for row in document_rows]
    for left_index, (left, left_tokens) in enumerate(title_tokens):
        if len(left_tokens) < 2:
            continue
        for right, right_tokens in title_tokens[left_index + 1 :]:
            if len(right_tokens) < 2:
                continue
            similarity = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
            if similarity >= 0.8:
                rows.append(
                    [
                        "near_title",
                        f"similarity={similarity:.2f}",
                        "2",
                        f"{left.space_key}:{left.page_id}, {right.space_key}:{right.page_id}",
                    ]
                )
    for left_index, left in enumerate(document_rows):
        for right in document_rows[left_index + 1 :]:
            if left.text_simhash and right.text_simhash:
                distance = hamming_distance_hex(left.text_simhash, right.text_simhash)
                if distance <= 6:
                    rows.append(
                        [
                            "body_simhash",
                            f"hamming={distance}",
                            "2",
                            f"{left.space_key}:{left.page_id}, {right.space_key}:{right.page_id}",
                        ]
                    )
            if (
                left.heading_simhash
                and right.heading_simhash
                and left.heading_count
                and right.heading_count
            ):
                distance = hamming_distance_hex(left.heading_simhash, right.heading_simhash)
                if distance <= 6:
                    rows.append(
                        [
                            "heading_simhash",
                            f"hamming={distance}",
                            "2",
                            f"{left.space_key}:{left.page_id}, {right.space_key}:{right.page_id}",
                        ]
                    )
    table = (
        markdown_table(["Match type", "Key", "Count", "Documents"], rows[:200])
        if rows
        else "No duplicate candidates found."
    )
    cluster_table = (
        markdown_table(
            [
                "Family",
                "Family type",
                "Versions",
                "Environments",
                "Regions",
                "Count",
                "Canonical candidate",
                "Latest member",
                "Latest production member",
                "Documents",
            ],
            cluster_rows[:100],
        )
        if cluster_rows
        else "No duplicate clusters found."
    )
    content = f"""# Duplicate Candidates

## Clusters

{cluster_table}

## Pairwise Evidence

{table}
"""
    path = out_dir / "duplicate_candidates.md"
    atomic_write_text(path, content)
    return path


def duplicate_cluster_rows(document_rows: list[DocumentIndexRow]) -> list[list[str]]:
    groups: dict[tuple[str, str], list[DocumentIndexRow]] = defaultdict(list)
    for row in document_rows:
        if row.source_content_hash:
            groups[("content_hash", row.source_content_hash)].append(row)
        normalized_title = normalize_term(row.title)
        if normalized_title:
            groups[("normalized_title", normalized_title)].append(row)
    for key, group in near_title_clusters(document_rows).items():
        groups[("near_title_family", key)].extend(group)
    rows: list[list[str]] = []
    seen_clusters: set[tuple[str, ...]] = set()
    for (reason, key), group in sorted(groups.items()):
        if len(group) < 2:
            continue
        doc_keys = tuple(sorted(f"{row.space_key}:{row.page_id}" for row in group))
        if doc_keys in seen_clusters:
            continue
        seen_clusters.add(doc_keys)
        keeper = recommended_duplicate_keeper(group)
        latest = latest_duplicate_member(group)
        latest_prod = latest_production_member(group)
        family = duplicate_family_metadata(group)
        rows.append(
            [
                duplicate_family_label(key, group),
                family["family_type"] if reason == "near_title_family" else reason,
                family["versions"],
                family["environments"],
                family["regions"],
                str(len(group)),
                f"{keeper.space_key}:{keeper.page_id} {keeper.title}",
                f"{latest.space_key}:{latest.page_id} {latest.title}",
                (
                    f"{latest_prod.space_key}:{latest_prod.page_id} {latest_prod.title}"
                    if latest_prod is not None
                    else "-"
                ),
                ", ".join(doc_keys[:20]),
            ]
        )
    return rows


def near_title_clusters(document_rows: list[DocumentIndexRow]) -> dict[str, list[DocumentIndexRow]]:
    parent = list(range(len(document_rows)))
    title_tokens = [(row, set(normalize_term(row.title).split())) for row in document_rows]

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left_index, (_, left_tokens) in enumerate(title_tokens):
        if len(left_tokens) < 3:
            continue
        for right_index in range(left_index + 1, len(title_tokens)):
            right_tokens = title_tokens[right_index][1]
            if len(right_tokens) < 3:
                continue
            similarity = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
            if similarity >= 0.8:
                union(left_index, right_index)
    grouped: dict[int, list[DocumentIndexRow]] = defaultdict(list)
    for index, (row, _) in enumerate(title_tokens):
        grouped[find(index)].append(row)
    clusters: dict[str, list[DocumentIndexRow]] = {}
    for group in grouped.values():
        if len(group) < 3:
            continue
        key = common_title_cluster_key(group)
        clusters[key] = sorted(group, key=lambda row: (row.space_key, row.page_id))
    return clusters


def common_title_cluster_key(group: list[DocumentIndexRow]) -> str:
    token_sets = [set(canonical_title_tokens(row.title)) for row in group]
    common = set.intersection(*token_sets) if token_sets else set()
    if common:
        return " ".join(sorted(common))[:80]
    return normalize_term(group[0].title)[:80]


def canonical_title_tokens(title: str) -> list[str]:
    normalized = normalize_term(title)
    tokens = []
    noisy_tokens = {
        "ppe",
        "prod",
        "production",
        "dev",
        "qa",
        "use1",
        "euw1",
        "apse1",
        "east",
        "west",
        "southeast",
        "us",
        "eu",
        "ap",
        "hotfix",
        "fix",
    }
    for token in normalized.split():
        if token in noisy_tokens:
            continue
        if re.fullmatch(r"v?\d+(?:\.\d+){1,3}", token):
            continue
        tokens.append(token)
    return tokens


def duplicate_family_metadata(group: list[DocumentIndexRow]) -> dict[str, str]:
    versions: set[str] = set()
    environments: set[str] = set()
    regions: set[str] = set()
    for row in group:
        title = row.title.lower()
        versions.update(re.findall(r"\b\d+\.\d+(?:\.\d+)?\b", title))
        if "ppe" in title:
            environments.add("PPE")
        if "prod" in title or "production" in title:
            environments.add("PROD")
        if "dev" in title:
            environments.add("DEV")
        if "qa" in title:
            environments.add("QA")
        for pattern, label in {
            r"\buse1\b|us-east-1": "us-east-1",
            r"\beuw1\b|eu-west-1": "eu-west-1",
            r"\bapse1\b|ap-southeast-1": "ap-southeast-1",
        }.items():
            if re.search(pattern, title):
                regions.add(label)
    family_type = "true_duplicate"
    if len(versions) > 1:
        family_type = "version_series"
    elif len(regions) > 1:
        family_type = "regional_variant"
    elif len(environments) > 1:
        family_type = "environment_variant"
    return {
        "family_type": family_type,
        "versions": ", ".join(sorted(versions)) or "-",
        "environments": ", ".join(sorted(environments)) or "-",
        "regions": ", ".join(sorted(regions)) or "-",
    }


def recommended_duplicate_keeper(group: list[DocumentIndexRow]) -> DocumentIndexRow:
    return sorted(
        group,
        key=lambda row: (
            "deprecated" not in row.status_flags and "archived" not in row.status_flags,
            row.document_type in {"runbook", "support_model", "architecture", "knowledge_article"},
            row.source_updated_at or "",
            row.word_count,
        ),
        reverse=True,
    )[0]


def latest_duplicate_member(group: list[DocumentIndexRow]) -> DocumentIndexRow:
    return sorted(group, key=lambda row: (row.source_updated_at or "", row.page_id), reverse=True)[
        0
    ]


def latest_production_member(group: list[DocumentIndexRow]) -> DocumentIndexRow | None:
    production_rows = [row for row in group if re.search(r"\bprod(?:uction)?\b", row.title, re.I)]
    if not production_rows:
        return None
    return latest_duplicate_member(production_rows)


def duplicate_family_label(key: str, group: list[DocumentIndexRow]) -> str:
    key_lower = key.lower()
    title_values = [row.title.lower() for row in group]
    titles = " ".join(title_values)
    combined = f"{key_lower} {titles}"
    if "ip whitelisting" in combined or "ip whitelist" in combined:
        return "IP Whitelisting installation guides"
    if "account linking service" in combined:
        return "Account Linking Service installation guides"
    admin_count = sum(1 for title in title_values if "scim admin" in title or "admin api" in title)
    scim_api_count = sum(1 for title in title_values if "scim api" in title)
    if admin_count > scim_api_count:
        return "SCIM Admin API document family"
    if "scim api" in combined or "api guide installation scim" in combined:
        return "SCIM API installation guides"
    if "iam scim" in combined:
        return "IAM SCIM API document family"
    words = [word for word in key.split() if not word.isdigit()]
    return " ".join(words).title()[:80] or group[0].title[:80]


def write_llm_usage_report(*, out_dir: Path, usage: list[LLMUsage]) -> Path:
    grouped: dict[tuple[str, str, str], list[LLMUsage]] = defaultdict(list)
    for item in usage:
        grouped[(item.provider, item.model, item.task)].append(item)
    rows = []
    for (provider, model, task), items in sorted(grouped.items()):
        cached = sum(1 for item in items if item.cached)
        hit_rate = cached / len(items) if items else 0
        rows.append(
            [
                provider,
                model,
                task,
                str(len(items)),
                str(len(items) - cached),
                str(sum(item.input_tokens or 0 for item in items)),
                str(sum(item.output_tokens or 0 for item in items)),
                str(sum(item.input_tokens or 0 for item in items if not item.cached)),
                str(sum(item.output_tokens or 0 for item in items if not item.cached)),
                str(cached),
                f"{hit_rate:.0%}",
                str(sum(item.retries for item in items)),
                f"{sum(item.estimated_cost_usd or 0 for item in items):.6f}",
                (
                    "configured"
                    if any(item.estimated_cost_usd is not None for item in items)
                    else "not configured"
                ),
            ]
        )
    table = (
        markdown_table(
            [
                "Provider",
                "Model",
                "Task",
                "Calls",
                "Live calls",
                "Input tokens",
                "Output tokens",
                "Live input tokens",
                "Live output tokens",
                "Cached",
                "Cache hit rate",
                "Retries",
                "Est. cost USD",
                "Cost status",
            ],
            rows,
        )
        if rows
        else "No LLM usage recorded."
    )
    content = f"# LLM Usage\n\n{table}\n"
    path = out_dir / "llm_usage.md"
    atomic_write_text(path, content)
    return path


def write_page_failures_report(*, out_dir: Path, failures: list[PageFailure]) -> Path:
    summary = "No page failures recorded."
    if failures:
        grouped = Counter((failure.stage, failure.error_type) for failure in failures)
        summary = markdown_table(
            ["Stage", "Error type", "Count"],
            [
                [stage, error_type, str(count)]
                for (stage, error_type), count in grouped.most_common(20)
            ],
        )
    rows = [
        [
            failure.space_key,
            failure.page_id,
            failure.stage,
            failure.error_type,
            failure.message[:300],
            failure.suggested_action or "",
        ]
        for failure in failures[:200]
    ]
    table = (
        markdown_table(["Space", "Page ID", "Stage", "Type", "Message", "Action"], rows)
        if rows
        else "No page failures recorded."
    )
    content = f"""# Page Failures

## Summary

{summary}

## Details

{table}
"""
    path = out_dir / "page_failures.md"
    atomic_write_text(path, content)
    return path


def _visual_image_counts(artifact: VisualExtractionArtifact) -> Counter[str]:
    if artifact.images:
        return Counter(image.status for image in artifact.images)
    return Counter(
        {
            "success": artifact.images_succeeded,
            "skipped": artifact.images_skipped,
            "failed": artifact.images_failed,
        }
    )


def _source_host(source: str) -> str:
    parsed = urlparse(source)
    return parsed.hostname or parsed.scheme or "unknown"


def write_visual_extraction_report(
    *,
    out_dir: Path,
    dataset_name: str,
    pages: list[VisualReportPage],
    total_pages: int | None = None,
    document_rows: list[DocumentIndexRow] | None = None,
    quality_rows: list[QualityScoreRow] | None = None,
    low_confidence_threshold: float = 0.75,
) -> Path:
    page_counts: Counter[str] = Counter(page.artifact.status for page in pages)
    image_counts: Counter[str] = Counter()
    capped_rows: list[list[str]] = []
    failed_rows: list[list[str]] = []
    high_value_omitted_rows: list[list[str]] = []
    skipped_remote_counts: Counter[tuple[str, str, str]] = Counter()
    low_confidence_rows: list[list[str]] = []
    images_by_hash: dict[str, list[tuple[VisualReportPage, str, str]]] = defaultdict(list)
    document_by_page_id = {row.page_id: row for row in document_rows or []}
    quality_by_doc = {row.document_id: row.quality_score for row in quality_rows or []}

    for page in pages:
        artifact = page.artifact
        document_row = document_by_page_id.get(artifact.page_id)
        image_counts.update(_visual_image_counts(artifact))
        processed_count = artifact.image_count or len(artifact.images)
        if page.discovered_image_count is not None:
            omitted = max(0, page.discovered_image_count - processed_count)
            if omitted:
                capped_rows.append(
                    [
                        page.artifact.space_key,
                        page.artifact.page_id,
                        str(page.discovered_image_count),
                        str(processed_count),
                        str(omitted),
                        page.title,
                        page.url or "",
                    ]
                )
                if document_row is not None:
                    quality = quality_by_doc.get(document_row.document_id, 0)
                    priority = review_queue_priority(document_row, quality)
                    if priority >= 80 or document_row.document_type in {"runbook", "support_model"}:
                        high_value_omitted_rows.append(
                            [
                                str(priority),
                                artifact.space_key,
                                artifact.page_id,
                                document_row.document_type,
                                document_row.document_subtype or "",
                                str(quality),
                                str(page.discovered_image_count),
                                str(processed_count),
                                str(omitted),
                                page.title,
                                page.url or "",
                            ]
                        )
        for image in artifact.images:
            if image.status == "failed":
                failed_rows.append(
                    [
                        artifact.space_key,
                        artifact.page_id,
                        image.image_id,
                        image.error_type or "unknown",
                        image.source_kind,
                        image.source,
                    ]
                )
            if image.status == "skipped" and image.error_type == "remote_source_not_in_cache":
                skipped_remote_counts[
                    (_source_host(image.source), image.source_kind, image.error_type or "unknown")
                ] += 1
            if (
                image.status == "success"
                and image.confidence is not None
                and image.confidence < low_confidence_threshold
            ):
                low_confidence_rows.append(
                    [
                        f"{image.confidence:.2f}",
                        artifact.space_key,
                        artifact.page_id,
                        image.image_id,
                        image.source_kind,
                        image.source,
                    ]
                )
            if image.content_sha256:
                images_by_hash[image.content_sha256].append((page, image.image_id, image.source))

    capped_rows.sort(key=lambda row: int(row[4]), reverse=True)
    high_value_omitted_rows.sort(key=lambda row: (int(row[0]), int(row[8])), reverse=True)
    failed_rows.sort(key=lambda row: (row[0], row[1], row[2]))
    low_confidence_rows.sort(key=lambda row: float(row[0]))
    duplicate_rows = []
    for content_hash, group in images_by_hash.items():
        if len(group) < 2:
            continue
        duplicate_rows.append(
            [
                content_hash,
                str(len(group)),
                ", ".join(
                    f"{page.artifact.space_key}:{page.artifact.page_id}:{image_id}"
                    for page, image_id, _source in group[:12]
                ),
                ", ".join(source for _page, _image_id, source in group[:4]),
            ]
        )
    duplicate_rows.sort(key=lambda row: int(row[1]), reverse=True)

    page_status_table = markdown_table(
        ["Page status", "Count"],
        [
            [status, str(page_counts.get(status, 0))]
            for status in ["complete", "partial", "skipped", "failed"]
        ],
    )
    image_status_table = markdown_table(
        ["Image status", "Count"],
        [[status, str(image_counts.get(status, 0))] for status in ["success", "skipped", "failed"]],
    )
    capped_table = (
        markdown_table(
            ["Space", "Page ID", "Discovered", "Processed", "Omitted", "Title", "URL"],
            capped_rows[:100],
        )
        if capped_rows
        else "No capped visual pages found."
    )
    high_value_omitted_table = (
        markdown_table(
            [
                "Priority",
                "Space",
                "Page ID",
                "Type",
                "Subtype",
                "Quality",
                "Discovered",
                "Processed",
                "Omitted",
                "Title",
                "URL",
            ],
            high_value_omitted_rows[:100],
        )
        if high_value_omitted_rows
        else "No high-value pages with omitted visual evidence found."
    )
    failed_table = (
        markdown_table(
            ["Space", "Page ID", "Image ID", "Type", "Source kind", "Source"], failed_rows[:200]
        )
        if failed_rows
        else "No failed visual images found."
    )
    skipped_remote_rows = [
        [host, source_kind, error_type, str(count)]
        for (host, source_kind, error_type), count in sorted(
            skipped_remote_counts.items(), key=lambda item: item[1], reverse=True
        )
    ]
    skipped_remote_table = (
        markdown_table(["Host/source", "Source kind", "Error type", "Count"], skipped_remote_rows)
        if skipped_remote_rows
        else "No skipped remote visual images found."
    )
    low_confidence_table = (
        markdown_table(
            ["Confidence", "Space", "Page ID", "Image ID", "Source kind", "Source"],
            low_confidence_rows[:200],
        )
        if low_confidence_rows
        else f"No successful images below confidence {low_confidence_threshold:.2f}."
    )
    duplicate_table = (
        markdown_table(
            ["Content SHA-256", "Count", "Images", "Sample sources"], duplicate_rows[:200]
        )
        if duplicate_rows
        else "No duplicate visual image hashes found."
    )
    pages_with_artifacts = len(pages)
    pages_without_artifacts = max(0, (total_pages or pages_with_artifacts) - pages_with_artifacts)
    coverage_rows = [
        ["Cache pages", str(total_pages if total_pages is not None else "unknown")],
        ["Pages with visual artifacts", str(pages_with_artifacts)],
        ["Pages without visual artifacts", str(pages_without_artifacts)],
        ["Images discovered", str(sum(page.discovered_image_count or 0 for page in pages))],
        [
            "Images in artifacts",
            str(sum(sum(_visual_image_counts(page.artifact).values()) for page in pages)),
        ],
    ]
    content = f"""# Visual Extraction

Dataset: {dataset_name}

## Coverage Summary

{markdown_table(["Metric", "Value"], coverage_rows)}

## Page Status Counts

{page_status_table}

## Image Status Counts

{image_status_table}

## Capped Pages

{capped_table}

## High-Value Pages With Omitted Visuals

{high_value_omitted_table}

## Failed Images

{failed_table}

## Skipped Remote Images

{skipped_remote_table}

## Low Confidence Successful Images

Threshold: {low_confidence_threshold:.2f}

{low_confidence_table}

## Duplicate Image Hashes

{duplicate_table}
"""
    path = out_dir / "visual_extraction.md"
    atomic_write_text(path, content)
    return path


def write_enrichment_reports(
    *,
    out_dir: Path,
    dataset_name: str,
    document_rows: list[DocumentIndexRow],
    quality_rows: list[QualityScoreRow],
    enrichments: list[Enrichment],
    llm_usage: list[LLMUsage] | None = None,
    failures: list[PageFailure] | None = None,
    visual_pages: list[VisualReportPage] | None = None,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    return [
        write_enrichment_summary(
            out_dir=out_dir,
            dataset_name=dataset_name,
            document_rows=document_rows,
            quality_rows=quality_rows,
        ),
        write_document_types_report(out_dir=out_dir, document_rows=document_rows),
        write_stale_or_deprecated_report(out_dir=out_dir, document_rows=document_rows),
        write_high_value_sources_report(
            out_dir=out_dir, document_rows=document_rows, quality_rows=quality_rows
        ),
        write_missing_owners_report(out_dir=out_dir, enrichments=enrichments),
        write_high_value_subtrees_report(out_dir=out_dir, enrichments=enrichments),
        write_attachment_followups_report(out_dir=out_dir, document_rows=document_rows),
        write_duplicate_candidates_report(out_dir=out_dir, document_rows=document_rows),
        write_llm_usage_report(out_dir=out_dir, usage=llm_usage or []),
        write_llm_failures_report(out_dir=out_dir, enrichments=enrichments),
        write_page_failures_report(out_dir=out_dir, failures=failures or []),
        write_visual_extraction_report(
            out_dir=out_dir, dataset_name=dataset_name, pages=visual_pages or []
        ),
    ]
