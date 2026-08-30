from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


def test_task_runtime_uses_runtime_postgres_schema():
    text=(ROOT/"server/task_runtime/store.py").read_text(encoding="utf-8")
    assert "get_connection(runtime=True)" in text
    assert "ensure_runtime_schema()" in text


def test_checkpoint_session_and_memory_use_runtime_postgres_schema():
    files=(
        "agent/runtime_state/run_checkpoint_store.py",
        "agent/collaboration/session_state.py",
        "agent/memory/postgres_memory_store.py",
    )
    for rel in files:
        text=(ROOT/rel).read_text(encoding="utf-8")
        assert "get_connection(runtime=True)" in text
        assert "ensure_runtime_schema" in text


def test_business_repositories_use_postgres_store():
    repo_dir=ROOT/"database/repositories"
    checked=0
    for path in repo_dir.glob("*_repository.py"):
        text=path.read_text(encoding="utf-8")
        if "class " not in text:
            continue
        if path.name == "proposal_repository.py":
            assert "get_connection" in text
        else:
            assert "PostgresStore" in text, path.name
        checked += 1
    assert checked >= 8


def test_task_create_does_not_depend_on_legacy_sqlite_defaults():
    text=(ROOT/"server/task_runtime/store.py").read_text(encoding="utf-8")
    create_block=text[text.index("def create("):text.index("def get(", text.index("def create("))]
    for column in ("progress","message","attempt","cancel_requested"):
        assert column in create_block
    assert "0.0" in create_block


def test_runtime_schema_initialization_is_cross_process_safe():
    text=(ROOT/"database/runtime_schema.py").read_text(encoding="utf-8")
    assert "pg_advisory_xact_lock" in text
    assert "836080806" in text


def test_database_cutover_can_disable_scheduler_without_changing_final_default():
    compose=(ROOT/"docker-compose.yml").read_text(encoding="utf-8")
    assert 'STOCK_APP_RUNTIME_SCHEDULER_ENABLED: "${STOCK_APP_RUNTIME_SCHEDULER_ENABLED:-1}"' in compose


def test_scheduler_startup_failure_does_not_abort_api_liveness():
    text=(ROOT/"scheduler/runtime_scheduler.py").read_text(encoding="utf-8")
    block=text[text.index("def start_runtime_scheduler"):text.index("def reload_runtime_scheduler")]
    assert "except Exception as exc" in block
    assert "return _SCHEDULER" in block


def test_database_repository_package_exports_runtime_dependencies():
    text=(ROOT/"database/repositories/__init__.py").read_text(encoding="utf-8")
    required=(
        "AgentRepository",
        "EvaluationRepository",
        "NewsRepository",
        "PortfolioRepository",
        "PredictionRepository",
        "ProposalRepository",
        "RecommendationRepository",
        "RuntimeDataImportAuditRepository",
        "RuntimeStateRepository",
        "StockRepository",
        "StrategyWorkflowRepository",
        "SystemMonitorRepository",
        "UserRepository",
    )
    for name in required:
        assert name in text, name


def test_postgres_only_contract_scans_strategies_and_core():
    text=(ROOT/"tests/unit/test_stage3_6b8_postgres_only_contract.py").read_text(encoding="utf-8")
    excluded=text.split("RUNTIME_SOURCE_EXCLUDE_DIRS",1)[1].split("}",1)[0]
    assert 'ROOT.rglob("*.py")' in text
    assert '"strategies"' not in excluded
    assert '"core"' not in excluded
    assert '"app"' not in excluded


def test_runtime_scan_excludes_only_historical_backup_trees():
    text=(ROOT/"tests/unit/test_stage3_6b8_postgres_only_contract.py").read_text(encoding="utf-8")
    assert '"backups"' in text
    assert '"context_audit_bundle_"' in text
    excluded=text.split("RUNTIME_SOURCE_EXCLUDE_DIRS",1)[1].split("}",1)[0]
    assert '"strategies"' not in excluded
    assert '"news_mapping"' not in excluded


def test_strategy_binding_repository_is_native_postgres():
    text=(ROOT/"strategies/binding_repository.py").read_text(encoding="utf-8")
    assert "PostgresStore" in text
    assert "FOR UPDATE" in text
    assert "BEGIN IMMEDIATE" not in text
    assert "SQLiteStore" not in text
    assert "database.sqlite_store" not in text


