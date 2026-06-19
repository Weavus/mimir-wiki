from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from pydantic import ValidationError

from mimir_wiki.cache_reader import CacheReader, PageBundle
from mimir_wiki.config import AppConfig
from mimir_wiki.constants import EXIT_PARTIAL_SUCCESS, EXIT_SUCCESS, EXIT_USER_ERROR
from mimir_wiki.enrichers.deterministic import enrich_page, refreshed_for_run, signature_matches
from mimir_wiki.enrichers.llm import apply_llm_enrichment, enabled_llm_tasks
from mimir_wiki.hierarchy import build_hierarchy_context, build_tree_counts
from mimir_wiki.llm.base import LLMError, LLMProvider, provider_for_config
from mimir_wiki.reports import write_cache_validation_report, write_enrichment_reports
from mimir_wiki.schemas import (
    DocumentIndexRow,
    Enrichment,
    HierarchyContext,
    LLMUsage,
    PageFailure,
    QualityScoreRow,
    RunSummary,
    WarningRecord,
)
from mimir_wiki.utils import atomic_write_json, atomic_write_jsonl, load_jsonl, new_run_id, utc_now
from mimir_wiki.visual_extraction import (
    discover_visual_sources,
    load_visual_extraction,
    run_extract_visuals_for_page,
    visual_extraction_path,
)
from mimir_wiki.writers.artifacts import (
    aggregate_candidate_entity_rows,
    aggregate_candidate_fact_rows,
    aggregate_concept_rows,
    aggregate_theme_rows,
    document_index_row,
    load_enrichment,
    quality_score_row,
    write_enrichment,
    write_global_jsonl,
)
from mimir_wiki.writers.onyx_markdown import write_onyx_markdown


@dataclass
class CommandResult:
    summary: RunSummary
    failures: list[PageFailure] = field(default_factory=list)
    warnings: list[WarningRecord] = field(default_factory=list)
    output_paths: list[Path] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return self.summary.exit_code

    def to_dict(self) -> dict[str, Any]:
        data = self.summary.model_dump(mode="json")
        data["failures"] = [failure.model_dump(mode="json") for failure in self.failures]
        data["warnings"] = [warning.model_dump(mode="json") for warning in self.warnings]
        data["output_paths"] = [str(path) for path in self.output_paths]
        return data


