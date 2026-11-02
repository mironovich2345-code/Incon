# 4. Настройка существующего приложения Meta «Content Intelligence»

> Новое приложение не создаём. Приложение не публикуем (оно остаётся в режиме разработки /
> Development). Сайт mironovich.pro не трогаем. Названия пунктов кабинета Meta периодически
> меняются — ориентируйтесь на смысл шага.

## Что уже есть
- Приложение **Content Intelligence**.
- Сценарий (use case) **«Управление сообщениями и контентом в Instagram»**.
- Конфигурация **Unified Login** — для новых приложений Meta это вход Facebook Login for Business
  с `config_id` (старый вход «API setup with Instagram login» новым приложениям недоступен,
  а запрос с параметром `scope` они отклоняют с ошибкой «Invalid Scopes»).

## Redirect URI
Зарегистрирован (production, Railway):

```
https://incon-production.up.railway.app/meta/callback
```

`META_REDIRECT_URI` на Railway должен совпадать с ним посимвольно (без слэша в конце, без параметров).
Приложение проверяет это при запуске и показывает расхождение на странице «API».
Локальный вариант `https://localhost:8000/meta/callback` нужен, только если вы запускаете
приложение на своём компьютере и добавили этот URI в Meta.

Развёртывание и переменные — [06-railway-deploy.md](06-railway-deploy.md);
где взять App ID и App Secret — раздел 6.4 там же.

## Шаги в Meta Developers

### 1. Настройки входа
App Dashboard → Use cases → «Управление сообщениями и контентом в Instagram» → Customize →
настройка через Facebook Login (Facebook Login for Business) → Settings:
- **Valid OAuth Redirect URIs:** `https://incon-production.up.railway.app/meta/callback`
- **Client OAuth login:** включено; **Web OAuth login:** включено
- **Enforce HTTPS:** включено; **Use Strict Mode for redirect URIs:** включено (URI должен совпадать посимвольно)
- Если в кабинете есть «Login with the JavaScript SDK» — не нужен, оставьте выключенным.

### 2. Разрешения в сценарии
В списке разрешений сценария должны быть (при необходимости нажмите «Add» напротив):

| Разрешение | Зачем | Обязательно |
|---|---|---|
| `instagram_basic` | профиль, список публикаций, Business Discovery | да |
| `instagram_manage_insights` | статистика Reels и аккаунта | да |
| `pages_show_list` | найти Facebook-страницу, к которой привязан Instagram | да |
| `pages_read_engagement` | чтение данных страницы и связанного Instagram | да |
| `business_management` | только если страница принадлежит бизнес-портфолио и без него `/me/accounts` пуст | по ситуации |

Сценарий «сообщения и контент» по умолчанию может предлагать `instagram_manage_messages`,
`instagram_manage_comments`, `instagram_content_publish`, `pages_manage_metadata`. Для Content
Intelligence они **не нужны**: не добавляйте их в конфигурацию входа (шаг 3). Если кабинет не даёт
убрать какое-то разрешение из сценария — достаточно не включать его в конфигурацию; диагностика
покажет, если лишнее разрешение всё равно выдано.

### 3. Конфигурация Unified Login
App Dashboard → Facebook Login for Business → **Configurations** → откройте вашу конфигурацию
(или создайте новую рядом со старой, если существующую нельзя редактировать):
- **Login variation:** General.
- **Access token type:** *User access token* — предпочтительно: токен страницы, полученный через него,
  даёт доступ к Business Discovery через вашу страницу. Если кабинет предлагает Instagram-разрешения
  только для *System-user access token*, используйте его — код поддерживает оба типа и определяет тип
  через `debug_token`.
- **Token expiration:** для user token — 60 дней; для system-user — Never.
- **Assets:** Pages и Instagram accounts (выберите свою страницу и Instagram-аккаунт).
- **Permissions:** только `instagram_basic`, `instagram_manage_insights`, `pages_show_list`,
  `pages_read_engagement` (+ `business_management` по ситуации).
- Сохраните и скопируйте **Configuration ID** → `META_LOGIN_CONFIG_ID`.

### 4. Роли и режим
- App Roles: ваш Facebook-пользователь — **Administrator** (или Developer/Tester). В режиме
  разработки разрешения работают для пользователей с ролью в приложении — App Review и публикация
  приложения не нужны.
- **Не переключайте** приложение в режим Live.
- App Settings → Advanced → **Require App Secret** можно включить: клиент отправляет `appsecret_proof`.

### 5. Ключи
App Settings → Basic: **App ID** → `META_APP_ID`, **App Secret** → `META_APP_SECRET`.


### 6. Instagram и страница
- Instagram-аккаунт — профессиональный (Business или Creator).
- Привязан к Facebook-странице, на которой у вас есть полный доступ
  (Meta Business Suite → Настройки → Аккаунты Instagram).

## Запуск на своём компьютере по HTTPS
```bash
pip install -r requirements.txt
```
```bash
cp .env.example .env
```
```bash
python -m app.cli gen-key
```
Вставьте результат в `TOKEN_ENCRYPTION_KEY`, заполните `META_APP_ID`, `META_APP_SECRET`,
`META_LOGIN_CONFIG_ID`, `APP_PASSWORD`, установите `META_API_ENABLED=true`.
```bash
python -m app.cli dev-cert
```
```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --ssl-keyfile data/tls/localhost.key --ssl-certfile data/tls/localhost.crt
```
Откройте `https://localhost:8000/connect` (браузер предупредит о самоподписанном сертификате —
это ожидаемо для локального запуска), введите пароль из `APP_PASSWORD`, проверьте, что блок
«Настройка» без замечаний, нажмите **«Войти через Meta»**, выберите страницу и Instagram-аккаунт,
затем **«Запустить диагностику»**.

## Отключение
«API» → «Удалить токены» удаляет токены с сервера. Доступ приложения также отзывается в
Facebook → Настройки → Бизнес-интеграции.
