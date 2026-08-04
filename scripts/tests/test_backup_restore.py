"""Backup/restore round-trip tests (P0-11)."""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from backup import backup_database, restore_database


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="bak_src_")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello'), ('world')")
    conn.commit()
    conn.close()
    yield path
    if os.path.exists(path):
        try:
            os.remove(path)
        except PermissionError:
            pass


def test_backup_creates_valid_db_file(db_path):
    fd, backup_path = tempfile.mkstemp(suffix=".bak", prefix="bak_out_")
    os.close(fd)
    os.remove(backup_path)
    try:
        backup_database(db_path, backup_path)
        assert os.path.exists(backup_path)
        # Backup is a readable SQLite file with the same data
        conn = sqlite3.connect(backup_path)
        rows = conn.execute("SELECT v FROM t ORDER BY id").fetchall()
        conn.close()
        assert rows == [("hello",), ("world",)]
    finally:
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except PermissionError:
                pass


def test_restore_replaces_target(db_path):
    # Target DB has different data
    fd, target_path = tempfile.mkstemp(suffix=".db", prefix="bak_tgt_")
    os.close(fd)
    conn = sqlite3.connect(target_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('old')")
    conn.commit()
    conn.close()

    # Make a backup of the source, then restore into target
    fd, backup_path = tempfile.mkstemp(suffix=".bak", prefix="bak_mid_")
    os.close(fd)
    os.remove(backup_path)
    try:
        backup_database(db_path, backup_path)
        restore_database(backup_path, target_path)

        conn = sqlite3.connect(target_path)
        rows = conn.execute("SELECT v FROM t ORDER BY id").fetchall()
        conn.close()
        assert rows == [("hello",), ("world",)]
    finally:
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except PermissionError:
                pass
        if os.path.exists(target_path):
            try:
                os.remove(target_path)
            except PermissionError:
                pass


def test_restore_from_missing_backup_raises(db_path):
    with pytest.raises(FileNotFoundError):
        restore_database("/no/such/backup.db", db_path)
