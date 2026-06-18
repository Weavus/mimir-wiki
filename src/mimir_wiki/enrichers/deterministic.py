from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime

from mimir_wiki.cache_reader import PageBundle
from mimir_wiki.config import AppConfig
from mimir_wiki.hierarchy import adjust_quality_for_hierarchy, build_hierarchy_context
from mimir_wiki.schemas import (
    CandidateEntity,
    CandidateFact,
    CandidateMention,
    Enrichment,
    EnrichmentSignature,
    HierarchyContext,
    OnyxMetadata,
    OperationalSignals,
)
from mimir_wiki.scoring import build_quality, currentness, quality_band
from mimir_wiki.utils import (
    extract_headings,
    normalize_term,
    strip_front_matter,
    word_count,
)

STOPWORDS = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "both",
    "but",
    "can",
    "confluence",
    "for",
    "from",
    "has",
    "have",
    "how",
    "into",
    "its",
    "not",
    "of",
    "on",
    "or",
    "page",
    "please",
    "should",
    "source",
    "that",
    "the",
    "this",
    "to",
    "with",
    "you",
}

GENERIC_ENTITY_TERMS = {
    "access",
    "analysis",
    "api",
    "business",
    "business document",
    "business documents",
    "capacity",
    "cases",
    "document",
    "documents",
    "performance",
    "provisioning",
    "run",
    "test",
    "use",
}

GENERIC_TAXONOMY_TERMS = GENERIC_ENTITY_TERMS | {
    "admin",
    "application",
    "api",
    "architecture",
    "asset insight",
    "change",
    "checklist",
    "ciame",
    "complete",
    "configuration",
    "confluence",
    "confluence ciame",
    "confluence ciame documentation",
    "confluence refinitiv com pages viewpage action",
    "customer identity and access management",
    "customer identity and access management entra",
    "deployment",
    "deployments",
    "details",
    "database",
    "design",
    "document",
    "documentation",
    "documents",
    "failed",
    "for individual users",
    "governance",
    "heavy",
    "guide",
    "here",
    "https",
    "iam",
    "information",
    "installation",
    "linking",
    "load",
    "open",
    "operational",
    "overview",
    "pageid",
    "pre",
    "pre requisite steps",
    "previous",
    "question",
    "reference",
    "refinitiv",
    "region",
    "release schedule",
    "response",
    "result",
    "runbook",
    "scim",
    "service",
    "summary",
    "technical",
    "technical document",
    "test",
    "testing",
    "user",
    "verify",
}
SHORT_TAXONOMY_ALLOWLIST = {"dev", "qa", "ppe", "prod", "k6", "uat", "dr"}
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
INTERNAL_EMAIL_DOMAINS = (
    "lseg.com",
    "refinitiv.com",
    "thomsonreuters.com",
)
MONTH_NAMES = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
HIGH_VALUE_ATTACHMENT_SUFFIXES = {
    ".bat",
    ".csv",
    ".json",
    ".pdf",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".yaml",
    ".yml",
    ".zip",
}

DOCUMENT_TYPE_RULES: list[tuple[str, list[str]]] = [
    ("archive", ["archive", "archived", "obsolete", "deprecated", "retired"]),
    ("rca", ["rca", "root cause", "postmortem", "post-mortem", "incident review"]),
    ("incident", ["incident", "outage", "sev1", "sev2", "major incident", "pir"]),
    ("known_error", ["known error", "known issue", "workaround", "error code"]),
    (
        "runbook",
        ["runbook", "troubleshoot", "troubleshooting", "recovery", "how to", "restart", "restore"],
    ),
    (
        "support_model",
        ["support model", "support process", "escalation", "assignment group", "support group"],
    ),
    ("architecture", ["architecture", "component", "data flow", "interface", "diagram"]),
    ("design", ["design", "proposal", "solution", "low level design", "high level design"]),
    ("migration", ["migration", "migrate", "cutover", "decommission"]),
    ("onboarding", ["onboarding", "getting started", "setup", "access request"]),
    ("meeting_notes", ["meeting notes", "minutes", "agenda", "standup"]),
    ("project_plan", ["project plan", "milestone", "roadmap", "delivery plan"]),
    ("change_record", ["change record", "change request", "cab", "implementation plan"]),
    ("knowledge_article", ["knowledge article", "faq", "guide", "procedure"]),
    ("reference", ["reference", "glossary", "inventory", "list of"]),
]

