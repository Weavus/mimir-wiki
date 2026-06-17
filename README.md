# mimir-wiki

`mimir-wiki` validates and enriches local `mimir-confluence` cache exports. MVP
outputs are deterministic enrichment artifacts, stable JSONL indexes, reports,
and Onyx POC Markdown with first-line `#ONYX_METADATA={...}` metadata.

## Setup

```bash
uv sync --extra dev
```

Provider secrets should live in `.env`, not in YAML. Start from the examples:

```bash
cp mimir-wiki.yaml.example mimir-wiki.yaml
cp .env.example .env
```

Keep `mimir-wiki.yaml` limited to configuration and environment variable names.
Populate `.env` only when running live LLM enrichment.

## Deterministic MVP Flow

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki validate-cache \
  --cache ./cache/customer-identity-and-access-management-entra

UV_CACHE_DIR=.uv-cache uv run mimir-wiki enrich \
  --cache ./cache/customer-identity-and-access-management-entra \
  --provider none \
  --changed-only \
  --emit-onyx-markdown

UV_CACHE_DIR=.uv-cache uv run mimir-wiki report \
  --cache ./cache/customer-identity-and-access-management-entra
```

Useful development run:

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki enrich \
  --cache ./cache/carel3support \
  --provider none \
  --limit 25 \
  --log-file ./runs/enrich-dev.jsonl
```

Scriptable JSON output:

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki validate-cache \
  --cache ./cache/carel3support \
  --json \
  --quiet
```

Plan a run without writing artifacts:

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki enrich \
  --cache ./cache/carel3support \
  --provider none \
  --dry-run \
  --limit 25
```

Reprocess only changed pages. Changed-only compares source content hash, schema
version, prompt version, provider, model/deployment, enabled LLM tasks, and the
enrichment config hash.

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki enrich \
  --cache ./cache/carel3support \
  --provider none \
  --changed-only
```

Write per-command and per-page JSONL events:

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki enrich \
  --cache ./cache/carel3support \
  --provider none \
  --log-file ./runs/enrich-events.jsonl
```

Export JSON schemas for generated artifacts:

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki export-schema --out ./schemas
```

## CLI Options

Common options:

- `--config PATH`: load `mimir-wiki.yaml` from a specific path.
- `--profile NAME`: apply a named profile from the config file after base config.
- `--cache PATH`: source `mimir-confluence` cache directory.
- `--out PATH`: command-specific output directory. For `enrich`, this overrides `knowledge`; for `report` and `validate-cache`, this overrides `reports`.
- `--limit INT`: process only the first N manifest pages, useful for smoke tests.
- `--dry-run`: validate and plan without writing files.
- `--json`: emit machine-readable JSON and suppress decorative output.
- `--quiet`: suppress human summaries.
- `--no-color`: disable color output.
- `--verbose`: include tracebacks for unexpected errors.
- `--log-file PATH`: write JSONL command, page, retry, cancellation, and artifact events.

`enrich` options:

- `--provider none|openai|azure-openai|azure-ai-foundry|openai-compatible`: select the LLM provider. Use `none` for deterministic local runs.
- `--enable-llm / --disable-llm`: enable or disable configured LLM tasks for this run.
- `--llm-task TASK`: enable one LLM task; repeat for multiple tasks.
- `--changed-only`: skip page enrichment when the source and enrichment signature are unchanged.
- `--force`: reprocess pages even when changed-only signatures match.
- `--document-type-filter TYPE`: keep only pages classified as the selected type.
- `--space-filter SPACE`: process only one Confluence space key.
- `--emit-onyx-markdown / --no-emit-onyx-markdown`: write or suppress Onyx POC Markdown.
- `--onyx-out PATH`: override the Onyx POC Markdown output root.
- `--include-source-content / --no-include-source-content`: include or omit cleaned source Markdown in Onyx output.
- `--redaction redact|fail|off`: redact secrets, fail on matches, or disable redaction.

Cancellation behavior:

- Pressing `Ctrl-C` during `enrich` cancels pending page work.
- Already running page workers are allowed to finish their current file writes.
- Completed page results, global JSONL files, reports, warnings, failures, and run summary are still written.
- The run exits as partial success and records `run_cancelled` in `warnings.jsonl` and `--log-file` events.
- `pages_cancelled` is included in the run summary counts.

## Generated Schemas

`mimir-wiki export-schema` writes JSON Schema files for the generated artifact
contracts. These schemas are intended for downstream validation, ingestion jobs,
and compatibility checks. Schema files are generated from the Pydantic models in
`src/mimir_wiki/schemas.py` and include:

- `enrichment.schema.json` for per-page `pages/{page_id}/enrichment.json`.
- `document_index_row.schema.json` for `knowledge/document_index.jsonl` rows.
- `quality_score_row.schema.json` for `knowledge/quality_scores.jsonl` rows.
- `theme_row.schema.json` for `knowledge/themes.jsonl` rows.
- `concept_row.schema.json` for `knowledge/concepts.jsonl` rows.
- `candidate_entity_row.schema.json` for `knowledge/candidate_entities.jsonl` rows.
- `candidate_fact_row.schema.json` for `knowledge/facts.jsonl` rows.
- `page_failure.schema.json` for `runs/{run_id}/page_failures.jsonl` rows.
- `warning_record.schema.json` for `runs/{run_id}/warnings.jsonl` rows.
- `llm_usage.schema.json` for `runs/{run_id}/llm_usage.jsonl` rows.
- `run_summary.schema.json` for `runs/{run_id}/summary.json`.

Compatibility expectations:

- Generated JSON and JSONL rows include `schema_version: mimir-wiki/v1`.
- Global JSONL rows are sorted by stable keys where applicable.
- New optional fields may be added within the same schema version.
- Removing fields, changing required field meanings, or changing enum semantics
  should use a new schema version.
- Consumers should ignore unknown fields unless they explicitly require a strict
  schema validation mode.

## Live LLM Runs

Set `.env` values such as:

```dotenv
AZURE_OPENAI_ENDPOINT=https://example.openai.azure.com
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_DEPLOYMENT=gpt-4.1
AZURE_OPENAI_API_VERSION=2025-01-01-preview
```

For Azure AI Foundry OpenAI v1 endpoints like the Foundry sample code:

```dotenv
AZURE_AI_FOUNDRY_ENDPOINT=https://example.services.ai.azure.com/openai/v1
AZURE_AI_FOUNDRY_API_KEY=...
AZURE_AI_FOUNDRY_DEPLOYMENT=gpt-5.5
```

Use:

```yaml
llm:
  provider: azure-ai-foundry
  model: gpt-5.5
  azure_ai_foundry:
    endpoint_env: AZURE_AI_FOUNDRY_ENDPOINT
    api_key_env: AZURE_AI_FOUNDRY_API_KEY
    deployment_env: AZURE_AI_FOUNDRY_DEPLOYMENT
    api_mode: auto