@dataclass
class RunContext:
    command: str
    config: AppConfig
    cache_path: Path | None
    dataset_name: str | None
    profile: str | None
    dry_run: bool
    run_id: str = field(init=False)
    started_at: str = field(init=False)
    generated_at: str = field(init=False)
    start_monotonic: float = field(init=False)
    failures: list[PageFailure] = field(default_factory=list)
    warnings: list[WarningRecord] = field(default_factory=list)
    llm_usage: list[LLMUsage] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    files_written: int = 0
    llm_retries: int = 0

    def __post_init__(self) -> None:
        self.run_id = new_run_id(self.command)
        self.started_at = utc_now()
        self.generated_at = self.started_at
        self.start_monotonic = time.monotonic()

    @property
    def runs_dir(self) -> Path:
        return Path(self.config.paths.runs) / self.run_id

    def page_failure(
        self,
        *,
        bundle: PageBundle,
        stage: str,
        error_type: str,
        message: str,
        retryable: bool = False,
        attempts: int = 1,
        suggested_action: str | None = None,
    ) -> None:
        self.failures.append(
            PageFailure(
                run_id=self.run_id,
                dataset_name=self.dataset_name or "unknown",
                generated_at=utc_now(),
                document_id=bundle.document_id,
                page_id=bundle.metadata.page_id,
                space_key=bundle.metadata.space_key,
                title=bundle.metadata.title,
                source_updated_at=bundle.metadata.updated_at,
                source_content_hash=bundle.source_content_hash,
                stage=stage,
                error_type=error_type,
                message=message,
                retryable=retryable,
                attempts=attempts,
                suggested_action=suggested_action,
            )
        )

    def build_summary(
        self,
        *,
        status: str,
        exit_code: int,
        counts: dict[str, int],
        output_paths: list[Path],
    ) -> RunSummary:
        finished_at = utc_now()
        counts = dict(counts)
        counts.setdefault("warnings", len(self.warnings))
        counts.setdefault("files_written", self.files_written)
        return RunSummary(
            run_id=self.run_id,
            generated_at=finished_at,
            command=self.command,
            started_at=self.started_at,
            finished_at=finished_at,
            elapsed_seconds=round(time.monotonic() - self.start_monotonic, 3),
            status=status,  # type: ignore[arg-type]
            exit_code=exit_code,
            dataset_name=self.dataset_name or "unknown",
            cache_path=str(self.cache_path) if self.cache_path else None,
            config_profile=self.profile,
            resolved_config=self.config.non_secret_dict(),
            counts=counts,
            outputs={
                **self.outputs,
                "run": str(self.runs_dir),
                **{path.name: str(path) for path in output_paths},
            },
        )

    def write_run_artifacts(
        self, summary: RunSummary, event_callback: Callable[[dict[str, Any]], None] | None = None
    ) -> int:
        if self.dry_run:
            return 0
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        summary_path = self.runs_dir / "summary.json"
        failures_path = self.runs_dir / "page_failures.jsonl"
        warnings_path = self.runs_dir / "warnings.jsonl"
        usage_path = self.runs_dir / "llm_usage.jsonl"
        atomic_write_json(summary_path, summary.model_dump(mode="json"))
        _artifact_event(
            event_callback,
            run_id=self.run_id,
            artifact_type="run_summary",
            path=summary_path,
        )
        atomic_write_jsonl(
            failures_path,
            [failure.model_dump(mode="json") for failure in self.failures],
        )
        _artifact_event(
            event_callback,
            run_id=self.run_id,
            artifact_type="page_failures",
            path=failures_path,
        )
        atomic_write_jsonl(
            warnings_path,
            [warning.model_dump(mode="json") for warning in self.warnings],
        )
        _artifact_event(
            event_callback,
            run_id=self.run_id,
            artifact_type="warnings",
            path=warnings_path,
        )
        atomic_write_jsonl(
            usage_path,
            [usage.model_dump(mode="json") for usage in self.llm_usage],
        )
        _artifact_event(
            event_callback,
            run_id=self.run_id,
            artifact_type="llm_usage",
            path=usage_path,
        )
        return 4


@dataclass
class PageProcessResult:
    page_id: str
    enrichment: Enrichment | None = None
    document_row: DocumentIndexRow | None = None
    quality_row: QualityScoreRow | None = None
    failures: list[PageFailure] = field(default_factory=list)
    warnings: list[WarningRecord] = field(default_factory=list)
    llm_usage: list[LLMUsage] = field(default_factory=list)
    output_paths: list[Path] = field(default_factory=list)
    files_written: int = 0
    llm_retries: int = 0
    processed: int = 0
    skipped: int = 0
    filtered: bool = False


def _artifact_event(
    event_callback: Callable[[dict[str, Any]], None] | None,
    *,
    run_id: str,
    artifact_type: str,
    path: Path,
    page_id: str | None = None,
    space_key: str | None = None,
) -> None:
    if event_callback is None:
        return
    event_callback(
        {
            "event": "artifact_written",
            "run_id": run_id,
            "artifact_type": artifact_type,
            "path": str(path),
            "page_id": page_id,
            "space_key": space_key,
        }
    )


def resolve_cache_path(config: AppConfig, cache: Path | None = None) -> Path:
    selected = cache or (Path(config.paths.cache) if config.paths.cache else None)
    if selected is None:
        raise ValueError("--cache is required or paths.cache must be configured")
    return selected


