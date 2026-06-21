# CLI Reference

`mimir-wiki` is implemented with Typer and Rich. Commands are human-readable by
default and scriptable with `--json` and `--quiet`.

## Commands

- `mimir-wiki validate-cache`
- `mimir-wiki enrich`
- `mimir-wiki extract-visuals`
- `mimir-wiki probe-ocr`
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
environment variables, then CLI flags.

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

`enrich` is the command that creates or refreshes knowledge artifacts. It reads
source cache pages, writes `pages/{page_id}/enrichment.json`, rewrites stable
JSONL indexes under `knowledge/`, writes Onyx POC Markdown when enabled, and
finishes by writing the standard report set. Use `--changed-only` for normal
incremental runs and `--force` after changing enrichment logic or regenerated
visual extraction artifacts.

`enrich` does not perform image OCR by itself. If `pages/{page_id}/visual_extraction.json`
exists from a previous `extract-visuals` run, `enrich` reads it, adds
`visual_content_extracted` when extraction completed successfully, and includes
extracted visual evidence in generated Onyx Markdown. If extraction is partial,
`enrich` keeps missing-content review flags.

Exit codes:

- `0`: success.
- `1`: validation, input or credential problem.
- `2`: runtime/provider failure after retries.
- `3`: partial success with page-level failures or cancellation.

## `extract-visuals`

Extract OCR text and captions from visual source artifacts already present in a
local `mimir-confluence` cache.

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki extract-visuals \
  --cache ./cache/carel3support \
  --provider azure-ai-foundry \
  --model gpt-5.4-mini
```

Important boundary: `mimir-wiki` does not connect to Confluence and does not
download source images. Downloading attachments is `mimir-confluence`'s job.
`extract-visuals` only processes:

- image files already downloaded under `pages/{page_id}/attachments/`
- Markdown image references that resolve to local attachment files, including
  Confluence `/download/attachments/{page_id}/...` URLs for other pages already
  present in the same cache
- embedded `data:image/...` references already present in `clean.md`

Remote image URLs that do not resolve to local cache attachments are recorded in
`visual_extraction.json` as skipped with `error_type: remote_source_not_in_cache`.
These can be Confluence-hosted attachment URLs outside the local cache,
Confluence plugin/generated image URLs, or completely external URLs. Rerun
`mimir-confluence` with attachment export enabled, and with embedded/linked-page
attachment handling if available, when the missing Confluence-hosted visuals are
important. External URLs require separate source-system support; `mimir-wiki`
intentionally does not fetch them.

Selection behavior:

- all candidate image sources are discovered before any page cap is applied
- candidates are ranked by deterministic context signals such as nearby heading,
  filename, runbook/incident/architecture/dashboard/log keywords, and low-text pages
- exact duplicate image content is reused by `content_sha256` to avoid repeated
  multimodal calls
- obvious logo/icon/placeholder/tiny images are skipped before provider calls
- report-like pages use an adaptive lower cap from `visual_extraction.report_page_max_images`
- repeated dashboard/chart/report images are sampled by representative group
- omitted images are recorded for review instead of disappearing silently

Useful options:

- `--provider PROVIDER`: multimodal provider, defaults to `azure-ai-foundry`.
- `--model MODEL`: multimodal model/deployment, defaults to `gpt-5.4-mini`.
- `--limit INT`: process only the first N successful manifest pages.
- `--space-filter SPACE`: process one Confluence space key.
- `--force`: re-extract pages even when a complete artifact already exists.
- `--dry-run`: count candidate pages/images without writing artifacts.
- `--json --quiet`: machine-readable summary.

Outputs:

- `pages/{page_id}/visual_extraction.json`
- `runs/{run_id}/visual_omitted_images.jsonl` when ranked/grouped/capped images are omitted
- `runs/{run_id}/summary.json`
- `runs/{run_id}/page_failures.jsonl`
- `runs/{run_id}/warnings.jsonl`
- `runs/{run_id}/llm_usage.jsonl` when provider usage data is available

Typical workflow:

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki extract-visuals \
  --cache ./cache/carel3support

UV_CACHE_DIR=.uv-cache uv run mimir-wiki enrich \
  --cache ./cache/carel3support \
  --provider none \
  --force
```

Exit codes:

- `0`: success.
- `1`: validation, input or credential problem.
- `3`: partial success with page-level extraction failures.

## `probe-ocr`

Probe whether a configured model/deployment accepts image input and can read a
tiny generated image containing `MIMIR 42`.

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki probe-ocr \
  --provider azure-ai-foundry \
  --model gpt-5.4-mini \
  --json
```

The JSON result includes:

- `image_input_accepted`: the provider accepted image content.
- `ocr_text_matched`: the response contained the expected `MIMIR 42` text.
- `status`: `ok`, `image_accepted_ocr_mismatch`, or `unsupported_or_failed`.
- `usage`: token usage when the provider returns it.

Use `probe-ocr` before choosing a visual extraction model. A model can accept
images but still be a poor OCR choice if `ocr_text_matched` is false.

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

`report` does not re-read every source page to produce new enrichments, does not
call LLM providers, does not run visual OCR, and does not rewrite Onyx Markdown.
It is a read-only summarization pass over the current cache, existing
`knowledge/*.jsonl` indexes, existing per-page enrichment/visual artifacts, and
historical `runs/*` artifacts. Use it after `enrich` or `extract-visuals` when
you want fresh dashboards without changing page artifacts.

## `export-schema`

Export JSON Schema files for generated artifacts.

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki export-schema --out ./schemas
```

Schema files include enrichment, visual extraction, document index rows, quality
rows, themes, concepts, candidate entities, facts, failures, warnings, LLM usage
and run summary.

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
