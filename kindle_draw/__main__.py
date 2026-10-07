"""Allow ``python -m kindle_draw``.

With no arguments, opens the GUI launcher. Any argument (except
``--launcher``) routes straight to the CLI, so scripted use is unchanged.
"""

import sys

from .app import main as cli_main


def main() -> int:
    args = sys.argv[1:]

    # Explicit CLI invocation: any real argument goes to the CLI.
    if args and args != ["--launcher"]:
        return cli_main()

    # Bare invocation (or explicit --launcher): try the GUI.
    try:
        from .launcher import run_launcher
    except ImportError as exc:
        print(f"GUI launcher unavailable ({exc}); falling back to CLI.",
              file=sys.stderr)
        return cli_main()

    return run_launcher()


if __name__ == "__main__":
    raise SystemExit(main())