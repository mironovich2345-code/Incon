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
                     debug_token, exchange_code, exchange_long_lived, ig_ids_from_debug, scrub)  # noqa: F401

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


# Понятные причины неудачного подключения (показываются в интерфейсе).
REASONS = {
    "state_invalid": "Проверка безопасности входа не пройдена (state). Начните вход заново кнопкой на этой странице.",
    "user_denied": "Вход отменён или разрешения не выданы в окне Meta.",
    "meta_error": "Meta вернула ошибку в ответ на вход.",
    "no_code": "Meta не вернула код авторизации.",
    "exchange_failed": "Не удалось обменять код авторизации на токен (проверьте META_APP_SECRET и Redirect URI).",
    "debug_token_failed": "Не удалось проверить токен через debug_token (проверьте META_APP_ID и META_APP_SECRET).",
    "token_invalid": "Meta выдала недействительный токен.",
    "unsupported_token_type": "Тип токена не поддерживается (ожидается USER или SYSTEM_USER).",
    "long_lived_failed": "Не удалось получить долгоживущий токен.",
    "no_instagram_scopes": "В токене нет ни одного Instagram-разрешения: конфигурация входа не выдаёт доступ к Instagram.",
    "no_instagram_found": "Вход выполнен, но ни один способ не вернул Instagram-аккаунт.",
    "business_management_missing": ("Вход выполнен токеном системного пользователя (SYSTEM_USER), но в нём нет "
                                    "разрешения business_management, и ни один задокументированный способ не вернул "
                                    "Instagram-аккаунт: endpoints бизнес-портфолио отвечают «Missing Permission»."),
    "profile_unreadable": "Instagram-аккаунт найден, но его профиль не читается с выданным токеном.",
    "username_mismatch": "Аккаунт из META_IG_USERNAME не найден среди доступных этому входу.",
    "storage_failed": "Не удалось сохранить токены (проверьте TOKEN_ENCRYPTION_KEY).",
    "ok": "Подключено.",
}


class LoginFailure(OAuthError):
    def __init__(self, reason: str, detail: str = "", report: dict | None = None):
        self.reason, self.detail, self.report = reason, detail, report or {}
        super().__init__(REASONS.get(reason, reason) + (f" {detail}" if detail else ""))


def save_login_attempt(conn: sqlite3.Connection, report: dict) -> None:
    """Обезличенный отчёт: без токенов, кодов OAuth, ID и имён. Хранятся 20 последних."""
    import json
    report.setdefault("at", datetime.now().isoformat(timespec="seconds"))
    conn.execute("INSERT INTO login_attempts(ok, reason, report_json) VALUES (?,?,?)",
                 (1 if report.get("reason") == "ok" else 0, report.get("reason"),
                  json.dumps(report, ensure_ascii=False, default=str)))
    conn.execute("DELETE FROM login_attempts WHERE id NOT IN (SELECT id FROM login_attempts ORDER BY id DESC LIMIT 20)")
    conn.commit()


