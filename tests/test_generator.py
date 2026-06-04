import uuid
from pathlib import Path

import pytest

from fine_defender.classifier import Classifier
from fine_defender.config import AppConfig
from fine_defender.dispute.generator import DisputeGenerator, DraftError
from fine_defender.ingestion.worker import IngestionWorker
from fine_defender.repository import InMemoryRepository, Seller
from fine_defender.wb.client import MockReportClient


class _NoopCipher:
    def decrypt(self, blob: bytes) -> str:
        return "mock-token"


def _config() -> AppConfig:
    return AppConfig.load(Path("config/config.yaml"))


def _prepare(sample_rows):
    seller = Seller(id=uuid.uuid4(), name="ООО Ромашка", wb_token_enc=b"", token_scopes=["read"])
    repo = InMemoryRepository(sellers=[seller])
    cfg = _config()
    IngestionWorker(repo, MockReportClient(sample_rows), config=cfg, cipher=_NoopCipher()).run_all()
    Classifier(repo, config=cfg).classify_all()
    return repo, seller, cfg


def test_generate_draft_for_oversize(sample_rows):
    repo, seller, cfg = _prepare(sample_rows)
    fine = repo.list_fines(seller.id, category="oversize_logistics")[0]
    draft = DisputeGenerator(repo, config=cfg).generate_for_fine(fine.id, seller.name)

    assert "Претензия" in draft.body_md
    assert "ООО Ромашка" in draft.body_md
    assert f"{fine.amount:.2f}" in draft.body_md
    assert fine.reason_raw in draft.body_md
    assert len(draft.evidence_checklist) == 5
    assert "измерительной лентой" in draft.body_md
    # генерация перевела штраф в drafting
    assert repo.get_fine(fine.id).status == "drafting"
    assert repo.get_dispute_draft(fine.id) is not None


def test_generate_rejects_other_category(sample_rows):
    repo, seller, cfg = _prepare(sample_rows)
    other = repo.list_fines(seller.id, category="other")[0]
    with pytest.raises(DraftError):
        DisputeGenerator(repo, config=cfg).generate_for_fine(other.id, seller.name)


def test_generate_unknown_fine_raises(sample_rows):
    repo, seller, cfg = _prepare(sample_rows)
    with pytest.raises(DraftError):
        DisputeGenerator(repo, config=cfg).generate_for_fine(uuid.uuid4(), seller.name)
