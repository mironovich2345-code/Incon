"""Синхронизация данных из Graph API в локальную БД."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from .. import classify
from ..db import get_or_create_account, kv_get
from ..importers import parse_datetime, record_followers, upsert_media
from ..metrics import API_TO_COLUMN, REEL_INSIGHT_METRICS
from .client import GraphClient, GraphError


def _set_availability(conn: sqlite3.Connection, metric: str, scope: str, status: str, detail: str | None = None) -> None:
    conn.execute(
        """INSERT INTO metric_availability(metric, scope, status, detail, checked_at) VALUES (?,?,?,?, datetime('now'))
           ON CONFLICT(metric, scope) DO UPDATE SET status=excluded.status, detail=excluded.detail, checked_at=excluded.checked_at""",
        (metric, scope, status, detail),
    )


def client_from_store(conn: sqlite3.Connection) -> tuple[GraphClient, str]:
    from .oauth import active_token
    ig_user_id = kv_get(conn, "ig_user_id")
    token, _provider = active_token(conn)
    if not token or not ig_user_id:
        raise GraphError("Instagram не подключён. Выполните вход через Meta на странице «API».")
    if kv_get(conn, "reauth_required"):
        raise GraphError("Нужен повторный вход через Meta: " + (kv_get(conn, "reauth_required") or ""), code=190)
    return GraphClient(token), ig_user_id


def sync_own(conn: sqlite3.Connection, client: GraphClient, ig_user_id: str) -> dict:
    profile = client.ig_user(ig_user_id)
    acc_id = get_or_create_account(conn, profile["username"], is_own=True)
    conn.execute("UPDATE accounts SET ig_user_id=?, name=? WHERE id=?", (ig_user_id, profile.get("name"), acc_id))
    record_followers(conn, acc_id, profile.get("followers_count"), "api",
                     follows_count=profile.get("follows_count"), media_count=profile.get("media_count"))
    media = [m for m in client.list_media(ig_user_id) if m.get("media_product_type") == "REELS"]
    unavailable_all: dict[str, str] = {}
    for m in media:
        values, unavailable = client.media_insights(m["id"])
        unavailable_all.update(unavailable)
        rec = {
            "ig_media_id": m["id"], "permalink": m.get("permalink"), "caption": m.get("caption"),
            "media_type": m.get("media_type"), "media_product_type": m.get("media_product_type"),
            "published_at": parse_datetime(m.get("timestamp")),
        }
        for api_name, col in API_TO_COLUMN.items():
            if api_name in values:
                rec[col] = values[api_name]
        c = classify.classify_text(m.get("caption"))
        existing = conn.execute("SELECT classification_source FROM media WHERE account_id=? AND ig_media_id=?", (acc_id, m["id"])).fetchone()
        if not existing or (existing["classification_source"] or "rules") == "rules":
            rec.update({"format": c.format, "topic": c.topic, "hook_type": c.hook_type, "hook_text": c.hook_text,
                        "classification_source": "rules"})
        media_id = upsert_media(conn, acc_id, rec, "api")
        conn.execute("INSERT INTO media_snapshots(media_id, captured_at, views, likes, comments, source) VALUES (?,?,?,?,?, 'api')",
                     (media_id, datetime.now().isoformat(timespec="seconds"), rec.get("views"), rec.get("likes"), rec.get("comments")))
    for metric in REEL_INSIGHT_METRICS:
        if metric in unavailable_all:
            _set_availability(conn, metric, "own_reel", "unavailable", unavailable_all[metric][:300])
        elif media:
            _set_availability(conn, metric, "own_reel", "available")
    conn.commit()
    return {"reels": len(media), "unavailable": unavailable_all}


def sync_competitor(conn: sqlite3.Connection, client: GraphClient, my_ig_user_id: str, username: str, pages: int = 4) -> dict:
    acc_id = get_or_create_account(conn, username)
    after, total = None, 0
    try:
        for _ in range(pages):
            bd = client.business_discovery(my_ig_user_id, username, after=after)
            if total == 0:
                conn.execute("UPDATE accounts SET discovery_status='ok', discovery_detail=NULL, name=?, biography=? WHERE id=?",
                             (bd.get("name"), bd.get("biography"), acc_id))
                record_followers(conn, acc_id, bd.get("followers_count"), "business_discovery",
                                 follows_count=bd.get("follows_count"), media_count=bd.get("media_count"))
            items = bd.get("media", {}).get("data", [])
            saw_views = False
            for m in items:
                saw_views = saw_views or ("view_count" in m)
                c = classify.classify_text(m.get("caption"))
                rec = {
                    "ig_media_id": m["id"], "permalink": m.get("permalink"), "caption": m.get("caption"),
                    "media_type": m.get("media_type"), "media_product_type": m.get("media_product_type"),
                    "published_at": parse_datetime(m.get("timestamp")),
                    "likes": m.get("like_count"), "comments": m.get("comments_count"), "views": m.get("view_count"),
                }
                existing = conn.execute("SELECT classification_source FROM media WHERE account_id=? AND ig_media_id=?", (acc_id, m["id"])).fetchone()
                if not existing or (existing["classification_source"] or "rules") == "rules":
                    rec.update({"format": c.format, "topic": c.topic, "hook_type": c.hook_type, "hook_text": c.hook_text,
                                "classification_source": "rules"})
                media_id = upsert_media(conn, acc_id, rec, "business_discovery")
                conn.execute("INSERT INTO media_snapshots(media_id, captured_at, views, likes, comments, source) VALUES (?,?,?,?,?, 'business_discovery')",
                             (media_id, datetime.now().isoformat(timespec="seconds"), rec["views"], rec["likes"], rec["comments"]))
            total += len(items)
            if items:
                _set_availability(conn, "view_count", "competitor_media", "available" if saw_views else "unavailable",
                                  None if saw_views else "Поле не вернулось в ответе business_discovery")
            after = bd.get("media", {}).get("paging", {}).get("cursors", {}).get("after")
            if not after or not items:
                break
    except GraphError as exc:
        if exc.kind in {"token", "rate_limit", "network"}:
            raise  # общая проблема, а не свойство конкретного конкурента
        # Business Discovery возвращает ошибку для личных, несуществующих и недоступных аккаунтов;
        # различить эти случаи по ответу надёжно нельзя, поэтому статус — «недоступен» с текстом ошибки.
        status = "not_professional" if exc.code == 110 else "error"
        conn.execute("UPDATE accounts SET discovery_status=?, discovery_detail=? WHERE id=?",
                     (status, f"HTTP {exc.status}, code {exc.code}: {exc}"[:300], acc_id))
        conn.commit()
        return {"ok": False, "error": str(exc), "media": total}
    conn.commit()
    return {"ok": True, "media": total}
