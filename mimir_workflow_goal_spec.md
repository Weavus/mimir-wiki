# Mimir Workflow and Goal Specification

## 1. Purpose

Mimir is a personal operational knowledge system designed to turn messy, stale, scattered work documentation into a curated, human-reviewed, AI-searchable knowledge base.

The system is not intended to be just a chatbot over Confluence. Its goal is to create a repeatable pipeline that:

1. exports raw documentation from Confluence and other sources;
2. caches the source material locally in durable formats;
3. converts pages to clean Markdown;
4. enriches each document with structured metadata, summaries, entities, facts, quality scores, and warnings;
5. compiles higher-quality LLM-wiki-style documentation from the best available evidence;
6. allows human review and approval in a Markdown-first frontend such as Obsidian;
7. indexes only approved curated knowledge into Onyx for day-to-day AI search and Q&A.

The long-term goal is a trusted personal knowledge layer that supports incident response, change review, onboarding, runbook generation, application understanding, and documentation cleanup.

---

## 2. Core Principle

Mimir separates raw evidence from curated knowledge.

```text
Raw Confluence / KB / architecture docs
        ↓
Local source cache
        ↓
Clean Markdown + structured enrichment
        ↓
LLM-wiki compiler
        ↓
Human-reviewed Markdown knowledge base
        ↓
Onyx AI search / Q&A
```

Onyx should not be treated as the knowledge governance layer. Onyx is the AI consumption/search frontend. Mimir is the pipeline that extracts, scores, compiles, and governs knowledge.

---

## 3. Main Design Decisions

### 3.1 Use Custom Python for Confluence Export

A custom Python CLI is preferred over a generic Confluence CLI because the system needs more than page export.

The exporter must support:

- Confluence Personal Access Token authentication;
- crawling by space, page tree, label, CQL, or explicit page list;
- flexible link depth;
- optional cross-space crawling;
- page metadata capture;
- page hierarchy capture;
- attachment capture, later phase;
- raw HTML/source storage;
- Markdown conversion;
- incremental refresh using page version and content hashes;
- progress reporting via `tqdm` or `rich`;
- safe rate limiting and retry logic.

### 3.2 Store Raw and Derived Formats

Each Confluence page should be cached as raw and derived artifacts.

```text
cache/confluence/pages/{page_id}/
  metadata.json
  raw_storage.html
  raw_export_view.html
  clean.md
  text.txt
  enrichment.json
```

Use:

- `raw_storage.html` as the source-of-truth Confluence representation;
- `raw_export_view.html` as the best input for HTML-to-Markdown conversion;
- `clean.md` as the readable source document for humans, enrichment, and optional raw indexing;
- `text.txt` for lightweight search/scoring/deduplication;
- `enrichment.json` for structured document intelligence.

### 3.3 Enrichment Is JSON

Structured enrichment should be stored as JSON because it needs to be queried, validated, scored, diffed, and eventually imported into Postgres.

Markdown is for human-readable documentation. JSON is for machine-readable knowledge.

### 3.4 Compiled Knowledge Is Markdown

LLM-wiki-style compiled outputs should be Markdown files with YAML front matter.

These are the files humans review in Obsidian and that Onyx indexes after approval.

Example:

```yaml
---
title: ForgeRock Authentication Failure Runbook
page_type: runbook
status: needs_review
confidence: medium
source_documents:
  - confluence:IDENTITY:123456
  - confluence:ARCH:987654
tags:
  - mimir
  - identity
  - forgerock
  - runbook
---
```

### 3.5 Obsidian and Onyx Have Different Roles

Use both.

```text
Obsidian = human review, editing, linking, sense-making
Onyx     = AI search, chat, retrieval, Q&A
```

Onyx should read from a clean approved Markdown export, not blindly from the full Obsidian vault.

Recommended pattern:

