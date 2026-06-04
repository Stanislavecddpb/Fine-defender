import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "report_detail_sample.json"


@pytest.fixture
def sample_rows() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))
