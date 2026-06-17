from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8"
    )


@pytest.fixture
def tiny_cache(tmp_path: Path) -> Path:
    cache = tmp_path / "cache" / "tiny"
    page = cache / "pages" / "123"
    write_json(
        cache / "dataset.json",
        {
            "source": "confluence",
            "dataset_name": "tiny",
            "base_url": "https://confluence.example.com",
            "api_root": "/rest/api",
            "crawl_type": "tree",
            "crawl_config": {"include_attachments": False},
            "tool_version": "0.1.0",
            "created_at": "2026-06-17T00:00:00Z",
            "updated_at": "2026-06-17T00:00:00Z",
        },
    )
    write_jsonl(
        cache / "manifest.jsonl",
        [
            {
                "markdown_path": "pages/123/clean.md",
                "page_id": "123",
                "path": "pages/123",
                "space_key": "IDENTITY",
                "status": "success",
                "title": "ForgeRock Support Runbook",
                "updated_at": "2026-05-01T12:45:00Z",
                "version": 42,
            }
        ],
    )
    write_json(
        cache / "manifest.summary.json",
        {
            "spaces": {"IDENTITY": 1},
            "status": "complete",
            "statuses": {"success": 1},
            "total_pages": 1,
        },
    )
    write_json(
        page / "metadata.json",
        {
            "ancestors": [{"id": "1", "title": "Identity"}, {"id": "2", "title": "Runbooks"}],
            "author": {"display_name": "Jane Smith", "username": "123456"},
            "content_hashes": {
                "storage_sha256": "a" * 64,
                "export_view_sha256": "b" * 64,
                "markdown_sha256": "c" * 64,
                "text_sha256": "d" * 64,
            },
            "conversion_status": "success",
            "created_at": "2025-01-01T00:00:00Z",
            "download_status": "success",
            "labels": ["runbook", "identity", "production"],
            "page_id": "123",
            "space_key": "IDENTITY",
            "space_name": "Identity",
            "status": "current",
            "title": "ForgeRock Support Runbook",
            "url": "https://confluence.example.com/pages/viewpage.action?pageId=123",
            "version": 42,
            "updated_at": "2026-05-01T12:45:00Z",
            "retrieved_at": "2026-06-16T09:00:00Z",
        },
    )
    write_json(
        page / "links.json",
        {
            "page_id": "123",
            "links": [
                {
                    "type": "external_url",
                    "href": "https://jira.example.com/browse/ABC-123",
                    "text": "ABC-123",
                    "crawlable": False,
                    "target_page_id": None,
                    "target_space_key": None,
                    "target_title": None,
                }
            ],
        },
    )
    write_json(
        page / "conversion.json",
        {
            "converter": "mimir_confluence.converter",
            "converter_version": "0.1.0",
            "converted_at": "2026-06-16T12:00:48Z",
            "markdown_sha256": "c" * 64,
            "text_sha256": "d" * 64,
            "warnings": [],
        },
    )
    (page / "attachments").mkdir(parents=True)
    (page / "clean.md").write_text(
        """---
source: confluence
page_id: "123"
---

# ForgeRock Support Runbook

Owner: Identity SRE

Support group: Identity L3

## Diagnostic Checks

Check Splunk dashboard and LDAP health.

## Recovery Steps

Restart ForgeRock after confirming dependency status.

## Validation Steps

Validate login success in production.
""",
        encoding="utf-8",
    )
    text = (
        "ForgeRock Support Runbook Owner Identity SRE Support group Identity L3 "
        "Diagnostic Checks Splunk dashboard LDAP Recovery Steps Restart ForgeRock "
        "Validation Steps production"
    )
    (page / "text.txt").write_text(text, encoding="utf-8")
    (page / "raw_storage.html").write_text("<p>raw</p>", encoding="utf-8")
    (page / "raw_export_view.html").write_text("<p>raw</p>", encoding="utf-8")
    return cache
