"""Фильтр логов: вырезает секреты из всех сообщений.

Журнал доступа uvicorn пишет полный путь запроса, включая query string. Для
/meta/callback это ?code=…&state=… — одноразовые, но всё же секретные значения.
Фильтр заменяет значения чувствительных параметров на *** до того, как строка
попадёт в логи Railway.
"""
from __future__ import annotations

import logging
import re

SENSITIVE_PARAMS = ("code", "state", "access_token", "input_token", "fb_exchange_token", "client_secret",
                    "appsecret_proof", "token", "password")
_PARAM_RE = re.compile(r"(?i)\b(" + "|".join(SENSITIVE_PARAMS) + r")=([^&\s\"']+)")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_FB_TOKEN_RE = re.compile(r"\bEA[A-Za-z0-9]{30,}")


def scrub_text(text: str) -> str:
    text = _PARAM_RE.sub(lambda m: f"{m.group(1)}=***", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} ***", text)
    return _FB_TOKEN_RE.sub("EA***", text)


class ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(scrub_text(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: scrub_text(v) if isinstance(v, str) else v for k, v in record.args.items()}
        if isinstance(record.msg, str):
            record.msg = scrub_text(record.msg)
        return True


def install() -> None:
    """Повесить фильтр на логгеры uvicorn и корневой логгер (идемпотентно)."""
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "fastapi"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, ScrubFilter) for f in lg.filters):
            lg.addFilter(ScrubFilter())
        for h in lg.handlers:
            if not any(isinstance(f, ScrubFilter) for f in h.filters):
                h.addFilter(ScrubFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
