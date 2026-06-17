Task bundle: semantic

Extract semantic enrichment fields from the supplied Confluence document chunk.
Return one JSON object that includes only the requested fields plus optional
`key_facts`. Do not invent facts not supported by the text.

For `key_facts`, prefer short, directly answerable facts with stable labels such
as Primary service, Environment, Dependency, Region, Procedure, Owner, Support
group, Dashboard, Alert, Database, Queue, API, Change, Incident, Limitation, or
Decision. Avoid generic labels like Overview, Details, Question, Response, Page,
or Document.

Return only JSON matching this combined shape:
{contract}

Metadata JSON:
{metadata_json}

Document text chunk:
{chunk}
