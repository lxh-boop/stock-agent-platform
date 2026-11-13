"""Database integration tests share one initialization in a local temporary database only."""
import os
from pathlib import Path
import pytest

@pytest.fixture(scope="session")
def isolated_postgres_schema():
    if os.environ.get("STOCK_TEST_DATABASE") != "1":
        pytest.skip("requires isolated test database")
    psycopg = pytest.importorskip("psycopg")
    from database.postgres_config import PostgresSettings
    settings = PostgresSettings.from_env()
    if settings.database != "stock_test_isolation" or settings.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Refuse non-local or non-test database")
    path = Path(__file__).resolve().parents[2] / "database/postgres_migrations/001_baseline.sql"
    script = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("\\"))
    with psycopg.connect(**settings.connection_kwargs()) as conn:
        conn.execute(script)
    yield
