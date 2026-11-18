#!/usr/bin/env python3
"""Fix a MariaDB 11.x / MySQL 8.x SQL dump for import into MariaDB 10.8+."""

import argparse
import re
import sys
from collections import Counter

# Collations unsupported on MariaDB 10.8 (0900 from MySQL 8.0, uca1400 from MariaDB 10.10+)
COLLATION_REPLACEMENTS = [
    (re.compile(r"\butf8mb4_0900_[a-z0-9_]+"), "utf8mb4_unicode_ci"),
    (re.compile(r"\butf8mb4_uca1400_[a-z0-9_]+"), "utf8mb4_unicode_ci"),
    (re.compile(r"\butf8mb3_uca1400_[a-z0-9_]+"), "utf8mb3_unicode_ci"),
    (re.compile(r"\butf8_uca1400_[a-z0-9_]+"), "utf8_unicode_ci"),
]

CREATE_TABLE_RE = re.compile(r"^CREATE TABLE(?: IF NOT EXISTS)? `([^`]+)`")
CREATE_VIEW_RE = re.compile(
    r"^CREATE(?:\s+OR\s+REPLACE)?\b(?!.*\bTABLE\b).*\bVIEW\b",
    re.IGNORECASE,
)
DROP_TABLE_VIEW_RE = re.compile(
    r"^(DROP\s+(?:TABLE|VIEW)(?:\s+IF\s+EXISTS)?\s+)`[^`]+`\.`([^`]+)`",
    re.IGNORECASE,
)
DEFINER_RE = re.compile(r"DEFINER=`[^`]+`@`[^`]+`\s*")
CHARSET_LATIN_RE = re.compile(
    r"(?:DEFAULT\s+)?CHARSET=(?:latin1|ascii)(?:\s+COLLATE=(?:latin1|ascii)_[a-z0-9_]+)?",
    re.IGNORECASE,
)

# Inline row size threshold below InnoDB's 8126-byte limit (leaves headroom for row header/null bitmap)
INLINE_ROW_LIMIT = 7500


def estimate_inline_bytes(cols: list[str], table_tail: str) -> int:
    """Estimate InnoDB DYNAMIC inline bytes for a CREATE TABLE definition."""
    if re.search(r"CHARSET=(?:utf8mb4)\b", table_tail, re.I):
        tbl_mult = 4
    elif re.search(r"CHARSET=(?:utf8|utf8mb3)\b", table_tail, re.I):
        tbl_mult = 3
    else:
        tbl_mult = 1

    inline = 0
    for col in cols:
        stripped = col.strip()
        if stripped.startswith(("PRIMARY KEY", "KEY ", "UNIQUE ", "CONSTRAINT ", "FULLTEXT ", "INDEX ")):
            continue

        if re.search(r"\bCHARACTER SET\s+utf8mb4\b", col, re.I):
            mult = 4
        elif re.search(r"\bCHARACTER SET\s+(?:utf8|utf8mb3)\b", col, re.I):
            mult = 3
        elif re.search(r"\bCHARACTER SET\s+(?:latin1|ascii)\b", col, re.I):
            mult = 1
        else:
            mult = tbl_mult

        vm = re.search(r"\b(?:var)?char\((\d+)\)", col, re.I)
        if vm:
            max_bytes = int(vm.group(1)) * mult
            # In InnoDB DYNAMIC, VARCHAR > 255 bytes can overflow off-page (count ~40 bytes inline)
            inline += max_bytes if max_bytes <= 255 else 40
        elif re.search(r"\b(?:tiny|medium|long)?(?:text|blob)\b", col, re.I):
            inline += 20
        elif re.search(r"\b(?:bigint|double|datetime|timestamp)\b", col, re.I):
            inline += 8
        elif re.search(r"\b(?:int|integer|float)\b", col, re.I):
            inline += 4
        elif re.search(r"\b(?:date|mediumint|time)\b", col, re.I):
            inline += 3
        elif re.search(r"\bsmallint\b", col, re.I):
            inline += 2
        elif re.search(r"\b(?:tinyint|enum|year)\b", col, re.I):
            inline += 1
        else:
            inline += 16

    return inline


def rewrite_collations(line: str, stats: Counter) -> str:
    """Replace unsupported MySQL 8 / MariaDB 10.10+ collations."""
    for pattern, replacement in COLLATION_REPLACEMENTS:
        line, count = pattern.subn(replacement, line)
        if count:
            stats[f"collation->{replacement}"] += count
    return line


