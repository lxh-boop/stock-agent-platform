from __future__ import annotations

from typing import Any

from .models import CapabilityContract, ContractCompletionReport


class CapabilityContractValidator:
    """Execution-end business contract hook.

    Stage 3.3 intentionally disables deterministic business-content validation.
    The function name and call site are preserved so semantic/business validation
    can be restored later without changing the Worker execution chain.

    Only the already-known Worker execution status is reflected in the returned
    contract reports.  Promised field presence, JSON paths, business emptiness,
    forbidden business outputs, and acceptance-rule semantics are not evaluated.
    """

    def validate(
        self,
        *,
        contracts: list[CapabilityContract],
        produced_data_names: set[str],
        materialized_data: dict[str, Any] | None,
        result_status: str,
        result_payload: dict[str, Any] | None,
        evidence_refs: list[str] | None = None,
    ) -> list[ContractCompletionReport]:
        # Keep the interface and execution-end invocation stable, but deliberately
        # do not inspect business values/keywords/paths in this stage.
        del materialized_data, result_payload

        status_value = str(result_status or "").strip().lower()
        if status_value in {"completed", "proposal_ready"}:
            terminal_status = "completed"
        elif status_value in {"need_context", "waiting_context"}:
            terminal_status = "need_context"
        elif status_value in {"blocked", "not_executed"}:
            terminal_status = "blocked"
        elif status_value == "partial":
            # PARTIAL here reflects execution state, not a business-content
            # judgment.  canonicalize_completion_report keeps the Worker partial.
            terminal_status = "business_insufficient"
        else:
            terminal_status = "failed"

        produced = {str(name) for name in produced_data_names if str(name)}
        reports: list[ContractCompletionReport] = []
        for contract in contracts:
            promised = set(contract.output_data_names())
            reports.append(
                ContractCompletionReport(
                    contract_id=contract.contract_id,
                    status=terminal_status,
                    satisfied_outputs=sorted(promised.intersection(produced)),
                    missing_outputs=[],
                    failed_acceptance_rules=[],
                    evidence_refs=list(evidence_refs or []),
                    limitations=[],
                )
            )
        return reports
