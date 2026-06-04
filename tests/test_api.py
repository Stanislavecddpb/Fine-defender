"""Тесты кабинета: авторизация, изоляция тенантов (IDOR), токен, запуск проверки.

Все env выставляются ДО импорта приложения (get_settings кэшируется).
Memory-бэкенд засевает демо-селлера с логином demo@fine-defender.ru / demo12345.
"""

import os
from decimal import Decimal

os.environ["APP_BACKEND"] = "memory"
os.environ["WB_CLIENT_MODE"] = "mock"  # ingest без реального токена
os.environ["TOKEN_ENCRYPTION_KEY"] = "IvnQ9HefdQtOOu400YqK1oYpzNysgCtDX-yWCTyeWh0="
os.environ["SESSION_SECRET"] = "test-session-secret"

from fastapi.testclient import TestClient  # noqa: E402

from fine_defender.api.main import app  # noqa: E402

DEMO = {"email": "demo@fine-defender.ru", "password": "demo12345"}


def fresh():
    return TestClient(app)


def login_demo():
    c = fresh()
    r = c.post("/auth/login", json=DEMO)
    assert r.status_code == 200, r.text
    return c


def register(email, *, token=None, password="password123"):
    c = fresh()
    r = c.post("/auth/register", json={
        "company_name": "Тест", "email": email, "password": password, "wb_token": token,
    })
    return c, r


# --- публичные ---

def test_health():
    assert fresh().get("/health").json()["status"] == "ok"


def test_readiness():
    assert fresh().get("/health/ready").status_code == 200


def test_dashboard_served():
    r = fresh().get("/")
    assert r.status_code == 200 and "Fine Defender" in r.text


# --- авторизация обязательна ---

def test_api_requires_auth():
    assert fresh().get("/api/fines").status_code == 401
    assert fresh().get("/api/summary").status_code == 401
    assert fresh().get("/auth/me").status_code == 401


def test_login_lists_demo_fines():
    c = login_demo()
    fines = c.get("/api/fines").json()
    assert len(fines) == 3  # демо: 2 oversize + 1 other
    assert len([f for f in fines if f["category"] == "oversize_logistics"]) == 2


def test_demo_summary():
    c = login_demo()
    s = c.get("/api/summary").json()
    assert s["fines_total"] == 3
    assert s["disputable_total"] == 2
    assert Decimal(str(s["recoverable_total"])) == Decimal("1550.50")


def test_logout_revokes_access():
    c = login_demo()
    assert c.get("/api/fines").status_code == 200
    c.post("/auth/logout")
    assert c.get("/api/fines").status_code == 401


# --- регистрация и изоляция тенантов ---

def test_register_logs_in():
    c, r = register("newuser@example.com")
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["email"] == "newuser@example.com"
    assert me["has_token"] is False
    # сразу авторизован
    assert c.get("/auth/me").status_code == 200


def test_duplicate_email_rejected():
    register("dup@example.com")
    _, r2 = register("dup@example.com")
    assert r2.status_code == 409


def test_short_password_rejected():
    _, r = register("shortpw@example.com", password="123")
    assert r.status_code == 422


def test_new_tenant_sees_no_foreign_data_and_idor_404():
    # демо-штраф
    demo = login_demo()
    demo_fine_id = demo.get("/api/fines").json()[0]["id"]
    # новый тенант
    tenant, _ = register("isolation@example.com")
    assert tenant.get("/api/fines").json() == []          # чужие штрафы не видны
    assert tenant.get(f"/api/fines/{demo_fine_id}").status_code == 404  # IDOR → 404
    # и действия по чужому штрафу запрещены
    assert tenant.post(f"/api/fines/{demo_fine_id}/draft").status_code == 404


# --- токен и запуск проверки ---

def test_set_token():
    c, _ = register("token@example.com")
    assert c.get("/auth/me").json()["has_token"] is False
    r = c.post("/api/token", json={"wb_token": "SOME-READ-TOKEN"})
    assert r.status_code == 200
    assert r.json()["has_token"] is True


def test_ingest_run_populates_cabinet_and_flow():
    c, _ = register("ingest@example.com")
    assert c.get("/api/fines").json() == []

    run = c.post("/api/ingest/run").json()
    # samples/weekly_report_demo.json: 11 транзакций → 8 штрафов (5 oversize + 3 other)
    assert run["fines_new"] == 8
    assert run["by_category"]["oversize_logistics"] == 5

    fines = c.get("/api/fines").json()
    assert len(fines) == 8
    oversize = [f for f in fines if f["category"] == "oversize_logistics"]

    # генерация черновика по своему штрафу
    fid = oversize[0]["id"]
    draft = c.post(f"/api/fines/{fid}/draft").json()
    assert "Претензия" in draft["body_md"]

    # действие оператора: возврат
    c.post(f"/api/fines/{fid}/events", json={"status": "won", "recovered_amount": "780.00"})
    s = c.get("/api/summary").json()
    assert Decimal(str(s["recovered_total"])) == Decimal("780.00")


def test_idempotent_ingest():
    c, _ = register("idem@example.com")
    c.post("/api/ingest/run")
    second = c.post("/api/ingest/run").json()
    assert second["rows_new"] == 0  # повторная выгрузка не плодит дубли
    assert second["fines_new"] == 0
