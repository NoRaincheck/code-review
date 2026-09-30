"""code-review: background AI code review of every commit, orchestrated over ACP."""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__", "main"]


def main(argv: list[str] | None = None) -> int:
    from .cli import main as _main

    return _main(argv)
