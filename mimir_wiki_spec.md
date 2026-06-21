# Mimir Wiki Specification

**Document status:** Draft for iteration  
**Component:** `mimir-wiki`  
**Related component:** `mimir-confluence`  
**Strategic goal:** Convert exported Confluence source material into curated,
reviewable, human-maintained Markdown knowledge.
**MVP 1 goal:** Enrich and inventory exported Confluence content so it can be
tested in Onyx and used to plan later curated wiki generation.

**Implementation status:** MVP1 is implemented in `src/mimir_wiki/`. Current
operator documentation lives in `README.md`, `docs/cli.md`, `docs/workflow.md`
and `docs/architecture.md`. This specification remains the design intent and is
augmented by the implementation notes below.

---

## 0. Implemented MVP1 Addendum

The current implementation extends the original MVP1 contract with these
operational details:

- CLI commands are `validate-cache`, `enrich`, `extract-visuals`, `probe-ocr`,
  `report` and `export-schema`.
- Generated artifacts include `schema_version: mimir-wiki/v1` and are written
  with atomic writes.
- `.env` is loaded for provider credentials and proxy settings, while YAML stores
  environment variable names rather than secret values.
- LLM providers include `none`, `openai`, `azure-openai`, `azure-ai-foundry` and
  `openai-compatible`.
- Azure AI Foundry supports OpenAI v1 Responses API endpoints such as
  `https://...services.ai.azure.com/openai/v1` without `api-version`.
- LLM task bundles can combine semantic tasks and operational tasks to reduce
  provider calls while preserving task-specific merge semantics.
- LLM responses are cached by source hash, prompt text, prompt version,
  provider, model, task or bundle and enrichment config hash.
- Per-page hierarchy context is computed and stored in enrichment artifacts,
  document index rows, LLM prompt metadata and Onyx Key Facts.
- Hierarchy context includes depth, parent/root title, section path, page role,
  parent context type, sibling count and child count.
- Onyx POC Markdown uses a retrieval-oriented body order: Answer Summary, Key
  Facts, Source Links, Source Content, Additional Source Links, Enrichment
  Details and Source Metadata.
- Onyx Markdown rewrites source images to concise placeholders and prioritizes
  useful source links before full link inventories.
- Taxonomy terms are filtered at page level and aggregate level to reduce
  generic keywords, themes and concepts while preserving useful phrases.
- Reports include high-value hierarchy subtrees, duplicate candidates, LLM usage,
  page failures, attachment followups, missing owners, visual extraction health
  and source quality views.
- Page processing uses bounded worker concurrency and supports graceful
  cancellation with partial run artifacts.
- Visual extraction reads only local cache evidence, ranks all candidate images
  before capping, reuses exact image OCR by `content_sha256`, skips obvious
  low-value visuals, adaptively caps report-like pages, samples repeated visual
  groups and writes omitted image inventories under `runs/{run_id}/`.
- Onyx visual evidence sections deduplicate repeated image hashes and truncate
  long OCR text before export.
- `export-schema` writes JSON Schema files for generated artifact contracts.

These implemented details should be treated as part of the current MVP1 behavior
unless a later spec revision supersedes them.

---

## 1. Executive Summary

`mimir-wiki` is the second major component in the Mimir knowledge system.

`mimir-confluence` crawls and exports Confluence content into a local,
repeatable source cache. The first `mimir-wiki` MVP takes that exported
material, validates it, enriches it, inventories the corpus, and emits
Onyx-ready enriched Markdown so the usefulness of the content can be tested
before building a curated wiki compiler.

The longer-term system should compile structured Markdown knowledge such as
application cards, runbooks, architecture summaries, support models, dependency
pages, known failure modes and documentation quality reports. That compiled
knowledge is intended to be reviewed in Obsidian, version controlled in Git,
and indexed into Onyx only after approval.

The strategic split is:

```text
mimir-confluence = source acquisition and local cache
mimir-wiki MVP 1 = cache validation, enrichment, inventory and Onyx POC output
mimir-wiki later = compilation and curated knowledge generation
Obsidian later   = human review and editing frontend
Onyx MVP 1       = AI search and Q&A over enriched source documents
Onyx later       = AI search and Q&A over approved knowledge
```

`mimir-wiki` should not blindly summarise raw documents. MVP 1 should produce
versioned, repeatable enrichment artifacts using source metadata, quality
signals, deterministic extraction, optional LLM enrichment, validation and clear
provenance. Later phases should use those artifacts to compile evidence-backed
knowledge with templates and human review gates.

---

## 2. System Goal

The purpose of `mimir-wiki` is to turn fragmented, stale, duplicated, and inconsistent documentation into a curated operational knowledge base.

It should help answer questions such as:

- What do we know about this application?
- Which documents are most trustworthy?
- Which sources are stale or contradictory?
- Who owns/supports a system?
- What does this system depend on?
- What runbooks exist or can be drafted?
- What operational gaps exist in the documentation?
- What knowledge should be promoted into approved day-to-day documentation?

The goal is not simply to make Confluence searchable. That is Onyx's job. The goal is to create better knowledge from messy source material.

---

## 3. Non-Goals

`mimir-wiki` should not initially try to:

- replace Confluence;
- replace Onyx;
- provide a full web UI;
- enforce enterprise permissions;
- automatically publish unreviewed AI-generated content;
- create a perfect organisation-wide knowledge graph in version one;
- rewrite original Confluence pages directly;
- trust AI-generated output without provenance and review;
- process every company document on day one.

The first version should focus on one exported cache at a time and produce
versioned enrichment, inventory, reports and Onyx POC Markdown. Curated wiki
pages are explicitly post-MVP.

---

## 4. End-to-End Workflow

MVP 1 workflow:

```text
Confluence
  ↓
mimir-confluence export
  ↓
cache/{dataset_name}/
  dataset.json
  manifest.jsonl
  manifest.summary.json
  pages/{page_id}/metadata.json
  pages/{page_id}/clean.md
  pages/{page_id}/text.txt
  pages/{page_id}/raw_storage.html
  pages/{page_id}/raw_export_view.html
  pages/{page_id}/links.json
  pages/{page_id}/conversion.json
  ↓
mimir-wiki validate-cache
  ↓
mimir-wiki enrich
  ↓
pages/{page_id}/enrichment.json
knowledge/document_index.jsonl
knowledge/quality_scores.jsonl
knowledge/themes.jsonl
knowledge/concepts.jsonl
knowledge/candidate_entities.jsonl
dist/onyx-enriched/{dataset_name}/{space_key}/{page_id}-{slug}.md
reports/*.md
  ↓
Onyx file connector POC over enriched source documents
```

Later curated wiki workflow:

```text
validated and enriched source cache
  ↓
mimir-wiki compile
  ↓
vault/00 Review/*.md
  ↓
Human review in Obsidian
  ↓
status: approved
  ↓
mimir-wiki publish
  ↓
dist/onyx-approved/*.md
  ↓
Onyx indexes approved curated Markdown
```

---

## 5. Input Contract from `mimir-confluence`

`mimir-wiki` expects one local source cache folder generated by
`mimir-confluence`. A cache folder is normally named for the exported dataset,
for example `cache/carel3support`, `cache/aaa-domain-application-support`, or
`cache/customer-identity-and-access-management-entra`.

The first implementation should support one cache folder at a time. It should
not assume the folder is literally named `source_cache`.

### 5.1 Expected Directory Structure

```text
cache/{dataset_name}/
  dataset.json
  manifest.jsonl
  manifest.summary.json
  errors.jsonl              # optional; present when export/download errors occurred
  pages/
    123456789/
      metadata.json
      raw_storage.html
      raw_export_view.html
      clean.md
      text.txt
      links.json
      conversion.json
      attachments/
        architecture.pdf
        diagram.png
```

`attachments/` may exist even when attachment export was disabled or no
attachments were downloaded.

### 5.2 `dataset.json`

The dataset file describes the export as a whole.

Observed fields from current `mimir-confluence` exports:

```json
{
  "source": "confluence",
  "dataset_name": "carel3support",
  "base_url": "https://confluence.example.com",
  "api_root": "/rest/api",
  "crawl_type": "tree",
  "crawl_config": {
    "page_id": "634690804",
    "include_root": true,
    "include_attachments": false,
    "include_comments": false,
    "max_depth": 20,
    "max_pages": 10000,
    "exclude_pages": ["1459361354"]
  },
  "tool_version": "0.1.0",
  "created_at": "2026-06-17T11:41:55Z",
  "updated_at": "2026-06-17T11:41:55Z"
}
```

### 5.3 `manifest.jsonl`

Each line represents one successfully exported source document. Current
`mimir-confluence` manifests are intentionally compact; `mimir-wiki` should use
the manifest for page discovery and load richer metadata from each page folder.

Observed manifest row:

```json
{
  "markdown_path": "pages/123456789/clean.md",
  "page_id": "123456789",
  "path": "pages/123456789",
  "space_key": "IDENTITY",
  "status": "success",
  "title": "ForgeRock Support Runbook",
  "updated_at": "2026-05-01T12:45:00Z",
  "version": 42
}
```

`mimir-wiki` should derive a stable source document ID when one is not present
in the manifest:

```text
confluence:{space_key}:{page_id}
```

### 5.4 `manifest.summary.json` and `errors.jsonl`

`manifest.summary.json` summarises page counts by space and export status.

Observed summary:

```json
{
  "spaces": {
    "IDENTITY": 695
  },
  "status": "complete",
  "statuses": {
    "success": 695
  },
  "total_pages": 695
}
```

`errors.jsonl` is optional and may exist even when the final summary status is
`complete`. `validate-cache` and reports should inspect it and surface download
or conversion errors separately from manifest success counts.

Observed error row:

```json
{
  "operation": "download",
  "page_id": "847359615",
  "error": "Invalid IPv6 URL",
  "timestamp": "2026-06-17T11:52:09Z"
}
```

### 5.5 `metadata.json`

Expected fields:

```json
{
  "ancestors": [
    {"id": "111", "title": "Identity"},
    {"id": "222", "title": "ForgeRock"}
  ],
  "author": {
    "display_name": "Jane Smith",
    "username": "123456"
  },
  "content_hashes": {
    "storage_sha256": "abc123",
    "export_view_sha256": "def456",
    "markdown_sha256": "ghi789",
    "text_sha256": "jkl012"
  },
  "conversion_status": "success",
  "created_at": "2023-04-01T10:00:00Z",
  "download_status": "success",
  "labels": ["runbook", "identity", "production"],
  "page_id": "123456789",
  "space_key": "IDENTITY",
  "space_name": "Identity",
  "status": "current",
  "title": "ForgeRock Support Runbook",
  "url": "https://confluence.example.com/pages/viewpage.action?pageId=123456789",
  "version": 42,
  "updated_at": "2026-05-01T12:45:00Z",
  "retrieved_at": "2026-06-16T09:00:00Z"
}
```

### 5.6 `links.json`

Outbound links are stored separately from `metadata.json`.

Observed shape:

```json
{
  "page_id": "123456789",
  "links": [
    {
      "type": "external_url",
      "href": "https://jira.example.com/browse/ABC-123",
      "text": "ABC-123",
      "crawlable": false,
      "target_page_id": null,
      "target_space_key": null,
      "target_title": null
    }
  ]
}
```

### 5.7 `conversion.json`

Conversion metadata records converter identity, conversion timestamp, hashes and
warnings.

Observed shape:

```json
{
  "converter": "mimir_confluence.converter",
  "converter_version": "0.1.0",
  "converted_at": "2026-06-16T12:00:48Z",
  "markdown_sha256": "abc123",
  "text_sha256": "def456",
  "warnings": []
}
```

### 5.8 `clean.md`

This is the Markdown representation of the source page.

`mimir-wiki` should treat it as source evidence, not as approved knowledge.
Current exporter output includes YAML front matter with source, page ID, space,
title, URL, version, update/retrieval timestamps, labels and ancestor titles.

---

## 6. Output Types

`mimir-wiki` should produce three categories of output:

1. machine-readable enrichment artifacts;
2. Onyx POC enriched Markdown exports;
3. human-readable compiled Markdown knowledge.

### 6.1 Enrichment Artifacts

Per source document:

```text
pages/{page_id}/enrichment.json
```

Global knowledge files:

```text
knowledge/
  candidate_entities.jsonl
  concepts.jsonl
  entities.jsonl
  facts.jsonl
  contradictions.jsonl
  quality_scores.jsonl
  document_index.jsonl
  themes.jsonl
```

### 6.2 Onyx POC Enriched Markdown

MVP 1 should optionally produce Markdown files that can be uploaded with an
Onyx file connector to test whether enriched source content is useful for
search and Q&A. These files are not approved curated knowledge and must be kept
separate from the later approved Onyx export.

```text
dist/
  onyx-enriched/
    {dataset_name}/
      {space_key}/
        {page_id}-{slug}.md
```

Each file should include original source text plus enrichment output in a
consistent, front-matter-rich Markdown format.

### 6.3 Compiled Markdown Knowledge

Generated into an Obsidian-compatible vault:

```text
vault/
  00 Review/
    applications/
    runbooks/
    architecture/
    support_models/
    dependencies/
    teams/
    quality_reports/

  10 Applications/
  20 Runbooks/
  30 Architecture/
  40 Dependencies/
  50 Teams/
  60 Quality Reports/
```

