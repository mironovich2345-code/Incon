"""Клиент Instagram Graph API (Instagram API with Facebook Login for Business / Unified Login).

Только чтение: профиль, медиа, insights, business_discovery, debug_token.
Методов публикации здесь нет намеренно (POST /media и /media_publish не реализованы).

Безопасность:
  * токен передаётся только в запрос к graph.facebook.com и вырезается из всех текстов ошибок;
  * логгер httpx принудительно переведён на WARNING — иначе на уровне INFO он пишет URL с токеном;
  * если задан META_APP_SECRET, к каждому запросу добавляется appsecret_proof
    (HMAC-SHA256 токена ключом app secret) — можно включить «Require App Secret» в настройках приложения.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import settings
from ..metrics import REEL_INSIGHT_METRICS

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

GRAPH = "https://graph.facebook.com"

MEDIA_FIELDS = "id,caption,media_type,media_product_type,permalink,shortcode,timestamp,like_count,comments_count,thumbnail_url"
DISCOVERY_PROFILE_FIELDS = "username,name,biography,followers_count,follows_count,media_count,website"
DISCOVERY_MEDIA_FIELDS = "id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count,view_count"

# Минимальный набор разрешений для чтения (см. docs/01-meta-api-research.md).
REQUIRED_SCOPES = ["instagram_basic", "instagram_manage_insights", "pages_show_list", "pages_read_engagement"]
# Нужно только если доступ к странице выдан через бизнес-портфолио.
CONDITIONAL_SCOPES = ["business_management"]
# Разрешения, которые этому приложению не нужны: диагностика предупредит, если они выданы.
UNNEEDED_SCOPES = [
    "instagram_content_publish", "instagram_manage_messages", "instagram_manage_comments",
    "instagram_business_content_publish", "instagram_business_manage_messages", "instagram_business_manage_comments",
    "ads_management", "ads_read", "pages_manage_metadata", "pages_messaging", "pages_manage_posts",
]

# Коды ошибок Graph API, по которым принимаем решения.
TOKEN_ERROR_CODES = {190, 102}            # токен недействителен / истёк
PERMISSION_ERROR_CODES = {10, 200, 3}     # нет разрешения / возможности у приложения
RATE_LIMIT_CODES = {4, 17, 32, 613, 80002}

_TOKENISH = re.compile(r"(access_token|input_token|fb_exchange_token|client_secret|appsecret_proof|code)=[^&\s\"']+")


def scrub(text: str, *secrets: str | None) -> str:
    """Удаляет из строки токены и секреты (и по имени параметра, и по значению)."""
    out = _TOKENISH.sub(lambda m: f"{m.group(1)}=***", str(text))
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "***")
    return out


class GraphError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, subcode: int | None = None,
                 status: int | None = None, error_type: str | None = None, fbtrace_id: str | None = None):
        super().__init__(message)
        self.code, self.subcode, self.status = code, subcode, status
        self.error_type, self.fbtrace_id = error_type, fbtrace_id

    @property
    def kind(self) -> str:
        if self.status is None:
            return "network"
        if self.code in TOKEN_ERROR_CODES:
            return "token"
        if self.code in PERMISSION_ERROR_CODES:
            return "permission"
        if self.code in RATE_LIMIT_CODES:
            return "rate_limit"
        return "api"

    def as_dict(self) -> dict:
        return {"message": str(self), "code": self.code, "subcode": self.subcode, "http_status": self.status,
                "type": self.error_type, "fbtrace_id": self.fbtrace_id, "kind": self.kind}


@dataclass
class CallResult:
    """Результат одного запроса — без токена, пригоден для сохранения в диагностике."""
    path: str
    params: dict
    http_status: int | None
    ok: bool
    data: Any = None
    error: dict | None = None
    ms: int = 0
    usage: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"path": self.path, "params": self.params, "http_status": self.http_status, "ok": self.ok,
                "error": self.error, "ms": self.ms, "usage": self.usage}


def appsecret_proof(token: str, secret: str) -> str:
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


class GraphClient:
    def __init__(self, token: str, version: str | None = None, transport: httpx.BaseTransport | None = None,
                 app_secret: str | None = None):
        self._token = token
        self._secret = settings.meta_app_secret if app_secret is None else app_secret
        self.version = version or settings.graph_api_version
        self._http = httpx.Client(base_url=f"{GRAPH}/{self.version}", timeout=30, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GraphClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def call(self, path: str, **params: Any) -> CallResult:
        """Запрос без исключений: всегда возвращает CallResult (для диагностики)."""
        public_params = {k: v for k, v in params.items()}
        q = dict(params)
        q["access_token"] = self._token
        if self._secret:
            q["appsecret_proof"] = appsecret_proof(self._token, self._secret)
        t0 = time.monotonic()
        try:
            resp = self._http.get(path, params=q)
        except httpx.HTTPError as exc:
            err = GraphError(scrub(f"Сетевая ошибка: {type(exc).__name__}: {exc}", self._token, self._secret))
            return CallResult(path, public_params, None, False, error=err.as_dict(), ms=int((time.monotonic() - t0) * 1000))
        ms = int((time.monotonic() - t0) * 1000)
        usage = {k: resp.headers[k] for k in ("x-app-usage", "x-business-use-case-usage") if k in resp.headers}
        try:
            data = resp.json()
        except ValueError:
            err = GraphError(f"Ответ не в формате JSON (HTTP {resp.status_code})", status=resp.status_code)
            return CallResult(path, public_params, resp.status_code, False, error=err.as_dict(), ms=ms, usage=usage)
        if resp.status_code >= 400 or (isinstance(data, dict) and "error" in data):
            e = data.get("error", {}) if isinstance(data, dict) else {}
            err = GraphError(scrub(e.get("message", f"HTTP {resp.status_code}"), self._token, self._secret),
                             e.get("code"), e.get("error_subcode"), resp.status_code, e.get("type"), e.get("fbtrace_id"))
            return CallResult(path, public_params, resp.status_code, False, error=err.as_dict(), ms=ms, usage=usage)
        return CallResult(path, public_params, resp.status_code, True, data=data, ms=ms, usage=usage)

    def get(self, path: str, **params: Any) -> dict:
        r = self.call(path, **params)
        if not r.ok:
            e = r.error or {}
            raise GraphError(e.get("message", "ошибка"), e.get("code"), e.get("subcode"), e.get("http_status"),
                             e.get("type"), e.get("fbtrace_id"))
        return r.data

    # ---------- Свой аккаунт ----------
    def pages_with_instagram(self) -> list[dict]:
        out, after = [], None
        for _ in range(10):
            params = {"fields": "id,name,access_token,instagram_business_account{id,username}", "limit": 50}
            if after:
                params["after"] = after
            data = self.get("/me/accounts", **params)
            out.extend(p for p in data.get("data", []) if p.get("instagram_business_account"))
            after = data.get("paging", {}).get("cursors", {}).get("after")
            if not data.get("paging", {}).get("next"):
                break
        return out

    def ig_user(self, ig_user_id: str) -> dict:
        return self.get(f"/{ig_user_id}", fields="id,username,name,followers_count,follows_count,media_count")

    def list_media(self, ig_user_id: str, limit: int = 50, max_pages: int = 10) -> list[dict]:
        out, after = [], None
        for _ in range(max_pages):
            params = {"fields": MEDIA_FIELDS, "limit": limit}
            if after:
                params["after"] = after
            data = self.get(f"/{ig_user_id}/media", **params)
            out.extend(data.get("data", []))
            after = data.get("paging", {}).get("cursors", {}).get("after")
            if not data.get("paging", {}).get("next"):
                break
        return out

    def media_insights(self, media_id: str, metrics: list[str] | None = None) -> tuple[dict, dict]:
        """Пакетный запрос; при ошибке — по одной метрике, чтобы понять, какая недоступна.
        Возвращает (значения, {метрика: причина}). Ошибки токена и лимитов пробрасываются."""
        metrics = metrics or REEL_INSIGHT_METRICS
        r = self.call(f"/{media_id}/insights", metric=",".join(metrics))
        if r.ok:
            return parse_insights(r.data), {}
        _raise_if_fatal(r)
        values, unavailable = {}, {}
        for m in metrics:
            one = self.call(f"/{media_id}/insights", metric=m)
            if one.ok:
                values.update(parse_insights(one.data))
            else:
                _raise_if_fatal(one)
                unavailable[m] = (one.error or {}).get("message", "ошибка")
        return values, unavailable

    def account_insights(self, ig_user_id: str, metrics: list[str], period: str = "day",
                         metric_type: str | None = "total_value", since: int | None = None, until: int | None = None,
                         breakdown: str | None = None) -> dict:
        params: dict[str, Any] = {"metric": ",".join(metrics), "period": period}
        if metric_type:
            params["metric_type"] = metric_type
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        if breakdown:
            params["breakdown"] = breakdown
        return self.get(f"/{ig_user_id}/insights", **params)

    # ---------- Конкуренты ----------
    @staticmethod
    def discovery_fields(username: str, media_limit: int = 25, after: str | None = None) -> str:
        media_part = f"media.after({after}).limit({media_limit})" if after else f"media.limit({media_limit})"
        return f"business_discovery.username({username}){{{DISCOVERY_PROFILE_FIELDS},{media_part}{{{DISCOVERY_MEDIA_FIELDS}}}}}"

    def business_discovery(self, my_ig_user_id: str, username: str, media_limit: int = 25, after: str | None = None) -> dict:
        """Публичные данные другого профессионального аккаунта (только через field expansion)."""
        if not re.fullmatch(r"[a-z0-9._]{1,30}", username):
            raise GraphError(f"Некорректный username: {username!r}")
        return self.get(f"/{my_ig_user_id}", fields=self.discovery_fields(username, media_limit, after)).get("business_discovery", {})


def _raise_if_fatal(r: CallResult) -> None:
    e = r.error or {}
    err = GraphError(e.get("message", "ошибка"), e.get("code"), e.get("subcode"), e.get("http_status"),
                     e.get("type"), e.get("fbtrace_id"))
    if err.kind in {"token", "rate_limit", "network"}:
        raise err


def parse_insights(data: dict) -> dict:
    out = {}
    for item in data.get("data", []):
        name = item.get("name")
        if "total_value" in item:
            out[name] = item["total_value"].get("value")
        elif item.get("values"):
            out[name] = item["values"][-1].get("value")
    return out


# ---------------- OAuth (Facebook Login for Business / Unified Login) ----------------

def oauth_dialog_url(state: str) -> str:
    """URL диалога входа. Новые приложения Meta принимают только config_id (scope отклоняется
    с ошибкой «Invalid Scopes»), поэтому без META_LOGIN_CONFIG_ID вход не выполняется.
    Пароль вводится только на сайте Meta."""
    if not settings.meta_login_config_id:
        raise GraphError("Не задан META_LOGIN_CONFIG_ID (ID конфигурации Facebook Login for Business / Unified Login)")
    params = {
        "client_id": settings.meta_app_id,
        "redirect_uri": settings.meta_redirect_uri,
        "state": state,
        "response_type": "code",
        "config_id": settings.meta_login_config_id,
        "override_default_response_type": "true",
    }
    return f"https://www.facebook.com/{settings.graph_api_version}/dialog/oauth?" + str(httpx.QueryParams(params))


def _token_request(http: httpx.Client, params: dict) -> dict:
    secrets_ = (params.get("client_secret"), params.get("code"), params.get("fb_exchange_token"))
    try:
        r = http.get("/oauth/access_token", params=params)
    except httpx.HTTPError as exc:
        raise GraphError(scrub(f"Сетевая ошибка при обмене токена: {type(exc).__name__}", *secrets_))
    try:
        data = r.json()
    except ValueError:
        raise GraphError(f"Ответ OAuth не в формате JSON (HTTP {r.status_code})", status=r.status_code)
    if r.status_code >= 400 or "error" in data or "access_token" not in data:
        e = data.get("error", {})
        raise GraphError(scrub(e.get("message", f"OAuth HTTP {r.status_code}"), *secrets_),
                         e.get("code"), e.get("error_subcode"), r.status_code, e.get("type"), e.get("fbtrace_id"))
    return data


def exchange_code(code: str, transport: httpx.BaseTransport | None = None) -> dict:
    """code -> токен. Возвращает {access_token, expires_in?, exchanged: bool}."""
    with httpx.Client(base_url=f"{GRAPH}/{settings.graph_api_version}", timeout=30, transport=transport) as http:
        return _token_request(http, {
            "client_id": settings.meta_app_id, "client_secret": settings.meta_app_secret,
            "redirect_uri": settings.meta_redirect_uri, "code": code})


def exchange_long_lived(token: str, transport: httpx.BaseTransport | None = None) -> dict:
    """Короткий user token -> долгоживущий (~60 дней). Для system-user токенов не нужен."""
    with httpx.Client(base_url=f"{GRAPH}/{settings.graph_api_version}", timeout=30, transport=transport) as http:
        return _token_request(http, {
            "grant_type": "fb_exchange_token", "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret, "fb_exchange_token": token})


def debug_token(token: str, transport: httpx.BaseTransport | None = None) -> CallResult:
    """GET /debug_token с app access token (app_id|app_secret).
    Возвращает тип токена, разрешения, сроки и (в granular_scopes) id связанных объектов."""
    app_token = f"{settings.meta_app_id}|{settings.meta_app_secret}"
    client = GraphClient(app_token, transport=transport, app_secret="")
    try:
        r = client.call("/debug_token", input_token=token)
    finally:
        client.close()
    r.params = {"input_token": "***"}
    if r.error:
        r.error["message"] = scrub(r.error["message"], token, settings.meta_app_secret)
    return r


def ig_ids_from_debug(data: dict) -> list[str]:
    """Instagram-аккаунты, к которым выдан instagram_basic (granular_scopes.target_ids)."""
    for g in (data.get("data", {}) or {}).get("granular_scopes", []) or []:
        if g.get("scope") in {"instagram_basic", "instagram_manage_insights"} and g.get("target_ids"):
            return [str(t) for t in g["target_ids"]]
    return []


def check_app_credentials(transport: httpx.BaseTransport | None = None) -> dict:
    """Проверяет, что META_APP_ID и META_APP_SECRET образуют действительную пару.

    GET /{app-id}?fields=id,name с app access token «app_id|app_secret». Неверный секрет
    даёт ошибку OAuth; ответ с тем же id подтверждает App ID. Токены в результат не попадают.
    """
    app_token = f"{settings.meta_app_id}|{settings.meta_app_secret}"
    c = GraphClient(app_token, transport=transport, app_secret="")
    try:
        r = c.call(f"/{settings.meta_app_id}", fields="id,name")
    finally:
        c.close()
    if r.ok and str((r.data or {}).get("id")) == settings.meta_app_id:
        return {"ok": True, "id": r.data["id"], "name": r.data.get("name"), "http_status": r.http_status}
    err = (r.error or {}).get("message") or "ответ не содержит ожидаемый id"
    return {"ok": False, "http_status": r.http_status, "code": (r.error or {}).get("code"),
            "error": scrub(err, settings.meta_app_secret, app_token)}
