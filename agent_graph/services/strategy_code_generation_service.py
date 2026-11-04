from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_graph.services.strategy_config_compiler import StrategyImplementationSpec


class StrategyCodeGenerationService:
    """Generate deterministic strategy artifacts only inside an isolated root."""

    def generate(
        self,
        spec: StrategyImplementationSpec,
        *,
        artifact_root: Path,
        allowed_root: Path,
    ) -> list[Path]:
        root = artifact_root.resolve()
        allowed = allowed_root.resolve()
        if root != allowed and allowed not in root.parents:
            raise PermissionError("generated_code_path_outside_strategy_drafts")
        generated_dir = root / "generated_code"
        generated_dir.mkdir(parents=True, exist_ok=True)

        if spec.implementation_type == "config":
            return []
        if spec.implementation_type == "composite":
            path = generated_dir / "composition.json"
            path.write_text(
                json.dumps(
                    {
                        "components": spec.components,
                        "config": spec.generated_config,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            return [path]

        path = generated_dir / "strategy_plugin.py"
        path.write_text(
            self._plugin_source(),
            encoding="utf-8",
        )
        capability_path = generated_dir / "capability_spec.json"
        capability_path.write_text(
            json.dumps(
                spec.new_capability_spec,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return [path, capability_path]

    @staticmethod
    def _plugin_source() -> str:
        return '''from __future__ import annotations

from strategies.base import PortfolioStrategy, StrategyContext, StrategyResult


class GeneratedIsolatedStrategy(PortfolioStrategy):
    strategy_id = "generated_isolated_strategy"
    strategy_name = "Generated isolated strategy"
    version = "draft"

    def get_config_schema(self) -> dict:
        return {"type": "object", "additionalProperties": True}

    def validate_config(self, config: dict) -> list[str]:
        return [] if isinstance(config, dict) else ["config_must_be_object"]

    def generate_target(
        self,
        context: StrategyContext,
        config: dict,
    ) -> StrategyResult:
        return StrategyResult(
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            trade_date=context.trade_date,
            target_weights={},
            cash_weight=1.0,
            warnings=["isolated_generated_strategy_requires_validated_logic"],
            metadata={"isolated_draft": True},
        )
'''
