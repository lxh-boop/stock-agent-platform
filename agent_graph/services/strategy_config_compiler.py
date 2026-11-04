from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


CANONICAL_CONFIG_DEFAULTS: dict[str, Any] = {
    "entry_top_k": 10,
    "hold_buffer_rank": 15,
    "max_positions": 10,
    "target_invested_weight": 0.80,
    "minimum_cash_ratio": 0.05,
    "min_rebalance_weight_delta": 0.01,
}

LEGACY_CONFIG_FIELDS = {
    "top_n": "entry_top_k",
    "target_ratio": "target_invested_weight",
    "min_cash_ratio": "minimum_cash_ratio",
}

IMPLEMENTATION_TYPES = {"config", "composite", "code"}


@dataclass(frozen=True)
class StrategyImplementationSpec:
    implementation_type: str
    base_strategy_id: str
    base_strategy_version: str
    generated_config: dict[str, Any] = field(default_factory=dict)
    components: list[dict[str, Any]] = field(default_factory=list)
    new_capability_spec: dict[str, Any] = field(default_factory=dict)
    formal_files: list[str] = field(default_factory=list)
    semantic_source: str = "locked_proposal"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StrategyConfigCompiler:
    """Compile structured Proposal JSON without interpreting free-form language."""

    def compile(
        self,
        proposal_json: dict[str, Any],
        *,
        base_strategy_id: str,
        base_strategy_version: str,
    ) -> StrategyImplementationSpec:
        proposal = dict(proposal_json or {})
        explicit_type = str(
            proposal.get("implementation_type")
            or proposal.get("implementation_path")
            or ""
        ).strip().lower()
        aliases = {
            "configuration": "config",
            "existing_config": "config",
            "existing_strategy_config": "config",
            "component": "composite",
            "components": "composite",
            "new_code": "code",
            "plugin": "code",
        }
        explicit_type = aliases.get(explicit_type, explicit_type)

        raw_config = (
            proposal.get("generated_config")
            or proposal.get("proposed_config")
            or proposal.get("runtime_config")
            or proposal.get("config")
            or {}
        )
        config = self._canonical_config(raw_config)
        components = [
            dict(item)
            for item in list(proposal.get("components") or [])
            if isinstance(item, dict)
        ]
        capability = dict(
            proposal.get("new_capability_spec")
            or proposal.get("implementation_spec")
            or {}
        )
        if proposal.get("requires_new_capability") is True:
            explicit_type = "code"

        implementation_type = explicit_type
        if not implementation_type:
            if capability:
                implementation_type = "code"
            elif components:
                implementation_type = "composite"
            elif raw_config:
                implementation_type = "config"
        if implementation_type not in IMPLEMENTATION_TYPES:
            raise ValueError("proposal_not_compilable_return_to_discussion")

        if implementation_type == "config":
            if not raw_config:
                raise ValueError("config_proposal_requires_config_object")
            formal_files = [
                "strategies/config_versions/<strategy_id>/<version>.json"
            ]
        elif implementation_type == "composite":
            if not components:
                raise ValueError("composite_proposal_requires_components")
            formal_files = [
                "strategies/generated/<strategy_id>_<version>/composition.json"
            ]
        else:
            if not capability:
                raise ValueError("code_proposal_requires_new_capability_spec")
            formal_files = [
                "strategies/generated/<strategy_id>_<version>/strategy_plugin.py"
            ]

        return StrategyImplementationSpec(
            implementation_type=implementation_type,
            base_strategy_id=str(base_strategy_id or ""),
            base_strategy_version=str(base_strategy_version or ""),
            generated_config=config,
            components=components,
            new_capability_spec=capability,
            formal_files=formal_files,
        )

    @staticmethod
    def _canonical_config(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("strategy_config_must_be_object")
        result = dict(CANONICAL_CONFIG_DEFAULTS)
        legacy_top_n = value.get("top_n")
        if legacy_top_n not in [None, ""]:
            result["entry_top_k"] = legacy_top_n
            result["max_positions"] = legacy_top_n
        for raw_key, raw_value in value.items():
            key = LEGACY_CONFIG_FIELDS.get(str(raw_key), str(raw_key))
            result[key] = raw_value
        return result
