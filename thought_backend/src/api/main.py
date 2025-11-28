from __future__ import annotations

import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException, status, Header, Query, Path
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

# Application metadata and tags for OpenAPI
app = FastAPI(
    title="Daily Thought Chain API",
    description="API for submitting and retrieving daily thoughts. "
    "Each user can submit a single thought per UTC day. "
    "Thoughts are stored in SQLite and returned in chronological order.",
    version="1.2.0",
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


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a column exists on a table."""
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = [r["name"] for r in cur.fetchall()]
    return column in cols


def _index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    """Check if an index exists by name."""
    cur = conn.execute("PRAGMA index_list(thoughts)")
    indexes = [r["name"] if isinstance(r, sqlite3.Row) else r[1] for r in cur.fetchall()]
    return index_name in indexes


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """
    Ensure the 'thoughts' table exists with the required columns.
    Additive/idempotent: also ensure nullable edit_token and updated_at exist.
    Additionally, add required token column and create an index guard that helps
    enforce (token, date(created_at)) uniqueness at application level for SQLite.
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
    # Idempotent ALTERs
    if not _column_exists(conn, "thoughts", "edit_token"):
        conn.execute("ALTER TABLE thoughts ADD COLUMN edit_token TEXT")
    if not _column_exists(conn, "thoughts", "updated_at"):
        conn.execute("ALTER TABLE thoughts ADD COLUMN updated_at DATETIME")
    # Add anonymous token column (TEXT NOT NULL defaulting to empty then backfill not possible easily;
    # keep as nullable but enforce NOT NULL at application layer for existing rows).
    if not _column_exists(conn, "thoughts", "token"):
        conn.execute("ALTER TABLE thoughts ADD COLUMN token TEXT")

    # Create a helper computed day column if not present via companion table approach.
    # Because SQLite doesn't support generated columns with date(created_at) reliably across versions here,
    # we create a side table to guard uniqueness (token, day_key). It's additive and idempotent.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thought_token_guard (
            thought_id INTEGER UNIQUE,
            token TEXT NOT NULL,
            day_key TEXT NOT NULL,
            UNIQUE(token, day_key)
        )
        """
    )
    # Indexes to speed up lookups (idempotent via IF NOT EXISTS not supported for index_list; check manually)
    if not _index_exists(conn, "idx_thoughts_created_at"):
        conn.execute("CREATE INDEX idx_thoughts_created_at ON thoughts (created_at)")
    if not _index_exists(conn, "idx_thoughts_token"):
        conn.execute("CREATE INDEX idx_thoughts_token ON thoughts (token)")
    conn.commit()


# PUBLIC_INTERFACE
class ThoughtIn(BaseModel):
    """Pydantic model for incoming thought submissions."""

    username: str = Field(..., description="The username of the person submitting the thought.", min_length=1, max_length=50)
    thought_text: str = Field(..., description="The textual content of the user's thought.", min_length=1, max_length=500)
    token: str = Field(..., description="Anonymous client token to enforce 1 submission per UTC day.", min_length=8, max_length=200)

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

    @field_validator("token")
    @classmethod
    def validate_token(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Token is required.")
        if len(v) < 8 or len(v) > 200:
            raise ValueError("Token length must be between 8 and 200 characters.")
        return v


# PUBLIC_INTERFACE
class ThoughtOut(BaseModel):
    """Pydantic model for outgoing thought objects."""

    id: int = Field(..., description="Unique identifier of the thought.")
    username: str = Field(..., description="Username of the author.")
    thought_text: str = Field(..., description="Thought text.")
    created_at: str = Field(..., description="Creation timestamp in ISO 8601 UTC.")


# PUBLIC_INTERFACE
class ThoughtCreatedResponse(ThoughtOut):
    """Extended create response including edit_token. Not used for listing."""
    edit_token: str = Field(..., description="Secret token required to edit or delete this thought. Keep it safe.")


# PUBLIC_INTERFACE
class ThoughtPatchIn(BaseModel):
    """Payload for updating an existing thought's text."""

    thought_text: str = Field(..., description="The new textual content of the thought.", min_length=1, max_length=500)

    @field_validator("thought_text")
    @classmethod
    def validate_thought_text(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Thought text cannot be empty.")
        if len(v) > 500:
            raise ValueError("Thought text must be at most 500 characters.")
        return v


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


def _day_key_utc_now() -> str:
    """Return current UTC date key in YYYY-MM-DD using SQLite-compatible 'now' semantics replicated in app."""
    # We base on UTC now to align with SQLite date('now')
    return datetime.utcnow().strftime("%Y-%m-%d")


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
    response_model=ThoughtCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new thought (one per user per UTC day)",
    description=(
        "Create a thought for a user. Inputs are trimmed and validated. "
        "Enforces one thought per user per UTC day by anonymous token. "
        "Returns 409 if the token already submitted a thought today. "
        "Response includes an edit_token which is required to edit/delete the item."
    ),
    tags=["Thoughts"],
    responses={
        201: {"description": "Thought created successfully."},
        400: {"description": "Validation error."},
        409: {"description": "Duplicate submission for token in current UTC day."},
        500: {"description": "Server error."},
    },
)
def create_thought(payload: ThoughtIn) -> ThoughtCreatedResponse:
    """
    Create a new thought entry.

    Parameters:
        payload (ThoughtIn): Contains `username`, `thought_text`, and `token`. All are trimmed and validated.

    Returns:
        ThoughtCreatedResponse: The created thought row with a secret edit_token.

    Errors:
        409 on duplicate per-token-per-UTC-day.
    """
    # Extra guardrails to ensure clear messages even if upstream validation is bypassed
    username = (payload.username or "").strip()
    thought_text = (payload.thought_text or "").strip()
    anon_token = (payload.token or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username cannot be empty.")
    if len(username) > 50:
        raise HTTPException(status_code=400, detail="Username must be at most 50 characters.")
    if not thought_text:
        raise HTTPException(status_code=400, detail="Thought text cannot be empty.")
    if len(thought_text) > 500:
        raise HTTPException(status_code=400, detail="Thought text must be at most 500 characters.")
    if not anon_token:
        raise HTTPException(status_code=400, detail="Token is required.")
    if len(anon_token) < 8 or len(anon_token) > 200:
        raise HTTPException(status_code=400, detail="Token length must be between 8 and 200 characters.")

    edit_token = secrets.token_urlsafe(16)

    conn = _get_db_connection()
    try:
        _ensure_schema(conn)

        # Application-level duplicate check by token for the current UTC day using SQLite date('now')
        duplicate_cur = conn.execute(
            """
            SELECT t.id
            FROM thoughts t
            WHERE t.token = ?
              AND date(t.created_at) = date('now')
            LIMIT 1
            """,
            (anon_token,),
        )
        dup = duplicate_cur.fetchone()
        if dup:
            # 409 Conflict with clear string detail
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This token has already submitted a thought today (UTC). Try again tomorrow.",
            )

        # Insert primary row
        cur = conn.execute(
            """
            INSERT INTO thoughts (username, thought_text, edit_token, token)
            VALUES (?, ?, ?, ?)
            """,
            (username, thought_text, edit_token, anon_token),
        )
        new_id = cur.lastrowid

        # Insert guard row to enforce uniqueness (token, day_key) at logical level
        day_key = _day_key_utc_now()
        conn.execute(
            """
            INSERT OR IGNORE INTO thought_token_guard (thought_id, token, day_key)
            VALUES (?, ?, ?)
            """,
            (new_id, anon_token, day_key),
        )

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

        base = _row_to_thought_out(row)
        # Return extended response including token (not exposed in listing)
        return ThoughtCreatedResponse(**base.model_dump(), edit_token=edit_token)
    finally:
        conn.close()


