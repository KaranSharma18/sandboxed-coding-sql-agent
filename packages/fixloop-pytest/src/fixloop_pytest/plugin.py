"""Fixloop's pytest plugin (loaded explicitly with `-p fixloop_pytest.plugin`).

Writes one JSON object per line to `<out>/report.jsonl`:
  {"type": "collection", "items": [...], "errors": [...]}
  {"type": "test", "nodeid", "when", "outcome", "duration", "wasxfail", "exc_type", "message",
   "assertion", "frames": [...], "stdout", "stderr", "longrepr"}
  {"type": "coverage", "nodeid", "lines": {"path": [line, ...]}}     (only with --fixloop-changed)
  {"type": "summary", "exitstatus", "duration", "python"}
and, with --fixloop-deep-trace, `<out>/deeptrace.json`.

Why our own plugin (ADR-14): pytest's terminal output changes between versions and plugins and
is ambiguous; this gives one stable, machine-readable record per test, the collected set and
changed-line coverage in one place. It has no dependencies and supports pytest 7 to 9.

The plugin must never break a test run: every hook body is guarded.
"""

from __future__ import annotations

import json
import os
import random
import re
import signal
import sys
import time
import traceback
from typing import Any

MAX_TEXT = 4000
MAX_LONGREPR = 6000
SECRETISH = re.compile(r"pass(word|wd)?|secret|token|api_?key|credential|private", re.I)


class FixloopTestTimeout(Exception):
    """Raised inside a test that exceeded the per-test time limit."""


def pytest_addoption(parser: Any) -> None:
    group = parser.getgroup("fixloop", "Fixloop reporting")
    group.addoption("--fixloop-out", default=None, help="directory for report.jsonl")
    group.addoption("--fixloop-test-timeout", type=float, default=0.0, help="per-test timeout in seconds")
    group.addoption(
        "--fixloop-changed", default="", help="comma-separated files whose executed lines to record"
    )
    group.addoption("--fixloop-deep-trace", default="", help="node id to trace in depth")
    group.addoption(
        "--fixloop-shuffle-seed", type=int, default=None, help="shuffle test order with this seed"
    )


def pytest_configure(config: Any) -> None:
    out = config.getoption("--fixloop-out", default=None)
    if out:
        config.pluginmanager.register(_Reporter(config, out), "fixloop-reporter")


