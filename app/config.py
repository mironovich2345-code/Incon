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


def _default_data_dir() -> Path:
    # DATA_DIR > том Railway (RAILWAY_VOLUME_MOUNT_PATH задаётся Railway автоматически) > ./data
    return Path(os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or BASE_DIR / "data")


def _default_redirect_uri() -> str:
    explicit = os.environ.get("META_REDIRECT_URI")
    if explicit:
        return explicit.strip()
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    return f"https://{domain}/meta/callback" if domain else "https://localhost:8000/meta/callback"


@dataclass
class Settings:
    data_dir: Path = field(default_factory=_default_data_dir)
    app_password: str = field(default_factory=lambda: os.environ.get("APP_PASSWORD", ""))

    # Шифрование токенов (Fernet key, 32 байта base64). Обязателен для API-режима.
    token_encryption_key: str = field(default_factory=lambda: os.environ.get("TOKEN_ENCRYPTION_KEY", ""))
    # Предыдущие ключи (через запятую) — только на время ротации ключа.
    token_encryption_keys_old: list[str] = field(
        default_factory=lambda: [k.strip() for k in os.environ.get("TOKEN_ENCRYPTION_KEYS_OLD", "").split(",") if k.strip()]
    )

    # Meta / Instagram (Facebook Login for Business). По умолчанию API выключен:
    # MVP работает на ручном импорте данных.
    meta_api_enabled: bool = field(default_factory=lambda: _bool("META_API_ENABLED"))
    meta_app_id: str = field(default_factory=lambda: os.environ.get("META_APP_ID", "").strip())
    meta_app_secret: str = field(default_factory=lambda: os.environ.get("META_APP_SECRET", "").strip())
    meta_login_config_id: str = field(default_factory=lambda: os.environ.get("META_LOGIN_CONFIG_ID", "").strip())
    # Должен ТОЧНО совпадать с адресом в «Valid OAuth Redirect URIs» приложения Meta.
    meta_redirect_uri: str = field(default_factory=_default_redirect_uri)
    # Если к пользователю привязано несколько Instagram-аккаунтов — какой подключать.
    meta_ig_username: str = field(default_factory=lambda: os.environ.get("META_IG_USERNAME", "").lstrip("@").lower())
    # Разрешить работу без APP_PASSWORD (только если интерфейс доступен исключительно с localhost).
    allow_no_password: bool = field(default_factory=lambda: _bool("ALLOW_NO_PASSWORD"))
    # Проверьте актуальную версию в Graph API changelog перед подключением.
    graph_api_version: str = field(default_factory=lambda: os.environ.get("GRAPH_API_VERSION", "v24.0"))

    # Окружение Railway (переменные задаются Railway автоматически).
    railway_environment: str = field(default_factory=lambda: os.environ.get("RAILWAY_ENVIRONMENT", ""))
    railway_public_domain: str = field(default_factory=lambda: os.environ.get("RAILWAY_PUBLIC_DOMAIN", ""))
    railway_volume_mount_path: str = field(default_factory=lambda: os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ""))

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
    def is_hosted(self) -> bool:
        """Публично доступное развёртывание (Railway): пароль обязателен, ALLOW_NO_PASSWORD игнорируется."""
        return bool(self.railway_environment)

    @property
    def password_required(self) -> bool:
        return self.is_hosted or not self.allow_no_password

    @property
    def storage_persistent(self) -> bool | None:
        """На Railway данные должны лежать на томе, иначе пропадут при каждом деплое."""
        if not self.is_hosted:
            return None
        vol = self.railway_volume_mount_path
        if not vol:
            return False
        try:
            self.data_dir.resolve().relative_to(Path(vol).resolve())
            return True
        except ValueError:
            return False

    def deployment_problems(self) -> list[str]:
        p = []
        if self.is_hosted and not self.app_password:
            p.append("APP_PASSWORD не задан — интерфейс закрыт до установки пароля")
        if self.storage_persistent is False:
            p.append("Данные не на постоянном томе Railway: подключите Volume (Mount Path /data) — "
                     "иначе база и загруженные файлы удаляются при каждом деплое")
        return p

    @property
    def meta_configured(self) -> bool:
        return not self.meta_config_problems()

    def meta_config_problems(self) -> list[str]:
        """Что мешает OAuth-входу. Значения секретов не раскрываются."""
        p = []
        if not self.meta_app_id:
            p.append("META_APP_ID не задан")
        if not self.meta_app_secret:
            p.append("META_APP_SECRET не задан")
        if not self.meta_login_config_id:
            p.append("META_LOGIN_CONFIG_ID не задан (новые приложения Meta не принимают scope, нужен config_id)")
        if not self.token_encryption_key:
            p.append("TOKEN_ENCRYPTION_KEY не задан")
        from urllib.parse import urlparse
        u = urlparse(self.meta_redirect_uri)
        if u.scheme != "https":
            p.append("META_REDIRECT_URI должен начинаться с https://")
        if u.path != "/meta/callback":
            p.append("Путь META_REDIRECT_URI должен быть /meta/callback")
        if u.query or u.fragment:
            p.append("META_REDIRECT_URI не должен содержать ? или #")
        if u.netloc.split(":")[0] in {"0.0.0.0", "127.0.0.1"}:
            p.append("META_REDIRECT_URI должен указывать на домен, зарегистрированный в Meta")
        if self.railway_public_domain and u.netloc != self.railway_public_domain:
            p.append(f"Хост META_REDIRECT_URI ({u.netloc}) не совпадает с доменом сервиса Railway "
                     f"({self.railway_public_domain})")
        if self.meta_app_id and not self.meta_app_id.isdigit():
            p.append("META_APP_ID должен состоять только из цифр")
        if self.meta_login_config_id and not self.meta_login_config_id.isdigit():
            p.append("META_LOGIN_CONFIG_ID должен состоять только из цифр")
        if not self.app_password and self.password_required:
            p.append("APP_PASSWORD не задан: интерфейс с токенами нельзя оставлять без пароля"
                     + ("" if self.is_hosted else " (или ALLOW_NO_PASSWORD=true, если сервер слушает только 127.0.0.1)"))
        p.extend(x for x in self.deployment_problems() if not x.startswith("APP_PASSWORD"))
        return p


settings = Settings()
