"""Точка входа Sharp: запуск TUI."""
from __future__ import annotations

import sys


def main() -> int:
    from .config import CFG

    if not CFG.api_key:
        print("Не задан ключ Gemini. Укажи api_key в ~/.config/sharp/config.toml "
              "или экспортируй GEMINI_API_KEY.", file=sys.stderr)
        return 1

    from .tui.app import run
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
