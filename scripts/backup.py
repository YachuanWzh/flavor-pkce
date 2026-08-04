"""SQLite online backup / restore helpers (P0-11).

Uses the sqlite3 ``backup()`` API so the source database can be backed up
while the server is running (consistent snapshot, no downtime).
"""

import argparse
import datetime
import os
import sqlite3
import sys
from pathlib import Path


def backup_database(src_path: str | os.PathLike, dst_path: str | os.PathLike) -> None:
    """Create a consistent snapshot of ``src_path`` into ``dst_path``."""
    dst_path = os.fspath(dst_path)
    Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(os.fspath(src_path))
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def restore_database(backup_path: str | os.PathLike, target_path: str | os.PathLike) -> None:
    """Replace ``target_path`` with the contents of ``backup_path``."""
    if not os.path.exists(os.fspath(backup_path)):
        raise FileNotFoundError(f"Backup file not found: {backup_path}")
    backup_database(backup_path, target_path)


def _default_backup_name(db_path: str) -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{Path(db_path).stem}-{stamp}.db.bak"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SQLite backup/restore")
    sub = parser.add_subparsers(dest="command", required=True)

    p_backup = sub.add_parser("backup", help="Create a consistent snapshot")
    p_backup.add_argument("db", help="Source database file")
    p_backup.add_argument("--out", help="Destination file (default: <db>-<timestamp>.db.bak)")

    p_restore = sub.add_parser("restore", help="Restore a backup over a database")
    p_restore.add_argument("backup", help="Backup file")
    p_restore.add_argument("db", help="Target database file")

    args = parser.parse_args(argv)

    if args.command == "backup":
        out = args.out or _default_backup_name(args.db)
        backup_database(args.db, out)
        print(f"Backed up {args.db} -> {out}")
    else:
        restore_database(args.backup, args.db)
        print(f"Restored {args.backup} -> {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
