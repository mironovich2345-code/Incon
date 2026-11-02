"""Классификация тем, хуков и форматов Reels по тексту (подпись + транскрипт).

Это быстрый детерминированный классификатор на правилах. Он даёт черновую
разметку с указанием «источник = rules»; финальное слово — за ручной правкой
или за Claude (classification_source = manual / claude).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

TOPICS: dict[str, list[str]] = {
    "Управление бизнесом": ["управлен", "собственник", "бизнес", "директор", "стратег", "регламент", "делегир", "kpi", "процесс", "масштаб", "прибыл"],
    "Сотрудники": ["сотрудник", "команд", "найм", "нанят", "увольн", "мотивац", "персонал", "hr", "руководител", "менеджер", "подчинён", "подчинен"],
    "Продажи": ["продаж", "продав", "клиент", "лид", "заявк", "воронк", "конверс", "сделк", "отдел продаж", "скрипт", "crm"],
    "Обучение": ["обучен", "обучить", "курс", "знани", "навык", "адаптац", "онбординг", "тренинг", "наставн", "база знаний"],
    "Автоматизация и ИИ": ["автоматиз", r"ai\b", "нейросет", "chatgpt", "gpt", "claude", "бот", "интеграц", "no-code", "агент", "искусственн"],
}

FORMATS: dict[str, list[str]] = {
    "Список / топ-N": [r"\b\d+\s+(способ|ошиб|прав|шаг|причин|инструмент|вопрос|признак)", r"\bтоп[- ]?\d+"],
    "Ошибки / антипример": [r"ошибк", r"не делайте", r"никогда не", r"главная проблема", r"почему не работает"],
    "Кейс / история": [r"кейс", r"история", r"однажды", r"клиент пришёл", r"клиент пришел", r"было\s*[—-]\s*стало", r"результат"],
    "Инструкция / демонстрация": [r"как (сделать|настроить|внедрить|построить)", r"пошагов", r"показываю", r"смотрите", r"инструкц"],
    "Миф / мнение": [r"миф", r"на самом деле", r"все говорят", r"принято считать", r"непопулярн", r"моё мнение", r"мое мнение"],
    "Ответ на вопрос": [r"меня спрашива", r"частый вопрос", r"вопрос из комментар", r"отвечаю"],
}

HOOKS: dict[str, list[str]] = {
    "Вопрос": [r"\?"],
    "Число / конкретика": [r"\b\d+[%₽$]?", r"\bв \d+ раз"],
    "Негатив / ошибка": [r"ошибк", r"теряете", r"сливаете", r"убивает", r"провал", r"не работает", r"перестаньте"],
    "Контринтуитивное утверждение": [r"на самом деле", r"миф", r"никто не говорит", r"неправ", r"забудьте"],
    "Обещание результата": [r"как .*(увелич|сократ|сэконом|удвоит|вырастит)", r"за \d+ (дн|минут|недел|месяц)"],
    "История": [r"однажды", r"когда я", r"мой клиент", r"история"],
    "Прямое обращение": [r"^\s*(если вы|вы\s|собственник|руководител|предприниматель)"],
}


@dataclass
class Classification:
    topic: str | None
    format: str | None
    hook_type: str | None
    hook_text: str | None
    scores: dict


def _first_sentence(text: str, limit: int = 140) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    m = re.split(r"(?<=[.!?…])\s+|\n", text, maxsplit=1)
    return m[0][:limit].strip() or None


def classify_text(caption: str | None, transcript: str | None = None) -> Classification:
    full = f"{caption or ''}\n{transcript or ''}".lower()
    hook_source = transcript if transcript and transcript.strip() else caption
    hook_text = _first_sentence(hook_source or "")
    hook_lower = (hook_text or "").lower()

    topic_scores = {t: sum(len(re.findall(r"\b" + k, full)) for k in kws) for t, kws in TOPICS.items()}
    # «ии» — подстрока многих слов («линии», «компании»), считаем только отдельным словом
    topic_scores["Автоматизация и ИИ"] += len(re.findall(r"\bии\b", full))
    format_scores = {f: sum(len(re.findall(p, full)) for p in pats) for f, pats in FORMATS.items()}
    hook_scores = {h: sum(len(re.findall(p, hook_lower)) for p in pats) for h, pats in HOOKS.items()}

    def best(scores: dict) -> str | None:
        k, v = max(scores.items(), key=lambda kv: kv[1])
        return k if v > 0 else None

    fmt = best(format_scores) or ("Говорящая голова / экспертное мнение" if full.strip() else None)
    return Classification(
        topic=best(topic_scores),
        format=fmt,
        hook_type=best(hook_scores) or ("Утверждение" if hook_text else None),
        hook_text=hook_text,
        scores={"topic": topic_scores, "format": format_scores, "hook": hook_scores},
    )


TOPIC_LIST = list(TOPICS.keys())
FORMAT_LIST = list(FORMATS.keys()) + ["Говорящая голова / экспертное мнение"]
HOOK_LIST = list(HOOKS.keys()) + ["Утверждение"]
