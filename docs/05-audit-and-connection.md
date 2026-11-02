# 5. Технический аудит MVP и подготовка подключения Instagram API

Дата: 02.11.2026. Ветка `claude/happy-sagan-yvzs26`.

## 5.1. Структура проекта (фактическая)
```
app/
  main.py          FastAPI: 40+ маршрутов (страницы, импорт, референсы, план, утверждение, Meta)
  config.py        настройки из .env, проверка готовности OAuth (meta_config_problems)
  db.py            SQLite-схема (15 таблиц), helpers
  metrics.py       каталог метрик и их доступности
  security.py      шифрование токенов (MultiFernet), атомарная замена, ротация ключа
  importers.py     CSV-импорт — резервный источник
  analytics.py     медианы, сравнение групп, выбросы, прирост
  classify.py      темы/форматы/хуки (правила)
  references.py    ffmpeg/ffprobe, транскрипт, разбор структуры
  claude_ai.py     промпты и вызовы Claude
  planner.py       план 25 Reels (ортогональные латинские квадраты)
  publishing.py    черновики и согласование (без публикации)
  reports.py       отчёты
  meta/client.py   Graph API клиент (только GET), OAuth URL, обмен токенов, debug_token
  meta/oauth.py    поток входа, state, поиск Instagram-аккаунта, здоровье токена
  meta/sync.py     синхронизация своих Reels и конкурентов
  meta/diagnostics.py  диагностика доступности API
tests/             55 тестов (pytest)
```

## 5.2. Что работало и что исправлено

| # | Проблема (найдена и воспроизведена) | Последствие | Исправление |
|---|---|---|---|
| 1 | Ролик из CSV (по permalink) затем приходил из API (по ig_media_id) → `IntegrityError` | синхронизация падала | поиск записи по `ig_media_id` ИЛИ `permalink` |
| 2 | Повторный CSV-импорт затирал метрики, полученные из API | ручные данные подменяли официальные | CSV поверх API только заполняет пустые поля; источник остаётся `api` |
| 3 | Сетевая ошибка (`httpx.ConnectError`, таймаут) не перехватывалась | HTTP 500 | `GraphClient.call()` возвращает структурированную ошибку `kind=network` |
| 4 | Логгер `httpx` на уровне INFO пишет полный URL, включая `access_token` | утечка токена в логи | логгеры `httpx/httpcore` принудительно на WARNING + scrub во всех ошибках |
| 5 | URL входа отправлял `scope`, если нет `config_id`; не было `override_default_response_type` | новые приложения Meta отклоняют `scope` («Invalid Scopes») | вход только по `config_id`, параметры как у Facebook Login for Business |
| 6 | Redirect URI по умолчанию `http://` | Meta требует HTTPS | `https://localhost:8000/meta/callback`, валидация схемы/пути, `app.cli dev-cert` |
| 7 | state хранился только в БД, без привязки к браузеру | слабая защита от CSRF | state в HttpOnly/Secure/SameSite=Lax cookie + БД, одноразовый, TTL 10 мин |
| 8 | Аккаунт искался только через `/me/accounts`, бралась первая страница | при System-user токене Unified Login аккаунт не находился | fallback через `debug_token.granular_scopes.target_ids`, выбор по `META_IG_USERNAME` |
| 9 | Всегда выполнялся обмен на долгоживущий токен | ошибка для System-user токенов | тип токена определяется через `debug_token`, обмен только для USER |
| 10 | Ошибка токена (190) при insights считалась «метрика недоступна» | ложные выводы о метриках | ошибки токена/лимитов/сети пробрасываются, помечается «нужен повторный вход» |
| 11 | Новые токены записывались по одному | при сбое — смесь старых и новых | атомарная замена (SAVEPOINT) |
| 12 | Нельзя сменить ключ шифрования | — | MultiFernet + `TOKEN_ENCRYPTION_KEYS_OLD` + `python -m app.cli rotate-key` |
| 13 | Интерфейс с токенами мог работать без пароля | доступ к синхронизации без авторизации | вход через Meta блокируется без `APP_PASSWORD` (или явного `ALLOW_NO_PASSWORD=true`) |
| 14 | Нет диагностики | — | `POST /meta/diagnostics`, `GET /meta/diagnostics.json`, `GET /meta/health` |

Остальные модули (аналитика, классификация, референсы, план, утверждение, отчёты) работали
корректно и не переписывались.

## 5.3. Совместимость: Facebook Login for Business, Unified Login, Business Discovery

