from __future__ import annotations

import traceback
from pathlib import Path
from threading import Lock
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from mimir_wiki.config import AppConfig, apply_runtime_overrides, load_config
from mimir_wiki.constants import EXIT_RUNTIME_ERROR
from mimir_wiki.pipeline import (
    CommandResult,
    enrich_command,
    report_command,
    validate_cache_command,
)
from mimir_wiki.schema_export import export_json_schemas
from mimir_wiki.utils import json_dumps

app = typer.Typer(help="Validate, enrich, inventory, and report on mimir-confluence caches.")
_LOG_LOCK = Lock()


def _console(*, no_color: bool, quiet: bool, json_output: bool) -> Console:
    return Console(no_color=no_color or quiet or json_output, quiet=quiet and not json_output)


def _print_result(
    console: Console, result: CommandResult, *, json_output: bool, quiet: bool
) -> None:
    if json_output:
        typer.echo(json_dumps(result.to_dict(), pretty=True))
        return
    if quiet:
        return
    table = Table(title=f"mimir-wiki {result.summary.command}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Status", result.summary.status)
    table.add_row("Dataset", result.summary.dataset_name)
    if result.summary.cache_path:
        table.add_row("Cache", result.summary.cache_path)
    for key, value in result.summary.counts.items():
        table.add_row(key, str(value))
    table.add_row("Run", result.summary.outputs.get("run", "dry-run"))
    console.print(table)
    if result.output_paths:
        console.print("Outputs:")
        for path in result.output_paths[:20]:
            console.print(f"- {path}")
        if len(result.output_paths) > 20:
            console.print(f"- ... {len(result.output_paths) - 20} more")