```

No `AZURE_OPENAI_API_VERSION` is needed for the Foundry `/openai/v1` Responses API path.

Then run with a profile or flags:

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki enrich \
  --config ./mimir-wiki.yaml \
  --profile azure-openai \
  --cache ./cache/carel3support \
  --enable-llm \
  --llm-task summary \
  --llm-task themes \
  --limit 10
```

If you want LLM cost estimates in `reports/llm_usage.md`, add rates to
`mimir-wiki.yaml` under `llm.costs_usd_per_1k_tokens`:

```yaml
llm:
  costs_usd_per_1k_tokens:
    azure-openai:gpt-4.1:
      input: 0.00
      output: 0.00
```

To reduce provider round trips, configure task bundles. Bundled tasks are sent
as one LLM request per page chunk and merged back into the normal enrichment
fields:

```yaml
llm:
  task_bundles:
    semantic:
      tasks:
        - summary
        - keywords
        - themes
        - concepts
      model: gpt-5.5
      prompt_version: semantic-v1
    operational:
      tasks:
        - candidate_entities
        - operational_signals
        - quality_warnings
      model: gpt-5.5
      prompt_version: operational-v1
```

With these bundles enabled, the default eight tasks become three calls per page:
`classification`, `bundle:semantic`, and `bundle:operational`.

## Outputs

- `pages/{page_id}/enrichment.json`
- `knowledge/document_index.jsonl`
- `knowledge/quality_scores.jsonl`
- `knowledge/themes.jsonl`
- `knowledge/concepts.jsonl`
- `knowledge/candidate_entities.jsonl`
- `knowledge/facts.jsonl`
- `dist/onyx-enriched/{dataset_name}/{space_key}/{page_id}-{slug}.md`
- `reports/*.md`
- `runs/{run_id}/summary.json`
- `runs/{run_id}/page_failures.jsonl`
- `runs/{run_id}/warnings.jsonl`
- `runs/{run_id}/llm_usage.jsonl`
- `schemas/*.schema.json` when `export-schema` is run

## Quality Checks

```bash
UV_CACHE_DIR=.uv-cache uv run --extra dev scripts/quality.sh
```

Optional local smoke tests against real caches are disabled by default:

```bash
MIMIR_WIKI_RUN_SMOKE=1 UV_CACHE_DIR=.uv-cache uv run --extra dev pytest -m smoke
```
