Task: quality_warnings

Identify documentation quality warnings from the supplied Confluence document
chunk. Warnings should be concise and grounded in missing, stale, ambiguous, or
operationally risky documentation signals.

Return only JSON matching this shape:
{contract}

Metadata JSON:
{metadata_json}

Document text chunk:
{chunk}
