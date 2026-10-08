"""Код подтверждения Яндекса: раньше 7 цифр, с октября 2026 — 16 букв и цифр."""
import pytest

from handlers.admin_ext_forms_oauth import normalize_code


@pytest.mark.parametrize("raw, expected", [
    ("jpfsigsxfj3nyrof", "jpfsigsxfj3nyrof"),
    ("  jpfsigsxfj3nyrof\n", "jpfsigsxfj3nyrof"),
    ("AbC123dEf456", "AbC123dEf456"),
    ("1234567", "1234567"),
    ("123 4567", "1234567"),
])
def test_accepts_yandex_codes(raw, expected):
    assert normalize_code(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "код: 1234567", "12345", "abc-def-ghi", "x" * 40])
def test_rejects_non_codes(raw):
    assert normalize_code(raw) is None