| Утверждение | Статус | Основание |
|---|---|---|
| Новые приложения Meta получают вход «Unified Login» вместо «API setup with Instagram login» | подтверждено | сообщения кабинета Meta, процитированные разработчиками; обзор Instagram Platform |
| Unified Login реализуется через диалог Facebook Login for Business с `config_id`; `scope` отклоняется | подтверждено сообщениями разработчиков, не первоисточником | issues postiz-app #2350, openpost #495 |
| Business Discovery доступен в Instagram API with Facebook Login (`graph.facebook.com`) | подтверждено документацией | справочник Business Discovery / IG User |
| Business Discovery доступен с токеном Unified Login именно вашего приложения | **не подтверждено** | проверяется шагом диагностики «Business Discovery: свой аккаунт» |
| Business Discovery работает с System-user токеном | **не подтверждено** | то же |
| `view_count` возвращается в Business Discovery | **не подтверждено** | диагностика фиксирует наличие поля |
| После Unified Login `/me/accounts` содержит страницу с Instagram | **не подтверждено**; разработчики сообщали о токенах без привязанного Instagram | поэтому есть запасной путь через `debug_token` |
| Работа без App Review в режиме разработки для пользователя с ролью | стандартное поведение Meta; для ваших разрешений проверяется входом | — |

## 5.4. Минимальные разрешения
Обязательные: `instagram_basic`, `instagram_manage_insights`, `pages_show_list`, `pages_read_engagement`.
По ситуации: `business_management` (страница в бизнес-портфолио).
Не нужны: `instagram_content_publish`, `instagram_manage_messages`, `instagram_manage_comments`,
`ads_management`, `ads_read`, `pages_manage_metadata`, `pages_messaging`, `pages_manage_posts`.
Если что-то из ненужного выдано, это показывают callback и диагностика.

## 5.5. Запросы после подключения
Все запросы — `GET https://graph.facebook.com/{GRAPH_API_VERSION}/...`, токен передаётся только Meta,
с `appsecret_proof`.

**Вход (один раз):**
1. `GET /oauth/access_token?client_id&client_secret&redirect_uri&code` — code → токен.
2. `GET /debug_token?input_token=…` (app token) — тип, срок, разрешения, `granular_scopes`.
3. `GET /oauth/access_token?grant_type=fb_exchange_token…` — только для USER-токена.
4. `GET /me/accounts?fields=id,name,access_token,instagram_business_account{id,username}`.
5. (если 4 пуст) `GET /{ig-id}?fields=id,username,…` для id из `granular_scopes`.

**Диагностика:**
`/debug_token`, `/me`, `/me/accounts`, `/{ig-id}` (профиль), `/{ig-id}/media`,
`/{reel-id}/insights?metric=<каждая метрика отдельно>`,
`/{ig-id}/insights?metric=<reach|views|accounts_engaged|total_interactions|profile_views|follows_and_unfollows>&period=day&metric_type=total_value`,
`/{ig-id}?fields=business_discovery.username(<свой>){…}`, затем для каждого конкурента.

**Синхронизация:**
`/{ig-id}` (подписчики), `/{ig-id}/media` (все страницы), `/{reel-id}/insights` для каждого Reels,
`/{ig-id}?fields=business_discovery.username(<конкурент>){…,media.limit(25){…}}` до 4 страниц на конкурента.

**Не выполняется никогда:** `POST` к Instagram, `/media`, `/media_publish`, сообщения, комментарии, реклама.

## 5.6. Что нельзя подтвердить без авторизации
- Какой тип токена выдаёт ваша конфигурация Unified Login (USER или SYSTEM_USER) и его срок.
- Найдётся ли Instagram через `/me/accounts` или только через `granular_scopes`.
- Доступность Business Discovery для вашего приложения и токена; поле `view_count`.
- Являются ли @aironzak и @di.sukharev профессиональными аккаунтами.
- Возвращаются ли для Reels `follows`, `profile_visits`, `reels_skip_rate`.
- Отдаёт ли `follows_and_unfollows` данные (порог 100 подписчиков формально пройден).
- Нужен ли `business_management` для вашей страницы.
- Актуальна ли версия `v24.0` (меняется в `GRAPH_API_VERSION`).

Всё это покажет таблица диагностики после первого входа (HTTP-статус, код ошибки Meta, fbtrace_id).

## Источники
- [Customize the Manage messaging and content on Instagram use case](https://developers.facebook.com/documentation/development/create-an-app/instagram-use-case)
- [Instagram Platform overview](https://developers.facebook.com/documentation/instagram-platform/overview)
- [Instagram API with Facebook Login](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login)
- [Facebook Login for Business (Instagram)](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login/business-login-for-instagram)
- [Business Discovery reference](https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery)
- [IG User reference](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/)
- [debug_token reference](https://developers.facebook.com/docs/graph-api/reference/debug_token/)
- Сообщения разработчиков (не первоисточник): [postiz-app #2350](https://github.com/gitroomhq/postiz-app/issues/2350), [openpost #495](https://github.com/getopenpost/openpost/issues/495), [ai-sales #139](https://github.com/tarunvalluri3/ai-sales/pull/139)
