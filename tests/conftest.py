import os

# Before any Qt import: headless platform plugin for the offscreen view smoke tests.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest_plugins = ["wake_support"]
