import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_SOURCE_EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "node_modules", "external_repos", "__pycache__", ".pytest_cache",
    "tests", "benchmarks", "docs", "scripts", "outputs", "data", "runtime", "logs", "dist", "build",
    "backups",
}
RUNTIME_SOURCE_EXCLUDE_PREFIXES = {"context_audit_bundle_"}


def _runtime_python_files():
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if any(part in RUNTIME_SOURCE_EXCLUDE_DIRS for part in rel.parts):
            continue
        if any(
            part.startswith(prefix)
            for part in rel.parts
            for prefix in RUNTIME_SOURCE_EXCLUDE_PREFIXES
        ):
            continue
        yield path



def test_postgres_store_is_only_database_store():
    text = (ROOT / "database/postgres_store.py").read_text(encoding="utf-8")
    for method in ("insert", "upsert", "get", "list", "list_by_values", "update", "delete", "transaction"):
        assert f"def {method}(" in text
    assert "PostgresStore" in text


def _enclosing_function(parents: dict[object, object], node: object) -> str:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
    return ""


def test_runtime_source_has_no_legacy_file_database_chain():
    forbidden_text = [
        "database." + "sqlite_store",
        "database." + "persistence_store",
        "database." + "persistence_config",
        "BEGIN " + "IMMEDIATE",
        "sqlite_" + "master",
        "STOCK_DB_" + "SHADOW",
        "STOCK_DB_" + "READ_CUTOVER",
    ]
    allowed_sensitive_markers = {
        ("agent/memory/memory_policy.py", "_value_has_forbidden_text", "agent_quant" + ".db"),
        ("agent/reflection/critic_engine.py", "_contains_sensitive", "agent_quant" + ".db"),
    }

    hits = []
    for path in _runtime_python_files():
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")

        for token in forbidden_text:
            if token in text:
                hits.append(f"{rel}:{token}")

        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            hits.append(f"{rel}:syntax_error:{exc}")
            continue

        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name == "sqlite3" or alias.name.startswith("sqlite3.") for alias in node.names):
                    hits.append(f"{rel}:import sqlite3")
            elif isinstance(node, ast.ImportFrom):
                if node.module == "sqlite3" or str(node.module or "").startswith("sqlite3."):
                    hits.append(f"{rel}:from sqlite3")

            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                for marker in ("agent_quant" + ".db", "task_runtime" + ".sqlite3"):
                    if marker not in value:
                        continue
                    function_name = _enclosing_function(parents, node)
                    if (rel, function_name, marker) not in allowed_sensitive_markers:
                        hits.append(f"{rel}:{function_name or '<module>'}:{marker}")

    assert not hits, hits


def test_compose_is_postgresql_only():
    text=(ROOT/"docker-compose.yml").read_text(encoding="utf-8")
    assert "STOCK_POSTGRES_SCHEMA: ${STOCK_POSTGRES_SCHEMA:-stock_app}" in text
    assert "STOCK_POSTGRES_RUNTIME_SCHEMA: ${STOCK_POSTGRES_RUNTIME_SCHEMA:-stock_runtime}" in text
    assert "task_runtime_data" not in text
    assert "STOCK_DB_MODE" not in text
    assert "STOCK_DB_SHADOW" not in text
    assert "STOCK_DB_READ_CUTOVER" not in text


def test_old_transition_modules_are_removed():
    for rel in (
        "database/sqlite_store.py",
        "database/persistence_store.py",
        "database/persistence_config.py",
        "scripts/persistence",
    ):
        assert not (ROOT/rel).exists(), rel


def test_active_news_refresh_and_distribution_chain_are_postgres_only():
    fulltext=(ROOT/"news_fulltext_ingestion.py").read_text(encoding="utf-8")
    refresh=(ROOT/"scripts/refresh_news_rag_fulltext.py").read_text(encoding="utf-8")
    scheduler=(ROOT/"scheduler/daily_worker.py").read_text(encoding="utf-8")
    paths=(ROOT/"core/config/paths.py").read_text(encoding="utf-8")
    prepare=(ROOT/"scripts/prepare_distribution_assets.py").read_text(encoding="utf-8")
    spec=(ROOT/"stock_daily_app.spec").read_text(encoding="utf-8")

    for text in (fulltext, refresh, scheduler, paths, prepare):
        assert "agent_quant.db" not in text

    assert "db_path" not in fulltext.split("def run_full_text_news_ingestion",1)[1].split(") -> FullTextIngestionReport",1)[0]
    assert "initialize_database" not in fulltext
    assert '",".join("?"' not in fulltext
    assert "=:title" not in fulltext
    assert "import sqlite3" not in refresh
    assert "PRAGMA" not in refresh
    assert '"--db-path"' not in scheduler
    assert '"--db-path"' not in refresh

    assert "_DATABASE_FILE_SUFFIXES" in paths
    assert "bundled_db" not in paths
    assert "fallback_db" not in paths
    assert "database/postgres_migrations" in spec
    assert "DATABASE_FILE_SUFFIXES" in spec
    assert "database/migrations" not in spec
    assert "database/seed" not in spec
    assert "initialize_database" not in prepare
    assert "agent_quant_probe.db" not in prepare


def test_memory_upper_layer_is_storage_backend_neutral():
    upper=(
        "agent/memory/memory_manager.py",
        "agent/memory/memory_retriever.py",
        "agent/memory/memory_consolidator.py",
        "agent/memory/memory_pruner.py",
        "agent/memory/memory_context_bridge.py",
        "agent/memory/memory_tool.py",
        "agent/memory/__init__.py",
    )
    forbidden=(
        "SQLiteMemoryStore",
        "PostgresMemoryStore",
        "memory_store_path",
        "DEFAULT_MEMORY_STORE_PATH",
        "memory_store.sqlite",
        "db_path",
        "import sqlite3",
        "get_connection",
        "psycopg",
    )
    for rel in upper:
        text=(ROOT/rel).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, (rel, token)
    manager=(ROOT/"agent/memory/memory_manager.py").read_text(encoding="utf-8")
    assert "MemoryStore" in manager
    assert "create_memory_store" in manager


def test_memory_backend_specific_code_is_isolated_in_adapter():
    facade=(ROOT/"agent/memory/memory_store.py").read_text(encoding="utf-8")
    pg=(ROOT/"agent/memory/postgres_memory_store.py").read_text(encoding="utf-8")
    factory=(ROOT/"agent/memory/memory_store_factory.py").read_text(encoding="utf-8")
    contract=(ROOT/"agent/memory/memory_store_contract.py").read_text(encoding="utf-8")
    assert "get_connection" not in facade
    assert "PostgresMemoryStore" not in facade
    assert "class MemoryStore(Protocol)" in contract
    assert "get_connection(runtime=True)" in pg
    assert "STOCK_MEMORY_STORE_BACKEND" in factory
    assert "register_memory_store_backend" in factory
