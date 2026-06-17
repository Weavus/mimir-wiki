from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from mimir_wiki.cache_reader import ValidationResult
from mimir_wiki.schemas import DocumentIndexRow, Enrichment, LLMUsage, PageFailure, QualityScoreRow
from mimir_wiki.utils import atomic_write_text, hamming_distance_hex, normalize_term


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(cell.replace("\n", " ") for cell in row) + " |")
    return "\n".join(lines)


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
) -> Path:
    average_quality = 0
    if quality_rows:
        average_quality = round(sum(row.quality_score for row in quality_rows) / len(quality_rows))
    stale_count = sum(
        1
        for row in document_rows
        if any(flag in row.status_flags for flag in ("stale", "deprecated", "archived"))
    )
    content = f"""# Enrichment Summary

Dataset: {dataset_name}

## Summary

- Documents: {len(document_rows)}
- Average quality score: {average_quality}
- Good or better documents: {sum(1 for row in quality_rows if row.quality_score >= 70)}
- Stale/deprecated/archive documents: {stale_count}
"""
    path = out_dir / "enrichment_summary.md"
    atomic_write_text(path, content)
    return path


def write_document_types_report(*, out_dir: Path, document_rows: list[DocumentIndexRow]) -> Path:
    counts = Counter(row.document_type for row in document_rows)
    rows = [[document_type, str(count)] for document_type, count in sorted(counts.items())]
    table = markdown_table(["Document type", "Count"], rows)
    content = f"# Document Types\n\n{table}\n"
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
    high_value = sorted(
        document_rows,
        key=lambda row: (
            quality_by_doc.get(row.document_id, 0),
            row.outbound_link_count,
            row.word_count,
        ),
        reverse=True,
    )[:100]
    rows = [
        [
            row.space_key,
            row.page_id,
            str(quality_by_doc.get(row.document_id, 0)),
            row.document_type,
            row.title,
            row.url or "",
        ]
        for row in high_value
    ]
    table = markdown_table(["Space", "Page ID", "Quality", "Type", "Title", "URL"], rows)
    content = f"# High Value Sources\n\n{table}\n"
    path = out_dir / "high_value_sources.md"
    atomic_write_text(path, content)
    return path


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


def write_duplicate_candidates_report(
    *, out_dir: Path, document_rows: list[DocumentIndexRow]
) -> Path:
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
    content = f"# Duplicate Candidates\n\n{table}\n"
    path = out_dir / "duplicate_candidates.md"
    atomic_write_text(path, content)
    return path


def write_llm_usage_report(*, out_dir: Path, usage: list[LLMUsage]) -> Path:
    grouped: dict[tuple[str, str, str], list[LLMUsage]] = defaultdict(list)
    for item in usage:
        grouped[(item.provider, item.model, item.task)].append(item)
    rows = []
    for (provider, model, task), items in sorted(grouped.items()):
        rows.append(
            [
                provider,
                model,
                task,
                str(len(items)),
                str(sum(item.input_tokens or 0 for item in items)),
                str(sum(item.output_tokens or 0 for item in items)),
                str(sum(1 for item in items if item.cached)),
                str(sum(item.retries for item in items)),
                f"{sum(item.estimated_cost_usd or 0 for item in items):.6f}",
            ]
        )
    table = (
        markdown_table(
            [
                "Provider",
                "Model",
                "Task",
                "Calls",
                "Input tokens",
                "Output tokens",
                "Cached",
                "Retries",
                "Est. cost USD",
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
    content = f"# Page Failures\n\n{table}\n"
    path = out_dir / "page_failures.md"
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
        write_attachment_followups_report(out_dir=out_dir, document_rows=document_rows),
        write_duplicate_candidates_report(out_dir=out_dir, document_rows=document_rows),
        write_llm_usage_report(out_dir=out_dir, usage=llm_usage or []),
        write_page_failures_report(out_dir=out_dir, failures=failures or []),
    ]
