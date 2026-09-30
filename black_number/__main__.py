"""Entry point: `python -m black_number` or the `bn` / `blacknumber` command.

    bn                          talk to it in the terminal
    bn "what's the weather"     one request, then exit
    bn serve [--port 3002]      the local web console, in a browser
    bn --help
"""
from __future__ import annotations

import sys

USAGE = """Black Number — a voice-activated assistant for your Mac.

  bn                            interactive, in this terminal
  bn "what's the weather"       run one request and exit
  bn serve [--port 3002]        local web console at http://127.0.0.1:3002
  bn --version

Configuration lives in .env — copy .env.example. Nothing in it is required.
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    if argv and argv[0] in ("-V", "--version", "version"):
        from . import __version__

        print(f"black-number {__version__}")
        return 0

    if argv and argv[0] == "serve":
        from .ui.web import DEFAULT_PORT, serve

        port, open_browser = DEFAULT_PORT, True
        rest = argv[1:]
        i = 0
        while i < len(rest):
            token = rest[i]
            if token in ("-p", "--port"):
                if i + 1 >= len(rest):
                    print("--port needs a number", file=sys.stderr)
                    return 2
                try:
                    port = int(rest[i + 1])
                except ValueError:
                    print(f"'{rest[i + 1]}' is not a port number", file=sys.stderr)
                    return 2
                i += 2
                continue
            if token in ("-n", "--no-browser"):
                open_browser = False
                i += 1
                continue
            print(f"Unknown option for serve: {token}", file=sys.stderr)
            return 2
        if not 1 <= port <= 65535:
            print("Ports run from 1 to 65535", file=sys.stderr)
            return 2
        return serve(port=port, open_browser=open_browser)

    from .ui.cli import run

    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
