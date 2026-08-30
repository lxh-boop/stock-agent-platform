from __future__ import annotations

from pathlib import Path
import csv
import json
from .planner import GovernancePlan


def write_plan(plan: GovernancePlan, output_dir: str | Path) -> dict[str, Path]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "storage_governance_plan.json"
    csv_path = root / "storage_governance_plan.csv"
    md_path = root / "storage_governance_plan.md"

    json_path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    fields = ["path", "size_bytes", "size_mb", "age_days", "decision", "reason", "rule", "auto_apply_allowed"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in plan.files:
            w.writerow({k: getattr(row, k) for k in fields})

    totals = plan.totals()
    lines = [
        "# Stage 3.6A Storage Governance Plan",
        "",
        f"- Project: `{plan.project}`",
        f"- Policy: `{plan.policy_version}`",
        f"- Created: `{plan.created_at}`",
        "- Mode: **DRY RUN ONLY**",
        "",
        "## Decision totals",
        "",
        "| Decision | Files | Size GB |",
        "|---|---:|---:|",
    ]
    for key, row in sorted(totals.items(), key=lambda x: int(x[1]["size_bytes"]), reverse=True):
        lines.append(f'| {key} | {row["files"]} | {row["size_gb"]} |')
    lines += [
        "",
        f"Verified auto-apply-safe candidate bytes: **{plan.reclaimable_bytes() / (1024**3):.3f} GB**",
        "",
        "> Stage 3.6A main installer does not delete these files. CLEANUP means eligible after reviewing this report.",
        "",
        "## Largest governed files",
        "",
        "| Decision | Size MB | Path | Reason |",
        "|---|---:|---|---|",
    ]
    governed = [x for x in plan.files if x.decision != "UNKNOWN"][:200]
    for row in governed:
        reason = row.reason.replace("|", "/")
        lines.append(f"| {row.decision} | {row.size_mb:.3f} | `{row.path}` | {reason} |")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {"json": json_path, "csv": csv_path, "md": md_path}