Approved output for Onyx:

```text
dist/
  onyx-approved/
    applications/
    runbooks/
    architecture/
    dependencies/
    support_models/
```

---

## 7. Core Concepts

### 7.1 Source Document

A raw exported document from Confluence or another source.

It is evidence, not necessarily truth.

### 7.2 Enrichment

Structured analysis attached to a source document.

Examples:

- document type;
- summary;
- keywords;
- entities;
- operational signals;
- quality scores;
- warnings;
- candidate facts.

### 7.3 Entity

A named thing that can be linked across documents.

Entity types may include:

- application;
- service;
- platform;
- database;
- queue;
- API;
- server;
- team;
- support group;
- person;
- technology;
- environment;
- region;
- dashboard;
- runbook;
- incident;
- RCA;
- change record.

### 7.4 Fact

An evidence-backed statement extracted from source material.

Example:

```json
{
  "subject": "ForgeRock",
  "predicate": "depends_on",
  "object": "LDAP",
  "source_document_id": "confluence:IDENTITY:123456789",
  "confidence": 0.86,
  "evidence_text": "ForgeRock authenticates users against LDAP."
}
```

### 7.5 Compiled Page

A generated Markdown page produced from multiple source documents, facts, and evidence.

Examples:

- application card;
- runbook;
- architecture summary;
- support model;
- dependency page;
- known failure mode;
- documentation quality report.

### 7.6 Review Status

Compiled pages should have a lifecycle:

```text
generated → needs_review → approved → superseded/deprecated/rejected
```

Only approved pages should be indexed into the main Onyx source.

---

## 8. Enrichment Model

Each source document should receive an `enrichment.json` file.

### 8.1 Example `enrichment.json`

```json
{
  "schema_version": "mimir-wiki/v1",
  "run_id": "20260617T120000Z-enrich-abc123",
  "generated_at": "2026-06-17T12:00:00Z",
  "generator": "mimir-wiki",
  "dataset_name": "identity-support",
  "source_system": "confluence",
  "document_id": "confluence:IDENTITY:123456789",
  "page_id": "123456789",
  "space_key": "IDENTITY",
  "source_updated_at": "2026-05-01T12:45:00Z",
  "source_content_hash": "sha256:abc123",
  "enriched_at": "2026-06-16T10:00:00Z",
  "ONYX_METADATA": {
    "link": "https://confluence.example.com/spaces/IDENTITY/pages/123456789/ForgeRock+Support+Runbook",
    "file_display_name": "ForgeRock Support Runbook",
    "doc_updated_at": "2026-05-01T12:45:00Z"
  },
  "document_type": "runbook",
  "document_type_confidence": 0.91,
  "short_summary": "Explains how to diagnose and recover ForgeRock authentication failures.",
  "detailed_summary": "This runbook covers symptoms, diagnostic checks, recovery actions and escalation paths for ForgeRock authentication issues.",
  "keywords": [
    "ForgeRock",
    "authentication",
    "LDAP",
    "SAML",
    "login failure"
  ],
  "categories": [
    "identity",
    "production support",
    "runbook"
  ],
  "entities": {
    "applications": ["ForgeRock"],
    "technologies": ["LDAP", "SAML"],
    "teams": ["Identity SRE"],
    "support_groups": ["Identity SRE"],
    "dashboards": [],
    "databases": [],
    "queues": [],
    "apis": []
  },
  "operational_signals": {
    "has_owner": true,
    "has_support_group": true,
    "has_escalation_path": true,
    "has_runbook_steps": true,
    "has_diagnostic_steps": true,
    "has_recovery_steps": true,
    "has_validation_steps": true,
    "has_backout_steps": false,
    "has_monitoring_links": true,
    "has_dependencies": true,
    "has_known_errors": true
  },
  "quality": {
    "freshness_score": 85,
    "authority_score": 80,
    "completeness_score": 78,
    "operational_value_score": 90,
    "ownership_clarity_score": 75,
    "staleness_risk_score": 20,
    "contradiction_risk_score": 35,
    "overall_score": 82
  },
  "warnings": [
    "No explicit backout procedure found"
  ],
  "candidate_facts": [
    {
      "subject": "ForgeRock",
      "predicate": "supported_by",
      "object": "Identity SRE",
      "confidence": 0.82,
      "evidence_text": "Production support is handled by Identity SRE."
    }
  ],
  "confidence": 0.84
}
```

### 8.1.1 Onyx POC Enriched Markdown

When `enrich` is run with Onyx POC export enabled, each source document should
also produce a Markdown file under `dist/onyx-enriched/{dataset_name}/`.

The goal is to test enrichment usefulness with an Onyx file connector before
the curated wiki compiler and approval workflow exist. These files should be
plain Markdown with an Onyx metadata line as the first line of the file, not
YAML front matter. They should include the original Confluence-derived source
content and the enrichment signals that help Onyx retrieve and answer over the
document.

Required first line:

```markdown
#ONYX_METADATA={"link":"https://confluence.example.com/spaces/IDENTITY/pages/123456789/Page+Title","file_display_name":"ForgeRock Support Runbook","doc_updated_at":"2026-05-01T12:45:00Z","dataset_name":"identity-support","source_system":"confluence","space_key":"IDENTITY","document_type":"runbook","quality_band":"good","approval_status":"unreviewed"}
```

The Onyx file connector requires `ONYX_METADATA` to be the first line of the
file and formatted as JSON after `ONYX_METADATA=`. The preferred MVP format is
the hash-prefix line above. The HTML comment form is also accepted by Onyx, but
`mimir-wiki` should emit one format consistently.

Onyx also supports a root `.onyx_metadata.json` file for zip uploads. MVP 1
should not rely on that mode because per-file first-line metadata keeps each
Markdown file self-describing when uploaded individually or as part of a zip.
Add `.onyx_metadata.json` generation later only if a zip-packaging workflow
needs it.

Required Onyx metadata JSON keys:

| Field | Source |
|---|---|
| `link` | Confluence source URL from `metadata.json.url` |
| `file_display_name` | Human-readable source title from `metadata.json.title` |
| `doc_updated_at` | Confluence update timestamp from `metadata.json.updated_at` |

Useful Mimir tag keys may also be included as low-cardinality scalar JSON
values:

```text
dataset_name
source_system
space_key
document_type
quality_band
approval_status
historical
currentness
```

Do not put YAML front matter before or around the Onyx metadata line. Avoid
high-cardinality implementation values such as `run_id`, `document_id`,
`page_id`, `source_content_hash`, exact quality scores and prompt versions in
the Onyx metadata line unless they are needed as UI filters. Any richer metadata
should be written into the Markdown body under `## Source Metadata` and
`## Enrichment Summary`.

`primary_owners` and `secondary_owners` may be included only when `mimir-wiki`
has reliable owner values, preferably email addresses or stable user IDs. Do not
populate them from a Confluence page author alone; authorship is not the same as
document ownership.

The Markdown body should use a predictable structure:

```markdown
#ONYX_METADATA={"link":"...","file_display_name":"...","doc_updated_at":"..."}

# ForgeRock Support Runbook

> Source-enriched Confluence document. Not approved curated knowledge.

## Enrichment Summary

...

## Keywords

...

## Themes

...

## Concepts

...

## Candidate Entities

...

## Quality Signals

...

## Source Metadata

Schema version: mimir-wiki/v1
Run ID: 20260617T120000Z-enrich-abc123
Document ID: confluence:IDENTITY:123456789
Page ID: 123456789
Space: IDENTITY
Source updated at: 2026-05-01T12:45:00Z
Source content hash: sha256:abc123

## Source Content

...
```

The source content section should include the cleaned Markdown from `clean.md`
unless the user disables source inclusion. For the POC, including source
content is preferred so Onyx can test retrieval over both the original document
and the enrichment context in one uploaded file.

### 8.2 Document Types

Initial supported document types:

```text
runbook
architecture
design
support_model
incident
rca
knowledge_article
known_error
migration
onboarding
meeting_notes
project_plan
change_record
reference
archive
unknown
```

### 8.3 Operational Signals

Operational signals are boolean or scored indicators that help decide whether a page is useful for support/engineering operations.

Examples:

- has owner;
- has support group;
- has escalation path;
- has runbook steps;
- has recovery steps;
- has validation steps;
- has rollback/backout steps;
- has monitoring links;
- has log locations;
- has known errors;
- has dependencies;
- has architecture description;
- has environment details.

RCA documents should also be checked for:

- has impact summary;
- has incident timeline;
- has root cause;
- has contributing factors;
- has detection gap;
- has monitoring gap;
- has runbook gap;
- has corrective actions;
- has preventive actions;
- has action owners;
- has due dates;
- links to incident or change records.

---

## 9. Quality Scoring

`mimir-wiki` should create a repeatable quality score for each source document.

### 9.1 Suggested Score Dimensions

| Dimension | Description |
|---|---|
| Freshness | Based on last updated date and semantic staleness indicators |
| Authority | Based on source space, labels, page hierarchy and source type |
| Completeness | Whether the expected sections exist for the document type |
| Operational value | Usefulness for support, troubleshooting, architecture or onboarding |
| Ownership clarity | Whether owner/support group/SME information is present |
| Staleness risk | Risk that the content is obsolete or historical only |
| Contradiction risk | Risk that this content disagrees with other sources |
| Overall score | Weighted aggregate |

### 9.2 Example Weighting

For initial MVP:

```text
freshness_score          20%
authority_score          20%
completeness_score       20%
operational_value_score  25%
ownership_clarity_score  10%
contradiction_penalty     5%
```

This should be configurable later.

### 9.3 Deterministic vs AI Scoring

Deterministic scoring should handle:

- date age;
- labels;
- title/path patterns;
- presence of expected headings;
- broken/dead-looking links;
- known archive/deprecated markers;
- content length;
- table/code/link counts.

AI scoring should help with:

- whether the document is operationally useful;
- whether the document appears stale despite a recent update;
- whether runbook steps are actionable;
- whether architecture descriptions are complete;
- whether ownership is clear;
- whether an RCA contains reusable failure-mode lessons;
- whether warnings should be raised.

### 9.4 RCA Scoring

RCA documents should use a specialised scoring profile. Old RCAs should not be
treated as stale in the same way as old runbooks or architecture pages. An RCA
from several years ago may still be highly valuable as historical evidence for
recurring failure modes, monitoring gaps, dependency risks, and operational
lessons.

Suggested RCA score dimensions:

| Dimension | Description |
|---|---|
| Impact clarity | Whether the customer, business or operational impact is described |
| Timeline completeness | Whether detection, response, mitigation and resolution times are present |
| Root cause clarity | Whether the root cause is specific and evidence-backed |
| Contributing factors | Whether process, architecture, monitoring or dependency factors are captured |
| Corrective actions | Whether remediation and prevention actions are explicit |
| Action ownership | Whether actions have owners and due dates |
| Reusable lesson value | Whether the RCA teaches something useful for runbooks or failure-mode pages |
| Evidence links | Whether incident, change, dashboard or related-document links are present |

RCA freshness should primarily affect `currentness`, not usefulness. Old RCAs
should normally be marked as historical evidence rather than excluded.

### 9.5 Evidence Authority by Claim Type

Document type should influence evidence weight, but should not decide truth by
itself. `mimir-wiki` should score evidence by claim type, source authority,
freshness/currentness, approval status and corroboration.

Default document-type authority should start with:

```text
approved_runbook   95
runbook            90
knowledge_article  85
rca                85
architecture       85
known_error        80
support_model      80
incident           75
change_record      70
reference          50
project_plan       40
meeting_notes      30
archive            20
unknown            10
```

These are baseline authority scores, not final quality scores. They should be
modified by source space/path authority, labels, freshness, approval status,
explicit deprecation/archive markers and corroborating or conflicting evidence.

Different document types are authoritative for different claims:

| Claim type | Stronger evidence sources | Notes |
|---|---|---|
| Current recovery procedure | approved runbook, runbook, knowledge article | RCAs can suggest improvements, but should not define current procedure alone |
| Current escalation or support model | support model, approved runbook, knowledge article | Incident notes may show who helped, not necessarily who owns support |
| Historical impact and timeline | incident, RCA | Runbooks are usually weak evidence for what happened in a specific incident |
| Root cause and contributing factors | RCA, finalized incident, known error | Draft incidents should be treated as provisional |
| Known failure pattern | RCA cluster, known error, incident cluster, runbook | Prefer corroborated recurring patterns over a single weak source |
| Current architecture or dependency | architecture, application card, runbook, support model | Incidents and RCAs can reveal operational reality or hidden dependencies |
| Documentation gap | RCA, incident, quality report, runbook review | Strong when multiple RCAs mention the same missing monitoring/runbook/escalation path |
| Planned future state | change record, design, project plan | Should not override current-state sources until implemented |

For example, an RCA is strong evidence that a specific outage was caused by an
LDAP timeout, but weak evidence that a restart procedure is still the approved
current recovery step unless a current runbook or knowledge article supports
that procedure.

---

## 10. Fact Extraction

`mimir-wiki` should extract structured candidate facts from source documents.

### 10.1 Initial Predicate Types

