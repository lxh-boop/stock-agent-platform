#!/usr/bin/env python
"""把多用户改造前的遗留数据认领到指定账号名下。

多用户上线后，历史数据的 user_id/owner_id 仍是 'default' 或 'refactor_test'。
注册一个对应用户名的账号即可直接继承这些数据；或用本脚本把它们批量改到
任意已有账号（例如注册的第一个管理员）：

    .\\.venv\\Scripts\\python.exe scripts\\migrate_legacy_owner.py --to admin

脚本按 information_schema 自动发现所有带 user_id / owner_id 列的业务表，
不触碰 journal/临时表。操作幂等，重复执行无副作用。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.connection import get_connection

LEGACY_OWNERS = ("default", "refactor_test")
ID_COLUMNS = ("user_id", "owner_id")


def discover_targets(conn) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        """
        SELECT table_schema, table_name, column_name
        FROM information_schema.columns
        WHERE column_name IN ('user_id', 'owner_id')
          AND table_schema IN ('stock_app', 'stock_runtime')
        ORDER BY table_schema, table_name, column_name
        """
    ).fetchall()
    return [(str(r["table_schema"]), str(r["table_name"]), str(r["column_name"])) for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", required=True, help="目标账号 user_id（须已注册）")
    parser.add_argument("--from", dest="sources", action="append", default=[],
                        help=f"要合并的旧身份，默认 {','.join(LEGACY_OWNERS)}；可多次传入")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的 UPDATE")
    args = parser.parse_args()

    target = args.to.strip()
    sources = [s.strip() for s in (args.sources or list(LEGACY_OWNERS)) if s.strip()]
    if not target or target in sources:
        print("ERROR: --to 不能为空且不能与旧身份相同")
        return 2

    with get_connection() as conn:
        # 同时校验 stock_runtime 与 stock_app 两个 schema
        conn.execute('SET search_path TO stock_app, stock_runtime, public')
        account = conn.execute(
            "SELECT user_id FROM stock_app.user_account WHERE user_id = %s", (target,)
        ).fetchone()
        if not account and not args.dry_run:
            print(f"ERROR: 账号 {target} 不存在，请先在页面注册")
            return 2

        total = 0
        for schema, table, column in discover_targets(conn):
            placeholders = ", ".join("%s" for _ in sources)
            query = (
                f'UPDATE "{schema}"."{table}" SET "{column}" = %s '
                f'WHERE "{column}" IN ({placeholders})'
            )
            params = [target, *sources]
            if args.dry_run:
                print(f"[dry-run] {query} -- {params}")
                continue
            cursor = conn.execute(query, params)
            count = int(cursor.rowcount or 0)
            if count:
                print(f"{schema}.{table}.{column}: {count} rows -> {target}")
            total += count

        # 派生 ID 修正：很多主键/账户列内嵌用户段（paper_<user_id>、
        # portfolio_risk_<user_id>_<date>、paper_nav_<user_id>_<date> 等）。
        # 只改 user_id 会导致新写入的确定性 ID 与旧行不一致，进而违反
        # (user_id, as_of_date) 等唯一索引（paper-trading.update 曾因此失败）。
        # 因此对已认领的行同步重写 account_id 与内嵌旧用户段的 *_id。
        if not args.dry_run:
            id_columns = conn.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'stock_app'
                  AND (column_name LIKE '%%_id' OR column_name = 'account_id')
                  AND column_name NOT IN ('user_id', 'owner_id')
                """
            ).fetchall()
            for row in id_columns:
                table, col = str(row["table_name"]), str(row["column_name"])
                if col == "account_id":
                    for source in sources:
                        changed = conn.execute(
                            f"UPDATE stock_app.{table} SET account_id = %s "
                            "WHERE user_id = %s AND account_id = %s",
                            (f"paper_{target}", target, f"paper_{source}"),
                        ).rowcount
                        if changed:
                            print(f"{table}.account_id: {changed} rows -> paper_{target}")
                elif col.endswith("_id"):
                    for source in sources:
                        changed = conn.execute(
                            f"UPDATE stock_app.{table} SET {col} = replace({col}, %s, %s) "
                            f"WHERE user_id = %s AND {col} LIKE %s",
                            (f"_{source}_", f"_{target}_", target, f"%_{source}_%"),
                        ).rowcount
                        if changed:
                            print(f"{table}.{col}: {changed} ids remapped -> {target}")
            conn.commit()
        print(f"完成：共更新 {total} 行" if not args.dry_run else "dry-run 结束")
        return 0


if __name__ == "__main__":
    sys.exit(main())
