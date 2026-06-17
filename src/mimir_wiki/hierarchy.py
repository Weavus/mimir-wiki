from __future__ import annotations

from collections import Counter

from mimir_wiki.cache_reader import PageBundle
from mimir_wiki.schemas import HierarchyContext, Quality

CONTEXT_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("runbook", ("runbook", "operations", "operational", "procedure", "troubleshoot")),
    ("support_model", ("support model", "support", "escalation")),
    ("architecture", ("architecture", "technical design", "hld", "lld")),
    ("release", ("release", "change", "deployment")),
    ("performance", ("performance", "load test", "stress test", "k6")),
    ("business", ("business", "product document")),
    ("reference", ("reference", "resources", "glossary", "faq")),
)


def context_type_for_title(title: str | None) -> str | None:
    if not title:
        return None
    lowered = title.lower()
    for context_type, markers in CONTEXT_HINTS:
        if any(marker in lowered for marker in markers):
            return context_type
    return None


def infer_page_role(
    bundle: PageBundle,
    *,
    child_count: int,
    parent_context_type: str | None,
    ancestor_context_types: list[str],
) -> str:
    title = bundle.metadata.title.lower()
    if child_count > 0 and any(marker in title for marker in ("index", "summary", "overview")):
        return "index_page"
    if child_count > 0 and context_type_for_title(title) == "runbook":
        return "runbook_index"
    if any(marker in title for marker in ("rollback", "failover", "procedure")):
        return "procedure_page"
    if any(marker in title for marker in ("release", "change")):
        return "release_note"
    if any(marker in title for marker in ("performance", "load test", "stress test", "k6")):
        return "test_report"
    if parent_context_type == "runbook" or "runbook" in ancestor_context_types:
        return "runbook_detail"
    if parent_context_type in {"architecture", "business", "reference"}:
        return "reference_detail"
    if child_count > 0:
        return "index_page"
    return "leaf_page"


def build_tree_counts(pages: list[PageBundle]) -> tuple[dict[str, int], dict[str, int]]:
    child_counts: Counter[str] = Counter()
    sibling_group_counts: Counter[str] = Counter()
    page_ids = {page.metadata.page_id for page in pages}
    for page in pages:
        parent_id = page.metadata.ancestors[-1].id if page.metadata.ancestors else "__root__"
        sibling_group_counts[parent_id or "__root__"] += 1
        if parent_id and parent_id in page_ids:
            child_counts[parent_id] += 1
    return dict(child_counts), dict(sibling_group_counts)


def build_hierarchy_context(
    bundle: PageBundle,
    *,
    child_count: int = 0,
    sibling_count: int = 0,
) -> HierarchyContext:
    ancestor_titles = bundle.ancestor_titles
    root_title = ancestor_titles[0] if ancestor_titles else bundle.metadata.title
    parent_title = ancestor_titles[-1] if ancestor_titles else None
    section_parts = [*ancestor_titles, bundle.metadata.title]
    section_path = " > ".join(part for part in section_parts if part)
    ancestor_context_types = [
        context_type
        for context_type in (context_type_for_title(title) for title in ancestor_titles)
        if context_type is not None
    ]
    parent_context_type = context_type_for_title(parent_title)
    page_role = infer_page_role(
        bundle,
        child_count=child_count,
        parent_context_type=parent_context_type,
        ancestor_context_types=ancestor_context_types,
    )
    depth = len(ancestor_titles)
    return HierarchyContext(
        ancestor_titles=ancestor_titles,
        depth=depth,
        root_title=root_title,
        parent_title=parent_title,
        section_path=section_path,
        is_root_page=depth == 0,
        is_index_page=page_role in {"index_page", "runbook_index"},
        is_leaf_page=child_count == 0,
        sibling_count=max(0, sibling_count - 1),
        child_count=child_count,
        page_role=page_role,
        parent_context_type=parent_context_type,
        ancestor_context_types=sorted(set(ancestor_context_types)),
    )


def adjust_quality_for_hierarchy(quality: Quality, hierarchy: HierarchyContext) -> Quality:
    data = quality.model_dump(mode="python")
    context_bonus = 0
    if hierarchy.parent_context_type in {"runbook", "support_model", "architecture"}:
        context_bonus += 5
    if hierarchy.page_role in {"runbook_detail", "procedure_page"}:
        context_bonus += 5
    if hierarchy.is_index_page and quality.completeness_score < 50:
        data["completeness_score"] = max(0, data["completeness_score"] - 5)
    if context_bonus:
        data["authority_score"] = min(100, data["authority_score"] + context_bonus)
        data["operational_value_score"] = min(100, data["operational_value_score"] + context_bonus)
        data["overall_score"] = min(100, data["overall_score"] + round(context_bonus * 0.4))
    return Quality.model_validate(data)
