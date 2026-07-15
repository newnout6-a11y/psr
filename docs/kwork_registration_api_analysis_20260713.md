# Kwork: анализ регистрации аккаунта

Дата исследования: 2026-07-13
Статус: frontend-контракт регистрации восстановлен; успешное создание аккаунта live не выполнялось.

## Итог

В frontend Kwork найдена полная клиентская цепочка регистрации. Основной запрос отправляется как `multipart/form-data` через JavaScript `FormData`.

Основные endpoint-ы:

```text
GET  /api/ban/disallowfreeregister
GET  /api/user/checkemail?email=...
POST /api/user/checklogin
POST /api/user/signup
POST /api/user/simplesignup
```

Исторический endpoint snapshot фиксирует эту цепочку в `kwork_js_endpoint_snapshot_2026-07-08T144640Z.json`:

- строки 459-469: gate регистрации;
- строки 638-646: динамический signup endpoint;
- строки 649-657: проверка email;
- строки 660-668: проверка логина;
- строки 728-743: login и login verification, не создание аккаунта.

## Общий flow

```text
/signup
  -> проверка разрешения регистрации
  -> проверка email
  -> генерация и проверка логина
  -> пароль, соглашения и CAPTCHA
  -> POST /api/user/signup
```

Клик по регистрации сначала вызывает `GET /api/ban/disallowfreeregister`. После разрешения открывается signup UI. На текущей странице `/signup` используется Vue-компонент `<auth-page mode="signup">`.

## Email-регистрация

### Проверка email

Перед отправкой основной формы frontend:

- удаляет пробелы по краям;
- требует непустой email;
- проверяет формат;
- ограничивает длину 90 символами;
- отбрасывает запрещённые или временные домены;
- вызывает `GET /api/user/checkemail?email=<encodeURIComponent(email)>`.

Обработка ответа:

- `success=true` означает, что email уже найден в системе; UI переводит пользователя во вход и использует возвращённые `email` и `warningMessage`;
- `in_stop_list=true` показывает ошибку о ранее удалённом аккаунте;
- `success=false` с `error` показывает ошибку формы;
- `success=false` без ошибки позволяет продолжить регистрацию.

В `isSimple`-режиме после отрицательной проверки email форма может сразу перейти к отправке signup-запроса. В обычном режиме открывается второй шаг с логином и паролем.

### Генерация и проверка логина

Логин строится из части email до символа `@`:

- кириллица транслитерируется;
- длина ограничивается 20 символами;
- удаляются символы, кроме латинских букв, цифр, `_` и `-`;
- клиентская проверка требует от 4 до 20 символов;
- имена, содержащие `kwork` или `support`, блокируются.

Первичная проверка использует:

```text
POST /api/user/checklogin
login=<value>
getFreeLogin=true
forceGenerate=<optional>
```

При изменении логина frontend после debounce около одной секунды отправляет также:

```text
POST /api/user/checklogin
jsub=1
getFreeLogin=true
login=<value>
```

При свободном логине ответ содержит `success=true`. Для занятого или короткого значения сервер может вернуть предложенный вариант в поле `login`.

### Основная форма

Обычная регистрация отправляет:

```text
POST /api/user/signup
Content-Type: multipart/form-data
```

Поля `FormData`:

```text
action_after       optional
track_client_id
userType           1 = покупатель, 2 = продавец
user_email
user_username
user_password
user_promo         может быть пустым
jsub=1
tz                 Intl.DateTimeFormat().resolvedOptions().timeZone
is_subscribed      передаётся только при значении 1
signup_mode=email
```

В упрощённом режиме URL меняется на:

```text
POST /api/user/simplesignup
```

`isSimple` также меняет порядок шагов и часть клиентской валидации.

## Телефонная ветка

Телефонная регистрация реализована в текущем signup bundle, но не была полностью выделена в старом endpoint snapshot.

Endpoint-ы:

```text
GET  /phone_codes
POST /captcha/check_phone_captcha_enabled
POST /signup_send_code
POST /signup_verify_code
```

Последовательность:

```text
GET /phone_codes
  -> POST /signup_send_code
  -> ввод 4 цифр из звонка
  -> POST /signup_verify_code
  -> временный phone token
  -> финальный /api/user/signup
```

Отправка кода:

```text
POST /signup_send_code
phone=<phone>
smart-token=<captcha token>       # Yandex SmartCaptcha
```

При legacy CAPTCHA вместо `smart-token` используется `g-recaptcha-response`.

Подтверждение:

```text
POST /signup_verify_code
phone=<phone>
code=<4 digits>
```

Успешный ответ содержит временный `data.token`. Он добавляется в итоговую форму:

```text
phone_token=<data.token>
user_phone=<phone>
signup_mode=phone
```

