from __future__ import annotations

import sqlite3
from typing import Optional

from .main import _get_db_connection, _ensure_schema  # reuse implementation


# PUBLIC_INTERFACE
def upgrade_database(conn: Optional[sqlite3.Connection] = None) -> None:
    """Upgrade the SQLite database schema in-place, idempotently.

    This function ensures:
    - thoughts table exists
    - columns edit_token, updated_at, token exist
    - guard table thought_token_guard exists with UNIQUE(token, day_key)
    - helpful indexes exist

    If conn is not provided, a new connection is opened and closed.
    """
    owns_conn = False
    if conn is None:
        conn = _get_db_connection()
        owns_conn = True
    try:
        _ensure_schema(conn)
    finally:
        if owns_conn:
            conn.close()


if __name__ == "__main__":
    upgrade_database()
    print("Database upgraded successfully.")
