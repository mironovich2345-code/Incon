"""Аналитика собственного аккаунта и конкурентов.

Все агрегаты считаются только по известным значениям. В каждом агрегате
возвращается n (сколько роликов реально имеют эту метрику), чтобы было видно,
на какой выборке основан вывод.
"""
from __future__ import annotations

import sqlite3
from statistics import median
from typing import Iterable

from .config import settings
from .db import rows


def _known(values: Iterable) -> list[float]:
    return [float(v) for v in values if v is not None]


def med(values: Iterable) -> float | None:
    k = _known(values)
    return median(k) if k else None


def mad(values: list[float]) -> float | None:
    if not values:
        return None
    m = median(values)
    return median([abs(v - m) for v in values])


def safe_rate(num, den, per: float = 1.0) -> float | None:
    if num is None or den in (None, 0):
        return None
    return num / den * per


def enrich(r: dict) -> dict:
    """Производные показатели. Если исходной метрики нет — производной тоже нет."""
    r = dict(r)
    # Публичное вовлечение: только лайки + комментарии и только если известны оба.
    r["engagement"] = (r["likes"] + r["comments"]) if r.get("likes") is not None and r.get("comments") is not None else None
    r["follows_per_1k"] = safe_rate(r.get("follows"), r.get("views"), 1000)
    r["shares_per_1k"] = safe_rate(r.get("shares"), r.get("views"), 1000)
    r["saves_per_1k"] = safe_rate(r.get("saved"), r.get("views"), 1000)
    r["leads_per_1k"] = safe_rate(r.get("leads"), r.get("views"), 1000)
    avg = r.get("avg_watch_time_ms")
    dur = r.get("duration_sec")
    r["avg_watch_sec"] = avg / 1000 if avg is not None else None
    r["watch_ratio"] = (avg / 1000 / dur) if avg is not None and dur else None
    return r


def own_reels(conn: sqlite3.Connection, order: str = "views") -> list[dict]:
    allowed = {"views", "reach", "published_at", "follows", "shares", "saved", "avg_watch_time_ms", "leads"}
    order = order if order in allowed else "views"
    data = rows(conn, f"""SELECT m.* FROM media m JOIN accounts a ON a.id=m.account_id
                         WHERE a.is_own=1 ORDER BY m.{order} IS NULL, m.{order} DESC""")
    return [enrich(r) for r in data]


GROUP_METRICS = ["views", "reach", "avg_watch_sec", "watch_ratio", "follows_per_1k", "shares_per_1k", "saves_per_1k", "leads"]


def compare_by(reels: list[dict], field: str) -> list[dict]:
    """Сравнение групп (формат/тема/хук/дата) по медианам. n_* — размер выборки по метрике."""
    groups: dict[str, list[dict]] = {}
    for r in reels:
        key = r.get(field) or "—не размечено—"
        if field == "published_at":
            key = (r.get("published_at") or "")[:10] or "—без даты—"
        groups.setdefault(key, []).append(r)
    out = []
    for key, items in groups.items():
        row = {"group": key, "count": len(items)}
        for m in GROUP_METRICS:
            vals = _known(i.get(m) for i in items)
            row[m] = median(vals) if vals else None
            row[f"n_{m}"] = len(vals)
        out.append(row)
    out.sort(key=lambda r: (r["views"] is None, -(r["views"] or 0)))
    return out


