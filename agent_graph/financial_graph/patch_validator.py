from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import GraphAuthority, GraphPatch
from .errors import GraphPatchValidationError
from .store import FinancialGraphStore


@dataclass(frozen=True)
class PatchValidationPolicy:
    canonical_sources: frozenset[str] = frozenset(
        {
            "security_master",
            "exchange_master",
            "company_registry",
            "portfolio_repository",
            "approved_user_action",
            "system_migration",
        }
    )
    allow_candidate_terms: bool = True
    require_source_refs: bool = True
    max_objects: int = 1000
    max_assertions: int = 5000
    max_evidence: int = 2000


@dataclass
class PatchValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    normalized_patch: GraphPatch | None = None

    def raise_for_error(self) -> None:
        if not self.valid:
            raise GraphPatchValidationError(";".join(self.errors or ["graph_patch_invalid"]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "patch_id": self.normalized_patch.patch_id if self.normalized_patch else "",
        }


class GraphPatchValidator:
    """The only write gate between Workers and the canonical graph."""

    def __init__(
        self,
        store: FinancialGraphStore,
        *,
        policy: PatchValidationPolicy | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or PatchValidationPolicy()

    @staticmethod
    def _duplicates(values: list[str]) -> list[str]:
        seen: set[str] = set()
        duplicates: list[str] = []
        for value in values:
            if value in seen and value not in duplicates:
                duplicates.append(value)
            seen.add(value)
        return duplicates

    def validate(self, patch: GraphPatch) -> PatchValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        if len(patch.objects) > self.policy.max_objects:
            errors.append("graph_patch_too_many_objects")
        if len(patch.assertions) > self.policy.max_assertions:
            errors.append("graph_patch_too_many_assertions")
        if len(patch.evidence) > self.policy.max_evidence:
            errors.append("graph_patch_too_many_evidence_records")

        for label, ids in {
            "object": [item.object_id for item in patch.objects],
            "identity": [item.identity_id for item in patch.identities],
            "term": [item.term_id for item in patch.terms],
            "assertion": [item.assertion_id for item in patch.assertions],
            "evidence": [item.evidence_id for item in patch.evidence],
        }.items():
            duplicates = self._duplicates(ids)
            if duplicates:
                errors.append(f"duplicate_{label}_ids:{','.join(duplicates[:10])}")

        included_objects = {item.object_id for item in patch.objects}
        included_terms = {item.term_id for item in patch.terms}
        included_evidence = {item.evidence_id for item in patch.evidence}

        external_object_ids = {
            item.object_id for item in patch.identities if item.object_id not in included_objects
        }
        for assertion in patch.assertions:
            if assertion.subject_id not in included_objects:
                external_object_ids.add(assertion.subject_id)
            if assertion.object_id and assertion.object_id not in included_objects:
                external_object_ids.add(assertion.object_id)
        existing_objects = self._existing_ids("GraphObject", "object_id", external_object_ids)
        missing_objects = sorted(external_object_ids - existing_objects)
        if missing_objects:
            errors.append(f"graph_patch_missing_object_refs:{','.join(missing_objects[:20])}")

        external_term_ids = {
            item.predicate_term_id for item in patch.assertions if item.predicate_term_id not in included_terms
        }
        existing_terms = self._existing_ids("GraphTerm", "term_id", external_term_ids)
        missing_terms = sorted(external_term_ids - existing_terms)
        if missing_terms:
            errors.append(f"graph_patch_missing_term_refs:{','.join(missing_terms[:20])}")

        external_evidence_ids: set[str] = set()
        for assertion in patch.assertions:
            external_evidence_ids.update(item for item in assertion.evidence_ids if item not in included_evidence)
            external_evidence_ids.update(item for item in assertion.contradicting_evidence_ids if item not in included_evidence)
        existing_evidence = self._existing_ids("GraphEvidence", "evidence_id", external_evidence_ids)
        missing_evidence = sorted(external_evidence_ids - existing_evidence)
        if missing_evidence:
            errors.append(f"graph_patch_missing_evidence_refs:{','.join(missing_evidence[:20])}")

        if self.policy.require_source_refs and not patch.source_refs:
            errors.append("graph_patch_source_refs_required")

        allowed_canonical = any(source in self.policy.canonical_sources for source in patch.source_refs)
        for obj in patch.objects:
            if obj.authority == GraphAuthority.CANONICAL and not allowed_canonical:
                errors.append(f"canonical_object_from_untrusted_source:{obj.object_id}")
            if not obj.source_refs and self.policy.require_source_refs:
                warnings.append(f"object_source_refs_empty:{obj.object_id}")
        for assertion in patch.assertions:
            if assertion.authority == GraphAuthority.CANONICAL and not allowed_canonical:
                errors.append(f"canonical_assertion_from_untrusted_source:{assertion.assertion_id}")
            if assertion.assertion_class == "claim" and not assertion.evidence_ids:
                errors.append(f"claim_without_evidence:{assertion.assertion_id}")

        patch.validation_status = "validated" if not errors else "rejected"
        return PatchValidationResult(
            valid=not errors,
            errors=errors,
            warnings=warnings,
            normalized_patch=patch,
        )

    def _existing_ids(self, label: str, key: str, values: set[str]) -> set[str]:
        if not values:
            return set()
        if label not in {"GraphObject", "GraphTerm", "GraphEvidence"}:
            raise ValueError("unsupported_graph_label")
        if key not in {"object_id", "term_id", "evidence_id"}:
            raise ValueError("unsupported_graph_key")
        rows = self.store.execute_read(
            f"MATCH (n:{label}) WHERE n.{key} IN $ids RETURN n.{key} AS id",
            {"ids": sorted(values)},
        )
        return {str(row.get("id") or "") for row in rows if row.get("id")}

    def validate_and_apply(self, patch: GraphPatch) -> dict[str, Any]:
        result = self.validate(patch)
        result.raise_for_error()
        applied = self.store.apply_patch(result.normalized_patch or patch)
        return {"validation": result.to_dict(), "applied": applied}
