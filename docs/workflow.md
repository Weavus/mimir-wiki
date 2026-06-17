# Workflow Guide

This document describes the implemented MVP1 workflow for turning a local
`mimir-confluence` cache into enriched artifacts, reports and Onyx POC Markdown.

## Inputs

The primary input is one local cache directory:

```text
cache/{dataset_name}/
  dataset.json
  manifest.jsonl
  manifest.summary.json
  errors.jsonl              # optional
  pages/{page_id}/
    metadata.json
    clean.md
    text.txt
    links.json
    conversion.json
    attachments/
```

The source cache is treated as evidence. `mimir-wiki` writes only derived
artifacts, such as per-page `enrichment.json`, generated JSONL indexes, reports,
run manifests and Onyx POC Markdown.

## Standard Flow

1. Validate the cache.
2. Enrich pages deterministically or with optional LLM tasks.
3. Write per-page `enrichment.json` artifacts.
4. Write stable global JSONL indexes under `knowledge/`.
5. Write Onyx POC Markdown under `dist/onyx-enriched/`.
6. Write human-readable reports under `reports/`.
7. Write run artifacts under `runs/{run_id}/`.

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki validate-cache \
  --cache ./cache/customer-identity-and-access-management-entra

UV_CACHE_DIR=.uv-cache uv run mimir-wiki enrich \
  --config ./mimir-wiki.yaml \
  --cache ./cache/customer-identity-and-access-management-entra \
  --changed-only

UV_CACHE_DIR=.uv-cache uv run mimir-wiki report \
  --cache ./cache/customer-identity-and-access-management-entra
```

## Deterministic Runs

Use deterministic runs for tests, smoke checks and reproducible local work.

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki enrich \
  --cache ./cache/customer-identity-and-access-management-entra \
  --provider none \
  --limit 25
```

Deterministic enrichment includes document classification, subtype inference,
hierarchy context, basic summaries, keywords, themes, concepts, candidate
entities, facts, operational signals, warnings and quality scoring.

## Live LLM Runs

Secrets belong in `.env`, not YAML. Foundry OpenAI v1 example:

```dotenv
AZURE_AI_FOUNDRY_ENDPOINT=https://example.services.ai.azure.com/openai/v1
AZURE_AI_FOUNDRY_API_KEY=...
AZURE_AI_FOUNDRY_DEPLOYMENT=gpt-5.5
```

Recommended LLM configuration uses task bundles to reduce calls:

```yaml
llm:
  provider: azure-ai-foundry
  model: gpt-5.5
  task_bundles:
    semantic:
      tasks: [summary, keywords, themes, concepts]
      model: gpt-5.5
      prompt_version: semantic-v1
    operational:
      tasks: [candidate_entities, operational_signals, quality_warnings]
      model: gpt-5.5
      prompt_version: operational-v1
```

With classification plus the two bundles, an unchunked page normally requires
three LLM calls.

## LLM Cache And Changed-Only

LLM responses are cached under `paths.llm_cache`, defaulting to
`.mimir-wiki/llm-cache`. Cache keys include source content hash, task or bundle,
prompt text, prompt version, provider, model and enrichment config hash.

`--changed-only` skips page processing when the page enrichment signature still
matches the current source hash and enrichment configuration.

## Hierarchy Workflow

Hierarchy context is computed for every processed page. It includes depth,
parent/root title, section path, page role, sibling/child counts and parent
context type. This context is used in enrichment, LLM prompts, Onyx Key Facts,

Examples of page roles:

- `runbook_index`
- `runbook_detail`
- `procedure_page`
- `test_report`
- `release_note`
- `reference_detail`
- `leaf_page`

## Onyx POC Markdown

Every Onyx file starts with first-line metadata:

```markdown
#ONYX_METADATA={"link":"...","file_display_name":"...","doc_updated_at":"..."}
```

Body layout is optimized for retrieval:

```text
Answer Summary
Key Facts
Source Links
Source Content
Additional Source Links
Enrichment Details
Source Metadata
```

Source content is included early for grounding. Images are rewritten to concise
placeholders. Source links are prioritized so high-value runbook, procedure,
Jira and Confluence links appear before low-value profile or mail links.

## Reports Workflow

Reports summarize cache health, document types, stale/deprecated documents,
high-value sources, missing owners, high-value hierarchy subtrees, attachments,

Use `report` when you want to regenerate reports without rerunning enrichment.

## Recommended Iteration Loop

1. Run `enrich --limit 25 --force` on a representative cache.
2. Inspect selected Onyx files and `knowledge/themes.jsonl`/`concepts.jsonl`.
3. Tune taxonomy, layout, classification aliases or scoring.
4. Rerun the same limit with `--force`.
5. Run the full cache after the sample is healthy.

## Generated Directories

These are generated and ignored by git:

- `.mimir-wiki/`
- `knowledge/`
- `reports/`
- `runs/`
- `dist/`
