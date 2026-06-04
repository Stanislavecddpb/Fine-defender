"""Дымовой тест API без поднятия порта (FastAPI TestClient).

Проходит весь дашборд на memory-бэкенде: селлеры -> штрафы -> сводка ->
генерация черновика -> действие оператора.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient

from fine_defender.api.main import app

client = TestClient(app)


def show(method: str, path: str, resp) -> None:
    print(f"{method} {path} -> {resp.status_code}")
    print("  " + json.dumps(resp.json(), ensure_ascii=False)[:400])


print("=== /health ===")
show("GET", "/health", client.get("/health"))

print("\n=== Селлеры ===")
r = client.get("/api/sellers")
show("GET", "/api/sellers", r)
sid = r.json()[0]["id"]

print("\n=== Штрафы селлера ===")
r = client.get("/api/fines", params={"seller_id": sid})
for f in r.json():
    print(f"  [{f['status']:10}] {f['category']:18} {f['amount']} ₽  "
          f"дедлайн {f['dispute_deadline']} (через {f['days_to_deadline']} дн, soon={f['deadline_soon']})")

print("\n=== Сводка ===")
show("GET", "/api/summary", client.get("/api/summary", params={"seller_id": sid}))

oversize = [f for f in client.get("/api/fines", params={"seller_id": sid, "category": "oversize_logistics"}).json()]
fine_id = oversize[0]["id"]

print("\n=== Генерация черновика претензии ===")
show("POST", f"/api/fines/{fine_id}/draft", client.post(f"/api/fines/{fine_id}/draft"))

print("\n=== Действие оператора: спор выигран, возврат 350.50 ===")
show("POST", f"/api/fines/{fine_id}/events",
     client.post(f"/api/fines/{fine_id}/events",
                 json={"status": "won", "recovered_amount": "350.50", "note": "WB одобрил"}))

print("\n=== Сводка после возврата ===")
show("GET", "/api/summary", client.get("/api/summary", params={"seller_id": sid}))
