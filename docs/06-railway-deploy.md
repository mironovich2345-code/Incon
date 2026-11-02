# 6. Production-развёртывание на Railway

Сервис: `https://incon-production.up.railway.app`
Redirect URI (зарегистрирован в Meta): `https://incon-production.up.railway.app/meta/callback`
Configuration ID (Unified Login): `1313415880861047`

> Ограничения: mironovich.pro не затрагивается; новые конфигурации Meta не создаются;
> Meta-приложение не публикуется; публикации Reels в коде нет.

## 6.1. Как приложение работает на Railway

| Аспект | Реализация |
|---|---|
| Сборка | `Dockerfile` в корне (Railway собирает по нему автоматически): Python 3.12 + ffmpeg |
| Запуск | `python -m app.serve`: порт из `PORT`, хост `0.0.0.0`, один процесс |
| HTTPS | TLS завершается на прокси Railway; uvicorn с `proxy_headers` и `forwarded_allow_ips="*"` берёт схему и IP клиента из `X-Forwarded-Proto/For` |
| Healthcheck | `GET /healthz` → `{"status":"ok"}`, без авторизации и без сведений о конфигурации |
| Хранилище | `DATA_DIR=/data` (задан в Dockerfile) = том Railway: SQLite (WAL), видео-референсы, кадры, черновики |
| Без тома | приложение работает, но на каждой странице красное предупреждение: данные удалятся при деплое |
| Пароль | HTTP Basic (`APP_PASSWORD`). На Railway без пароля всё, кроме `/healthz`, отвечает 503; `ALLOW_NO_PASSWORD` игнорируется |
| Перебор пароля | 10 неудачных попыток с IP за 15 минут → 429 на 15 минут |
| Заголовки | HSTS, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` |
| Логи | фильтр удаляет `code`, `state`, `access_token`, `client_secret` и т. п. из журнала доступа и ошибок |
| OpenAPI/Docs | отключены (`/docs`, `/redoc`, `/openapi.json` → 404) |

Config-as-code (`railway.toml`) не используется: по документации Railway он устарел и недоступен
для новых сервисов, поэтому настройки задаются в панели сервиса (ниже).

## 6.2. Настройки сервиса Railway

| Настройка | Значение |
|---|---|
| Source | GitHub-репозиторий `mironovich2345-code/incon`, ветка `claude/happy-sagan-yvzs26` |
| Root Directory | пусто (корень репозитория) |
| Builder | Dockerfile (определяется автоматически) |
| **Start Command** | `python -m app.serve` (совпадает с `CMD` в Dockerfile; можно оставить пустым) |
| **Healthcheck Path** | `/healthz` |
| **Volume Mount Path** | `/data` |
| Replicas | 1 (SQLite и защита от перебора рассчитаны на один экземпляр) |
| Public Networking | домен `incon-production.up.railway.app` (уже создан) |

Контейнер работает от root, поэтому запись на том не требует `RAILWAY_RUN_UID`.

## 6.3. Переменные (Railway → сервис → Variables)

### Обязательные
| Переменная | Значение | Как получить |
|---|---|---|
| `APP_PASSWORD` | длинный случайный пароль | `python3 -c "import secrets; print(secrets.token_urlsafe(24))"` на своём компьютере. Логин в окне браузера — любой |
| `TOKEN_ENCRYPTION_KEY` | Fernet-ключ | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. **Сохраните копию**: при потере ключа токены не расшифровать (потребуется повторный вход) |
| `META_API_ENABLED` | `true` | — |
| `META_APP_ID` | число | см. 6.4 |
| `META_APP_SECRET` | строка | см. 6.4 |
| `META_LOGIN_CONFIG_ID` | `1313415880861047` | Facebook Login for Business → Configurations (уже известно) |
| `META_REDIRECT_URI` | `https://incon-production.up.railway.app/meta/callback` | точная копия URI из Meta: без `/` в конце, без параметров |

### Необязательные
| Переменная | По умолчанию | Когда задавать |
|---|---|---|
| `DATA_DIR` | `/data` (из Dockerfile) | только если том смонтирован в другой путь |
| `GRAPH_API_VERSION` | `v24.0` | если нужна другая версия Graph API |
| `META_IG_USERNAME` | — | если входу доступно несколько Instagram-аккаунтов (без `@`) |
| `ANTHROPIC_API_KEY` | — | для функций Claude (console.anthropic.com → API Keys); без ключа сохраняются готовые промпты |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | — |
| `CLAUDE_ENABLED` | `true` | `false`, чтобы не обращаться к Claude API |
| `OUTLIER_RATIO` | `2.0` | порог «аномального» ролика |
| `TOKEN_ENCRYPTION_KEYS_OLD` | — | только во время смены ключа шифрования |
| `FORWARDED_ALLOW_IPS` | `*` на Railway | если известны адреса прокси |

### Не задавать
`PORT`, `RAILWAY_*` (Railway задаёт их сам), `ALLOW_NO_PASSWORD` (на Railway игнорируется),
`ANTHROPIC_AUTH_TOKEN` (альтернатива ключу, не нужна).