def clean_view_statement(
    view_sql: str,
    explicit_db: str | None,
    stats: Counter,
) -> tuple[str, str, set[str]]:
    """Strip hardcoded database qualifiers from a CREATE VIEW statement.

    In MySQL dumps (e.g. from mysqldump or phpMyAdmin), view definitions often
    contain hardcoded database prefixes (e.g. `templefabrics_orbite`.`stock_items`),
    which cause imports into another database (e.g. `samyakk_old`) to fail with
    ERROR 1146 (Table '<old_db>.<tbl>' doesn't exist).

    When explicit_db is not provided, this function auto-detects schema qualifiers
    from 3-level identifiers (`db`.`tbl`.`col`) and FROM/JOIN clauses.
    If no qualifiers are found, the view is returned unmodified.
    """
    vname_match = re.search(
        r"\bVIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:`[^`]+`\.)?`([^`]+)`",
        view_sql,
        re.IGNORECASE,
    )
    view_name = vname_match.group(1) if vname_match else "unnamed_view"

    detected_dbs = set()
    if explicit_db:
        detected_dbs.add(explicit_db)
    else:
        # Source 1: 3-level backticked identifiers: `db`.`tbl`.`col`
        for m in re.finditer(
            r"`([a-zA-Z0-9_]+)`\.`[a-zA-Z0-9_]+`\.`[a-zA-Z0-9_]+`", view_sql
        ):
            detected_dbs.add(m.group(1))

        # Source 2: Table references in FROM / JOIN clauses: FROM `db`.`tbl` or JOIN `db`.`tbl`
        for m in re.finditer(
            r"(?:\b(?:FROM|JOIN)\s+[\(\s]*)`([a-zA-Z0-9_]+)`\.`[a-zA-Z0-9_]+`",
            view_sql,
            re.IGNORECASE,
        ):
            detected_dbs.add(m.group(1))

        # Source 3: View name qualified with db: CREATE ... VIEW `db`.`view_name`
        for m in re.finditer(
            r"\bVIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?`([a-zA-Z0-9_]+)`\.`[a-zA-Z0-9_]+`",
            view_sql,
            re.IGNORECASE,
        ):
            detected_dbs.add(m.group(1))

    if not detected_dbs:
        return view_sql, view_name, set()

    cleaned_dbs = set()
    for db in sorted(detected_dbs):
        esc_db = re.escape(db)
        total_stripped = 0

        # Rule 1: 3-level identifier `db`.`tbl`.`col` -> `tbl`.`col`
        view_sql, c1 = re.subn(
            rf"`{esc_db}`\.(`[^`]+`\.`[^`]+`)",
            r"\1",
            view_sql,
        )
        total_stripped += c1

        # Rule 2: 2-level identifier in FROM / JOIN: FROM `db`.`tbl` -> FROM `tbl`
        view_sql, c2 = re.subn(
            rf"(\b(?:FROM|JOIN)\s+[\(\s]*)`{esc_db}`\.",
            r"\1",
            view_sql,
            flags=re.IGNORECASE,
        )
        total_stripped += c2

        # Rule 3: 2-level identifier in CREATE VIEW: VIEW `db`.`view_name` -> VIEW `view_name`
        view_sql, c3 = re.subn(
            rf"(\bVIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?)`{esc_db}`\.",
            r"\1",
            view_sql,
            flags=re.IGNORECASE,
        )
        total_stripped += c3

        # Unbackticked 3-level identifier: db.tbl.col -> tbl.col
        view_sql, c1_unb = re.subn(
            rf"\b{esc_db}\.([a-zA-Z0-9_]+\.[a-zA-Z0-9_]+)",
            r"\1",
            view_sql,
        )
        total_stripped += c1_unb

        # Unbackticked FROM / JOIN: FROM db.tbl -> FROM tbl
        view_sql, c2_unb = re.subn(
            rf"(\b(?:FROM|JOIN)\s+[\(\s]*){esc_db}\.([a-zA-Z0-9_]+)",
            r"\1\2",
            view_sql,
            flags=re.IGNORECASE,
        )
        total_stripped += c2_unb

        if total_stripped > 0:
            stats[f"view_db_stripped->{db}"] += total_stripped
            cleaned_dbs.add(db)

    if cleaned_dbs:
        stats["views_modified"] += 1

    return view_sql, view_name, cleaned_dbs


