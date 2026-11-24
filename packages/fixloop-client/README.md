# fixloop-client

A small Python client for the Fixloop API and the `fixloop` command line.

```python
from fixloop_client import FixloopClient

with FixloopClient("http://localhost:8080", "fl_live_...") as client:
    task = client.create_task(spec, idempotency_key="my-task-1")
    for event in client.events(task["id"]):
        print(event["type"])
    diff, verified = client.patch(task["id"])
```

`fixloop --help` lists the CLI commands (`login`, `fix`, `sql`, `watch`, `status`, `patch`, `list`,
`cancel`, `retry`).