OPERATIONAL_PATTERNS: dict[str, list[str]] = {
    "has_owner": ["owner", "owned by", "service owner", "application owner", "sme"],
    "has_support_group": ["support group", "assignment group", "support team", "resolver group"],
    "has_escalation_path": ["escalat", "on call", "contact", "pager", "callout"],
    "has_runbook_steps": ["runbook", "steps", "procedure", "how to"],
    "has_diagnostic_steps": ["diagnos", "check", "verify", "investigate", "triage"],
    "has_recovery_steps": ["recover", "restart", "restore", "rollback", "fix", "mitigate"],
    "has_validation_steps": ["validate", "validation", "confirm", "test after"],
    "has_backout_steps": ["backout", "back out", "rollback", "roll back"],
    "has_monitoring_links": ["monitor", "dashboard", "grafana", "splunk", "alert"],
    "has_dependencies": ["depend", "upstream", "downstream", "database", "queue", "api"],
    "has_known_errors": ["known error", "known issue", "symptom", "error code"],
    "has_architecture_description": ["architecture", "component", "data flow", "diagram"],
    "has_environment_details": ["production", "uat", "dev", "environment", "region"],
    "has_impact_summary": ["impact", "affected", "customer impact", "business impact"],
    "has_incident_timeline": ["timeline", "detected", "resolved", "mitigated"],
    "has_root_cause": ["root cause", "cause", "caused by"],
    "has_contributing_factors": ["contributing factor", "contributed", "factor"],
    "has_detection_gap": ["detection gap", "not detected", "alert gap"],
    "has_monitoring_gap": ["monitoring gap", "missing alert", "no alert"],
    "has_runbook_gap": ["runbook gap", "missing runbook", "documentation gap"],
    "has_corrective_actions": ["corrective action", "remediation", "action item", "fix forward"],
    "has_preventive_actions": ["preventive action", "prevention", "prevent recurrence"],
    "has_action_owners": ["action owner", "assigned to", "owner:"],
    "has_due_dates": ["due date", "target date", "by ", "eta"],
    "links_to_incident_or_change": [
        "incident",
        "change",
        "jira",
        "servicenow",
        "snow",
        "chg",
        "inc",
    ],
}

ENTITY_TYPE_HINTS: dict[str, list[str]] = {
    "environment": ["production", "prod", "uat", "dev", "test", "staging", "dr"],
    "database": [
        "oracle",
        "postgres",
        "postgresql",
        "mysql",
        "mssql",
        "sql server",
        "sybase",
        "db2",
    ],
    "queue": ["kafka", "rabbitmq", "mq", "sqs", "queue"],
    "dashboard": ["grafana", "splunk", "kibana", "datadog", "dashboard"],
    "technology": ["aws", "azure", "kubernetes", "linux", "windows", "tomcat", "websphere"],
}


def classify_document(bundle: PageBundle) -> tuple[str, float]:
    haystack = " ".join(
        [
            bundle.metadata.title,
            " ".join(bundle.metadata.labels),
            " ".join(bundle.ancestor_titles),
            bundle.text[:5000],
        ]
    ).lower()
    scores: Counter[str] = Counter()
    for document_type, terms in DOCUMENT_TYPE_RULES:
        for term in terms:
            if term in haystack:
                scores[document_type] += 2 if term in bundle.metadata.title.lower() else 1
    if scores:
        document_type, score = scores.most_common(1)[0]
        return document_type, min(0.95, 0.55 + score * 0.08)
    if len(bundle.text.split()) < 80:
        return "reference", 0.45
    return "unknown", 0.35


def status_flags(bundle: PageBundle, document_type: str, updated_at: str | None) -> list[str]:
    text = " ".join(
        [bundle.metadata.title, " ".join(bundle.metadata.labels), bundle.metadata.status or ""]
    ).lower()
    flags: list[str] = []
    if bundle.metadata.status:
        flags.append(bundle.metadata.status)
    if document_type == "archive" or any(term in text for term in ("archive", "archived")):
        flags.append("archived")
    if any(term in text for term in ("deprecated", "obsolete", "retired")):
        flags.append("deprecated")
    historical, current = currentness(document_type, flags, updated_at)
    if historical and "historical" not in flags:
        flags.append("historical")
    if current not in flags:
        flags.append(current)
    return sorted(set(flags))


def extract_keywords(bundle: PageBundle, headings: list[str], max_terms: int = 18) -> list[str]:
    terms: Counter[str] = Counter()
    sources = [bundle.metadata.title, " ".join(bundle.metadata.labels), " ".join(headings[:20])]
    sources.extend(link.text or "" for link in bundle.links.links[:50])
    text = "\n".join(sources)
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9_+./-]{2,}", text):
        normalized = normalize_term(raw)
        if not normalized or normalized in STOPWORDS or len(normalized) < 3:
            continue
        terms[normalized] += 1
    for phrase in re.findall(r"\b[A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,3}\b", text):
        normalized = normalize_term(phrase)
        if normalized and normalized not in STOPWORDS:
            terms[normalized] += 2
    return filter_taxonomy_terms([term for term, _ in terms.most_common(max_terms * 2)])[:max_terms]


