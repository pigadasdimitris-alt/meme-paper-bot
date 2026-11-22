import pytest

from bot import engine


@pytest.fixture(autouse=True)
def _clean_shared_rugs():
    engine.SHARED_RUGS.clear()
    yield
    engine.SHARED_RUGS.clear()