```text
Mimir generated Markdown repo
        ↓
Obsidian opens it as a vault
        ↓
Human review/edit/approve
        ↓
Mimir publish creates dist/onyx-approved
        ↓
Onyx indexes approved content
```

---

## 4. System Components

## 4.1 `mimir-confluence`

A Python CLI for crawling and caching Confluence pages.

### Responsibilities

- authenticate to Confluence using PAT;
- test API connectivity;
- crawl spaces, page trees, labels, CQL results, explicit URLs/page IDs;
- support crawl depth limits;
- support same-space-only or cross-space crawling;
- cache page bodies and metadata;
- convert HTML to Markdown;
- create a manifest of exported pages;
- refresh changed pages only;
- avoid repeatedly repulling unchanged data.

### Example Commands

```bash
mimir-confluence test-auth

mimir-confluence export-space \
  --space IDENTITY \
  --out ./cache/confluence/identity

mimir-confluence export-tree \
  --page-id 123456789 \
  --max-depth 4 \
  --cross-space false \
  --out ./cache/confluence/forgerock

mimir-confluence refresh \
  --cache ./cache/confluence/identity \
  --changed-only

mimir-confluence build-markdown \
  --cache ./cache/confluence/identity
```

---

## 4.2 `mimir-wiki enrich`

A Python enrichment pipeline that analyses each exported Markdown document and creates structured metadata.

### Responsibilities

For each source page, generate:

- document type;
- short summary;
- detailed summary;
- keywords;
- categories;
- entities;
- operational signals;
- quality scores;
- staleness warnings;
- extracted facts;
- open questions;
- confidence score.

### Example Command

```bash
mimir-wiki enrich \
  --cache ./cache/confluence/identity \
  --llm-provider azure-openai \
  --changed-only \
  --workers 8
```

### Example `enrichment.json`

```json
{
  "document_id": "confluence:IDENTITY:123456",
  "document_type": "runbook",
  "short_summary": "Explains how to diagnose and recover authentication failures for Example App.",
  "keywords": ["authentication", "LDAP", "ForgeRock", "SAML"],
  "categories": ["identity", "runbook", "production support"],
  "entities": {
    "applications": ["Example App"],
    "technologies": ["LDAP", "ForgeRock"],
    "teams": ["Identity SRE"],
    "databases": [],
    "queues": [],
    "apis": []
  },
  "operational_signals": {
    "has_owner": true,
    "has_support_group": true,
    "has_runbook_steps": true,
    "has_validation_steps": true,
    "has_backout_steps": false,
    "has_monitoring_links": true,
    "has_escalation_path": true,
    "has_dependencies": true
  },
  "quality": {
    "freshness_score": 82,
    "authority_score": 75,
    "completeness_score": 78,
    "operational_value_score": 88,
    "overall_score": 81
  },
  "warnings": [
    "No explicit backout section found",
    "One monitoring link may be stale"
  ],
  "confidence": 0.79
}
```

---

## 4.3 `mimir-wiki compile`

The LLM-wiki compiler that turns many enriched source documents into curated Markdown pages.

This is the key transformation step.

It does not overwrite source documents. It uses them as evidence to produce new, better documentation.

### Responsibilities

`mimir-wiki compile` should:

1. load the exported Markdown cache;
2. load `metadata.json` and `enrichment.json` for each page;
3. build indexes of documents, entities, facts, source quality, and relationships;
4. select a target entity or page type;
5. find relevant source documents;
6. rank sources by relevance, quality, freshness, authority, and operational value;
7. extract or load structured facts;
8. identify contradictions and gaps;
9. create a page plan;
10. use an LLM to draft a structured Markdown page;
11. validate that the generated page is grounded in evidence;
12. write the result to the Obsidian review vault with `status: needs_review`.

### Example Commands

