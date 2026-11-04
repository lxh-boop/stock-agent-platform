"""Deterministic business-Graph mutation adapter for approved Proposals."""
from __future__ import annotations

from typing import Any

from agent_graph.session.proposals import ProposalArtifact


class BusinessGraphMutationAdapter:
    mutation_scope = "graph.business.write"
    can_mutate = True
    execution_stage = "mutation"

    def __init__(self, validator: Any) -> None:
        self.validator = validator

    def execute(self, *, proposal: ProposalArtifact) -> dict[str, Any]:
        if proposal.proposal_type != "business_graph_mutation":
            return {"success": False, "code": "unsupported_graph_proposal_type", "business_effect_applied": False}
        if proposal.status.value != "executing":
            return {"success": False, "code": "proposal_not_executing", "business_effect_applied": False}
        patch_payload = (proposal.payload or {}).get("graph_patch")
        if not isinstance(patch_payload, dict) or not patch_payload:
            return {"success": False, "code": "graph_patch_required", "business_effect_applied": False}

        from agent_graph.financial_graph.contracts import GraphPatch

        result = self.validator.validate_and_apply(GraphPatch.from_dict(dict(patch_payload)))
        if hasattr(result, "to_dict"):
            result = result.to_dict()
        data = dict(result or {}) if isinstance(result, dict) else {"result": str(result)}
        success = bool(data.get("success", True))
        return {
            "success": success,
            "code": "graph_commit_completed" if success else "graph_commit_failed",
            "message": str(data.get("message") or ""),
            "data": data,
            "errors": [] if success else [str(data.get("code") or "graph_commit_failed")],
            "warnings": [],
            "business_effect_applied": success,
            "canonical_proposal_id": proposal.proposal_id,
            "canonical_proposal_version": proposal.current_version,
        }


__all__ = ["BusinessGraphMutationAdapter"]
