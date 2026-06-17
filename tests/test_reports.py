from __future__ import annotations

from pathlib import Path

from mimir_wiki.reports import write_duplicate_candidates_report
from mimir_wiki.schemas import DocumentIndexRow


def _row(page_id: str, title: str, text_simhash: str, heading_simhash: str) -> DocumentIndexRow:
    return DocumentIndexRow(
        run_id="run-1",
        dataset_name="tiny",
        generated_at="2026-06-17T00:00:00Z",
        document_id=f"confluence:SPACE:{page_id}",
        page_id=page_id,
        space_key="SPACE",
        source_content_hash=f"sha256:{page_id}",
        title=title,
        document_type="runbook",
        document_type_confidence=0.8,
        text_simhash=text_simhash,
        heading_simhash=heading_simhash,
        heading_count=3,
    )


def test_duplicate_report_includes_body_and_heading_simhash(tmp_path: Path) -> None:
    path = write_duplicate_candidates_report(
        out_dir=tmp_path,
        document_rows=[
            _row("1", "ForgeRock Support Runbook", "0000000000000000", "000000000000000f"),
            _row("2", "ForgeRock Support Guide", "0000000000000001", "000000000000000e"),
        ],
    )
    content = path.read_text(encoding="utf-8")
    assert "body_simhash" in content
    assert "heading_simhash" in content