```text
owned_by
supported_by
escalates_to
depends_on
uses_database
uses_queue
uses_api
runs_in_environment
has_dashboard
has_log_source
has_alert
has_runbook
has_known_failure_mode
has_diagnostic_step
has_recovery_step
has_validation_step
has_backout_step
replaced_by
deprecated_by
related_to_incident
related_to_change
had_incident
had_impact
had_root_cause
had_contributing_factor
had_detection_gap
had_monitoring_gap
had_runbook_gap
had_recovery_action
had_preventive_action
had_followup_action
affected_dependency
affected_customer_group
recurred_as
```

### 10.2 Fact Requirements

Each fact should include:

- subject;
- predicate;
- object;
- source document ID;
- evidence text or section reference;
- confidence;
- extraction method;
- timestamp.

Example:

```json
{
  "fact_id": "fact:001",
  "subject": "ForgeRock",
  "predicate": "depends_on",
  "object": "LDAP",
  "source_document_id": "confluence:IDENTITY:123456789",
  "source_section": "Architecture > Authentication",
  "evidence_text": "ForgeRock validates users against LDAP.",
  "confidence": 0.87,
  "extraction_method": "llm_structured",
  "extracted_at": "2026-06-16T10:15:00Z"
}
```

---

## 11. Contradiction Detection

`mimir-wiki` should detect conflicts between facts.

Examples:

- two different support groups for the same application;
- two different owners;
- dependency listed in one newer source but absent/removed in another;
- one page says a system is active, another says retired;
- two runbooks describe different recovery procedures for the same scenario;
- repeated RCAs point to the same failure mode but list different root causes.

### 11.1 MVP Contradiction Types

Start with:

```text
ownership_conflict
support_group_conflict
dependency_conflict
status_conflict
runbook_step_conflict
architecture_conflict
rca_root_cause_conflict
```

### 11.2 Contradiction Output

```json
{
  "contradiction_id": "contradiction:001",
  "entity": "ForgeRock",
  "type": "support_group_conflict",
  "facts": ["fact:001", "fact:002"],
  "summary": "Two sources list different support groups for ForgeRock.",
  "recommended_resolution": "Prefer the newer runbook, but SME review is required.",
  "severity": "medium"
}
```

Contradictions should be surfaced in compiled pages instead of hidden.

---

## 12. Compiled Page Types

### 12.1 Application Card

Purpose: provide a concise, structured overview of an application or service.

Sections:

```text
Summary
Purpose
Ownership
Support Model
Environments
Architecture Summary
Dependencies
Monitoring and Logging
Known Failure Modes
Runbooks
Source Evidence
Conflicting Evidence
Open Questions
Review Notes
```

### 12.2 Runbook

Purpose: provide scenario-specific operational guidance.

Sections:

```text
Purpose
Scope
Symptoms
Impact
Prerequisites
Initial Triage
Diagnostic Checks
Recovery Steps
Validation Steps
Escalation
Known Risks
Backout / Rollback
Related Systems
Source Evidence
Open Questions
```

### 12.3 Architecture Summary

Purpose: summarise the best available architecture understanding.

Sections:

```text
Current Understanding
Components
Data Flows
Authentication / Authorisation
Dependencies
Environments
Resilience and Failover
Known Limitations
Conflicting Evidence
Source Evidence
Open Questions
```

### 12.4 Support Model

Purpose: describe how a system is supported.

Sections:

```text
Support Ownership
Assignment Groups
SMEs
Escalation Path
Operational Hours
Monitoring
Incident Handling
Change Considerations
Known Support Gaps
Source Evidence
Open Questions
```

### 12.5 Dependency Page

Purpose: explain a shared dependency and which systems rely on it.

Sections:

```text
Summary
Consumers
Operational Importance
Known Failure Modes
Related Runbooks
Monitoring
Source Evidence
Open Questions
```

### 12.6 Documentation Quality Report

Purpose: help improve documentation quality.

Sections:

```text
Summary
Best Available Sources
Stale Sources
Conflicting Sources
Missing Information
Duplicate / Near-Duplicate Documents
High-Value Cleanup Candidates
Recommended Actions
```

### 12.7 Known Failure Mode

Purpose: turn RCA evidence and known-error material into reusable operational
knowledge for recurring or high-impact failure patterns.

Sections:

```text
Summary
Affected Applications and Dependencies
Symptoms
Observed Impacts
Common Root Causes
Contributing Factors
Detection and Monitoring Gaps
Diagnostic Checks
Recovery Actions
Preventive Actions
Related Runbooks
Related RCAs
Source Evidence
Open Questions
```

Known failure mode pages should usually be generated from clusters of RCAs,
known-error pages, incidents and runbooks. They should not copy full RCA
narratives; they should extract reusable lessons with source evidence.

---

## 13. Compiled Markdown Front Matter

Every compiled page should include YAML front matter.

Example:

```yaml
---
title: ForgeRock Authentication Failure Runbook
mimir_id: runbook:forgerock-authentication-failure
page_type: runbook
status: needs_review
confidence: medium
quality_score: 78
source_count: 6
source_documents:
  - confluence:IDENTITY:123456789
  - confluence:ARCH:987654321
created_by: mimir-wiki
compiled_at: 2026-06-16T10:30:00Z
reviewed_by:
reviewed_at:
tags:
  - mimir
  - runbook
  - identity
  - forgerock
---
```

### 13.1 Required Front Matter Fields

```text
title
mimir_id
page_type
status
confidence
source_documents
created_by
compiled_at
tags
```

### 13.2 Optional Front Matter Fields

```text
reviewed_by
reviewed_at
supersedes
superseded_by
quality_score
entity
scenario
aliases
```

---

## 14. AI Usage

`mimir-wiki` should use AI, but in controlled stages.

### 14.1 AI-Assisted Tasks

AI should help with:

- document classification;
- short and detailed summaries;
- entity extraction;
- operational signal detection;
- candidate fact extraction;
- semantic staleness detection;
- open question generation;
- contradiction explanation;
- page drafting;
- architecture/runbook summarisation.

### 14.2 Non-AI Tasks

AI should not be used for:

- page IDs;
- source URLs;
- timestamps;
- labels;
- content hashes;
- path handling;
- approval status;
- publish decisions;
- deterministic freshness date calculations;
- manifest generation.

### 14.3 AI Output Requirements

AI outputs should be:

- schema constrained;
- JSON validated with Pydantic;
- grounded in supplied text;
- confidence scored;
- reproducible where possible;
- cached by source content hash, prompt version, provider and model/deployment
  identifier;
- retried with bounded exponential backoff for transient provider failures;
- rate limited and concurrency limited by configuration.

### 14.4 LLM Provider Abstraction

LLM usage must be behind a provider interface so MVP enrichment can run with:

```text
none
openai
azure-openai
azure-ai-foundry
openai-compatible
```

`--provider none` is required for deterministic local runs and tests. OpenAI,
Azure OpenAI and Azure AI Foundry models may be used for semantic enrichment,
including summaries, themes, concepts, candidate entities, operational-signal
interpretation and quality warnings.

Provider configuration should be read from `mimir-wiki.yaml`, environment
variables and `.env`, but secrets must not be committed. API keys, Azure
endpoints, deployment names and project-specific credentials should come from
environment variables or secret stores.

### 14.4.1 LLM Rate Limits, Retries and Backoff

All live LLM calls must go through a shared rate-limited client wrapper. The
wrapper should apply the same policy to OpenAI, Azure OpenAI, Azure AI Foundry
and OpenAI-compatible providers unless a provider requires stricter limits.

Required behavior:

- limit concurrent in-flight requests with a configurable semaphore;
- optionally limit requests per minute and tokens per minute when configured;
- respect provider `Retry-After` headers when present;
- retry transient failures with exponential backoff and jitter;
- apply a per-request timeout;
- apply a maximum retry count and maximum backoff delay;
- record final failures in enrichment output and reports instead of crashing the
  whole run unless fail-fast mode is enabled;
- never retry deterministic validation failures such as schema validation
  errors, malformed prompts, missing credentials, authorization failures or
  context-length errors that require chunking changes.

Default retryable failures:

```text
timeout
connection reset
HTTP 408
HTTP 409
HTTP 429
HTTP 500
HTTP 502
HTTP 503
HTTP 504
provider-specific transient rate-limit or overload errors
```

Default non-retryable failures:

```text
HTTP 400 invalid request
HTTP 401 unauthorized
HTTP 403 forbidden
HTTP 404 deployment/model not found
context length exceeded
schema validation failed after a syntactically valid response
unsupported provider configuration
```

Backoff should use exponential growth with jitter, for example:

```text
sleep = min(max_delay_seconds, initial_delay_seconds * 2 ** attempt)
sleep = sleep * random_jitter_between_0.5_and_1.5
```

If a provider returns `Retry-After`, that delay should take precedence unless it
exceeds a configured maximum. Retry state should be logged with provider, model
or deployment, attempt count, status/error type and document ID.

### 14.4.2 Task-Specific Model Routing

`mimir-wiki` should support different models or deployments for different
enrichment tasks. A single global default model is acceptable as a fallback, but
the implementation should route by task when configuration is present.

Initial task routes:

```text
classification
summary
keywords
themes
concepts
candidate_entities
operational_signals
quality_warnings
onyx_markdown_context
```

Routing should allow cheaper/faster models for classification and keyword-like
tasks, and stronger models for summaries, concepts, operational interpretation
and quality warnings. The selected provider, model/deployment and prompt
version must be included in cache keys so changing a task model invalidates only
the affected cached enrichment.

When `--provider none` is used, all LLM task routes are skipped and only
deterministic enrichers run.

### 14.5 Prompting Principle

Bad pattern:

```text
Read these docs and write a wiki.
```

Good pattern:

```text
Classify this document using this schema.
Extract facts using these predicate types.
Compile this page using only these facts and excerpts.
Include uncertainty and source evidence.
```

---

## 15. CLI Specification

The CLI should be implemented with `typer` and `rich`. It should feel
consistent, predictable and pleasant for interactive use, while still being
safe for automation.

CLI design principles:

- use the same option names and semantics across commands;
- default to human-readable output with colour, progress bars and summary
  tables when attached to a TTY;
- degrade cleanly to plain output when running in CI, non-TTY shells, or when
  `--no-color` is used;
- offer scriptable output with `--json` without progress bars or decorative
  formatting;
- make common flows discoverable through concise `--help` text and examples;
- print every important output path at the end of a successful or partially
  successful run;
- make repeated runs idempotent by default, especially with `--changed-only`;
- explain failures with the page ID, title, path and suggested next action when
  those details are available.

Common options:

```text
--config PATH
--profile NAME
--cache PATH
--out PATH
--provider none|openai|azure-openai|azure-ai-foundry|openai-compatible
--enable-llm / --disable-llm
--llm-task TASK
--emit-onyx-markdown / --no-emit-onyx-markdown
--include-source-content / --no-include-source-content
--redaction redact|fail|off
--limit INT
--changed-only
--force
--dry-run
--json
--no-color
--quiet
--verbose
--log-file PATH
```

Colour rules:

- use colour only when stdout is interactive, unless explicitly forced by a
  future option;
- use green for successful completion, yellow for warnings and partial success,
  red for errors, cyan or blue for paths and command names, and dim text for
  low-priority detail;
- never rely on colour alone; labels and symbols must still communicate status
  in plain output.

Progress rules:

- show progress bars for cache validation, page enrichment, LLM calls, Onyx
  Markdown writing and report generation when running interactively;
- keep progress bars bounded and honest: page counts, skipped counts, retrying
  calls and failed items should be visible;
- suppress progress bars under `--json`, `--quiet`, non-TTY output and CI;
- for long LLM runs, include current task type and retry/backoff state without
  printing secrets or full prompts.

Every command should finish with a concise summary table unless `--json` or
`--quiet` is set. The summary should include the cache path, dataset, total
pages considered, processed pages, skipped pages, failed pages, changed versus
unchanged pages, output artifacts written, elapsed time, provider/model usage,
retry count and warning count when relevant.

Exit codes:

```text
0  success
1  validation error, invalid user input, missing files or missing credentials
2  provider/runtime failure after retries
3  partial success with recorded page-level failures
```

`--dry-run` must perform validation and planning without writing files. It
should print the planned commands, page counts and output paths that would be
used.

`--verbose` should include stack traces and lower-level diagnostics. Normal
output should stay focused on what happened, what was written, and what the
user can do next.

### 15.1 `mimir-wiki validate-cache`

Validate that a `mimir-confluence` cache is usable.

```bash
mimir-wiki validate-cache --cache ./cache/customer-identity-and-access-management-entra
```

Checks:

- dataset metadata exists;
- manifest exists;
- manifest summary exists;
- page folders exist;
- metadata files exist;
- Markdown files exist;
- text files exist;
- links files exist;
- conversion files exist;
- content hashes are valid;
- required fields are present;
- optional `errors.jsonl` entries are surfaced in the validation report.

### 15.2 `mimir-wiki enrich`

Analyse source documents and create `enrichment.json` files.

```bash
mimir-wiki enrich \
  --cache ./cache/{dataset_name} \
  --changed-only \
  --workers 8 \
  --provider azure-openai \
  --emit-onyx-markdown \
  --onyx-out ./dist/onyx-enriched
```

Options:

