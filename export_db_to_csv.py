"""Export SQLite tables to CSV files.

Examples:
    python export_db_to_csv.py
    python export_db_to_csv.py --output-dir .\csv_export --table jobs
    python export_db_to_csv.py --database .\resume_agent.db
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path


DEFAULT_DATABASE = Path(__file__).with_name("resume_agent.db")
DEFAULT_OUTPUT_DIR = Path(__file__).with_name("csv_export")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export SQLite tables to individual CSV files."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"SQLite database path (default: {DEFAULT_DATABASE})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for CSV files (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--table",
        action="append",
        dest="tables",
        help="Export only this table. Repeat the option to export multiple tables.",
    )
    return parser.parse_args()


def get_table_names(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    return [row[0] for row in rows]


def quote_identifier(identifier: str) -> str:
    """Quote a SQLite identifier safely."""
    return '"' + identifier.replace('"', '""') + '"'


def export_table(
    connection: sqlite3.Connection, table_name: str, output_path: Path
) -> int:
    quoted_table = quote_identifier(table_name)
    cursor = connection.execute(f"SELECT * FROM {quoted_table}")
    column_names = [column[0] for column in cursor.description]

    with output_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(column_names)
        row_count = 0
        for row in cursor:
            writer.writerow(
                value.decode("utf-8", errors="replace")
                if isinstance(value, bytes)
                else value
                for value in row
            )
            row_count += 1
    return row_count


def main() -> int:
    args = parse_args()
    database_path = args.database.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()

    if not database_path.is_file():
        raise SystemExit(f"数据库文件不存在: {database_path}")

    requested_tables = set(args.tables or [])
    output_dir.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(database_path) as connection:
        available_tables = get_table_names(connection)
        if requested_tables:
            missing_tables = requested_tables.difference(available_tables)
            if missing_tables:
                missing = ", ".join(sorted(missing_tables))
                raise SystemExit(f"数据库中不存在指定表: {missing}")
            table_names = [
                table_name for table_name in available_tables if table_name in requested_tables
            ]
        else:
            table_names = available_tables

        if not table_names:
            raise SystemExit("数据库中没有可导出的表。")

        for table_name in table_names:
            output_path = output_dir / f"{table_name}.csv"
            row_count = export_table(connection, table_name, output_path)
            print(f"已导出 {table_name}: {row_count} 行 -> {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