def follower_growth(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    snaps = rows(conn, """SELECT captured_on, MAX(followers_count) AS followers FROM account_snapshots
                          WHERE account_id=? AND followers_count IS NOT NULL GROUP BY captured_on ORDER BY captured_on""", (account_id,))
    daily = {r["day"]: r for r in rows(conn, "SELECT day, follows, unfollows FROM account_daily WHERE account_id=?", (account_id,))}
    prev = None
    for s in snaps:
        s["delta"] = s["followers"] - prev if prev is not None else None
        d = daily.get(s["captured_on"], {})
        s["follows"] = d.get("follows")
        s["unfollows"] = d.get("unfollows")
        prev = s["followers"]
    return snaps


def account_summary(conn: sqlite3.Connection, account_id: int) -> dict:
    reels = [r for r in own_reels(conn)]
    growth = follower_growth(conn, account_id)
    return {
        "reels_count": len(reels),
        "median_views": med(r["views"] for r in reels),
        "n_views": len(_known(r["views"] for r in reels)),
        "median_reach": med(r["reach"] for r in reels),
        "median_avg_watch_sec": med(r["avg_watch_sec"] for r in reels),
        "n_watch": len(_known(r["avg_watch_sec"] for r in reels)),
        "total_follows_from_reels": sum(_known(r["follows"] for r in reels)) if _known(r["follows"] for r in reels) else None,
        "total_leads": sum(_known(r["leads"] for r in reels)) if _known(r["leads"] for r in reels) else None,
        "followers_now": growth[-1]["followers"] if growth else None,
        "followers_first": growth[0]["followers"] if growth else None,
        "growth": growth,
    }


# ---------------- Конкуренты ----------------

def competitor_media(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    return [enrich(r) for r in rows(conn, "SELECT * FROM media WHERE account_id=? ORDER BY published_at DESC", (account_id,))]


def score_outliers(items: list[dict], ratio: float | None = None) -> dict:
    """Медиана просмотров аккаунта и выбросы.

    Основная метрика — views. Если просмотров нет ни у одной публикации,
    используется прокси «лайки + комментарии», и это явно помечается.
    """
    ratio = ratio or settings.outlier_ratio
    views = _known(i.get("views") for i in items)
    if views:
        basis, key = "views", "views"
        values = views
    else:
        basis, key = "likes+comments (прокси: просмотры недоступны)", "engagement"
        values = _known(i.get("engagement") for i in items)
    m = median(values) if values else None
    d = mad(values)
    for i in items:
        v = i.get(key)
        i["score_basis"] = basis
        i["outlier_ratio"] = (v / m) if v is not None and m else None
        i["robust_z"] = ((v - m) / (1.4826 * d)) if v is not None and m is not None and d else None
        i["is_outlier"] = bool(i["outlier_ratio"] and i["outlier_ratio"] >= ratio)
    return {"basis": basis, "median": m, "n": len(values), "ratio_threshold": ratio, "mad": d}


def competitors_table(conn: sqlite3.Connection) -> list[dict]:
    accs = rows(conn, "SELECT * FROM accounts WHERE is_own=0 ORDER BY username")
    out = []
    for a in accs:
        items = competitor_media(conn, a["id"])
        stats = score_outliers(items)
        snap = conn.execute("SELECT followers_count, media_count, captured_on FROM account_snapshots WHERE account_id=? ORDER BY captured_on DESC LIMIT 1", (a["id"],)).fetchone()
        a.update({
            "posts": len(items),
            "median": stats["median"],
            "basis": stats["basis"],
            "n": stats["n"],
            "outliers": sum(1 for i in items if i["is_outlier"]),
            "followers": snap["followers_count"] if snap else None,
            "followers_on": snap["captured_on"] if snap else None,
            "last_post": items[0]["published_at"] if items else None,
        })
        out.append(a)
    return out


def top_reels_all(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Рейтинг роликов по кратности к медиане своего аккаунта (сравнимо между аккаунтами разного размера)."""
    accs = rows(conn, "SELECT id, username, is_own FROM accounts")
    pool = []
    for a in accs:
        items = competitor_media(conn, a["id"])
        score_outliers(items)
        for i in items:
            i["username"] = a["username"]
            i["is_own"] = a["is_own"]
        pool.extend(i for i in items if i.get("outlier_ratio") is not None)
    pool.sort(key=lambda i: -i["outlier_ratio"])
    return pool[:limit]


def pattern_summary(items: list[dict]) -> dict[str, list[dict]]:
    """Какие темы/форматы/хуки чаще встречаются среди выбросов, чем в среднем."""
    out = {}
    total = len(items) or 1
    outl = [i for i in items if i.get("is_outlier")]
    for field in ("format", "topic", "hook_type"):
        counts: dict[str, list[int]] = {}
        for i in items:
            k = i.get(field) or "—"
            counts.setdefault(k, [0, 0])[0] += 1
        for i in outl:
            k = i.get(field) or "—"
            counts[k][1] += 1
        res = []
        for k, (n_all, n_out) in counts.items():
            share_all = n_all / total
            share_out = n_out / len(outl) if outl else None
            res.append({"value": k, "n": n_all, "n_outliers": n_out,
                        "lift": (share_out / share_all) if share_out is not None and share_all else None})
        res.sort(key=lambda r: (-(r["lift"] or 0), -r["n"]))
        out[field] = res
    return out
