"""Клиент Instagram Graph API (вариант «Instagram API with Facebook Login for Business»).

Только чтение: профиль, медиа, insights, business_discovery.
Методов публикации здесь нет намеренно (POST /media, /media_publish не реализованы).

Хост: graph.facebook.com/{version}. Токен передаётся только в запросе к Meta,
не логируется и вырезается из текстов ошибок.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from ..config import settings
from ..metrics import REEL_INSIGHT_METRICS

GRAPH = "https://graph.facebook.com"

MEDIA_FIELDS = "id,caption,media_type,media_product_type,permalink,shortcode,timestamp,like_count,comments_count,thumbnail_url"
DISCOVERY_PROFILE_FIELDS = "username,name,biography,followers_count,follows_count,media_count,website"
DISCOVERY_MEDIA_FIELDS = "id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count,view_count"


class GraphError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, subcode: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code, self.subcode, self.status = code, subcode, status


def _scrub(text: str) -> str:
    return re.sub(r"access_token=[^&\s\"']+", "access_token=***", text)


class GraphClient:
    def __init__(self, token: str, version: str | None = None, transport: httpx.BaseTransport | None = None):
        self._token = token
        self.version = version or settings.graph_api_version
        self._http = httpx.Client(base_url=f"{GRAPH}/{self.version}", timeout=30, transport=transport)

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, **params: Any) -> dict:
        params["access_token"] = self._token
        resp = self._http.get(path, params=params)
        try:
            data = resp.json()
        except ValueError:
            raise GraphError(_scrub(f"Неожиданный ответ {resp.status_code}"), status=resp.status_code)
        if resp.status_code >= 400 or "error" in data:
            err = data.get("error", {})
            raise GraphError(_scrub(err.get("message", f"HTTP {resp.status_code}")), err.get("code"),
                             err.get("error_subcode"), resp.status_code)
        return data

    # ---------- Свой аккаунт ----------
    def pages_with_instagram(self) -> list[dict]:
        data = self.get("/me/accounts", fields="id,name,access_token,instagram_business_account{id,username}")
        return [p for p in data.get("data", []) if p.get("instagram_business_account")]

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
        """Запрашивает метрики. При ошибке пакетного запроса — по одной, чтобы понять,
        какая именно недоступна. Возвращает (значения, {метрика: причина недоступности})."""
        metrics = metrics or REEL_INSIGHT_METRICS
        try:
            return self._parse_insights(self.get(f"/{media_id}/insights", metric=",".join(metrics))), {}
        except GraphError:
            pass
        values, unavailable = {}, {}
        for m in metrics:
            try:
                values.update(self._parse_insights(self.get(f"/{media_id}/insights", metric=m)))
            except GraphError as exc:
                unavailable[m] = str(exc)
        return values, unavailable

    @staticmethod
    def _parse_insights(data: dict) -> dict:
        out = {}
        for item in data.get("data", []):
            name = item.get("name")
            if "total_value" in item:
                out[name] = item["total_value"].get("value")
            elif item.get("values"):
                out[name] = item["values"][-1].get("value")
        return out

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
    def business_discovery(self, my_ig_user_id: str, username: str, media_limit: int = 25, after: str | None = None) -> dict:
        """Публичные данные другого профессионального аккаунта.
        Пагинация медиа — через курсор after внутри field expansion."""
        media_part = f"media.limit({media_limit})"
        if after:
            media_part = f"media.after({after}).limit({media_limit})"
        fields = f"business_discovery.username({username}){{{DISCOVERY_PROFILE_FIELDS},{media_part}{{{DISCOVERY_MEDIA_FIELDS}}}}}"
        return self.get(f"/{my_ig_user_id}", fields=fields).get("business_discovery", {})


def oauth_dialog_url(state: str) -> str:
    """URL диалога Facebook Login for Business. Пароль вводится только на сайте Meta."""
    params = {
        "client_id": settings.meta_app_id,
        "redirect_uri": settings.meta_redirect_uri,
        "state": state,
        "response_type": "code",
    }
    if settings.meta_login_config_id:
        params["config_id"] = settings.meta_login_config_id
    else:
        params["scope"] = ",".join(REQUIRED_SCOPES)
    return f"https://www.facebook.com/{settings.graph_api_version}/dialog/oauth?" + httpx.QueryParams(params).__str__()


# Минимальный набор разрешений для чтения. instagram_content_publish НЕ запрашиваем.
REQUIRED_SCOPES = [
    "instagram_basic",
    "instagram_manage_insights",
    "pages_show_list",
    "pages_read_engagement",
    "business_management",
]


def exchange_code(code: str, transport: httpx.BaseTransport | None = None) -> dict:
    """code -> короткий user token -> долгоживущий (~60 дней) user token."""
    with httpx.Client(base_url=f"{GRAPH}/{settings.graph_api_version}", timeout=30, transport=transport) as http:
        r = http.get("/oauth/access_token", params={
            "client_id": settings.meta_app_id, "client_secret": settings.meta_app_secret,
            "redirect_uri": settings.meta_redirect_uri, "code": code})
        short = r.json()
        if "error" in short:
            raise GraphError(_scrub(short["error"].get("message", "OAuth error")))
        r = http.get("/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret, "fb_exchange_token": short["access_token"]})
        long = r.json()
        if "error" in long:
            raise GraphError(_scrub(long["error"].get("message", "OAuth error")))
        return long  # {access_token, token_type, expires_in}
