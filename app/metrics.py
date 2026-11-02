"""Каталог метрик: что доступно через официальный API, что нет и почему.

Статусы:
  api          — задокументировано Meta для текущих версий Graph API;
  api_limited  — задокументировано, но с ограничениями (порог подписчиков, окно дат);
  unverified   — встречается в документации/обзорах, но поведение надо подтвердить
                 запросом (кнопка «Проверить доступность» в интерфейсе);
  deprecated   — выведено из API (не запрашиваем, не показываем как данные);
  not_in_api   — видно только в приложении Instagram; можно ввести вручную;
  never        — недоступно для чужих аккаунтов ни в каком виде.

Источники и даты — в docs/01-meta-api-research.md.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    key: str              # колонка в нашей БД или условный ключ
    api_name: str | None  # имя метрики/поля в Graph API
    scope: str            # own_reel / own_account / competitor_profile / competitor_media
    label: str
    status: str
    note: str = ""
    manual_ok: bool = True  # можно ли ввести вручную из приложения Instagram


CATALOG: list[Metric] = [
    # --- Собственные Reels: GET /{ig-media-id}/insights ---
    Metric("views", "views", "own_reel", "Просмотры", "api", "Заменила plays/video_views с 2025 г."),
    Metric("reach", "reach", "own_reel", "Охват (уникальные аккаунты)", "api", "Оценочная метрика Meta."),
    Metric("likes", "likes", "own_reel", "Лайки", "api"),
    Metric("comments", "comments", "own_reel", "Комментарии", "api"),
    Metric("shares", "shares", "own_reel", "Репосты", "api"),
    Metric("saved", "saved", "own_reel", "Сохранения", "api"),
    Metric("total_interactions", "total_interactions", "own_reel", "Всего взаимодействий", "api"),
    Metric("avg_watch_time_ms", "ig_reels_avg_watch_time", "own_reel", "Среднее время просмотра", "api",
           "Возвращается в миллисекундах."),
    Metric("total_watch_time_ms", "ig_reels_video_view_total_time", "own_reel", "Суммарное время просмотра", "api",
           "Миллисекунды; включает повторы."),
    Metric("follows", "follows", "own_reel", "Подписки с ролика", "unverified",
           "Документировано для публикаций ленты; для Reels проверяется запросом."),
    Metric("profile_visits", "profile_visits", "own_reel", "Переходы в профиль", "unverified",
           "Документировано для публикаций ленты; для Reels проверяется запросом."),
    Metric("skip_rate", "reels_skip_rate", "own_reel", "Доля пропусков (skip rate)", "unverified",
           "Есть в приложении; наличие в API подтверждается запросом."),
    Metric("retention_curve", None, "own_reel", "Кривая удержания по секундам", "not_in_api",
           "График удержания виден только в приложении Instagram. Через API не отдаётся.", manual_ok=False),
    Metric("traffic_sources", None, "own_reel", "Источники просмотров (лента/Reels/Explore)", "not_in_api",
           "Только в приложении."),
    Metric("plays", "plays", "own_reel", "Plays / начальные воспроизведения", "deprecated",
           "Выведено 21.04.2025 (v22.0+ и все версии). Заменено на views.", manual_ok=False),
    Metric("impressions", "impressions", "own_reel", "Показы (impressions)", "deprecated",
           "Выведено 21.04.2025 для медиа после 02.07.2024.", manual_ok=False),
    Metric("leads", None, "own_reel", "B2B-заявки", "not_in_api",
           "Instagram не знает о ваших заявках. Вводится вручную из CRM/директа."),

    # --- Собственный аккаунт: GET /{ig-user-id}/insights ---
    Metric("followers_count", "followers_count", "own_account", "Подписчики (текущее число)", "api",
           "Поле профиля; прирост считаем по ежедневным снимкам."),
    Metric("follows_unfollows", "follows_and_unfollows", "own_account", "Подписки / отписки за день", "api_limited",
           "Не возвращается при < 100 подписчиков (у вас 208 — доступно). Без since/until."),
    Metric("account_reach", "reach", "own_account", "Охват аккаунта", "api"),
    Metric("account_views", "views", "own_account", "Просмотры контента аккаунта", "api", "metric_type=total_value."),
    Metric("profile_views", "profile_views", "own_account", "Просмотры профиля", "api_limited", "metric_type=total_value."),
    Metric("accounts_engaged", "accounts_engaged", "own_account", "Вовлечённые аккаунты", "api"),
    Metric("account_total_interactions", "total_interactions", "own_account", "Взаимодействия", "api"),
    Metric("demographics", "follower_demographics", "own_account", "Демография подписчиков", "api_limited",
           "Только при ≥ 100 подписчиков; топ-45 значений."),

    # --- Конкуренты: business_discovery ---
    Metric("c_followers", "followers_count", "competitor_profile", "Подписчики", "api",
           "Только если аккаунт профессиональный (Business/Creator)."),
    Metric("c_media_count", "media_count", "competitor_profile", "Число публикаций", "api"),
    Metric("c_views", "view_count", "competitor_media", "Просмотры Reels", "unverified",
           "Поле указано в справочнике Business Discovery; включает платные просмотры. Проверяется запросом."),
    Metric("c_likes", "like_count", "competitor_media", "Лайки", "api_limited",
           "Не отдаётся, если автор скрыл лайки."),
    Metric("c_comments", "comments_count", "competitor_media", "Комментарии", "api"),
    Metric("c_reach", None, "competitor_media", "Охват, сохранения, репосты, время просмотра", "never",
           "Приватные insights чужих аккаунтов недоступны никому, кроме владельца.", manual_ok=False),
]

STATUS_LABELS = {
    "api": "Доступно через API",
    "api_limited": "Доступно с ограничениями",
    "unverified": "Требует проверки запросом",
    "deprecated": "Выведено из API",
    "not_in_api": "Нет в API (только приложение / ручной ввод)",
    "never": "Недоступно",
}

SCOPE_LABELS = {
    "own_reel": "Мои Reels",
    "own_account": "Мой аккаунт",
    "competitor_profile": "Конкурент: профиль",
    "competitor_media": "Конкурент: публикации",
}

# Метрики, которые запрашиваем у /insights собственного Reels (только живые и непроверенные).
REEL_INSIGHT_METRICS = [m.api_name for m in CATALOG if m.scope == "own_reel" and m.status in {"api", "unverified"} and m.api_name]

API_TO_COLUMN = {m.api_name: m.key for m in CATALOG if m.scope == "own_reel" and m.api_name}


def by_scope() -> dict[str, list[Metric]]:
    out: dict[str, list[Metric]] = {}
    for m in CATALOG:
        out.setdefault(m.scope, []).append(m)
    return out
