"""Генератор черновика претензии (M4).

На вход — данные штрафа (+ сырая транзакция + селлер), на выход — body_md и
evidence_checklist (раздел 7 ТЗ). Шаблон и состав чек-листа — параметры
(templates/*.j2 и config.dispute.evidence_checklist), редактируются без правки кода.

Генерируется ТОЛЬКО черновик: продукт не отправляет претензию. Оператор копирует
и подаёт сам (concierge).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from ..config import AppConfig, get_app_config
from ..repository import DisputeDraft, Fine, Repository

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class DraftError(RuntimeError):
    pass


class DisputeGenerator:
    def __init__(self, repo: Repository, *, config: AppConfig | None = None) -> None:
        self._repo = repo
        self._config = config or get_app_config()
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=select_autoescape(enabled_extensions=()),
            undefined=StrictUndefined,
            trim_blocks=False,
            lstrip_blocks=False,
        )

    def _checklist_for(self, category: str) -> list[str]:
        return list(self._config.dispute.evidence_checklist.get(category, []))

    def render(self, fine: Fine, raw_json: dict[str, Any], seller_name: str | None) -> tuple[str, list[str]]:
        if fine.category != "oversize_logistics":
            raise DraftError(
                f"Шаблон претензии есть только для oversize_logistics, получено: {fine.category}"
            )
        checklist = self._checklist_for(fine.category)
        template = self._env.get_template(f"{fine.category}.md.j2")
        body_md = template.render(
            seller_name=seller_name or "—",
            today=date.today().isoformat(),
            txn_key=raw_json.get("rrd_id") or raw_json.get("rrdId") or raw_json.get("srid") or "—",
            shk_id=raw_json.get("shk_id") or raw_json.get("shkId"),
            nm_id=raw_json.get("nm_id") or raw_json.get("nmId"),
            charged_at=fine.charged_at.isoformat(),
            reason_raw=fine.reason_raw,
            amount=f"{fine.amount:.2f}",
            evidence_checklist=checklist,
        )
        return body_md, checklist

    def generate_for_fine(self, fine_id: uuid.UUID, seller_name: str | None = None) -> DisputeDraft:
        fine = self._repo.get_fine(fine_id)
        if fine is None:
            raise DraftError(f"Штраф не найден: {fine_id}")
        raw = self._repo.get_raw_txn(fine.raw_txn_id)
        raw_json = raw.raw_json if raw else {}
        body_md, checklist = self.render(fine, raw_json, seller_name)
        draft = self._repo.save_dispute_draft(fine_id, body_md, checklist)
        # генерация черновика двигает штраф в статус drafting
        self._repo.add_dispute_event(fine_id, status="drafting", note="Черновик сгенерирован")
        logger.info("Черновик претензии сгенерирован для fine=%s", fine_id)
        return draft
