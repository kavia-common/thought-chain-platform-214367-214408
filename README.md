# thought-chain-platform-214367-214408

## Dev Maintenance: Clear All Thoughts (Local Only)

A development-only endpoint allows wiping all saved thoughts for a clean test run without altering schema.

- Endpoint: DELETE /admin/dev/clear-thoughts
- Guarded by environment flag: DEV_MAINTENANCE=1
- Effect: Removes all rows from `thoughts` and `thought_token_guard`; does not change schema.

Usage locally:

1) Ensure the backend process is started with DEV_MAINTENANCE=1 (example):
   DEV_MAINTENANCE=1 uvicorn src.api.main:app --host 0.0.0.0 --port 3001

2) Call the endpoint (curl):
   curl -X DELETE http://localhost:3001/admin/dev/clear-thoughts

3) Or use the helper script:
   # Optionally override API_BASE; defaults to http://localhost:3001
   API_BASE=http://localhost:3001 python -m src.api.clear_thoughts_once

Note: In any non-dev environment, do not set DEV_MAINTENANCE=1; the endpoint will return 403.