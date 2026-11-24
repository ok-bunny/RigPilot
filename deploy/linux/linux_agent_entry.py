"""PyInstaller entry point for the standalone Linux Agent bundle."""

from agent.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())
