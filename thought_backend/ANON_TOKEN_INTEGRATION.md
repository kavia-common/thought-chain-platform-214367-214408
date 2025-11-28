# Anonymous Token Per-Day Restriction (Backend Integration Guide)

- The backend now requires a `token` field in POST /thoughts to enforce 1 submission per UTC day per token.
- Tokens never appear in GET responses.
- Existing edit_token behavior for PATCH/DELETE is unchanged.

## Example Requests

POST /thoughts
Content-Type: application/json
{
  "username": "Alice",
  "thought_text": "Grateful for the sunshine.",
  "token": "local-uuid-or-random-string"
}

201 Created
{
  "id": 42,
  "username": "Alice",
  "thought_text": "Grateful for the sunshine.",
  "created_at": "2025-11-28T06:00:00+00:00",
  "edit_token": "s3cr3t-edit-token"
}

Duplicate same UTC day with same token
409 Conflict
{
  "detail": "This token has already submitted a thought today (UTC). Try again tomorrow."
}

GET /thoughts
200 OK
[
  {
    "id": 41,
    "username": "Bob",
    "thought_text": "Learned something new.",
    "created_at": "2025-11-28T05:20:00+00:00"
  }
]

## Frontend Integration (React pseudocode)

- Generate or load a persistent anonymous token from localStorage.
- Include it in the POST /thoughts payload.

```js
function getOrCreateAnonToken() {
  const key = 'dailyThoughtAnonToken';
  let tok = localStorage.getItem(key);
  if (!tok) {
    // Use crypto for randomness; keep length >= 16
    const arr = new Uint8Array(16);
    crypto.getRandomValues(arr);
    tok = Array.from(arr).map(b => b.toString(16).padStart(2, '0')).join('');
    localStorage.setItem(key, tok);
  }
  return tok;
}

async function submitThought(apiBase, username, thoughtText) {
  const token = getOrCreateAnonToken();
  const res = await fetch(`${apiBase}/thoughts`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, thought_text: thoughtText, token }),
  });
  if (res.status === 201) {
    const data = await res.json();
    // Save edit_token if you plan to allow client-side edit/delete later
    return data;
  }
  if (res.status === 409) {
    const err = await res.json();
    throw new Error(err.detail || 'Already submitted today.');
  }
  const err = await res.json().catch(() => ({}));
  throw new Error(err.detail || `Unexpected error (${res.status})`);
}
```

Notes:
- Token constraints: 8..200 characters.
- The server uses UTC days (SQLite date('now')).
- No .env changes required for this feature.