```text
--cache PATH
--changed-only
--force
--workers INT
--provider none|openai|azure-openai|azure-ai-foundry|openai-compatible
--enable-llm / --disable-llm
--llm-task TASK
--limit INT
--document-type-filter TYPE
--space-filter SPACE
--emit-onyx-markdown / --no-emit-onyx-markdown
--onyx-out PATH
--include-source-content / --no-include-source-content
--redaction redact|fail|off
--dry-run
```

Outputs:

```text
pages/{page_id}/enrichment.json
knowledge/document_index.jsonl
knowledge/quality_scores.jsonl
dist/onyx-enriched/{dataset_name}/{space_key}/{page_id}-{slug}.md
```

### 15.3 `mimir-wiki extract-facts`

Extract candidate facts from enriched documents.

```bash
mimir-wiki extract-facts --cache ./cache/{dataset_name} --changed-only
```

Outputs:

```text
knowledge/facts.jsonl
```

### 15.4 `mimir-wiki build-entities`

Build the entity registry.

```bash
mimir-wiki build-entities --cache ./cache/{dataset_name}
```

Outputs:

```text
knowledge/entities.jsonl
knowledge/entity_aliases.jsonl
```

### 15.5 `mimir-wiki detect-contradictions`

Detect contradictions between facts.

```bash
mimir-wiki detect-contradictions --cache ./cache/{dataset_name}
```

Outputs:

```text
knowledge/contradictions.jsonl
```

### 15.6 `mimir-wiki compile application`

Compile an application card.

```bash
mimir-wiki compile application \
  --entity "ForgeRock" \
  --cache ./cache/{dataset_name} \
  --vault ./vault
```

Output:

```text
vault/00 Review/applications/forgerock.md
```

### 15.7 `mimir-wiki compile runbook`

Compile a scenario-specific runbook.

```bash
mimir-wiki compile runbook \
  --entity "ForgeRock" \
  --scenario "authentication failure" \
  --cache ./cache/{dataset_name} \
  --vault ./vault
```

Output:

```text
vault/00 Review/runbooks/forgerock-authentication-failure.md
```

### 15.8 `mimir-wiki compile quality-report`

Compile a documentation quality report.

```bash
mimir-wiki compile quality-report \
  --entity "ForgeRock" \
  --cache ./cache/{dataset_name} \
  --vault ./vault
```

### 15.9 `mimir-wiki compile known-failure-mode`

Compile a reusable failure-mode page from RCA, incident, known-error and
runbook evidence.

```bash
mimir-wiki compile known-failure-mode \
  --entity "ForgeRock" \
  --failure-mode "authentication failure" \
  --cache ./cache/{dataset_name} \
  --vault ./vault
```

Output:

```text
vault/00 Review/known_failure_modes/forgerock-authentication-failure.md
```

### 15.10 `mimir-wiki rca-index`

Build an RCA-oriented index grouped by application, dependency, failure mode,
impact type and reusable lesson.

```bash
mimir-wiki rca-index \
  --cache ./cache/{dataset_name} \
  --out ./knowledge/rca_index.jsonl
```

Outputs:

```text
knowledge/rca_index.jsonl
reports/rca_clusters.md
```

### 15.11 Future: `mimir-wiki compile all`

Compile pages for all discovered high-value entities.

This should be deferred until manual single-entity compilation works well and
source-to-page dependency tracking exists. Compilation is post-MVP; when it
begins, start with `compile application`, followed by targeted runbook and
quality-report compilation.

```bash
mimir-wiki compile all \
  --cache ./cache/{dataset_name} \
  --vault ./vault \
  --changed-only
```

Options:

```text
--entity-type application|dependency|team|all
--min-quality-score INT
--max-pages INT
--changed-only
--dry-run
```

### 15.12 `mimir-wiki publish`

Copy approved pages from the Obsidian vault to an Onyx indexing folder.

```bash
mimir-wiki publish \
  --vault ./vault \
  --out ./dist/onyx-approved \
  --status approved
```

Rules:

- include only `status: approved` by default;
- preserve folder structure;
- optionally add Onyx-friendly metadata lines;
- validate links and front matter;
- fail or warn on missing source evidence.

### 15.13 `mimir-wiki report`

Generate reports about the current knowledge state.

```bash
mimir-wiki report --cache ./cache/{dataset_name} --out ./reports
```

Report types:

```text
enrichment_summary.md
stale_docs.md
missing_owners.md
low_quality_high_value.md
contradictions.md
candidate_runbooks.md
```

---

## 16. Source Selection and Ranking

When compiling a page, `mimir-wiki` should select and rank relevant sources.

### 16.1 Ranking Inputs

- entity match;
- alias match;
- title/path relevance;
- label relevance;
- document type;
- quality score;
- freshness;
- authority;
- operational value;
- contradiction risk;
- source recency;
- explicit user-supplied source URLs or IDs.

### 16.2 Suggested Ranking Formula

Initial version:

```text
relevance_score          35%
quality_score            25%
freshness_score          15%
authority_score          15%
operational_value_score  10%
```

Apply penalties for:

- archived/deprecated content;
- low-confidence enrichment;
- contradiction risk;
- meeting notes/project plans used for current-state claims.

---

## 17. Review Workflow in Obsidian

`mimir-wiki` should produce Markdown that works cleanly in Obsidian.

### 17.1 Review States

```text
generated
needs_review
approved
rejected
superseded
deprecated
```

### 17.2 Human Review Rules

A human reviewer should:

- check the generated page against source evidence;
- correct bad assumptions;
- resolve or preserve conflicts;
- add missing context if known;
- update front matter to `status: approved` only when safe;
- optionally add review notes.

### 17.3 Approval Example

Before:

```yaml
status: needs_review
reviewed_by:
reviewed_at:
```

After:

```yaml
status: approved
reviewed_by: Weavus
reviewed_at: 2026-06-16
```

---

## 18. Onyx Integration

`mimir-wiki` should not require Onyx, but should support two different Onyx
flows:

1. MVP POC indexing of enriched source documents with an Onyx file connector;
2. later approved indexing of curated Markdown after human review.

These flows must stay separate so source-enriched POC content is not mistaken
for approved curated knowledge.

### 18.1 MVP Onyx POC Source

For MVP 1, `mimir-wiki enrich --emit-onyx-markdown` writes enriched Markdown
files to:

```text
dist/onyx-enriched/{dataset_name}/
```

Recommended Onyx source name:

```text
Mimir - Enriched Confluence POC
```

Each file must include an Onyx metadata line as the first line. The MVP should
emit the hash-prefix form:

```markdown
#ONYX_METADATA={"link":"<confluence url>","file_display_name":"<confluence title>","doc_updated_at":"<metadata.json updated_at>"}
```

The metadata after `ONYX_METADATA=` must be valid JSON. Do not use YAML front
matter for Onyx POC files. The optional HTML comment form
`<!-- ONYX_METADATA={...} -->` is supported by Onyx, but `mimir-wiki` should
emit only one stable format.

Do not generate `.onyx_metadata.json` in MVP 1. Prefer self-describing files
with first-line metadata so each Markdown file remains valid whether uploaded
alone or inside a zip.

The POC source should be treated as unreviewed source evidence. It is useful
for evaluating search, retrieval and Q&A quality, but it should not be mixed
with approved curated knowledge.

### 18.2 Recommended Approved Onyx Sources

```text
Mimir - Approved Knowledge
Mimir - Approved Runbooks
Mimir - Quality Reports
Raw - Confluence Evidence - Dev Only
```

### 18.3 Approved Onyx Publishing Rules

- main Onyx source should receive only `status: approved` pages;
- draft/needs-review pages should be excluded or placed in a separate low-trust source;
- raw Confluence should not be mixed equally with curated knowledge;
- compiled pages should preserve source evidence sections;
- Onyx should index `dist/onyx-approved`, not the whole Obsidian vault.

---

## 19. Storage Strategy

### 19.1 File-Based MVP

Use files first:

```text
cache/{dataset_name}/
knowledge/
vault/
dist/
reports/
```

This is easy to inspect, Git-friendly, and compatible with Obsidian.

### 19.2 Future Postgres Store

Later, move structured records to Postgres:

```text
documents
document_versions
document_enrichments
entities
facts
fact_sources
contradictions
compiled_pages
compiled_page_sources
reviews
```

The file format should be designed so it can be imported into Postgres later.

---

## 20. Recommended Python Stack

Core:

```text
typer
rich
tqdm
pydantic
orjson
python-frontmatter
jinja2
markdown-it-py
rapidfuzz
httpx
```

Optional enrichment helpers:

```text
gliner
keybert
yake
spacy
```

Optional document helpers:

```text
docling
pymupdf
python-docx
beautifulsoup4
markdownify
```

LLM providers:

```text
OpenAI
Azure OpenAI
Azure AI Foundry
OpenAI-compatible API
Ollama
LiteLLM-compatible endpoint
```

### 20.1 Recommended Module Boundaries

The implementation should keep the enrichment pipeline modular and testable.

Recommended package layout:

```text
src/mimir_wiki/
  cli.py
  config.py
  schemas.py
  pipeline.py
  cache_reader.py
  enrichers/
    deterministic.py
    llm.py
    prompts/
  llm/
    base.py
    openai_provider.py
    azure_openai_provider.py
    azure_foundry_provider.py
  scoring.py
  writers/
    enrichment_json.py
    jsonl_indexes.py
    onyx_markdown.py
  reports.py
```

Design rules:

- use Pydantic models for external file contracts and LLM structured outputs;
- keep cache reading, enrichment, scoring, writing and reporting separate;
- keep provider-specific LLM code behind an interface;
- keep prompt templates versioned in files;
- include source content hash, task, prompt version, provider and
  model/deployment in LLM cache keys;
- do not run live LLM calls in normal unit tests;
- use structured logs and progress output for long cache runs;
- bound LLM concurrency, retries and rate limits through configuration;
- keep source cache files read-only except for explicitly configured derived
  enrichment outputs.

### 20.2 Concurrency Model

The enrichment pipeline should use bounded concurrency, but it should not create
unbounded threads. The default design should be:

- synchronous or streaming cache discovery;
- bounded worker queues for page-level enrichment;
- async I/O for live LLM calls where the provider SDK supports it;
- a shared LLM semaphore and rate limiter across all task routes;
- one writer path per output artifact type, or explicit file locks where
  concurrent writes are unavoidable;
- optional thread pools only for blocking file/HTML parsing work if profiling
  shows it helps.

Using multiple workers is useful for a 5,000+ page cache, especially for
network-bound LLM calls and file parsing. It is not a reason to make every
stage multi-threaded. MVP 1 should start with simple bounded workers controlled
by config (`page_workers`, `llm_workers`, `writer_workers`) and keep output
writes deterministic.

---

## 21. MVP Scope

The first useful MVP should be an enrichment and inventory pipeline, not a wiki
compiler. It should support one exported Confluence space or page tree cache
folder at a time and make the exported corpus understandable, searchable and
rankable without generating curated wiki pages yet.

The current local examples include broad mixed application/support exports and
an RCA-specific export:

```text
cache/aaa-domain-application-support/
cache/carel3support/
cache/customer-identity-and-access-management-entra/
cache/carel3support-rca/
```

The MVP path should work on the broad mixed exports first: application/support
pages, runbooks, technical designs, troubleshooting guides, business documents,
migration pages, archived/deprecated pages and incident lists. If the selected
source cache contains RCA documents, they should be classified and enriched as
historical evidence, but RCA clustering, known-failure-mode compilation and
runbook generation should remain post-MVP.

The MVP should answer:

- What documents are in this export?
- What topics, themes and concepts appear?
- Which documents look like runbooks, architecture, support models, business
  docs, project pages, incidents, RCAs or archives?
- Which pages look useful, stale, duplicated, thin, deprecated or operationally
  valuable?
- Which pages mention likely applications, dependencies, teams, environments,
  dashboards, incidents, changes or external systems?
- Which documents should be reviewed first before attempting compiled wiki
  generation?

### 21.1 MVP Commands

```bash
mimir-wiki validate-cache --cache ./cache/customer-identity-and-access-management-entra
mimir-wiki enrich --cache ./cache/customer-identity-and-access-management-entra --provider none --changed-only --emit-onyx-markdown --onyx-out ./dist/onyx-enriched
mimir-wiki report --cache ./cache/customer-identity-and-access-management-entra --out ./reports
```

### 21.2 MVP Outputs

- `enrichment.json` per document;
- `knowledge/document_index.jsonl`;
- `knowledge/quality_scores.jsonl`;
- `knowledge/themes.jsonl`;
- `knowledge/concepts.jsonl`;
- `knowledge/candidate_entities.jsonl`;
- `dist/onyx-enriched/{dataset_name}/{space_key}/{page_id}-{slug}.md`;
- `reports/enrichment_summary.md`;
- `reports/document_types.md`;
- `reports/stale_or_deprecated.md`;
- `reports/high_value_sources.md`;
- `reports/cache_validation.md`.

### 21.3 MVP Success Criteria

The MVP is successful if it can:

- validate a real exported Confluence page tree and surface export errors;
- process thousands of pages incrementally using content hashes;
- normalize source metadata into a document index;
- classify broad document types well enough for review and filtering;
- extract deterministic keywords, headings, links, source paths and metadata
  signals;
- optionally enrich summaries, themes, concepts and candidate entities with an
  LLM when a provider is configured;
