from __future__ import annotations

import json
from pathlib import Path

from mimir_wiki.cache_reader import CacheReader


def test_validate_cache_succeeds_with_observed_schema(tiny_cache: Path) -> None:
    result = CacheReader(tiny_cache).validate()
    assert result.ok
    assert result.dataset_name == "tiny"
    assert result.pages_valid == 1
    assert result.pages_failed == 0


def test_validate_cache_fails_for_missing_markdown(tiny_cache: Path) -> None:
    (tiny_cache / "pages" / "123" / "clean.md").unlink()
    result = CacheReader(tiny_cache).validate()
    assert not result.ok
    assert any(issue.code == "markdown_missing" for issue in result.issues)


def test_validate_cache_surfaces_errors_jsonl(tiny_cache: Path) -> None:
    error_row = (
        '{"operation":"download","page_id":"123","error":"Invalid IPv6 URL",'
        '"timestamp":"2026-06-17T00:00:00Z"}\n'
    )
    (tiny_cache / "errors.jsonl").write_text(
        error_row,
        encoding="utf-8",
    )
    result = CacheReader(tiny_cache).validate()
    assert result.ok
    assert result.export_errors == 1
    assert any(issue.code == "export_errors_present" for issue in result.issues)


def test_validate_cache_surfaces_conversion_warnings(tiny_cache: Path) -> None:
    conversion_path = tiny_cache / "pages" / "123" / "conversion.json"
    conversion = json.loads(conversion_path.read_text(encoding="utf-8"))
    conversion["warnings"] = ["table conversion fallback used"]
    conversion_path.write_text(json.dumps(conversion), encoding="utf-8")
    result = CacheReader(tiny_cache).validate()
    assert result.ok
    assert any(issue.code == "conversion_warnings_present" for issue in result.issues)


def test_validate_cache_allows_missing_optional_metadata_fields(tiny_cache: Path) -> None:
    metadata_path = tiny_cache / "pages" / "123" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("author")
    metadata.pop("labels")
    metadata["ancestors"] = []
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    result = CacheReader(tiny_cache).validate()
    assert result.ok


def test_reader_ignores_cache_artifacts_as_attachments(tiny_cache: Path) -> None:
    attachments = tiny_cache / "pages" / "123" / "attachments"
    (attachments / "manifest.jsonl").write_text("{}\n", encoding="utf-8")
    (attachments / "diagram.png").write_bytes(b"fake")

    bundle = CacheReader(tiny_cache).iter_pages()[0]

    assert bundle.attachment_names == ["diagram.png"]
    assert bundle.attachment_reference_names == ["diagram.png"]
