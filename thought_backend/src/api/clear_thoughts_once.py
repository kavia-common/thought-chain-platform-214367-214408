#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import json
import urllib.request
import urllib.error


# PUBLIC_INTERFACE
def run_clear(base_url: str) -> int:
    """One-time helper to invoke the dev maintenance endpoint to clear all thoughts.

    This requires the backend to be running and the environment variable DEV_MAINTENANCE=1
    set for the backend process. Intended for local/dev only.

    Parameters:
        base_url: Base URL of the running backend (e.g., http://localhost:3001).

    Returns:
        Exit code 0 on success, non-zero otherwise.
    """
    url = base_url.rstrip("/") + "/admin/dev/clear-thoughts"
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read().decode("utf-8")
            try:
                payload = json.loads(data)
            except Exception:
                payload = {"raw": data}
            print("Success:", json.dumps(payload, indent=2))
            return 0
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        print(f"HTTP {e.code}: {body}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"Error calling endpoint: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    # Default to http://localhost:3001 if not provided
    # Usage:
    #   DEV_MAINTENANCE=1 uvicorn src.api.main:app --port 3001
    #   API_BASE=http://localhost:3001 python -m src.api.clear_thoughts_once
    base = os.environ.get("API_BASE", "http://localhost:3001")
    sys.exit(run_clear(base))
