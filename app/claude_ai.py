"""Интеграция с Claude: разбор референсов, сценарии, раскадровки, отчёты.

Если ключ Anthropic не настроен, функции возвращают готовый промпт — его можно
вставить в Claude вручную (или передать Claude Code в этой же сессии).
Все промпты требуют: не выдумывать метрики, явно отмечать отсутствующие данные,
не копировать чужие сценарии, а брать только приёмы и структуру.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from .config import settings

SYSTEM = """Ты — аналитик и сценарист экспертного Instagram в нише B2B: управление бизнесом, сотрудники, продажи, обучение, автоматизация и ИИ.
Аудитория — собственники и руководители малого и среднего бизнеса. Цель контента — просмотры, подписки и B2B-заявки.

Жёсткие правила:
1. Используй только переданные данные. Если метрики нет (null / «нет данных»), так и пиши — не оценивай, не придумывай, не подставляй ноль.
2. Указывай размер выборки рядом с выводом; при n < 5 помечай вывод как гипотезу.
3. Из чужих роликов бери только приёмы: структуру, темп, тип хука, монтажные решения. Не копируй текст, формулировки, сюжеты и визуальный стиль конкретного автора.
4. Пиши по-русски, конкретно, без воды."""


def claude_available() -> bool:
    if not settings.claude_enabled:
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _image_block(path: str) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(p.read_bytes()).decode()}}


def ask(prompt: str, images: list[str] | None = None) -> dict:
    """Вызов Claude. Возвращает {'ok', 'text', 'prompt', 'error'}."""
    if not claude_available():
        return {"ok": False, "text": None, "prompt": prompt,
                "error": "Claude API не настроен (ANTHROPIC_API_KEY). Скопируйте промпт и выполните вручную."}
    import anthropic

    content: list[dict] = []
    for img in (images or [])[:20]:
        block = _image_block(img)
        if block:
            content.append(block)
    content.append({"type": "text", "text": prompt})
    client = anthropic.Anthropic()
    try:
        with client.beta.messages.stream(
            model=settings.anthropic_model,
            max_tokens=32000,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            # Серверный фолбэк на случай отказа классификатора безопасности.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": content}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.RateLimitError as exc:
        return {"ok": False, "text": None, "prompt": prompt, "error": f"Лимит запросов Claude: {exc}"}
    except anthropic.APIStatusError as exc:
        return {"ok": False, "text": None, "prompt": prompt, "error": f"Ошибка Claude API {exc.status_code}: {exc.message}"}
    except anthropic.APIConnectionError as exc:
        return {"ok": False, "text": None, "prompt": prompt, "error": f"Нет соединения с Claude API: {exc}"}
    if msg.stop_reason == "refusal":
        return {"ok": False, "text": None, "prompt": prompt, "error": "Claude отклонил запрос."}
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    return {"ok": True, "text": text, "prompt": prompt, "error": None}


def _j(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)


def reference_analysis_prompt(ref: dict) -> str:
    kf = json.loads(ref.get("keyframes") or "[]")
    return f"""Разбери видео-референс «{ref['title']}» (автор: {ref.get('author') or 'не указан'}).
Приложены ключевые кадры по порядку (таймкоды: {', '.join(str(k['t']) + 's/' + k['kind'] for k in kf)}).

Автоматические измерения (null = не измерено):
{_j(json.loads(ref.get('structure') or '{}'))}

Транскрипт (источник: {ref.get('transcript_source')}):
{ref.get('transcript') or 'нет транскрипта'}

Сделай разбор:
1. Хук (0–3 с): что именно удерживает — текст, визуал, действие. Тип хука.
2. Структура по битам с таймкодами: проблема → напряжение → решение → CTA (или иная).
3. Монтажные приёмы: темп склеек, смена планов, текст на экране, B-roll, зум, субтитры — только то, что видно на кадрах; невидимое помечай «не видно на кадрах».
4. Почему это может работать для B2B-аудитории.
5. 3 приёма, которые можно перенести в мои ролики без копирования содержания.
"""


def script_prompt(topic: str, fmt: str, hook_type: str, refs: list[dict], insights: str, duration: int = 40) -> str:
    ref_part = "\n\n".join(
        f"Референс «{r['title']}»: структура {_j(json.loads(r.get('structure') or '{}'))}\nРазбор: {(r.get('analysis_md') or '')[:2500]}"
        for r in refs
    ) or "Референсы не выбраны."
    return f"""Напиши оригинальный сценарий Reels для моего экспертного аккаунта.
