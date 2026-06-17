from __future__ import annotations

from datetime import UTC, datetime

from mimir_wiki.config import ScoringConfig
from mimir_wiki.schemas import OperationalSignals, Quality
from mimir_wiki.utils import parse_datetime


def quality_band(score: int) -> str:
    if score >= 85:
        return "excellent"
    if score >= 70:
        return "good"
    if score >= 50:
        return "fair"
    return "poor"


def currentness(
    document_type: str, status_flags: list[str], updated_at: str | None
) -> tuple[bool, str]:
    if "archived" in status_flags or "deprecated" in status_flags:
        return True, "deprecated"
    if document_type == "rca":
        return True, "historical"
    parsed = parse_datetime(updated_at)
    if parsed is None:
        return False, "unknown"
    age_days = (datetime.now(UTC) - parsed).days
    if age_days > 730:
        return False, "stale"
    return False, "current"


def score_freshness(
    updated_at: str | None, document_type: str, flags: list[str], config: ScoringConfig
) -> int:
    if document_type == "rca":
        return 75
    if "archived" in flags or "deprecated" in flags:
        return 15
    parsed = parse_datetime(updated_at)
    if parsed is None:
        return 35
    age_days = (datetime.now(UTC) - parsed).days
    thresholds = config.freshness_days
    if age_days <= thresholds.get("excellent", 90):
        return 100
    if age_days <= thresholds.get("good", 180):
        return 85
    if age_days <= thresholds.get("acceptable", 365):
        return 70
    if age_days <= thresholds.get("stale", 730):
        return 45
    return 20


def score_completeness(word_count: int, heading_count: int, signals: OperationalSignals) -> int:
    score = 20
    if word_count >= 200:
        score += 20
    if word_count >= 800:
        score += 15
    if heading_count >= 3:
        score += 15
    if heading_count >= 8:
        score += 10
    signal_count = sum(1 for value in signals.model_dump().values() if value is True)
    score += min(20, signal_count * 3)
    return min(100, score)


def score_operational_value(
    document_type: str, signals: OperationalSignals, link_count: int
) -> int:
    type_base = {
        "runbook": 85,
        "known_error": 80,
        "rca": 80,
        "incident": 70,
        "support_model": 75,
        "architecture": 70,
        "knowledge_article": 65,
        "design": 55,
        "migration": 55,
        "reference": 45,
        "project_plan": 35,
        "meeting_notes": 25,
        "archive": 15,
        "unknown": 25,
    }.get(document_type, 35)
    signal_bonus = min(20, sum(1 for value in signals.model_dump().values() if value is True) * 2)
    link_bonus = min(5, link_count)
    return min(100, type_base + signal_bonus + link_bonus)


def score_ownership(signals: OperationalSignals) -> int:
    score = 0
    if signals.has_owner:
        score += 45
    if signals.has_support_group:
        score += 35
    if signals.has_escalation_path:
        score += 20
    return min(100, score)


def build_quality(
    *,
    document_type: str,
    updated_at: str | None,
    status_flags: list[str],
    word_count: int,
    heading_count: int,
    signals: OperationalSignals,
    outbound_link_count: int,
    config: ScoringConfig,
) -> Quality:
    freshness = score_freshness(updated_at, document_type, status_flags, config)
    authority = config.document_type_weights.get(
        document_type, config.document_type_weights.get("unknown", 10)
    )
    if "deprecated" in status_flags:
        authority = max(0, authority - 50)
    if "archived" in status_flags:
        authority = max(0, authority - 40)
    completeness = score_completeness(word_count, heading_count, signals)
    operational = score_operational_value(document_type, signals, outbound_link_count)
    ownership = score_ownership(signals)
    contradiction_risk = 35 if document_type in {"unknown", "meeting_notes", "project_plan"} else 10
    staleness_risk = max(0, 100 - freshness)
    weights = config.weights
    overall = round(
        freshness * weights.get("freshness", 0.20)
        + authority * weights.get("authority", 0.20)
        + completeness * weights.get("completeness", 0.20)
        + operational * weights.get("operational_value", 0.25)
        + ownership * weights.get("ownership_clarity", 0.10)
        - contradiction_risk * weights.get("contradiction_penalty", 0.05)
    )
    overall = max(0, min(100, overall))
    return Quality(
        freshness_score=freshness,
        authority_score=authority,
        completeness_score=completeness,
        operational_value_score=operational,
        ownership_clarity_score=ownership,
        staleness_risk_score=staleness_risk,
        contradiction_risk_score=contradiction_risk,
        overall_score=overall,
    )