def _clip(text: Any, limit: int = MAX_TEXT) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[: limit // 2] + "\n...[cut]...\n" + text[-limit // 2 :]


class _Reporter:
    def __init__(self, config: Any, out: str) -> None:
        self.config = config
        self.root = os.path.realpath(str(config.rootpath if hasattr(config, "rootpath") else config.rootdir))
        os.makedirs(out, exist_ok=True)
        self.out = out
        # Opened before collection, so test code never decides where results go.
        self._fh = open(os.path.join(out, "report.jsonl"), "w", encoding="utf-8")  # noqa: SIM115
        self._started = time.monotonic()
        self._collection_errors: list[dict[str, Any]] = []
        self.timeout = float(config.getoption("--fixloop-test-timeout") or 0)
        changed = [p for p in (config.getoption("--fixloop-changed") or "").split(",") if p]
        self.coverage = _LineCollector(self.root, changed) if changed else None
        self.deep_target = config.getoption("--fixloop-deep-trace") or ""
        self.deep: _DeepTracer | None = None
        self.seed = config.getoption("--fixloop-shuffle-seed")

    # -------------------------------------------------------------- output
    def _write(self, record: dict[str, Any]) -> None:
        try:
            self._fh.write(json.dumps(record, default=str) + "\n")
            self._fh.flush()
        except Exception:
            pass

    def _rel(self, path: str) -> tuple[str, bool]:
        real = os.path.realpath(path)
        in_repo = (
            real.startswith(self.root + os.sep) and "site-packages" not in real and "/.venv/" not in real
        )
        return (os.path.relpath(real, self.root).replace(os.sep, "/") if in_repo else real), in_repo

    def _frames(self, excinfo: Any) -> list[dict[str, Any]]:
        frames = []
        try:
            for entry in excinfo.traceback:
                path, in_repo = self._rel(str(entry.path))
                try:
                    source = str(entry.statement).strip()
                except Exception:
                    source = ""
                frames.append(
                    {
                        "path": path,
                        "line": int(entry.lineno) + 1,
                        "function": entry.name,
                        "source": source[:300],
                        "in_repo": in_repo,
                    }
                )
        except Exception:
            pass
        # Drop pytest's own frames before the test and this plugin's frame (timeout handler).
        first = next((i for i, f in enumerate(frames) if f["in_repo"]), 0)
        own = os.path.realpath(__file__)
        frames = [f for f in frames[first:] if os.path.realpath(f["path"]) != own]
        return frames[-40:]

    # -------------------------------------------------------------- collection
    def pytest_collection_modifyitems(self, session: Any, config: Any, items: list[Any]) -> None:
        if self.seed is not None:
            random.Random(self.seed).shuffle(items)

    def pytest_collectreport(self, report: Any) -> None:
        if report.failed:
            text = _clip(report.longrepr, MAX_LONGREPR)
            matches = re.findall(
                r"^E\s+([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning))\b:?\s*(.*)$", text, re.M
            )
            exc_type, message = (
                matches[-1] if matches else ("CollectionError", text.strip().splitlines()[-1:] or [""])
            )
            if isinstance(message, list):
                message = message[0] if message else ""
            self._collection_errors.append(
                {
                    "nodeid": report.nodeid,
                    "exc_type": exc_type.rsplit(".", 1)[-1],
                    "message": str(message)[:500],
                    "longrepr": text,
                }
            )

    def pytest_collection_finish(self, session: Any) -> None:
        self._write(
            {
                "type": "collection",
                "items": [item.nodeid for item in session.items],
                "errors": self._collection_errors,
            }
        )

    # -------------------------------------------------------------- running
    def pytest_runtest_setup(self, item: Any) -> None:
        if self.coverage is not None:
            self.coverage.start()

    def pytest_runtest_logfinish(self, nodeid: str, location: Any) -> None:
        if self.coverage is not None:
            lines = self.coverage.stop()
            self._write({"type": "coverage", "nodeid": nodeid, "lines": lines})

    def pytest_runtest_logreport(self, report: Any) -> None:
        record: dict[str, Any] = {
            "type": "test",
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "duration": round(float(getattr(report, "duration", 0.0)), 4),
            "wasxfail": bool(hasattr(report, "wasxfail")),
        }
        extra = getattr(report, "_fixloop", None)
        if extra:
            record.update(extra)
        if report.failed or hasattr(report, "wasxfail"):
            record["longrepr"] = _clip(report.longrepr, MAX_LONGREPR)
            crash = getattr(getattr(report, "longrepr", None), "reprcrash", None)
            if crash is not None:
                record["assertion"] = _clip(getattr(crash, "message", ""), 1500)
        if report.when == "call" or report.failed:
            record["stdout"] = _clip(getattr(report, "capstdout", ""))
            record["stderr"] = _clip(getattr(report, "capstderr", ""))
        self._write(record)

    def pytest_sessionfinish(self, session: Any, exitstatus: Any) -> None:
        self._write(
            {
                "type": "summary",
                "exitstatus": int(exitstatus),
                "python": sys.version.split()[0],
                "duration": round(time.monotonic() - self._started, 3),
            }
        )
        try:
            self._fh.close()
        except Exception:
            pass


def _attach(report: Any, info: dict[str, Any]) -> None:
    try:
        report._fixloop = info
    except Exception:
        pass


def _reporter(config: Any) -> _Reporter | None:
    plugin = config.pluginmanager.get_plugin("fixloop-reporter")
    return plugin if isinstance(plugin, _Reporter) else None


try:  # pytest >= 8 exposes `hookimpl` here; older versions too
    import pytest

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(item: Any, call: Any) -> Any:
        outcome = yield
        reporter = _reporter(item.config)
        if reporter is None:
            return
        try:
            report = outcome.get_result()
            if call.excinfo is not None and report.failed:
                value = call.excinfo.value
                _attach(
                    report,
                    {
                        "exc_type": type(value).__name__,
                        "message": _clip(str(value), 1500),
                        "frames": reporter._frames(call.excinfo),
                    },
                )
        except Exception:
            pass

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(item: Any) -> Any:
        reporter = _reporter(item.config)
        alarm = bool(reporter and reporter.timeout > 0 and hasattr(signal, "SIGALRM") and _is_main_thread())
        previous = None
        if alarm:
            limit = reporter.timeout  # type: ignore[union-attr]

            def on_alarm(signum: int, frame: Any) -> None:
                raise FixloopTestTimeout(f"test exceeded the per-test limit of {limit:g}s")

            previous = signal.signal(signal.SIGALRM, on_alarm)
            signal.setitimer(signal.ITIMER_REAL, limit)
            try:
                import faulthandler

                faulthandler.dump_traceback_later(limit + 5, exit=False)
            except Exception:
                pass
        deep = reporter is not None and reporter.deep_target and item.nodeid == reporter.deep_target
        tracer = _DeepTracer(reporter.root) if deep and reporter else None
        if tracer:
            tracer.start()
        try:
            yield
        finally:
            if tracer and reporter:
                tracer.stop()
                tracer.write(os.path.join(reporter.out, "deeptrace.json"), item.nodeid)
            if alarm:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous)
                try:
                    import faulthandler

                    faulthandler.cancel_dump_traceback_later()
                except Exception:
                    pass
except ImportError:  # pragma: no cover - pytest is always present when the plugin loads
    pass


def _is_main_thread() -> bool:
    import threading

    return threading.current_thread() is threading.main_thread()


