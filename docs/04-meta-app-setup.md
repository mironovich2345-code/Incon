# 4. Настройка Meta-приложения (выполняете вы, когда решите подключать API)

> Мы не создаём приложение и не меняем настройки аккаунта за вас. Названия пунктов в кабинете
> Meta периодически меняются — сверяйтесь с текущим интерфейсом developers.facebook.com.

## Предварительно
1. Instagram → Настройки → Тип аккаунта → **Профессиональный** (Business или Creator).
2. Meta Business Suite → Настройки → Аккаунты Instagram → привязать Instagram к Facebook-странице.

## Приложение
1. developers.facebook.com → My Apps → **Create App** → тип/сценарий для бизнеса
   (Business; use case для управления Instagram через Facebook Login for Business).
2. Добавить продукт **Facebook Login for Business**.
3. Valid OAuth Redirect URIs: `https://<ваш-домен>/meta/callback`
   (для локального теста — `http://localhost:8000/meta/callback`, если кабинет разрешит).
4. Создать **Configuration** для Facebook Login for Business с разрешениями:
   `instagram_basic`, `instagram_manage_insights`, `pages_show_list`, `pages_read_engagement`
   (+ `business_management`, если страница в бизнес-портфолио). **Не добавлять** `instagram_content_publish`.
   Скопировать Configuration ID → `META_LOGIN_CONFIG_ID`.
5. App ID → `META_APP_ID`, App Secret → `META_APP_SECRET` (только в `.env` на сервере).
6. Убедиться, что ваш Facebook-пользователь — администратор приложения (тогда App Review не нужен).

## Сервер
```bash
python -m app.cli gen-key
```
Вставить результат в `TOKEN_ENCRYPTION_KEY`, задать `META_API_ENABLED=true`, `APP_PASSWORD`,
перезапустить приложение, открыть «API» → «Войти через Meta».

## Отключение
«API» → «Удалить токены» удаляет токены с сервера. Дополнительно можно отозвать доступ приложения
в настройках Facebook → Бизнес-интеграции.
