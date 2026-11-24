"""Building argument lists from the fixed command templates.

The model never supplies any part of a command. The worker names a template and typed arguments;
this module validates every argument against `config/command_templates.yaml` and returns an
argument list. There is no shell anywhere, and a node id can never become a flag.
"""

from __future__ import annotations

import re
from typing import Any

from fixloop_core.config import CommandTemplate, TemplateParam
from fixloop_core.spec import NODE_ID_RE, validate_repo_path


class TemplateError(ValueError):
    pass


def _check_nodeid(value: Any) -> str:
    text = str(value)
    if (
        not text
        or text.startswith("-")
        or (not NODE_ID_RE.match(text) and not re.fullmatch(r"(?!-)[\w./-]+\.py", text))
    ):
        raise TemplateError(f"invalid node id: {text!r}")
    return text


def _param_values(name: str, param: TemplateParam, value: Any) -> list[str]:
    if param.type == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise TemplateError(f"{name} must be an integer")
        if (param.min is not None and value < param.min) or (param.max is not None and value > param.max):
            raise TemplateError(f"{name} out of range")
        return [str(value)]
    if param.type == "nodeid":
        return [_check_nodeid(value)]
    if param.type == "nodeid_list":
        if not isinstance(value, list):
            raise TemplateError(f"{name} must be a list")
        if param.max_items is not None and len(value) > param.max_items:
            raise TemplateError(f"{name} has too many items")
        return [_check_nodeid(v) for v in value]
    if param.type == "path_list":
        if not isinstance(value, list):
            raise TemplateError(f"{name} must be a list")
        paths = []
        for item in value:
            try:
                paths.append(validate_repo_path(str(item)))
            except ValueError as exc:
                raise TemplateError(str(exc)) from exc
            if "," in str(item):
                raise TemplateError("paths may not contain commas")
        return [",".join(paths)] if paths else []
    if param.type == "str":
        text = str(value)
        if not re.fullmatch(r"[\w.:/=-]{1,200}", text):
            raise TemplateError(f"{name} contains unsupported characters")
        return [text]
    raise TemplateError(f"unknown parameter type for {name}")


def build_argv(template: CommandTemplate, args: dict[str, Any], placeholders: dict[str, str]) -> list[str]:
    unknown = set(args) - set(template.params)
    if unknown:
        raise TemplateError(f"unknown arguments: {', '.join(sorted(unknown))}")
    argv = [part.format_map(_Strict(placeholders)) for part in template.argv]
    positional: list[str] = []
    for name, param in template.params.items():
        value = args.get(name, param.default)
        if value is None or value == [] or value == "":
            continue
        values = _param_values(name, param, value)
        if param.positional:
            positional.extend(values)
        elif param.flag and values:
            argv.append(param.flag.format(value=values[0]))
    if positional:
        argv.append("--")  # everything after this is a node id, never an option
        argv.extend(positional)
    return argv


class _Strict(dict):  # type: ignore[type-arg]
    def __missing__(self, key: str) -> str:
        raise TemplateError(f"missing placeholder {key}")
