import uuid

from fine_defender.config import AppConfig
from fine_defender.ingestion.worker import IngestionWorker
from fine_defender.repository import InMemoryRepository, Seller
from fine_defender.wb.client import MockReportClient


class _NoopCipher:
    def decrypt(self, blob: bytes) -> str:
        return "mock-token"


def _app_config() -> AppConfig:
    return AppConfig.load(__import__("pathlib").Path("config/config.yaml"))


def _make_worker(rows):
    seller = Seller(id=uuid.uuid4(), name="MOCK", wb_token_enc=b"", token_scopes=["read"])
    repo = InMemoryRepository(sellers=[seller])
    worker = IngestionWorker(
        repo, MockReportClient(rows), config=_app_config(), cipher=_NoopCipher()
    )
    return repo, worker, seller


def test_ingest_counts_new_rows_and_skips_keyless(sample_rows):
    repo, worker, _ = _make_worker(sample_rows)
    [res] = worker.run_all()
    # 5 строк в фикстуре, одна без ключа транзакции.
    assert res.rows_seen == 5
    assert res.rows_skipped_no_key == 1
    assert res.rows_new == 4
    assert res.status == "partial"  # были пропущенные строки


def test_ingestion_is_idempotent(sample_rows):
    repo, worker, _ = _make_worker(sample_rows)
    worker.run_all()
    [second] = worker.run_all()
    # Повторная выгрузка не создаёт новых записей в сыром слое.
    assert second.rows_new == 0
    assert len(repo.raw) == 4


def test_run_journal_written(sample_rows):
    repo, worker, _ = _make_worker(sample_rows)
    worker.run_all()
    assert len(repo.runs) == 1
    run = repo.runs[0]
    assert run["status"] == "partial"
    assert run["rows_ingested"] == 4
    assert run["finished_at"] is not None


def test_failed_fetch_marks_run_failed():
    from fine_defender.wb.client import WbApiError

    class _BoomClient:
        def fetch_rows(self, token, date_from):
            raise WbApiError("status=503")

    seller = Seller(id=uuid.uuid4(), name="MOCK", wb_token_enc=b"", token_scopes=["read"])
    repo = InMemoryRepository(sellers=[seller])
    worker = IngestionWorker(repo, _BoomClient(), config=_app_config(), cipher=_NoopCipher())
    [res] = worker.run_all()
    assert res.status == "failed"
    assert repo.runs[0]["status"] == "failed"
    assert "503" in repo.runs[0]["error_detail"]