Код читает только перечисленные переменные (проверено поиском `os.environ` по `app/`), а также
`PORT`, `HOST`, `FORWARDED_ALLOW_IPS` в `app/serve.py` и `RAILWAY_ENVIRONMENT`,
`RAILWAY_PUBLIC_DOMAIN`, `RAILWAY_VOLUME_MOUNT_PATH` от Railway.

## 6.4. App ID и App Secret

**App ID** — проверяемые способы:
1. Адрес страницы приложения в Meta Developers имеет вид `https://developers.facebook.com/apps/<число>/…`;
   `<число>` — App ID. Он же показан рядом с названием приложения в шапке панели.
2. В адресе окна входа Meta параметр `client_id=<число>` — это App ID. После деплоя его можно увидеть,
   открыв «API» → «Войти через Meta» и посмотрев адресную строку facebook.com.
3. Проверка без секрета (после ввода App ID) — откройте в браузере:
   ```
   https://www.facebook.com/v24.0/dialog/oauth?client_id=<APP_ID>&redirect_uri=https%3A%2F%2Fincon-production.up.railway.app%2Fmeta%2Fcallback&response_type=code&config_id=1313415880861047&override_default_response_type=true&state=check
   ```
   Если App ID неверный, Meta сообщит об ошибке приложения; если Redirect URI не зарегистрирован —
   об ошибке URL. Если открылось окно входа, App ID и Redirect URI верны. Входить при этой проверке
   не нужно; если войдёте, приложение отклонит ответ из-за `state=check` — это ожидаемо.

**App Secret** — по документации Meta («Basic settings»): секрет приложения находится на странице
базовых настроек приложения (App settings → Basic), поле App secret, кнопка Show с повторным вводом
пароля Facebook. Прямой адрес этой страницы обычно имеет вид
`https://developers.facebook.com/apps/<APP_ID>/settings/basic/` — проверьте его в своём кабинете.
Секрет виден только пользователям с ролью **Administrator** приложения: если поля нет,
проверьте свою роль в App Roles.

**Проверка пары:** после деплоя — «API» → **«Проверить App ID и App Secret»**. Приложение выполняет
`GET /{app-id}?fields=id,name` с app access token и показывает название приложения или ошибку Meta.
Секрет в ответ и в логи не попадает.

## 6.5. Пошаговое развёртывание
1. Railway → проект → сервис `incon` → Settings → Source: репозиторий и ветка `claude/happy-sagan-yvzs26`.
2. Settings → Deploy: Start Command `python -m app.serve`, Healthcheck Path `/healthz`, Replicas 1.
3. Сервис → правый клик / Command Palette → **Attach Volume** (или «+ New → Volume» с привязкой к сервису),
   Mount Path `/data`.
4. Variables: обязательные переменные из 6.3 (через Raw Editor удобно вставить блок целиком).
5. Deploy. В Deploy Logs должно быть `Uvicorn running on http://0.0.0.0:<PORT>`.
6. Откройте `https://incon-production.up.railway.app/healthz` → `{"status":"ok"}`.
7. Откройте `https://incon-production.up.railway.app/connect`, введите пароль.
   Блок «Настройка» не должен содержать замечаний; красных баннеров про том быть не должно.
8. «Проверить App ID и App Secret» → должно показать название приложения.
9. «Войти через Meta» → выбрать страницу и Instagram-аккаунт → вернуться на `/connect`.
10. «Запустить диагностику» → таблица с HTTP-статусами и строка «Возможности токена».
11. Сделайте redeploy и убедитесь, что подключение и данные сохранились (проверка тома).

## 6.6. Системный токен (SUAT)
Если конфигурация `1313415880861047` выдаёт System-user access token:
- обмен на долгоживущий токен не выполняется; срок «без срока» сохраняется как отсутствие даты;
- `/me/accounts` может быть недоступен — тогда Instagram-аккаунт берётся из `granular_scopes`
  `debug_token`; страница и её токен в этом случае не используются;
- Insights и Business Discovery с SUAT **не подтверждены**: диагностика покажет, какие методы
  работают (`me_accounts`, `profile`, `media`, `reel_insights`, `account_insights`,
  `business_discovery_self`, `business_discovery_competitors`);
- если тип токена определить не удалось (ошибка `debug_token`) или он не USER/SYSTEM_USER,
  вход прерывается и ничего не сохраняется.

## 6.7. Проверено в этой сессии
- 85 автотестов, включая production-конфигурацию, точное совпадение redirect_uri в диалоге и при
  обмене code, закрытие интерфейса без пароля, блокировку перебора, отсутствие секретов в
  страницах/JSON/логах, сценарии SUAT.
- Локальный запуск `python -m app.serve` с переменными Railway: привязка `0.0.0.0:$PORT`,
  `/healthz` 200, HSTS по `X-Forwarded-Proto: https`, реальный IP из `X-Forwarded-For`,
  SQLite на томе, `code=***&state=***` в журнале доступа.
- **Не проверено:** полная сборка Docker-образа — в песочнице недоступны apt-репозитории Debian
  (сборка дошла до установки ffmpeg). Первая сборка на Railway — первая реальная проверка Dockerfile.
