"""Deterministic write executor for approved proposals.

WRITE never calls the LLM. It only resolves and executes an existing canonical
Proposal after explicit user authorization (confirm/reject/cancel), with
owner/scope validation, idempotent action requests, and CAS execution claims.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agent_graph.session.proposals import ProposalArtifact, ProposalStatus, ProposalStore, ProposalStoreError
from .graph_business_mutation import BusinessGraphMutationAdapter
from .portfolio_mutation import PortfolioMutationAdapter


_PROPOSAL_RE = re.compile(r"\b(proposal_[0-9a-fA-F]{12,64})\b")


class ProposalResolutionError(RuntimeError):
    pass


class ProposalResolver:
    def __init__(self, store: ProposalStore) -> None:
        self.store = store

    @staticmethod
    def _context_id(context: dict[str, Any] | None) -> str:
        raw = dict(context or {})
        for key in ("proposal_id", "pending_proposal_id"):
            value = str(raw.get(key) or "").strip()
            if value.startswith("proposal_"):
                return value
        return ""

    def resolve(self, *, query: str, user_id: str, session_id: str, context: dict[str, Any] | None) -> ProposalArtifact:
        match = _PROPOSAL_RE.search(str(query or ""))
        proposal_id = match.group(1) if match else self._context_id(context)
        if proposal_id:
            proposal = self.store.get(proposal_id)
            if proposal is None:
                raise ProposalResolutionError("proposal_not_found")
            if proposal.user_id != str(user_id or "default"):
                raise ProposalResolutionError("proposal_owner_mismatch")
            return proposal
        candidates = self.store.list_pending(user_id=user_id, session_id=session_id, limit=3)
        if not candidates:
            raise ProposalResolutionError("pending_proposal_not_found")
        if len(candidates) > 1:
            ids = ",".join(item.proposal_id for item in candidates[:3])
            raise ProposalResolutionError("multiple_pending_proposals:" + ids)
        return candidates[0]


class ApprovalGuard:
    @staticmethod
    def validate_scope(proposal: ProposalArtifact, *, user_id: str, session_id: str) -> None:
        if proposal.user_id != str(user_id or "default"):
            raise ProposalStoreError("proposal_owner_mismatch")
        if proposal.session_id and str(session_id or "") and proposal.session_id != str(session_id or ""):
            raise ProposalStoreError("proposal_scope_mismatch")

    @classmethod
    def validate_for_execution(cls, proposal: ProposalArtifact, *, user_id: str, session_id: str) -> None:
        cls.validate_scope(proposal, user_id=user_id, session_id=session_id)
        if proposal.status != ProposalStatus.PENDING_APPROVAL:
            raise ProposalStoreError(f"proposal_not_pending:{proposal.status.value}")
        if not proposal.current_payload_hash:
            raise ProposalStoreError("proposal_payload_hash_missing")
        if proposal.proposal_type not in {"portfolio_mutation", "business_graph_mutation"}:
            raise ProposalStoreError(f"mutation_capability_unavailable:{proposal.proposal_type}")


class MutationCapabilityResolver:
    def __init__(self, *, graph_validator: Any = None) -> None:
        self.portfolio = PortfolioMutationAdapter()
        self.graph = BusinessGraphMutationAdapter(graph_validator) if graph_validator is not None else None

    def execute(
        self,
        *,
        proposal: ProposalArtifact,
        user_id: str,
        session_id: str,
        run_id: str,
        output_dir: str | Path,
        db_path: str | Path | None,
    ) -> dict[str, Any]:
        if proposal.proposal_type == "portfolio_mutation":
            return self.portfolio.execute(
                proposal=proposal,
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                output_dir=output_dir,
                db_path=db_path,
            )
        if proposal.proposal_type == "business_graph_mutation":
            if self.graph is None:
                return {"success": False, "code": "graph_mutation_runtime_unavailable", "business_effect_applied": False}
            return self.graph.execute(proposal=proposal)
        return {"success": False, "code": "mutation_capability_unavailable", "business_effect_applied": False}


class WriteRequestExecutor:
    """One deterministic WRITE request. No LLM and no MainAgent dependency."""

    def __init__(
        self,
        *,
        output_dir: str | Path,
        db_path: str | Path | None,
        graph_validator: Any = None,
    ) -> None:
        self.output_dir = output_dir
        self.db_path = db_path
        self.store = ProposalStore(output_dir=output_dir, db_path=db_path)
        self.resolver = ProposalResolver(self.store)
        self.guard = ApprovalGuard()
        self.capabilities = MutationCapabilityResolver(graph_validator=graph_validator)

    def execute(
        self,
        *,
        action_type: str,
        query: str,
        user_id: str,
        session_id: str,
        run_id: str,
        context: dict[str, Any] | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        action = str(action_type or "").strip().lower()
        if action not in {"confirm_execute", "reject", "cancel"}:
            return self._result(False, "unsupported_write_action", action=action)
        try:
            proposal = self.resolver.resolve(
                query=query,
                user_id=user_id,
                session_id=session_id,
                context=context,
            )
        except ProposalResolutionError as exc:
            code = str(exc)
            waiting = code.startswith("multiple_pending_proposals") or code == "pending_proposal_not_found"
            return self._result(
                False,
                code,
                action=action,
                status="waiting_user_input" if waiting else "failed",
                clarification_question=(
                    "存在多个待审批方案，请明确 proposal_id。"
                    if code.startswith("multiple_pending_proposals")
                    else "当前没有唯一可确认的待审批方案。"
                    if code == "pending_proposal_not_found"
                    else ""
                ),
            )

        action_request: dict[str, Any] | None = None
        try:
            self.guard.validate_scope(proposal, user_id=user_id, session_id=session_id)
            effective_key = str(idempotency_key or "").strip() or (
                f"{proposal.proposal_id}:{proposal.current_version}:{action}"
            )
            action_request, created = self.store.begin_action(
                proposal=proposal,
                action_type=action,
                user_id=str(user_id or "default"),
                session_id=str(session_id or ""),
                idempotency_key=effective_key,
            )
            if not created:
                previous = dict(action_request.get("result") or {})
                if previous:
                    previous["idempotent_replay"] = True
                    return previous
                return self._result(
                    False,
                    "proposal_action_in_progress",
                    action=action,
                    proposal=proposal,
                    status="in_progress",
                )
        except ProposalStoreError as exc:
            return self._result(False, str(exc), action=action, proposal=proposal)

        if action in {"reject", "cancel"}:
            try:
                target = ProposalStatus.REJECTED if action == "reject" else ProposalStatus.CANCELLED
                updated = self.store.transition(proposal_id=proposal.proposal_id, user_id=user_id, target=target)
                result = self._result(
                    True,
                    "proposal_rejected" if action == "reject" else "proposal_cancelled",
                    action=action,
                    proposal=updated,
                    business_effect_applied=False,
                )
                self._complete_action(action_request, result)
                return result
            except ProposalStoreError as exc:
                result = self._result(False, str(exc), action=action, proposal=proposal)
                self._complete_action(action_request, result)
                return result

        try:
            self.guard.validate_for_execution(proposal, user_id=user_id, session_id=session_id)
            executing = self.store.claim_for_execution(
                proposal_id=proposal.proposal_id,
                user_id=user_id,
                expected_version=proposal.current_version,
                expected_payload_hash=proposal.current_payload_hash,
                approved_by=user_id,
                approval_run_id=run_id,
            )
            mutation = self.capabilities.execute(
                proposal=executing,
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                output_dir=self.output_dir,
                db_path=self.db_path,
            )
            if bool(mutation.get("success")) and bool(mutation.get("business_effect_applied")):
                final = self.store.transition(
                    proposal_id=executing.proposal_id,
                    user_id=user_id,
                    target=ProposalStatus.EXECUTED,
                )
                result = self._result(
                    True,
                    str(mutation.get("code") or "executed"),
                    action=action,
                    proposal=final,
                    mutation=mutation,
                    business_effect_applied=True,
                )
                self._complete_action(action_request, result)
                return result
            failed = self.store.transition(
                proposal_id=executing.proposal_id,
                user_id=user_id,
                target=ProposalStatus.FAILED,
            )
            result = self._result(
                False,
                str(mutation.get("code") or "mutation_failed"),
                action=action,
                proposal=failed,
                mutation=mutation,
                business_effect_applied=False,
            )
            self._complete_action(action_request, result)
            return result
        except ProposalStoreError as exc:
            result = self._result(False, str(exc), action=action, proposal=proposal)
            self._complete_action(action_request, result)
            return result
        except Exception as exc:
            # Unexpected execution exceptions are never converted into a new
            # plan or sent to MainAgent. The WRITE request stops safely.
            try:
                current = self.store.get(proposal.proposal_id)
                if current and current.status == ProposalStatus.EXECUTING:
                    self.store.transition(
                        proposal_id=proposal.proposal_id,
                        user_id=user_id,
                        target=ProposalStatus.FAILED,
                    )
            except Exception:
                pass
            result = self._result(
                False,
                f"write_runtime_error:{type(exc).__name__}",
                action=action,
                proposal=proposal,
            )
            self._complete_action(action_request, result)
            return result

    def _complete_action(self, action_request: dict[str, Any] | None, result: dict[str, Any]) -> None:
        if not action_request:
            return
        self.store.complete_action(
            action_request_id=str(action_request.get("action_request_id") or ""),
            result=result,
        )

    @staticmethod
    def _result(
        success: bool,
        code: str,
        *,
        action: str = "",
        status: str | None = None,
        proposal: ProposalArtifact | None = None,
        mutation: dict[str, Any] | None = None,
        business_effect_applied: bool = False,
        clarification_question: str = "",
    ) -> dict[str, Any]:
        effective_status = status or ("completed" if success else "failed")
        return {
            "success": bool(success),
            "request_type": "write",
            "status": effective_status,
            "requested_effect": "write",
            "business_effect_applied": bool(business_effect_applied),
            "outcome": str(code or ""),
            "action_type": str(action or ""),
            "proposal_id": proposal.proposal_id if proposal else "",
            "proposal_version": proposal.current_version if proposal else 0,
            "proposal_status": proposal.status.value if proposal else "",
            "proposal_payload_hash": proposal.current_payload_hash if proposal else "",
            "mutation_status": str((mutation or {}).get("code") or ""),
            "mutation_result": dict(mutation or {}),
            "need_clarification": effective_status == "waiting_user_input",
            "clarification_question": str(clarification_question or ""),
            "errors": [] if success else [str(code or "write_failed")],
            "warnings": [],
        }


__all__ = [
    "ApprovalGuard",
    "MutationCapabilityResolver",
    "ProposalResolver",
    "WriteRequestExecutor",
    "execute_control_action",
]


def execute_control_action(
    *,
    action: str,
    query: str = "",
    proposal_id: str = "",
    idempotency_key: str = "",
    user_id: str = "default",
    session_id: str = "",
    run_id: str = "",
    language: str = "zh",
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """UI-facing control entry: normalize the action and delegate to the executor."""

    del language
    normalized = str(action or "").strip().lower()
    action_type = {
        "confirm": "confirm_execute",
        "confirm_execute": "confirm_execute",
        "reject": "reject",
        "cancel": "cancel",
    }.get(normalized)
    if action_type is None:
        raise ValueError(f"unsupported_write_action:{normalized}")

    merged_context = dict(context or {})
    resolved_proposal_id = str(proposal_id or "").strip()
    if resolved_proposal_id:
        merged_context["proposal_id"] = resolved_proposal_id

    result = WriteRequestExecutor(output_dir=output_dir, db_path=db_path).execute(
        action_type=action_type,
        query=str(query or ""),
        user_id=str(user_id or "default"),
        session_id=str(session_id or ""),
        run_id=str(run_id or ""),
        context=merged_context,
        idempotency_key=str(idempotency_key or ""),
    )
    result.setdefault("answer", str(result.get("outcome") or ""))
    return result
