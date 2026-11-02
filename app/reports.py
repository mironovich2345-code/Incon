"""Отчёты: детерминированный (по правилам) и контекст для отчёта Claude."""
from __future__ import annotations

import sqlite3

from . import analytics
from .db import own_account, rows


def _f(v, digits: int = 0, suffix: str = "") -> str:
    if v is None:
        return "нет данных"
    return f"{v:,.{digits}f}".replace(",", " ") + suffix


def build_context(conn: sqlite3.Connection) -> dict:
    reels = analytics.own_reels(conn)
    acc = own_account(conn)
    comp_rows = analytics.competitors_table(conn)
    top = analytics.top_reels_all(conn, 15)
    return {
        "own_account": acc["username"] if acc else None,
        "summary": {k: v for k, v in (analytics.account_summary(conn, acc["id"]) if acc else {}).items() if k != "growth"},
        "by_format": analytics.compare_by(reels, "format"),
        "by_topic": analytics.compare_by(reels, "topic"),
        "by_hook": analytics.compare_by(reels, "hook_type"),
        "competitors": [{k: c[k] for k in ("username", "posts", "median", "basis", "n", "outliers", "followers")} for c in comp_rows],
        "top_outliers": [{k: t.get(k) for k in ("username", "permalink", "views", "likes", "comments", "outlier_ratio", "score_basis", "format", "topic", "hook_type", "hook_text")} for t in top],
    }


def rules_report(conn: sqlite3.Connection) -> str:
    ctx = build_context(conn)
    s = ctx["summary"]
    lines = ["# Отчёт по контенту (автоматический, без LLM)", ""]
    lines += ["## Мой аккаунт",
              f"- Reels в базе: {s.get('reels_count', 0)}; с известными просмотрами: {s.get('n_views', 0)}",
              f"- Медиана просмотров: {_f(s.get('median_views'))}",
              f"- Медиана охвата: {_f(s.get('median_reach'))}",
              f"- Медиана среднего времени просмотра: {_f(s.get('median_avg_watch_sec'), 1, ' с')} (n={s.get('n_watch', 0)})",
              f"- Подписки с роликов (сумма известных): {_f(s.get('total_follows_from_reels'))}",
              f"- B2B-заявки (ручной ввод): {_f(s.get('total_leads'))}", ""]
    for title, key in (("форматам", "by_format"), ("темам", "by_topic"), ("типам хуков", "by_hook")):
        lines.append(f"## Сравнение по {title} (медианы)")
        lines.append("| Группа | Роликов | Просмотры | Ср. просмотр, с | Подписки на 1000 | Заявки |")
        lines.append("|---|---|---|---|---|---|")
        for g in ctx[key]:
            lines.append(f"| {g['group']} | {g['count']} | {_f(g['views'])} (n={g['n_views']}) | {_f(g['avg_watch_sec'], 1)} (n={g['n_avg_watch_sec']}) | {_f(g['follows_per_1k'], 1)} (n={g['n_follows_per_1k']}) | {_f(g['leads'])} |")
        small = [g["group"] for g in ctx[key] if g["count"] < 5]
        if small:
            lines.append(f"\n_Малая выборка (< 5 роликов): {', '.join(small)} — выводы как гипотезы._")
        lines.append("")
    lines.append("## Конкуренты")
    for c in ctx["competitors"]:
        lines.append(f"- @{c['username']}: публикаций {c['posts']}, медиана {_f(c['median'])} по «{c['basis']}» (n={c['n']}), выбросов {c['outliers']}")
    lines += ["", "## Топ роликов по кратности к медиане аккаунта"]
    for t in ctx["top_outliers"][:10]:
        lines.append(f"- @{t['username']} ×{t['outlier_ratio']:.1f} — {t.get('format') or '?'} / {t.get('topic') or '?'} / хук: {t.get('hook_type') or '?'} — {t.get('permalink') or ''}")
    unavailable = rows(conn, "SELECT metric, scope, detail FROM metric_availability WHERE status!='available'")
    lines += ["", "## Ограничения данных",
              "- Кривая удержания по секундам и источники трафика через API недоступны.",
              "- У конкурентов недоступны охват, сохранения, репосты и время просмотра."]
    for u in unavailable:
        lines.append(f"- Проверка API: `{u['metric']}` ({u['scope']}) — недоступна: {u['detail'] or ''}")
    return "\n".join(lines)