```bash
mimir-wiki compile application \
  --entity "ForgeRock" \
  --cache ./cache/confluence/identity \
  --out ./vault/00-Review

mimir-wiki compile runbook \
  --entity "ForgeRock" \
  --scenario "authentication failure" \
  --cache ./cache/confluence/identity \
  --out ./vault/00-Review

mimir-wiki compile quality-report \
  --entity "ForgeRock" \
  --cache ./cache/confluence/identity \
  --out ./vault/00-Review

mimir-wiki compile all \
  --cache ./cache/confluence/identity \
  --changed-only \
  --out ./vault/00-Review
```

---

## 5. What `mimir-wiki compile` Does With Exported Markdown

### 5.1 Load Source Documents

The compiler reads:

```text
metadata.json
clean.md
enrichment.json
```

for each cached source page.

### 5.2 Build a Working Knowledge Index

The compiler builds internal maps:

```text
entity → related documents
document → entities
document → quality score
document → source authority
entity → aliases
entity → candidate facts
fact → source evidence
```

This lets the compiler answer:

- Which docs are relevant to this application?
- Which are current?
- Which are stale?
- Which are runbooks?
- Which contain ownership/support details?
- Which contradict each other?

### 5.3 Select Best Sources

For a target such as `ForgeRock`, the compiler may find:

```text
ForgeRock Support Runbook
ForgeRock Architecture Overview
Identity Platform Support Model
ForgeRock 2022 Migration Notes
Authentication Failure KB
Old Cutover Plan
```

It ranks them by:

- relevance;
- document type;
- freshness;
- source authority;
- operational value;
- quality score;
- staleness warnings.

### 5.4 Extract Facts

The compiler works from structured facts where possible.

Example:

```json
{
  "subject": "ForgeRock",
  "predicate": "depends_on",
  "object": "LDAP",
  "source_id": "confluence:IDENTITY:123456",
  "confidence": 0.86,
  "evidence": "Authentication requests are validated against LDAP."
}
```

Useful predicates:

```text
owned_by
supported_by
depends_on
uses_database
uses_queue
has_dashboard
has_log_source
has_runbook
has_known_failure_mode
has_recovery_step
has_validation_step
has_escalation_path
has_environment
has_region
deprecated_by
replaced_by
```

### 5.5 Detect Contradictions

The compiler should not silently resolve conflicting evidence.

Example output section:

```markdown
## Conflicting Evidence

| Topic | Source | Value | Updated | Confidence |
|---|---|---|---|---|
| Support group | ForgeRock Support Runbook | Identity SRE | 2026-04-12 | High |
| Support group | Old Migration Notes | Access Management L2 | 2022-03-08 | Low |

Current interpretation: Identity SRE appears most likely, but this should be reviewed.
```

### 5.6 Detect Gaps

The compiler should explicitly surface missing information.

Examples:

```text
No current owner found.
No validation steps found.
No rollback procedure found.
No monitoring dashboard found.
No current architecture diagram found.
```

### 5.7 Create a Page Plan

The compiler creates a structured plan before generation.

Example application card plan:

```json
{
  "page_type": "application_card",
  "entity": "ForgeRock",
  "sections": [
    "Summary",
    "Ownership",
    "Support Model",
    "Architecture",
    "Dependencies",
    "Monitoring",
    "Known Failure Modes",
    "Runbooks",
    "Conflicting Evidence",
    "Open Questions",
    "Source Evidence"
  ]
}
```

### 5.8 Use AI to Draft the Page

The compiler uses an LLM to draft the Markdown page, but only from supplied facts and source excerpts.

The LLM should not freely invent information.

The prompt should include rules such as:

```text
Use only the provided facts and source excerpts.
Do not invent missing owners, recovery steps, dashboards, or dependencies.
Where evidence is weak, say so.
Where sources disagree, include a conflict section.
Use the required Markdown template.
Include source evidence.
```

### 5.9 Validate the Output

The generated page should be validated before being written.

Validation checks:

- YAML front matter exists;
- status is `needs_review`, not `approved`;
- required sections exist;
- source IDs are valid;
- evidence table exists;
- internal links are valid where possible;
- unsupported source IDs are not introduced;
- confidence field is present.

