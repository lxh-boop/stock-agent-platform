from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from agent_graph.financial_graph.bootstrap import FinancialGraphBootstrapper


def _read_rows(path: Path, encoding: str) -> list[dict[str, Any]]:
    with path.open("r", encoding=encoding, newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Import authoritative security master data into Neo4j.")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--encoding", default="utf-8-sig")
    args = parser.parse_args()
    if not args.csv_path.exists():
        raise FileNotFoundError(args.csv_path)
    rows = _read_rows(args.csv_path, args.encoding)
    bootstrapper = FinancialGraphBootstrapper.from_env()
    try:
        bootstrapper.initialize()
        result = bootstrapper.import_security_master(rows)
        print(json.dumps({"success": True, "row_count": len(rows), "result": result}, ensure_ascii=False, default=str))
    finally:
        bootstrapper.store.close()


if __name__ == "__main__":
    main()
