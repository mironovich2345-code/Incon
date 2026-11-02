"""Точка запуска для production (Railway) и локально: python -m app.serve

* Порт — из переменной PORT (Railway задаёт её сам), по умолчанию 8000.
* На Railway слушаем 0.0.0.0, локально — 127.0.0.1.
* HTTPS завершается на прокси Railway; приложение получает HTTP и доверяет заголовкам
  X-Forwarded-Proto / X-Forwarded-For (proxy_headers). Список доверенных адресов прокси —
  FORWARDED_ALLOW_IPS (на Railway по умолчанию «*»: контейнер доступен только через прокси).
* Значение PORT читается в Python, поэтому Start Command не зависит от подстановки
  переменных оболочкой.
"""
from __future__ import annotations

import copy
import os

import uvicorn
from uvicorn.config import LOGGING_CONFIG


def build_options(env: dict | None = None) -> dict:
    env = dict(os.environ if env is None else env)
    hosted = bool(env.get("RAILWAY_ENVIRONMENT"))
    port_raw = env.get("PORT", "8000")
    if not port_raw.isdigit() or not (0 < int(port_raw) < 65536):
        raise SystemExit(f"Некорректный PORT: {port_raw!r}")
    log_config = copy.deepcopy(LOGGING_CONFIG)
    log_config.setdefault("filters", {})["scrub"] = {"()": "app.logsafe.ScrubFilter"}
    for h in log_config["handlers"].values():
        h.setdefault("filters", []).append("scrub")
    return {
        "app": "app.main:app",
        "host": env.get("HOST") or ("0.0.0.0" if hosted else "127.0.0.1"),
        "port": int(port_raw),
        "proxy_headers": True,
        "forwarded_allow_ips": env.get("FORWARDED_ALLOW_IPS") or ("*" if hosted else "127.0.0.1"),
        "log_config": log_config,
        "workers": 1,  # SQLite и in-memory защита от перебора пароля рассчитаны на один процесс
        "timeout_keep_alive": 30,
    }


def main() -> None:
    uvicorn.run(**build_options())


if __name__ == "__main__":
    main()
