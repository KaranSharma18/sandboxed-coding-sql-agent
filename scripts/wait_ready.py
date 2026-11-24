"""Wait until the local stack answers /readyz, then say where it is (used by `poe dev`).

Polling the API works the same with every Docker Compose version, unlike `up --wait`, whose
handling of one-shot containers (migrations, bucket creation) has changed between releases.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

URL = "http://localhost:8080/readyz"
TIMEOUT_S = 300


def main() -> int:
    deadline = time.monotonic() + TIMEOUT_S
    last = "no answer yet"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(URL, timeout=5) as response:
                body = json.loads(response.read())
                if body.get("ready"):
                    runners = body.get("checks", {}).get("runners", 0)
                    print(f"Fixloop is up: http://localhost:8080/docs  (runners alive: {runners})")
                    return 0
                last = json.dumps(body.get("checks"))
        except (urllib.error.URLError, ConnectionError, TimeoutError, ValueError) as exc:
            last = str(exc)
        time.sleep(3)
    print(
        f"the stack did not become ready in {TIMEOUT_S}s ({last}).\n"
        "Look at: docker compose ps   and   docker compose logs migrate api worker runner",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
