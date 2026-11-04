from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

from agent_graph.services.strategy_backtest_service import StrategyBacktestService
from agent_graph.services.strategy_implementation_service import (
    StrategyImplementation,
    StrategyImplementationService,
    canonical_json,
    sha256_file,
    sha256_text,
)
from agent_graph.services.strategy_validation_service import (
    StrategyValidationService,
)
from strategies.base import PortfolioStrategy


FORMAL_BASELINE_FILES = [
    Path("portfolio/hierarchical_top10_allocator.py"),
    Path("portfolio/rebalance_rules.py"),
    Path("strategies/adapters/hierarchical_top10_strategy.py"),
]


def _now_text() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class StrategyImplementationPreview:
    implementation_id: str
    proposal_id: str
    proposal_version: int
    implementation_type: str
    formal_files_planned: list[str]
    config_diff: dict[str, Any]
    code_diff_summary: str
    security_result: dict[str, Any]
    test_result: dict[str, Any]
    backtest_result: dict[str, Any]
    risk_return_tradeoffs: list[dict[str, Any]]
    rollback_plan: dict[str, Any]
    affects_current_positions: bool
    semantic_change_required: bool
    validation_status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StrategyReviewService:
    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        runtime_dir: str | Path = "runtime",
        project_root: str | Path = ".",
    ) -> None:
        self.implementations = StrategyImplementationService(
            db_path=db_path,
            runtime_dir=runtime_dir,
        )
        self.validator = StrategyValidationService()
        self.backtester = StrategyBacktestService()
        self.project_root = Path(project_root).resolve()

    def validate_and_preview(
        self,
        implementation_id: str,
        *,
        user_id: str,
    ) -> StrategyImplementation:
        implementation = self.implementations.get(
            implementation_id,
            user_id=user_id,
        )
        if implementation is None:
            raise ValueError("implementation_not_found")
        root = Path(implementation.artifact_root)
        spec = json.loads(
            (root / "implementation_spec.json").read_text(encoding="utf-8")
        )
        config = json.loads(
            (root / "generated_config.json").read_text(encoding="utf-8")
        )
        config_result = self._validate_config(config)
        security_result = self.validator.scan_generated_code(
            root / "generated_code"
        )
        interface_result = self._validate_interface(
            root,
            implementation.implementation_type,
        )
        test_result = {
            "status": (
                "passed"
                if config_result["status"] == "passed"
                and security_result["status"] == "passed"
                and interface_result["status"] == "passed"
                else "failed"
            ),
            "schema": config_result,
            "security": security_result,
            "interface": interface_result,
            "artifact_isolation": {
                "status": "passed"
                if root.resolve().is_relative_to(
                    self.implementations.drafts_root.resolve()
                )
                else "failed",
                "artifact_root": str(root.resolve()),
            },
        }
        semantic_change_required = config_result["status"] != "passed"
        backtest = (
            self.backtester.run(config)
            if test_result["status"] == "passed"
            else {
                "status": "blocked",
                "reason": "validation_failed",
                "tradeoffs": [],
                "isolation": {
                    "writes_formal_account": False,
                    "writes_formal_outputs": False,
                },
            }
        )
        formal_hashes_before = self._formal_hashes()
        (root / "security_report.json").write_text(
            json.dumps(
                security_result,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        (root / "test_report.json").write_text(
            json.dumps(
                test_result,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        (root / "backtest_report.json").write_text(
            json.dumps(
                backtest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        formal_hashes_after = self._formal_hashes()
        if formal_hashes_before != formal_hashes_after:
            raise RuntimeError("formal_project_changed_during_isolated_review")

        default_config = {
            "entry_top_k": 10,
            "hold_buffer_rank": 15,
            "max_positions": 10,
            "target_invested_weight": 0.80,
            "minimum_cash_ratio": 0.05,
            "min_rebalance_weight_delta": 0.01,
        }
        config_diff = {
            key: {"before": default_config.get(key), "after": value}
            for key, value in config.items()
            if default_config.get(key) != value
        }
        preview = StrategyImplementationPreview(
            implementation_id=implementation.implementation_id,
            proposal_id=implementation.proposal_id,
            proposal_version=implementation.proposal_version,
            implementation_type=implementation.implementation_type,
            formal_files_planned=list(spec.get("formal_files") or []),
            config_diff=config_diff,
            code_diff_summary=(root / "diff.patch").read_text(
                encoding="utf-8"
            ),
            security_result=security_result,
            test_result=test_result,
            backtest_result=backtest,
            risk_return_tradeoffs=list(backtest.get("tradeoffs") or []),
            rollback_plan={
                "method": "remove_new_version_and_restore_previous_binding",
                "preserve_history": True,
                "original_strategy_files_overwritten": False,
            },
            affects_current_positions=False,
            semantic_change_required=semantic_change_required,
            validation_status=test_result["status"],
        )
        preview_path = root / "implementation_preview.json"
        preview_path.write_text(
            json.dumps(
                preview.to_dict(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self._write_preview_markdown(root, preview)
        manifest, manifest_hash = self._refresh_manifest(
            root,
            implementation,
            formal_hashes_before=formal_hashes_before,
        )
        status = (
            "validated"
            if test_result["status"] == "passed"
            and backtest.get("status") == "passed"
            else "validation_failed"
        )
        self.implementations.repository.update_implementation(
            implementation.implementation_id,
            {
                "implementation_hash": manifest["implementation_hash"],
                "artifact_manifest_hash": manifest_hash,
                "status": status,
                "updated_at": _now_text(),
            },
        )
        if semantic_change_required:
            self.implementations.proposals.set_status(
                implementation.proposal_id,
                user_id=user_id,
                status="revising",
                expected_version=implementation.proposal_version,
            )
        updated = self.implementations.get(
            implementation.implementation_id,
            user_id=user_id,
        )
        if updated is None:
            raise RuntimeError("implementation_update_lost")
        return updated

    @staticmethod
    def _validate_config(config: dict[str, Any]) -> dict[str, Any]:
        errors: list[str] = []
        numeric = [
            "target_invested_weight",
            "minimum_cash_ratio",
            "min_rebalance_weight_delta",
        ]
        integers = ["entry_top_k", "hold_buffer_rank", "max_positions"]
        parsed: dict[str, float] = {}
        for key in numeric + integers:
            try:
                parsed[key] = float(config[key])
            except (KeyError, TypeError, ValueError):
                errors.append(f"{key}_must_be_numeric")
        for key in numeric:
            if key in parsed and not 0.0 <= parsed[key] <= 1.0:
                errors.append(f"{key}_must_be_between_0_and_1")
        for key in integers:
            if key in parsed and not 1 <= parsed[key] <= 300:
                errors.append(f"{key}_must_be_between_1_and_300")
        if all(key in parsed for key in integers):
            if parsed["max_positions"] > parsed["entry_top_k"]:
                errors.append("max_positions_must_not_exceed_entry_top_k")
            if parsed["entry_top_k"] > parsed["hold_buffer_rank"]:
                errors.append("entry_top_k_must_not_exceed_hold_buffer_rank")
        if all(
            key in parsed
            for key in ["target_invested_weight", "minimum_cash_ratio"]
        ) and (
            parsed["target_invested_weight"]
            + parsed["minimum_cash_ratio"]
            > 1.0 + 1e-12
        ):
            errors.append(
                "target_invested_weight_plus_minimum_cash_must_not_exceed_one"
            )
        return {
            "status": "passed" if not errors else "failed",
            "errors": errors,
            "canonical_config": config,
            "semantic_change_required": bool(errors),
        }

    @staticmethod
    def _validate_interface(
        root: Path,
        implementation_type: str,
    ) -> dict[str, Any]:
        if implementation_type != "code":
            return {
                "status": "passed",
                "reason": "no_generated_python_for_this_path",
            }
        path = root / "generated_code" / "strategy_plugin.py"
        try:
            module_spec = importlib.util.spec_from_file_location(
                f"_isolated_strategy_{hashlib.sha256(str(path).encode()).hexdigest()[:12]}",
                path,
            )
            if module_spec is None or module_spec.loader is None:
                raise ImportError("module_spec_unavailable")
            module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(module)
            strategy_class = getattr(module, "GeneratedIsolatedStrategy")
            instance = strategy_class()
            if not isinstance(instance, PortfolioStrategy):
                raise TypeError("not_portfolio_strategy")
            schema = instance.get_config_schema()
            errors = instance.validate_config({})
            if not isinstance(schema, dict) or not isinstance(errors, list):
                raise TypeError("invalid_strategy_interface_return_type")
        except Exception as exc:
            return {
                "status": "failed",
                "errors": [f"{type(exc).__name__}:{exc}"],
            }
        return {
            "status": "passed",
            "class_name": "GeneratedIsolatedStrategy",
            "base_class": "PortfolioStrategy",
        }

    def _formal_hashes(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for relative in FORMAL_BASELINE_FILES:
            path = self.project_root / relative
            if path.exists():
                result[relative.as_posix()] = sha256_file(path)
        return result

    @staticmethod
    def _write_preview_markdown(
        root: Path,
        preview: StrategyImplementationPreview,
    ) -> None:
        (root / "implementation_preview.md").write_text(
            "\n".join(
                [
                    "# 策略实施预览",
                    "",
                    f"- Proposal：`{preview.proposal_id}` v{preview.proposal_version}",
                    f"- 实现类型：`{preview.implementation_type}`",
                    f"- 校验：`{preview.validation_status}`",
                    f"- 正式文件计划：{', '.join(preview.formal_files_planned) or '无'}",
                    f"- 配置差异：`{canonical_json(preview.config_diff)}`",
                    f"- 安全：`{preview.security_result.get('status')}`",
                    f"- 隔离回测：`{preview.backtest_result.get('status')}`",
                    "- 回滚：删除新版本并恢复前一账户 Binding；保留全部历史。",
                    "- 是否立即影响当前持仓：否。",
                    "",
                    "本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。",
                ]
            ),
            encoding="utf-8",
        )

    def _refresh_manifest(
        self,
        root: Path,
        implementation: StrategyImplementation,
        *,
        formal_hashes_before: dict[str, str],
    ) -> tuple[dict[str, Any], str]:
        old = dict(implementation.artifact_manifest or {})
        files = sorted(
            path
            for path in root.rglob("*")
            if path.is_file() and path.name != "artifact_manifest.json"
        )
        hashes = {
            path.relative_to(root).as_posix(): sha256_file(path)
            for path in files
        }
        implementation_hash = sha256_text(canonical_json(hashes))
        manifest = {
            **old,
            "implementation_hash": implementation_hash,
            "artifact_hashes": hashes,
            "formal_baseline_hashes": formal_hashes_before,
            "security_report_hash": hashes.get("security_report.json", ""),
            "test_report_hash": hashes.get("test_report.json", ""),
            "backtest_report_hash": hashes.get("backtest_report.json", ""),
            "diff_hash": hashes.get("diff.patch", ""),
            "implementation_preview_hash": hashes.get(
                "implementation_preview.json",
                "",
            ),
            "validation_status": json.loads(
                (root / "test_report.json").read_text(encoding="utf-8")
            ).get("status"),
        }
        text = json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        (root / "artifact_manifest.json").write_text(text, encoding="utf-8")
        return manifest, sha256_text(text)
