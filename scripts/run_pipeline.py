"""Сквозной прогон M1→M2→M4 на моках, с печатью дашборд-вью.

Показывает весь путь без БД и без реального токена:
  выгрузка (фикстура) -> классификация -> сводка -> генерация черновика претензии.

Запуск:
  python scripts/run_pipeline.py
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fine_defender.classifier import Classifier  # noqa: E402
from fine_defender.config import get_app_config  # noqa: E402
from fine_defender.dispute.generator import DisputeGenerator  # noqa: E402
from fine_defender.ingestion.worker import IngestionWorker  # noqa: E402
from fine_defender.logging_utils import configure_logging  # noqa: E402
from fine_defender.repository import InMemoryRepository, Seller  # noqa: E402
from fine_defender.wb.client import MockReportClient  # noqa: E402

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "report_detail_sample.json"


class _NoopCipher:
    def decrypt(self, blob: bytes) -> str:
        return "mock-token"


def main() -> int:
    configure_logging()
    cfg = get_app_config()
    # Путь к отчёту можно передать аргументом; по умолчанию — тестовая фикстура.
    report_path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else FIXTURE
    rows = json.loads(report_path.read_text(encoding="utf-8"))
    print(f"Отчёт: {report_path.name} ({len(rows)} строк)\n")

    seller = Seller(id=uuid.uuid4(), name="ООО Ромашка (демо)", wb_token_enc=b"", token_scopes=["read"])
    repo = InMemoryRepository(sellers=[seller])

    print("=== M1: выгрузка ===")
    for r in IngestionWorker(repo, MockReportClient(rows), config=cfg, cipher=_NoopCipher()).run_all():
        print(f"  {r}")

    print("\n=== M2: классификация ===")
    for r in Classifier(repo, config=cfg).classify_all():
        print(f"  {r}")

    print("\n=== Дашборд: штрафы селлера (по близости дедлайна) ===")
    warn = cfg.dispute.deadline_warning_days
    for f in repo.list_fines(seller.id):
        if f.dispute_deadline is None:
            mark, deadline = "—", "—"
        else:
            days = (f.dispute_deadline - date.today()).days
            deadline = f.dispute_deadline.isoformat()
            if days < 0:
                mark = f"ПРОСРОЧЕН ({days} дн)"
            elif days <= warn:
                mark = f"⚠ ГОРИТ ({days} дн)"
            else:
                mark = f"{days} дн"
        print(
            f"  [{f.status:10}] {f.category:18} {f.amount:>9} ₽  "
            f"начислен {f.charged_at}  дедлайн {deadline:10}  {mark}"
        )

    s = repo.summary(seller.id)
    print("\n=== Сводка ===")
    print(f"  штрафов всего: {s.fines_total}, оспоримых: {s.disputable_total}")
    print(f"  под возврат: {s.recoverable_total} ₽, отбито: {s.recovered_total} ₽")

    print("\n=== M4: черновик претензии (по первому oversize_logistics) ===")
    oversize = repo.list_fines(seller.id, category="oversize_logistics")
    if oversize:
        draft = DisputeGenerator(repo, config=cfg).generate_for_fine(oversize[0].id, seller.name)
        print(draft.body_md)
        print("  Чек-лист доказательств:")
        for i, item in enumerate(draft.evidence_checklist, 1):
            print(f"    {i}. {item}")

    print("\n=== Демо действия оператора: выигран спор, возврат 350.50 ₽ ===")
    from decimal import Decimal

    repo.add_dispute_event(oversize[0].id, status="won", recovered_amount=Decimal("350.50"), note="WB одобрил")
    s2 = repo.summary(seller.id)
    print(f"  отбито теперь: {s2.recovered_total} ₽")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
