"""DuckDB catalogue over the processed Parquet files (views, no data copy).

    import duckdb; con = duckdb.connect("data/processed/institutional_alpha.duckdb", read_only=True)
    con.sql("select * from monthly_panel where symbol='SAILIFE' order by date").df()
"""
from __future__ import annotations

import duckdb

from src.common import get_logger, path

log = get_logger("warehouse")


def build() -> None:
    proc = path("processed")
    db = proc / "institutional_alpha.duckdb"
    if db.exists():
        db.unlink()
    con = duckdb.connect(str(db))
    for f in sorted(proc.glob("*.parquet")):
        con.execute(f"create view {f.stem} as select * from read_parquet('{f.resolve()}')")
    con.close()
    log.info("duckdb views created in %s", db)


if __name__ == "__main__":
    build()
