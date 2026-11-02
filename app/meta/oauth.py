"""OAuth-подключение Instagram через Facebook Login for Business (конфигурация Unified Login).

Поток:
  1. /meta/login: генерируем state, кладём его в HttpOnly-cookie и в БД (TTL 10 мин),
     перенаправляем в диалог Meta с config_id.
  2. /meta/callback: сверяем state (cookie + БД, одноразовый), обрабатываем отказ пользователя,
     меняем code на токен.
  3. debug_token: определяем тип токена (USER / SYSTEM_USER), срок жизни, выданные разрешения.
  4. Для USER-токена с ограниченным сроком — обмен на долгоживущий (~60 дней).
     SYSTEM_USER-токены и токены «без срока» не обмениваем.
  5. Поиск Instagram-аккаунта (по порядку):
       a) /me/accounts → страница с instagram_business_account → токен страницы;
       b) granular_scopes.target_ids из debug_token → id Instagram-аккаунта, работаем токеном входа;
     Если аккаунтов несколько — выбираем по META_IG_USERNAME.
  6. Атомарно заменяем старые токены новыми (ротация), снимаем флаг «нужен повторный вход».

Facebook user token нельзя продлить без участия пользователя: когда срок подходит к концу
(или Meta возвращает ошибку 190), интерфейс просит войти заново — это и есть ротация.
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from datetime import datetime, timedelta

import httpx

from ..config import settings
from ..db import kv_get, kv_set
from ..security import load_token, replace_tokens
from .client import (CONDITIONAL_SCOPES, REQUIRED_SCOPES, UNNEEDED_SCOPES, GraphClient, GraphError,
                     debug_token, exchange_code, exchange_long_lived, ig_ids_from_debug, scrub)

STATE_TTL = 600
STATE_COOKIE = "ci_oauth_state"
WARN_DAYS = 7


class OAuthError(RuntimeError):
    pass


# ---------------- state ----------------

def new_state(conn: sqlite3.Connection) -> str:
    state = secrets.token_urlsafe(32)
    kv_set(conn, "oauth_state", f"{state}:{int(time.time())}")
    return state


def check_state(conn: sqlite3.Connection, state: str | None, cookie_state: str | None) -> None:
    """Одноразовая проверка: state из URL == state в cookie браузера == state в БД, не старше 10 минут."""
    saved = kv_get(conn, "oauth_state") or ""
    kv_set(conn, "oauth_state", "")  # одноразовый
    value, _, ts = saved.partition(":")
    if not state or not value or not cookie_state:
        raise OAuthError("Отсутствует state. Начните вход заново со страницы «API».")
    if not (secrets.compare_digest(state, value) and secrets.compare_digest(state, cookie_state)):
        raise OAuthError("state не совпадает (возможная CSRF-атака или вход начат в другом окне). Повторите вход.")
    if time.time() - int(ts or 0) > STATE_TTL:
        raise OAuthError("Срок state истёк (10 минут). Повторите вход.")


# ---------------- connect ----------------

def _ts(v) -> datetime | None:
    return datetime.fromtimestamp(int(v)) if v else None


def complete_login(conn: sqlite3.Connection, code: str, transport: httpx.BaseTransport | None = None) -> dict:
    """Шаги 2–6. Возвращает сводку подключения (без токенов)."""
    tok = exchange_code(code, transport=transport)
    token = tok["access_token"]

    dbg = debug_token(token, transport=transport)
    if not dbg.ok:
        # Без типа токена нельзя безопасно решить, нужен ли обмен на долгоживущий:
        # иначе сохранился бы короткий токен (1–2 часа). Токен не сохраняем.
        e = dbg.error or {}
        raise OAuthError(f"Не удалось проверить токен (debug_token): HTTP {dbg.http_status}, code {e.get('code')}: "
                         f"{e.get('message')}. Проверьте META_APP_SECRET.")
    info = (dbg.data or {}).get("data", {})
    if not info.get("is_valid", False):
        raise OAuthError("Meta вернула недействительный токен (debug_token: is_valid=false)")
    token_type = (info.get("type") or "UNKNOWN").upper()
    if token_type not in {"USER", "SYSTEM_USER"}:
        raise OAuthError(f"Неподдерживаемый тип токена: {token_type}. Ожидается USER или SYSTEM_USER "
                         "(проверьте тип токена в конфигурации входа).")
    scopes = info.get("scopes") or []
    exchanged = False

    expires_in = tok.get("expires_in")
    if token_type == "USER" and (expires_in or info.get("expires_at")):
        try:
            long = exchange_long_lived(token, transport=transport)
            token, expires_in, exchanged = long["access_token"], long.get("expires_in"), True
            dbg2 = debug_token(token, transport=transport)
            if dbg2.ok:
                info = dbg2.data.get("data", {})
        except GraphError as exc:
            raise OAuthError(f"Не удалось получить долгоживущий токен: {exc}") from exc

    user_expires = _ts(info.get("expires_at")) or (datetime.now() + timedelta(seconds=int(expires_in)) if expires_in else None)
    login_provider = "meta_system_user" if token_type == "SYSTEM_USER" else "meta_user"
    items = [{"provider": login_provider, "subject": str(info.get("user_id") or "me"), "token": token,
              "scopes": ",".join(scopes), "expires_at": user_expires}]

    with GraphClient(token, transport=transport) as client:
        candidates = []  # (ig_id, username, page_id, page_token)
        pages_error = None
        try:
            for p in client.pages_with_instagram():
                iga = p["instagram_business_account"]
                candidates.append((iga["id"], (iga.get("username") or "").lower(), p["id"], p.get("access_token")))
        except GraphError as exc:
            pages_error = str(exc)
        source = "me/accounts"
        if not candidates:
            source = "debug_token.granular_scopes"
            for ig_id in ig_ids_from_debug({"data": info}):
                try:
                    u = client.ig_user(ig_id)
                    candidates.append((ig_id, (u.get("username") or "").lower(), None, None))
                except GraphError:
                    candidates.append((ig_id, "", None, None))
    if not candidates:
        raise OAuthError("Не найден профессиональный Instagram-аккаунт, доступный этому входу. "
                         "Проверьте, что Instagram привязан к Facebook-странице и выбран в окне входа Meta"
                         + (f" (/me/accounts: {pages_error})" if pages_error else ""))
    chosen = candidates[0]
    if settings.meta_ig_username:
        match = [c for c in candidates if c[1] == settings.meta_ig_username]
        if not match:
            raise OAuthError(f"Аккаунт @{settings.meta_ig_username} не найден среди доступных: "
                             + ", ".join("@" + (c[1] or c[0]) for c in candidates))
        chosen = match[0]
    ig_id, ig_username, page_id, page_token = chosen
    if page_id and page_token:
        # Токен страницы, полученный из долгоживущего user token, не имеет фиксированного срока.
        items.append({"provider": "meta_page", "subject": page_id, "token": page_token, "scopes": None, "expires_at": None})

    replace_tokens(conn, items)
    for k, v in {"ig_user_id": ig_id, "ig_username": ig_username, "page_id": page_id or "",
                 "token_type": token_type, "account_source": source, "auth_provider": login_provider,
                 "connected_at": datetime.now().isoformat(timespec="seconds"), "reauth_required": ""}.items():
        kv_set(conn, k, v)
    return {"ig_user_id": ig_id, "ig_username": ig_username, "page_id": page_id, "token_type": token_type,
            "exchanged_long_lived": exchanged, "account_source": source, "candidates": len(candidates),
            "scopes": scopes, "missing_scopes": [s for s in REQUIRED_SCOPES if scopes and s not in scopes],
            "unneeded_scopes": [s for s in scopes if s in UNNEEDED_SCOPES]}


def active_token(conn: sqlite3.Connection) -> tuple[str | None, str]:
    """Токен для запросов: токен страницы, иначе токен входа."""
    page_id = kv_get(conn, "page_id")
    if page_id:
        t = load_token(conn, "meta_page", page_id)
        if t:
            return t, "meta_page"
    for provider in ("meta_user", "meta_system_user"):
        t = load_token(conn, provider)
        if t:
            return t, provider
    return None, ""


def login_token(conn: sqlite3.Connection) -> str | None:
    return load_token(conn, "meta_user") or load_token(conn, "meta_system_user")


def mark_reauth(conn: sqlite3.Connection, reason: str) -> None:
    kv_set(conn, "reauth_required", scrub(reason)[:300])


def token_health(conn: sqlite3.Connection, transport: httpx.BaseTransport | None = None) -> dict:
    """Проверка токена входа через debug_token: валидность, тип, сроки, разрешения."""
    token = login_token(conn)
    if not token:
        return {"status": "not_connected"}
    r = debug_token(token, transport=transport)
    if not r.ok:
        return {"status": "error", "error": r.error, "http_status": r.http_status}
    d = r.data.get("data", {})
    now = time.time()
    exp, data_exp = d.get("expires_at") or 0, d.get("data_access_expires_at") or 0
    days = lambda t: round((t - now) / 86400, 1) if t else None  # noqa: E731
    scopes = d.get("scopes") or []
    res = {
        "is_valid": d.get("is_valid"), "type": d.get("type"), "application": d.get("application"),
        "expires_at": _ts(exp).isoformat() if exp else "без срока",
        "data_access_expires_at": _ts(data_exp).isoformat() if data_exp else None,
        "days_left": days(exp), "data_access_days_left": days(data_exp),
        "scopes": scopes,
        "missing_scopes": [s for s in REQUIRED_SCOPES if s not in scopes],
        "conditional_scopes": [s for s in CONDITIONAL_SCOPES if s in scopes],
        "unneeded_scopes": [s for s in scopes if s in UNNEEDED_SCOPES],
        "granular_ig_ids": ig_ids_from_debug(r.data),
    }
    left = [x for x in (res["days_left"], res["data_access_days_left"]) if x is not None]
    if not d.get("is_valid"):
        res["status"] = "invalid"
        mark_reauth(conn, "Токен недействителен (debug_token)")
    elif left and min(left) <= 0:
        res["status"] = "expired"
        mark_reauth(conn, "Срок токена или доступа к данным истёк")
    elif left and min(left) < WARN_DAYS:
        res["status"] = "expiring"
    else:
        res["status"] = "ok"
    return res
