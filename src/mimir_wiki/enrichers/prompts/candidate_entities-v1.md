Task: candidate_entities

Extract candidate entities from the supplied Confluence document chunk. Include
only entities with direct textual evidence. Prefer stable application, service,
technology, team, support group, dashboard, database, queue, API, incident, and
change names.

Return only JSON matching this shape:
{contract}

Metadata JSON:
{metadata_json}

Document text chunk:
{chunk}
