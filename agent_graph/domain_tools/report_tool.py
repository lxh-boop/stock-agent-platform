from __future__ import annotations

from pathlib import Path
from typing import Any


def query_latest_reports(output_dir: str | Path = "outputs") -> dict[str, Any]:
    root = Path(output_dir)
    candidates = []
    for pattern in ["reports/**/*", "*report*", "portfolio/*/history/risk/*.json"]:
        candidates.extend(path for path in root.glob(pattern) if path.is_file())
    latest = sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True)[:10]
    return {
        "status": "success" if latest else "no_reports",
        "reports": [{"path": str(path), "name": path.name, "modified_time": path.stat().st_mtime} for path in latest],
    }
