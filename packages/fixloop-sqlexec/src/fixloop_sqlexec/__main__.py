"""Command line used by the sandbox command templates.

python -m fixloop_sqlexec prepare --spec sql_prepare.json --env /env --out /out
python -m fixloop_sqlexec run     --spec sql_run.json     --env /env --out /out
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

from fixloop_sqlexec.engines import engine_for


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fixloop-sqlexec")
    parser.add_argument("command", choices=["prepare", "run"])
    parser.add_argument("--spec", required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--work", default=None, help="scratch directory for a running Postgres copy")
    args = parser.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    with open(args.spec, encoding="utf-8") as handle:
        spec = json.load(handle)
    name = "env.json" if args.command == "prepare" else "sql_result.json"
    try:
        engine = engine_for(spec["dialect"], args.env, args.work)
        payload = engine.prepare(spec) if args.command == "prepare" else engine.run(spec)
        payload["ok"] = True
        code = 0
    except Exception as exc:
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:3000],
            "traceback": traceback.format_exc()[-3000:],
        }
        code = 1
    with open(os.path.join(args.out, name), "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    if args.command == "prepare" and payload.get("ok"):
        with open(os.path.join(args.env, "meta.json"), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    return code


if __name__ == "__main__":
    sys.exit(main())
