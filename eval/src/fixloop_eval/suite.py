"""Suites are JSON files of cases. A case is a task spec plus what we expect to happen.

A case's source may be `{"type": "example", "path": "examples/buggy-dates"}`: the folder is read
from the repository and sent inline. `hidden_tests` (a folder) is uploaded and used as the gold
check the model never sees.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fixloop_client.repo import list_files, make_archive, source_for

ROOT = Path(__file__).resolve().parents[3]


@dataclass
class Case:
    name: str
    spec: dict[str, Any]
    expect_outcome: str = "verified"
    hidden_tests: str | None = None
    tags: list[str] = field(default_factory=list)


def load_suite(name: str) -> list[Case]:
    folder = ROOT / "eval" / "suites" / name
    if not folder.is_dir():
        raise FileNotFoundError(f"no suite named {name!r} under eval/suites/")
    cases: list[Case] = []
    for path in sorted(folder.glob("*.json")):
        for raw in json.loads(path.read_text(encoding="utf-8")):
            cases.append(
                Case(
                    name=raw["name"],
                    spec=raw["spec"],
                    expect_outcome=raw.get("expect", "verified"),
                    hidden_tests=raw.get("hidden_tests"),
                    tags=raw.get("tags", []),
                )
            )
    return cases


def materialize(case: Case, upload: Any) -> dict[str, Any]:
    """Turn a case into a real task spec (reading example folders, uploading hidden tests)."""
    spec = json.loads(json.dumps(case.spec))
    source = spec.get("source", {})
    if source.get("type") == "example":
        spec["source"] = source_for(ROOT / source["path"], upload)
    elif source.get("type") == "sql_example":
        folder = ROOT / source["path"]
        spec["source"] = {
            "type": "sql_snapshot",
            "ddl": (folder / "schema.sql").read_text(encoding="utf-8"),
            "seeds": [
                {"table": p.stem, "inline_csv": p.read_text(encoding="utf-8")}
                for p in sorted(folder.glob("*.csv"))
                if p.stem != "expected"
            ],
        }
        reference = folder / "reference.sql"
        if reference.exists() and "reference_query" not in spec["oracle"]:
            spec["oracle"]["reference_query"] = reference.read_text(encoding="utf-8")
    if case.hidden_tests:
        folder = ROOT / case.hidden_tests
        spec["oracle"]["hidden_tests"] = upload(
            make_archive(folder, list_files(folder)), "hidden_tests", "application/gzip"
        )
    return spec
