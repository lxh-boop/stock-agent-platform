from __future__ import annotations

import argparse
from pathlib import Path

from storage_governance import GovernancePlanner, load_default_policy
from storage_governance.cleanup import CONFIRM_TOKEN, apply_safe_cleanup, safe_cleanup_rows


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 3.6A guarded cleanup executor")
    p.add_argument("--project", default=r"D:\stock_daily_app")
    p.add_argument("--policy", default="")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--confirm-token", default="")
    p.add_argument("--max-delete-gb", type=float, default=2.0)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    policy = load_default_policy(args.policy or None)
    plan = GovernancePlanner(policy).plan(Path(args.project))
    rows = safe_cleanup_rows(plan)
    total = sum(r.size_bytes for r in rows)
    print(f"Safe cleanup candidates: {len(rows)} files / {total/(1024**3):.3f} GB")
    if not args.apply:
        print("DRY RUN. Nothing deleted.")
        print(f"Explicit apply requires --apply --confirm-token {CONFIRM_TOKEN}")
        return 0
    result = apply_safe_cleanup(
        plan,
        confirm_token=args.confirm_token,
        max_delete_bytes=int(args.max_delete_gb * 1024**3),
    )
    print(f"Deleted files={result.deleted_files}, bytes={result.deleted_bytes}, skipped={result.skipped_files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
