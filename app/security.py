"""Безопасное хранение токенов: шифрование Fernet (AES-128-CBC + HMAC-SHA256).

Ключ шифрования живёт только в переменной окружения сервера (TOKEN_ENCRYPTION_KEY),
в БД лежит лишь шифротекст. Токены не выводятся в интерфейс и в логи.
Пароль от Instagram приложение не запрашивает и не хранит никогда — авторизация
только через официальный OAuth-диалог Meta.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


class TokenStoreError(RuntimeError):
    pass


def generate_key() -> str:
    return Fernet.generate_key().decode()


def _fernet(key: str | None = None) -> Fernet:
    key = key or settings.token_encryption_key
    if not key:
        raise TokenStoreError("TOKEN_ENCRYPTION_KEY не задан. Сгенерируйте: python -m app.cli gen-key")
    return Fernet(key.encode())


def save_token(conn: sqlite3.Connection, provider: str, subject: str, token: str,
               scopes: str | None = None, expires_at: datetime | None = None, key: str | None = None) -> None:
    enc = _fernet(key).encrypt(token.encode())
    conn.execute(
        """INSERT INTO oauth_tokens(provider, subject, token_encrypted, scopes, expires_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(provider, subject) DO UPDATE SET token_encrypted=excluded.token_encrypted,
             scopes=excluded.scopes, expires_at=excluded.expires_at, created_at=datetime('now')""",
        (provider, subject, enc, scopes, expires_at.isoformat() if expires_at else None),
    )
    conn.commit()


def load_token(conn: sqlite3.Connection, provider: str, subject: str | None = None, key: str | None = None) -> str | None:
    if subject:
        r = conn.execute("SELECT token_encrypted FROM oauth_tokens WHERE provider=? AND subject=?", (provider, subject)).fetchone()
    else:
        r = conn.execute("SELECT token_encrypted FROM oauth_tokens WHERE provider=? ORDER BY id DESC LIMIT 1", (provider,)).fetchone()
    if not r:
        return None
    try:
        return _fernet(key).decrypt(r["token_encrypted"]).decode()
    except InvalidToken as exc:
        raise TokenStoreError("Не удалось расшифровать токен: ключ изменился?") from exc


def token_status(conn: sqlite3.Connection) -> list[dict]:
    """Метаданные токенов без самих токенов — для отображения в интерфейсе."""
    return [dict(r) for r in conn.execute(
        "SELECT provider, subject, scopes, expires_at, created_at FROM oauth_tokens ORDER BY provider"
    ).fetchall()]


def delete_tokens(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM oauth_tokens")
    conn.commit()