def validate_cache_command(
    *,
    config: AppConfig,
    cache_path: Path,
    profile: str | None,
    dry_run: bool,
    limit: int | None = None,
    event_callback: Callable[[dict[str, Any]], None] | None = None,
) -> CommandResult:
    reader = CacheReader(cache_path)
    result = reader.validate(limit=limit)
    dataset_name = result.dataset_name
    context = RunContext("validate-cache", config, cache_path, dataset_name, profile, dry_run)
    output_paths: list[Path] = []
    if not dry_run:
        reports_dir = Path(config.paths.reports)
        output_paths.append(write_cache_validation_report(result, reports_dir))
        context.files_written += 1
        context.outputs["reports"] = str(reports_dir)
        _artifact_event(
            event_callback,
            run_id=context.run_id,
            artifact_type="cache_validation_report",
            path=output_paths[-1],
        )
    exit_code = EXIT_SUCCESS if result.ok else EXIT_USER_ERROR
    status = "success" if result.ok else "failed"
    counts = {
        "pages_total": result.pages_total,
        "pages_considered": result.pages_total,
        "pages_processed": result.pages_valid,
        "pages_skipped_unchanged": 0,
        "pages_failed": result.pages_failed,
        "export_errors": result.export_errors,
    }
    summary = context.build_summary(
        status=status, exit_code=exit_code, counts=counts, output_paths=output_paths
    )
    context.files_written += context.write_run_artifacts(summary, event_callback)
    return CommandResult(summary=summary, warnings=context.warnings, output_paths=output_paths)


def _load_existing_enrichment(path: Path) -> Enrichment | None:
    if not path.exists():
        return None
    try:
        return load_enrichment(path)
    except (OSError, ValueError, ValidationError):
        return None


