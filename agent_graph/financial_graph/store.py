from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock
from typing import Any, ClassVar, Iterable, Iterator, Protocol
from typing import Any, Iterable, Iterator, Protocol

from .contracts import (
    GraphAssertionRecord,
    GraphEvidenceRecord,
    GraphIdentityRecord,
    GraphObjectRecord,
    GraphPatch,
    GraphTermRecord,
)
from .errors import GraphConfigurationError, GraphUnavailableError
from .schema import CONSTRAINTS, FULLTEXT_INDEXES, INDEXES
from .settings import Neo4jSettings


def normalize_identity_value(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().split())


def normalize_term_name(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _record_to_dict(record: Any) -> dict[str, Any]:
    if hasattr(record, "data") and callable(record.data):
        return dict(record.data())
    return dict(record)


class FinancialGraphStore(Protocol):
    graph_id: str

    def verify_connectivity(self) -> None: ...
    def ensure_schema(self) -> None: ...
    def execute_read(self, cypher: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...
    def execute_write(self, cypher: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...
    def apply_patch(self, patch: GraphPatch) -> dict[str, Any]: ...
    def close(self) -> None: ...


@dataclass
class Neo4jFinancialGraphStore:
    settings: Neo4jSettings

    _schema_ready_keys: ClassVar[set[tuple[str, str, str]]] = set()
    _schema_lock: ClassVar[RLock] = RLock()

    def __post_init__(self) -> None:
        self.settings.validate()
        self.graph_id = self.settings.graph_id
        self._driver = None
        self._lock = RLock()

    def _get_driver(self):
        with self._lock:
            if self._driver is not None:
                return self._driver
            try:
                from neo4j import GraphDatabase
            except Exception as exc:
                raise GraphConfigurationError(
                    "The official Neo4j Python driver is not installed. Install `neo4j>=6,<7`."
                ) from exc
            kwargs: dict[str, Any] = {
                "max_connection_pool_size": self.settings.max_connection_pool_size,
                "connection_timeout": self.settings.connection_timeout_seconds,
            }
            if self.settings.encrypted is not None:
                kwargs["encrypted"] = self.settings.encrypted
            self._driver = GraphDatabase.driver(
                self.settings.uri,
                auth=(self.settings.username, self.settings.password),
                **kwargs,
            )
            return self._driver

    @contextmanager
    def _session(self, *, write: bool) -> Iterator[Any]:
        driver = self._get_driver()
        try:
            from neo4j import READ_ACCESS, WRITE_ACCESS
            access_mode = WRITE_ACCESS if write else READ_ACCESS
            with driver.session(database=self.settings.database, default_access_mode=access_mode) as session:
                yield session
        except Exception as exc:
            raise GraphUnavailableError(f"neo4j_session_failed:{type(exc).__name__}:{exc}") from exc

    def verify_connectivity(self) -> None:
        try:
            self._get_driver().verify_connectivity()
        except Exception as exc:
            raise GraphUnavailableError(f"neo4j_connectivity_failed:{type(exc).__name__}:{exc}") from exc

    def execute_read(self, cypher: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with self._session(write=False) as session:
            try:
                result = session.run(str(cypher), dict(parameters or {}))
                return [_record_to_dict(record) for record in result]
            except Exception as exc:
                raise GraphUnavailableError(f"neo4j_read_failed:{type(exc).__name__}:{exc}") from exc

    def execute_write(self, cypher: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with self._session(write=True) as session:
            try:
                def work(tx):
                    return [_record_to_dict(record) for record in tx.run(str(cypher), dict(parameters or {}))]

                return session.execute_write(work)
            except Exception as exc:
                raise GraphUnavailableError(f"neo4j_write_failed:{type(exc).__name__}:{exc}") from exc

    def ensure_schema(self) -> None:
        cache_key = (
            str(self.settings.uri),
            str(self.settings.database),
            str(self.settings.graph_id),
        )
        with self._schema_lock:
            if cache_key in self._schema_ready_keys:
                return

        for statement in (*CONSTRAINTS, *INDEXES):
            self.execute_write(statement)
        for statement in FULLTEXT_INDEXES:
            try:
                self.execute_write(statement)
            except GraphUnavailableError:
                # Full-text syntax differs on older Neo4j installations. Exact
                # identity resolution and normal indexes remain authoritative.
                continue

        with self._schema_lock:
            self._schema_ready_keys.add(cache_key)

    def _upsert_objects(self, rows: Iterable[GraphObjectRecord]) -> int:
        payload = [
            {
                **row.to_dict(),
                "authority": row.authority.value,
                "alias_text": " | ".join(row.aliases),
                "properties_json": _json(row.properties),
            }
            for row in rows
        ]
        if not payload:
            return 0
        self.execute_write(
            """
            UNWIND $rows AS row
            MERGE (n:GraphObject {object_id: row.object_id})
            ON CREATE SET n.created_at = datetime()
            SET n.name = row.name,
                n.object_class = row.object_class,
                n.aliases = row.aliases,
                n.alias_text = row.alias_text,
                n.properties_json = row.properties_json,
                n.authority = row.authority,
                n.confidence = row.confidence,
                n.source_refs = row.source_refs,
                n.valid_from = row.valid_from,
                n.valid_to = row.valid_to,
                n.observed_at = row.observed_at,
                n.updated_at = datetime()
            RETURN count(n) AS count
            """,
            {"rows": payload},
        )
        return len(payload)

    def _upsert_identities(self, rows: Iterable[GraphIdentityRecord]) -> int:
        payload = [
            {
                **row.to_dict(),
                "normalized_value": normalize_identity_value(row.value),
            }
            for row in rows
        ]
        if not payload:
            return 0
        self.execute_write(
            """
            UNWIND $rows AS row
            MATCH (target:GraphObject {object_id: row.object_id})
            MERGE (identity:GraphIdentity {identity_id: row.identity_id})
            ON CREATE SET identity.created_at = datetime()
            SET identity.namespace = row.namespace,
                identity.value = row.value,
                identity.normalized_value = row.normalized_value,
                identity.identity_type = row.identity_type,
                identity.is_primary = row.is_primary,
                identity.confidence = row.confidence,
                identity.source_refs = row.source_refs,
                identity.valid_from = row.valid_from,
                identity.valid_to = row.valid_to,
                identity.updated_at = datetime()
            MERGE (identity)-[:IDENTIFIES]->(target)
            RETURN count(identity) AS count
            """,
            {"rows": payload},
        )
        return len(payload)

    def _upsert_terms(self, rows: Iterable[GraphTermRecord]) -> int:
        payload = [
            {
                **row.to_dict(),
                "normalized_name": normalize_term_name(row.name),
            }
            for row in rows
        ]
        if not payload:
            return 0
        self.execute_write(
            """
            UNWIND $rows AS row
            MERGE (term:GraphTerm {term_id: row.term_id})
            ON CREATE SET term.created_at = datetime()
            SET term.name = row.name,
                term.normalized_name = row.normalized_name,
                term.term_kind = row.term_kind,
                term.description = row.description,
                term.status = row.status,
                term.source_refs = row.source_refs,
                term.updated_at = datetime()
            WITH term, row
            UNWIND CASE WHEN size(row.parent_term_ids) = 0 THEN [null] ELSE row.parent_term_ids END AS parent_id
            OPTIONAL MATCH (parent:GraphTerm {term_id: parent_id})
            FOREACH (_ IN CASE WHEN parent IS NULL THEN [] ELSE [1] END | MERGE (term)-[:SUBCLASS_OF]->(parent))
            RETURN count(DISTINCT term) AS count
            """,
            {"rows": payload},
        )
        return len(payload)

    def _upsert_evidence(self, rows: Iterable[GraphEvidenceRecord]) -> int:
        payload = [{**row.to_dict(), "properties_json": _json(row.properties)} for row in rows]
        if not payload:
            return 0
        self.execute_write(
            """
            UNWIND $rows AS row
            MERGE (e:GraphEvidence {evidence_id: row.evidence_id})
            ON CREATE SET e.created_at = datetime()
            SET e.evidence_class = row.evidence_class,
                e.title = row.title,
                e.source = row.source,
                e.content_ref = row.content_ref,
                e.content_hash = row.content_hash,
                e.published_at = row.published_at,
                e.observed_at = row.observed_at,
                e.reliability = row.reliability,
                e.properties_json = row.properties_json,
                e.updated_at = datetime()
            RETURN count(e) AS count
            """,
            {"rows": payload},
        )
        return len(payload)

    def _upsert_assertions(self, rows: Iterable[GraphAssertionRecord]) -> int:
        payload = [
            {
                **row.to_dict(),
                "authority": row.authority.value,
                "literal_json": _json(row.literal_value),
                "properties_json": _json(row.properties),
            }
            for row in rows
        ]
        if not payload:
            return 0
        self.execute_write(
            """
            UNWIND $rows AS row
            MATCH (subject:GraphObject {object_id: row.subject_id})
            MATCH (predicate:GraphTerm {term_id: row.predicate_term_id})
            OPTIONAL MATCH (object:GraphObject {object_id: row.object_id})
            MERGE (a:GraphAssertion {assertion_id: row.assertion_id})
            ON CREATE SET a.created_at = datetime()
            SET a.subject_id = row.subject_id,
                a.predicate_term_id = row.predicate_term_id,
                a.object_id = row.object_id,
                a.literal_json = row.literal_json,
                a.assertion_class = row.assertion_class,
                a.authority = row.authority,
                a.polarity = row.polarity,
                a.modality = row.modality,
                a.confidence = row.confidence,
                a.properties_json = row.properties_json,
                a.source_refs = row.source_refs,
                a.valid_from = row.valid_from,
                a.valid_to = row.valid_to,
                a.observed_at = row.observed_at,
                a.updated_at = datetime()
            MERGE (a)-[:SUBJECT]->(subject)
            MERGE (a)-[:PREDICATE]->(predicate)
            FOREACH (_ IN CASE WHEN object IS NULL THEN [] ELSE [1] END | MERGE (a)-[:OBJECT]->(object))
            WITH a, row
            UNWIND CASE WHEN size(row.evidence_ids) = 0 THEN [null] ELSE row.evidence_ids END AS evidence_id
            OPTIONAL MATCH (e:GraphEvidence {evidence_id: evidence_id})
            FOREACH (_ IN CASE WHEN e IS NULL THEN [] ELSE [1] END | MERGE (a)-[:SUPPORTED_BY]->(e))
            WITH a, row
            UNWIND CASE WHEN size(row.contradicting_evidence_ids) = 0 THEN [null] ELSE row.contradicting_evidence_ids END AS evidence_id
            OPTIONAL MATCH (e2:GraphEvidence {evidence_id: evidence_id})
            FOREACH (_ IN CASE WHEN e2 IS NULL THEN [] ELSE [1] END | MERGE (a)-[:CONTRADICTED_BY]->(e2))
            RETURN count(DISTINCT a) AS count
            """,
            {"rows": payload},
        )
        return len(payload)

    def apply_patch(self, patch: GraphPatch) -> dict[str, Any]:
        counts = {
            "objects": self._upsert_objects(patch.objects),
            "terms": self._upsert_terms(patch.terms),
            "identities": self._upsert_identities(patch.identities),
            "evidence": self._upsert_evidence(patch.evidence),
            "assertions": self._upsert_assertions(patch.assertions),
            "invalidated": 0,
        }
        if patch.invalidation_ids:
            rows = self.execute_write(
                """
                MATCH (a:GraphAssertion)
                WHERE a.assertion_id IN $ids
                SET a.authority = 'rejected', a.invalidated_at = datetime()
                RETURN count(a) AS count
                """,
                {"ids": patch.invalidation_ids},
            )
            counts["invalidated"] = int(rows[0].get("count") or 0) if rows else 0
        return {
            "patch_id": patch.patch_id,
            "validation_status": patch.validation_status,
            "counts": counts,
        }

    def close(self) -> None:
        with self._lock:
            if self._driver is not None:
                self._driver.close()
                self._driver = None
