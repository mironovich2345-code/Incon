# 7. Исправление подключения через Unified Login и проверки

## 7.1. Что было не так
Ошибка «Не найден профессиональный Instagram-аккаунт» возникала, когда вход и проверка токена
проходили, но код искал Instagram-аккаунт только двумя способами:
1. `/me/accounts` — работает лишь при наличии Facebook-страницы;
2. `granular_scopes.target_ids` только у разрешений с именами `instagram_basic` / `instagram_manage_insights`.

При Unified Login без страницы и с доступом «ко всем активам» оба способа пусты: по документации
Meta `target_ids` не показываются, если разрешение выдано на все объекты. Кроме того, отчёт о неудаче
не сохранялся, поэтому причину нельзя было увидеть.

## 7.2. Что изменено
- `app/meta/discovery.py` — несколько независимых способов поиска, ни один не обязателен:
  `target_ids` любого `instagram*`-разрешения → `/me/accounts` → `/{page-id}?fields=instagram_business_account`
  для страниц из `target_ids` → `/me/businesses` и бизнесы из `business_management` →
  `/{business-id}/instagram_business_accounts` и `/{business-id}/instagram_accounts`.
  Каждый найденный аккаунт подтверждается чтением профиля.
- Каждая попытка входа (успешная или нет) сохраняет **обезличенный отчёт** (таблица `login_attempts`,
  20 последних): этап, тип токена, имена разрешений, число `target_ids`, по каждому способу —
  шаблон запроса, HTTP-статус, код ошибки Meta, число найденных объектов. Токены, коды OAuth,
  ID и имена аккаунтов в отчёт не попадают (числа ≥ 5 цифр и `@имена` вырезаются из текстов ошибок).
- Понятные причины на странице «API»: `no_instagram_found`, `no_instagram_scopes`, `profile_unreadable`,
  `exchange_failed`, `debug_token_failed`, `user_denied`, `state_invalid` и др.
- «Запустить диагностику» теперь показывает все способы поиска аккаунта и возможность `account_discovery`.
- `GET /meta/login-attempts.json` — отчёты о попытках (требует пароль).
- Railway Variables, данные и конфигурация Meta не менялись.

## 7.3. Повторная проверка на Railway
1. Дождитесь деплоя ветки `claude/happy-sagan-yvzs26` (Deployments → статус Success).
   `https://incon-production.up.railway.app/healthz` → `{"status":"ok"}`.
2. Откройте `https://incon-production.up.railway.app/connect`, введите пароль.
3. Нажмите «Проверить App ID и App Secret» → должно появиться название приложения.
4. Нажмите «Войти через Meta». В окне Meta выберите **@mironovich.pro** (и портфолио Mironovich),
   подтвердите. Если Meta предлагает «Continue with Instagram» — можно использовать его.
5. Вернувшись в Incon, посмотрите:
   - зелёное сообщение «Подключено: @… (токен …, аккаунт найден через …)» — вход исправлен;
   - или красное «Подключение не выполнено: <причина>» и блок **«Последняя попытка подключения»**
     с таблицей способов поиска.
6. Если подключено — «Запустить диагностику». В строке «Возможности токена» видно, работают ли
   `reel_insights`, `account_insights`, `business_discovery_self`, `business_discovery_competitors`.
7. Пришлите содержимое `https://incon-production.up.railway.app/meta/login-attempts.json` (обезличено)
   и, если подключение прошло, `…/meta/diagnostics.json`. Во втором файле есть ваш username и число
   подписчиков — при желании замените их перед отправкой. Токенов и секретов там нет.

### Как читать результат
| Причина | Что значит | Что делать |
|---|---|---|
| `no_instagram_scopes` | в токене нет instagram-разрешений | в конфигурации входа нет `instagram_basic` — потребуется правка конфигурации (отдельное согласование) |
| `business_management_missing` | SYSTEM_USER без `business_management`, бизнес-endpoints отвечают «Missing Permission» | см. [08-system-user-discovery.md](08-system-user-discovery.md), раздел 8.4 |
| `no_instagram_found` | разрешения есть, но ни один способ не вернул аккаунт | по таблице проб видно, какие запросы отклонены и с каким кодом; пришлите отчёт |
| `profile_unreadable` | аккаунт найден, профиль не читается | код ошибки в отчёте; обычно нехватка `instagram_basic` на этот аккаунт |
| `exchange_failed` | обмен code не прошёл | проверить `META_APP_SECRET` и Redirect URI |
| `debug_token_failed` | Meta не проверила токен | проверить пару App ID / App Secret кнопкой на странице |
| `ok`, но в диагностике `reel_insights: нет` | нет `instagram_manage_insights` | добавить разрешение в конфигурацию (отдельное согласование) |

## 7.4. Проверка прямого Instagram Login в существующем Meta App (без кода и без изменений)
Цель — понять, доступна ли нашему приложению «Instagram API with Instagram Login»
(`instagram.com/oauth/authorize`, `graph.instagram.com`). Второй OAuth-поток пока не реализован.

1. Meta Developers → приложение Content Intelligence → **Use cases** → «Управление сообщениями и
   контентом в Instagram» → настройка (Customize / Settings).
2. Посмотрите, какие варианты API setup показаны:
   - есть **«API setup with Instagram login»** (или «Business login settings») — прямой вход доступен;
   - есть только **«API setup with Unified login»** и/или сообщение вида
     «API setup with Instagram login is not available for newer apps» — прямой вход для этого
     приложения недоступен (так сообщают разработчики о новых приложениях Meta).
3. Если вариант есть, запишите (не присылайте секрет):
   - отображается ли отдельный **Instagram app ID** (он отличается от Meta App ID) и **Instagram app secret**;
   - список разрешений: есть ли `instagram_business_basic` и `instagram_business_manage_insights`;
   - есть ли раздел генерации токенов с кнопкой добавления Instagram-аккаунта
     (Generate access tokens / Add account) — **не генерируйте токен**, только отметьте наличие;
   - App Roles: есть ли роль **Instagram Testers**.
4. Необязательная проверка ссылкой (ничего не меняет в Meta): откройте в браузере
   ```
   https://www.instagram.com/oauth/authorize?client_id=<INSTAGRAM_APP_ID_ИЛИ_META_APP_ID>&redirect_uri=https%3A%2F%2Fincon-production.up.railway.app%2Finstagram%2Fcallback&response_type=code&scope=instagram_business_basic%2Cinstagram_business_manage_insights
   ```
   и **не завершайте вход**. Сообщение «Invalid platform app» означает, что прямой вход недоступен;
   ошибка про redirect_uri — что приложение поддерживает вход, но адрес ещё не зарегистрирован
   (регистрировать его будем только после согласования). Маршрута `/instagram/callback` в Incon пока нет.
5. Пришлите: какой вариант API setup показан, есть ли Instagram app ID, роль Instagram Testers
   и результат шага 4.

От этих ответов зависит архитектура этапа 2: отдельный Instagram Login или Unified Login как основной вход.
