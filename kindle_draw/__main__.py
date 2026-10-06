"""Allow ``python -m kindle_draw``."""

from .app import main

if __name__ == "__main__":
    raise SystemExit(main())