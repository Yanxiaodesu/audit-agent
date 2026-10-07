"""一键初始化 + 启动自检 —— **幂等，可以反复跑**。

做三件事：

  1. 等 MySQL 就绪（Docker 里 mysql 容器 healthy 不等于业务端口能连上）
  2. 库/表不存在就建（执行 `sql/*.sql`）；**已存在就跳过，绝不 DROP**
  3. 商品表是空的才灌种子数据

## 为什么要有它

- `scripts/init_db.py` 会 `DROP DATABASE` 两个库，只能手动跑、不能进自动化
  （README 里专门写了「不要随手跑它」）
- Docker / CI 需要「起来就能用」，而且初始化过程不能有破坏性
- 连不上数据库时要给出**能看懂的**排查顺序，而不是一堆堆栈

## 为什么用 MULTI_STATEMENTS 而不是自己切 SQL

自己按 `;` 切语句会踩坑：DDL 里的 `COMMENT '审核任务表/队列'` 就可能带分号。
让 MySQL 自己解析最稳。这个连接只用来执行**项目自带的、可信的** SQL 文件，
不接受任何外部输入，所以 MULTI_STATEMENTS 在这里没有注入风险。
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import time

import pymysql
from pymysql.constants import CLIENT
from pymysql.cursors import DictCursor

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import BIZ_DB, DB_CONFIG, META_DB  # noqa: E402

SQL_DIR = ROOT / "sql"
SCHEMA_FILES = (("01_business.sql", BIZ_DB), ("02_meta.sql", META_DB))


def connect(db: str | None = None, multi: bool = False):
    cfg = {k: v for k, v in DB_CONFIG.items() if k != "database"}
    if db:
        cfg["database"] = db
    return pymysql.connect(cursorclass=DictCursor,
                           client_flag=CLIENT.MULTI_STATEMENTS if multi else 0,
                           **cfg)


# ============================================================
# 1. 等数据库
# ============================================================

def wait_for_mysql(timeout: int) -> None:
    host, port = DB_CONFIG.get("host"), DB_CONFIG.get("port")
    deadline = time.time() + timeout
    last = "(没有尝试成功过)"
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        try:
            connect().close()
            print(f"  [1/3] MySQL 就绪  {host}:{port}（第 {attempts} 次尝试）")
            return
        except Exception as e:  # noqa: BLE001 —— 连不上就是要一直等到超时
            last = f"{type(e).__name__}: {e}"
            time.sleep(2)

    print(f"\n  ✗ 连不上 MySQL（{host}:{port}），已等待 {timeout} 秒")
    print(f"    最后错误：{last}\n")
    print("  按顺序排查：")
    print("    1. MySQL 起了吗？  Windows 看「服务」里的 MySQL；"
          "Docker 跑 docker compose ps")
    print(f"    2. .env 里的 DB_HOST / DB_PORT / DB_USER / DB_PASSWORD 填对了吗"
          f"（当前 user={DB_CONFIG.get('user')}）")
    print("    3. 端口通不通？  本机 3306 常被别的实例占用")
    raise SystemExit(1)


# ============================================================
# 2. 建表
# ============================================================

def table_count(db: str) -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM information_schema.tables "
                "WHERE table_schema = %s", (db,))
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


def apply_schema(filename: str) -> None:
    """执行一个 .sql 文件（文件自带 DROP/CREATE DATABASE，所以只在缺表时调用）。"""
    path = SQL_DIR / filename
    if not path.exists():
        raise SystemExit(f"  找不到建表脚本：{path}")
    conn = connect(multi=True)
    try:
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
            while cur.nextset():        # 多语句要逐个 nextset 消费掉
                pass
        conn.commit()
    finally:
        conn.close()


# ============================================================
# 3. 灌数据
# ============================================================

def row_count(db: str, table: str) -> int:
    conn = connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM `{table}`")
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


# ============================================================
# 主流程
# ============================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="初始化数据库并自检（幂等）")
    ap.add_argument("--wait", type=int, default=60, help="等 MySQL 就绪的秒数")
    ap.add_argument("--no-seed", action="store_true", help="不灌种子数据")
    ap.add_argument("--force-schema", action="store_true",
                    help="⚠ 强制重建两个库 —— 会清空所有数据")
    args = ap.parse_args()

    print("=" * 68)
    print("  audit-agent  初始化 / 自检")
    print("=" * 68)

    wait_for_mysql(args.wait)

    # ---- 建表 ----
    if args.force_schema:
        print(f"  [2/3] --force-schema：重建两个库（**会清空数据**）")
        for name, _ in SCHEMA_FILES:
            apply_schema(name)
    else:
        missing = [(n, db) for n, db in SCHEMA_FILES if table_count(db) == 0]
        if missing:
            print(f"  [2/3] 缺表：{', '.join(db for _, db in missing)} → 建表")
            for name, _ in missing:
                apply_schema(name)
        else:
            print(f"  [2/3] 表结构已存在（{BIZ_DB} / {META_DB}）→ 跳过，不动已有数据")

    # ---- 灌数据 ----
    if args.no_seed:
        print("  [3/3] --no-seed：跳过种子数据")
    else:
        n = row_count(BIZ_DB, "item")
        if n == 0:
            print("  [3/3] 商品表是空的 → 灌种子数据 ...", flush=True)
            # 先 flush 自己的输出，否则子进程的输出会插到上面几行前面（缓冲顺序错乱）
            sys.stdout.flush()
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / "seed_data.py")],
                               cwd=str(ROOT))
            if r.returncode != 0:
                raise SystemExit("  ✗ 种子数据灌入失败")
            n = row_count(BIZ_DB, "item")
        else:
            print(f"  [3/3] 已有 {n} 件商品 → 跳过灌数据")

    # ---- 自检汇总 ----
    checks = [
        (f"{BIZ_DB}.item", row_count(BIZ_DB, "item")),
        (f"{BIZ_DB}.item_category", row_count(BIZ_DB, "item_category")),
        (f"{BIZ_DB}.sys_user", row_count(BIZ_DB, "sys_user")),
        (f"{META_DB}.audit_ground_truth", row_count(META_DB, "audit_ground_truth")),
    ]
    print()
    print("  ✓ 就绪")
    for name, cnt in checks:
        flag = " " if cnt else "⚠"
        print(f"      {flag} {name:<34} {cnt:>5} 行")

    empty = [n for n, c in checks if c == 0]
    if empty:
        print(f"\n  ⚠ 有 {len(empty)} 张表是空的：{', '.join(empty)}")
        print("     评测需要 audit_ground_truth 有标注数据，请检查 seed_data.py")

    print()
    print("  启动服务：")
    print("      python -m uvicorn app.main:app --host 127.0.0.1 --port 8100")
    print("      python -u worker.py")
    print("    然后打开  http://127.0.0.1:8100")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