- produce stable JSON/JSONL artifacts that can be diffed in Git;
- produce Onyx file-connector-ready enriched Markdown with required
  `ONYX_METADATA` fields;
- produce reports that help decide which documents, applications or topics
  should be used for later wiki compilation.

### 21.4 MVP 1 Implementation Contract

Recommendation:

```text
Build MVP 1 around a strict, versioned artifact contract before adding more
semantic ambition. The first implementation should make validate-cache, enrich
and report boringly reliable against real exports, with deterministic
--provider none output, run manifests, page-level failure records, stable
JSON/JSONL schemas and Onyx POC Markdown. Add LLM enrichment only behind the
same schemas and retry/rate-limit controls.
```

#### 21.4.1 Common Artifact Fields

Every generated JSON artifact, JSONL row and generated Markdown metadata
section must include enough metadata to explain which run created it and whether
it is still valid. For Onyx POC Markdown, the Onyx-specific metadata must be in
the first-line `#ONYX_METADATA={...}` JSON object.

Required fields for all generated artifacts:

```text
schema_version
run_id
generated_at
generator
dataset_name
```

Required additional fields for page-scoped artifacts:

```text
source_system
document_id
page_id
space_key
source_updated_at
source_content_hash
```

`schema_version` should start at `mimir-wiki/v1`. Schema changes that break
compatibility must increment the version. `run_id` should be stable within a
single CLI execution and should be written to every artifact created by that
execution.

#### 21.4.2 Configuration Precedence

Configuration must resolve in this order, with later sources overriding earlier
sources:

```text
built-in defaults
  -> config file
  -> profiles.<name> from config file
  -> .env
  -> environment variables
  -> CLI flags
```

The resolved non-secret configuration should be written into the run manifest.
Secrets must be redacted.

#### 21.4.3 Feature Flags and Runtime Overrides

Feature flags should be controlled in both configuration and CLI parameters:

- config file and profile values define repeatable defaults;
- CLI flags override those defaults for one run;
- environment variables should be used for secrets and deployment-specific
  values, not as the main feature-toggle interface;
- the resolved feature set must be written into `runs/{run_id}/summary.json`.

The MVP should expose coarse CLI controls for common experimentation:

```text
--provider none|openai|azure-openai|azure-ai-foundry|openai-compatible
--enable-llm / --disable-llm
--llm-task classification
--llm-task summary
--llm-task keywords
--llm-task themes
--llm-task concepts
--llm-task candidate_entities
--llm-task operational_signals
--llm-task quality_warnings
--emit-onyx-markdown / --no-emit-onyx-markdown
--include-source-content / --no-include-source-content
--redaction redact|fail|off
```

If `--provider none` is selected, live LLM features must be disabled regardless
of task flags. Deterministic enrichment features should remain enabled unless
explicitly disabled in config for testing. Fine-grained defaults should live in
`mimir-wiki.yaml`, not as a large set of command-line switches.

#### 21.4.4 Run Manifest and Failure Records

Every command that reads a cache should create a run manifest unless `--dry-run`
is used. `--dry-run` should print the planned manifest path but should not write
it.

Run artifacts:

```text
runs/{run_id}/summary.json
runs/{run_id}/page_failures.jsonl
runs/{run_id}/warnings.jsonl
runs/{run_id}/llm_usage.jsonl
```

`summary.json` required fields:

```json
{
  "schema_version": "mimir-wiki/v1",
  "run_id": "20260617T120000Z-enrich-abc123",
  "generated_at": "2026-06-17T12:08:31Z",
  "generator": "mimir-wiki",
  "command": "enrich",
  "started_at": "2026-06-17T12:00:00Z",
  "finished_at": "2026-06-17T12:08:31Z",
  "elapsed_seconds": 511.2,
  "status": "partial_success",
  "exit_code": 3,
  "dataset_name": "customer-identity-and-access-management-entra",
  "cache_path": "cache/customer-identity-and-access-management-entra",
  "config_profile": "default",
  "resolved_config": {},
  "counts": {
    "pages_total": 695,
    "pages_considered": 695,
    "pages_processed": 680,
    "pages_skipped_unchanged": 10,
    "pages_failed": 5,
    "warnings": 12,
    "files_written": 1365
  },
  "outputs": {
    "knowledge": "knowledge",
    "onyx_enriched": "dist/onyx-enriched",
    "reports": "reports"
  }
}
```

`page_failures.jsonl` row shape:

```json
{
  "schema_version": "mimir-wiki/v1",
  "run_id": "20260617T120000Z-enrich-abc123",
  "dataset_name": "customer-identity-and-access-management-entra",
  "source_system": "confluence",
  "document_id": "confluence:CIAME:123456789",
  "page_id": "123456789",
  "space_key": "CIAME",
  "title": "ForgeRock Support Runbook",
  "source_updated_at": "2026-05-01T12:45:00Z",
  "source_content_hash": "sha256:abc123",
  "stage": "llm.summary",
  "error_type": "rate_limit_exhausted",
  "message": "Provider returned HTTP 429 after 3 retries.",
  "retryable": true,
  "attempts": 4,
  "suggested_action": "Rerun with lower llm_workers or requests_per_minute.",
  "generated_at": "2026-06-17T12:04:12Z",
  "generator": "mimir-wiki"
}
```

Page-level failures should not corrupt global JSONL files. Failed pages should
be included in reports and should cause exit code `3` unless fail-fast mode
causes an earlier command failure.

#### 21.4.5 Required JSONL Schemas

`knowledge/document_index.jsonl` row shape:

```json
{
  "schema_version": "mimir-wiki/v1",
  "run_id": "20260617T120000Z-enrich-abc123",
  "dataset_name": "customer-identity-and-access-management-entra",
  "source_system": "confluence",
  "document_id": "confluence:CIAME:123456789",
  "page_id": "123456789",
  "space_key": "CIAME",
  "title": "ForgeRock Support Runbook",
  "url": "https://confluence.example.com/pages/viewpage.action?pageId=123456789",
  "source_updated_at": "2026-05-01T12:45:00Z",
  "retrieved_at": "2026-06-16T09:00:00Z",
  "version": 42,
  "source_content_hash": "sha256:abc123",
  "document_type": "runbook",
  "document_type_confidence": 0.91,
  "status_flags": ["current"],
  "labels": ["runbook", "identity"],
  "ancestor_titles": ["Identity", "ForgeRock"],
  "outbound_link_count": 7,
  "attachment_count": 0,
  "word_count": 1844,
  "heading_count": 18,
  "generated_at": "2026-06-17T12:00:00Z",
  "generator": "mimir-wiki"
}
```

`knowledge/quality_scores.jsonl` row shape:

```json
{
  "schema_version": "mimir-wiki/v1",
  "run_id": "20260617T120000Z-enrich-abc123",
  "dataset_name": "customer-identity-and-access-management-entra",
  "source_system": "confluence",
  "document_id": "confluence:CIAME:123456789",
  "page_id": "123456789",
  "space_key": "CIAME",
  "source_updated_at": "2026-05-01T12:45:00Z",
  "source_content_hash": "sha256:abc123",
  "quality_score": 82,
  "quality_band": "good",
  "dimensions": {
    "freshness": 90,
    "authority": 85,
    "completeness": 80,
    "operational_value": 88,
    "ownership_clarity": 70,
    "contradiction_penalty": 0
  },
  "warnings": ["missing_explicit_owner"],
  "generated_at": "2026-06-17T12:00:00Z",
  "generator": "mimir-wiki"
}
```

`knowledge/themes.jsonl` row shape:

```json
{
  "schema_version": "mimir-wiki/v1",
  "run_id": "20260617T120000Z-enrich-abc123",
  "dataset_name": "customer-identity-and-access-management-entra",
  "source_system": "confluence",
  "theme_id": "theme:identity-support",
  "theme": "identity support",
  "normalized_theme": "identity support",
  "document_count": 42,
  "documents": ["confluence:CIAME:123456789"],
  "confidence": 0.84,
  "method": "deterministic+llm",
  "generated_at": "2026-06-17T12:00:00Z",
  "generator": "mimir-wiki"
}
```

`knowledge/concepts.jsonl` row shape:

```json
{
  "schema_version": "mimir-wiki/v1",
  "run_id": "20260617T120000Z-enrich-abc123",
  "dataset_name": "customer-identity-and-access-management-entra",
  "source_system": "confluence",
  "concept_id": "concept:authentication-failure-recovery",
  "concept": "authentication failure recovery",
  "normalized_concept": "authentication failure recovery",
  "description": "Recovering service access after an authentication outage.",
  "document_count": 18,
  "documents": ["confluence:CIAME:123456789"],
  "confidence": 0.82,
  "method": "llm",
  "generated_at": "2026-06-17T12:00:00Z",
  "generator": "mimir-wiki"
}
```

`knowledge/candidate_entities.jsonl` row shape:

```json
{
  "schema_version": "mimir-wiki/v1",
  "run_id": "20260617T120000Z-enrich-abc123",
  "dataset_name": "customer-identity-and-access-management-entra",
  "source_system": "confluence",
  "entity_id": "candidate:application:forgerock",
  "name": "ForgeRock",
  "normalized_name": "forgerock",
  "entity_type": "application",
  "aliases": ["OpenAM"],
  "document_count": 12,
  "mentions": [
    {
      "document_id": "confluence:CIAME:123456789",
      "page_id": "123456789",
      "evidence": "ForgeRock Support Runbook",
      "source_field": "title"
    }
  ],
  "confidence": 0.86,
  "method": "deterministic+llm",
  "generated_at": "2026-06-17T12:00:00Z",
  "generator": "mimir-wiki"
}
```

#### 21.4.6 Enrichment Semantics

The first implementation should keep enrichment terms distinct:

```text
keywords
  Short document-level terms. Useful for search and filtering. Usually 5-20 per
  page. May come from headings, labels, title terms, links and LLM extraction.

themes
  Corpus-level recurring topics that group multiple documents. Themes should be
  normalized and deduplicated across the selected cache.

concepts
  More precise technical or operational ideas than themes. Concepts can be
  document-level and corpus-level, and should include a short description when
  generated by an LLM.

candidate_entities
  Possible applications, teams, dependencies, environments, dashboards,
  incidents, changes, support groups or external systems. They are candidates
  until a later entity-resolution phase confirms them.
```

Each item should record `confidence`, `method`, and source evidence. Duplicate
values should be normalized case-insensitively, punctuation-insensitively and
with simple whitespace collapsing. LLM outputs should merge into deterministic
outputs rather than replacing them.

#### 21.4.7 Incremental Processing and Atomic Writes

`--changed-only` should skip a page only when all relevant signatures match:

```text
source_content_hash
schema_version
prompt_version
provider
model_or_deployment
task
enrichment_config_hash
```

Changing a prompt, schema, provider, model, task route, scoring config or Onyx
Markdown config should invalidate only the affected artifacts. Failed pages
should be eligible for retry on the next run unless the failure is marked
non-retryable.

Writers must use atomic writes: write to a temporary file in the same directory,
flush, and rename into place. Global JSONL files should be generated in a stable
sort order, preferably by `(space_key, page_id)`, so diffs are useful.

#### 21.4.8 Onyx POC Markdown Rules

Generated Onyx POC filenames must be stable and collision-resistant:

```text
dist/onyx-enriched/{dataset_name}/{space_key}/{page_id}-{slug}.md
```

Rules:

- always prefix filenames with `page_id`;
- build `slug` from the title using lowercase ASCII, hyphens and a maximum
  length;
- write `#ONYX_METADATA={...}` as the first line of every generated file;
- serialize the metadata after `ONYX_METADATA=` as compact valid JSON;
- escape JSON values correctly for quotes, backslashes, newlines and Unicode;
- preserve source title in `ONYX_METADATA.file_display_name`, not the slug;
- include required `link`, `file_display_name` and `doc_updated_at` keys;
- include useful Mimir scalar tags such as dataset, page ID, document type,
  quality score and run ID;
- include source content by default, but allow `--no-include-source-content`;
- cap included source content with a configurable maximum and report truncation;
- never split one source page into multiple Onyx files in MVP 1 unless an
  explicit chunked-output option is added later.

#### 21.4.9 Oversized Pages, Attachments and Redaction

Oversized pages:

- deterministic metadata, headings, links and source stats should run over the
  full page;
- LLM tasks should use configurable chunking when content exceeds the task
  model context budget;
- chunked LLM outputs must be merged into the same page-level schema and record
  chunk count in `enrichment.json`;
- context-length provider errors should be non-retryable and should recommend a
  chunking/config change.

Attachments:

- MVP 1 should not parse binary attachments;
- attachment names, counts and metadata may be indexed from the cache;
- reports should surface pages with attachments as possible high-value follow-up
  candidates.

Redaction and privacy:

- generated Onyx POC Markdown should pass through configurable redaction rules
  before writing when redaction is enabled;
- high-confidence secret matches should fail the page export or redact the
  value, depending on config;
- run manifests and logs must never include secrets, full API keys, bearer
  tokens or unredacted provider credentials;
- redaction warnings should appear in `warnings.jsonl` and reports.

#### 21.4.10 LLM Usage and Cost Accounting

Every live LLM call should emit usage metadata when the provider returns it:

