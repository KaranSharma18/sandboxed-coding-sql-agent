# fixloop-pytest

Runs inside the sandbox. Two pieces, no dependencies, Python 3.10+:

* `fixloop_pytest.plugin`: a pytest plugin that writes `report.jsonl` (collection, per-test
  outcomes with in-repository frames and assertion details, per-test coverage of changed files)
  and supports a per-test timeout and a deep trace (local variables per frame).
* `fixloop_pytest.prepare`: builds the task's virtual environment with uv, wheels only, from the
  allowlisted index (or offline from the image's pytest wheelhouse).

```text
python -m pytest -p fixloop_pytest.plugin --fixloop-out=OUT --junitxml=OUT/junit.xml ...
python -m fixloop_pytest.prepare --venv /opt/venv --requirements reqs.txt --index-url URL --report OUT/prepare.json
```