@app.patch(
    "/thoughts/{thought_id}",
    response_model=ThoughtOut,
    summary="Update a thought's text (token required)",
    description=(
        "Update the thought_text of a thought. Requires the correct edit token supplied either via "
        "header 'X-Edit-Token' or query parameter 'token'. Trims and validates text. "
        "Sets updated_at to CURRENT_TIMESTAMP on success."
    ),
    tags=["Thoughts"],
    responses={
        200: {"description": "Thought updated successfully."},
        400: {"description": "Validation error."},
        403: {"description": "Invalid or missing edit token."},
        404: {"description": "Thought not found."},
    },
)
def update_thought(
    thought_id: int = Path(..., description="ID of the thought to update."),
    payload: ThoughtPatchIn = ...,
    x_edit_token: Optional[str] = Header(None, alias="X-Edit-Token"),
    token: Optional[str] = Query(None, description="Edit token (alternative to header)."),
) -> ThoughtOut:
    """
    Update an existing thought's text.

    Parameters:
        thought_id: The target thought ID.
        payload: Contains the new thought_text.
        x_edit_token: Optional token from header 'X-Edit-Token'.
        token: Optional token from query param 'token'.

    Returns:
        ThoughtOut: The updated thought.

    Errors:
        403 if token is missing/invalid; 404 if id not found.
    """
    provided = (x_edit_token or token or "").strip()
    if not provided:
        # 403 with clear message
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing edit token.")

    new_text = payload.thought_text.strip()
    if not new_text or len(new_text) > 500:
        raise HTTPException(status_code=400, detail="Invalid thought_text (1..500 chars required).")

    conn = _get_db_connection()
    try:
        _ensure_schema(conn)

        # Verify id exists and token matches
        cur = conn.execute("SELECT id, edit_token FROM thoughts WHERE id = ?", (thought_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Thought not found.")
        if row["edit_token"] != provided:
            raise HTTPException(status_code=403, detail="Invalid edit token.")

        # Perform update and set updated_at
        conn.execute(
            """
            UPDATE thoughts
            SET thought_text = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (new_text, thought_id),
        )
        conn.commit()

        # Return updated record (without token)
        fetch_cur = conn.execute(
            "SELECT id, username, thought_text, created_at FROM thoughts WHERE id = ?",
            (thought_id,),
        )
        row2 = fetch_cur.fetchone()
        if not row2:
            raise HTTPException(status_code=500, detail="Failed to load updated thought.")
        return _row_to_thought_out(row2)
    finally:
        conn.close()


@app.delete(
    "/thoughts/{thought_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a thought (token required)",
    description=(
        "Delete a thought by ID. Requires the correct edit token supplied via header 'X-Edit-Token' "
        "or query parameter 'token'. Returns 204 on success, 403 on invalid token, 404 if not found."
    ),
    tags=["Thoughts"],
    responses={
        204: {"description": "Thought deleted."},
        403: {"description": "Invalid or missing edit token."},
        404: {"description": "Thought not found."},
    },
)
def delete_thought(
    thought_id: int = Path(..., description="ID of the thought to delete."),
    x_edit_token: Optional[str] = Header(None, alias="X-Edit-Token"),
    token: Optional[str] = Query(None, description="Edit token (alternative to header)."),
):
    """
    Delete the given thought.

    Parameters:
        thought_id: The target thought ID.
        x_edit_token: Optional token from header 'X-Edit-Token'.
        token: Optional token from query param 'token'.

    Returns:
        204 No Content on success.

    Errors:
        403 if token is missing/invalid; 404 if id not found.
    """
    provided = (x_edit_token or token or "").strip()
    if not provided:
        # 403 with clear message
        raise HTTPException(status_code=403, detail="Missing edit token.")

    conn = _get_db_connection()
    try:
        _ensure_schema(conn)
        cur = conn.execute("SELECT id, edit_token FROM thoughts WHERE id = ?", (thought_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Thought not found.")
        if row["edit_token"] != provided:
            raise HTTPException(status_code=403, detail="Invalid edit token.")

        conn.execute("DELETE FROM thoughts WHERE id = ?", (thought_id,))
        conn.commit()
        return
    finally:
        conn.close()