---

## 6. Human Review Workflow

Generated pages are not automatically trusted.

They enter the Obsidian vault as drafts or review candidates.

```yaml
status: needs_review
```

The human reviewer edits and updates the status.

```yaml
status: approved
reviewed_by: Weavus
reviewed_at: 2026-06-16
```

Only approved pages should be indexed into the primary Onyx knowledge source.

Suggested statuses:

```text
generated
needs_review
approved
rejected
superseded
deprecated
```

---

## 7. Onyx Publishing Workflow

Onyx should index the approved curated Markdown, not the whole messy raw cache.

Recommended command:

```bash
mimir-wiki publish-onyx \
  --vault ./vault \
  --out ./dist/onyx-approved \
  --status approved
```

This command should:

1. scan the Obsidian vault;
2. read YAML front matter;
3. select only `status: approved` pages;
4. optionally transform Obsidian wikilinks into standard Markdown links;
5. optionally add Onyx-friendly metadata;
6. copy the result into `dist/onyx-approved`;
7. allow Onyx to index that folder via its File connector.

Recommended Onyx sources:

```text
Mimir - Approved Knowledge
Mimir - Approved Runbooks
Mimir - Quality Reports
Raw - Confluence Evidence - Dev Only
```

The normal user experience should prioritise approved Mimir content. Raw Confluence should be a fallback or development/debug source.

---

## 8. Repository Layout

Recommended repository layout:

```text
mimir-knowledge/
  source_cache/
    confluence/
      identity/
        manifest.jsonl
        enrichment_manifest.jsonl
        pages/
          123456789/
            metadata.json
            raw_storage.html
            raw_export_view.html
            clean.md
            text.txt
            enrichment.json

  vault/
    00 Review/
      applications/
      runbooks/
      architecture/
      quality_reports/

    10 Applications/
    20 Runbooks/
    30 Architecture/
    40 Dependencies/
    50 Teams/
    60 Quality Reports/

  dist/
    onyx-approved/
      applications/
      runbooks/
      architecture/
      dependencies/
      quality_reports/

  config/
    spaces.yaml
    authority_rules.yaml
    compiler_templates.yaml

  logs/
```

---

## 9. Recommended Python Stack

### Core

```text
typer
rich
tqdm
pydantic
orjson
httpx
beautifulsoup4
lxml
markdownify
markdown-it-py
python-frontmatter
jinja2
rapidfuzz
```

### Optional / Later

```text
docling
pymupdf
python-docx
gliner
keybert
sqlalchemy
alembic
pgvector
```

### AI Provider

Use Azure OpenAI / Azure AI Foundry or an OpenAI-compatible provider for:

- summaries;
- document classification;
- operational signal interpretation;
- fact extraction;
- contradiction explanation;
- page drafting;
- semantic validation.

Use Ollama optionally for local experimentation.

---

## 10. AI Usage Principles

Use AI for interpretation and drafting, not for deterministic source handling.

### Use AI For

```text
document classification
summaries
entity extraction
fact extraction
staleness interpretation
gap detection
contradiction explanation
page drafting
runbook generation
architecture summary generation
```

### Do Not Use AI For

```text
page IDs
URLs
timestamps
labels
content hashes
file paths
approval status
basic freshness date calculation
source inclusion rules
publishing decisions
```

### Best Pattern

```text
Deterministic code decides what evidence exists.
AI helps interpret and draft.
Deterministic validation checks the output.
Human review approves it.
```

---

## 11. Target Compiled Page Types

### 11.1 Application Card

```markdown
# Application Name

## Summary
## Business Purpose
## Ownership
## Support Model
## Environments
## Architecture
## Dependencies
## Monitoring
## Known Failure Modes
## Runbooks
## Conflicting Evidence
## Open Questions
## Source Evidence
```

