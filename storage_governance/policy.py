from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
import json


class GovernanceDecision(str, Enum):
    PROTECT = "PROTECT"
    PROTECT_UNTIL_REFACTOR = "PROTECT_UNTIL_REFACTOR"
    CACHE = "CACHE"
    REVIEW = "REVIEW"
    CLEANUP = "CLEANUP"
    ROTATE = "ROTATE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class GovernanceRuleResult:
    decision: GovernanceDecision
    reason: str
    rule: str
    auto_apply_allowed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GovernancePolicy:
    schema_version: str
    policy_version: str
    factor_cache_days: int = 120
    benchmark_workspace_days: int = 14
    runtime_test_days: int = 14
    log_copy_days: int = 30
    log_retention_days: int = 30
    artifact_expiry_grace_days: int = 7
    artifact_payload_days: int = 30
    artifact_gc_interval_minutes: int = 60
    news_raw_html_days: int = 90
    protected_exact: tuple[str, ...] = ()
    protected_prefixes: tuple[str, ...] = ()
    protected_until_refactor: tuple[str, ...] = ()
    review_globs: tuple[str, ...] = ()
    output_review_prefixes: tuple[str, ...] = ()
    cleanup_globs: tuple[str, ...] = ()
    runtime_cleanup_prefixes: tuple[str, ...] = ()
    large_log_mb: int = 50
    log_rotation_keep: int = 5
    log_rotation_segment_mb: int = 20

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "GovernancePolicy":
        return cls(
            schema_version=str(row.get("schema_version") or "storage_governance_policy.v1"),
            policy_version=str(row.get("policy_version") or "unknown"),
            factor_cache_days=int(row.get("factor_cache_days") or 120),
            benchmark_workspace_days=int(row.get("benchmark_workspace_days") or 14),
            runtime_test_days=int(row.get("runtime_test_days") or 14),
            log_copy_days=int(row.get("log_copy_days") or 30),
            log_retention_days=int(row.get("log_retention_days") or 30),
            artifact_expiry_grace_days=int(row.get("artifact_expiry_grace_days") or 7),
            artifact_payload_days=int(row.get("artifact_payload_days") or 30),
            artifact_gc_interval_minutes=int(row.get("artifact_gc_interval_minutes") or 60),
            news_raw_html_days=int(row.get("news_raw_html_days") or 90),
            protected_exact=tuple(_norm(x) for x in row.get("protected_exact") or []),
            protected_prefixes=tuple(_norm(x) for x in row.get("protected_prefixes") or []),
            protected_until_refactor=tuple(_norm(x) for x in row.get("protected_until_refactor") or []),
            review_globs=tuple(_norm(x) for x in row.get("review_globs") or []),
            output_review_prefixes=tuple(_norm(x) for x in row.get("output_review_prefixes") or []),
            cleanup_globs=tuple(_norm(x) for x in row.get("cleanup_globs") or []),
            runtime_cleanup_prefixes=tuple(_norm(x) for x in row.get("runtime_cleanup_prefixes") or []),
            large_log_mb=int(row.get("large_log_mb") or 50),
            log_rotation_keep=int(row.get("log_rotation_keep") or 5),
            log_rotation_segment_mb=int(row.get("log_rotation_segment_mb") or 20),
        )


def _norm(value: str) -> str:
    return str(value or "").replace("\\", "/").lstrip("./")


def default_policy_path() -> Path:
    return Path(__file__).with_name("default_policy.json")


def load_default_policy(path: str | Path | None = None) -> GovernancePolicy:
    p = Path(path) if path else default_policy_path()
    return GovernancePolicy.from_dict(json.loads(p.read_text(encoding="utf-8")))


def match_glob(path: str, pattern: str) -> bool:
    path = _norm(path)
    pattern = _norm(pattern)
    return fnmatch(path, pattern)
