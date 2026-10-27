"""PyInstaller entry point: the frozen ``luna.exe`` / ``luna-cli.exe`` start here."""

import sys

from luna.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
