# 8. Instagram Account ID для SYSTEM_USER (Unified Login): заключение

## 8.1. Факты из production (Railway, коммит 15da77c)
- Тип токена: `SYSTEM_USER` (токен системного пользователя бизнес-интеграции, BISU).
- Разрешения: `instagram_basic`, `instagram_manage_comments`, `instagram_manage_insights`,
  `instagram_content_publish`, `instagram_manage_messages`, `public_profile`. **Нет `business_management`.**
- `debug_token`: instagram-разрешения без `target_ids`.
- `/me/accounts`: 200, 0 страниц (Facebook-страницы нет).
- `/me/businesses`: 400, code 100, «Missing Permission».

## 8.2. Задокументированные способы получить Instagram Account ID
| Способ | Документация Meta | Требования | Применимо к нам |
|---|---|---|---|
| `granular_scopes.target_ids` в `debug_token` | Graph API · debug_token | target_ids **не показываются**, если доступ выдан ко всем объектам | ❌ target_ids нет — это не доказательство отсутствия доступа |
| `/me/accounts` → `instagram_business_account` | Instagram API with Facebook Login | Facebook-страница со связанным Instagram | ❌ страницы нет |
| `/me?fields=client_business_id` | Facebook Login for Business (BISU) | BISU-токен | ✅ добавлено в поиск; даёт ID бизнеса клиента |
| `/{system-user-id}/assigned_instagram_accounts` | Marketing API · System User | права на чтение назначенных активов; список разрешений в справочнике не указан | ✅ добавлено |
| `/{business-id}/owned_instagram_accounts`, `/instagram_accounts`, `/instagram_business_accounts` | Marketing API · Business | доступ к бизнесу (на практике `business_management`) | ✅ добавлено / уже было |
| `/me/businesses` | Graph API · User · businesses | `business_management` | ❌ «Missing Permission» подтверждает, что разрешения нет |

Другие endpoints не используются. Каждая проба в отчёте содержит ссылку на раздел документации.

## 8.3. Заключение
1. **Подтверждено ответом Meta:** у токена нет доступа к данным бизнес-портфолио (`/me/businesses` →
   code 100 Missing Permission), а страницы нет. Поэтому два способа, доступных без
   `business_management`, ничего не находят.
2. **По документации:** отсутствие `target_ids` при доступе «ко всем объектам» — нормальное поведение,
   выбор @mironovich.pro в окне Meta это не опровергает.
3. **По отчёту разработчика (не документация Meta):** в аналогичной конфигурации
   `/me/assigned_instagram_accounts` возвращал пустой список, а все вызовы к Instagram-аккаунту — `100/33`,
   пока в ту же конфигурацию (тот же `config_id`) не добавили `business_management`; после повторного
   входа аккаунт появился, заработали профиль, медиа и insights. Там же: `business_management`
   сохранялся в токене только при Advanced Access для `public_profile`.
4. **Вывод:** без `business_management` в нашей конфигурации обнаружить аккаунт задокументированными
   способами нельзя. Минимальное исправление — правка существующей конфигурации, не новое приложение.

## 8.4. Рекомендуемое решение (требует вашего согласования — это изменение в Meta)
1. Meta Developers → Content Intelligence → Facebook Login for Business → **Configurations** →
   конфигурация `1313415880861047` → Edit → добавить разрешение **`business_management`**. Сохранить
   (Configuration ID не меняется).
2. Заодно можно убрать ненужные Incon `instagram_content_publish` и `instagram_manage_messages`
   (не обязательно).
3. Если `business_management` пропадёт из токена после входа (`scopes` в отчёте), проверить в
   App Review → Permissions and Features уровень доступа `public_profile` и `business_management`
   (по отчёту разработчика нужен Advanced Access для `public_profile`). Публиковать приложение не нужно.
4. Войти заново на `/connect`, затем «Запустить диагностику».
5. Railway Variables не меняются.

## 8.5. Если правка конфигурации невозможна: Instagram Login
Минимальные изменения без нового проекта: второй OAuth-поток (`instagram.com/oauth/authorize` →
`api.instagram.com/oauth/access_token` → `graph.instagram.com`), переменные `IG_APP_ID`, `IG_APP_SECRET`,
`IG_REDIRECT_URI`, отдельное хранение токена и продление каждые < 60 дней, клиент с хостом
`graph.instagram.com`. Условие: в кабинете есть «API setup with Instagram login» (см. раздел 7.4) —
для новых приложений Meta, по отчётам разработчиков, его нет.

## 8.6. Сравнение способов входа
| Возможность | Unified Login (SYSTEM_USER) + `business_management` | Instagram Login |
|---|---|---|
| Профиль, подписчики, список Reels | ✅ `instagram_basic` (подтверждено отчётом) | ✅ `instagram_business_basic` |
| Статистика Reels и аккаунта | ✅ `instagram_manage_insights` | ✅ `instagram_business_manage_insights` |
| Business Discovery (конкуренты) | ❓ хост `graph.facebook.com` поддерживает; с SYSTEM_USER не подтверждено — проверит диагностика | ❌ только Facebook Login (справочник IG User) |
| Публикация | возможна (`instagram_content_publish`), в Incon не используется | возможна, не используется |
| Комментарии | `instagram_manage_comments` | `instagram_business_manage_comments` |
| Сообщения | `instagram_manage_messages` | `instagram_business_manage_messages` |
| Facebook-страница | не нужна | не нужна |
| Срок токена | без срока (BISU) | 60 дней, продление |
| Доступность для нашего приложения | ✅ уже настроен | ❓ вероятно недоступен для новых приложений |

## Источники
- Facebook Login for Business (BISU, `client_business_id`): https://developers.facebook.com/documentation/facebook-login/facebook-login-for-business
- System User · assigned_instagram_accounts: https://developers.facebook.com/documentation/ads-commerce/marketing-api/reference/system-user/assigned_instagram_accounts
- Business · owned_instagram_accounts / instagram_accounts / instagram_business_accounts: https://developers.facebook.com/documentation/ads-commerce/marketing-api/reference/business/owned_instagram_accounts
- debug_token: https://developers.facebook.com/docs/graph-api/reference/debug_token/
- IG User (business_discovery только Facebook Login): https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/
- Отчёт разработчика (не первоисточник): https://github.com/diwenne/openreply/issues/104
