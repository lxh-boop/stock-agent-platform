from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import json

from .planner import GovernancePlan, PlannedFile
from .policy import GovernanceDecision

CONFIRM_TOKEN = "DELETE_STAGE36A_SAFE_CANDIDATES"


class CleanupRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class CleanupResult:
    deleted_files: int
    deleted_bytes: int
    skipped_files: int


def _resolve_under(project: Path, relative: str) -> Path:
    root = project.resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise CleanupRefused(f"path escapes project: {relative}") from exc
    return target


def safe_cleanup_rows(plan: GovernancePlan) -> list[PlannedFile]:
    return [
        row for row in plan.files
        if row.decision == GovernanceDecision.CLEANUP.value and row.auto_apply_allowed
    ]


def apply_safe_cleanup(
    plan: GovernancePlan,
    *,
    confirm_token: str,
    max_delete_bytes: int = 2 * 1024 ** 3,
) -> CleanupResult:
    if confirm_token != CONFIRM_TOKEN:
        raise CleanupRefused("explicit confirm token required")
    project = Path(plan.project)
    rows = safe_cleanup_rows(plan)
    planned = sum(row.size_bytes for row in rows)
    if planned > max_delete_bytes:
        raise CleanupRefused(
            f"planned safe cleanup {planned} bytes exceeds max_delete_bytes={max_delete_bytes}; review in smaller batches"
        )
    deleted = 0
    deleted_bytes = 0
    skipped = 0
    for row in rows:
        target = _resolve_under(project, row.path)
        if target.is_file():
            size = target.stat().st_size
            target.unlink()
            deleted += 1
            deleted_bytes += size
        else:
            skipped += 1
    return CleanupResult(deleted_files=deleted, deleted_bytes=deleted_bytes, skipped_files=skipped)