# ------------------------------------------------------------------------------------ changed-line coverage
class _LineCollector:
    """Records which lines of the changed files run during each test.

    Python 3.12+ uses `sys.monitoring` (cheap: untracked code locations are disabled after their
    first event); older versions fall back to `sys.settrace` limited to the tracked files.
    """

    def __init__(self, root: str, changed: list[str]) -> None:
        self.tracked = {os.path.realpath(os.path.join(root, p)): p for p in changed}
        self.hits: dict[str, set[int]] = {}
        self.tool: int | None = None
        self.monitoring = hasattr(sys, "monitoring")
        if self.monitoring:
            mon = sys.monitoring  # type: ignore[attr-defined]
            for tool_id in (4, 3, 2, 1):
                try:
                    mon.use_tool_id(tool_id, "fixloop")
                    self.tool = tool_id
                    break
                except ValueError:
                    continue
            if self.tool is None:
                self.monitoring = False
            else:
                mon.register_callback(self.tool, mon.events.LINE, self._on_line)

    def _on_line(self, code: Any, line: int) -> Any:
        rel = self.tracked.get(os.path.realpath(code.co_filename)) if code.co_filename else None
        if rel is None:
            return sys.monitoring.DISABLE  # type: ignore[attr-defined]
        self.hits.setdefault(rel, set()).add(line)
        return sys.monitoring.DISABLE  # type: ignore[attr-defined]  # recorded once per test is enough

    def _trace(self, frame: Any, event: str, arg: Any) -> Any:
        rel = self.tracked.get(os.path.realpath(frame.f_code.co_filename))
        if rel is None:
            return None

        def local(frame: Any, event: str, arg: Any) -> Any:
            if event == "line":
                self.hits.setdefault(rel, set()).add(frame.f_lineno)
            return local

        return local

    def start(self) -> None:
        self.hits = {}
        try:
            if self.monitoring and self.tool is not None:
                mon = sys.monitoring  # type: ignore[attr-defined]
                mon.restart_events()
                mon.set_events(self.tool, mon.events.LINE)
            else:
                sys.settrace(self._trace)
        except Exception:
            pass

    def stop(self) -> dict[str, list[int]]:
        try:
            if self.monitoring and self.tool is not None:
                sys.monitoring.set_events(self.tool, 0)  # type: ignore[attr-defined]
            else:
                sys.settrace(None)
        except Exception:
            pass
        return {path: sorted(lines) for path, lines in self.hits.items()}


# ------------------------------------------------------------------------------------ deep trace
class _DeepTracer:
    """Call path and local variables through repository code for one failing test (design section 12)."""

    MAX_EVENTS = 2000

    def __init__(self, root: str) -> None:
        self.root = root
        self.events: list[dict[str, Any]] = []
        self.depth = 0
        self._previous: Any = None

    def _in_repo(self, filename: str) -> str | None:
        real = os.path.realpath(filename)
        if real.startswith(self.root + os.sep) and "site-packages" not in real and "/.venv/" not in real:
            return os.path.relpath(real, self.root).replace(os.sep, "/")
        return None

    @staticmethod
    def _locals(frame: Any) -> dict[str, str]:
        out = {}
        for name, value in list(frame.f_locals.items())[:20]:
            if SECRETISH.search(name):
                out[name] = "<redacted>"
                continue
            try:
                text = repr(value)
            except Exception:
                text = f"<unrepresentable {type(value).__name__}>"
            out[name] = text[:200]
        return out

    def _trace(self, frame: Any, event: str, arg: Any) -> Any:
        if len(self.events) >= self.MAX_EVENTS:
            return None
        rel = self._in_repo(frame.f_code.co_filename)
        if rel is None:
            return None
        if event == "call":
            self.depth += 1
            self.events.append(
                {
                    "event": "call",
                    "path": rel,
                    "line": frame.f_lineno,
                    "function": frame.f_code.co_name,
                    "depth": self.depth,
                }
            )

            def local(frame: Any, event: str, arg: Any) -> Any:
                if event == "return":
                    try:
                        value = repr(arg)[:200]
                    except Exception:
                        value = "<unrepresentable>"
                    self.events.append(
                        {
                            "event": "return",
                            "path": rel,
                            "line": frame.f_lineno,
                            "function": frame.f_code.co_name,
                            "depth": self.depth,
                            "locals": self._locals(frame),
                            "value": value,
                        }
                    )
                    self.depth -= 1
                elif event == "exception":
                    self.events.append(
                        {
                            "event": "exception",
                            "path": rel,
                            "line": frame.f_lineno,
                            "function": frame.f_code.co_name,
                            "depth": self.depth,
                            "exception": f"{arg[0].__name__}: {str(arg[1])[:200]}",
                            "locals": self._locals(frame),
                        }
                    )
                return local

            return local
        return None

    def start(self) -> None:
        self._previous = sys.gettrace()
        sys.settrace(self._trace)

    def stop(self) -> None:
        sys.settrace(self._previous)

    def write(self, path: str, nodeid: str) -> None:
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "nodeid": nodeid,
                        "events": self.events,
                        "truncated": len(self.events) >= self.MAX_EVENTS,
                    },
                    handle,
                    default=str,
                )
        except Exception:
            traceback.print_exc()
