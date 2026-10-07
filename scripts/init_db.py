"""建库：业务库 campus_market + 元数据库 audit_agent。

用法：
    python scripts/init_db.py

注意会 DROP 并重建两个库，可重复执行。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymysql  # noqa: E402
from pymysql.cursors import DictCursor  # noqa: E402

from app.config import BIZ_DB, DB_CONFIG, META_DB  # noqa: E402

SQL_FILES = [ROOT / "sql" / "01_business.sql", ROOT / "sql" / "02_meta.sql"]


def split_statements(text: str):
    """去掉整行 -- 注释后按 ; 切分。我们自己维护的 DDL 不含字符串内分号。"""
    kept = [ln for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(kept).split(";") if s.strip()]


def main() -> int:
    cfg = {k: v for k, v in DB_CONFIG.items() if k != "database"}
    conn = pymysql.connect(cursorclass=DictCursor, **cfg)
    try:
        with conn.cursor() as cur:
            for f in SQL_FILES:
                stmts = split_statements(f.read_text(encoding="utf-8"))
                for stmt in stmts:
                    cur.execute(stmt)
                print(f"[ok] 已执行 {f.name}（{len(stmts)} 条语句）")
        conn.commit()

        with conn.cursor() as cur:
            for db in (BIZ_DB, META_DB):
                cur.execute(f"USE `{db}`")
                cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema = %s", (db,))
                print(f"  {db}: {cur.fetchone()['n']} 张表")
    finally:
        conn.close()
    print("\n建库完成。下一步：python scripts/seed_data.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
