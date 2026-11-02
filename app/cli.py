"""Утилиты командной строки: python -m app.cli <команда>."""
from __future__ import annotations

import sys

from .security import generate_key


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "gen-key":
        print(generate_key())
    elif cmd == "init-db":
        from .db import connect, init_db
        init_db(connect())
        print("ok")
    else:
        print("Команды: gen-key, init-db")


if __name__ == "__main__":
    main()