Тема: {topic}. Формат: {fmt}. Тип хука: {hook_type}. Длительность: ~{duration} с.
Я снимаю сам на телефон, один в кадре; монтаж делает Claude (нарезка, субтитры, B-roll-вставки, текст на экране).

Что уже известно по моему аккаунту (не додумывай сверх этого):
{insights}

Приёмы из референсов (переноси приёмы, не текст):
{ref_part}

Верни:
1. 3 варианта хука (первые 1–2 секунды, текст + что в кадре).
2. Сценарий по битам с таймкодами: реплика, что в кадре, текст на экране.
3. Раскадровку в JSON-массиве: [{{"t_start":0,"t_end":2,"shot":"крупный план","action":"...","speech":"...","on_screen_text":"...","edit":"склейка/зум/B-roll"}}].
4. CTA, ведущий к B2B-заявке (без агрессивных продаж).
5. Подпись к публикации (до 600 знаков) и чек-лист съёмки.
"""


def report_prompt(context: dict) -> str:
    return f"""Подготовь отчёт и рекомендации по контенту на следующие 5 дней.
Данные (null = недоступно; n = размер выборки):
{_j(context)}

Структура отчёта:
1. Что сработало у меня (формат/тема/хук) — с цифрами и n.
2. Что работает у конкурентов: ролики-выбросы и их общие паттерны.
3. Какие метрики недоступны и как это ограничивает выводы.
4. 5 конкретных рекомендаций на следующую серию + какие гипотезы проверить.
"""


def template_script(topic: str, fmt: str, hook_type: str, duration: int = 40) -> dict:
    """Шаблон сценария без LLM: каркас битов с плейсхолдерами — текст пишете вы или Claude."""
    hooks = {
        "Вопрос": f"Почему [проблема в теме «{topic}»] у вас до сих пор не решена?",
        "Негатив / ошибка": f"Главная ошибка собственника в теме «{topic}» — [ошибка].",
        "Число / конкретика": f"[N] [шагов/ошибок/инструментов] в теме «{topic}», которые [результат].",
        "Контринтуитивное утверждение": f"[Распространённое убеждение] про «{topic}» — не работает. Вот почему.",
        "Обещание результата": f"Как [результат] в теме «{topic}» за [срок] — показываю.",
    }
    beats = [
        {"t_start": 0, "t_end": 2, "shot": "крупный план, взгляд в камеру", "speech": hooks.get(hook_type, "[хук]"),
         "on_screen_text": "[хук 3–6 слов]", "edit": "без заставки, сразу с фразы; субтитры"},
        {"t_start": 2, "t_end": 8, "shot": "средний план", "speech": "[почему это важно: цена проблемы в деньгах/времени]",
         "on_screen_text": "[цифра или следствие]", "edit": "склейка на смену плана"},
        {"t_start": 8, "t_end": duration - 8, "shot": "чередование крупный/средний", "speech": f"[основная часть в формате «{fmt}»]",
         "on_screen_text": "[пункты / шаги]", "edit": "склейка каждые 2–3 с, B-roll экрана/таблицы"},
        {"t_start": duration - 8, "t_end": duration - 3, "shot": "крупный план", "speech": "[вывод одной фразой]",
         "on_screen_text": "[вывод]", "edit": "пауза 0.3 с перед выводом"},
        {"t_start": duration - 3, "t_end": duration, "shot": "крупный план", "speech": "[CTA: напишите слово «…» в директ — пришлю …]",
         "on_screen_text": "[CTA]", "edit": "финальный титр"},
    ]
    body = f"# {topic}: {fmt}\n\nТип хука: **{hook_type}**. Длительность ≈ {duration} с.\n\n" + "\n".join(
        f"**{b['t_start']}–{b['t_end']} с** · {b['shot']}\n- Речь: {b['speech']}\n- Текст: {b['on_screen_text']}\n- Монтаж: {b['edit']}\n"
        for b in beats
    )
    return {"body_md": body, "storyboard": beats}


def extract_storyboard(text: str) -> list | None:
    """Достаёт JSON-раскадровку из ответа Claude, если она есть."""
    start = text.find("[")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start:i + 1])
                        if isinstance(data, list) and data and isinstance(data[0], dict):
                            return data
                    except json.JSONDecodeError:
                        pass
                    break
        start = text.find("[", start + 1)
    return None