def clean_taxonomy_term(value: str) -> str:
    normalized = normalize_term(value)
    normalized = re.sub(r"^(?:\d+\s+){2,}", "", normalized).strip()
    normalized = re.sub(r"^\d+\s+(?=[a-z])", "", normalized).strip()
    return normalized


def is_noisy_taxonomy_term(value: str) -> bool:
    words = value.split()
    if value in GENERIC_TAXONOMY_TERMS:
        return True
    if value.startswith(("http ", "https ")) or " http " in f" {value} ":
        return True
    if any(fragment in value for fragment in ("viewpage action", "pageid", "confluence refinitiv")):
        return True
    numeric_tokens = sum(1 for word in words if word.isdigit())
    if numeric_tokens >= 3 and len(words) >= 6:
        return True
    return len(words) == 1 and value not in SHORT_TAXONOMY_ALLOWLIST and len(value) < 5


def filter_taxonomy_terms(values: list[str], *, max_terms: int | None = None) -> list[str]:
    filtered: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = clean_taxonomy_term(value)
        if not cleaned or cleaned in seen:
            continue
        if is_noisy_taxonomy_term(cleaned):
            continue
        if len(cleaned) < 4 and cleaned not in SHORT_TAXONOMY_ALLOWLIST:
            continue
        seen.add(cleaned)
        filtered.append(cleaned)
        if max_terms is not None and len(filtered) >= max_terms:
            break
    return filtered


def detect_operational_signals(bundle: PageBundle) -> OperationalSignals:
    haystack = " ".join(
        [
            bundle.metadata.title,
            bundle.text,
            " ".join(link.href or "" for link in bundle.links.links),
        ]
    ).lower()
    values = {
        signal: any(pattern in haystack for pattern in patterns)
        for signal, patterns in OPERATIONAL_PATTERNS.items()
    }
    return OperationalSignals.model_validate(values)


def infer_entity_type(name: str, source_field: str) -> str:
    normalized = normalize_term(name)
    for entity_type, hints in ENTITY_TYPE_HINTS.items():
        if any(hint in normalized for hint in hints):
            return entity_type
    if re.search(r"\b[A-Z]{2,}[A-Z0-9-]*\b", name) and source_field == "title":
        return "application"
    if any(marker in normalized for marker in ("team", "support", "sre", "l2", "l3")):
        return "support_group"
    if re.search(r"\b(INC|CHG|RITM|DOC)-?\d+\b", name, flags=re.IGNORECASE):
        return "incident" if "inc" in normalized else "change_record"
    return "technology"


def extract_candidate_entities(bundle: PageBundle, keywords: list[str]) -> list[CandidateEntity]:
    mentions: dict[tuple[str, str], CandidateEntity] = {}
    title_parts = re.findall(
        r"[A-Z][A-Za-z0-9+.-]{2,}(?:\s+[A-Z][A-Za-z0-9+.-]{2,}){0,3}", bundle.metadata.title
    )
    candidates = [(part.strip(), "title") for part in title_parts]
    candidates.extend((label, "label") for label in bundle.metadata.labels)
    candidates.extend((keyword, "keyword") for keyword in keywords[:10])
    for link in bundle.links.links:
        if link.target_title:
            candidates.append((link.target_title, "link_target_title"))
        elif link.text and len(link.text) <= 80:
            candidates.append((link.text, "link_text"))
    for raw_name, source_field in candidates:
        name = re.sub(r"\s+", " ", raw_name).strip(" -_#")
        normalized = normalize_term(name)
        if not normalized or normalized in STOPWORDS or len(normalized) < 3:
            continue
        if normalized in GENERIC_ENTITY_TERMS:
            continue
        if source_field == "keyword" and len(normalized.split()) == 1:
            continue
        entity_type = infer_entity_type(name, source_field)
        key = (entity_type, normalized)
        mention = CandidateMention(
            document_id=bundle.document_id,
            page_id=bundle.metadata.page_id,
            evidence=name[:240],
            source_field=source_field,
        )
        if key not in mentions:
            mentions[key] = CandidateEntity(
                name=name[:120],
                normalized_name=normalized,
                entity_type=entity_type,
                mentions=[mention],
                confidence=0.75 if source_field == "title" else 0.6,
            )
        else:
            mentions[key].mentions.append(mention)
    return sorted(mentions.values(), key=lambda item: (item.entity_type, item.normalized_name))[:40]


