from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.session.pending_action_store import get_pending_plan
from database.repositories.agent_repository import AgentRepository
from database.repositories.portfolio_repository import PortfolioRepository
from database.repositories.strategy_workflow_repository import (
    StrategyWorkflowRepository,
)
from strategies.binding_repository import StrategyBindingRepository
from strategies.registry import StrategyRegistry


SENSITIVE_AUDIT_FIELDS = {
    "confirmation_token",
    "confirmation_token_hash",
}


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _safe(item)
            for key, item in value.items()
            if str(key) not in SENSITIVE_AUDIT_FIELDS
        }
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


class StrategyAuditService:
    """Reconstruct the strategy lifecycle from its stable identifiers."""

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        output_dir: str | Path = "outputs",
    ) -> None:
        self.db_path = db_path
        self.output_dir = Path(output_dir)
        self.workflow = StrategyWorkflowRepository(db_path)
        self.agent = AgentRepository(db_path)
        self.bindings = StrategyBindingRepository(db_path)
        self.portfolio = PortfolioRepository(db_path)
        self.registry = StrategyRegistry(
            output_dir=output_dir,
            db_path=db_path,
        )

    def _load_plan(
        self,
        *,
        user_id: str,
        plan_id: str,
    ) -> dict[str, Any] | None:
        if not plan_id:
            return None
        plan = get_pending_plan(
            user_id,
            plan_id,
            self.output_dir,
        )
        if plan:
            return plan
        persisted = self.agent.get_action_proposal(plan_id)
        if not persisted or str(persisted.get("user_id") or "") != user_id:
            return None
        metadata = dict(persisted.get("metadata") or {})
        return {
            **persisted,
            "intent": str(
                metadata.get("intent")
                or persisted.get("operation_type")
                or ""
            ),
            "run_id": str(
                persisted.get("run_id")
                or metadata.get("source_run_id")
                or ""
            ),
            "execution_status": str(
                metadata.get("execution_status") or ""
            ),
        }

    def trace(
        self,
        *,
        user_id: str,
        proposal_id: str = "",
        implementation_id: str = "",
        plan_id: str = "",
        commit_id: str = "",
        binding_id: str = "",
        run_id: str = "",
        conversation_id: str = "",
    ) -> dict[str, Any]:
        user_id = str(user_id or "default")
        if commit_id.startswith("commit_") and not plan_id:
            plan_id = commit_id.removeprefix("commit_")
        if run_id and not binding_id:
            run_executions = [
                item
                for item in self.portfolio.list_strategy_execution_history(
                    user_id
                )
                if str(item.get("run_id") or "") == run_id
            ]
            if run_executions:
                binding_id = str(
                    run_executions[-1].get("binding_id") or ""
                )
        if conversation_id and not proposal_id:
            scoped_proposals = self.workflow.list_proposals(
                user_id=user_id,
                conversation_id=conversation_id,
                limit=1,
            )
            if scoped_proposals:
                proposal_id = str(
                    scoped_proposals[0].get("proposal_id") or ""
                )
        implementation = (
            self.workflow.get_implementation(
                implementation_id,
                user_id=user_id,
            )
            if implementation_id
            else None
        )
        if implementation and not proposal_id:
            proposal_id = str(implementation.get("proposal_id") or "")
        proposal = (
            self.workflow.get_proposal(
                proposal_id,
                user_id=user_id,
            )
            if proposal_id
            else None
        )
        proposal_versions = (
            self.workflow.list_versions(proposal_id)
            if proposal
            else []
        )
        if proposal and not conversation_id:
            conversation_id = str(
                proposal.get("conversation_id") or ""
            )
        plan = (
            self._load_plan(user_id=user_id, plan_id=plan_id)
            if plan_id
            else None
        )
        if plan:
            implementation_id = (
                implementation_id
                or str(plan.get("implementation_id") or "")
            )
            proposal_id = proposal_id or str(
                plan.get("proposal_id") or ""
            )
            binding_id = binding_id or str(
                plan.get("binding_id") or ""
            )
            run_id = run_id or str(plan.get("run_id") or "")
            conversation_id = conversation_id or str(
                plan.get("conversation_id") or ""
            )
        if implementation_id and not implementation:
            implementation = self.workflow.get_implementation(
                implementation_id,
                user_id=user_id,
            )
        if implementation and not proposal_id:
            proposal_id = str(implementation.get("proposal_id") or "")
        if proposal_id and not proposal:
            proposal = self.workflow.get_proposal(
                proposal_id,
                user_id=user_id,
            )
            proposal_versions = (
                self.workflow.list_versions(proposal_id)
                if proposal
                else []
            )
        if proposal and not conversation_id:
            conversation_id = str(proposal.get("conversation_id") or "")
        if proposal_id and not implementation:
            scoped_implementations = [
                item
                for item in self.workflow.list_implementations(
                    user_id=user_id,
                )
                if str(item.get("proposal_id") or "") == proposal_id
            ]
            if scoped_implementations:
                implementation = scoped_implementations[0]
                implementation_id = str(
                    implementation.get("implementation_id") or ""
                )
        registration_candidate = None
        for manifest in reversed(
            self.registry.list(include_archived=True)
        ):
            metadata = dict(manifest.metadata)
            if manifest.created_by != user_id:
                continue
            if (
                implementation_id
                and str(metadata.get("implementation_id") or "")
                == implementation_id
            ) or (
                proposal_id
                and str(metadata.get("proposal_id") or "")
                == proposal_id
            ) or (
                plan_id
                and str(metadata.get("source_plan_id") or "")
                == plan_id
            ):
                registration_candidate = manifest
                break
        if registration_candidate and not binding_id:
            binding_rows = self.bindings.store.list(
                "strategy_bindings",
                filters={"user_id": user_id},
                order_by="created_at",
                descending=True,
            )
            matching_binding = next(
                (
                    row
                    for row in binding_rows
                    if str(row.get("strategy_id") or "")
                    == registration_candidate.strategy_id
                    and str(row.get("strategy_version") or "")
                    == registration_candidate.version
                ),
                None,
            )
            if matching_binding:
                binding_id = str(
                    matching_binding.get("binding_id") or ""
                )
        binding = (
            self.bindings.get(binding_id, user_id=user_id)
            if binding_id
            else None
        )
        registration = registration_candidate or (
            self.registry.get(
                binding.strategy_id,
                binding.strategy_version,
            )
            if binding
            else None
        )
        registration_metadata = (
            dict(registration.metadata)
            if registration
            else {}
        )
        if registration_metadata:
            implementation_id = implementation_id or str(
                registration_metadata.get("implementation_id") or ""
            )
            proposal_id = proposal_id or str(
                registration_metadata.get("proposal_id") or ""
            )
        if implementation_id and not implementation:
            implementation = self.workflow.get_implementation(
                implementation_id,
                user_id=user_id,
            )
        if implementation and not proposal_id:
            proposal_id = str(implementation.get("proposal_id") or "")
        if proposal_id and not proposal:
            proposal = self.workflow.get_proposal(
                proposal_id,
                user_id=user_id,
            )
            proposal_versions = (
                self.workflow.list_versions(proposal_id)
                if proposal
                else []
            )
        if proposal and not conversation_id:
            conversation_id = str(proposal.get("conversation_id") or "")
        plan_intent = str(
            (plan or {}).get("intent")
            or (plan or {}).get("operation_type")
            or ""
        )
        application_plan_id = str(
            registration_metadata.get("source_plan_id") or ""
        )
        activation_plan_id = (
            str(binding.source_plan_id)
            if binding
            else ""
        )
        if plan_id and plan_intent == "apply_strategy_implementation":
            application_plan_id = plan_id
        elif plan_id and plan_intent in {
            "activate_strategy_binding",
            "rollback_strategy_binding",
        }:
            activation_plan_id = plan_id
        application_plan = (
            self._load_plan(
                user_id=user_id,
                plan_id=application_plan_id,
            )
            if application_plan_id
            else None
        )
        activation_plan = (
            self._load_plan(
                user_id=user_id,
                plan_id=activation_plan_id,
            )
            if activation_plan_id
            else None
        )
        run = (
            self.agent.store.get(
                "agent_runs",
                {"run_id": run_id, "user_id": user_id},
            )
            if run_id
            else None
        )
        conversation = (
            self.agent.get_conversation(conversation_id)
            if conversation_id
            else None
        )
        if conversation and str(conversation.get("user_id") or "") != user_id:
            conversation = None
        messages = (
            self.agent.list_messages(conversation_id, limit=200)
            if conversation
            else []
        )
        commit_key = commit_id or (f"commit_{plan_id}" if plan_id else "")
        commit = (
            self.agent.store.get(
                "action_commits",
                {
                    "commit_id": commit_key,
                    "user_id": user_id,
                },
            )
            if commit_key
            else None
        )
        approvals = (
            self.agent.store.list(
                "action_approvals",
                filters={
                    "plan_id": plan_id,
                    "user_id": user_id,
                },
                order_by="created_at",
            )
            if plan_id
            else []
        )
        application_approvals = (
            self.agent.store.list(
                "action_approvals",
                filters={
                    "plan_id": application_plan_id,
                    "user_id": user_id,
                },
                order_by="created_at",
            )
            if application_plan_id
            else []
        )
        activation_approvals = (
            self.agent.store.list(
                "action_approvals",
                filters={
                    "plan_id": activation_plan_id,
                    "user_id": user_id,
                },
                order_by="created_at",
            )
            if activation_plan_id
            else []
        )
        application_commit = (
            self.agent.store.get(
                "action_commits",
                {
                    "commit_id": f"commit_{application_plan_id}",
                    "user_id": user_id,
                },
            )
            if application_plan_id
            else None
        )
        activation_commit = (
            self.agent.store.get(
                "action_commits",
                {
                    "commit_id": f"commit_{activation_plan_id}",
                    "user_id": user_id,
                },
            )
            if activation_plan_id
            else None
        )
        executions = self.portfolio.list_strategy_execution_history(user_id)
        if binding_id:
            executions = [
                item
                for item in executions
                if str(item.get("binding_id") or "") == binding_id
            ]
        elif run_id:
            executions = [
                item
                for item in executions
                if str(item.get("run_id") or "") == run_id
            ]
        artifact_manifest: dict[str, Any] = {}
        if implementation:
            root = Path(str(implementation.get("artifact_root") or ""))
            manifest_path = root / "artifact_manifest.json"
            if manifest_path.exists():
                import json

                try:
                    artifact_manifest = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )
                except Exception:
                    artifact_manifest = {}
        trace = {
            "ids": {
                "proposal_id": proposal_id,
                "implementation_id": implementation_id,
                "plan_id": plan_id,
                "commit_id": commit_key,
                "binding_id": binding_id,
                "run_id": run_id,
                "conversation_id": conversation_id,
                "application_plan_id": application_plan_id,
                "activation_plan_id": activation_plan_id,
            },
            "original_request": (
                proposal.get("original_request") if proposal else ""
            ),
            "proposal": proposal or {},
            "proposal_versions": proposal_versions,
            "locked_version": (
                implementation.get("proposal_version")
                if implementation
                else None
            ),
            "implementation": implementation or {},
            "artifact_manifest": artifact_manifest,
            "registration_result": (
                registration.to_dict()
                if registration
                else {}
            ),
            "requested_plan": plan or {},
            "requested_plan_approvals": approvals,
            "requested_plan_commit": commit or {},
            "application_plan": application_plan or {},
            "application_approvals": application_approvals,
            "application_commit": application_commit or {},
            "activation_plan": activation_plan or {},
            "activation_approvals": activation_approvals,
            "activation_commit": activation_commit or {},
            "binding": binding.to_dict() if binding else {},
            "actual_strategy_executions": executions,
            "conversation": conversation or {},
            "conversation_messages": messages,
            "agent_run": run or {},
        }
        trace["complete_links"] = {
            key: bool(value)
            for key, value in {
                "proposal": trace["proposal"],
                "implementation": trace["implementation"],
                "application_plan": trace["application_plan"],
                "application_commit": trace["application_commit"],
                "registration": trace["registration_result"],
                "activation_plan": trace["activation_plan"],
                "activation_commit": trace["activation_commit"],
                "binding": trace["binding"],
                "execution": trace["actual_strategy_executions"],
                "conversation": trace["conversation"],
                "run": trace["agent_run"],
            }.items()
        }
        return _safe(trace)
