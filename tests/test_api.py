"""Тесты дашборд-API на memory-бэкенде (засев из фикстуры в bootstrap)."""

import os
from decimal import Decimal

# Юнит-тесты дашборда работают на memory-бэкенде (засев из фикстуры),
# независимо от дефолта app_backend. Выставляем ДО импорта приложения.
os.environ["APP_BACKEND"] = "memory"

from fastapi.testclient import TestClient

from fine_defender.api.main import app

client = TestClient(app)


def _seller_id() -> str:
    sellers = client.get("/api/sellers").json()
    assert sellers, "memory-бэкенд должен засеять демо-селлера"
    return sellers[0]["id"]


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_readiness():
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_fines_listed_with_deadline():
    sid = _seller_id()
    fines = client.get("/api/fines", params={"seller_id": sid}).json()
    assert len(fines) == 3  # 2 oversize + 1 other
    oversize = [f for f in fines if f["category"] == "oversize_logistics"]
    assert len(oversize) == 2
    for f in oversize:
        assert f["status"] == "disputable"
        assert f["dispute_deadline"] is not None
        assert f["days_to_deadline"] is not None


def test_filter_by_category():
    sid = _seller_id()
    other = client.get("/api/fines", params={"seller_id": sid, "category": "other"}).json()
    assert len(other) == 1
    assert other[0]["dispute_deadline"] is None


def test_summary_endpoint():
    sid = _seller_id()
    s = client.get("/api/summary", params={"seller_id": sid}).json()
    assert s["fines_total"] == 3
    assert s["disputable_total"] == 2
    # recoverable = сумма oversize (350.50 + 1200.00)
    assert Decimal(str(s["recoverable_total"])) == Decimal("1550.50")
    assert Decimal(str(s["recovered_total"])) == Decimal("0")


def test_generate_draft_and_operator_flow():
    sid = _seller_id()
    oversize = client.get(
        "/api/fines", params={"seller_id": sid, "category": "oversize_logistics"}
    ).json()
    fine_id = oversize[0]["id"]

    # генерация черновика
    draft = client.post(f"/api/fines/{fine_id}/draft").json()
    assert "Претензия" in draft["body_md"]
    assert len(draft["evidence_checklist"]) == 5

    # карточка отражает черновик и статус drafting
    card = client.get(f"/api/fines/{fine_id}").json()
    assert card["draft"] is not None
    assert card["fine"]["status"] == "drafting"
    assert card["raw_json"]  # сырая транзакция приложена

    # действие оператора: спор выигран, возврат
    ev = client.post(
        f"/api/fines/{fine_id}/events",
        json={"status": "won", "recovered_amount": "350.50", "note": "WB одобрил"},
    ).json()
    assert ev["status"] == "won"

    # сводка обновилась: отбито 350.50
    s = client.get("/api/summary", params={"seller_id": sid}).json()
    assert Decimal(str(s["recovered_total"])) == Decimal("350.50")


def test_draft_rejected_for_other_category():
    sid = _seller_id()
    other = client.get("/api/fines", params={"seller_id": sid, "category": "other"}).json()
    resp = client.post(f"/api/fines/{other[0]['id']}/draft")
    assert resp.status_code == 422


def test_unknown_fine_404():
    assert client.get("/api/fines/00000000-0000-0000-0000-000000000000").status_code == 404
