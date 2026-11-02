"""Контент-план эксперимента: 25 Reels за 5 дней.

Схема — три взаимно ортогональных латинских квадрата 5×5
(значение = (k·день + слот) mod 5, k = 1, 2, 3 для формата, темы и хука):
  * каждый формат, тема и тип хука встречается ровно 1 раз в каждый день
    и ровно 1 раз в каждом слоте (времени публикации);
  * любые две характеристики ортогональны: каждая пара «формат × тема»,
    «формат × хук», «тема × хук» встречается ровно один раз.
Так влияние формата, темы и хука не смешивается ни друг с другом, ни с днём
и временем публикации.

Важно: при 208 подписчиках разброс просмотров большой, 5 роликов на группу —
малая выборка. Выводы трактуем как гипотезы для следующей итерации.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

PLAN_TOPICS = ["Управление бизнесом", "Сотрудники", "Продажи", "Обучение", "Автоматизация и ИИ"]
PLAN_FORMATS = [
    "Говорящая голова / экспертное мнение",
    "Список / топ-N",
    "Кейс / история",
    "Инструкция / демонстрация",
    "Миф / мнение",
]
PLAN_HOOKS = ["Вопрос", "Негатив / ошибка", "Число / конкретика", "Контринтуитивное утверждение", "Обещание результата"]


def latin_plan(start: date, days: int = 5, per_day: int = 5) -> list[dict]:
    n = 5
    assert days <= n and per_day <= n
    plan = []
    for d in range(days):
        for s in range(per_day):
            plan.append({
                "day": (start + timedelta(days=d)).isoformat(),
                "slot": s + 1,
                "format": PLAN_FORMATS[(d + s) % n],
                "topic": PLAN_TOPICS[(2 * d + s) % n],
                "hook_type": PLAN_HOOKS[(3 * d + s) % n],
            })
    return plan


def create_plan(conn: sqlite3.Connection, start: date, replace: bool = False) -> int:
    items = latin_plan(start)
    if replace:
        conn.execute("DELETE FROM content_plan WHERE day BETWEEN ? AND ?", (items[0]["day"], items[-1]["day"]))
    n = 0
    for it in items:
        cur = conn.execute(
            "INSERT OR IGNORE INTO content_plan(day, slot, topic, format, hook_type, title) VALUES (?,?,?,?,?,?)",
            (it["day"], it["slot"], it["topic"], it["format"], it["hook_type"], f"{it['topic']}: {it['format']}"),
        )
        n += cur.rowcount
    conn.commit()
    return n
