"""Безопасное хранение токенов: шифрование Fernet (AES-128-CBC + HMAC-SHA256).

Ключ шифрования живёт только в переменной окружения сервера (TOKEN_ENCRYPTION_KEY),
в БД лежит лишь шифротекст. Токены не выводятся в интерфейс и в логи.
Пароль от Instagram приложение не запрашивает и не хранит — авторизация только через
официальный OAuth-диалог Meta.

Ротация ключа шифрования:
  1. сгенерировать новый ключ (python -m app.cli gen-key);
  2. TOKEN_ENCRYPTION_KEY=<новый>, TOKEN_ENCRYPTION_KEYS_OLD=<старый>[,<ещё старее>];
  3. python -m app.cli rotate-key — перешифрует все токены новым ключом;
  4. удалить старый ключ из TOKEN_ENCRYPTION_KEYS_OLD.
Ротация самих токенов Meta — повторный вход: новые токены атомарно заменяют старые.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from .config import settings


class TokenStoreError(RuntimeError):
    pass


def generate_key() -> str:
    return Fernet.generate_key().decode()


def _fernet(key: str | None = None) -> MultiFernet:
    primary = key or settings.token_encryption_key
    if not primary:
        raise TokenStoreError("TOKEN_ENCRYPTION_KEY не задан. Сгенерируйте: python -m app.cli gen-key")
    keys = [primary] + ([] if key else [k for k in settings.token_encryption_keys_old if k])
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except (ValueError, TypeError) as exc:
        raise TokenStoreError("Некорректный формат ключа шифрования (нужен Fernet key)") from exc


def _upsert(conn: sqlite3.Connection, f: MultiFernet, provider: str, subject: str, token: str,
            scopes: str | None, expires_at: datetime | None) -> None:
    conn.execute(
        """INSERT INTO oauth_tokens(provider, subject, token_encrypted, scopes, expires_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(provider, subject) DO UPDATE SET token_encrypted=excluded.token_encrypted,
             scopes=excluded.scopes, expires_at=excluded.expires_at, created_at=datetime('now')""",
        (provider, subject, f.encrypt(token.encode()), scopes, expires_at.isoformat(timespec="seconds") if expires_at else None),
    )


def save_token(conn: sqlite3.Connection, provider: str, subject: str, token: str,
               scopes: str | None = None, expires_at: datetime | None = None, key: str | None = None) -> None:
    _upsert(conn, _fernet(key), provider, subject, token, scopes, expires_at)
    conn.commit()


def replace_tokens(conn: sqlite3.Connection, items: list[dict], key: str | None = None) -> None:
    """Атомарно заменить все токены Meta новым набором (ротация после повторного входа).
    items: [{provider, subject, token, scopes?, expires_at?}]. При ошибке старые токены сохраняются."""
    f = _fernet(key)
    try:
        conn.execute("SAVEPOINT replace_tokens")
        conn.execute("DELETE FROM oauth_tokens WHERE provider LIKE 'meta_%'")
        for it in items:
            _upsert(conn, f, it["provider"], it["subject"], it["token"], it.get("scopes"), it.get("expires_at"))
        conn.execute("RELEASE SAVEPOINT replace_tokens")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT replace_tokens")
        conn.execute("RELEASE SAVEPOINT replace_tokens")
        raise


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
        raise TokenStoreError("Не удалось расшифровать токен: ключ изменился? Повторите вход через Meta.") from exc


def rotate_encryption(conn: sqlite3.Connection) -> int:
    """Перешифровать все токены текущим основным ключом."""
    f = _fernet()
    rows = conn.execute("SELECT id, token_encrypted FROM oauth_tokens").fetchall()
    for r in rows:
        try:
            new = f.rotate(r["token_encrypted"])
        except InvalidToken as exc:
            raise TokenStoreError(f"Токен #{r['id']} не расшифровывается ни одним из ключей") from exc
        conn.execute("UPDATE oauth_tokens SET token_encrypted=? WHERE id=?", (new, r["id"]))
    conn.commit()
    return len(rows)


def token_status(conn: sqlite3.Connection) -> list[dict]:
    """Метаданные токенов без самих токенов — для отображения в интерфейсе."""
    return [dict(r) for r in conn.execute(
        "SELECT provider, subject, scopes, expires_at, created_at FROM oauth_tokens ORDER BY provider"
    ).fetchall()]


def delete_tokens(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM oauth_tokens")
    conn.commit()
