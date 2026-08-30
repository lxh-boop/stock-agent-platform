from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from storage_governance import GovernancePlanner, load_default_policy
from storage_governance.reporting import write_plan


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 3.6A storage governance dry-run report")
    p.add_argument("--project", default=r"D:\stock_daily_app")
    p.add_argument("--output-root", default=r"D:\google\storage_governance_reports")
    p.add_argument("--policy", default="")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    project = Path(args.project)
    if not project.exists():
        raise SystemExit(f"Project not found: {project}")
    policy = load_default_policy(args.policy or None)
    plan = GovernancePlanner(policy).plan(project)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.output_root) / stamp
    paths = write_plan(plan, out)
    print("[SUCCESS] Stage 3.6A dry-run storage plan generated.")
    print(f"Policy : {policy.policy_version}")
    print(f"Files  : {len(plan.files)}")
    print(f"Safe candidate size: {plan.reclaimable_bytes() / (1024**3):.3f} GB")
    print(f"Report : {paths['md']}")
    print("No project data was deleted or modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
