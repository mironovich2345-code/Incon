# Instagram Content Intelligence

Система анализа и производства контента для экспертного Instagram (управление бизнесом,
сотрудники, продажи, обучение, автоматизация, ИИ). Цель — найти форматы, которые дают просмотры,
подписки и B2B-заявки.

Основной источник данных — официальный Instagram API (Facebook Login for Business / Unified Login,
только чтение). CSV-импорт — резервный способ: пока API не подключён и для данных, которых в API нет.
Подключение: [docs/04-meta-app-setup.md](docs/04-meta-app-setup.md), аудит и диагностика:
[docs/05-audit-and-connection.md](docs/05-audit-and-connection.md).

## Что умеет
| Раздел | Возможности |
|---|---|
| Обзор | подписчики и прирост, медианы просмотров и времени просмотра, подписки и заявки с Reels |
| Мои Reels | список, метрики, сравнение по формату / теме / хуку / дате, ручная разметка и заявки |
| Конкуренты | отслеживаемые аккаунты (@aironzak, @di.sukharev), Business Discovery, медиана, выбросы, паттерны |
| Рейтинг | лучшие ролики по кратности к медиане своего аккаунта |
| Референсы | импорт MP4, склейки и ключевые кадры (ffmpeg), транскрипт, разбор структуры, разбор Claude |
| Сценарии | каркас по шаблону или оригинальный сценарий + раскадровка от Claude |
| Контент-план | 25 Reels за 5 дней, сбалансированный эксперимент по форматам/темам/хукам |
| Утверждение | черновики публикаций, отправка на утверждение, история решений. **Без автопубликации** |
| Отчёты | автоотчёт по данным и рекомендации Claude |
| Метрики | что доступно через API, что нет и почему; результат реальной проверки |
| API | вход через Meta, диагностика доступа (HTTP-статусы, коды ошибок), состояние токена, синхронизация |

## Принципы
- Только официальные API Meta. Никакого скрейпинга, обхода CAPTCHA, автоматизации сессий Instagram.
- Пароль от Instagram не запрашивается; токены хранятся зашифрованными на сервере.
- Отсутствующая метрика = «—», а не 0 и не оценка. Рядом с медианами указан размер выборки.

## Production (Railway)
Dockerfile + `python -m app.serve`, Healthcheck `/healthz`, Volume `/data`.
Подробно: [docs/06-railway-deploy.md](docs/06-railway-deploy.md).

## Локальный запуск
```bash
pip install -r requirements-dev.txt
```
```bash
cp .env.example .env
```
Без API (только CSV):
```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```
С Instagram API локально — по HTTPS (только если `https://localhost:8000/meta/callback` добавлен в Meta):
```bash
python -m app.cli dev-cert
```
```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --ssl-keyfile data/tls/localhost.key --ssl-certfile data/tls/localhost.crt
```
Затем страница «API» → «Войти через Meta» → «Запустить диагностику».

Нужен `ffmpeg` для референсов. Для локальной транскрипции речи — `pip install faster-whisper`
(иначе загружайте субтитры .srt/.vtt/.txt). Для функций Claude — `ANTHROPIC_API_KEY` в `.env`;
без ключа приложение сохраняет готовый промпт.

## Тесты
```bash
python -m pytest -q
```

## Документация
1. [Исследование Meta API, авторизация, разрешения, доступность метрик](docs/01-meta-api-research.md)
2. [Архитектура](docs/02-architecture.md)
3. [Пошаговый план](docs/03-implementation-plan.md)
4. [Настройка существующего Meta-приложения и Redirect URI](docs/04-meta-app-setup.md)
5. [Технический аудит, совместимость, запросы после подключения](docs/05-audit-and-connection.md)
6. [Production-развёртывание на Railway, переменные, App ID / App Secret](docs/06-railway-deploy.md)
