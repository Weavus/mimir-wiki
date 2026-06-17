from __future__ import annotations

import re
from collections import Counter

from mimir_wiki.cache_reader import PageBundle
from mimir_wiki.config import AppConfig
from mimir_wiki.schemas import (
    CandidateEntity,
    CandidateFact,
    CandidateMention,
    Enrichment,
    EnrichmentSignature,
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
    return [term for term, _ in terms.most_common(max_terms)]


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
    return [value for value in dict.fromkeys(values) if value]


def concepts_for(headings: list[str], keywords: list[str]) -> list[str]:
    values = [normalize_term(heading) for heading in headings[:8]]
    values.extend(keywords[:6])
    return [value for value in dict.fromkeys(values) if value]


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
) -> list[str]:
    warnings: list[str] = []
    if "deprecated" in status_flags or "archived" in status_flags:
        warnings.append("source_is_archived_or_deprecated")
    if quality_score < 50:
        warnings.append("low_quality_score")
    if not signals.has_owner:
        warnings.append("missing_explicit_owner")
    if document_type == "runbook" and not signals.has_validation_steps:
        warnings.append("missing_validation_steps")
    if document_type == "runbook" and not signals.has_backout_steps:
        if has_linked_procedure:
            warnings.append("linked_procedure_not_expanded")
        else:
            warnings.append("missing_backout_steps")
    if attachment_count:
        warnings.append("attachments_present_not_parsed")
    return warnings


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


def enrich_page(
    bundle: PageBundle, *, run_id: str, dataset_name: str, config: AppConfig, generated_at: str
) -> Enrichment:
    headings = extract_headings(bundle.clean_markdown)
    document_type, document_type_confidence = classify_document(bundle)
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
        attachment_count=len(bundle.attachment_names),
        has_linked_procedure=has_linked_procedure(bundle),
    )
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
