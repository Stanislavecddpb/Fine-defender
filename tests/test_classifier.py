import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from fine_defender.classifier import Classifier
from fine_defender.config import AppConfig
from fine_defender.ingestion.worker import IngestionWorker
from fine_defender.repository import InMemoryRepository, Seller
from fine_defender.wb.client import MockReportClient


class _NoopCipher:
    def decrypt(self, blob: bytes) -> str:
        return "mock-token"


def _config() -> AppConfig:
    return AppConfig.load(Path("config/config.yaml"))


def _ingest(sample_rows):
    seller = Seller(id=uuid.uuid4(), name="MOCK", wb_token_enc=b"", token_scopes=["read"])
    repo = InMemoryRepository(sellers=[seller])
    IngestionWorker(repo, MockReportClient(sample_rows), config=_config(), cipher=_NoopCipher()).run_all()
    return repo, seller


def test_classifier_splits_categories(sample_rows):
    repo, seller = _ingest(sample_rows)
    [res] = Classifier(repo, config=_config()).classify_all()
    # 4 сохранённых сырых строки: 3 штрафа (penalty>0), 1 продажа (не штраф).
    assert res.skipped_not_fine == 1
    assert res.fines_new == 3
    assert res.by_category == {"oversize_logistics": 2, "other": 1}


def test_oversize_fines_get_deadline_and_estimate(sample_rows):
    repo, seller = _ingest(sample_rows)
    cfg = _config()
    Classifier(repo, config=cfg).classify_all()
    oversize = repo.list_fines(seller.id, category="oversize_logistics")
    assert len(oversize) == 2
    for f in oversize:
        assert f.status == "disputable"
        assert f.dispute_deadline == f.charged_at + timedelta(days=cfg.dispute.window_days)
        assert f.recoverable_est == f.amount  # ratio=1.0


def test_other_fine_has_no_deadline(sample_rows):
    repo, seller = _ingest(sample_rows)
    Classifier(repo, config=_config()).classify_all()
    other = repo.list_fines(seller.id, category="other")
    assert len(other) == 1
    assert other[0].status == "new"
    assert other[0].dispute_deadline is None
    assert other[0].reason_raw == "Штраф за подмену товара"


def test_classification_is_idempotent(sample_rows):
    repo, seller = _ingest(sample_rows)
    clf = Classifier(repo, config=_config())
    clf.classify_all()
    [second] = clf.classify_all()  # уже всё классифицировано
    assert second.fines_new == 0
    assert len(repo.fines) == 3


def test_string_money_summed_correctly(sample_rows):
    repo, seller = _ingest(sample_rows)
    Classifier(repo, config=_config()).classify_all()
    oversize = repo.list_fines(seller.id, category="oversize_logistics")
    amounts = sorted(f.amount for f in oversize)
    assert amounts == [Decimal("350.50"), Decimal("1200.00")]
