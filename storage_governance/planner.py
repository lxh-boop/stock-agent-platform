from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .inventory import StorageFile, scan_files
from .policy import GovernanceDecision, GovernancePolicy, GovernanceRuleResult, load_default_policy, match_glob


@dataclass(frozen=True)
class PlannedFile:
    path: str
    size_bytes: int
    size_mb: float
    age_days: float
    decision: str
    reason: str
    rule: str
    auto_apply_allowed: bool


@dataclass(frozen=True)
class GovernancePlan:
    project: str
    policy_version: str
    created_at: str
    files: tuple[PlannedFile, ...]

    def totals(self) -> dict[str, dict[str, float | int]]:
        out: dict[str, dict[str, float | int]] = {}
        for row in self.files:
            item = out.setdefault(row.decision, {"files": 0, "size_bytes": 0, "size_gb": 0.0})
            item["files"] = int(item["files"]) + 1
            item["size_bytes"] = int(item["size_bytes"]) + row.size_bytes
        for item in out.values():
            item["size_gb"] = round(int(item["size_bytes"]) / (1024 ** 3), 4)
        return out

    def reclaimable_bytes(self) -> int:
        return sum(row.size_bytes for row in self.files if row.decision == GovernanceDecision.CLEANUP.value and row.auto_apply_allowed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "policy_version": self.policy_version,
            "created_at": self.created_at,
            "totals": self.totals(),
            "reclaimable_bytes": self.reclaimable_bytes(),
            "files": [asdict(x) for x in self.files],
        }