### 11.2 Scenario Runbook

```markdown
# Application Name — Scenario Runbook

## Purpose
## Scope
## Symptoms
## Impact
## Prerequisites
## Initial Triage
## Diagnostic Checks
## Recovery Steps
## Validation
## Escalation
## Known Risks
## Related Systems
## Conflicting Evidence
## Open Questions
## Source Evidence
```

### 11.3 Architecture Summary

```markdown
# Platform / Application Architecture Summary

## Current Understanding
## Components
## Data Flows
## Authentication Model
## Dependencies
## Resilience / Failover
## Known Limitations
## Conflicting Evidence
## Open Questions
## Source Evidence
```

### 11.4 Documentation Quality Report

```markdown
# Application Documentation Quality Report

## Summary
## Best Available Sources
## Stale Sources
## Conflicting Sources
## Missing Information
## Duplicate / Near-Duplicate Docs
## Recommended Cleanup Actions
## Source Evidence
```

---

## 12. MVP Scope

The first MVP should be narrow.

Recommended MVP:

```text
One Confluence space or one page tree
One application or platform
One application card
One scenario runbook
One documentation quality report
Obsidian review
Onyx indexing of approved outputs
```

### MVP Commands

```bash
mimir-confluence test-auth

mimir-confluence export-tree \
  --page-id 123456789 \
  --max-depth 4 \
  --cross-space false \
  --out ./source_cache/confluence/identity

mimir-wiki enrich \
  --cache ./source_cache/confluence/identity \
  --changed-only

mimir-wiki compile application \
  --entity "ForgeRock" \
  --cache ./source_cache/confluence/identity \
  --out ./vault/00-Review

mimir-wiki compile runbook \
  --entity "ForgeRock" \
  --scenario "authentication failure" \
  --cache ./source_cache/confluence/identity \
  --out ./vault/00-Review

mimir-wiki compile quality-report \
  --entity "ForgeRock" \
  --cache ./source_cache/confluence/identity \
  --out ./vault/00-Review

mimir-wiki publish-onyx \
  --vault ./vault \
  --out ./dist/onyx-approved \
  --status approved
```

---

## 13. Success Criteria

Mimir MVP is successful if it can answer these questions for one target application:

```text
What is this application?
Who owns it?
Who supports it?
What does it depend on?
Where are its runbooks?
What are the known failure modes?
What monitoring/logging exists?
What docs are stale?
Where do docs contradict each other?
What information is missing?
Can an approved runbook be indexed into Onyx and used for Q&A?
```

---

## 14. Future Enhancements

Potential later additions:

- Postgres-backed document registry;
- entity/fact database;
- dependency graph;
- application inventory dashboard;
- stale-document dashboard;
- contradiction review queue;
- ServiceNow KB ingestion;
- incident/PIR ingestion;
- change record enrichment;
- PDF/Word architecture doc parsing with Docling;
- automatic Onyx indexing/export sync;
- publishing approved docs back to Confluence;
- MkDocs/Material static documentation site;
- integration with Odin, Forseti, Huginn, and Muninn.

---

## 15. Final Target Model

The final Mimir workflow should look like this:

```text
Confluence / KB / PDFs / architecture docs
        ↓
mimir-confluence export/cache
        ↓
clean Markdown + metadata JSON
        ↓
mimir-wiki enrich
        ↓
enrichment JSON + facts + quality scores
        ↓
mimir-wiki compile
        ↓
LLM-wiki-style generated Markdown
        ↓
Obsidian review/edit/approval
        ↓
mimir-wiki publish-onyx
        ↓
Onyx indexes approved curated knowledge
        ↓
Daily AI search/Q&A over trusted documentation
```

This keeps the system clean:

```text
Raw evidence remains traceable.
AI-generated content is reviewable.
Approved knowledge is separated from drafts.
Onyx stays focused on high-quality content.
Mimir owns enrichment, compilation, and governance.
```