def extract_candidate_facts(
    bundle: PageBundle, entities: list[CandidateEntity]
) -> list[CandidateFact]:
    facts: list[CandidateFact] = []
    subject = entities[0].name if entities else bundle.metadata.title

    def add_fact(predicate: str, value: str, confidence: float, evidence: str) -> None:
        cleaned = re.sub(r"\s+", " ", value).strip(" :-|.;")[:160]
        if not cleaned or cleaned.lower() in {"none", "n/a", "unknown", "tbd"}:
            return
        key = (predicate, normalize_term(cleaned), evidence[:120])
        existing = {
            (fact.predicate, normalize_term(fact.object), fact.evidence_text[:120])
            for fact in facts
        }
        if key in existing:
            return
        facts.append(
            CandidateFact(
                subject=subject,
                predicate=predicate,
                object=cleaned,
                confidence=confidence,
                evidence_text=evidence[:500],
                source_document_id=bundle.document_id,
            )
        )

    source_text = "\n".join([strip_front_matter(bundle.clean_markdown), bundle.text])
    lines: list[str] = []
    for raw_line in source_text.splitlines():
        stripped = raw_line.strip()
        if stripped:
            lines.append(stripped)
    if len(lines) <= 2:
        lines.extend(re.split(r"(?<=[.;])\s+", bundle.text))

    for line in lines:
        stripped = re.sub(r"\s+", " ", line.strip())
        lowered = stripped.lower()
        if stripped.startswith("#"):
            continue
        if len(stripped) > 300 or len(stripped) < 12:
            continue

        pattern_specs = [
            ("owned_by", r"(?:service |application )?owner\s*[:=-]\s*(.+)$", 0.7),
            ("owned_by", r"\bowned by\s+(.+)$", 0.65),
            ("supported_by", r"support (?:group|team)\s*[:=-]\s*(.+)$", 0.7),
            ("supported_by", r"\bsupported by\s+(.+)$", 0.65),
            ("escalates_to", r"escalat(?:e|ion|es|ed)?(?: to| path)?\s*[:=-]\s*(.+)$", 0.6),
            ("depends_on", r"\bdepends on\s+(.+)$", 0.6),
            ("depends_on", r"dependenc(?:y|ies)\s*[:=-]\s*(.+)$", 0.55),
            ("uses_database", r"database(?:s)?\s*[:=-]\s*(.+)$", 0.55),
            ("uses_queue", r"queue(?:s)?\s*[:=-]\s*(.+)$", 0.55),
            ("uses_api", r"^api(?:s)?\s*[:=]\s*(.+)$", 0.55),
            ("has_dashboard", r"dashboard(?:s)?\s*[:=-]\s*(.+)$", 0.55),
            ("has_log_source", r"logs?(?: source| location)?\s*[:=-]\s*(.+)$", 0.55),
            ("has_alert", r"alerts?\s*[:=-]\s*(.+)$", 0.55),
            ("runs_in_environment", r"environments?\s*[:=-]\s*(.+)$", 0.55),
            ("has_runbook", r"runbook(?:s)?\s*[:=-]\s*(.+)$", 0.55),
            (
                "has_known_failure_mode",
                r"known (?:failure mode|issue|error)s?\s*[:=-]\s*(.+)$",
                0.55,
            ),
            ("related_to_incident", r"\b(?:incident|inc)\s*[:#=-]\s*([A-Z]*-?\d+.+)$", 0.55),
            ("related_to_change", r"\b(?:change|chg)\s*[:#=-]\s*([A-Z]*-?\d+.+)$", 0.55),
            ("had_impact", r"impact\s*[:=-]\s*(.+)$", 0.55),
            ("had_root_cause", r"root cause\s*[:=-]\s*(.+)$", 0.6),
            ("had_contributing_factor", r"contributing factors?\s*[:=-]\s*(.+)$", 0.55),
            ("had_detection_gap", r"detection gap\s*[:=-]\s*(.+)$", 0.55),
            ("had_monitoring_gap", r"monitoring gap\s*[:=-]\s*(.+)$", 0.55),
            ("had_runbook_gap", r"runbook gap\s*[:=-]\s*(.+)$", 0.55),
            ("had_followup_action", r"(?:follow[ -]?up|action item)s?\s*[:=-]\s*(.+)$", 0.55),
        ]
        for predicate, pattern, confidence in pattern_specs:
            match = re.search(pattern, stripped, flags=re.IGNORECASE)
            if match:
                add_fact(predicate, match.group(1), confidence, stripped)

        if "owner" in lowered and "support group" in lowered:
            owner_match = re.search(r"owner\s+(.+?)\s+support group", stripped, flags=re.IGNORECASE)
            support_match = re.search(
                r"support group\s+(.+?)(?:\s+diagnostic|\s+recovery|\s+validation|$)",
                stripped,
                flags=re.IGNORECASE,
            )
            if owner_match:
                add_fact("owned_by", owner_match.group(1), 0.5, stripped)
            if support_match:
                add_fact("supported_by", support_match.group(1), 0.5, stripped)

        step_specs = [
            (
                "has_diagnostic_step",
                (r"\bcheck\b", r"\bverify\b", r"\bdiagnos\w*\b", r"\btriage\b"),
            ),
            (
                "has_recovery_step",
                (r"\brestart\b", r"\brecover\b", r"\brestore\b", r"\bmitigate\b", r"\bfix\b"),
            ),
            ("has_validation_step", (r"\bvalidate\b", r"\bvalidation\b", r"\bconfirm\b")),
            (
                "has_backout_step",
                (r"\brollback\b", r"\broll back\b", r"\bbackout\b", r"\bback out\b"),
            ),
        ]
        for predicate, patterns in step_specs:
            if any(re.search(pattern, lowered) for pattern in patterns):
                add_fact(predicate, stripped, 0.45, stripped)

        if len(facts) >= 20:
            break
    return facts


