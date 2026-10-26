"""Take a test database back to an older schema version, so the store re-runs the later migrations on it (r30).

A migration test writes rows with a real store (they need its key), takes the file back to version ``v`` and opens
it again. Setting ``PRAGMA user_version`` back alone re-runs every later migration, and two of them can't run twice
("duplicate column name"): migration 43 (lCore, 10 Oct) adds the skills' learner columns and moments' ``entity_key``,
and migration 49 (mon1, 22 Oct) adds meanings' ``called_ciphertext``. :func:`rewind` drops those columns first (their
indexes before them: SQLite refuses to drop an indexed column), then sets the version. The other migrations after 37
only create what is missing (``IF NOT EXISTS``) or move data idempotently, so they re-run as they would on an old file.

For versions below 43 only. A test that takes back a migration of its own before 43 (its columns and tables) still
does that itself, before calling :func:`rewind`.
"""

from __future__ import annotations

import sqlite3

#: The columns migrations 43 and 49 add with ``ALTER TABLE ... ADD COLUMN``, by table.
ADDED_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("skills", ("capability_id", "spec_ciphertext", "name_ciphertext", "provenance", "deleted_at")),   # 43
    ("moments", ("entity_key",)),                                                                     # 43
    ("meanings", ("called_ciphertext",)),                                                             # 49
)
#: The indexes on those columns (migration 43), dropped before them.
ADDED_INDEXES: tuple[str, ...] = ("skills_capability", "moments_entity")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def rewind(conn: sqlite3.Connection, version: int) -> None:
    """``conn``'s database back to schema ``version`` (< 43): the indexes and columns that migrations 43 and 49 add are
    dropped (those present), then ``PRAGMA user_version = version``, committed."""
    if not 0 <= int(version) < 43:
        raise ValueError(f"rewind covers versions below 43 only, not {version}")
    for name in ADDED_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    for table, columns in ADDED_COLUMNS:
        have = _columns(conn, table)
        for column in columns:
            if column in have:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    conn.execute(f"PRAGMA user_version = {int(version)}")
    conn.commit()