def _handle_exception(
    console: Console, exc: Exception, *, verbose: bool, json_output: bool
) -> None:
    if json_output:
        typer.echo(
            json_dumps(
                {
                    "status": "failed",
                    "exit_code": EXIT_RUNTIME_ERROR,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                pretty=True,
            )
        )
    else:
        console.print(f"Error: {exc}", style="red")
        if verbose:
            console.print(traceback.format_exc())


def _handle_keyboard_interrupt(
    console: Console, *, log_file: Path | None, json_output: bool
) -> None:
    payload = {"event": "command_cancelled", "status": "cancelled", "exit_code": EXIT_RUNTIME_ERROR}
    _write_log(log_file, payload)
    if json_output:
        typer.echo(json_dumps(payload, pretty=True))
    else:
        console.print("Cancelled", style="yellow")


def _write_log(log_file: Path | None, event: dict[str, object]) -> None:
    if log_file is None:
        return
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_LOCK, log_file.open("a", encoding="utf-8") as handle:
        handle.write(json_dumps(event) + "\n")


def _show_progress(config: AppConfig, *, json_output: bool, quiet: bool) -> bool:
    if json_output or quiet:
        return False
    if config.cli.progress == "never":
        return False
    if config.cli.progress == "always":
        return True
    return Console().is_terminal


def _snapshot_int(snapshot: dict[str, object], key: str, default: int = 0) -> int:
    value = snapshot.get(key, default)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default


def _load_runtime_config(
    *,
    config_path: Path | None,
    profile: str | None,
    cache: Path | None,
    out: Path | None,
    provider: str | None,
    enable_llm: bool | None,
    llm_tasks: list[str] | None,
    emit_onyx_markdown: bool | None,
    include_source_content: bool | None,
    redaction: str | None,
    onyx_out: Path | None = None,
    reports_out: Path | None = None,
) -> AppConfig:
    overrides = apply_runtime_overrides(
        provider=provider,
        enable_llm=enable_llm,
        llm_tasks=llm_tasks,
        emit_onyx_markdown=emit_onyx_markdown,
        include_source_content=include_source_content,
        redaction=redaction,
        cache=cache,
        out=out,
        onyx_out=onyx_out,
        reports_out=reports_out,
    )
    return load_config(config_path=config_path, profile=profile, cli_overrides=overrides)


ConfigOption = Annotated[Path | None, typer.Option("--config", help="Path to mimir-wiki.yaml")]
ProfileOption = Annotated[str | None, typer.Option("--profile", help="Config profile name")]
CacheOption = Annotated[
    Path, typer.Option("--cache", exists=True, file_okay=False, help="mimir-confluence cache path")
]
OutOption = Annotated[Path | None, typer.Option("--out", help="Output directory")]
ProviderOption = Annotated[
    str | None,
    typer.Option(
        "--provider",
        help="LLM provider: none, openai, azure-openai, azure-ai-foundry, or openai-compatible",
    ),
]
LimitOption = Annotated[int | None, typer.Option("--limit", min=1, help="Limit pages processed")]
JsonOption = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON")]
NoColorOption = Annotated[bool, typer.Option("--no-color", help="Disable color output")]
QuietOption = Annotated[bool, typer.Option("--quiet", help="Suppress human summary")]
VerboseOption = Annotated[bool, typer.Option("--verbose", help="Show tracebacks")]
DryRunOption = Annotated[bool, typer.Option("--dry-run", help="Validate and plan without writing")]


@app.command("validate-cache")
def validate_cache(
    config_path: ConfigOption = None,
    profile: ProfileOption = None,
    cache: CacheOption = Path("cache"),
    out: OutOption = None,
    limit: LimitOption = None,
    dry_run: DryRunOption = False,
    json_output: JsonOption = False,
    no_color: NoColorOption = False,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
    log_file: Annotated[Path | None, typer.Option("--log-file", help="Write JSONL logs")] = None,
) -> None:
    console = _console(no_color=no_color, quiet=quiet, json_output=json_output)
    try:
        config = _load_runtime_config(
            config_path=config_path,
            profile=profile,
            cache=cache,
            out=None,
            provider="none",
            enable_llm=False,
            llm_tasks=None,
            emit_onyx_markdown=None,
            include_source_content=None,
            redaction=None,
            reports_out=out,
        )
        if _show_progress(config, json_output=json_output, quiet=quiet):
            with Progress(
                TextColumn("[bold blue]validate-cache"),
                BarColumn(),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task_id = progress.add_task("cache", total=1)
                result = validate_cache_command(
                    config=config,
                    cache_path=cache,
                    profile=profile,
                    dry_run=dry_run,
                    limit=limit,
                    event_callback=lambda event: _write_log(log_file, event),
                )
                progress.update(task_id, completed=1)
        else:
            result = validate_cache_command(
                config=config,
                cache_path=cache,
                profile=profile,
                dry_run=dry_run,
                limit=limit,
                event_callback=lambda event: _write_log(log_file, event),
            )
        _write_log(
            log_file, {"event": "command_finished", **result.summary.model_dump(mode="json")}
        )
        _print_result(console, result, json_output=json_output, quiet=quiet)
        raise typer.Exit(result.exit_code)
    except typer.Exit:
        raise
    except KeyboardInterrupt as exc:
        _handle_keyboard_interrupt(console, log_file=log_file, json_output=json_output)
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    except Exception as exc:
        _write_log(
            log_file,
            {"event": "command_failed", "error_type": type(exc).__name__, "message": str(exc)},
        )
        _handle_exception(console, exc, verbose=verbose, json_output=json_output)
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc


@app.command("enrich")
def enrich(
    config_path: ConfigOption = None,
    profile: ProfileOption = None,
    cache: CacheOption = Path("cache"),
    out: OutOption = None,
    provider: ProviderOption = None,
    enable_llm: Annotated[
        bool | None, typer.Option("--enable-llm/--disable-llm", help="Enable or disable LLM tasks")
    ] = None,
    llm_tasks: Annotated[
        list[str] | None, typer.Option("--llm-task", help="Enable one LLM task; repeatable")
    ] = None,
    limit: LimitOption = None,
    changed_only: Annotated[
        bool, typer.Option("--changed-only", help="Skip unchanged pages by signature")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Reprocess even when signatures match")
    ] = False,
    document_type_filter: Annotated[
        str | None, typer.Option("--document-type-filter", help="Only keep matching document type")
    ] = None,
    space_filter: Annotated[
        str | None, typer.Option("--space-filter", help="Only process a space key")
    ] = None,
    emit_onyx_markdown: Annotated[
        bool | None,
        typer.Option(
            "--emit-onyx-markdown/--no-emit-onyx-markdown", help="Write Onyx POC Markdown"
        ),
    ] = None,
    onyx_out: Annotated[
        Path | None, typer.Option("--onyx-out", help="Onyx enriched Markdown output root")
    ] = None,
    include_source_content: Annotated[
        bool | None,
        typer.Option(
            "--include-source-content/--no-include-source-content",
            help="Include source Markdown in Onyx output",
        ),
    ] = None,
    redaction: Annotated[
        str | None, typer.Option("--redaction", help="redact, fail, or off")
    ] = None,
    dry_run: DryRunOption = False,
    json_output: JsonOption = False,
    no_color: NoColorOption = False,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
    log_file: Annotated[Path | None, typer.Option("--log-file", help="Write JSONL logs")] = None,
) -> None:
    console = _console(no_color=no_color, quiet=quiet, json_output=json_output)
    try:
        config = _load_runtime_config(
            config_path=config_path,
            profile=profile,
            cache=cache,
            out=out,
            provider=provider,
            enable_llm=enable_llm,
            llm_tasks=llm_tasks,
            emit_onyx_markdown=emit_onyx_markdown,
            include_source_content=include_source_content,
            redaction=redaction,
            onyx_out=onyx_out,
        )
        if _show_progress(config, json_output=json_output, quiet=quiet):
            progress = Progress(
                TextColumn("[bold blue]enrich"),
                BarColumn(),
                TaskProgressColumn(),
                TextColumn(
                    "processed={task.fields[processed]} skipped={task.fields[skipped]} "
                    "failed={task.fields[failed]} llm={task.fields[llm_done]}/"
                    "{task.fields[llm_total]} cached={task.fields[llm_cached]} "
                    "retries={task.fields[retries]} current={task.fields[current]}"
                ),
                TimeElapsedColumn(),
                console=console,
            )
            with progress:
                task_id = progress.add_task(
                    "pages",
                    total=1,
                    processed=0,
                    skipped=0,
                    failed=0,
                    retries=0,
                    llm_done=0,
                    llm_total=0,
                    llm_cached=0,
                    current="-",
                )

                def progress_callback(snapshot: dict[str, object]) -> None:
                    current_task = str(snapshot.get("llm_current_task") or "-")
                    current_page = str(snapshot.get("llm_current_page") or "-")
                    current = "-" if current_task == "-" else f"{current_task}:{current_page}"
                    progress.update(
                        task_id,
                        total=max(1, _snapshot_int(snapshot, "total", 1)),
                        completed=_snapshot_int(snapshot, "considered"),
                        processed=_snapshot_int(snapshot, "processed"),
                        skipped=_snapshot_int(snapshot, "skipped"),
                        failed=_snapshot_int(snapshot, "failed"),
                        retries=_snapshot_int(snapshot, "llm_retries"),
                        llm_done=_snapshot_int(snapshot, "llm_calls_completed"),
                        llm_total=_snapshot_int(snapshot, "llm_calls_planned"),
                        llm_cached=_snapshot_int(snapshot, "llm_cached_calls"),
                        current=current,
                    )

                result = enrich_command(
                    config=config,
                    cache_path=cache,
                    profile=profile,
                    dry_run=dry_run,
                    limit=limit,
                    changed_only=changed_only,
                    force=force,
                    document_type_filter=document_type_filter,
                    space_filter=space_filter,
                    progress_callback=progress_callback,
                    event_callback=lambda event: _write_log(log_file, event),
                )
        else:
            result = enrich_command(
                config=config,
                cache_path=cache,
                profile=profile,
                dry_run=dry_run,
                limit=limit,
                changed_only=changed_only,
                force=force,
                document_type_filter=document_type_filter,
                space_filter=space_filter,
                event_callback=lambda event: _write_log(log_file, event),
            )
        _write_log(
            log_file, {"event": "command_finished", **result.summary.model_dump(mode="json")}
        )
        _print_result(console, result, json_output=json_output, quiet=quiet)
        raise typer.Exit(result.exit_code)
    except typer.Exit:
        raise
    except KeyboardInterrupt as exc:
        _handle_keyboard_interrupt(console, log_file=log_file, json_output=json_output)
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    except Exception as exc:
        _write_log(
            log_file,
            {"event": "command_failed", "error_type": type(exc).__name__, "message": str(exc)},
        )
        _handle_exception(console, exc, verbose=verbose, json_output=json_output)
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc


@app.command("report")
def report(
    config_path: ConfigOption = None,
    profile: ProfileOption = None,
    cache: CacheOption = Path("cache"),
    out: OutOption = None,
    limit: LimitOption = None,
    dry_run: DryRunOption = False,
    json_output: JsonOption = False,
    no_color: NoColorOption = False,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
    log_file: Annotated[Path | None, typer.Option("--log-file", help="Write JSONL logs")] = None,
) -> None:
    console = _console(no_color=no_color, quiet=quiet, json_output=json_output)
    try:
        config = _load_runtime_config(
            config_path=config_path,
            profile=profile,
            cache=cache,
            out=None,
            provider="none",
            enable_llm=False,
            llm_tasks=None,
            emit_onyx_markdown=None,
            include_source_content=None,
            redaction=None,
            reports_out=out,
        )
        if _show_progress(config, json_output=json_output, quiet=quiet):
            with Progress(
                TextColumn("[bold blue]report"),
                BarColumn(),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task_id = progress.add_task("reports", total=1)
                result = report_command(
                    config=config,
                    cache_path=cache,
                    profile=profile,
                    dry_run=dry_run,
                    limit=limit,
                    event_callback=lambda event: _write_log(log_file, event),
                )
                progress.update(task_id, completed=1)
        else:
            result = report_command(
                config=config,
                cache_path=cache,
                profile=profile,
                dry_run=dry_run,
                limit=limit,
                event_callback=lambda event: _write_log(log_file, event),
            )
        _write_log(
            log_file, {"event": "command_finished", **result.summary.model_dump(mode="json")}
        )
        _print_result(console, result, json_output=json_output, quiet=quiet)
        raise typer.Exit(result.exit_code)
    except typer.Exit:
        raise
    except KeyboardInterrupt as exc:
        _handle_keyboard_interrupt(console, log_file=log_file, json_output=json_output)
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    except Exception as exc:
        _write_log(
            log_file,
            {"event": "command_failed", "error_type": type(exc).__name__, "message": str(exc)},
        )
        _handle_exception(console, exc, verbose=verbose, json_output=json_output)
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc


@app.command("export-schema")
def export_schema(
    out: Annotated[Path, typer.Option("--out", help="Schema output directory")] = Path("schemas"),
    json_output: JsonOption = False,
    no_color: NoColorOption = False,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
    log_file: Annotated[Path | None, typer.Option("--log-file", help="Write JSONL logs")] = None,
) -> None:
    console = _console(no_color=no_color, quiet=quiet, json_output=json_output)
    try:
        paths = export_json_schemas(out)
        payload = {
            "status": "success",
            "schemas_written": len(paths),
            "paths": [str(path) for path in paths],
        }
        _write_log(log_file, {"event": "command_finished", "command": "export-schema", **payload})
        if json_output:
            typer.echo(json_dumps(payload, pretty=True))
        elif not quiet:
            console.print(f"Wrote {len(paths)} schema files to {out}")
        raise typer.Exit(0)
    except typer.Exit:
        raise
    except KeyboardInterrupt as exc:
        _handle_keyboard_interrupt(console, log_file=log_file, json_output=json_output)
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    except Exception as exc:
        _write_log(
            log_file,
            {"event": "command_failed", "error_type": type(exc).__name__, "message": str(exc)},
        )
        _handle_exception(console, exc, verbose=verbose, json_output=json_output)
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc


def main() -> None:
    app()


if __name__ == "__main__":
    main()
