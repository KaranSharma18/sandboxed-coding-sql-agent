from datetime import date

from dates import parse_date


def test_end_of_month_both_formats():
    assert parse_date("2023-12-31") == date(2023, 12, 31)
    assert parse_date("31/12/2023") == date(2023, 12, 31)


def test_single_digit_parts():
    assert parse_date("2024-1-5") == date(2024, 1, 5)
