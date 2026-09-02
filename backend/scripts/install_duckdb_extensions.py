"""Install deployment-owned DuckDB extensions required by platform workers."""
from __future__ import annotations

import duckdb


def main() -> None:
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("INSTALL excel FROM core")
        connection.execute("LOAD excel")
        row = connection.execute(
            "SELECT extension_version FROM duckdb_extensions() "
            "WHERE extension_name = 'excel' AND installed AND loaded"
        ).fetchone()
        if row is None:
            raise RuntimeError("DuckDB Excel extension installation was not verified")
        print(
            "DuckDB Excel extension ready "
            f"(duckdb={duckdb.__version__}, extension={row[0] or 'bundled'})"
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
