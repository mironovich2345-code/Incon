# 1. Официальные Instagram API от Meta: исследование и выбор

> Дата проверки: 02.11.2026.
> **Ограничение проверки:** в облачной среде, где писался проект, домен `developers.facebook.com`
> заблокирован сетевой политикой, поэтому страницы Meta не открывались напрямую. Сведения собраны
> через поиск по официальной документации Meta (фрагменты страниц) и сверены со сторонними
> справочниками. Всё, что не подтверждено первоисточником, помечено **«проверить»** и в коде
> проверяется запросом к API во время синхронизации (страница «Метрики» в интерфейсе).

## 1.1. Два варианта Instagram Platform API

| | Instagram API with **Instagram Login** | Instagram API with **Facebook Login for Business** |
|---|---|---|
| Хост | `graph.instagram.com` | `graph.facebook.com` |
| Аккаунт | Professional (Business/Creator) | Professional, **связанный с Facebook-страницей** |
| Разрешения | `instagram_business_basic`, `instagram_business_manage_insights`, … | `instagram_basic`, `instagram_manage_insights`, `pages_show_list`, `pages_read_engagement`, … |
| Insights своих публикаций и аккаунта | да | да |
| **Business Discovery** (данные других проф. аккаунтов) | **нет** | **да** |
| Hashtag Search | нет | да |
| Публикация контента | да (не используем) | да (не используем) |

Старые короткие имена scope (`business_basic` и т. п.) выведены 27.01.2025.
Instagram Basic Display API закрыт в декабре 2024 — его не используем.

## 1.2. Выбранная архитектура авторизации

**Instagram API with Facebook Login for Business**, потому что задача 3 (конкурентный анализ)
требует Business Discovery, а он есть только в этом варианте.

Что нужно от вас (делаете сами, мы ничего не создаём без подтверждения):
1. Instagram-аккаунт переведён в профессиональный (Business или Creator).
2. Аккаунт привязан к Facebook-странице (Meta Business Suite → Настройки → Аккаунты Instagram).
3. Создано Meta-приложение типа «Business» с продуктом «Facebook Login for Business» — инструкция
   в [04-meta-app-setup.md](04-meta-app-setup.md).

Поток авторизации (OAuth 2.0, Authorization Code):
```
Браузер → /meta/login → facebook.com/{v}/dialog/oauth (вы входите на сайте Meta)
        → /meta/callback?code&state  (проверка state против CSRF, TTL 10 мин)
Сервер  → обмен code на короткий user token → обмен на долгоживущий (~60 дней)
        → /me/accounts → токен страницы + id Instagram-аккаунта
        → токены шифруются (Fernet) и сохраняются в SQLite на сервере
```
- Пароль от Instagram/Facebook приложение не видит и не хранит.
- `app_secret` и ключ шифрования — только в переменных окружения сервера.
- Токены не отдаются в браузер и вырезаются из текстов ошибок.
- Токен страницы, полученный из долгоживущего user token, не имеет фиксированного срока, но
  инвалидируется при смене пароля, снятии прав и т. п. — тогда нужен повторный вход.

**Доступ без App Review.** Пока приложение используется только вами (вы — администратор/разработчик
приложения), разрешения работают в режиме Standard Access для пользователей с ролью в приложении.
App Review и Advanced Access нужны, только если подключать чужие аккаунты. **Проверить** при создании
приложения: требования к Business Verification для `business_management`.

### Минимальный набор разрешений
| Разрешение | Зачем |
|---|---|
| `instagram_basic` | профиль, список медиа, Business Discovery |
| `instagram_manage_insights` | insights публикаций и аккаунта |
| `pages_show_list` | найти страницу, к которой привязан Instagram |
| `pages_read_engagement` | чтение данных страницы / Business Discovery |
| `business_management` | если страница принадлежит бизнес-портфолио (**проверить**, может не понадобиться) |

**Не запрашиваем:** `instagram_content_publish`, `instagram_manage_comments`, `instagram_manage_messages`.

## 1.3. Какие метрики доступны

### Собственные Reels — `GET /{ig-media-id}/insights`
| Метрика API | Статус | Комментарий |
|---|---|---|
| `views` | доступна | введена в 2025 г. вместо `plays`/`impressions` |
| `reach` | доступна | оценочная |
| `likes`, `comments`, `shares`, `saved`, `total_interactions` | доступны | |
| `ig_reels_avg_watch_time` | доступна | миллисекунды |
| `ig_reels_video_view_total_time` | доступна | миллисекунды |
| `follows`, `profile_visits` | **проверить** | документированы для публикаций ленты; для Reels проверяем запросом |
| `reels_skip_rate` | **проверить** | есть в приложении; наличие в API проверяем запросом |
| `plays`, `clips_replays_count`, `ig_reels_aggregated_all_plays_count`, `impressions`, `video_views` | **выведены** 21.04.2025 | не запрашиваем |
| Кривая удержания по секундам | **нет в API** | только в приложении Instagram |
| Источники просмотров (лента/Reels/Explore) | **нет в API** | только в приложении |

