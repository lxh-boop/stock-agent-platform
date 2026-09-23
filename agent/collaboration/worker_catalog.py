"""MainAgent-visible Worker capability catalog.

The catalog exposes professional scope and simple business-data names. Private
Tool details remain hidden. Mutation-capable Workers are not eligible for the
normal BUSINESS planning path.
"""
from __future__ import annotations

from typing import Any

from agent.capabilities.data_names import LEGACY_OUTPUT_NAME_MAP, normalize_data_name
from .worker_directory import CapabilityWorkerDirectory


class WorkerDescriptionCatalog:
    def __init__(self, directory: CapabilityWorkerDirectory, capability_registry: Any, *, worker_tool_directory: Any | None = None) -> None:
        self.directory = directory
        self.capability_registry = capability_registry
        self.worker_tool_directory = worker_tool_directory

    def _eligible(self, card: Any, effect_limit: str) -> bool:
        del effect_limit
        return str(card.availability or "available") == "available" and not bool(card.can_mutate)

    @staticmethod
    def _normalize_tool_output_name(value: str) -> str:
        raw = str(value or "").strip()
        mapped = LEGACY_OUTPUT_NAME_MAP.get(raw, raw)
        try:
            return normalize_data_name(mapped)
        except Exception:
            return mapped

    def descriptions(self, *, effect_limit: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for card in self.directory.list():
            if not self._eligible(card, effect_limit):
                continue
            scope = self.capability_registry.aggregate_scope(card.supported_boundary_ids)
            input_patterns = list(scope["accepted_data_patterns"])
            output_patterns = list(scope["produced_data_patterns"])
            input_examples = list(scope["input_data_examples"])
            output_examples = list(scope["output_data_examples"])
            private_tool_output_names: list[str] = []
            if self.worker_tool_directory is not None and list(card.private_tool_ids or []):
                legacy_names = self.worker_tool_directory.semantic_output_slots(
                    card.agent_id, tool_names=list(card.private_tool_ids)
                )
                private_tool_output_names = list(dict.fromkeys(
                    self._normalize_tool_output_name(x) for x in legacy_names if str(x)
                ))
                output_examples.extend(private_tool_output_names)
            rows.append({
                "worker_id": card.worker_id,
                "public_role": card.role,
                "short_description": card.short_description,
                "delegation_description": card.delegation_description or card.short_description,
                "delegate_when": list(card.delegate_when),
                "full_description": card.full_description,
                "capability_tags": list(card.capability_tags),
                "supported_scenarios": list(card.supported_scenarios),
                "unsupported_scenarios": list(card.unsupported_scenarios),
                "limitations": list(card.limitations),
                "escalation_policy": card.escalation_policy,
                "execution_mode": card.execution_mode,
                "execution_stage": card.execution_stage,
                "output_publication_mode": card.output_publication_mode,
                "working_memory_mode": card.working_memory_mode,
                "can_mutate": bool(card.can_mutate),
                "accepted_data_patterns": list(dict.fromkeys(input_patterns)),
                "produced_data_patterns": list(dict.fromkeys(output_patterns)),
                "input_data_examples": list(dict.fromkeys(input_examples)),
                "output_data_examples": list(dict.fromkeys(output_examples)),
                "allowed_acceptance_rule_ids": list(scope["allowed_acceptance_rule_ids"]),
                "accepted_business_parameter_patterns": list(scope.get("accepted_business_parameter_patterns") or []),
                "capability_scope_mode": "worker_level",
                "private_tool_semantic_outputs": private_tool_output_names,
                "private_tool_details_visible_to_main_agent": False,
            })
        return rows

    def capability_catalog(self, *, effect_limit: str) -> list[dict[str, Any]]:
        """Return the compact public Worker catalog for Need/Worker planning.

        This is deliberately a different layer from the Worker-private Tool
        catalog.  It exposes only enough capability identity and semantic output
        names for MainAgent Worker binding; Tool IDs, arguments, schemas and
        private execution details never enter this catalog.
        """
        rows: list[dict[str, Any]] = []
        for card in self.directory.list():
            if not self._eligible(card, effect_limit):
                continue
            scope = self.capability_registry.aggregate_scope(card.supported_boundary_ids)
            output_patterns = list(scope["produced_data_patterns"])
            private_output_names: list[str] = []
            if self.worker_tool_directory is not None and list(card.private_tool_ids or []):
                legacy_names = self.worker_tool_directory.semantic_output_slots(
                    card.agent_id, tool_names=list(card.private_tool_ids)
                )
                private_output_names = list(dict.fromkeys(
                    self._normalize_tool_output_name(x) for x in legacy_names if str(x)
                ))
            output_names = list(dict.fromkeys([
                *output_patterns,
                *private_output_names,
            ]))
            rows.append({
                "worker_id": card.worker_id,
                "role": card.role,
                "summary": " ".join(str(card.short_description or card.delegation_description or "").split())[:240],
                "delegate_when": [
                    " ".join(str(item).split())[:180]
                    for item in (card.delegate_when or [])[:6]
                    if str(item).strip()
                ],
                "supported_scenarios": [
                    " ".join(str(item).split())[:120]
                    for item in (card.supported_scenarios or [])[:6]
                    if str(item).strip()
                ],
                "supports_output_semantics": output_names,
            })
        return rows


__all__ = ["WorkerDescriptionCatalog"]
