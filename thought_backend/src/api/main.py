from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import List

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

# Application metadata and tags for OpenAPI
app = FastAPI(
    title="Daily Thought Chain API",
    description="API for submitting and retrieving daily thoughts. "
    "Each user can submit a single thought per UTC day. "
    "Thoughts are stored in SQLite and returned in chronological order.",
    version="1.0.0",
    openapi_tags=[
        {
            "name": "Health",
            "description": "Service status and health checks.",
        },
        {
            "name": "Thoughts",
            "description": "Submit and fetch daily thoughts. One per user per UTC day.",
        },
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict CORS domains
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _read_sqlite_path_from_db_container() -> str:
    """
    Read the SQLite DB file path from the database container's db_connection.txt.
    The file includes comments and a 'File path:' line. We parse the first absolute path found.

    Returns:
        str: Absolute filesystem path to the SQLite DB file.

    Raises:
        RuntimeError: If the file cannot be read or a path cannot be determined.
    """
    # Path relative to this backend workspace
    info_file = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "../../../../thought-chain-platform-214367-214407/thought_database/db_connection.txt",
        )
    )

    if not os.path.exists(info_file):
        raise RuntimeError(f"Database connection file not found at: {info_file}")

    with open(info_file, "r", encoding="utf-8") as f:
        content = f.read()

    # Try to find an absolute POSIX path in the file
    # Prefer the explicit 'File path:' line, else pick first absolute-looking path.
    file_path_match = re.search(r"File path:\s*(/.+)", content)
    if file_path_match:
        db_path = file_path_match.group(1).strip()
        if os.path.isabs(db_path):
            return db_path

    # Fallback: any absolute path in file
    any_path = re.search(r"(/[^ \n]+\.db)", content)
    if any_path:
        db_path = any_path.group(1).strip()
        if os.path.isabs(db_path):
            return db_path

    raise RuntimeError("Could not parse SQLite DB file path from db_connection.txt")


def _get_db_connection() -> sqlite3.Connection:
    """
    Open a connection to the SQLite database using the path from db_connection.txt.
    Returns a connection with row factory set to sqlite3.Row for column access by name.
    """
    db_path = _read_sqlite_path_from_db_container()
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """
    Ensure the 'thoughts' table exists with the required columns.
    This is idempotent and safe to call at startup or per-request if needed.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thoughts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            thought_text TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


# PUBLIC_INTERFACE
class ThoughtIn(BaseModel):
    """Pydantic model for incoming thought submissions."""

    username: str = Field(..., description="The username of the person submitting the thought.", min_length=1, max_length=50)
    thought_text: str = Field(..., description="The textual content of the user's thought.", min_length=1, max_length=500)

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Username cannot be empty.")
        if len(v) > 50:
            raise ValueError("Username must be at most 50 characters.")
        return v

    @field_validator("thought_text")
    @classmethod
    def validate_thought_text(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Thought text cannot be empty.")
        if len(v) > 500:
            raise ValueError("Thought text must be at most 500 characters.")
        return v


# PUBLIC_INTERFACE
class ThoughtOut(BaseModel):
    """Pydantic model for outgoing thought objects."""

    id: int = Field(..., description="Unique identifier of the thought.")
    username: str = Field(..., description="Username of the author.")
    thought_text: str = Field(..., description="Thought text.")
    created_at: str = Field(..., description="Creation timestamp in ISO 8601 UTC.")


def _row_to_thought_out(row: sqlite3.Row) -> ThoughtOut:
    """
    Convert a DB row to ThoughtOut model, normalizing created_at to ISO format string in UTC if possible.
    """
    created_val = row["created_at"]
    created_iso: str
    try:
        # If created_at is a string like '2025-11-28 05:32:15'
        if isinstance(created_val, str):
            # Treat as naive UTC timestamp stored by SQLite CURRENT_TIMESTAMP
            dt = datetime.fromisoformat(created_val.replace(" ", "T"))
            dt = dt.replace(tzinfo=timezone.utc)
            created_iso = dt.isoformat()
        else:
            created_iso = str(created_val)
    except Exception:
        created_iso = str(created_val)

    return ThoughtOut(
        id=row["id"],
        username=row["username"],
        thought_text=row["thought_text"],
        created_at=created_iso,
    )


@app.get(
    "/",
    summary="Health Check",
    tags=["Health"],
)
def health_check():
    """
    Health check endpoint.

    Returns:
        JSON object with a simple message indicating service is healthy.
    """
    return {"message": "Healthy"}


@app.get(
    "/thoughts",
    response_model=List[ThoughtOut],
    summary="List all thoughts (oldest first)",
    description="Fetch all thoughts ordered by creation time ascending (oldest first).",
    tags=["Thoughts"],
)
def list_thoughts() -> List[ThoughtOut]:
    """
    Retrieve all thoughts ordered oldest first.

    Returns:
        List[ThoughtOut]: All stored thoughts chronologically.
    """
    conn = _get_db_connection()
    try:
        _ensure_schema(conn)
        cur = conn.execute(
            """
            SELECT id, username, thought_text, created_at
            FROM thoughts
            ORDER BY datetime(created_at) ASC, id ASC
            """
        )
        rows = cur.fetchall()
        return [_row_to_thought_out(r) for r in rows]
    finally:
        conn.close()


@app.post(
    "/thoughts",
    response_model=ThoughtOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new thought (one per user per UTC day)",
    description=(
        "Create a thought for a user. Inputs are trimmed and validated. "
        "Enforces one thought per user per UTC day using SQLite date('now'). "
        "Returns 409 if the user already submitted a thought today."
    ),
    tags=["Thoughts"],
    responses={
        201: {"description": "Thought created successfully."},
        400: {"description": "Validation error."},
        409: {"description": "Duplicate submission for user in current UTC day."},
        500: {"description": "Server error."},
    },
)
def create_thought(payload: ThoughtIn) -> ThoughtOut:
    """
    Create a new thought entry.

    Parameters:
        payload (ThoughtIn): Contains `username` and `thought_text`. Both are trimmed and validated.

    Returns:
        ThoughtOut: The created thought row.

    Errors:
        409 on duplicate per-user-per-UTC-day.
    """
    username = payload.username.strip()
    thought_text = payload.thought_text.strip()

    conn = _get_db_connection()
    try:
        _ensure_schema(conn)

        # Duplicate check: one per user per UTC day using date('now') which is UTC in SQLite.
        duplicate_cur = conn.execute(
            """
            SELECT id FROM thoughts
            WHERE username = ?
              AND date(created_at) = date('now')
            LIMIT 1
            """,
            (username,),
        )
        dup = duplicate_cur.fetchone()
        if dup:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User has already submitted a thought today (UTC).",
            )

        # Insert
        cur = conn.execute(
            """
            INSERT INTO thoughts (username, thought_text)
            VALUES (?, ?)
            """,
            (username, thought_text),
        )
        new_id = cur.lastrowid
        conn.commit()

        # Fetch the inserted row
        fetch_cur = conn.execute(
            """
            SELECT id, username, thought_text, created_at
            FROM thoughts
            WHERE id = ?
            """,
            (new_id,),
        )
        row = fetch_cur.fetchone()
        if not row:
            # Extremely unlikely; handle gracefully
            raise HTTPException(status_code=500, detail="Failed to retrieve created thought.")
        return _row_to_thought_out(row)
    finally:
        conn.close()
