from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mimir_wiki.schemas import (
    Conversion,
    Dataset,
    ExportError,
    LinksFile,
    ManifestRow,
    ManifestSummary,
    PageMetadata,
)
from mimir_wiki.utils import load_json, load_jsonl, read_text, source_hash_from_metadata


@dataclass(frozen=True)
class PagePaths:
    root: Path
    metadata: Path
    clean_md: Path
    text_txt: Path
    links: Path
    conversion: Path
    attachments: Path


@dataclass(frozen=True)
class PageBundle:
    manifest: ManifestRow
    metadata: PageMetadata
    links: LinksFile
    conversion: Conversion
    clean_markdown: str
    text: str
    paths: PagePaths

    @property
    def document_id(self) -> str:
        return f"confluence:{self.metadata.space_key}:{self.metadata.page_id}"

    @property
    def source_content_hash(self) -> str:
        return source_hash_from_metadata(self.metadata.content_hashes.as_source_mapping())

    @property
    def attachment_names(self) -> list[str]:
        if not self.paths.attachments.exists():
            return []
        return sorted(item.name for item in self.paths.attachments.iterdir() if item.is_file())

    @property
    def attachment_link_names(self) -> list[str]:
        names: list[str] = []
        for link in self.links.links:
            href = link.href or ""
            if link.type != "confluence_attachment" and "/download/attachments/" not in href:
                continue
            name = (link.text or href.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]).strip()
            if name:
                names.append(name)
        return sorted(set(names))

    @property
    def missing_attachment_names(self) -> list[str]:
        exported = set(self.attachment_names)
        return [name for name in self.attachment_link_names if name not in exported]

    @property
    def attachment_reference_names(self) -> list[str]:
        return sorted(set(self.attachment_names) | set(self.attachment_link_names))

    @property
    def attachment_reference_count(self) -> int:
        return len(self.attachment_reference_names)

    @property
    def ancestor_titles(self) -> list[str]:
        return [ancestor.title for ancestor in self.metadata.ancestors]


@dataclass(frozen=True)
class ValidationIssue:
    level: str
    code: str
    message: str
    path: str | None = None
    page_id: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    cache_path: Path
    dataset_name: str | None
    pages_total: int
    pages_valid: int
    pages_failed: int
    export_errors: int
    issues: list[ValidationIssue]
    dataset: Dataset | None = None
    summary: ManifestSummary | None = None

    @property
    def ok(self) -> bool:
        return not any(issue.level == "error" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cache_path": str(self.cache_path),
            "dataset_name": self.dataset_name,
            "pages_total": self.pages_total,
            "pages_valid": self.pages_valid,
            "pages_failed": self.pages_failed,
            "export_errors": self.export_errors,
            "ok": self.ok,
            "issues": [issue.__dict__ for issue in self.issues],
        }


