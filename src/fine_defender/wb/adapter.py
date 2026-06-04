"""Version-tolerant адаптер ответа reportDetailByPeriod.

Весь парсинг сырого JSON WB изолирован здесь (раздел 4 ТЗ): смена версии метода
или переименование полей не должны ломать остальной код. Поэтому каждое
логическое поле ищется по СПИСКУ кандидатов-имён, а денежные значения парсятся
из строки ИЛИ числа (новая версия отдаёт деньги строками).

[СВЕРИТЬ] точные имена полей по dev.wildberries.ru перед боевым подключением
(раздел 13 ТЗ). Списки кандидатов ниже — отправная точка, расширяются без правки
логики.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

# Кандидаты имён полей (snake_case из текущего отчёта + camelCase из новой версии).
_KEY_CANDIDATES = ("rrd_id", "rrdId", "srid")
_DATE_CANDIDATES = ("rr_dt", "rrDt", "rr_date")
_PENALTY_CANDIDATES = ("penalty", "penaltyAmount")
_OP_TYPE_CANDIDATES = (
    "supplier_oper_name",
    "supplierOperName",
    "bonus_type_name",
    "bonusTypeName",
    "doc_type_name",
)
_REASON_CANDIDATES = ("bonus_type_name", "bonusTypeName", "penalty_reason", "reason", "title")
_SHK_CANDIDATES = ("shk_id", "shkId")
_FORPAY_CANDIDATES = ("ppvz_for_pay", "ppvzForPay")
_TITLE_CANDIDATES = ("title",)


@dataclass(frozen=True)
class NormalizedRow:
    """Нормализованная строка отчёта — то, на чём работает классификатор (M2)."""

    txn_key: str
    charged_at: date | None
    penalty: Decimal
    operation_type: str
    reason_raw: str
    shk_id: str | None
    for_pay: Decimal | None
    title: str | None
    raw: dict[str, Any] = field(repr=False)


def _first(row: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    for key in candidates:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def parse_money(value: Any) -> Decimal:
    """Деньги приходят строкой ИЛИ числом. Пустое/невалидное -> 0.

    Терпим к запятой-разделителю и пробелам-разрядам ('1 234,56').
    """
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = value.replace("\xa0", "").replace(" ", "").replace(",", ".")
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return Decimal("0")
    return Decimal("0")


def _parse_money_optional(value: Any) -> Decimal | None:
    return None if value in (None, "") else parse_money(value)


def parse_date(value: Any) -> date | None:
    """rr_dt приходит как ISO-строка ('2026-05-01' или '2026-05-01T00:00:00')."""
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    # Берём дату до 'T'/пробела, остальное игнорируем.
    head = text.split("T")[0].split(" ")[0]
    try:
        return date.fromisoformat(head)
    except ValueError:
        return None


def normalize_row(row: dict[str, Any]) -> NormalizedRow | None:
    """Преобразовать сырую строку отчёта в NormalizedRow.

    Возвращает None, если нет ключа транзакции (без него строку нельзя
    дедуплицировать и сохранить идемпотентно).
    """
    raw_key = _first(row, _KEY_CANDIDATES)
    if raw_key is None:
        return None

    return NormalizedRow(
        txn_key=str(raw_key),
        charged_at=parse_date(_first(row, _DATE_CANDIDATES)),
        penalty=parse_money(_first(row, _PENALTY_CANDIDATES)),
        operation_type=str(_first(row, _OP_TYPE_CANDIDATES) or ""),
        reason_raw=str(_first(row, _REASON_CANDIDATES) or ""),
        shk_id=(str(v) if (v := _first(row, _SHK_CANDIDATES)) is not None else None),
        for_pay=_parse_money_optional(_first(row, _FORPAY_CANDIDATES)),
        title=(str(v) if (v := _first(row, _TITLE_CANDIDATES)) is not None else None),
        raw=row,
    )
