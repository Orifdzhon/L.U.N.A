import logging

import pytest

from luna.logs.redact import clear_secrets


@pytest.fixture(autouse=True)
def clean_logging_state():
    """No registered secrets leak between tests; root logger handlers/level restored."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    clear_secrets()
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)
    clear_secrets()
