"""Run the local chat UI.

    uv run python -m src.ui
"""

from __future__ import annotations

import argparse
import os


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="LearningFocused local chat UI")
    parser.add_argument(
        "--host",
        default=os.environ.get("UI_HOST", "127.0.0.1"),
        help="Bind host (default 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("UI_PORT", "8765")),
        help="Bind port (default 8765)",
    )
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run("src.ui.app:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