```json
{
  "schema_version": "mimir-wiki/v1",
  "run_id": "20260617T120000Z-enrich-abc123",
  "dataset_name": "customer-identity-and-access-management-entra",
  "source_system": "confluence",
  "document_id": "confluence:CIAME:123456789",
  "page_id": "123456789",
  "space_key": "CIAME",
  "source_updated_at": "2026-05-01T12:45:00Z",
  "source_content_hash": "sha256:abc123",
  "task": "summary",
  "provider": "azure-openai",
  "model": "gpt-4.1",
  "prompt_version": "summary-v1",
  "input_tokens": 1800,
  "output_tokens": 240,
  "cached": false,
  "attempts": 1,
  "elapsed_ms": 1820,
  "generated_at": "2026-06-17T12:02:00Z",
  "generator": "mimir-wiki"
}
```

Reports should summarize LLM calls by provider, model/deployment, task, cached
versus live calls, token usage and final failure count. Cost estimates may be
added later, but token accounting should exist from the start.

#### 21.4.11 MVP Acceptance Tests

Before MVP 1 is considered implemented, tests should cover:

- `validate-cache` succeeds against a tiny valid fixture and fails with useful
  messages for missing `dataset.json`, `manifest.jsonl`, page metadata and
  Markdown files;
- real-cache-derived fixtures preserve observed schema details such as compact
  manifest rows, object-shaped authors, separate `links.json`, `conversion.json`
  and separate content hashes;
- `enrich --provider none` writes deterministic `enrichment.json`,
  `document_index.jsonl`, `quality_scores.jsonl`, Onyx POC Markdown and reports;
- `--changed-only` skips unchanged pages and reprocesses pages when content hash
  or task signatures change;
- Onyx POC Markdown starts with a valid `#ONYX_METADATA={...}` first line
  containing `link`, `file_display_name` and `doc_updated_at`;
- CLI JSON mode suppresses progress output and returns machine-readable
  summaries;
- retry, backoff and rate-limit handling is tested with mocked providers;
- no unit test requires a live Confluence instance or live LLM provider.

---

## 22. Key Design Decisions

These decisions define the recommended MVP behaviour. Later iterations can relax
or extend them once the first application-focused workflow is reliable.

### 22.1 Source Scope

Questions:

- Should v1 process one space, one page tree, or arbitrary cache folders?
- Should it support multiple source caches at once?
- Should non-Confluence sources be included immediately or later?

Suggested v1 decision:

```text
Support one local mimir-confluence cache folder first. That folder may be a
broad mixed application/support tree or a specialised RCA tree. Add multi-cache
joins and non-Confluence sources after the one-cache workflow is stable.
```

### 22.2 LLM Provider

Questions:

- Azure OpenAI first?
- Ollama fallback?
- OpenAI-compatible abstraction?
- Should enrichment work in `--provider none` deterministic-only mode?

Suggested v1 decision:

```text
Use deterministic-only --provider none for tests and pipeline validation.
Support OpenAI, Azure OpenAI and Azure AI Foundry behind a provider
abstraction. Allow task-specific model routing so cheaper/faster deployments
can handle classification and keyword-like tasks while stronger deployments can
handle summaries, concepts, operational interpretation and quality warnings.
Defer Ollama support unless offline/local operation becomes a hard requirement.
```

### 22.3 Fact Store Format

Questions:

- Store facts inside `enrichment.json` only?
- Also create global `facts.jsonl`?
- Move to Postgres early?

Suggested v1 decision:

```text
Store candidate facts in each enrichment.json and also materialise global facts.jsonl for compilation.
```

### 22.4 Entity Resolution

Questions:

- How should aliases be handled?
- Should users maintain an alias file?
- Should AI suggest aliases?

Suggested v1 decision:

```text
Use a user-editable entity_aliases.yaml file plus AI-suggested aliases marked as unreviewed.
```

Example:

```yaml
ForgeRock:
  type: application
  aliases:
    - FR
    - OpenAM
    - Identity Platform
```

### 22.5 Review Status Model

Questions:

- Which statuses are required?
- Should approved pages be moved folders or only marked in front matter?
- Should `publish` require both `status: approved` and `reviewed_by`?

Suggested v1 decision:

```text
Approval is front-matter driven. publish requires status: approved,
reviewed_at, and reviewed_by when configured. Major factual sections must have
source evidence or an explicit unknown/not found/needs review marker.
```

### 22.6 Onyx Publishing

Questions:

- Should Onyx index the Obsidian vault directly?
- Should there be a clean `dist/onyx-approved` folder?
- Should draft pages be published to a separate source?

Suggested v1 decision:

```text
Approved pages remain in the Obsidian vault as the human-maintained source of
truth. publish copies approved pages into dist/onyx-approved, and Onyx indexes
only that clean approved folder. Draft pages are not published to Onyx.
```

### 22.7 Markdown Link Style

Questions:

- Use Obsidian wikilinks `[[ForgeRock]]`?
- Use standard Markdown links `[ForgeRock](../applications/forgerock.md)`?
- Support both?

Suggested v1 decision:

```text
Use standard Markdown links for portability, with optional Obsidian wikilink generation later.
```

### 22.8 Template Strategy

Questions:

- Should templates be hard-coded?
- Should users edit Jinja2 templates?
- Should templates be versioned?

Suggested v1 decision:

```text
Use Jinja2 templates stored in templates/ and allow user overrides.
```

### 22.9 Quality Score Rubric

Questions:

- What weights should be used?
- Should authority be configured by Confluence space/key/path?
- How should old but useful historical docs be handled?

Suggested v1 decision:

```text
Use deterministic scoring as the base and AI only for warnings, semantic
staleness, and operational usefulness interpretation. Use configurable YAML
scoring profiles.
```

Initial source-use thresholds:

```text
overall_score >= 70:
  usable as supporting evidence.

overall_score 50-69:
  usable only with warnings or corroboration from stronger sources.

overall_score < 50:
  excluded from generated factual claims by default, but may be listed as a
  stale, low-quality, or historical source.
```

Score thresholds do not override explicit archive/deprecated markers or strong
source-authority rules.

Example:

```yaml
spaces:
  IDENTITY:
    authority_score: 80
  ARCH:
    authority_score: 90
  PERSONAL:
    authority_score: 30

document_type_weights:
  approved_runbook: 95
  runbook: 90
  knowledge_article: 85
  rca: 85
  architecture: 85
  known_error: 80
  support_model: 80
  incident: 75
  change_record: 70
  reference: 50
  meeting_notes: 30
  project_plan: 40
```

Claim-specific authority should be layered on top of document-type authority.
For example:

```yaml
claim_type_authority:
  current_recovery_procedure:
    preferred:
      - approved_runbook
      - runbook
      - knowledge_article
    supporting:
      - known_error
      - rca
      - incident
  historical_impact_timeline:
    preferred:
      - incident
      - rca
    supporting:
      - change_record
  root_cause:
    preferred:
      - rca
      - finalized_incident
      - known_error
    supporting:
      - incident
      - runbook
  current_architecture_dependency:
    preferred:
      - architecture
      - application_card
      - runbook
      - support_model
    supporting:
      - rca
      - incident
```

Old but useful documents should be represented as historical evidence rather
than current truth. Enrichment and compiled source lists should support fields
such as `historical: true`, `currentness: historical`, `superseded_by`, and
`valid_for_context`.

RCA documents are a primary example of useful historical evidence. Large RCA
sets should be indexed and clustered by application, dependency, failure mode,
impact type and reusable lesson. They should feed application cards, runbooks,
known-failure-mode pages and documentation quality reports, but raw RCA
narratives should not be treated as approved current operating procedures.

### 22.10 Human Edits and Regeneration

Questions:

- What happens when a generated page is manually edited?
- Should recompilation overwrite it?
- Should generated sections be protected?

Suggested v1 decision:

```text
Never overwrite approved pages by default. Regenerate into 00 Review with a diff report.
```

Generated facts should remain source-derived. Human corrections should live in
a separate override file, such as `knowledge/fact_overrides.yaml`, so
regeneration does not erase review decisions and provenance remains clear.

### 22.11 Incremental Processing

Questions:

- Re-enrich only changed source documents?
- Recompile only affected pages?
- How should source-to-compiled-page dependencies be tracked?

Suggested v1 decision:

```text
Use content hashes for changed-only enrichment. Track compiled_page_sources.jsonl for later incremental compilation.
```

### 22.12 Security and Data Handling

Questions:

- Should the repo contain internal source docs?
- Should secrets ever be stored?
- Should there be a redaction mode?

Suggested v1 decision:

```text
Never store secrets. Keep PATs in environment variables. Add optional redaction rules before publishing to wider locations.
```

Keep raw source Markdown outside the main Obsidian vault by default. Compiled
pages should link to source document IDs and URLs, and `mimir-wiki` may generate
a source index or evidence appendix. Avoid placing raw source dumps under a
`90 Sources` vault folder in the MVP because it blurs the boundary between
source evidence and curated knowledge.

---

## 23. Configuration Files

### 23.1 `mimir-wiki.yaml`

Example:

```yaml
project_name: identity-mimir

paths:
  cache: ./cache/{dataset_name}
  knowledge: ./knowledge
  vault: ./vault
  dist: ./dist/onyx-approved
  dist_onyx_enriched: ./dist/onyx-enriched
  reports: ./reports
  runs: ./runs
  llm_cache: ./.mimir-wiki/llm-cache

features:
  deterministic:
    document_classification: true
    keywords: true
    headings: true
    links: true
    quality_scoring: true
    candidate_entities: true
  llm:
    enabled: false
    tasks:
      classification: false
      summary: true
      keywords: false
      themes: true
      concepts: true
      candidate_entities: true
      operational_signals: true
      quality_warnings: true
  outputs:
    enrichment_json: true
    document_index: true
    quality_scores: true
    themes: true
    concepts: true
    candidate_entities: true
    onyx_poc_markdown: true
    reports: true

llm:
  provider: azure-openai  # none | openai | azure-openai | azure-ai-foundry | openai-compatible
  model: gpt-4.1
  prompt_version: enrichment-v1
  task_models:
    classification:
      provider: azure-openai
      model: gpt-4.1-mini
      prompt_version: classification-v1
    summary:
      provider: azure-openai
      model: gpt-4.1
      prompt_version: summary-v1
    keywords:
      provider: azure-openai
      model: gpt-4.1-mini
      prompt_version: keywords-v1
    themes:
      provider: azure-openai
      model: gpt-4.1
      prompt_version: themes-v1
    concepts:
      provider: azure-openai
      model: gpt-4.1
      prompt_version: concepts-v1
    candidate_entities:
      provider: azure-openai
      model: gpt-4.1
      prompt_version: candidate-entities-v1
    operational_signals:
      provider: azure-openai
      model: gpt-4.1
      prompt_version: operational-signals-v1
    quality_warnings:
      provider: azure-openai
      model: gpt-4.1
      prompt_version: quality-warnings-v1
  temperature: 0
  max_concurrency: 4
  requests_per_minute:
  tokens_per_minute:
  max_retries: 3
  initial_backoff_seconds: 1
  max_backoff_seconds: 60
  backoff_jitter: true
  respect_retry_after: true
  timeout_seconds: 60
  fail_fast: false
  retryable_status_codes:
    - 408
    - 409
    - 429
    - 500
    - 502
    - 503
    - 504
  cache_by:
    - source_content_hash
    - prompt_version
    - provider
    - model
    - task
  openai:
    api_key_env: OPENAI_API_KEY
  azure_openai:
    endpoint_env: AZURE_OPENAI_ENDPOINT
    api_key_env: AZURE_OPENAI_API_KEY
    deployment_env: AZURE_OPENAI_DEPLOYMENT
    api_version_env: AZURE_OPENAI_API_VERSION
  azure_ai_foundry:
    endpoint_env: AZURE_AI_FOUNDRY_ENDPOINT
    api_key_env: AZURE_AI_FOUNDRY_API_KEY
    deployment_env: AZURE_AI_FOUNDRY_DEPLOYMENT
  chunking:
    enabled: true
    max_input_tokens_per_chunk: 8000
    overlap_tokens: 200
    max_chunks_per_document: 20

processing:
  page_workers: 8
  llm_workers: 4
  writer_workers: 1
  use_threads_for_blocking_io: false
  fail_fast: false

cli:
  color: auto  # auto | always | never
  progress: auto  # auto | always | never
  default_output: human  # human | json
  summary: true
  log_level: info
  show_tracebacks: false
  quiet: false

scoring:
  freshness_days:
    excellent: 90
    good: 180
    acceptable: 365
    stale: 730
  weights:
    freshness: 0.20
    authority: 0.20
    completeness: 0.20
    operational_value: 0.25
    ownership_clarity: 0.10
    contradiction_penalty: 0.05
  rca:
    treat_old_documents_as_historical: true
    required_signals:
      - has_impact_summary
      - has_root_cause
      - has_corrective_actions
    preferred_signals:
      - has_incident_timeline
      - has_contributing_factors
      - has_action_owners
      - has_detection_gap
      - has_monitoring_gap
      - has_runbook_gap
  document_type_weights:
    approved_runbook: 95
    runbook: 90
    knowledge_article: 85
    rca: 85
    architecture: 85
    known_error: 80
    support_model: 80
    incident: 75
    change_record: 70
    reference: 50
    project_plan: 40
    meeting_notes: 30
    archive: 20
    unknown: 10
  claim_type_authority:
    current_recovery_procedure:
      preferred:
        - approved_runbook
        - runbook
        - knowledge_article
      supporting:
        - known_error
        - rca
        - incident
    historical_impact_timeline:
      preferred:
        - incident
        - rca
    root_cause:
      preferred:
        - rca
        - finalized_incident
        - known_error
      supporting:
        - incident
        - runbook

publishing:
  require_status: approved
  require_reviewed_at: true
  include_quality_reports: true

onyx_poc:
  emit_enriched_markdown: true
  include_source_content: true
  max_source_content_chars: 200000
  slug_max_chars: 90
  chunked_output: false
  metadata_field: ONYX_METADATA
  metadata_line_format: hash_prefix  # hash_prefix | html_comment
  metadata_policy: lean_filters  # lean_filters | extended_debug
  required_metadata:
    link: source.url
    file_display_name: source.title
    doc_updated_at: source.updated_at
  tag_metadata:
    dataset_name: source.dataset_name
    source_system: source.system
    space_key: source.space_key
    document_type: enrichment.document_type
    quality_band: enrichment.quality_band
    approval_status: unreviewed
    historical: enrichment.historical
    currentness: enrichment.currentness
  owner_metadata:
    enabled: false
    primary_owners: enrichment.primary_owners
    secondary_owners: enrichment.secondary_owners

redaction:
  enabled: true
  action: redact  # redact | fail
  fail_on_high_confidence_secret: true
  replacement: "[REDACTED]"
  patterns:
    - aws_access_key
    - github_token
    - slack_token
    - openai_key
    - jwt
    - password_assignment

artifacts:
  schema_version: mimir-wiki/v1
  atomic_writes: true
  stable_sort_order:
    - space_key
    - page_id

markdown:
  link_style: markdown
  front_matter: yaml
```