Повторная отправка кода также использует `/signup_send_code`, а frontend хранит таймер между попытками.

## CAPTCHA

На текущей странице `/signup` обнаружены:

```text
window.isRegistrationAllowed=true
window.isYandexSmartCaptcha=true
window.yandexSmartCaptchaToken="smart-token"
```

Основная ветка использует Yandex SmartCaptcha. Legacy fallback использует Google reCAPTCHA v2.

Frontend:

- получает ответ виджета через `smartCaptcha.getResponse(widgetId)` либо из `g-recaptcha-response`;
- добавляет token только если он непустой;
- при ошибке сбрасывает CAPTCHA;
- читает `recaptcha_show` из ответа backend и может показать CAPTCHA повторно.

Проверка phone-CAPTCHA выполняется отдельным `POST /captcha/check_phone_captcha_enabled`.

## Ответ signup endpoint

Клиент явно обрабатывает следующие поля ответа:

```text
success
errors
error
recaptcha_show
csrftoken
action_after
redirect
token
```

При успехе:

- если есть `csrftoken`, он записывается в скрытые DOM-поля для последующих форм;
- `followAfterAuthUrl` имеет приоритетный redirect;
- `action_after=order` может автоматически отправить форму заказа;
- `mirror_redirect` использует `token` и `redirect`;
- `index_redirect`, `redirect` и обычный reload обрабатываются отдельными ветками.

В найденном signup flow отдельный endpoint подтверждения email не обнаружен.

## Связанные, но не signup endpoint-ы

Эти запросы относятся к уже существующей авторизации:

```text
POST /api/user/login
POST /api/user/login/check-auth-code
GET/POST /verify_phone_activation_code
```

Они не создают аккаунт.

Социальная ветка формирует ссылки вида:

```text
/login_soc?type=<provider>&tz=<timezone>&usertype=1
```

Но успешное создание аккаунта через OAuth в документах не подтверждено.

## Read-only live-проверки

Запросы регистрации и verification-коды не отправлялись.

Проверено без побочных эффектов:

- `GET https://kwork.ru/signup` вернул HTTP 200;
- страница содержит `isRegistrationAllowed=true`;
- `GET /api/ban/disallowfreeregister` вернул `false`, то есть регистрация не была запрещена на момент проверки;
- `GET /phone_codes` вернул JSON со списком стран и кодов;
- синтетический свободный логин вернул `{"success":true}`;
- занятые/короткие логины вернули `success=false` и предложенный вариант;
- существующий `test@example.com` вернул `success=true` и предупреждение о существующем пользователе;
- временный `.invalid` email был отклонён как некорректный.

## Ограничения доказательств

В документах нет:

- успешного live `POST /api/user/signup`;
- фактически созданного аккаунта;
- сохранённого signup payload с реальными данными;
- точной полной схемы backend-ошибок;
- подтверждённого набора cookies после регистрации;
- подтверждения обязательного email activation flow.

Документированная live-проба `kwork_js_endpoint_live_status_2026-07-08T144816Z.json` проверяла `GET /api/user/checklogin`, хотя endpoint ожидает `POST`; сервер вернул техническую HTML-страницу. Это не подтверждает регистрацию. См. строки 45-50 файла и строки 775-780 `kwork_api_inspection_report.md`.

Файл `kwork_market_snapshots/kwork_js_buyer_endpoint_discovery_20260709T072340Z.json` содержит дублирующиеся JSON-ключи `offer` и `Offer`, поэтому не используется как авторитетный источник.

## Связь с PSR

PSR теперь содержит изолированный email-only signup flow в `src/platforms/kwork.py`, REST-клиент Firstmail в `src/utils/firstmail.py`, FastAPI route `POST /api/kwork/register` и отдельный desktop-раздел `/kwork-registration`. Пароль Kwork не возвращается в API-результате в открытом виде; runtime metadata хранит только маску.

Подтверждённая активация требует не только перехода по allowlisted Kwork URL, но и read-only повторной авторизации с вызовом `actor`. При CAPTCHA, отсутствии свежего письма, неубедительном ответе активации или отказе post-activation login результат остаётся pending/failed.

Live signup в рамках реализации не подтверждён: доступный `почты.txt` содержит Gmail-адреса без паролей Firstmail и не даёт валидной пары для безопасного end-to-end запуска. Поэтому документ не заявляет созданный аккаунт как факт.

## Вывод

Статический frontend-анализ надёжно подтверждает существование signup API и почти полностью восстанавливает его клиентский контракт. Для окончательной валидации backend нужен пользовательский браузерный тест с ручным прохождением CAPTCHA и подтверждения телефона/email, если оно будет запрошено. Автоматический signup без такого теста и без сохранения секретов не подтверждён.
