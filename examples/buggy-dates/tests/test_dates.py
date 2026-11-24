from datetime import date

import pytest

from dates import parse_date, parse_dates


def test_parse_dates():
    assert parse_dates(["2024-03-15", "01/02/2023"]) == [date(2024, 3, 15), date(2023, 2, 1)]


def test_strips_whitespace():
    assert parse_date("  2024-01-01 ") == date(2024, 1, 1)


def test_rejects_garbage():
    with pytest.raises(ValueError):
        parse_date("yesterday")