### 23.2 `entity_aliases.yaml`

```yaml
entities:
  ForgeRock:
    type: application
    aliases:
      - FR
      - OpenAM
      - Identity Platform
  LDAP:
    type: dependency
    aliases:
      - Directory Services
```

### 23.3 `source_authority.yaml`

```yaml
spaces:
  IDENTITY:
    default_authority_score: 80
  ARCH:
    default_authority_score: 90
  PROJECTS:
    default_authority_score: 45

labels:
  rca: 20
  postmortem: 20
  root-cause-analysis: 20
  runbook: 15
  knowledge-base: 15
  knowledge-article: 15
  known-error: 15
  production: 10
  archive: -40
  deprecated: -60
```

---

## 24. Validation Rules

### 24.1 Enrichment Validation

- `document_id` must match manifest;
- `source_content_hash` must match current source hash;
- `ONYX_METADATA.link` must match source URL;
- `ONYX_METADATA.file_display_name` must match source title;
- `ONYX_METADATA.doc_updated_at` must match Confluence `updated_at`;
- document type must be from allowed enum;
- quality scores must be between 0 and 100;
- confidence must be between 0 and 1;
- candidate facts must have subject, predicate, object and evidence.
- candidate facts should include a claim type where practical, so source
  authority can be evaluated against the kind of claim being made.
- RCA enrichments must preserve impact, root-cause, corrective-action and
  follow-up claims as historical evidence unless explicitly superseded or
  deprecated.

### 24.2 Onyx POC Enriched Markdown Validation

- first line starts with `#ONYX_METADATA=`;
- text after `ONYX_METADATA=` is valid JSON;
- no YAML front matter appears before the Onyx metadata line;
- `link` is populated from the Confluence source URL;
- `file_display_name` is populated from the Confluence source
  title;
- `doc_updated_at` is populated from the Confluence `updated_at`
  timestamp;
- low-cardinality tag keys such as `dataset_name`, `source_system`,
  `space_key`, `document_type`, `quality_band`, `approval_status`,
  `historical` and `currentness` exist when available;
- high-cardinality debug values such as `run_id`, `document_id`, `page_id`,
  source hashes and prompt versions are not included in the Onyx metadata line
  by default;
- `approval_status` is `unreviewed`;
- Markdown body includes an enrichment summary and source metadata section;
- Markdown body includes source content unless `--no-include-source-content`
  was used;
- generated file paths are stable across runs for unchanged page IDs/titles.

### 24.3 Compiled Page Validation

- YAML front matter exists;
- required fields exist;
- page type is valid;
- status is valid;
- source documents exist in manifest;
- required sections are present;
- source evidence section exists;
- internal links resolve where possible;
- approved pages have `reviewed_at` and `reviewed_by` if configured.
- major factual claims are supported by evidence sources suitable for the claim
  type, or are explicitly marked as weak, historical, provisional or needing
  review.
- approved known-failure-mode pages have at least one RCA, incident,
  known-error or runbook source for each major factual section.

---

## 25. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| AI generates plausible but wrong docs | Require source evidence, validation and human review |
| Stale docs are compiled into canonical pages | Use freshness and authority scoring; expose open questions |
| Manual edits are overwritten | Never overwrite approved pages by default |
| Too much noise from raw Confluence | Use source selection, quality scoring and narrow scopes |
| Entity aliases are wrong | Use editable alias files and review suggested aliases |
| Facts are extracted incorrectly | Store confidence and evidence text; review before approval |
| Trusted sources are trusted for the wrong claim type | Score authority by claim type; require warnings or corroboration when evidence is mismatched |
| RCA lessons are treated as current procedures | Mark RCAs as historical evidence and compile reusable lessons into reviewed known-failure-mode or runbook pages |
| RCA volume overwhelms reviewers | Cluster by application, dependency and failure mode before generating pages |
| Onyx indexes drafts accidentally | Publish only `status: approved` into `dist/onyx-approved` |
| Pipeline becomes over-complex | Deliver MVP with limited commands and page types first |

---

## 26. Suggested Implementation Phases

### 26.0 MVP Kickoff TODO List

Use this as the concrete implementation checklist for the first coding pass.
Complete items in order unless a later item is needed to unblock tests.

1. Create project skeleton.
   - Add `pyproject.toml`, `src/mimir_wiki/`, `tests/`, `.env.example` and
     `scripts/quality.sh`.
   - Add Typer, Rich, Pydantic, PyYAML, python-dotenv, orjson, pytest, ruff and
     mypy dependencies.
   - Done when `mimir-wiki --help` runs and quality commands can execute.

2. Define core schemas.
   - Implement Pydantic models for `dataset.json`, manifest rows,
     `metadata.json`, `links.json`, `conversion.json`, `enrichment.json`, run
     manifests, page failures and all MVP JSONL rows.
   - Include `schema_version`, `run_id`, source IDs, hashes and generated
     timestamps where required by section 21.4.
   - Done when schema unit tests cover valid fixtures and common missing-field
     failures.

3. Implement config loading.
   - Support built-in defaults, config file, profile merge, `.env`, environment
     variables and CLI flags in the precedence order defined by section 21.4.2.
   - Add `features`, `llm`, `processing`, `onyx_poc`, `redaction`, `paths` and
     `artifacts` config models.
   - Done when tests prove CLI flags override profile/config values and secrets
     are redacted from resolved config output.

4. Implement cache reader and validator.
   - Load the observed `mimir-confluence` cache layout.
   - Validate `dataset.json`, `manifest.jsonl`, `manifest.summary.json`, page
     folders, `metadata.json`, `clean.md`, `text.txt`, `links.json` and
     `conversion.json`.
   - Surface optional `errors.jsonl` in validation output and reports.
   - Done when `validate-cache --cache ./cache/customer-identity-and-access-management-entra`
     produces a summary without mutating source files.

5. Implement CLI shell and run context.
   - Add `validate-cache`, `enrich` and `report` commands.
   - Implement Rich output, `--json`, `--quiet`, `--verbose`, `--no-color`,
     exit codes, run IDs and run manifests.
   - Done when each command prints a human summary by default and a machine
     summary with `--json`.

6. Implement deterministic enrichment.
   - Support `--provider none`.
   - Extract document index fields, headings, labels, links, source stats,
     deterministic keywords, simple document types, candidate entities, quality
     scores and status flags.
   - Done when `enrich --provider none --limit 10` writes deterministic
     artifacts without live network or LLM calls.

7. Implement artifact writers.
   - Write `pages/{page_id}/enrichment.json`,
     `knowledge/document_index.jsonl`, `knowledge/quality_scores.jsonl`,
     `knowledge/themes.jsonl`, `knowledge/concepts.jsonl` and
     `knowledge/candidate_entities.jsonl`.
   - Use atomic writes and stable sort order.
   - Done when two unchanged runs produce stable diffs.

8. Implement Onyx POC Markdown writer.
   - Write `dist/onyx-enriched/{dataset_name}/{space_key}/{page_id}-{slug}.md`.
   - Make the first line `#ONYX_METADATA={...}` with valid JSON.
   - Include required Onyx keys and only lean low-cardinality filter tags by
     default.
   - Put source IDs, run ID, hashes and prompt/config details in the Markdown
     body, not first-line metadata.
   - Done when validation proves the first line parses as JSON and contains
     `link`, `file_display_name` and `doc_updated_at`.

9. Implement changed-only processing.
   - Compare source hash, schema version, prompt version, provider,
     model/deployment, task and enrichment config hash.
   - Retry previously failed pages unless marked non-retryable.
   - Done when tests prove unchanged pages are skipped and changed signatures
     force regeneration.

10. Implement reports.
    - Generate `reports/cache_validation.md`, `reports/enrichment_summary.md`,
      `reports/document_types.md`, `reports/stale_or_deprecated.md` and
      `reports/high_value_sources.md`.
    - Include links back to page IDs, titles, source URLs and cache paths.
    - Done when reports are useful after a deterministic run on a real sample
      cache.

11. Add LLM provider interface, without turning it on by default.
    - Add provider interfaces, retry/rate-limit wrapper, task routing, response
      cache keys and usage logging.
    - Keep `features.llm.enabled: false` and `--provider none` as the default
      local/test path.
    - Done when mocked provider tests cover retries, rate limiting, usage
      logging and final failure records.

12. Add first optional LLM tasks.
    - Start with summaries, themes, concepts and candidate entities.
    - Keep deterministic outputs and LLM outputs merged under the same schemas.
    - Done when `--enable-llm --llm-task summary` can enrich a small fixture
      through a mocked provider and a configured live provider.

13. Run MVP acceptance suite.
    - Run formatter, linter, mypy and pytest.
    - Run deterministic CLI smoke tests against at least one tiny fixture and
      one real sample cache with a small `--limit`.
    - Done when section 21.4.11 acceptance tests pass.

### Phase 1: Enrichment Foundation

- validate cache;
- load manifest/pages;
- define Pydantic schemas;
- create config system;
- create CLI skeleton;
- create deterministic metadata analysis;
- build `knowledge/document_index.jsonl`;
- write `reports/cache_validation.md`.

### Phase 2: Enrichment and Inventory

- document classification;
- keywords;
- themes and concepts;
- candidate entity extraction;
- operational signals;
- RCA-specific signal extraction;
- quality scoring;
- changed-only processing by content hash;
- `enrichment.json` output;
- `knowledge/quality_scores.jsonl`;
- `knowledge/themes.jsonl`;
- `knowledge/concepts.jsonl`;
- `knowledge/candidate_entities.jsonl`;
- Onyx POC enriched Markdown export;
- enrichment reports.

### Phase 3: Knowledge Model

- global entity registry;
- facts extraction;
- RCA index and failure-mode clustering;
- contradiction detection;
- reports.

### Phase 4: Compilation

- application card template;
- known failure mode template;
- runbook template;
- quality report template;
- source selection/ranking;
- AI-assisted page generation;
- compiled Markdown output.

### Phase 5: Review and Publish

- Obsidian-compatible vault layout;
- approval status handling;
- publish to `dist/onyx-approved`;
- link validation;
- report generation.

### Phase 6: Iteration

- incremental recompilation;
- Postgres backend;
- Onyx API integration;
- MkDocs publishing;
- Confluence publishing;
- richer dashboards.

---

## 27. Backlog Status

This was the initial backlog. It is now annotated with current implementation
status so the spec can continue to serve as the project TODO list.

### Must Have

- [x] Pydantic schemas;
- [x] `validate-cache`;
- [x] `enrich`;
- [x] `report`;
- [x] document index output;
- [x] per-page enrichment output;
- [x] deterministic `--provider none` enrichment;
- [x] document type classification;
- [x] keyword, theme and concept extraction;
- [x] candidate entity extraction;
- [x] Onyx POC enriched Markdown export with `ONYX_METADATA`;
- [x] quality scoring;
- [x] changed-only processing;
- [x] progress bars and structured logs.

### Should Have

- [x] LLM-assisted summaries;
- [x] LLM-assisted themes and concepts;
- [x] RCA-specific enrichment signals;
- [x] duplicate or near-duplicate detection;
- [x] stale/deprecated/archive reports;
- [x] high-value-source reports;
- [ ] Onyx file connector smoke test using `dist/onyx-enriched`;
- [x] configurable scoring;
- [x] small sanitized fixtures derived from real exports.

### Could Have

