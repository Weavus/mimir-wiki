# CLI Reference

`mimir-wiki` is implemented with Typer and Rich. Commands are human-readable by
default and scriptable with `--json` and `--quiet`.

## Commands

- `mimir-wiki validate-cache`
- `mimir-wiki enrich`
- `mimir-wiki report`
- `mimir-wiki export-schema`

## Common Options

- `--config PATH`: load a specific `mimir-wiki.yaml` file.
- `--profile NAME`: apply a named config profile after base config.
- `--cache PATH`: source `mimir-confluence` cache directory.
- `--out PATH`: command-specific output directory.
- `--limit INT`: process only the first N successful manifest pages.
- `--dry-run`: validate and plan without writing command outputs.
- `--json`: print a JSON result payload.
- `--quiet`: suppress human summary output.
- `--no-color`: disable color output.
- `--verbose`: include stack traces for unexpected failures.
- `--log-file PATH`: append structured JSONL command, page, retry, cancellation and artifact events.

Config precedence is built-in defaults, config file, profile, `.env`, process

## `validate-cache`

Validate one exported cache.

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki validate-cache \
  --cache ./cache/customer-identity-and-access-management-entra
```

Important checks:

- required root files: `dataset.json`, `manifest.jsonl`, `manifest.summary.json`
- page artifacts: `metadata.json`, `clean.md`, `text.txt`, `links.json`, `conversion.json`
- observed metadata schema details such as object `author`, `content_hashes`, ancestor `id`/`title`
- optional `errors.jsonl`
- conversion warnings

Useful options:

- `--out PATH`: report output directory, defaults to configured `reports`.
- `--limit INT`: validate only a subset of manifest pages.
- `--json --quiet`: machine-readable validation summary.

Exit codes:

- `0`: validation success.
- `1`: missing/invalid input or schema errors.

## `enrich`

Validate, classify, enrich, index, report and optionally write Onyx POC Markdown.

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki enrich \
  --config ./mimir-wiki.yaml \
  --cache ./cache/customer-identity-and-access-management-entra \
  --limit 25 \
  --force
```

Provider options:

- `--provider none`: deterministic local enrichment only.
- `--provider openai`: OpenAI public chat completions endpoint.
- `--provider azure-openai`: classic Azure OpenAI deployment endpoint with `api-version`.
- `--provider azure-ai-foundry`: Azure AI Foundry OpenAI v1 Responses API or explicit chat completions endpoint.
- `--provider openai-compatible`: OpenAI-compatible chat completions endpoint.

LLM options:

- `--enable-llm / --disable-llm`: override the global LLM feature gate for one run.
- `--llm-task TASK`: enable a specific LLM task; repeat for multiple tasks.
- `--changed-only`: skip pages whose enrichment signature still matches.
- `--force`: reprocess pages even if signatures match.

Filter and output options:

- `--document-type-filter TYPE`: only keep pages classified as `TYPE`.
- `--space-filter SPACE`: process one Confluence space key.
- `--emit-onyx-markdown / --no-emit-onyx-markdown`: write or suppress Onyx POC Markdown.
- `--onyx-out PATH`: override `dist/onyx-enriched` output root.
- `--include-source-content / --no-include-source-content`: include or omit cleaned source Markdown in Onyx files.
- `--redaction redact|fail|off`: redact likely secrets, fail if found, or disable redaction.

Progress output includes page counts, failed pages, LLM calls completed/planned,
cached calls, retries and current LLM task/page.

Exit codes:

- `0`: success.
- `1`: validation, input or credential problem.
- `2`: runtime/provider failure after retries.
- `3`: partial success with page-level failures or cancellation.

## `report`

Regenerate reports from the current cache, knowledge indexes and run artifacts.

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki report \
  --cache ./cache/customer-identity-and-access-management-entra
```

Reports include:

- `cache_validation.md`
- `enrichment_summary.md`
- `document_types.md`
- `stale_or_deprecated.md`
- `high_value_sources.md`
- `missing_owners.md`
- `high_value_subtrees.md`
- `attachment_followups.md`
- `duplicate_candidates.md`
- `llm_usage.md`
- `page_failures.md`

## `export-schema`

Export JSON Schema files for generated artifacts.

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki export-schema --out ./schemas
```

Schema files include enrichment, document index rows, quality rows, themes,
concepts, candidate entities, facts, failures, warnings, LLM usage and run
summary.

## Cancellation

Pressing `Ctrl-C` during `enrich` cancels pending page futures. Running page
workers finish their current writes. Completed page results, global JSONL files,
reports, warnings, failures and run summary are preserved. The run records
`run_cancelled`, exits with code `3`, and includes `pages_cancelled` in summary
counts.

## Examples

Deterministic smoke run:

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki enrich \
  --cache ./cache/carel3support \
  --provider none \
  --limit 25
```

Azure AI Foundry Responses API run:

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki enrich \
  --config ./mimir-wiki.yaml \
  --cache ./cache/customer-identity-and-access-management-entra \
  --provider azure-ai-foundry \
  --enable-llm \
  --limit 5 \
  --force
```

Machine-readable validation:

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki validate-cache \
  --cache ./cache/carel3support \
  --json \
  --quiet
```
