"""Утилиты командной строки: python -m app.cli <команда>."""
from __future__ import annotations

import sys

from .security import generate_key


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "gen-key":
        print(generate_key())
    elif cmd == "rotate-key":
        from .db import connect, init_db
        from .security import rotate_encryption
        c = connect()
        init_db(c)
        print(f"Перешифровано токенов: {rotate_encryption(c)}")
    elif cmd == "dev-cert":
        # Самоподписанный сертификат для https://localhost (локальный Redirect URI)
        import subprocess
        from .config import BASE_DIR
        d = BASE_DIR / "data" / "tls"
        d.mkdir(parents=True, exist_ok=True)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "365",
                        "-keyout", str(d / "localhost.key"), "-out", str(d / "localhost.crt"),
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True)
        print(f"Сертификат: {d}/localhost.crt, ключ: {d}/localhost.key")
    elif cmd == "init-db":
        from .db import connect, init_db
        init_db(connect())
        print("ok")
    else:
        print("Команды: gen-key, rotate-key, dev-cert, init-db")


if __name__ == "__main__":
    main()
