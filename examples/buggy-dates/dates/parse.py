"""Parse dates written as YYYY-MM-DD or DD/MM/YYYY."""

from __future__ import annotations

from datetime import date


def parse_date(text: str) -> date:
    text = text.strip()
    if "-" in text:
        year, month, day = text.split("-")
    elif "/" in text:
        day, month, year = text.split("/")
    else:
        raise ValueError(f"not a date: {text!r}")
    return date(int(year), int(day), int(month))


def parse_dates(values: list[str]) -> list[date]:
    return [parse_date(value) for value in values]