def test_news_mapping_is_native_postgres_and_namespaced():
    schema=(ROOT/"news_mapping/schema.py").read_text(encoding="utf-8")
    assert "get_postgres_connection" in schema
    assert "sqlite3" not in schema
    assert "news_mapping_stock_alias" in schema
    assert "news_mapping_news_items" in schema
    assert "pg_advisory_xact_lock" in schema
    for rel in (
        "news_mapping/concept_mapper.py",
        "news_mapping/entity_extractor.py",
        "news_mapping/llm_mapper.py",
        "news_mapping/mapping_pipeline.py",
        "news_mapping/mapping_store.py",
        "news_mapping/news_ingestor.py",
        "news_mapping/stock_alias_builder.py",
    ):
        text=(ROOT/rel).read_text(encoding="utf-8")
        assert "sqlite3" not in text, rel
        assert "database.sqlite_store" not in text, rel


def test_news_mapping_does_not_reuse_main_stock_alias_table():
    text=(ROOT/"news_mapping/schema.py").read_text(encoding="utf-8")
    assert 'TABLE_STOCK_ALIAS = "news_mapping_stock_alias"' in text
    assert 'TABLE_STOCK_MASTER = "news_mapping_stock_master"' in text


def test_root_news_sync_paths_are_native_postgres():
    for rel in ("news_content_fetcher.py", "news_db_sync.py"):
        text=(ROOT/rel).read_text(encoding="utf-8")
        assert "PostgresStore" in text, rel
        assert "initialize_database" not in text, rel
        assert "database.sqlite_store" not in text, rel
        assert "SQLiteStore" not in text, rel


def test_news_fulltext_postgres_sql_parameter_contract():
    text=(ROOT/"news_fulltext_ingestion.py").read_text(encoding="utf-8")
    assert '",".join("%s" for _ in news_ids)' in text
    assert '%(chunk_id)s' in text
    assert "cur.executemany" in text
    assert 'database_backend="postgresql"' in text


def test_frozen_seed_never_restores_sqlite_database_files():
    text=(ROOT/"core/config/paths.py").read_text(encoding="utf-8")
    assert '{".db", ".sqlite", ".sqlite3"}' in text
    block=text[text.index("def seed_user_data_from_bundle"):text.index("ROOT_DIR =", text.index("def seed_user_data_from_bundle"))]
    assert "agent_quant.db" not in block
    assert "news_mapping.db" not in block


def test_memory_store_contract_accepts_document_style_adapter():
    from agent.memory.in_memory_memory_store import InMemoryMemoryStore
    from agent.memory.memory_consolidator import MemoryConsolidator
    from agent.memory.memory_manager import MemoryManager
    from agent.memory.memory_pruner import MemoryPruner
    from agent.memory.memory_store_contract import MemoryStore
    from agent.memory.memory_types import MemoryScope, MemoryType

    class FakeDocumentMemoryStore(InMemoryMemoryStore):
        backend_name="fake_document"

        def describe(self):
            return {
                "backend_name": self.backend_name,
                "storage_kind": "document",
                "available": True,
            }

    store=FakeDocumentMemoryStore()
    assert isinstance(store, MemoryStore)
    manager=MemoryManager(store=store)
    record=manager.remember(
        user_id="u_doc",
        content="Prefer evidence-first explanations for 600519.",
        memory_type=MemoryType.SEMANTIC,
        memory_subtype="preference",
        scope=MemoryScope.USER,
        source_type="confirmed_user_preference",
        source_id="doc_1",
        stock_codes=["600519"],
        metadata={"user_confirmed": True},
        user_confirmed=True,
    )
    results=manager.retrieve(user_id="u_doc", query="600519 evidence", stock_codes=["600519"])
    assert results and results[0].record.memory_id == record.memory_id
    assert MemoryConsolidator().consolidate_store(store, user_id="u_doc")["input_count"] == 1
    assert MemoryPruner().prune_store(store, user_id="u_doc")["input_count"] == 1
    assert store.describe()["storage_kind"] == "document"


def test_memory_store_factory_is_the_only_default_backend_selection_point():
    factory=(ROOT/"agent/memory/memory_store_factory.py").read_text(encoding="utf-8")
    manager=(ROOT/"agent/memory/memory_manager.py").read_text(encoding="utf-8")
    assert 'os.getenv("STOCK_MEMORY_STORE_BACKEND", "postgresql")' in factory
    assert "STOCK_MEMORY_STORE_BACKEND" not in manager


def test_memory_store_factory_can_register_document_backend_without_upper_changes():
    from agent.memory.in_memory_memory_store import InMemoryMemoryStore
    from agent.memory.memory_store_factory import create_memory_store, register_memory_store_backend

    class FactoryDocumentStore(InMemoryMemoryStore):
        backend_name="factory_document"

    register_memory_store_backend(
        "factory_document",
        FactoryDocumentStore,
        replace=True,
    )
    store=create_memory_store("factory_document")
    assert store.backend_name == "factory_document"
    assert store.describe()["storage_kind"] == "memory"
