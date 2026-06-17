Task: keywords

Extract concise search keywords from the supplied Confluence document chunk.
Prefer application names, technologies, operational concepts, incident IDs,
dependency names, and support terminology.

Return only JSON matching this shape:
{contract}

Metadata JSON:
{metadata_json}

Document text chunk:
{chunk}