def fix_dump(
    input_path: str,
    output_path: str,
    strip_definer: bool,
    strip_view_db: bool = True,
    explicit_view_db: str | None = None,
) -> dict:
    """Stream-process input_path and write MariaDB 10.8-compatible SQL to output_path."""
    stats = Counter()
    upgraded_tables = []
    cleaned_views = []

    in_create = False
    table_name = ""
    table_lines: list[str] = []

    in_view = False
    view_lines: list[str] = []

    injected_strict_mode = False

    with (
        open(input_path, "r", encoding="utf-8", errors="surrogateescape") as fin,
        open(output_path, "w", encoding="utf-8", errors="surrogateescape") as fout,
    ):
        for line in fin:
            if not injected_strict_mode and line.lstrip().startswith("SET SQL_MODE"):
                fout.write("SET SESSION innodb_strict_mode = 0;\n")
                injected_strict_mode = True

            # Only rewrite collations in DDL/session lines, never inside INSERT data
            if not line.startswith("INSERT INTO "):
                line = rewrite_collations(line, stats)
                if strip_definer and "DEFINER=" in line:
                    line, d_count = DEFINER_RE.subn("", line)
                    stats["definer_stripped"] += d_count

            if in_view:
                view_lines.append(line)
                if line.rstrip().endswith(";"):
                    full_view = "".join(view_lines)
                    cleaned_view, vname, dbs = clean_view_statement(
                        full_view, explicit_view_db, stats
                    )
                    fout.write(cleaned_view)
                    if dbs:
                        cleaned_views.append((vname, sorted(dbs)))
                    in_view = False
                    view_lines = []
                continue

            if in_create:
                table_lines.append(line)
                if line.startswith(")"):
                    tail = table_lines[-1]
                    if re.search(r"ENGINE=InnoDB", tail, re.I) and CHARSET_LATIN_RE.search(tail):
                        est = estimate_inline_bytes(table_lines[1:-1], tail)
                        if est > INLINE_ROW_LIMIT:
                            table_lines[-1] = CHARSET_LATIN_RE.sub(
                                "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",
                                tail,
                            )
                            upgraded_tables.append((table_name, est))
                            stats["tables_upgraded_to_utf8mb4"] += 1
                    fout.writelines(table_lines)
                    in_create = False
                    table_lines = []
                continue

            if strip_view_db and CREATE_VIEW_RE.match(line):
                view_lines = [line]
                if line.rstrip().endswith(";"):
                    cleaned_view, vname, dbs = clean_view_statement(
                        line, explicit_view_db, stats
                    )
                    fout.write(cleaned_view)
                    if dbs:
                        cleaned_views.append((vname, sorted(dbs)))
                    view_lines = []
                else:
                    in_view = True
                continue

            if strip_view_db and DROP_TABLE_VIEW_RE.match(line):
                line = DROP_TABLE_VIEW_RE.sub(r"\1`\2`", line)
                stats["drop_db_prefix_stripped"] += 1

            m = CREATE_TABLE_RE.match(line)
            if m:
                in_create = True
                table_name = m.group(1)
                table_lines = [line]
                continue

            fout.write(line)

        if table_lines:
            fout.writelines(table_lines)
        if view_lines:
            full_view = "".join(view_lines)
            cleaned_view, vname, dbs = clean_view_statement(
                full_view, explicit_view_db, stats
            )
            fout.write(cleaned_view)
            if dbs:
                cleaned_views.append((vname, sorted(dbs)))

    return {
        "stats": stats,
        "upgraded_tables": upgraded_tables,
        "cleaned_views": cleaned_views,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fix MariaDB 11.x / MySQL 8.x SQL dumps for MariaDB 10.8+."
    )
    parser.add_argument("input", help="Path to source .sql file")
    parser.add_argument("output", help="Path to write fixed .sql file")
    parser.add_argument(
        "--strip-definer",
        action="store_true",
        help="Remove DEFINER=`user`@`host` clauses from views/procedures/triggers",
    )
    parser.add_argument(
        "--strip-view-db",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Strip hardcoded database qualifiers from CREATE VIEW statements (default: True, auto-detects DB names)",
    )
    parser.add_argument(
        "--view-db",
        default=None,
        help="Explicit database name to strip from VIEW statements (e.g. templefabrics_orbite). If omitted, auto-detected.",
    )
    args = parser.parse_args()

    result = fix_dump(
        args.input,
        args.output,
        args.strip_definer,
        strip_view_db=args.strip_view_db,
        explicit_view_db=args.view_db,
    )
    print(f"Fixed SQL written to: {args.output}")
    for key, val in sorted(result["stats"].items()):
        print(f"  {key}: {val}")
    for tbl, est in result["upgraded_tables"]:
        print(f"  upgraded `{tbl}` to utf8mb4_unicode_ci (estimated inline row: {est} B)")
    for vname, dbs in result["cleaned_views"]:
        print(f"  cleaned database prefix in view `{vname}`: {', '.join(dbs)}")


if __name__ == "__main__":
    main()
