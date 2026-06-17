from __future__ import annotations

from mimir_wiki.schemas import Conversion, LinksFile, ManifestRow, PageMetadata


def test_observed_cache_shapes_from_sample_exports_validate() -> None:
    manifest = ManifestRow.model_validate(
        {
            "markdown_path": "pages/123456789/clean.md",
            "page_id": "123456789",
            "path": "pages/123456789",
            "space_key": "CIAME",
            "status": "success",
            "title": "Sample Support Runbook",
            "updated_at": "2026-05-01T12:45:00Z",
            "version": 42,
        }
    )
    metadata = PageMetadata.model_validate(
        {
            "ancestors": [{"id": "111", "title": "Identity"}],
            "author": {"display_name": "Jane Smith", "username": "123456"},
            "content_hashes": {
                "storage_sha256": "a" * 64,
                "export_view_sha256": "b" * 64,
                "markdown_sha256": "c" * 64,
                "text_sha256": "d" * 64,
            },
            "labels": ["runbook", "identity"],
            "page_id": "123456789",
            "space_key": "CIAME",
            "space_name": "Identity",
            "status": "current",
            "title": "Sample Support Runbook",
            "url": "https://confluence.example.com/pages/viewpage.action?pageId=123456789",
            "version": 42,
            "updated_at": "2026-05-01T12:45:00Z",
        }
    )
    links = LinksFile.model_validate(
        {
            "page_id": "123456789",
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
        }
    )
    conversion = Conversion.model_validate(
        {
            "converter": "mimir_confluence.converter",
            "converter_version": "0.1.0",
            "converted_at": "2026-06-16T12:00:48Z",
            "markdown_sha256": "c" * 64,
            "text_sha256": "d" * 64,
            "warnings": [],
        }
    )
    assert manifest.page_id == metadata.page_id == links.page_id
    assert conversion.warnings == []
