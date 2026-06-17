Task bundle: operational

Extract operational enrichment fields from the supplied Confluence document
chunk. Return one JSON object that includes only the requested fields plus
optional `key_facts`. Keep warnings concise and ground every entity or signal in
the text.

For `key_facts`, prefer operational facts such as Owner, Assignment group,
Support path, Escalation, Recovery procedure, Backout procedure, Validation,
Monitoring, Alert, Dashboard, Runbook, Dependency, Environment, Region, Change,
or Incident. Omit low-value generic facts.

Return only JSON matching this combined shape:
{contract}

Metadata JSON:
{metadata_json}

Document text chunk:
{chunk}
