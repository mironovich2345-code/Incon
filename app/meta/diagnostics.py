"""Диагностика подключения Instagram API.

Проверяет по шагам, что реально доступно этому приложению и этому токену, и сохраняет
HTTP-статусы и ошибки. Токены и секреты в отчёт не попадают: параметры запросов сохраняются
без access_token/appsecret_proof, тексты ошибок проходят через scrub().

Все запросы — только чтение (GET). Ничего не публикуется и не изменяется.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import httpx

from ..config import settings
from ..db import kv_get, rows
from ..metrics import REEL_INSIGHT_METRICS
from .client import (REQUIRED_SCOPES, UNNEEDED_SCOPES, CallResult, GraphClient, debug_token,
                     ig_ids_from_debug, parse_insights, scrub)
from .oauth import active_token, login_token

ACCOUNT_METRICS = ["reach", "views", "accounts_engaged", "total_interactions", "profile_views", "follows_and_unfollows"]


def _step(name: str, r: CallResult | None, ok: bool | None = None, summary: str = "", **extra) -> dict:
    d = {"step": name, "ok": (r.ok if r else False) if ok is None else ok, "summary": summary}
    if r is not None:
        d.update(r.public())
    d.update(extra)
    return d


def run(conn: sqlite3.Connection, transport: httpx.BaseTransport | None = None, competitors_limit: int = 5) -> dict:
    steps: list[dict] = []
    report = {"started_at": datetime.now().isoformat(timespec="seconds"), "graph_api_version": settings.graph_api_version,
              "token_type": kv_get(conn, "token_type") or None, "steps": steps}

    problems = settings.meta_config_problems()
    steps.append({"step": "Конфигурация сервера", "ok": not problems, "summary": "; ".join(problems) or "ok",
                  "redirect_uri": settings.meta_redirect_uri, "config_id_set": bool(settings.meta_login_config_id)})

    token, provider = active_token(conn)
    ltoken = login_token(conn)
    if not token:
        steps.append({"step": "Токен", "ok": False, "summary": "Instagram не подключён — выполните вход через Meta"})
        return _finish(conn, report)

    # 1. debug_token
    if ltoken and settings.meta_app_id and settings.meta_app_secret:
        r = debug_token(ltoken, transport=transport)
        d = (r.data or {}).get("data", {}) if r.ok else {}
        scopes = d.get("scopes") or []
        steps.append(_step("debug_token (токен входа)", r, ok=r.ok and bool(d.get("is_valid")),
                           summary=f"type={d.get('type')}, valid={d.get('is_valid')}, expires_at={d.get('expires_at') or 'нет'}, "
                                   f"data_access_expires_at={d.get('data_access_expires_at') or 'нет'}",
                           scopes=scopes,
                           missing_scopes=[s for s in REQUIRED_SCOPES if s not in scopes],
                           unneeded_scopes=[s for s in scopes if s in UNNEEDED_SCOPES],
                           granular_ig_ids=ig_ids_from_debug(r.data or {})))

    ig_id = kv_get(conn, "ig_user_id")
    with GraphClient(token, transport=transport) as c:
        # 2. /me
        r = c.call("/me", fields="id,name")
        steps.append(_step("Идентичность токена (/me)", r, summary=f"токен: {provider}"))

        # 3. /me/accounts — только для токена входа
        if ltoken:
            with GraphClient(ltoken, transport=transport) as lc:
                r = lc.call("/me/accounts", fields="id,name,instagram_business_account{id,username}", limit=50)
            pages = (r.data or {}).get("data", []) if r.ok else []
            linked = [p for p in pages if p.get("instagram_business_account")]
            steps.append(_step("Страницы с Instagram (/me/accounts)", r,
                               summary=f"страниц: {len(pages)}, со связанным Instagram: {len(linked)}"))

        if not ig_id:
            steps.append({"step": "Instagram-аккаунт", "ok": False, "summary": "ig_user_id не определён при входе"})
            return _finish(conn, report)

        # 4. Профиль
        r = c.call(f"/{ig_id}", fields="id,username,name,followers_count,follows_count,media_count")
        p = r.data or {}
        steps.append(_step("Профиль Instagram", r, summary=f"@{p.get('username')}, подписчиков {p.get('followers_count')}, публикаций {p.get('media_count')}"))
        my_username = p.get("username")

        # 5. Медиа и Reels
        r = c.call(f"/{ig_id}/media", fields="id,media_type,media_product_type,timestamp,permalink", limit=50)
        media = (r.data or {}).get("data", []) if r.ok else []
        reels = [m for m in media if m.get("media_product_type") == "REELS"]
        steps.append(_step("Список публикаций и Reels", r, summary=f"публикаций на 1-й странице: {len(media)}, из них Reels: {len(reels)}"))

        # 6. Insights Reels — по одной метрике, чтобы видеть доступность каждой
        if reels:
            reel = reels[0]
            for m in REEL_INSIGHT_METRICS:
                r = c.call(f"/{reel['id']}/insights", metric=m)
                val = parse_insights(r.data).get(m) if r.ok else None
                steps.append(_step(f"Insights Reels: {m}", r, summary=f"значение для последнего Reels: {val}" if r.ok else "недоступна",
                                   metric=m, scope="own_reel"))
        else:
            steps.append({"step": "Insights Reels", "ok": None, "summary": "Нет Reels на первой странице — проверить нечем"})

        # 7. Insights аккаунта
        for m in ACCOUNT_METRICS:
            r = c.call(f"/{ig_id}/insights", metric=m, period="day", metric_type="total_value")
            val = parse_insights(r.data).get(m) if r.ok else None
            steps.append(_step(f"Insights аккаунта: {m}", r, summary=f"значение за день: {val}" if r.ok else "недоступна",
                               metric=m, scope="own_account"))

        # 8. Business Discovery: сначала на себе (проверка самого метода), затем конкуренты
        targets = ([my_username] if my_username else []) + [
            a["username"] for a in rows(conn, "SELECT username FROM accounts WHERE is_own=0 AND tracked=1 ORDER BY id")
        ][:competitors_limit]
        for i, u in enumerate(targets):
            r = c.call(f"/{ig_id}", fields=GraphClient.discovery_fields(u, media_limit=5))
            bd = (r.data or {}).get("business_discovery", {}) if r.ok else {}
            items = bd.get("media", {}).get("data", [])
            has_views = any("view_count" in m for m in items)
            has_likes = any("like_count" in m for m in items)
            label = "свой аккаунт (проверка метода)" if i == 0 and u == my_username else f"@{u}"
            steps.append(_step(f"Business Discovery: {label}", r,
                               summary=(f"подписчиков {bd.get('followers_count')}, медиа получено {len(items)}, "
                                        f"view_count {'есть' if has_views else 'нет'}, like_count {'есть' if has_likes else 'нет'}")
                               if r.ok else "недоступно",
                               metric="view_count", scope="competitor_media", has_view_count=has_views if r.ok else None))
    return _finish(conn, report)


CAPABILITY_STEPS = {
    "me_accounts": lambda n: n.startswith("Страницы с Instagram"),
    "profile": lambda n: n == "Профиль Instagram",
    "media": lambda n: n.startswith("Список публикаций"),
    "reel_insights": lambda n: n.startswith("Insights Reels:"),
    "account_insights": lambda n: n.startswith("Insights аккаунта:"),
    "business_discovery_self": lambda n: n.startswith("Business Discovery: свой"),
    "business_discovery_competitors": lambda n: n.startswith("Business Discovery: @"),
}


def capabilities(steps: list[dict]) -> dict:
    """Какие методы реально работают с текущим токеном (USER и SYSTEM_USER могут отличаться).
    Значение: True/False — все/ни один запрос группы успешен, "partial" — часть, None — не проверялось."""
    out = {}
    for cap, match in CAPABILITY_STEPS.items():
        res = [s.get("ok") for s in steps if match(s.get("step", "")) and s.get("http_status") is not None]
        out[cap] = None if not res else (True if all(res) else (False if not any(res) else "partial"))
    return out


def _finish(conn: sqlite3.Connection, report: dict) -> dict:
    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    report["capabilities"] = capabilities(report["steps"])
    report["ok"] = all(s.get("ok") is not False for s in report["steps"])
    # Обновить каталог доступности метрик по результатам реальных запросов
    for s in report["steps"]:
        if s.get("metric") and s.get("scope") and s.get("http_status") is not None:
            ok = s["ok"] and (s.get("has_view_count") is not False)
            detail = None if ok else ((s.get("error") or {}).get("message") or "поле не вернулось")
            conn.execute(
                """INSERT INTO metric_availability(metric, scope, status, detail, checked_at) VALUES (?,?,?,?, datetime('now'))
                   ON CONFLICT(metric, scope) DO UPDATE SET status=excluded.status, detail=excluded.detail, checked_at=excluded.checked_at""",
                (s["metric"], s["scope"], "available" if ok else "unavailable", scrub(detail or "")[:300] or None))
    payload = json.dumps(report, ensure_ascii=False, default=str)
    conn.execute("INSERT INTO diagnostic_runs(report_json, ok) VALUES (?, ?)", (payload, 1 if report["ok"] else 0))
    conn.commit()
    return report


def last_report(conn: sqlite3.Connection) -> dict | None:
    r = conn.execute("SELECT report_json FROM diagnostic_runs ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(r["report_json"]) if r else None
