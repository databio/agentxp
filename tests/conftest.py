from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "notes" / "tests" / "evals"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def lines():
    return lambda name: (FIXTURES / name).read_text().splitlines()