def last_login_attempt(conn: sqlite3.Connection) -> dict | None:
    import json
    r = conn.execute("SELECT report_json FROM login_attempts ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(r["report_json"]) if r else None


def record_failure(conn: sqlite3.Connection, reason: str, stage: str, detail: str = "", /, **extra) -> LoginFailure:
    from .discovery import anonymize
    extra.pop("stage", None)
    rep = {"reason": reason, "reason_text": REASONS.get(reason, reason), "stage": stage,
           "detail": anonymize(detail) if detail else None, **extra}
    save_login_attempt(conn, rep)
    return LoginFailure(reason, anonymize(detail) or "", rep)


def complete_login(conn: sqlite3.Connection, code: str, transport: httpx.BaseTransport | None = None) -> dict:
    """Шаги 2–6. Возвращает сводку подключения (без токенов).
    При любой неудаче сохраняет обезличенный отчёт и поднимает LoginFailure с понятной причиной."""
    from .discovery import anonymize, discover, granular_summary

    rep: dict = {"stage": "exchange", "graph_api_version": settings.graph_api_version}
    try:
        tok = exchange_code(code, transport=transport)
    except GraphError as exc:
        raise record_failure(conn, "exchange_failed", "exchange", str(exc), http_status=exc.status, error_code=exc.code)
    token = tok["access_token"]

    dbg = debug_token(token, transport=transport)
    if not dbg.ok:
        # Без типа токена нельзя безопасно решить, нужен ли обмен на долгоживущий:
        # иначе сохранился бы короткий токен (1–2 часа). Токен не сохраняем.
        e = dbg.error or {}
        raise record_failure(conn, "debug_token_failed", "debug_token", e.get("message", ""),
                             http_status=dbg.http_status, error_code=e.get("code"))
    info = (dbg.data or {}).get("data", {})
    token_type = (info.get("type") or "UNKNOWN").upper()
    scopes = info.get("scopes") or []
    rep.update({"token_type": token_type, "scopes": scopes, "granular_scopes": granular_summary(info),
                "token_has_expiry": bool(info.get("expires_at")), "data_access_expiry": bool(info.get("data_access_expires_at"))})
    if not info.get("is_valid", False):
        raise record_failure(conn, "token_invalid", "debug_token", **rep)
    if token_type not in {"USER", "SYSTEM_USER"}:
        raise record_failure(conn, "unsupported_token_type", "debug_token", f"Получен тип: {token_type}.", **rep)
    exchanged = False

    expires_in = tok.get("expires_in")
    if token_type == "USER" and (expires_in or info.get("expires_at")):
        try:
            long = exchange_long_lived(token, transport=transport)
            token, expires_in, exchanged = long["access_token"], long.get("expires_in"), True
            dbg2 = debug_token(token, transport=transport)
            if dbg2.ok:
                info = dbg2.data.get("data", {}) or info
        except GraphError as exc:
            raise record_failure(conn, "long_lived_failed", "long_lived", str(exc), **rep)
    rep["exchanged_long_lived"] = exchanged

    user_expires = _ts(info.get("expires_at")) or (datetime.now() + timedelta(seconds=int(expires_in)) if expires_in else None)
    login_provider = "meta_system_user" if token_type == "SYSTEM_USER" else "meta_user"
    items = [{"provider": login_provider, "subject": str(info.get("user_id") or "me"), "token": token,
              "scopes": ",".join(scopes), "expires_at": user_expires}]

    rep["stage"] = "discovery"
    with GraphClient(token, transport=transport) as client:
        found = discover(client, info, token_type=token_type)
    rep["probes"] = found.public_probes()
    rep["candidates"] = len(found.candidates)
    rep["verified_candidates"] = sum(1 for c in found.candidates if c.verified)
    rep["candidate_sources"] = sorted({c.source for c in found.candidates})

    if not found.candidates:
        has_ig_scope = any(str(s).startswith("instagram") for s in scopes)
        if scopes and not has_ig_scope:
            raise record_failure(conn, "no_instagram_scopes", "discovery",
                                 "Выданные разрешения: " + ", ".join(scopes) + ".", **rep)
        from .discovery import is_missing_permission
        blocked = sorted({p.endpoint for p in found.probes if is_missing_permission(p)})
        rep["missing_permission_endpoints"] = blocked
        if token_type == "SYSTEM_USER" and "business_management" not in scopes:
            raise record_failure(conn, "business_management_missing", "discovery",
                                 "Требуется добавить business_management в существующую конфигурацию входа "
                                 "(тот же config_id) и войти заново." + (" Отклонены: " + ", ".join(blocked) + "." if blocked else ""),
                                 **rep)
        raise record_failure(conn, "no_instagram_found", "discovery",
                             "Проверьте, что в окне Meta выбран Instagram-аккаунт и что конфигурация входа "
                             "включает instagram_basic. Подробности — в отчёте о попытке.", **rep)
    verified = [c for c in found.candidates if c.verified]
    if not verified:
        failed = [p for p in found.probes if p.method.startswith("Проверка профиля")]
        code_ = failed[0].error_code if failed else None
        raise record_failure(conn, "profile_unreadable", "verify",
                             f"Код ошибки Meta: {code_}." if code_ else "", **rep)
    chosen = verified[0]
    if settings.meta_ig_username:
        match = [c for c in verified if c.username == settings.meta_ig_username]
        if not match:
            raise record_failure(conn, "username_mismatch", "select",
                                 f"Доступно аккаунтов: {len(verified)}.", **rep)
        chosen = match[0]
    if chosen.page_id and chosen.page_token:
        # Токен страницы, полученный из долгоживущего user token, не имеет фиксированного срока.
        items.append({"provider": "meta_page", "subject": chosen.page_id, "token": chosen.page_token,
                      "scopes": None, "expires_at": None})

    try:
        replace_tokens(conn, items)
    except Exception as exc:  # noqa: BLE001 — сообщение без секретов
        raise record_failure(conn, "storage_failed", "store", type(exc).__name__, **rep) from exc
    page_used = bool(chosen.page_id and chosen.page_token)
    for k, v in {"ig_user_id": chosen.ig_id, "ig_username": chosen.username, "page_id": chosen.page_id if page_used else "",
                 "token_type": token_type, "account_source": chosen.source, "auth_provider": login_provider,
                 "connected_at": datetime.now().isoformat(timespec="seconds"), "reauth_required": ""}.items():
        kv_set(conn, k, v or "")
    rep.update({"reason": "ok", "reason_text": REASONS["ok"], "stage": "done", "account_source": chosen.source,
                "page_token_used": page_used})
    save_login_attempt(conn, rep)
    return {"ig_user_id": chosen.ig_id, "ig_username": chosen.username, "page_id": chosen.page_id if page_used else None,
            "token_type": token_type, "exchanged_long_lived": exchanged, "account_source": chosen.source,
            "candidates": len(found.candidates), "scopes": scopes,
            "missing_scopes": [s for s in REQUIRED_SCOPES if scopes and s not in scopes
                               and not (token_type == "SYSTEM_USER" and s.startswith("pages_"))],
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
