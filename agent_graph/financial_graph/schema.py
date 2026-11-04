from __future__ import annotations

# Only technical kernel labels are constrained. Financial object classes and
# predicates are data, not Python/Neo4j schema enumerations.
CONSTRAINTS: tuple[str, ...] = (
    "CREATE CONSTRAINT graph_object_id_unique IF NOT EXISTS FOR (n:GraphObject) REQUIRE n.object_id IS UNIQUE",
    "CREATE CONSTRAINT graph_identity_id_unique IF NOT EXISTS FOR (n:GraphIdentity) REQUIRE n.identity_id IS UNIQUE",
    "CREATE CONSTRAINT graph_term_id_unique IF NOT EXISTS FOR (n:GraphTerm) REQUIRE n.term_id IS UNIQUE",
    "CREATE CONSTRAINT graph_assertion_id_unique IF NOT EXISTS FOR (n:GraphAssertion) REQUIRE n.assertion_id IS UNIQUE",
    "CREATE CONSTRAINT graph_evidence_id_unique IF NOT EXISTS FOR (n:GraphEvidence) REQUIRE n.evidence_id IS UNIQUE",
)

INDEXES: tuple[str, ...] = (
    "CREATE INDEX graph_object_name IF NOT EXISTS FOR (n:GraphObject) ON (n.name)",
    "CREATE INDEX graph_object_class IF NOT EXISTS FOR (n:GraphObject) ON (n.object_class)",
    "CREATE INDEX graph_identity_lookup IF NOT EXISTS FOR (n:GraphIdentity) ON (n.namespace, n.normalized_value)",
    "CREATE INDEX graph_term_lookup IF NOT EXISTS FOR (n:GraphTerm) ON (n.term_kind, n.normalized_name)",
    "CREATE INDEX graph_assertion_predicate IF NOT EXISTS FOR (n:GraphAssertion) ON (n.predicate_term_id)",
    "CREATE INDEX graph_assertion_authority IF NOT EXISTS FOR (n:GraphAssertion) ON (n.authority)",
    "CREATE INDEX graph_evidence_published IF NOT EXISTS FOR (n:GraphEvidence) ON (n.published_at)",
)

FULLTEXT_INDEXES: tuple[str, ...] = (
    "CREATE FULLTEXT INDEX graph_object_text IF NOT EXISTS FOR (n:GraphObject) ON EACH [n.name, n.alias_text]",
    "CREATE FULLTEXT INDEX graph_term_text IF NOT EXISTS FOR (n:GraphTerm) ON EACH [n.name, n.description]",
)