**Удержание:** через API доступно только *среднее* и *суммарное* время просмотра. «Досмотр» в
интерфейсе считается как `avg_watch_time / длительность` и только если длительность известна
(её вводите вы или берём из файла ролика). График удержания по секундам через API недоступен — мы
его не показываем и не реконструируем.

### Собственный аккаунт — `GET /{ig-user-id}/insights`
| Метрика | Статус |
|---|---|
| `followers_count` (поле профиля) | доступно — прирост считаем по ежедневным снимкам |
| `follows_and_unfollows` | доступно при ≥ 100 подписчиков (у вас 208); без `since/until` |
| `reach`, `views`, `accounts_engaged`, `total_interactions`, `profile_views` | доступны, `metric_type=total_value` |
| `follower_demographics`, `engaged_audience_demographics` | при ≥ 100 подписчиков, топ-45 значений |
| `impressions` | выведена |
| Периоды `last_14_days`, `last_30_days`, `last_90_days`, `prev_month` | не поддерживаются с v20.0 |

### Конкуренты — Business Discovery
`GET /{my-ig-user-id}?fields=business_discovery.username(USERNAME){followers_count,media_count,media{...}}`

| Поле | Статус |
|---|---|
| `username`, `name`, `biography`, `followers_count`, `follows_count`, `media_count`, `website` | доступны |
| `media{id, caption, media_type, media_product_type, permalink, timestamp, comments_count}` | доступны |
| `like_count` | не возвращается, если автор скрыл лайки |
| `view_count` (Reels) | указан в справочнике; включает платные просмотры — **проверяем запросом** |
| Охват, сохранения, репосты, время просмотра, удержание | **недоступны никому, кроме владельца** |

Ограничения:
- Работает только для **профессиональных** аккаунтов. Если `@aironzak` или `@di.sukharev` —
  личные аккаунты, Business Discovery вернёт ошибку, и в таблице конкурентов будет статус
  «не проф. аккаунт». Тогда остаётся ручной ввод публично видимых чисел.
- Пагинация медиа — через курсоры `after/before` внутри field expansion (ссылок `next` нет).
- Прямой `GET /{media-id}` для чужих публикаций запрещён — только через field expansion.
- История публичных метрик строится нашими снимками (`media_snapshots`) при каждой синхронизации:
  Meta не отдаёт историю изменения лайков/просмотров.
- Лимиты: общий rate limit Graph API для Instagram (по числу вызовов на аккаунт в час) — синхронизация
  делает ограниченное число страниц (по умолчанию 4 × 25 публикаций на конкурента).

## 1.4. Что сознательно не делаем
- Не используем неофициальные API, скрейпинг, эмуляцию браузера, автоматизацию сессий Instagram,
  обход CAPTCHA.
- Не скачиваем чужие видео из Instagram. Библиотека референсов принимает только загруженные вами файлы.
- Не запрашиваем пароль.
- Не публикуем контент: в коде нет вызовов `POST /{ig-user-id}/media` и `/media_publish`.

## 1.5. Что проверить при подключении API (чек-лист этапа 2)
- [ ] Актуальная версия Graph API (`GRAPH_API_VERSION` в `.env`, по умолчанию `v24.0`).
- [ ] Возвращаются ли `follows`, `profile_visits`, `reels_skip_rate` для Reels (страница «Метрики»).
- [ ] Возвращается ли `view_count` в Business Discovery.
- [ ] Являются ли `@aironzak` и `@di.sukharev` профессиональными аккаунтами.
- [ ] Нужно ли `business_management` для вашей конфигурации страницы.

## Источники
- [Instagram Platform — Overview](https://developers.facebook.com/docs/instagram-platform/overview)
- [Instagram Media Insights (reference)](https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights/)
- [Instagram Account Insights (reference)](https://developers.facebook.com/docs/instagram-platform/api-reference/instagram-user/insights)
- [Business Discovery](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/business-discovery)
- [IG User business_discovery reference](https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery)
- [IG Media reference](https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/)
- [Insights (overview of permissions)](https://developers.facebook.com/docs/instagram-platform/insights)
- [Graph API changelog v22.0](https://developers.facebook.com/docs/graph-api/changelog/version22.0/)
- [Meta blog, 21.01.2025: views metric](https://developers.facebook.com/blog/post/2025/01/21/making-it-easier-to-build-integrations-across-the-instagram-api-and-marketing-api/)
- Сторонние сводки (для сверки, не первоисточник): [Emplifi — deprecation April 2025](https://docs.emplifi.io/platform/latest/home/instagram-insights-metrics-deprecation-april-2025), [Phyllo — permission scopes](https://www.getphyllo.com/post/instagram-api-permissions-scopes)