class CacheReader:
    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path

    @property
    def dataset_path(self) -> Path:
        return self.cache_path / "dataset.json"

    @property
    def manifest_path(self) -> Path:
        return self.cache_path / "manifest.jsonl"

    @property
    def summary_path(self) -> Path:
        return self.cache_path / "manifest.summary.json"

    @property
    def errors_path(self) -> Path:
        return self.cache_path / "errors.jsonl"

    def load_dataset(self) -> Dataset:
        return Dataset.model_validate(load_json(self.dataset_path))

    def load_manifest(self) -> list[ManifestRow]:
        return [ManifestRow.model_validate(row) for row in load_jsonl(self.manifest_path)]

    def load_summary(self) -> ManifestSummary:
        return ManifestSummary.model_validate(load_json(self.summary_path))

    def load_export_errors(self) -> list[ExportError]:
        if not self.errors_path.exists():
            return []
        return [ExportError.model_validate(row) for row in load_jsonl(self.errors_path)]

    def page_paths(self, manifest: ManifestRow) -> PagePaths:
        root = self.cache_path / manifest.path
        return PagePaths(
            root=root,
            metadata=root / "metadata.json",
            clean_md=self.cache_path / manifest.markdown_path,
            text_txt=root / "text.txt",
            links=root / "links.json",
            conversion=root / "conversion.json",
            attachments=root / "attachments",
        )

    def load_page(self, manifest: ManifestRow) -> PageBundle:
        paths = self.page_paths(manifest)
        metadata = PageMetadata.model_validate(load_json(paths.metadata))
        links = LinksFile.model_validate(load_json(paths.links))
        conversion = Conversion.model_validate(load_json(paths.conversion))
        return PageBundle(
            manifest=manifest,
            metadata=metadata,
            links=links,
            conversion=conversion,
            clean_markdown=read_text(paths.clean_md),
            text=read_text(paths.text_txt),
            paths=paths,
        )

    def iter_pages(
        self,
        *,
        limit: int | None = None,
        space_filter: str | None = None,
    ) -> list[PageBundle]:
        pages: list[PageBundle] = []
        for manifest in self.load_manifest():
            if manifest.status != "success":
                continue
            if space_filter and manifest.space_key != space_filter:
                continue
            pages.append(self.load_page(manifest))
            if limit is not None and len(pages) >= limit:
                break
        return pages

    def validate(self, *, limit: int | None = None) -> ValidationResult:
        issues: list[ValidationIssue] = []
        dataset: Dataset | None = None
        summary: ManifestSummary | None = None
        manifest_rows: list[ManifestRow] = []

        if not self.cache_path.exists():
            issues.append(
                ValidationIssue(
                    "error", "cache_missing", "Cache path does not exist", str(self.cache_path)
                )
            )
            return ValidationResult(self.cache_path, None, 0, 0, 0, 0, issues)

        for path, code in (
            (self.dataset_path, "dataset_missing"),
            (self.manifest_path, "manifest_missing"),
            (self.summary_path, "manifest_summary_missing"),
        ):
            if not path.exists():
                issues.append(
                    ValidationIssue("error", code, f"Required file missing: {path}", str(path))
                )

        if self.dataset_path.exists():
            try:
                dataset = self.load_dataset()
            except (ValidationError, OSError, ValueError) as exc:
                issues.append(
                    ValidationIssue(
                        "error",
                        "dataset_invalid",
                        f"dataset.json is invalid: {exc}",
                        str(self.dataset_path),
                    )
                )

        if self.summary_path.exists():
            try:
                summary = self.load_summary()
            except (ValidationError, OSError, ValueError) as exc:
                issues.append(
                    ValidationIssue(
                        "error",
                        "manifest_summary_invalid",
                        f"manifest.summary.json is invalid: {exc}",
                        str(self.summary_path),
                    )
                )

        if self.manifest_path.exists():
            try:
                manifest_rows = self.load_manifest()
            except (ValidationError, OSError, ValueError) as exc:
                issues.append(
                    ValidationIssue(
                        "error",
                        "manifest_invalid",
                        f"manifest.jsonl is invalid: {exc}",
                        str(self.manifest_path),
                    )
                )

        export_errors = 0
        if self.errors_path.exists():
            try:
                export_errors = len(self.load_export_errors())
                if export_errors:
                    issues.append(
                        ValidationIssue(
                            "warning",
                            "export_errors_present",
                            f"errors.jsonl contains {export_errors} exporter error(s)",
                            str(self.errors_path),
                        )
                    )
            except (ValidationError, OSError, ValueError) as exc:
                issues.append(
                    ValidationIssue(
                        "error",
                        "export_errors_invalid",
                        f"errors.jsonl is invalid: {exc}",
                        str(self.errors_path),
                    )
                )

        pages_valid = 0
        pages_failed = 0
        for row in manifest_rows[:limit]:
            if row.status != "success":
                continue
            page_issues_before = len([issue for issue in issues if issue.level == "error"])
            paths = self.page_paths(row)
            for path, code in (
                (paths.root, "page_folder_missing"),
                (paths.metadata, "metadata_missing"),
                (paths.clean_md, "markdown_missing"),
                (paths.text_txt, "text_missing"),
                (paths.links, "links_missing"),
                (paths.conversion, "conversion_missing"),
            ):
                if not path.exists():
                    issues.append(
                        ValidationIssue(
                            "error",
                            code,
                            f"Required page artifact missing: {path}",
                            str(path),
                            row.page_id,
                        )
                    )
            if paths.metadata.exists():
                try:
                    metadata = PageMetadata.model_validate(load_json(paths.metadata))
                    if metadata.page_id != row.page_id:
                        issues.append(
                            ValidationIssue(
                                "error",
                                "page_id_mismatch",
                                (
                                    f"metadata page_id {metadata.page_id} does not match "
                                    f"manifest {row.page_id}"
                                ),
                                str(paths.metadata),
                                row.page_id,
                            )
                        )
                    if not metadata.content_hashes.markdown_sha256:
                        issues.append(
                            ValidationIssue(
                                "warning",
                                "markdown_hash_missing",
                                "metadata content_hashes.markdown_sha256 is missing",
                                str(paths.metadata),
                                row.page_id,
                            )
                        )
                except (ValidationError, OSError, ValueError) as exc:
                    issues.append(
                        ValidationIssue(
                            "error",
                            "metadata_invalid",
                            f"metadata.json is invalid: {exc}",
                            str(paths.metadata),
                            row.page_id,
                        )
                    )
            for path, model, code in (
                (paths.links, LinksFile, "links_invalid"),
                (paths.conversion, Conversion, "conversion_invalid"),
            ):
                if path.exists():
                    try:
                        parsed_artifact = model.model_validate(load_json(path))
                        if isinstance(parsed_artifact, Conversion) and parsed_artifact.warnings:
                            issues.append(
                                ValidationIssue(
                                    "warning",
                                    "conversion_warnings_present",
                                    (
                                        f"conversion.json contains "
                                        f"{len(parsed_artifact.warnings)} warning(s)"
                                    ),
                                    str(path),
                                    row.page_id,
                                )
                            )
                    except (ValidationError, OSError, ValueError) as exc:
                        issues.append(
                            ValidationIssue(
                                "error",
                                code,
                                f"{path.name} is invalid: {exc}",
                                str(path),
                                row.page_id,
                            )
                        )
            page_issues_after = len([issue for issue in issues if issue.level == "error"])
            if page_issues_after == page_issues_before:
                pages_valid += 1
            else:
                pages_failed += 1

        dataset_name = dataset.dataset_name if dataset else None
        return ValidationResult(
            cache_path=self.cache_path,
            dataset_name=dataset_name,
            pages_total=len(manifest_rows),
            pages_valid=pages_valid,
            pages_failed=pages_failed,
            export_errors=export_errors,
            issues=issues,
            dataset=dataset,
            summary=summary,
        )