def summarize(bundle: PageBundle, document_type: str, keywords: list[str]) -> tuple[str, str]:
    clean_text = re.sub(r"\s+", " ", strip_front_matter(bundle.clean_markdown)).strip()
    if clean_text:
        first_sentence = re.split(r"(?<=[.!?])\s+", clean_text, maxsplit=1)[0]
        first_sentence = first_sentence[:260].strip()
    else:
        first_sentence = bundle.metadata.title
    keyword_text = ", ".join(keywords[:8]) if keywords else "no deterministic keywords"
    short = f"{bundle.metadata.title} is classified as {document_type}."
    detailed = f"{first_sentence} Deterministic signals include: {keyword_text}."
    return short, detailed


def categories_for(bundle: PageBundle, document_type: str, keywords: list[str]) -> list[str]:
    values = [document_type, bundle.metadata.space_key]
    values.extend(bundle.metadata.labels[:8])
    values.extend(keywords[:5])
    seen: set[str] = set()
    categories: list[str] = []
    for value in values:
        normalized = normalize_term(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            categories.append(normalized)
    return categories


def themes_for(bundle: PageBundle, document_type: str, keywords: list[str]) -> list[str]:
    values = [document_type]
    values.extend(normalize_term(title) for title in bundle.ancestor_titles[-2:])
    values.extend(keywords[:4])
    return filter_taxonomy_terms([value for value in dict.fromkeys(values) if value])


def concepts_for(headings: list[str], keywords: list[str]) -> list[str]:
    values = [normalize_term(heading) for heading in headings[:8]]
    values.extend(keywords[:6])
    return filter_taxonomy_terms([value for value in dict.fromkeys(values) if value])


def entity_bucket(candidate_entities: list[CandidateEntity]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {
        "applications": [],
        "technologies": [],
        "teams": [],
        "support_groups": [],
        "dashboards": [],
        "databases": [],
        "queues": [],
        "apis": [],
    }
    for entity in candidate_entities:
        if entity.entity_type == "application":
            buckets["applications"].append(entity.name)
        elif entity.entity_type == "support_group":
            buckets["support_groups"].append(entity.name)
        elif entity.entity_type == "dashboard":
            buckets["dashboards"].append(entity.name)
        elif entity.entity_type == "database":
            buckets["databases"].append(entity.name)
        elif entity.entity_type == "queue":
            buckets["queues"].append(entity.name)
        elif entity.entity_type == "api":
            buckets["apis"].append(entity.name)
        else:
            buckets["technologies"].append(entity.name)
    return {key: sorted(set(values)) for key, values in buckets.items()}


def warnings_for(
    *,
    document_type: str,
    quality_score: int,
    signals: OperationalSignals,
    status_flags: list[str],
    attachment_count: int,
    has_linked_procedure: bool = False,
    is_procedural: bool = True,
) -> list[str]:
    warnings: list[str] = []
    if "deprecated" in status_flags or "archived" in status_flags:
        warnings.append("source_is_archived_or_deprecated")
    if quality_score < 50:
        warnings.append("low_quality_score")
    if not signals.has_owner:
        warnings.append("missing_explicit_owner")
    if document_type == "runbook" and is_procedural and not signals.has_validation_steps:
        warnings.append("missing_validation_steps")
    if document_type == "runbook" and is_procedural and not signals.has_backout_steps:
        if has_linked_procedure:
            warnings.append("linked_procedure_not_expanded")
        else:
            warnings.append("missing_backout_steps")
    if attachment_count:
        warnings.append("attachments_present_not_parsed")
    return warnings


def sensitivity_review_flags(bundle: PageBundle) -> list[str]:
    text = "\n".join(
        [
            bundle.metadata.title,
            " ".join(bundle.metadata.labels),
            bundle.text,
            " ".join(link.href or "" for link in bundle.links.links),
        ]
    )
    lowered = text.lower()
    flags: set[str] = set()
    emails = EMAIL_PATTERN.findall(text)
    if emails:
        flags.add("contains_email_addresses")
    if any(not email.lower().endswith(INTERNAL_EMAIL_DOMAINS) for email in emails):
        flags.add("contains_customer_emails")
    if re.search(r"\bGE(?:DTC|US\d*)?-[A-Z0-9]{4,}\b", text):
        flags.add("contains_person_identifiers")
    if any(marker in lowered for marker in ("datadog", "cloudwatch", "log explorer")):
        flags.add("contains_log_links")
    if any(
        marker in lowered
        for marker in (
            "customer case",
            "customer support",
            "bain",
            "kpmg",
            "client",
            "user portfolio",
        )
    ) and (
        "contains_customer_emails" in flags
        or "contains_person_identifiers" in flags
        or "contains_log_links" in flags
    ):
        flags.add("contains_customer_case_data")
    if any(
        flag in flags
        for flag in (
            "contains_customer_emails",
            "contains_person_identifiers",
            "contains_customer_case_data",
        )
    ):
        flags.add("requires_restricted_audience")
    return sorted(flags)


def audience_for_review_flags(review_flags: list[str]) -> str:
    if "requires_restricted_audience" in review_flags:
        return "restricted_internal"
    return "internal"


def sensitivity_for_review_flags(review_flags: list[str]) -> str:
    if "contains_customer_case_data" in review_flags:
        return "customer_confidential"
    if any(
        flag in review_flags
        for flag in ("contains_customer_emails", "contains_person_identifiers")
    ):
        return "restricted"
    return "internal"


def trust_review_flags(bundle: PageBundle, document_type: str, document_subtype: str | None) -> list[str]:
    title = bundle.metadata.title.lower()
    labels = " ".join(bundle.metadata.labels).lower()
    early_text = bundle.text[:5000].lower()
    haystack = "\n".join([title, labels, early_text])
    flags: set[str] = set()
    if re.search(r"(?:^|[\[{(\s])draft(?:[\]}):\s]|$)", title) or "status draft" in haystack:
        flags.update({"draft", "not_for_execution"})
    if "wip" in title or "work in progress" in haystack:
        flags.update({"wip", "not_for_execution"})
    if "internal" in title or " internal" in labels:
        flags.add("internal_only")
    if any(term in haystack for term in ("to-be-modified", "to be modified", " tbd", "todo")):
        flags.add("contains_unresolved_items")
    if "open question" in haystack or "open points" in haystack:
        flags.add("contains_unresolved_items")
    if (
        document_type == "runbook"
        or document_subtype
        in {"installation_guide", "rollback_procedure", "failover_procedure", "release_report"}
        or any(term in title for term in ("installation guide", "release notes", "release report"))
    ):
        if re.search(r"\b\d+\.\d+(?:\.\d+)?\b", title):
            flags.add("versioned_operational_document")
    if has_future_date(bundle.text):
        flags.update({"future_dated", "not_for_execution_until_verified"})
    if flags & {"draft", "wip", "contains_unresolved_items", "future_dated"}:
        flags.add("manual_review_required")
    return sorted(flags)


def missing_content_review_flags(
    bundle: PageBundle, document_type: str, document_subtype: str | None
) -> list[str]:
    flags: set[str] = set()
    image_count = len(re.findall(r"!\[[^\]]*\]\([^)]+\)", bundle.clean_markdown))
    if image_count:
        flags.add("visual_content_missing")
    if image_count >= 3 or (image_count and word_count(bundle.text) < 150):
        flags.update({"visual_content_review_recommended", "manual_review_required"})
    if bundle.missing_attachment_names:
        flags.add("attachment_content_missing")
    if bundle.missing_attachment_names and (
        document_subtype in {"api_specification", "performance_test_report"}
        or any(is_high_value_attachment(name) for name in bundle.missing_attachment_names)
        or document_type in {"design", "architecture"}
    ):
        flags.update({"attachment_content_review_recommended", "manual_review_required"})
    return sorted(flags)


def is_high_value_attachment(name: str) -> bool:
    lowered = name.lower().split("?", 1)[0]
    return any(lowered.endswith(suffix) for suffix in HIGH_VALUE_ATTACHMENT_SUFFIXES)


def has_future_date(text: str) -> bool:
    now = datetime.now(UTC).date()
    for match in re.finditer(
        rf"\b(\d{{1,2}})[\s/-]+({MONTH_NAMES})[a-z]*[\s/-]+(20\d{{2}})\b",
        text,
        flags=re.IGNORECASE,
    ):
        day, month_text, year = match.groups()
        month = month_number(month_text)
        if month is None:
            continue
        try:
            parsed = datetime(int(year), month, int(day), tzinfo=UTC).date()
        except ValueError:
            continue
        if parsed > now:
            return True
    for match in re.finditer(
        r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b",
        text,
        flags=re.IGNORECASE,
    ):
        year, month, day = match.groups()
        try:
            parsed = datetime(int(year), int(month), int(day), tzinfo=UTC).date()
        except ValueError:
            continue
        if parsed > now:
            return True
    return False


def month_number(value: str) -> int | None:
    lookup = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "sept": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    return lookup.get(value[:4].lower(), lookup.get(value[:3].lower()))


def has_linked_procedure(bundle: PageBundle) -> bool:
    if not bundle.links.links:
        return False
    text = f"{bundle.metadata.title}\n{bundle.text}".lower()
    procedure_terms = (
        "backout procedure",
        "backout procedures",
        "failover procedure",
        "failover procedures",
        "rollback procedure",
        "rollback procedures",
        "recovery procedure",
        "recovery procedures",
        "procedure is documented",
        "procedures are documented",
        "steps required",
    )
    if not any(term in text for term in procedure_terms):
        return False
    return any(link.href for link in bundle.links.links)


def is_procedural_runbook(bundle: PageBundle) -> bool:
    text = (
        f"{bundle.metadata.title}\n{' '.join(bundle.ancestor_titles)}\n{bundle.text[:2000]}".lower()
    )
    indicators = (
        "runbook",
        "installation guide",
        "troubleshooting",
        "troubleshoot",
        "rollback",
        "failover",
        "procedure",
        "operational document",
        "scenario",
        "how to",
        "deployment tools",
        "pipeline",
        "health check",
        "synthetic monitoring",
    )
    non_procedural = (
        "business document",
        "business overview",
        "technical document",
        "release report",
        "quiet running report",
        "performance test",
        "load testing result",
        "database information",
    )
    return any(term in text for term in indicators) and not any(
        term in text for term in non_procedural
    )


def infer_document_subtype(bundle: PageBundle, document_type: str) -> str | None:
    text = f"{bundle.metadata.title}\n{' '.join(bundle.ancestor_titles)}".lower()
    subtype_rules = [
        ("database_information", ("database information",)),
        ("rollback_procedure", ("rollback procedure", "rollback procedures")),
        ("failover_procedure", ("failover",)),
        ("disaster_recovery", ("disaster recovery", " dr ", "test standard and dr")),
        ("installation_guide", ("installation guide",)),
        ("troubleshooting_guide", ("troubleshooting", "troubleshoot")),
        ("release_report", ("release report", "release notes")),
        ("performance_test_report", ("performance test", "load testing result", "test results")),
        ("api_specification", ("api specification", "open api specification")),
        ("technical_document", ("technical document",)),
        ("technical_design", ("technical design", "solution design")),
        ("business_overview", ("business overview", "business document")),
        ("support_model", ("support model", "support escalation")),
        ("onboarding_guide", ("onboarding", "on-boarding")),
        ("faq", ("faq", "faqs")),
        ("monitoring_dashboard", ("dashboard", "datadog", "alarms", "monitoring")),
        ("change_checklist", ("change management checklist", "production readiness checklist")),
    ]
    for subtype, terms in subtype_rules:
        if any(term in text for term in terms):
            return subtype
    return document_type if document_type in {"support_model", "rca", "incident"} else None


def document_type_for_subtype(document_type: str, document_subtype: str | None) -> str:
    if not document_subtype:
        return document_type
    strong_subtype_type_map = {
        "performance_test_report": "reference",
        "release_report": "change_record",
    }
    if document_subtype in strong_subtype_type_map:
        return strong_subtype_type_map[document_subtype]
    if document_type != "unknown":
        return document_type
    subtype_type_map = {
        "api_specification": "reference",
        "business_overview": "reference",
        "database_information": "reference",
        "faq": "knowledge_article",
        "performance_test_report": "reference",
        "release_report": "change_record",
        "technical_document": "reference",
        "technical_design": "design",
    }
    return subtype_type_map.get(document_subtype, document_type)


def enrich_page(
    bundle: PageBundle,
    *,
    run_id: str,
    dataset_name: str,
    config: AppConfig,
    generated_at: str,
    hierarchy: HierarchyContext | None = None,
) -> Enrichment:
    hierarchy = hierarchy or build_hierarchy_context(bundle)
    headings = extract_headings(bundle.clean_markdown)
    document_type, document_type_confidence = classify_document(bundle)
    document_subtype = infer_document_subtype(bundle, document_type)
    document_type = document_type_for_subtype(document_type, document_subtype)
    flags = status_flags(bundle, document_type, bundle.metadata.updated_at)
    keywords = extract_keywords(bundle, headings)
    signals = detect_operational_signals(bundle)
    candidates = extract_candidate_entities(bundle, keywords)
    facts = extract_candidate_facts(bundle, candidates)
    text_words = word_count(bundle.text)
    quality = build_quality(
        document_type=document_type,
        updated_at=bundle.metadata.updated_at,
        status_flags=flags,
        word_count=text_words,
        heading_count=len(headings),
        signals=signals,
        outbound_link_count=len(bundle.links.links),
        config=config.scoring,
    )
    quality = adjust_quality_for_hierarchy(quality, hierarchy)
    band = quality_band(quality.overall_score)
    historical, current = currentness(document_type, flags, bundle.metadata.updated_at)
    short_summary, detailed_summary = summarize(bundle, document_type, keywords)
    signature = EnrichmentSignature(
        source_content_hash=bundle.source_content_hash,
        prompt_version=config.llm.prompt_version,
        provider=config.llm.provider,
        model_or_deployment=config.llm.model,
        tasks=sorted(task for task, enabled in config.features.llm.tasks.items() if enabled),
        enrichment_config_hash=config.enrichment_config_hash(),
    )
    warnings = warnings_for(
        document_type=document_type,
        quality_score=quality.overall_score,
        signals=signals,
        status_flags=flags,
        attachment_count=bundle.attachment_reference_count,
        has_linked_procedure=has_linked_procedure(bundle),
        is_procedural=is_procedural_runbook(bundle),
    )
    review_flags = sorted(
        set(sensitivity_review_flags(bundle))
        | set(trust_review_flags(bundle, document_type, document_subtype))
        | set(missing_content_review_flags(bundle, document_type, document_subtype))
    )
    for flag in review_flags:
        if flag not in warnings:
            warnings.append(flag)
    audience = audience_for_review_flags(review_flags)
    sensitivity = sensitivity_for_review_flags(review_flags)
    onyx = OnyxMetadata(
        link=bundle.metadata.url or "",
        file_display_name=bundle.metadata.title,
        doc_updated_at=bundle.metadata.updated_at or "",
        dataset_name=dataset_name,
        source_system="confluence",
        space_key=bundle.metadata.space_key,
        document_type=document_type,
        quality_band=band,
        approval_status="unreviewed",
        historical=historical,
        currentness=current,
        document_subtype=document_subtype,
        audience=audience,
        sensitivity=sensitivity,
        review_flags=review_flags,
    )
    return Enrichment(
        run_id=run_id,
        generated_at=generated_at,
        dataset_name=dataset_name,
        document_id=bundle.document_id,
        page_id=bundle.metadata.page_id,
        space_key=bundle.metadata.space_key,
        source_updated_at=bundle.metadata.updated_at,
        source_content_hash=bundle.source_content_hash,
        enriched_at=generated_at,
        ONYX_METADATA=onyx,
        document_type=document_type,
        document_type_confidence=document_type_confidence,
        document_subtype=document_subtype,
        hierarchy=hierarchy,
        short_summary=short_summary,
        detailed_summary=detailed_summary,
        keywords=keywords,
        categories=categories_for(bundle, document_type, keywords),
        themes=themes_for(bundle, document_type, keywords),
        concepts=concepts_for(headings, keywords),
        candidate_entities=candidates,
        entities=entity_bucket(candidates),
        operational_signals=signals,
        quality=quality,
        quality_band=band,
        warnings=warnings,
        review_flags=review_flags,
        audience=audience,
        sensitivity=sensitivity,
        candidate_facts=facts,
        confidence=min(0.95, (document_type_confidence + quality.overall_score / 100) / 2),
        historical=historical,
        currentness=current,
        headings=headings,
        status_flags=flags,
        signatures=signature,
    )


def signature_matches(enrichment: Enrichment, bundle: PageBundle, config: AppConfig) -> bool:
    expected = EnrichmentSignature(
        source_content_hash=bundle.source_content_hash,
        prompt_version=config.llm.prompt_version,
        provider=config.llm.provider,
        model_or_deployment=config.llm.model,
        tasks=sorted(task for task, enabled in config.features.llm.tasks.items() if enabled),
        enrichment_config_hash=config.enrichment_config_hash(),
    )
    return enrichment.signatures == expected


def refreshed_for_run(
    enrichment: Enrichment, *, run_id: str, generated_at: str, dataset_name: str
) -> Enrichment:
    data = enrichment.model_dump(mode="python")
    data["run_id"] = run_id
    data["generated_at"] = generated_at
    data["dataset_name"] = dataset_name
    return Enrichment.model_validate(data)