class GovernancePlanner:
    def __init__(self, policy: GovernancePolicy | None = None) -> None:
        self.policy = policy or load_default_policy()

    def classify(self, item: StorageFile) -> GovernanceRuleResult:
        path = item.path.replace("\\", "/").lstrip("./")
        low = path.lower()
        p = self.policy

        # 0) Location ownership wins before generic file-type guards.
        # A known LocalAppData snapshot is a copied workspace. Its internal DB/model/cache
        # files are copies too; protecting them by extension would make the snapshot only
        # half-cleanable and is the exact conflict found by the real Stage 3.6A report.
        if low.startswith("logs/clean_localappdata_"):
            if item.age_days >= p.log_copy_days:
                return GovernanceRuleResult(
                    GovernanceDecision.CLEANUP,
                    f"copied LocalAppData snapshot older than {p.log_copy_days}d",
                    "localappdata_snapshot",
                    auto_apply_allowed=True,
                )
            return GovernanceRuleResult(GovernanceDecision.REVIEW, "recent copied LocalAppData snapshot", "localappdata_snapshot_recent")

        # Benchmark isolated workspaces are owned by benchmark execution, not by the DBs
        # contained inside them. Keep canonical summaries/traces outside these workspaces.
        if low.startswith("outputs/benchmarks/agent_capability/isolated_workspaces/"):
            if item.age_days >= p.benchmark_workspace_days:
                return GovernanceRuleResult(
                    GovernanceDecision.CLEANUP,
                    f"benchmark isolated workspace older than {p.benchmark_workspace_days}d; canonical summary/raw traces live outside workspace",
                    "benchmark_isolated_workspace",
                    auto_apply_allowed=True,
                )
            return GovernanceRuleResult(GovernanceDecision.REVIEW, "recent benchmark isolated workspace", "benchmark_isolated_workspace_recent")

        # Known runtime probe/test families are disposable after their retention window.
        if any(low.startswith(prefix.lower()) for prefix in p.runtime_cleanup_prefixes):
            if item.age_days >= p.runtime_test_days:
                return GovernanceRuleResult(
                    GovernanceDecision.CLEANUP,
                    f"runtime test/probe workspace older than {p.runtime_test_days}d",
                    "runtime_test_workspace",
                    auto_apply_allowed=True,
                )
            return GovernanceRuleResult(GovernanceDecision.REVIEW, "recent runtime test/probe workspace", "runtime_test_workspace_recent")

        # Build/distribution trees are reviewed as one ownership domain. A DB/model inside a
        # frozen distribution is not the live production DB/model.
        if low.startswith("dist/") or low.startswith("build/") or low.startswith("installer_output/"):
            root = low.split("/", 1)[0]
            return GovernanceRuleResult(GovernanceDecision.REVIEW, "build/distribution artifact; retain or remove as a release unit", f"build_artifact:{root}")

        # Raw news HTML must obey the actual age threshold. The previous broad glob marked
        # 20-48 day files REVIEW while claiming they were >90d; FIX1 removes that mismatch.
        if low.startswith("outputs/news_full_text/raw_html/"):
            if item.age_days >= p.news_raw_html_days:
                return GovernanceRuleResult(
                    GovernanceDecision.REVIEW,
                    f"raw news HTML older than {p.news_raw_html_days}d; remove only after DB evidence retention is verified",
                    "news_raw_html_retention",
                )
            return GovernanceRuleResult(
                GovernanceDecision.PROTECT,
                f"raw news HTML is within {p.news_raw_html_days}d retention window",
                "news_raw_html_recent",
            )

        # Stage 3.7: Alpha158 values are derived from authoritative raw market data.
        # This must run before the historical protected-until-refactor rules.
        if low.endswith("feature_stock_data_alpha158.csv"):
            return GovernanceRuleResult(
                GovernanceDecision.CLEANUP,
                "rebuildable Alpha158 values are computed from raw market data on demand",
                "alpha158_on_demand",
                auto_apply_allowed=True,
            )

        # 1) Hard live protections.
        if path in p.protected_exact:
            return GovernanceRuleResult(GovernanceDecision.PROTECT, "authoritative/live exact path", "protected_exact")
        if any(path.startswith(prefix) for prefix in p.protected_prefixes):
            return GovernanceRuleResult(GovernanceDecision.PROTECT, "authoritative/raw/production prefix", "protected_prefixes")
        if path in p.protected_until_refactor:
            reason = "active cache still referenced by current application; do not delete before owner refactor"
            return GovernanceRuleResult(GovernanceDecision.PROTECT_UNTIL_REFACTOR, reason, "protected_until_refactor")

        # 2) Derived/cache families.
        if match_glob(path, "data/model_precision/stock_direction_features*.parquet"):
            return GovernanceRuleResult(
                GovernanceDecision.REVIEW,
                "derived model feature matrix; candidate for removal after rebuild-chain verification",
                "derived_feature_review",
            )

        # 3) Explicit review patterns.
        for pattern in p.review_globs:
            if match_glob(path, pattern):
                return GovernanceRuleResult(GovernanceDecision.REVIEW, "explicit review-required retention rule", f"review_glob:{pattern}")

        # 4) Logs: large active log => ROTATE; old small log => REVIEW; recent log => PROTECT.
        if low.startswith("logs/"):
            if item.suffix == ".log" and item.size_bytes >= p.large_log_mb * 1024 * 1024:
                return GovernanceRuleResult(
                    GovernanceDecision.ROTATE,
                    f"large log >= {p.large_log_mb} MB; rotate/retain bounded segments",
                    "large_log_rotation",
                )
            if item.age_days >= p.log_retention_days:
                return GovernanceRuleResult(
                    GovernanceDecision.REVIEW,
                    f"log/output older than {p.log_retention_days}d; owner-aware retention integration required before deletion",
                    "old_log_review",
                )
            return GovernanceRuleResult(GovernanceDecision.PROTECT, "recent log within retention window", "recent_log_guard")

        # 5) Runtime artifacts use embedded expiry in a later lifecycle integration; keep now.
        if low.startswith("runtime/artifacts/") and item.suffix == ".json":
            return GovernanceRuleResult(GovernanceDecision.PROTECT, "artifact payload lifecycle is owner-managed; scanner does not delete runtime artifacts", "artifact_owner_managed")

        # 6) Generic database/model guards after location-owned cleanup/review rules.
        if item.suffix in {".db", ".sqlite", ".sqlite3"}:
            return GovernanceRuleResult(GovernanceDecision.PROTECT, "database outside known disposable workspace", "database_guard")
        if low.startswith("models/"):
            return GovernanceRuleResult(GovernanceDecision.PROTECT, "model asset", "model_guard")

        # 7) Data defaults conservative: data is presumed factual/business input unless a
        # specific derived/cache rule above says otherwise.
        if low.startswith("data/"):
            return GovernanceRuleResult(GovernanceDecision.PROTECT, "unclassified data under data/; conservative raw/business guard", "data_default_guard")

        # 8) Output families: known canonical traces stay protected; known generated outputs
        # become REVIEW; all other outputs are REVIEW rather than UNKNOWN so source files do
        # not inflate storage-governance uncertainty.
        if any(low.startswith(prefix.lower()) for prefix in p.output_review_prefixes):
            return GovernanceRuleResult(GovernanceDecision.REVIEW, "generated output requires owner-aware retention rule", "output_review_prefix")
        if low.startswith("outputs/"):
            return GovernanceRuleResult(GovernanceDecision.REVIEW, "unclassified generated output; review before retention integration", "outputs_default_review")

        # 9) Unclassified runtime state is protected until a concrete owner/expiry rule exists.
        if low.startswith("runtime/"):
            return GovernanceRuleResult(GovernanceDecision.PROTECT, "unclassified runtime state; conservative guard", "runtime_default_guard")

        # 10) Other managed storage roots (backups/checkpoints/etc.) are review-only.
        return GovernanceRuleResult(GovernanceDecision.REVIEW, "managed storage artifact without an auto-delete rule", "managed_storage_default_review")

    def plan(self, project: str | Path) -> GovernancePlan:
        root = Path(project).resolve()
        rows: list[PlannedFile] = []
        for item in scan_files(root):
            result = self.classify(item)
            rows.append(PlannedFile(
                path=item.path,
                size_bytes=item.size_bytes,
                size_mb=round(item.size_bytes / (1024 * 1024), 3),
                age_days=round(item.age_days, 2),
                decision=result.decision.value,
                reason=result.reason,
                rule=result.rule,
                auto_apply_allowed=result.auto_apply_allowed,
            ))
        rows.sort(key=lambda x: x.size_bytes, reverse=True)
        return GovernancePlan(
            project=str(root),
            policy_version=self.policy.policy_version,
            created_at=datetime.now(timezone.utc).isoformat(),
            files=tuple(rows),
        )
