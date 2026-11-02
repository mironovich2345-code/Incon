"""Настройки приложения. Читаются из переменных окружения и файла .env.

Секреты (токены Meta, ключ шифрования, ключ Anthropic) задаются только на
сервере и никогда не отдаются в браузер.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", BASE_DIR / "data")))
    app_password: str = field(default_factory=lambda: os.environ.get("APP_PASSWORD", ""))
    session_secret: str = field(default_factory=lambda: os.environ.get("SESSION_SECRET", ""))

    # Шифрование токенов (Fernet key, 32 байта base64). Обязателен для API-режима.
    token_encryption_key: str = field(default_factory=lambda: os.environ.get("TOKEN_ENCRYPTION_KEY", ""))

    # Meta / Instagram (Facebook Login for Business). По умолчанию API выключен:
    # MVP работает на ручном импорте данных.
    meta_api_enabled: bool = field(default_factory=lambda: _bool("META_API_ENABLED"))
    meta_app_id: str = field(default_factory=lambda: os.environ.get("META_APP_ID", ""))
    meta_app_secret: str = field(default_factory=lambda: os.environ.get("META_APP_SECRET", ""))
    meta_login_config_id: str = field(default_factory=lambda: os.environ.get("META_LOGIN_CONFIG_ID", ""))
    meta_redirect_uri: str = field(
        default_factory=lambda: os.environ.get("META_REDIRECT_URI", "http://localhost:8000/meta/callback")
    )
    # Проверьте актуальную версию в Graph API changelog перед подключением.
    graph_api_version: str = field(default_factory=lambda: os.environ.get("GRAPH_API_VERSION", "v24.0"))

    # Публикация намеренно отключена. Флага для включения нет — это отдельный этап.
    publishing_enabled: bool = False

    # Claude
    anthropic_model: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5"))
    claude_enabled: bool = field(default_factory=lambda: _bool("CLAUDE_ENABLED", True))

    # Порог «аномально высокого» ролика: views / медиана аккаунта.
    outlier_ratio: float = field(default_factory=lambda: float(os.environ.get("OUTLIER_RATIO", "2.0")))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "incon.sqlite3"

    @property
    def references_dir(self) -> Path:
        return self.data_dir / "references"

    @property
    def drafts_dir(self) -> Path:
        return self.data_dir / "drafts"

    @property
    def meta_configured(self) -> bool:
        return bool(self.meta_app_id and self.meta_app_secret and self.token_encryption_key)


settings = Settings()
