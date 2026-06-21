# Architecture

`mimir-wiki` is a file-based enrichment pipeline. It reads one
`mimir-confluence` cache at a time and writes versioned artifacts that are easy

## Package Layout

```text
src/mimir_wiki/
  cli.py
  config.py
  constants.py
  cache_reader.py
  hierarchy.py
  visual_extraction.py
  pipeline.py
  reports.py
  schema_export.py
  scoring.py
  schemas.py
  utils.py
  enrichers/
    deterministic.py
    llm.py
    prompts/
  llm/
    base.py
    probe.py
  writers/
    artifacts.py
    onyx_markdown.py
```

## Core Modules

- `cli.py`: Typer/Rich command surface, progress, JSON output and structured logging.
- `config.py`: config model and precedence logic.
- `cache_reader.py`: real cache parser and validator.
- `hierarchy.py`: deterministic hierarchy context, page roles and hierarchy-aware quality adjustment.
- `visual_extraction.py`: local-cache-only image discovery and multimodal OCR/caption extraction.
- `enrichers/deterministic.py`: deterministic classification, taxonomy, facts, entities, signals and quality baseline.
- `enrichers/llm.py`: LLM task and bundle execution, response caching, response validation and merge logic.
- `llm/base.py`: provider abstractions, HTTP providers, retry/backoff/rate limit wrapper.
- `llm/probe.py`: multimodal OCR capability probe helpers.
- `writers/artifacts.py`: enrichment JSON and global JSONL writers.
- `writers/onyx_markdown.py`: Onyx POC Markdown rendering and redaction.
- `reports.py`: human-readable report generation.
- `schemas.py`: Pydantic contracts for cache records and generated artifacts.
- `schema_export.py`: JSON Schema export for generated artifacts.

## Data Flow

```text
CacheReader
  -> PageBundle
  -> optional visual_extraction.json
  -> hierarchy context
  -> deterministic enrichment
  -> optional LLM enrichment and merge
  -> per-page enrichment.json
  -> global JSONL indexes
  -> Onyx POC Markdown
  -> reports
  -> run artifacts
```

## Config Precedence

Config is resolved in this order:

1. built-in defaults
2. config file
3. selected profile
4. `.env`
5. process environment variables
6. CLI flags

Blank `.env` override values are ignored. Provider secrets are read from
environment variable names configured in YAML.

## LLM Architecture

All live provider calls go through `RateLimitedLLMClient`, which handles
concurrency limits, optional request rate limits, timeouts, retryable HTTP
statuses, `Retry-After`, exponential backoff and retry event logging.

Supported providers:

- `none`
- `openai`
- `azure-openai`
- `azure-ai-foundry`
- `openai-compatible`

Azure AI Foundry supports `/openai/v1` Responses API endpoints and explicit
chat-completions endpoints. OpenAI-compatible providers use chat completions.

LLM task bundles reduce call count by requesting related fields in one response.
Bundled responses are validated through Pydantic and merged into the same
enrichment fields used by individual tasks.

`probe-ocr` uses the same provider configuration style to send a generated image
containing `MIMIR 42`. It is a capability check for image input and basic OCR,
not a cache-processing command.

## Visual Extraction Architecture

Visual extraction is a separate optional command because it uses multimodal live
model calls and can be expensive on image-heavy spaces.

```text
CacheReader
  -> PageBundle.clean_markdown image refs
  -> pages/{page_id}/attachments/ local image files
  -> multimodal provider call
  -> pages/{page_id}/visual_extraction.json
  -> later enrich reads visual_extraction.json
  -> visual_content_extracted review flag and Onyx extracted visual section
```

`mimir-wiki` does not fetch source images from Confluence or arbitrary remote
URLs. If `clean.md` contains a Confluence image URL, visual extraction resolves
that URL to a matching local file in the current page's attachments, or in
another exported page's `pages/{page_id}/attachments/` directory for
`/download/attachments/{page_id}/...` URLs. If the file is absent, the image is
recorded as skipped with
`remote_source_not_in_cache`. Attachment download and source-system access remain
the responsibility of `mimir-confluence`.

The visual extraction artifact stores OCR text, captions, confidence, source
path/reference, source hash, provider, model and prompt version. It is treated as
source-derived evidence. Successful complete extraction changes enrichment review
flags from `visual_content_missing` to `visual_content_extracted`; partial
extraction keeps the missing-content review flags.

## Hierarchy Model

Hierarchy context is deterministic and stored on each `Enrichment`:

```json
{
  "depth": 7,
  "root_title": "Customer Identity & Access Management - Entra",
  "parent_title": "IAM SCIM API - Runbook",
  "section_path": "... > IAM SCIM API - Runbook > Database information",
  "page_role": "runbook_detail",
  "parent_context_type": "runbook",
  "sibling_count": 12,
  "child_count": 0
}
```

Hierarchy is used for LLM prompt context, Onyx Key Facts, document index fields,
quality scoring and high-value subtree reports.

## Onyx Markdown Architecture

Onyx Markdown is generated from source evidence plus enrichment. The first line
is always compact `#ONYX_METADATA={...}` JSON. High-cardinality values such as
run ID and source hashes live in the body, not first-line metadata.

The body is ordered for retrieval:

1. `Answer Summary`
2. `Key Facts`
3. `Source Links`
4. `Source Content`
5. `Extracted Visual Content` when available
6. `Additional Source Links`
7. `Enrichment Details`
8. `Source Metadata`

The writer filters noisy displayed entities, limits early source links, rewrites
images to placeholders, applies redaction, and keeps original source content for
grounding. When `visual_extraction.json` contains successful image extractions,
the writer also renders extracted OCR/captions as source-derived evidence.

## Artifact Contracts

Generated JSON and JSONL rows include `schema_version: mimir-wiki/v1`. Global
JSONL files are sorted for stable diffs. Atomic writes are used for generated
files.

Main artifacts:

- `pages/{page_id}/enrichment.json`
- `pages/{page_id}/visual_extraction.json`
- `knowledge/document_index.jsonl`
- `knowledge/quality_scores.jsonl`
- `knowledge/themes.jsonl`
- `knowledge/concepts.jsonl`
- `knowledge/candidate_entities.jsonl`
- `knowledge/facts.jsonl`
- `dist/onyx-enriched/{dataset_name}/{space_key}/{page_id}-{slug}.md`
- `reports/*.md`
- `runs/{run_id}/*.jsonl`
- `schemas/*.schema.json`

## Concurrency And Cancellation

Page processing uses `processing.page_workers`. LLM-enabled runs cap concurrent
page work by `processing.llm_workers`. Page-level outputs are distinct files;
global JSONL files and reports are written after workers complete.

Cancellation cancels pending futures, lets running page writes finish, writes
partial run artifacts and exits as partial success.

## Testing Strategy

Tests cover observed cache shapes, validation, deterministic enrichment, LLM
retry/cache behavior, Foundry Responses API behavior, Onyx metadata/layout,
redaction, hierarchy context, local visual extraction, reports, schema export and
CLI JSON/log behavior.
The real-cache smoke test is opt-in with `MIMIR_WIKI_RUN_SMOKE=1`.
