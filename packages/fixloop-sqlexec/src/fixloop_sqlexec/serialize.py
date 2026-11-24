"""JSON-safe serialization of database values (exactness first: decimals travel as strings)."""

from __future__ import annotations

import datetime as dt
import math
import uuid
from decimal import Decimal
from typing import Any


def to_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value.total_seconds())
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_json(v) for k, v in value.items()}
    return str(value)
