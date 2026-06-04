from datetime import date
from decimal import Decimal

from fine_defender.wb.adapter import normalize_row, parse_date, parse_money


class TestParseMoney:
    def test_string_with_comma_and_spaces(self):
        assert parse_money("1 200,00") == Decimal("1200.00")

    def test_string_with_dot(self):
        assert parse_money("350.50") == Decimal("350.50")

    def test_int_and_float(self):
        assert parse_money(500) == Decimal("500")
        assert parse_money(12.5) == Decimal("12.5")

    def test_empty_and_none(self):
        assert parse_money("") == Decimal("0")
        assert parse_money(None) == Decimal("0")

    def test_garbage_is_zero(self):
        assert parse_money("н/д") == Decimal("0")


class TestParseDate:
    def test_iso_date(self):
        assert parse_date("2026-05-04") == date(2026, 5, 4)

    def test_iso_datetime(self):
        assert parse_date("2026-05-05T00:00:00") == date(2026, 5, 5)

    def test_empty(self):
        assert parse_date("") is None
        assert parse_date(None) is None


class TestNormalizeRow:
    def test_string_money_parsed(self, sample_rows):
        row = normalize_row(sample_rows[0])
        assert row is not None
        assert row.txn_key == "100001"
        assert row.penalty == Decimal("350.50")
        assert row.charged_at == date(2026, 5, 4)
        assert "габарит" in row.reason_raw.lower()
        assert row.for_pay == Decimal("1290.00")

    def test_int_money_parsed(self, sample_rows):
        row = normalize_row(sample_rows[3])
        assert row is not None
        assert row.penalty == Decimal("500")

    def test_row_without_txn_key_returns_none(self, sample_rows):
        # Последняя строка фикстуры — без rrd_id/srid.
        assert normalize_row(sample_rows[-1]) is None

    def test_camelcase_fallback(self):
        row = normalize_row({"rrdId": 42, "rrDt": "2026-01-01", "penaltyAmount": "10,00"})
        assert row is not None
        assert row.txn_key == "42"
        assert row.charged_at == date(2026, 1, 1)
        assert row.penalty == Decimal("10.00")
