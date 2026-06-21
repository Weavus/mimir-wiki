from __future__ import annotations

import asyncio
import time
import traceback
from pathlib import Path
from threading import Lock
from typing import Annotated

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from mimir_wiki.config import AppConfig, apply_runtime_overrides, load_config
from mimir_wiki.constants import EXIT_RUNTIME_ERROR, EXIT_SUCCESS, EXIT_USER_ERROR
from mimir_wiki.llm.probe import probe_multimodal_ocr
from mimir_wiki.pipeline import (
    CommandResult,
    enrich_command,
    extract_visuals_command,
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
    console.print(_completion_panel(result))


def _completion_panel(result: CommandResult) -> Panel:
    summary = result.summary
    counts = summary.counts
    table = Table.grid(expand=True)
    table.add_column("group", style="bold cyan", no_wrap=True, width=11)
    table.add_column("value", ratio=1)
    table.add_row(
        "Status",
        (
            f"{_styled_status(summary.status)}   exit {summary.exit_code}   "
            f"elapsed {_fmt_duration(summary.elapsed_seconds)}"
        ),
    )
    table.add_row("Dataset", str(summary.dataset_name or "-"))
    if summary.cache_path:
        table.add_row("Cache", str(summary.cache_path))
    if summary.command == "extract-visuals":
        table.add_row(
            "Images",
            (
                f"extracted {counts.get('visual_images_extracted', 0):,}   "
                f"skipped {counts.get('visual_images_skipped', 0):,}   "
                f"failed {_style_count(int(counts.get('visual_images_failed', 0)), warn=True)}"
            ),
        )
    table.add_row(
        "Pages",
        (
            f"processed {counts.get('pages_processed', 0):,}   "
            f"skipped {counts.get('pages_skipped_unchanged', 0):,}   "
            f"failed {_style_count(int(counts.get('pages_failed', 0)), warn=True)}"
        ),
    )
    if "llm_calls" in counts:
        live_calls = int(counts.get("llm_live_calls", counts.get("llm_calls", 0)))
        cached_calls = int(counts.get("llm_cached_calls", 0))
        table.add_row(
            "LLM",
            (
                f"tasks {counts.get('llm_tasks', counts.get('llm_calls', 0)):,}   "
                f"live {live_calls:,}   cached {cached_calls:,}   "
                f"retries {counts.get('llm_retries', 0):,}"
            ),
        )
        live_input = int(counts.get("llm_live_input_tokens", counts.get("llm_input_tokens", 0)))
        live_output = int(counts.get("llm_live_output_tokens", counts.get("llm_output_tokens", 0)))
        cached_input = int(counts.get("llm_cached_input_tokens", 0))
        cached_output = int(counts.get("llm_cached_output_tokens", 0))
        table.add_row(
            "Tokens",
            (
                f"live in {live_input:,} out {live_output:,}   "
                f"avg {_avg_tokens(live_input, live_calls)}/"
                f"{_avg_tokens(live_output, live_calls)}   "
                f"cache saved in {cached_input:,} out {cached_output:,}"
            ),
        )
    table.add_row("Run", str(summary.outputs.get("run", "dry-run")))
    if result.output_paths:
        table.add_row("Outputs", _sample_output_paths(result.output_paths))
    return Panel(
        table,
        title=f"[bold cyan]{summary.command} complete[/bold cyan]",
        border_style="green" if summary.exit_code == EXIT_SUCCESS else "yellow",
    )


def _styled_status(status: str) -> str:
    if status == "success":
        return "[bold green]success[/bold green]"
    if status == "partial_success":
        return "[bold yellow]partial_success[/bold yellow]"
    return f"[bold red]{status}[/bold red]"


def _sample_output_paths(paths: list[Path]) -> str:
    samples = [str(path) for path in paths[:3]]
    remaining = len(paths) - len(samples)
    if remaining > 0:
        samples.append(f"... {remaining} more")
    return "\n".join(samples)


def _handle_exception(
    console: Console, exc: Exception, *, verbose: bool, json_output: bool
) -> None:
    exit_code = _exit_code_for_exception(exc)
    if json_output:
        typer.echo(
            json_dumps(
                {
                    "status": "failed",
                    "exit_code": exit_code,
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


def _exit_code_for_exception(exc: Exception) -> int:
    if isinstance(exc, ValueError):
        return EXIT_USER_ERROR
    return EXIT_RUNTIME_ERROR


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


def _snapshot_float(snapshot: dict[str, object], key: str, default: float = 0) -> float:
    value = snapshot.get(key, default)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "-"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _fmt_rate(value: float, suffix: str = "/s") -> str:
    if value <= 0:
        return "-"
    if value >= 1000:
        return f"{value / 1000:.1f}k{suffix}"
    if value >= 10:
        return f"{value:.1f}{suffix}"
    return f"{value:.2f}{suffix}"


def _avg_tokens(tokens: int, calls: int) -> str:
    if calls <= 0:
        return "-"
    return f"{round(tokens / calls):,}"


def _style_count(value: int, *, warn: bool = False) -> str:
    if value <= 0:
        return "[green]0[/green]"
    return f"[yellow]{value}[/yellow]" if warn else f"[cyan]{value}[/cyan]"


class RunDashboard:
    def __init__(
        self,
        *,
        command: str,
        dataset: str,
        provider: str,
        model: str,
        primary_unit: str,
        console: Console,
    ) -> None:
        self.command = command
        self.dataset = dataset
        self.provider = provider
        self.model = model
        self.primary_unit = primary_unit
        self.console = console
        self.started_at = time.monotonic()
        self.snapshot: dict[str, object] = {}

    def update(self, snapshot: dict[str, object]) -> None:
        self.snapshot.update(snapshot)

    def render(self) -> Panel:
        elapsed = time.monotonic() - self.started_at
        primary_done, primary_total = self._primary_progress()
        percent = (primary_done / primary_total * 100) if primary_total else 0
        eta = self._eta(elapsed, primary_done, primary_total)
        table = Table.grid(expand=True)
        table.add_column("group", style="bold cyan", no_wrap=True, width=11)
        table.add_column("value", ratio=1)
        table.add_row(
            "Progress",
            (
                f"[bold]{primary_done:,} / {primary_total:,}[/bold] {self.primary_unit}   "
                f"[cyan]{percent:.1f}%[/cyan]   ETA [dim]{_fmt_duration(eta)}[/dim]   "
                f"Elapsed [dim]{_fmt_duration(elapsed)}[/dim]"
            ),
        )
        for row in self._work_rows(elapsed):
            table.add_row(*row)
        table.add_row(
            "",
            ProgressBar(total=100, completed=max(0, min(100, percent)), width=None),
        )
        header = Text.assemble(
            (self.dataset, "bold"),
            "  ",
            (self.provider, "blue"),
            " · ",
            (self.model, "blue"),
        )
        return Panel(
            table,
            title=f"[bold cyan]{self.command}[/bold cyan]",
            subtitle=header,
            border_style="cyan",
        )

    def _primary_progress(self) -> tuple[int, int]:
        if self.command == "extract-visuals":
            done = _snapshot_int(self.snapshot, "images_completed")
            if done == 0:
                done = (
                    _snapshot_int(self.snapshot, "images_extracted")
                    + _snapshot_int(self.snapshot, "images_failed")
                    + _snapshot_int(self.snapshot, "images_skipped")
                )
            total = _snapshot_int(self.snapshot, "images_considered") or _snapshot_int(
                self.snapshot, "images_discovered"
            )
            return done, max(1, total)
        return _snapshot_int(self.snapshot, "considered"), max(
            1, _snapshot_int(self.snapshot, "total")
        )

    def _eta(self, elapsed: float, done: int, total: int) -> float | None:
        if done <= 0 or total <= done:
            return None
        return elapsed / done * (total - done)

    def _work_rows(self, elapsed: float) -> list[tuple[str, str]]:
        if self.command == "extract-visuals":
            return self._visual_rows(elapsed)
        return self._enrich_rows(elapsed)

    def _enrich_rows(self, elapsed: float) -> list[tuple[str, str]]:
        considered = _snapshot_int(self.snapshot, "considered")
        task_done = _snapshot_int(self.snapshot, "llm_task_calls_completed")
        llm_planned = _snapshot_int(self.snapshot, "llm_calls_planned")
        live_calls = _snapshot_int(self.snapshot, "llm_calls_completed")
        cached_calls = _snapshot_int(self.snapshot, "llm_task_calls_cached")
        live_input = _snapshot_int(self.snapshot, "llm_live_input_tokens")
        live_output = _snapshot_int(self.snapshot, "llm_live_output_tokens")
        cached_input = _snapshot_int(self.snapshot, "llm_cached_input_tokens")
        cached_output = _snapshot_int(self.snapshot, "llm_cached_output_tokens")
        current_task = str(self.snapshot.get("llm_current_task") or "-")
        current_page = str(self.snapshot.get("llm_current_page") or "-")
        current_chunk = str(self.snapshot.get("llm_current_chunk") or "-")
        chunk_count = str(self.snapshot.get("llm_chunk_count") or "-")
        return [
            (
                "Work",
                (
                    f"processed {_snapshot_int(self.snapshot, 'processed'):,}   "
                    f"skipped {_snapshot_int(self.snapshot, 'skipped'):,}   "
                    f"failed {_style_count(_snapshot_int(self.snapshot, 'failed'), warn=True)}"
                ),
            ),
            (
                "Throughput",
                (
                    f"pages {_fmt_rate(considered / elapsed if elapsed else 0)}   "
                    f"live LLM {_fmt_rate(live_calls / elapsed if elapsed else 0)}   "
                    f"cache {_fmt_rate(cached_calls / elapsed if elapsed else 0)}"
                ),
            ),
            (
                "LLM",
                (
                    f"{task_done:,} / {llm_planned:,} tasks   "
                    f"live {live_calls:,}   "
                    f"in-flight {_snapshot_int(self.snapshot, 'llm_calls_in_flight'):,}   "
                    f"cached {cached_calls:,}"
                ),
            ),
            (
                "Tokens",
                (
                    f"live in {live_input:,} out {live_output:,} "
                    f"avg {_avg_tokens(live_input, live_calls)}/"
                    f"{_avg_tokens(live_output, live_calls)}   "
                    f"cache saved in {cached_input:,} out {cached_output:,}"
                ),
            ),
            self._health_row(),
            self._adaptive_row(),
            (
                "Current",
                f"{current_task}   page {current_page}   chunk {current_chunk}/{chunk_count}",
            ),
        ]

    def _visual_rows(self, elapsed: float) -> list[tuple[str, str]]:
        images_done = _snapshot_int(self.snapshot, "images_completed")
        live_calls = _snapshot_int(self.snapshot, "llm_calls_completed")
        live_input = _snapshot_int(self.snapshot, "llm_live_input_tokens") or _snapshot_int(
            self.snapshot, "llm_input_tokens"
        )
        live_output = _snapshot_int(self.snapshot, "llm_live_output_tokens") or _snapshot_int(
            self.snapshot, "llm_output_tokens"
        )
        current_page = str(self.snapshot.get("current_page") or "-")
        current_image = str(self.snapshot.get("current_image") or "-")
        current_status = str(self.snapshot.get("current_status") or "-")
        return [
            (
                "Pages",
                (
                    f"{_snapshot_int(self.snapshot, 'processed'):,} / "
                    f"{_snapshot_int(self.snapshot, 'considered'):,} done   "
                    f"skipped {_snapshot_int(self.snapshot, 'skipped'):,}   "
                    f"failed {_style_count(_snapshot_int(self.snapshot, 'failed'), warn=True)}"
                ),
            ),
            (
                "Throughput",
                (
                    f"images {_fmt_rate(images_done / elapsed if elapsed else 0)}   "
                    f"live LLM {_fmt_rate(live_calls / elapsed if elapsed else 0)}   "
                    f"tokens {_fmt_rate((live_input + live_output) / elapsed if elapsed else 0)}"
                ),
            ),
            (
                "LLM",
                (
                    f"{live_calls:,} calls   "
                    f"in-flight {_snapshot_int(self.snapshot, 'llm_calls_in_flight'):,}   "
                    f"cached {_snapshot_int(self.snapshot, 'images_cached'):,}"
                ),
            ),
            (
                "Tokens",
                (
                    f"input {live_input:,}   output {live_output:,}   "
                    f"avg {_avg_tokens(live_input, live_calls)}/"
                    f"{_avg_tokens(live_output, live_calls)}"
                ),
            ),
            self._health_row(),
            self._adaptive_row(),
            ("Current", f"{current_status}   page {current_page}   image {current_image}"),
        ]

    def _health_row(self) -> tuple[str, str]:
        failures = _snapshot_int(self.snapshot, "failed") + _snapshot_int(
            self.snapshot, "images_failed"
        )
        return (
            "Health",
            (
                f"retries {_style_count(_snapshot_int(self.snapshot, 'llm_retries'), warn=True)}   "
                f"429s "
                f"{_style_count(_snapshot_int(self.snapshot, 'llm_rate_limits'), warn=True)}   "
                f"failures {_style_count(failures, warn=True)}"
            ),
        )

    def _adaptive_row(self) -> tuple[str, str]:
        raw = self.snapshot.get("llm_adaptive_concurrency")
        if not isinstance(raw, dict) or not raw:
            initial = _snapshot_int(self.snapshot, "llm_adaptive_initial_concurrency")
            maximum = _snapshot_int(self.snapshot, "llm_max_concurrency")
            worker_cap = _snapshot_int(self.snapshot, "llm_worker_cap")
            if maximum:
                details = f"initial {initial}/{maximum}"
                if worker_cap:
                    details = f"{details}   page workers {worker_cap}"
                return "Adaptive", details
            return "Adaptive", "-"
        parts = []
        for key, value in sorted(raw.items()):
            if not isinstance(value, dict):
                continue
            model = key.split(":")[-1]
            current = value.get("current", "-")
            maximum = value.get("max", "-")
            cooldown = _snapshot_float(value, "cooldown_seconds")
            suffix = f" cooldown {_fmt_duration(cooldown)}" if cooldown > 0 else ""
            parts.append(f"{model} {current}/{maximum}{suffix}")
        return "Adaptive", "   ".join(parts) if parts else "-"


class ArtifactDashboard:
    def __init__(self, *, command: str, dataset: str, console: Console) -> None:
        self.command = command
        self.dataset = dataset
        self.console = console
        self.started_at = time.monotonic()
        self.snapshot: dict[str, object] = {}

    def update(self, snapshot: dict[str, object]) -> None:
        self.snapshot.update(snapshot)

    def render(self) -> Panel:
        elapsed = time.monotonic() - self.started_at
        done, total, unit = self._primary_progress()
        percent = (done / total * 100) if total else 0
        eta = elapsed / done * (total - done) if done > 0 and total > done else None
        table = Table.grid(expand=True)
        table.add_column("group", style="bold cyan", no_wrap=True, width=11)
        table.add_column("value", ratio=1)
        table.add_row(
            "Progress",
            (
                f"[bold]{done:,} / {total:,}[/bold] {unit}   "
                f"[cyan]{percent:.1f}%[/cyan]   ETA [dim]{_fmt_duration(eta)}[/dim]   "
                f"Elapsed [dim]{_fmt_duration(elapsed)}[/dim]"
            ),
        )
        for row in self._rows(elapsed):
            table.add_row(*row)
        table.add_row(
            "",
            ProgressBar(total=100, completed=max(0, min(100, percent)), width=None),
        )
        return Panel(
            table,
            title=f"[bold cyan]{self.command}[/bold cyan]",
            subtitle=Text.assemble((self.dataset, "bold"), "  ", ("local", "blue")),
            border_style="cyan",
        )

    def _primary_progress(self) -> tuple[int, int, str]:
        if self.command == "report":
            return (
                _snapshot_int(self.snapshot, "reports_written"),
                max(1, _snapshot_int(self.snapshot, "reports_planned", 1)),
                "reports",
            )
        return (
            _snapshot_int(self.snapshot, "pages_checked"),
            max(1, _snapshot_int(self.snapshot, "pages_total", 1)),
            "pages",
        )

    def _rows(self, elapsed: float) -> list[tuple[str, str]]:
        if self.command == "report":
            return self._report_rows(elapsed)
        return self._validate_rows(elapsed)

    def _report_rows(self, elapsed: float) -> list[tuple[str, str]]:
        written = _snapshot_int(self.snapshot, "reports_written")
        return [
            (
                "Inputs",
                (
                    f"pages {_snapshot_int(self.snapshot, 'pages_total'):,}   "
                    f"docs {_snapshot_int(self.snapshot, 'document_rows'):,}   "
                    f"enrichments {_snapshot_int(self.snapshot, 'enrichments'):,}   "
                    f"visuals {_snapshot_int(self.snapshot, 'visual_artifacts'):,}"
                ),
            ),
            (
                "Artifacts",
                (
                    f"written {written:,}   "
                    f"warnings {_snapshot_int(self.snapshot, 'warnings'):,}   "
                    f"failures {_style_count(_snapshot_int(self.snapshot, 'failures'), warn=True)}"
                ),
            ),
            ("Throughput", f"reports {_fmt_rate(written / elapsed if elapsed else 0)}"),
            ("Current", str(self.snapshot.get("current_report") or "-")),
        ]

    def _validate_rows(self, elapsed: float) -> list[tuple[str, str]]:
        checked = _snapshot_int(self.snapshot, "pages_checked")
        return [
            (
                "Artifacts",
                (
                    f"metadata {_snapshot_int(self.snapshot, 'metadata_checked'):,}   "
                    f"markdown {_snapshot_int(self.snapshot, 'markdown_checked'):,}   "
                    f"links {_snapshot_int(self.snapshot, 'links_checked'):,}   "
                    f"conversion {_snapshot_int(self.snapshot, 'conversion_checked'):,}"
                ),
            ),
            (
                "Health",
                (
                    f"errors {_style_count(_snapshot_int(self.snapshot, 'errors'), warn=True)}   "
                    f"warnings "
                    f"{_style_count(_snapshot_int(self.snapshot, 'warnings'), warn=True)}   "
                    f"failed "
                    f"{_style_count(_snapshot_int(self.snapshot, 'pages_failed'), warn=True)}"
                ),
            ),
            ("Throughput", f"pages {_fmt_rate(checked / elapsed if elapsed else 0)}"),
            (
                "Current",
                (
                    f"page {self.snapshot.get('current_page') or '-'}   "
                    f"artifact {self.snapshot.get('current_artifact') or '-'}"
                ),
            ),
        ]


class LiveDashboardUpdater:
    def __init__(self, dashboard: RunDashboard | ArtifactDashboard, live: Live) -> None:
        self.dashboard = dashboard
        self.live = live
        self.last_rendered_at = 0.0
        self.min_interval_seconds = 0.25

    def update(self, snapshot: dict[str, object], *, force: bool = False) -> None:
        self.dashboard.update(snapshot)
        now = time.monotonic()
        if force or now - self.last_rendered_at >= self.min_interval_seconds:
            self.live.update(self.dashboard.render(), refresh=True)
            self.last_rendered_at = now

    def finish(self) -> None:
        self.live.update(self.dashboard.render(), refresh=True)


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
            dashboard = ArtifactDashboard(
                command="validate-cache", dataset=cache.name, console=console
            )
            with Live(
                dashboard.render(), console=console, refresh_per_second=4, transient=True
            ) as live:
                updater = LiveDashboardUpdater(dashboard, live)

                def progress_callback(snapshot: dict[str, object]) -> None:
                    updater.update(snapshot)

                result = validate_cache_command(
                    config=config,
                    cache_path=cache,
                    profile=profile,
                    dry_run=dry_run,
                    limit=limit,
                    progress_callback=progress_callback,
                    event_callback=lambda event: _write_log(log_file, event),
                )
                updater.finish()
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
        raise typer.Exit(_exit_code_for_exception(exc)) from exc


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
            dashboard = RunDashboard(
                command="enrich",
                dataset=cache.name,
                provider=config.llm.provider,
                model="mixed"
                if config.llm.task_models or config.llm.task_bundles
                else config.llm.model,
                primary_unit="pages",
                console=console,
            )
            with Live(
                dashboard.render(), console=console, refresh_per_second=4, transient=True
            ) as live:
                updater = LiveDashboardUpdater(dashboard, live)

                def progress_callback(snapshot: dict[str, object]) -> None:
                    updater.update(snapshot)

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
                updater.finish()
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
        raise typer.Exit(_exit_code_for_exception(exc)) from exc


@app.command("extract-visuals")
def extract_visuals(
    config_path: ConfigOption = None,
    profile: ProfileOption = None,
    cache: CacheOption = Path("cache"),
    provider: ProviderOption = "azure-ai-foundry",
    model: Annotated[
        str, typer.Option("--model", help="Multimodal model/deployment for visual OCR")
    ] = "gpt-5.4-mini",
    limit: LimitOption = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-extract existing visual artifacts")
    ] = False,
    space_filter: Annotated[
        str | None, typer.Option("--space-filter", help="Only process a space key")
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
            out=None,
            provider=provider,
            enable_llm=True,
            llm_tasks=None,
            emit_onyx_markdown=None,
            include_source_content=None,
            redaction=None,
        )
        data = config.model_dump(mode="python")
        data["visual_extraction"]["enabled"] = True
        data["visual_extraction"]["provider"] = provider or data["visual_extraction"]["provider"]
        data["visual_extraction"]["model"] = model
        config = AppConfig.model_validate(data)
        if _show_progress(config, json_output=json_output, quiet=quiet):
            dashboard = RunDashboard(
                command="extract-visuals",
                dataset=cache.name,
                provider=config.visual_extraction.provider,
                model=config.visual_extraction.model,
                primary_unit="images",
                console=console,
            )
            with Live(
                dashboard.render(), console=console, refresh_per_second=4, transient=True
            ) as live:
                updater = LiveDashboardUpdater(dashboard, live)

                def progress_callback(snapshot: dict[str, object]) -> None:
                    updater.update(snapshot)

                result = extract_visuals_command(
                    config=config,
                    cache_path=cache,
                    profile=profile,
                    dry_run=dry_run,
                    limit=limit,
                    force=force,
                    space_filter=space_filter,
                    progress_callback=progress_callback,
                    event_callback=lambda event: _write_log(log_file, event),
                )
                updater.finish()
        else:
            result = extract_visuals_command(
                config=config,
                cache_path=cache,
                profile=profile,
                dry_run=dry_run,
                limit=limit,
                force=force,
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
        raise typer.Exit(_exit_code_for_exception(exc)) from exc


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
            dashboard = ArtifactDashboard(command="report", dataset=cache.name, console=console)
            with Live(
                dashboard.render(), console=console, refresh_per_second=4, transient=True
            ) as live:
                updater = LiveDashboardUpdater(dashboard, live)

                def progress_callback(snapshot: dict[str, object]) -> None:
                    updater.update(snapshot)

                result = report_command(
                    config=config,
                    cache_path=cache,
                    profile=profile,
                    dry_run=dry_run,
                    limit=limit,
                    progress_callback=progress_callback,
                    event_callback=lambda event: _write_log(log_file, event),
                )
                updater.finish()
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
        raise typer.Exit(_exit_code_for_exception(exc)) from exc


@app.command("probe-ocr")
def probe_ocr(
    config_path: ConfigOption = None,
    profile: ProfileOption = None,
    provider: ProviderOption = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Model or deployment to probe")
    ] = None,
    json_output: JsonOption = False,
    no_color: NoColorOption = False,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Probe whether the configured model accepts image input and can OCR a tiny PNG."""
    console = _console(no_color=no_color, quiet=quiet, json_output=json_output)
    try:
        overrides = apply_runtime_overrides(
            provider=provider,
            enable_llm=True,
            llm_tasks=None,
            emit_onyx_markdown=None,
            include_source_content=None,
            redaction=None,
            cache=None,
            out=None,
            onyx_out=None,
            reports_out=None,
        )
        if model:
            llm_overrides = overrides.setdefault("llm", {})
            llm_overrides["model"] = model
            llm_overrides.setdefault("azure_openai", {})["deployment_env"] = ""
            llm_overrides.setdefault("azure_ai_foundry", {})["deployment_env"] = ""
            llm_overrides.setdefault("openai_compatible", {})["model_env"] = ""
        config = load_config(config_path=config_path, profile=profile, cli_overrides=overrides)
        result = asyncio.run(probe_multimodal_ocr(config))
        if json_output:
            typer.echo(json_dumps(result, pretty=True))
        elif not quiet:
            table = Table(title="mimir-wiki probe-ocr")
            table.add_column("Field")
            table.add_column("Value")
            for key in (
                "status",
                "provider",
                "model",
                "api_kind",
                "status_code",
                "image_input_accepted",
                "ocr_text_matched",
                "expected_text",
                "response_text",
                "error_type",
                "error",
            ):
                if key in result:
                    table.add_row(key, str(result[key]))
            console.print(table)
        exit_code = EXIT_SUCCESS if result.get("ocr_text_matched") is True else EXIT_RUNTIME_ERROR
        raise typer.Exit(exit_code)
    except typer.Exit:
        raise
    except Exception as exc:
        _handle_exception(console, exc, verbose=verbose, json_output=json_output)
        raise typer.Exit(_exit_code_for_exception(exc)) from exc


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
        raise typer.Exit(_exit_code_for_exception(exc)) from exc


def main() -> None:
    app()


if __name__ == "__main__":
    main()
