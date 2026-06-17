Task: classification

Classify the supplied Confluence document using only the provided text and metadata.

Return only JSON matching this shape:
{contract}

Allowed document_type values are the documented mimir-wiki document types. Use
"unknown" when the evidence is insufficient.

Metadata JSON:
{metadata_json}

Document text chunk:
{chunk}