- [ ] `extract-facts` command;
- [ ] `build-entities` command;
- [ ] `detect-contradictions` command;
- [ ] `rca-index` command;
- [ ] GLiNER integration;
- [ ] KeyBERT/YAKE keyword extraction;
- [ ] entity alias file and resolved entity registry;
- [ ] Obsidian vault output;
- [ ] Markdown front matter for compiled pages;
- [x] source evidence sections for Onyx POC Markdown;
- [ ] `compile application`;
- [ ] `compile known-failure-mode`;
- [ ] `compile runbook`;
- [ ] `compile quality-report`;
- [ ] approved-only publish folder;
- [ ] MkDocs export;
- [ ] Onyx API push;
- [ ] Confluence publishing;
- [ ] Postgres storage;
- [ ] `compile all`;
- [x] visual extraction/index/report output;
- [ ] visual dependency graph output.

### Won't Have in MVP

- full web UI;
- automatic Confluence rewriting;
- enterprise permissions;
- perfect knowledge graph;
- fully autonomous approval;
- compiled wiki pages;
- runbook generation;
- application cards;
- known-failure-mode pages;
- Onyx publishing.

---

## 28. Resolved Iteration Decisions

1. Start MVP 1 with enrichment and inventory, not compiled wiki pages. The first
   useful deliverable is a reliable map of documents, document types, themes,
   concepts, candidate entities, quality signals and high-value source
   candidates.
2. Quality scoring should be mostly deterministic initially. AI may add warnings
   and semantic interpretation, but repeatable metadata and structure checks
   should drive the base score.
3. MVP 1 must work in deterministic-only `--provider none` mode for tests and
   repeatable local validation. Azure OpenAI can be added behind a provider
   abstraction for optional summaries, themes, concepts and candidate entity
   extraction. Defer Ollama unless local/offline operation becomes a hard
   requirement.
4. Compiled pages are post-MVP. When compilation begins, application cards
   should be the first compiled page type because they establish the entity
   spine for owners, support groups, dependencies, environments, dashboards and
   source evidence. Runbooks should follow once application identity and source
   selection are reliable.
5. Compiled pages should use standard Markdown links initially. Obsidian
   wikilinks can be added later as an optional render mode.
6. Approved pages should eventually live in-place in the Obsidian vault as
   reviewed knowledge and be copied to `dist/onyx-approved` for Onyx indexing.
   Onyx should not index the full vault directly.
7. Raw source Markdown should stay outside the main Obsidian vault. MVP 1 should
   not create a vault; later generated pages should reference source IDs/URLs
   and may link to a source index or evidence appendix.
8. `mimir-wiki compile all` should be deferred until manual single-entity
   compilation works well and dependency tracking exists.
9. Source-use thresholds should default to: `>= 70` usable as supporting
   evidence, `50-69` usable only with warnings or corroboration, and `< 50`
   excluded from generated factual claims by default.
10. Old but historically useful documents should be represented as historical
   evidence, with explicit fields such as `historical`, `currentness`,
   `superseded_by`, and `valid_for_context`.
   RCA documents should use this path by default unless explicitly deprecated
   or superseded.
11. Generated pages should include concise source lists by default, with full
    evidence tables generated in an appendix, companion report, or review view.
12. Extracted facts should be source-generated and provenance-preserving. Human
    corrections should live in a separate override file rather than editing the
    generated fact store directly.
13. Review approval should require source evidence for every major factual
    section, or an explicit `unknown`, `not found`, or `needs review` marker.
14. RCA corpora should be treated as first-class evidence. MVP 1 should classify
    and enrich RCA pages as historical evidence. RCA indexing, clustering and
    known-failure-mode generation are post-MVP knowledge-model and compilation
    work.
15. Runbooks, knowledge articles, RCAs and incident documents should be higher
    authority sources than meeting notes or project plans, but authority must
    be claim-aware. A runbook is strong evidence for current recovery procedure;
    an RCA is strong evidence for historical root cause and lessons; an incident
    is strong evidence for timeline and impact; a knowledge article is strong
    evidence for standard support guidance.

---

## 29. Recommended First Implementation Decision Set

For the first build, use these decisions:

```text
MVP target:
  Enrichment and inventory only. Do not generate curated wiki pages in MVP 1.

Source input:
  One mimir-confluence cache folder, selected with --cache. The folder may be
  a broad mixed application/support export or a specialised RCA export.

LLM:
  Deterministic --provider none must work first. Add Azure OpenAI behind a
  provider abstraction for optional summaries, themes, concepts and candidate
  entity extraction. Defer Ollama unless local/offline operation becomes
  required.

Storage:
  File-based JSON/JSONL and Markdown.

Primary outputs:
  pages/{page_id}/enrichment.json
  knowledge/document_index.jsonl
  knowledge/quality_scores.jsonl
  knowledge/themes.jsonl
  knowledge/concepts.jsonl
  knowledge/candidate_entities.jsonl
  dist/onyx-enriched/{dataset_name}/{space_key}/{page_id}-{slug}.md
  reports/*.md

Human frontend:
  Reports and JSON/JSONL artifacts. Obsidian vault output starts in a later
  compilation phase.

Onyx POC:
  MVP 1 may upload dist/onyx-enriched with an Onyx file connector. Each file
  must start with #ONYX_METADATA={...} as the first line. The JSON must include
  link, file_display_name and doc_updated_at derived from Confluence metadata.
  These files are unreviewed source-enriched artifacts, not approved curated
  knowledge.

Approval:
  No approval workflow in MVP 1 because no curated knowledge is published.

Compiled page types:
  None in MVP 1. Application cards, known failure modes, runbooks and quality
  reports start after enrichment artifacts are useful and stable.

Markdown links:
  Not applicable to MVP 1 except in generated reports.

Regeneration:
  Recompute derived enrichment artifacts when content hashes change. Do not
  mutate source cache files except for explicit enrichment output paths.

Quality scoring:
  Deterministic base score + AI warnings/interpretation.
  Sources >= 70 are usable, 50-69 require warning/corroboration, and < 50 are
  excluded from generated factual claims by default.
  Runbooks, knowledge articles, RCAs, incidents and known errors get higher
  baseline authority than meeting notes or project plans, but authority is
  evaluated by claim type.

Entity aliases:
  User-editable YAML file can be introduced after candidate entities are being
  extracted reliably.

Source Markdown:
  Keep raw source Markdown outside the main Obsidian vault by default.

Facts:
  Defer formal fact extraction to the knowledge-model phase. MVP 1 may emit
  candidate entities, themes and concepts, but not authoritative facts.

Evidence display:
  Reports should link every enrichment summary back to page IDs, titles, URLs
  and cache paths.

RCA corpus:
  Treat RCAs as first-class historical evidence. MVP 1 classifies and enriches
  RCA pages; clustering and known-failure-mode generation are later phases.

Batch compilation:
  Defer all compilation until enrichment, source ranking and candidate entity
  extraction are stable.
```

---

## 30. Current Implementation Status and TODO

This section is the current project tracker as of the MVP1 implementation in
`src/mimir_wiki/`. It supersedes the older unannotated backlog while preserving
the longer-term design intent above.

### 30.1 Implemented

- [x] Python package, CLI entry point, tests and quality tooling.
- [x] Cache reader and validator for observed `mimir-confluence` exports.
- [x] Config loading with defaults, YAML, profiles, `.env`, environment
  variables and CLI overrides.
- [x] Versioned Pydantic artifact schemas and JSON Schema export.
- [x] Run manifests, warnings, page failures and LLM usage artifacts under
  `runs/{run_id}/`.
- [x] Deterministic enrichment for document type, subtype, summaries, keywords,
  themes, concepts, candidate entities, candidate facts, operational signals,
  hierarchy context and quality scoring.
- [x] Optional LLM enrichment for classification, summaries, keywords, themes,
  concepts, candidate entities, operational signals, quality warnings and key
  facts.
- [x] LLM provider abstraction for `none`, OpenAI, Azure OpenAI, Azure AI
  Foundry and OpenAI-compatible endpoints.
- [x] Shared retrying/rate-limited LLM client with timeouts, bounded retries,
  jitter, retry-after handling, optional token-per-minute throttling, adaptive
  per-model concurrency on `429` responses and mocked-provider tests.
- [x] LLM response cache keyed by source hash, prompt text/version, provider,
  model, task or bundle and enrichment config hash.
- [x] Task-specific model routing and task bundles.
- [x] Stable global JSONL indexes for documents, quality, themes, concepts,
  candidate entities, facts and visual extraction rows.
- [x] Onyx POC Markdown with first-line `#ONYX_METADATA={...}`, source content,
  key facts, prioritized source links, redaction and visual evidence sections.
- [x] `extract-visuals` multimodal OCR/caption workflow over local cache images,
  including source ranking, hash reuse, low-value skips, adaptive caps,
  representative visual sampling, concurrent page/image execution and omitted
  image inventories.
- [x] `probe-ocr` multimodal capability check.
- [x] Reports for validation, enrichment summary, document types,
  stale/deprecated pages, high-value sources, missing owners, high-value
  subtrees, attachments, duplicate candidates, LLM usage, page failures and
  visual extraction health.
- [x] Bounded page-worker concurrency and graceful `enrich` cancellation.
- [x] Unit, CLI, LLM, report, redaction, observed-shape and smoke-test coverage.

### 30.2 Outstanding MVP Hardening

- [x] Resolve Confluence image URLs that point to attachments already downloaded
  under other exported page folders, so visual extraction can reuse those local
  files instead of marking them `remote_source_not_in_cache`.
- [x] Add explicit `claim_type` to candidate facts where practical so later
  source-authority checks can reason about claim-specific evidence strength.
- [x] Enforce `llm.tokens_per_minute` in `RateLimitedLLMClient`; request-per-
  minute limiting already exists.
- [ ] Decide whether `processing.writer_workers` needs a real writer queue or
  should be removed from MVP config until needed.
- [ ] Add a CLI `--workers` override or document that page worker tuning is
  config-only.
- [ ] Tighten `--llm-task` validation at CLI override boundaries so unknown tasks
  fail early and consistently.
- [ ] Add an optional Onyx file-connector smoke test or documented manual smoke
  checklist using `dist/onyx-enriched`.
- [ ] Reconcile report filenames with section 15.13 if downstream users expect
  `stale_docs.md`, `low_quality_high_value.md`, `contradictions.md` or
  `candidate_runbooks.md` specifically.

### 30.3 Post-MVP Knowledge Model TODO

- [ ] Implement `mimir-wiki extract-facts` as a standalone command or explicitly
  retire it in favor of `enrich` writing `knowledge/facts.jsonl`.
- [ ] Implement `mimir-wiki build-entities`.
- [ ] Write `knowledge/entities.jsonl` and `knowledge/entity_aliases.jsonl`.
- [ ] Add `entity_aliases.yaml` loading and deterministic alias resolution.
- [ ] Add `source_authority.yaml` loading or an equivalent claim-authority config
  model.
- [ ] Implement `mimir-wiki detect-contradictions` and
  `knowledge/contradictions.jsonl`.
- [ ] Add `reports/contradictions.md` once contradiction detection exists.
- [ ] Implement `mimir-wiki rca-index`, `knowledge/rca_index.jsonl` and
  `reports/rca_clusters.md`.
- [ ] Add RCA/failure-mode clustering across incidents, RCAs, known errors and
  runbooks.

### 30.4 Post-MVP Compilation and Publishing TODO

- [ ] Implement source selection/ranking for compiled pages.
- [ ] Implement `compile application` first, with evidence-backed application
  cards.
- [ ] Implement `compile runbook` after application/entity identity is reliable.
- [ ] Implement `compile quality-report`.
- [ ] Implement `compile known-failure-mode`.
- [ ] Add Obsidian-compatible vault layout under `vault/00 Review/`.
- [ ] Add compiled page YAML front matter and validation.
- [ ] Add human review status transitions and approval validation.
- [ ] Implement `publish` from approved vault pages to `dist/onyx-approved`.
- [ ] Add approved-only Onyx publishing rules and link/front-matter validation.
- [ ] Defer `compile all` until manual single-entity compilation and dependency
  tracking are reliable.

### 30.5 Longer-Term TODO

- [ ] Postgres-backed store for large corpus queries and dashboards.
- [ ] Onyx API push or packaging workflow beyond file connector output.
- [ ] MkDocs export.
- [ ] Confluence publishing of reviewed pages.
- [ ] Optional web UI.
- [ ] Richer graph outputs, including visual dependency graphs.

## 31. Summary

`mimir-wiki` is the enrichment, inventory and later curation engine for Mimir.

MVP 1 consumes exported Markdown and metadata from `mimir-confluence`, validates
the cache, enriches each document, scores quality, identifies keywords, themes,
concepts and candidate entities, writes versioned artifacts, and emits
Onyx-ready enriched Markdown for a file connector POC.

Later phases should use the enrichment artifacts to extract facts, detect
contradictions, compile structured Markdown knowledge, support human review in
Obsidian, and publish only approved curated pages into the folder indexed by
Onyx.

This creates a controlled loop:

```text
messy Confluence evidence
  → local source cache
  → MVP 1 enriched document intelligence
  → Onyx POC over enriched source documents
  → later compiled Markdown knowledge
  → later human review in Obsidian
  → later approved knowledge indexed by Onyx
```

The main differentiator is not AI summarisation. MVP 1 creates a reliable,
versioned understanding of the exported corpus; later phases turn that
understanding into evidence-backed, human-governed knowledge compilation.
