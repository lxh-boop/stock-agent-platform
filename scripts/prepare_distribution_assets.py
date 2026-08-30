from __future__ import annotations

import json
from fnmatch import fnmatch
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app_version import APP_NAME, APP_VERSION
from runtime_paths import (
    get_logs_dir,
    get_outputs_dir,
    get_project_root,
    get_resource_root,
    get_user_data_root,
    is_frozen_app,
)

DATABASE_FILE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
ALPHA158_FACTOR_PATTERNS = ("*feature_stock_data_alpha158.csv",)


def _is_alpha158_factor_file(path: Path) -> bool:
    return any(fnmatch(path.name.lower(), pattern.lower()) for pattern in ALPHA158_FACTOR_PATTERNS)


def _repo_root() -> Path:
    return PROJECT_ROOT


def _expected_frozen_user_root() -> Path:
    import os

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "StockDailyApp"
    return Path.home() / "AppData" / "Local" / "StockDailyApp"


def _database_seed_files(root: Path) -> list[str]:
    hits: list[str] = []
    for rel in ("data", "models", "outputs"):
        base = root / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in DATABASE_FILE_SUFFIXES:
                hits.append(path.relative_to(root).as_posix())
    return sorted(hits)


def _alpha158_seed_files(root: Path) -> list[str]:
    hits: list[str] = []
    for rel in ("data", "models", "outputs"):
        base = root / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and _is_alpha158_factor_file(path):
                hits.append(path.relative_to(root).as_posix())
    return sorted(hits)


def main() -> int:
    root = _repo_root()
    postgres_migrations = root / "database" / "postgres_migrations"
    resources = root / "resources"
    frozen_user_root = _expected_frozen_user_root()

    print("=" * 80)
    print(f"[Distribution Assets] {APP_NAME} {APP_VERSION}")
    print(f"[Mode] {'frozen' if is_frozen_app() else 'source'}")
    print(f"[Project Root] {get_project_root()}")
    print(f"[Resource Root] {get_resource_root()}")
    print(f"[User Data Root] {get_user_data_root()}")
    print(f"[Current Mode Outputs Dir] {get_outputs_dir()}")
    print(f"[Current Mode Logs Dir] {get_logs_dir()}")
    print(f"[Frozen User Data Root Expected] {frozen_user_root}")
    print("[Database Backend] PostgreSQL only")
    print("=" * 80)

    required = [
        root / "desktop_launcher.py",
        root / "stock_daily_app.spec",
        postgres_migrations,
        resources,
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        print("[Error] Missing required distribution inputs:")
        print(json.dumps(missing, ensure_ascii=False, indent=2))
        return 1

    migration_files = sorted(postgres_migrations.glob("*.sql"))
    if not migration_files:
        print(f"[Error] No PostgreSQL baseline/migration SQL found: {postgres_migrations}")
        return 1

    database_seed_files = _database_seed_files(root)
    if database_seed_files:
        print(
            "[Database Seed] Local database files exist in seed source trees; "
            "the PyInstaller spec must exclude them:"
        )
        print(json.dumps(database_seed_files, ensure_ascii=False, indent=2))

    alpha158_seed_files = _alpha158_seed_files(root)
    if alpha158_seed_files:
        print("[Alpha158 Seed] Derived factor CSVs exist in source trees; PyInstaller excludes them:")
        print(json.dumps(alpha158_seed_files, ensure_ascii=False, indent=2))

    root_sensitive = [
        root / ".env",
        root / "local_app_config.json",
        root / "local_config.json",
    ]
    present_sensitive = [str(path) for path in root_sensitive if path.exists()]
    if present_sensitive:
        print("[Sensitive Local Files] Present in development tree, intentionally not packaged:")
        print(json.dumps(present_sensitive, ensure_ascii=False, indent=2))

    print("[Package Resources]")
    print(
        json.dumps(
            {
                "frontend_runtime": "external production React service on port 3000",
                "database_backend": "postgresql",
                "database_schema_source": "database/postgres_migrations",
                "bundled_demo_data": [
                    "data/ -> bundled_seed/data/ (database files excluded)",
                    "models/ -> bundled_seed/models/",
                    "outputs/ -> bundled_seed/outputs/ (database files excluded)",
                ],
                "forbidden_bundled_database_suffixes": sorted(DATABASE_FILE_SUFFIXES),
                "forbidden_bundled_factor_patterns": list(ALPHA158_FACTOR_PATTERNS),
                "excluded_sensitive_files": [
                    "logs/",
                    "runtime/",
                    "local_app_config.json",
                    "config/local_app_config.json",
                    ".env",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("[OK] PostgreSQL-only distribution asset checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
