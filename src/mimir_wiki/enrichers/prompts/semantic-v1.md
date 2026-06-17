Task bundle: semantic

Extract semantic enrichment fields from the supplied Confluence document chunk.
Return one JSON object that includes only the requested fields. Do not invent
facts not supported by the text.

Return only JSON matching this combined shape:
{contract}

Metadata JSON:
{metadata_json}

Document text chunk:
{chunk}