def _process_page(
    *,
    bundle: PageBundle,
    config: AppConfig,
    provider: LLMProvider | None,
    run_id: str,
    dataset_name: str,
    generated_at: str,
    dry_run: bool,
    changed_only: bool,
    force: bool,
    document_type_filter: str | None,
    onyx_root: Path,
    event_callback: Callable[[dict[str, Any]], None] | None,
    llm_progress_callback: Callable[[dict[str, Any]], None] | None,
    hierarchy: HierarchyContext | None,
) -> PageProcessResult:
    result = PageProcessResult(page_id=bundle.metadata.page_id)
    enrichment_path = bundle.paths.root / "enrichment.json"
    if event_callback:
        event_callback(
            {
                "event": "page_started",
                "run_id": run_id,
                "page_id": bundle.metadata.page_id,
                "space_key": bundle.metadata.space_key,
                "title": bundle.metadata.title,
            }
        )
    try:
        existing = _load_existing_enrichment(enrichment_path)
        unchanged = (
            changed_only
            and not force
            and existing is not None
            and signature_matches(existing, bundle, config)
        )
        if unchanged:
            assert existing is not None
            enrichment = refreshed_for_run(
                existing,
                run_id=run_id,
                generated_at=generated_at,
                dataset_name=dataset_name,
            )
            result.skipped = 1
        else:
            enrichment = enrich_page(
                bundle,
                run_id=run_id,
                dataset_name=dataset_name,
                config=config,
                generated_at=generated_at,
                hierarchy=hierarchy,
            )
            if provider is not None:
                llm_result = apply_llm_enrichment(
                    bundle=bundle,
                    enrichment=enrichment,
                    config=config,
                    run_id=run_id,
                    dataset_name=dataset_name,
                    generated_at=generated_at,
                    provider=provider,
                    event_callback=event_callback,
                    progress_callback=llm_progress_callback,
                )
                enrichment = llm_result.enrichment
                result.llm_usage.extend(llm_result.usage)
                result.failures.extend(llm_result.failures)
                result.warnings.extend(llm_result.warnings)
                result.llm_retries += llm_result.retries
            if document_type_filter and enrichment.document_type != document_type_filter:
                result.filtered = True
                return result
            result.processed = 1
            if not dry_run and config.features.outputs.enrichment_json:
                write_enrichment(enrichment_path, enrichment)
                result.files_written += 1
                result.output_paths.append(enrichment_path)
                _artifact_event(
                    event_callback,
                    run_id=run_id,
                    artifact_type="enrichment_json",
                    path=enrichment_path,
                    page_id=bundle.metadata.page_id,
                    space_key=bundle.metadata.space_key,
                )
            if (
                not dry_run
                and config.features.outputs.onyx_poc_markdown
                and config.onyx_poc.emit_enriched_markdown
            ):
                path, warning_records = write_onyx_markdown(
                    root=onyx_root,
                    dataset_name=dataset_name,
                    bundle=bundle,
                    enrichment=enrichment,
                    config=config,
                    generated_at=generated_at,
                    run_id=run_id,
                )
                result.files_written += 1
                result.warnings.extend(warning_records)
                result.output_paths.append(path)
                _artifact_event(
                    event_callback,
                    run_id=run_id,
                    artifact_type="onyx_markdown",
                    path=path,
                    page_id=bundle.metadata.page_id,
                    space_key=bundle.metadata.space_key,
                )
        result.enrichment = enrichment
        result.document_row = document_index_row(
            bundle,
            enrichment,
            generated_at=generated_at,
            run_id=run_id,
            dataset_name=dataset_name,
        )
        result.quality_row = quality_score_row(
            enrichment,
            generated_at=generated_at,
            run_id=run_id,
            dataset_name=dataset_name,
        )
        if event_callback:
            event_callback(
                {
                    "event": "page_finished",
                    "run_id": run_id,
                    "page_id": bundle.metadata.page_id,
                    "space_key": bundle.metadata.space_key,
                    "title": bundle.metadata.title,
                    "processed": not unchanged,
                    "skipped": unchanged,
                    "document_type": enrichment.document_type,
                    "quality_score": enrichment.quality.overall_score,
                    "warnings": len(enrichment.warnings),
                }
            )
    except Exception as exc:
        failure = PageFailure(
            run_id=run_id,
            dataset_name=dataset_name,
            generated_at=utc_now(),
            document_id=bundle.document_id,
            page_id=bundle.metadata.page_id,
            space_key=bundle.metadata.space_key,
            title=bundle.metadata.title,
            source_updated_at=bundle.metadata.updated_at,
            source_content_hash=bundle.source_content_hash,
            stage="enrich",
            error_type=type(exc).__name__,
            message=str(exc),
            retryable=False,
            suggested_action="Inspect the source page artifact and rerun this page.",
        )
        result.failures.append(failure)
        if event_callback:
            event_callback(
                {
                    "event": "page_failed",
                    "run_id": run_id,
                    "page_id": bundle.metadata.page_id,
                    "space_key": bundle.metadata.space_key,
                    "title": bundle.metadata.title,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
    return result


def enrich_command(
    *,
    config: AppConfig,
    cache_path: Path,
    profile: str | None,
    dry_run: bool,
    limit: int | None = None,
    changed_only: bool = False,
    force: bool = False,
    document_type_filter: str | None = None,
    space_filter: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    event_callback: Callable[[dict[str, Any]], None] | None = None,
) -> CommandResult:
    reader = CacheReader(cache_path)
    validation = reader.validate(limit=limit)
    dataset_name = validation.dataset_name or cache_path.name
    context = RunContext("enrich", config, cache_path, dataset_name, profile, dry_run)
    if not validation.ok:
        summary = context.build_summary(
            status="failed",
            exit_code=EXIT_USER_ERROR,
            counts={
                "pages_total": validation.pages_total,
                "pages_considered": 0,
                "pages_processed": 0,
                "pages_skipped_unchanged": 0,
                "pages_failed": validation.pages_failed,
            },
            output_paths=[],
        )
        context.write_run_artifacts(summary, event_callback)
        return CommandResult(summary=summary)

    pages = reader.iter_pages(limit=limit, space_filter=space_filter)
    child_counts, sibling_group_counts = build_tree_counts(pages)
    hierarchy_by_page_id = {
        page.metadata.page_id: build_hierarchy_context(
            page,
            child_count=child_counts.get(page.metadata.page_id, 0),
            sibling_count=sibling_group_counts.get(
                (page.metadata.ancestors[-1].id if page.metadata.ancestors else None) or "__root__",
                1,
            ),
        )
        for page in pages
    }
    enrichments: list[Enrichment] = []
    document_rows: list[DocumentIndexRow] = []
    quality_rows: list[QualityScoreRow] = []
    processed = 0
    skipped = 0
    considered = 0
    llm_calls_planned = 0
    llm_calls_completed = 0
    llm_cached_calls_progress = 0
    llm_current_task = "-"
    llm_current_page = "-"
    output_paths: list[Path] = []
    knowledge_dir = Path(config.paths.knowledge)
    onyx_root = Path(config.paths.dist_onyx_enriched)
    reports_dir = Path(config.paths.reports)
    cancelled = False
    context.outputs.update(
        {
            "knowledge": str(knowledge_dir),
            "onyx_enriched": str(onyx_root),
            "reports": str(reports_dir),
        }
    )
    provider: LLMProvider | None = None
    if enabled_llm_tasks(config):
        try:
            provider = provider_for_config(config)
        except LLMError as exc:
            summary = context.build_summary(
                status="failed",
                exit_code=EXIT_USER_ERROR,
                counts={
                    "pages_total": validation.pages_total,
                    "pages_considered": len(pages),
                    "pages_processed": 0,
                    "pages_skipped_unchanged": 0,
                    "pages_failed": 0,
                    "llm_retries": 0,
                    "llm_calls": 0,
                    "warnings": 0,
                },
                output_paths=[],
            )
            context.warnings.append(
                WarningRecord(
                    run_id=context.run_id,
                    dataset_name=dataset_name,
                    generated_at=context.generated_at,
                    warning_type=exc.error_type,
                    message=str(exc),
                    stage="llm.provider_config",
                )
            )
            context.write_run_artifacts(summary, event_callback)
            return CommandResult(summary=summary, warnings=context.warnings)

    progress_lock = Lock()

    def emit_progress() -> None:
        if progress_callback:
            progress_callback(
                {
                    "total": len(pages),
                    "considered": considered,
                    "processed": processed,
                    "skipped": skipped,
                    "failed": len(context.failures),
                    "llm_retries": context.llm_retries,
                    "llm_calls_planned": llm_calls_planned,
                    "llm_calls_completed": llm_calls_completed,
                    "llm_cached_calls": llm_cached_calls_progress,
                    "llm_current_task": llm_current_task,
                    "llm_current_page": llm_current_page,
                }
            )

    emit_progress()

    def llm_progress_callback(event: dict[str, Any]) -> None:
        nonlocal llm_cached_calls_progress, llm_calls_completed, llm_calls_planned
        nonlocal llm_current_page, llm_current_task
        with progress_lock:
            event_name = event.get("event")
            if event_name == "llm_plan":
                llm_calls_planned += int(event.get("calls_planned") or 0)
                llm_current_page = str(event.get("page_id") or "-")
            elif event_name == "llm_call_started":
                llm_current_task = str(event.get("task") or "-")
                llm_current_page = str(event.get("page_id") or "-")
            elif event_name in {"llm_call_finished", "llm_call_failed"}:
                llm_calls_completed += 1
                if event.get("cached") is True:
                    llm_cached_calls_progress += 1
                llm_current_task = str(event.get("task") or "-")
                llm_current_page = str(event.get("page_id") or "-")
            emit_progress()

    worker_count = max(1, config.processing.page_workers)
    if provider is not None:
        worker_count = max(1, min(worker_count, config.processing.llm_workers))

    def merge_page_result(page_result: PageProcessResult) -> None:
        nonlocal processed, skipped, considered
        with progress_lock:
            considered += 1
            processed += page_result.processed
            skipped += page_result.skipped
            context.files_written += page_result.files_written
            context.llm_retries += page_result.llm_retries
            context.failures.extend(page_result.failures)
            context.warnings.extend(page_result.warnings)
            context.llm_usage.extend(page_result.llm_usage)
            output_paths.extend(page_result.output_paths)
            if page_result.enrichment is not None:
                enrichments.append(page_result.enrichment)
            if page_result.document_row is not None:
                document_rows.append(page_result.document_row)
            if page_result.quality_row is not None:
                quality_rows.append(page_result.quality_row)
            emit_progress()

    executor = ThreadPoolExecutor(max_workers=worker_count)
    futures = [
        executor.submit(
            _process_page,
            bundle=bundle,
            config=config,
            provider=provider,
            run_id=context.run_id,
            dataset_name=dataset_name,
            generated_at=context.generated_at,
            dry_run=dry_run,
            changed_only=changed_only,
            force=force,
            document_type_filter=document_type_filter,
            onyx_root=onyx_root,
            event_callback=event_callback,
            llm_progress_callback=llm_progress_callback if provider is not None else None,
            hierarchy=hierarchy_by_page_id.get(bundle.metadata.page_id),
        )
        for bundle in pages
    ]
    pending = set(futures)
    merged = set()
    try:
        for future in as_completed(futures):
            pending.discard(future)
            merge_page_result(future.result())
            merged.add(future)
            if config.processing.fail_fast and context.failures:
                for pending_future in pending:
                    pending_future.cancel()
                break
    except KeyboardInterrupt:
        cancelled = True
        for pending_future in pending:
            pending_future.cancel()
        context.warnings.append(
            WarningRecord(
                run_id=context.run_id,
                dataset_name=dataset_name,
                generated_at=utc_now(),
                warning_type="run_cancelled",
                message=(
                    "Cancellation requested; pending pages were cancelled and completed "
                    "page work was preserved."
                ),
                stage="enrich",
            )
        )
        if event_callback:
            event_callback(
                {
                    "event": "run_cancelled",
                    "run_id": context.run_id,
                    "pending_pages": sum(1 for future in pending if not future.done()),
                    "completed_pages": considered,
                }
            )
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    if cancelled:
        for future in futures:
            if future in merged or future.cancelled() or not future.done():
                continue
            try:
                merge_page_result(future.result())
                merged.add(future)
            except KeyboardInterrupt:
                continue
    else:
        for future in futures:
            if future in merged or future.cancelled() or not future.done():
                continue
            merge_page_result(future.result())
            merged.add(future)

    enrichments.sort(key=lambda item: (item.space_key, item.page_id))
    document_rows.sort(key=lambda item: (item.space_key, item.page_id))
    quality_rows.sort(key=lambda item: (item.space_key, item.page_id))
    output_paths.sort(key=lambda path: str(path))

    theme_rows = aggregate_theme_rows(
        enrichments,
        generated_at=context.generated_at,
        run_id=context.run_id,
        dataset_name=dataset_name,
    )
    concept_rows = aggregate_concept_rows(
        enrichments,
        generated_at=context.generated_at,
        run_id=context.run_id,
        dataset_name=dataset_name,
    )
    entity_rows = aggregate_candidate_entity_rows(
        enrichments,
        generated_at=context.generated_at,
        run_id=context.run_id,
        dataset_name=dataset_name,
    )
    fact_rows = aggregate_candidate_fact_rows(
        enrichments,
        generated_at=context.generated_at,
        run_id=context.run_id,
        dataset_name=dataset_name,
    )
    if not dry_run:
        context.files_written += write_global_jsonl(
            knowledge_dir=knowledge_dir,
            document_rows=document_rows,
            quality_rows=quality_rows,
            theme_rows=theme_rows,
            concept_rows=concept_rows,
            candidate_entity_rows=entity_rows,
            candidate_fact_rows=fact_rows,
        )
        output_paths.extend(
            [
                knowledge_dir / "document_index.jsonl",
                knowledge_dir / "quality_scores.jsonl",
                knowledge_dir / "themes.jsonl",
                knowledge_dir / "concepts.jsonl",
                knowledge_dir / "candidate_entities.jsonl",
                knowledge_dir / "facts.jsonl",
            ]
        )
        for path in output_paths[-6:]:
            _artifact_event(
                event_callback,
                run_id=context.run_id,
                artifact_type="knowledge_jsonl",
                path=path,
            )
        if config.features.outputs.reports:
            report_paths = write_enrichment_reports(
                out_dir=reports_dir,
                dataset_name=dataset_name,
                document_rows=document_rows,
                quality_rows=quality_rows,
                enrichments=enrichments,
                llm_usage=context.llm_usage,
                failures=context.failures,
            )
            context.files_written += len(report_paths)
            output_paths.extend(report_paths)
            for path in report_paths:
                _artifact_event(
                    event_callback,
                    run_id=context.run_id,
                    artifact_type="report",
                    path=path,
                )
            cache_report = write_cache_validation_report(validation, reports_dir)
            context.files_written += 1
            output_paths.append(cache_report)
            _artifact_event(
                event_callback,
                run_id=context.run_id,
                artifact_type="cache_validation_report",
                path=cache_report,
            )

    exit_code = EXIT_PARTIAL_SUCCESS if context.failures or cancelled else EXIT_SUCCESS
    status = "partial_success" if context.failures or cancelled else "success"
    llm_cached_calls = sum(1 for usage in context.llm_usage if usage.cached)
    llm_live_calls = len(context.llm_usage) - llm_cached_calls
    counts = {
        "pages_total": validation.pages_total,
        "pages_considered": considered,
        "pages_processed": processed,
        "pages_skipped_unchanged": skipped,
        "pages_failed": len(context.failures),
        "changed_pages": processed,
        "unchanged_pages": skipped,
        "warnings": len(context.warnings),
        "llm_calls": len(context.llm_usage),
        "llm_cached_calls": llm_cached_calls,
        "llm_live_calls": llm_live_calls,
        "llm_retries": context.llm_retries,
        "pages_cancelled": max(0, len(pages) - considered) if cancelled else 0,
    }
    summary = context.build_summary(
        status=status, exit_code=exit_code, counts=counts, output_paths=output_paths
    )
    context.files_written += context.write_run_artifacts(summary, event_callback)
    return CommandResult(
        summary=summary,
        failures=context.failures,
        warnings=context.warnings,
        output_paths=output_paths,
    )


def extract_visuals_command(
    *,
    config: AppConfig,
    cache_path: Path,
    profile: str | None,
    dry_run: bool,
    limit: int | None = None,
    force: bool = False,
    space_filter: str | None = None,
    event_callback: Callable[[dict[str, Any]], None] | None = None,
    llm_transport: Any | None = None,
) -> CommandResult:
    reader = CacheReader(cache_path)
    validation = reader.validate(limit=limit)
    dataset_name = validation.dataset_name or cache_path.name
    context = RunContext("extract-visuals", config, cache_path, dataset_name, profile, dry_run)
    output_paths: list[Path] = []
    if not validation.ok:
        summary = context.build_summary(
            status="failed",
            exit_code=EXIT_USER_ERROR,
            counts={
                "pages_total": validation.pages_total,
                "pages_considered": 0,
                "pages_processed": 0,
                "pages_skipped_unchanged": 0,
                "pages_failed": validation.pages_failed,
            },
            output_paths=[],
        )
        context.write_run_artifacts(summary, event_callback)
        return CommandResult(summary=summary)

    pages = reader.iter_pages(limit=limit, space_filter=space_filter)
    considered = 0
    processed = 0
    skipped = 0
    images_discovered = 0
    images_extracted = 0
    images_failed = 0
    for bundle in pages:
        sources = discover_visual_sources(
            bundle, max_images=config.visual_extraction.max_images_per_page
        )
        if not sources:
            skipped += 1
            continue
        considered += 1
        images_discovered += len(sources)
        existing = load_visual_extraction(bundle)
        if (
            not force
            and existing is not None
            and existing.status == "complete"
            and existing.source_content_hash == bundle.source_content_hash
        ):
            skipped += 1
            images_extracted += existing.images_succeeded
            continue
        if dry_run:
            processed += 1
            continue
        try:
            artifact, files_written = run_extract_visuals_for_page(
                bundle=bundle,
                config=config,
                run_id=context.run_id,
                dataset_name=dataset_name,
                generated_at=context.generated_at,
                dry_run=False,
                llm_transport=llm_transport,
            )
        except Exception as exc:
            context.page_failure(
                bundle=bundle,
                stage="extract-visuals",
                error_type=type(exc).__name__,
                message=str(exc),
                suggested_action=(
                    "Check visual extraction provider credentials and source image access."
                ),
            )
            images_failed += len(sources)
            continue
        processed += 1
        context.files_written += files_written
        images_extracted += artifact.images_succeeded
        images_failed += artifact.images_failed
        path = visual_extraction_path(bundle)
        output_paths.append(path)
        _artifact_event(
            event_callback,
            run_id=context.run_id,
            artifact_type="visual_extraction",
            path=path,
            page_id=bundle.metadata.page_id,
            space_key=bundle.metadata.space_key,
        )
    output_paths.sort(key=lambda path: str(path))
    exit_code = EXIT_PARTIAL_SUCCESS if context.failures else EXIT_SUCCESS
    status = "partial_success" if context.failures else "success"
    summary = context.build_summary(
        status=status,
        exit_code=exit_code,
        counts={
            "pages_total": validation.pages_total,
            "pages_considered": considered,
            "pages_processed": processed,
            "pages_skipped_unchanged": skipped,
            "pages_failed": len(context.failures),
            "visual_images_discovered": images_discovered,
            "visual_images_extracted": images_extracted,
            "visual_images_failed": images_failed,
        },
        output_paths=output_paths,
    )
    context.files_written += context.write_run_artifacts(summary, event_callback)
    return CommandResult(summary=summary, failures=context.failures, output_paths=output_paths)


def _read_document_rows(path: Path) -> list[DocumentIndexRow]:
    return [DocumentIndexRow.model_validate(row) for row in load_jsonl(path)]


def _read_quality_rows(path: Path) -> list[QualityScoreRow]:
    return [QualityScoreRow.model_validate(row) for row in load_jsonl(path)]


def _read_run_llm_usage(runs_dir: Path) -> list[LLMUsage]:
    usage: list[LLMUsage] = []
    if not runs_dir.exists():
        return usage
    for path in sorted(runs_dir.glob("*/llm_usage.jsonl")):
        usage.extend(LLMUsage.model_validate(row) for row in load_jsonl(path))
    return usage


def _read_run_failures(runs_dir: Path) -> list[PageFailure]:
    failures: list[PageFailure] = []
    if not runs_dir.exists():
        return failures
    for path in sorted(runs_dir.glob("*/page_failures.jsonl")):
        failures.extend(PageFailure.model_validate(row) for row in load_jsonl(path))
    return failures


def _read_enrichments(reader: CacheReader, limit: int | None) -> list[Enrichment]:
    enrichments: list[Enrichment] = []
    for manifest in reader.load_manifest()[:limit]:
        path = reader.page_paths(manifest).root / "enrichment.json"
        existing = _load_existing_enrichment(path)
        if existing is not None:
            enrichments.append(existing)
    return enrichments


def report_command(
    *,
    config: AppConfig,
    cache_path: Path,
    profile: str | None,
    dry_run: bool,
    limit: int | None = None,
    event_callback: Callable[[dict[str, Any]], None] | None = None,
) -> CommandResult:
    reader = CacheReader(cache_path)
    validation = reader.validate(limit=limit)
    dataset_name = validation.dataset_name or cache_path.name
    context = RunContext("report", config, cache_path, dataset_name, profile, dry_run)
    knowledge_dir = Path(config.paths.knowledge)
    reports_dir = Path(config.paths.reports)
    output_paths: list[Path] = []
    document_index = knowledge_dir / "document_index.jsonl"
    quality_scores = knowledge_dir / "quality_scores.jsonl"
    if document_index.exists() and quality_scores.exists():
        document_rows = _read_document_rows(document_index)
        quality_rows = _read_quality_rows(quality_scores)
    else:
        document_rows = []
        quality_rows = []
    enrichments = _read_enrichments(reader, limit)
    llm_usage = _read_run_llm_usage(Path(config.paths.runs))
    failures = _read_run_failures(Path(config.paths.runs))
    if not dry_run:
        output_paths.append(write_cache_validation_report(validation, reports_dir))
        _artifact_event(
            event_callback,
            run_id=context.run_id,
            artifact_type="cache_validation_report",
            path=output_paths[-1],
        )
        report_paths = write_enrichment_reports(
            out_dir=reports_dir,
            dataset_name=dataset_name,
            document_rows=document_rows,
            quality_rows=quality_rows,
            enrichments=enrichments,
            llm_usage=llm_usage,
            failures=failures,
        )
        output_paths.extend(report_paths)
        for path in report_paths:
            _artifact_event(
                event_callback,
                run_id=context.run_id,
                artifact_type="report",
                path=path,
            )
        context.files_written += len(output_paths)
    exit_code = EXIT_SUCCESS if validation.ok else EXIT_USER_ERROR
    status = "success" if validation.ok else "failed"
    summary = context.build_summary(
        status=status,
        exit_code=exit_code,
        counts={
            "pages_total": validation.pages_total,
            "pages_considered": len(document_rows),
            "pages_processed": len(document_rows),
            "pages_skipped_unchanged": 0,
            "pages_failed": validation.pages_failed,
        },
        output_paths=output_paths,
    )
    context.files_written += context.write_run_artifacts(summary, event_callback)
    return CommandResult(summary=summary, output_paths=output_paths)
