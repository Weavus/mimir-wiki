SCHEMA_VERSION = "mimir-wiki/v1"
GENERATOR = "mimir-wiki"

DOCUMENT_TYPES = {
    "approved_runbook",
    "runbook",
    "architecture",
    "design",
    "support_model",
    "incident",
    "rca",
    "knowledge_article",
    "known_error",
    "migration",
    "onboarding",
    "meeting_notes",
    "project_plan",
    "change_record",
    "reference",
    "archive",
    "unknown",
}

LLM_TASKS = {
    "classification",
    "summary",
    "keywords",
    "themes",
    "concepts",
    "candidate_entities",
    "operational_signals",
    "quality_warnings",
}

EXIT_SUCCESS = 0
EXIT_USER_ERROR = 1
EXIT_RUNTIME_ERROR = 2
EXIT_PARTIAL_SUCCESS = 3
