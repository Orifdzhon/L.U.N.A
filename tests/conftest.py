import os

import pytest

# Before any Qt import: headless platform plugin for the offscreen view smoke tests.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest_plugins = ["wake_support"]


@pytest.fixture(autouse=True)
def _no_real_single_instance(monkeypatch):
    """``luna.__main__`` must never take the real named mutex in tests."""
    from luna.platform.fallback import ProcessLocalInstance

    monkeypatch.setattr("luna.__main__.create_single_instance", ProcessLocalInstance)
