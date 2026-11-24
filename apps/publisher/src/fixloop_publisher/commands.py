"""`/fixloop` comment commands on issues and pull requests.

    /fixloop fix tests/test_dates.py::test_parse [more node ids] [--repairs 2]

Only collaborators with write, maintain or admin permission can start a task; everyone else
gets a polite refusal. On a pull request the task runs on the PR's head commit and the fix is
proposed as a draft PR into the PR's branch. Pull requests from forks are not run: their head
branch lives in a repository the App was not granted.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from fixloop_core.spec import validate_node_id

ALLOWED_PERMISSIONS = {"admin", "maintain", "write"}
HELP = (
    "Usage: `/fixloop fix <pytest node id> [more ids] [--repairs N]`, for example "
    "`/fixloop fix tests/test_dates.py::test_parse_dates`."
)


class CommandError(ValueError):
    pass


@dataclass
class FixCommand:
    targets: list[str]
    repairs: int = 3


def parse(body: str) -> FixCommand:
    first = body.strip().splitlines()[0] if body.strip() else ""
    try:
        words = shlex.split(first)
    except ValueError as exc:
        raise CommandError(f"could not read the command: {exc}. {HELP}") from exc
    if len(words) < 2 or words[0] != "/fixloop":
        raise CommandError(HELP)
    if words[1] != "fix":
        raise CommandError(f"unknown command `{words[1]}`. {HELP}")
    targets: list[str] = []
    repairs = 3
    rest = iter(words[2:])
    for word in rest:
        if word == "--repairs":
            try:
                repairs = max(0, min(3, int(next(rest))))
            except (StopIteration, ValueError) as exc:
                raise CommandError("--repairs needs a number from 0 to 3") from exc
            continue
        try:
            targets.append(validate_node_id(word))
        except ValueError as exc:
            raise CommandError(f"`{word}` is not a pytest node id. {HELP}") from exc
    if not targets:
        raise CommandError(f"name at least one target test. {HELP}")
    return FixCommand(targets=targets[:50], repairs=repairs)
