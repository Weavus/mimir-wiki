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
2. Optionally extract visual evidence from downloaded image attachments.
3. Enrich pages deterministically or with optional LLM tasks.
4. Write per-page `enrichment.json` artifacts.
5. Write stable global JSONL indexes under `knowledge/`.
6. Write Onyx POC Markdown under `dist/onyx-enriched/`.
7. Write human-readable reports under `reports/`.
8. Write run artifacts under `runs/{run_id}/`.

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki validate-cache \
  --cache ./cache/customer-identity-and-access-management-entra

UV_CACHE_DIR=.uv-cache uv run mimir-wiki extract-visuals \
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

`enrich` is the artifact-producing step. It reads source pages and existing
visual extraction artifacts, then writes or refreshes per-page enrichment JSON,
global indexes, Onyx POC Markdown and reports.

`report` is different: it regenerates report Markdown from the current cache,
existing knowledge indexes, existing enrichment/visual artifacts and historical
run artifacts. It does not call LLM providers, does not extract images, does not
rewrite `enrichment.json`, and does not rewrite Onyx Markdown. Use `report` when
you want fresh summaries after existing artifacts are already present.

## Visual Extraction Workflow

`mimir-wiki extract-visuals` extracts OCR text and captions from visual source
artifacts that are already present in the local cache. It writes one source-
derived artifact per processed page:

```text
cache/{dataset_name}/pages/{page_id}/visual_extraction.json
```

This command is intentionally explicit and separate from `enrich` because it
makes live multimodal model calls. It defaults to `gpt-5.4-mini`, based on the
current local capability probe result.

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki extract-visuals \
  --cache ./cache/carel3support \
  --provider azure-ai-foundry \
  --model gpt-5.4-mini
```

Boundary rule: `mimir-wiki` never connects to Confluence to fetch source images.
It only reads local cache evidence produced by `mimir-confluence`:

- downloaded image files under `pages/{page_id}/attachments/`
- Markdown image URLs that resolve to local attachment filenames, including
  Confluence `/download/attachments/{page_id}/...` URLs for other pages already
  present in the same cache
- embedded `data:image/...` references in `clean.md`

If a remote image URL is present in `clean.md` but the corresponding file is not
available in the current page's attachments or another exported page's
attachments, extraction records that image as skipped with
`remote_source_not_in_cache`. Rerun `mimir-confluence` with attachment export
enabled for pages where the missing image is important.

Visual source selection is deterministic and auditable:

- every candidate image source is discovered before applying page caps
- candidates are ranked using context from nearby headings, Markdown context,
  filenames and high-value terms such as architecture, diagram, runbook,
  incident, dashboard, alarm, terminal, command and log
- exact duplicate image content is reused by `content_sha256`, so one extraction
  can serve repeated images on the same page or later pages in the run
- obvious logo/icon/placeholder/tiny images are skipped before provider calls
- report-like pages use `visual_extraction.report_page_max_images` as an
  adaptive cap below the global `max_images_per_page`
- repeated dashboard/chart/report visuals are sampled by representative group
- omitted images are written to `runs/{run_id}/visual_omitted_images.jsonl` with
  page, source, hash when available, selection score, nearby heading and reason
- live visual OCR calls run through the shared LLM client with bounded async
  concurrency; concurrency is tracked per provider/model and is reduced when
  `429` rate limits are observed, then increased gradually after successful calls

Useful visual extraction config defaults:

```yaml
visual_extraction:
  max_images_per_page: 20
  skip_low_value_images: true
  min_image_pixels: 4096
  adaptive_page_caps: true
  report_page_max_images: 12
  representative_group_sampling: true
  max_images_per_representative_group: 3

llm:
  max_concurrency: 4
  requests_per_minute: null
  tokens_per_minute: null
  adaptive_concurrency: true
  adaptive_initial_concurrency: 4
  adaptive_min_concurrency: 1
```

Leave `requests_per_minute` and `tokens_per_minute` unset when provider limits
are unknown. The adaptive limiter still reacts to provider `429` responses by
reducing only the affected provider/model concurrency and honoring `Retry-After`
when present.

Skipped remote images need triage by source type:

- Confluence `/download/attachments/{page_id}/...` URLs are resolved against the
  matching page's local `attachments/` directory when that page exists in the
  same cache. Remaining skips usually mean the linked/embedded page is outside
  the export or the attachment was not downloaded.
- Confluence `/download/attachments/embedded-page/{space}/{title}/...` URLs are
  embedded-page attachments. Same-space embedded pages may be recoverable by
  expanding the crawl or improving embedded-page attachment handling.
- Confluence generated/plugin URLs such as PlantUML or placeholders are not
  normal attachments. They need explicit exporter support if they matter.
- Non-Confluence hosts such as Jira, Jive/TheHub, Lucid, Googleusercontent,
  GitLab or ServiceNow are external source systems. `mimir-confluence` should
  not be expected to collect those unless separate authenticated fetch support is
  intentionally added for that source.

After visual extraction, rerun enrichment so review flags and Onyx Markdown pick
up the extraction results:

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki enrich \
  --cache ./cache/carel3support \
  --provider none \
  --force
```

When every discovered image for a page is successfully extracted, enrichment adds
`visual_content_extracted`. If extraction only partially succeeds, enrichment
keeps `visual_content_missing` and adds `visual_content_partially_extracted` so
the page remains in the manual review queue.

## OCR Capability Probe

Use `probe-ocr` to verify that a provider/model accepts image input before
running extraction over a cache:

```bash
UV_CACHE_DIR=.uv-cache uv run mimir-wiki probe-ocr \
  --provider azure-ai-foundry \
  --model gpt-5.4-mini \
  --json
```

The probe sends a tiny generated PNG containing `MIMIR 42`. Treat
`image_input_accepted: true` and `ocr_text_matched: true` as the minimum signal
that the model is usable for visual extraction. If image input is accepted but
the OCR text does not match, test more samples before using that model broadly.

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

Processing concurrency is split by concern. `processing.page_workers` controls
page-level work, while live LLM calls are bounded by `llm.max_concurrency` and
the adaptive per-model limiter. `processing.writer_workers` remains in config as
a reserved future knob; MVP artifact writing is synchronous and deterministic.

Interactive `enrich` and `extract-visuals` runs show a live dashboard with
progress, ETA, throughput, in-flight LLM calls, retries, `429` rate limits,
adaptive concurrency state and current work. Non-interactive modes such as
`--json`, `--quiet`, CI and redirected output remain script-friendly.

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
Extracted Visual Content    # when visual_extraction.json has successful images
Additional Source Links
Enrichment Details
Source Metadata
```

Source content is included early for grounding. Images are rewritten to concise
placeholders. Source links are prioritized so high-value runbook, procedure,
Jira and Confluence links appear before low-value profile or mail links.

When successful `visual_extraction.json` artifacts exist, Onyx Markdown also
includes an `Extracted Visual Content` section containing source-derived OCR text
and captions. This section is evidence extracted from source images, not approved
curated wiki prose. The Onyx visual section deduplicates repeated image hashes,
limits the number of images rendered, truncates long OCR text, and labels OCR as
review evidence because recognition errors are possible.

## Reports Workflow

Reports summarize cache health, document types, stale/deprecated documents,
high-value sources, missing owners, high-value hierarchy subtrees, attachments,
LLM usage, page failures, visual extraction health and follow-up candidates.

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
