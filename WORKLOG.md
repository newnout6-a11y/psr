# Журнал работ

## 2026-07-14 - Kwork Market: выделенные аккаунты и VPNTE-маршруты (готово)

- Начата переделка durable-сбора из вкладки `Рынок Kwork` под изолированные пары «воркер + Kwork-аккаунт + VPNTE IP».
- Зафиксировано текущее состояние: worker уже эксклюзивно арендует VPNTE slot, но использует общие Session Hub cookies; аккаунт, IP регистрации и внешний IP маршрута не связаны в durable-пуле.
- Целевая инварианта: активные воркеры не делят Kwork-аккаунт, VPNTE slot или подтверждённый внешний IP; для аккаунта сначала выбирается slot/IP, на котором он был зарегистрирован, затем любой свободный уникальный маршрут.
- Пул будет ограничивать фактическую параллельность минимумом из requested workers, пригодных активированных аккаунтов и маршрутов с разными внешними IP. При меньшем числе аккаунтов они не будут использоваться одновременно несколькими воркерами.
- В план добавлены: сохранение registration VPNTE slot, durable account/IP leases, индивидуальный request identity, account-session clients, fleet API и UI для контроля назначений.
- Реализован `MarketIdentityPool`: выдаёт только активированный аккаунт с cookies, эксклюзивный VPNTE slot и подтверждённый уникальный egress IP. SQLite-ограничения не позволяют одновременно использовать один аккаунт, slot или IP в разных active bindings даже при гонке воркеров.
- В записи регистрации добавлены `registration_slot`, proxy регистрации, флаг включения в Market и стабильная persona (`machine-*`, User-Agent, язык, timezone, viewport). Новые batch-регистрации сохраняют точный VPNTE slot; старые записи используют сохранённый IP регистрации как первое предпочтение.
- Каждый Market worker теперь создаёт собственный Kwork-клиент с его cookies, паролем, proxy и persona headers; общие Session Hub cookies в этом режиме не применяются.
- Реализован fallback: при занятом IP регистрации воркер сохраняет свой аккаунт и берёт свободный подтверждённый IP. При manual rotate новый IP обязательно перепроверяется; если он совпал с занятым, воркер автоматически перепривязывается к свободному IP.
- Добавлены API и UI-панель `Fleet аккаунтов и IP` в карточке запуска: ёмкость пула, активные bindings, slot/current IP, причина fallback, persona, включение/отключение аккаунта, синхронизация, reconcile и ручная перепривязка.
- Размер web-batch установлен в 24 карточки. Планировщик и проверка partition-кандидатов поддерживают до 100 подтверждённых независимых сегментов; при наличии 100 таких сегментов создаются 100 стартовых batch-операций по одной на worker.
- Live read-only сверка 2026-07-14: 7 активированных аккаунтов с cookies, 6 healthy VPNTE routes и 6 разных подтверждённых egress IP, поэтому текущая честная параллельность равна 6. Внешние Kwork-запросы и регистрация в этой проверке не запускались.
- Проверки: 128 pytest Market/Kwork тестов и `ruff check` прошли; добавлены тесты на параллельную раздачу account/IP, fallback, конфликт IP после rotate, сохранение persona/registration slot и план из 100 стартовых batch-операций.
- Пересобран Windows NSIS-установщик: `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe` (83 017 442 байта, SHA256 `67A6B15AC7432C5278C824DACCB49935542A7CD3304723672B3CD00BA893E37C`).
- В форме «Новый сбор» добавлена явная таблица «Команда аккаунтов»: можно выбрать конкретные Kwork-аккаунты, увидеть email, cookies, IP/slot, persona и текущую занятость worker. Занятые, выключенные и неактивированные аккаунты недоступны для выбора; есть выбор всех доступных.
- Выбранная команда сохраняется в durable job как `account_registration_ids`; `MarketIdentityPool` учитывает её в расчёте ёмкости и при выдаче lease, поэтому невыбранный аккаунт не будет назначен worker. В карточке задачи команда также показана рядом с активными bindings.
- Для страницы создания пересобран Windows NSIS-установщик: `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe` (83 020 651 байт, SHA256 `8B6690B9D877D7E8DCF2D9BBA4245FA4F14F2864047011B83775D5F2050ACD47`). Проверки: 44 адресных pytest-теста, `ruff check` и `npm run build` прошли.
- Уточнён UX ёмкости команды: вместо неоднозначного `7 / 6` выводятся отдельные «Выбрано», «Запустится сейчас» и «Резерв». Аккаунты с общим IP регистрации помечаются в таблице; один предпочитает этот IP, второй получает иной свободный уникальный IP или ждёт в резерве. Установщик пересобран: 83 019 668 байт, SHA256 `4F31B9803D00EAFA63633E9D38EE1EECC854CC7C1EC5374A3A0595300DBCEE67`.
- Исправлена загрузка «Команды аккаунтов»: первичный запрос больше не ждёт `ipify` через каждый VPNTE-slot. Он сразу отдаёт локально сохранённые аккаунты и последний durable-срез маршрутов; кнопка обновления запускает отдельную проверку текущих VPNTE egress IP. Одновременно снижены timeout проверки до 4 с и параллельность проверки увеличена до 24, чтобы ручная сверка большого пула не блокировалась минутами.
- Лимит выбранных VPNTE slots для одной registration batch увеличен с 100 до 1 000. При 100 аккаунтах и 121 выбранном slot UI явно показывает: в этой пачке идут первые 100 слотов по порядку, 21 остаётся резервом.
- Чаты Kwork переведены на быстрый путь: API отдаёт сохранённые диалоги и историю сразу, а синхронизация списка и web-отметка прочтения выполняются в фоне и не удерживают UI. Таймаут чтения Kwork inbox снижен до 6 с.
- Убраны ошибочные fallback-вызовы `inboxRead` и `markInboxTracksAsRead`, которые после успешного открытия web-диалога отправлялись с неподходящими параметрами и засоряли логи сообщением «Недостаточно параметров для метода API».
- Пересобран Windows NSIS-установщик с этими изменениями: `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe` (83 024 059 байт, SHA256 `4B6D96CB125CC8AB1B1E7D8D25F53DA97887A822F81FE02347E7D84355104EA5`).

## 2026-07-12

- Разобраны причины проблем в карточке durable-запуска Kwork Market: устаревшие срезы интерфейса, потерянное продолжение после pause, отсутствие проекции полей каталога, шум от состояний исполнителей и скрытый пул маршрутов.
- Добавлено восстановление очереди при продолжении остановленного на паузе сбора: используются сохранённые cursor сегментов, а при достигнутой цели или лимите запускается анализ.
- Снижена детализация `worker.state_changed`: служебные polling-переходы `leasing/idle` сохраняются для восстановления, но больше не создают поток пользовательских событий.
- Нормализованы карточки web-каталога (`gtitle`, `userId`, `userName`) и добавлен backfill проекций из `canonical_json` при инициализации репозитория. Для текущего запуска подтверждены заголовки и продавцы.
- Попытки операций теперь фиксируют маршрут; список маршрутов показывает доступный пул после освобождения исполнителями.
- Добавлен job-scoped API и панель AI-разбора: история и запросы привязаны к конкретному durable-запуску, а не к legacy-анализу.
- Экран запуска переведён на русский, refresh стал частичным и coalesced по значимым событиям, а техническая лента заменена семантическим ходом запуска. Детали попытки показывают понятные счётчики и состояние позиции каталога.
- Проверки: `71 passed` в расширенном адресном наборе pytest, `ruff check` и `npx vite build` завершились успешно. Полную UI-проверку оставили пользователю по его просьбе.
- Финально обновлён persistent codebase index: `C-psr`, 19 723 nodes, 43 627 edges; артефакт записан в `.codebase-memory/graph.db.zst`.
- Пересобран Windows NSIS-установщик с новым AI-пайплайном: `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe` (x64, 82 775 219 байт, SHA256 `E60C27163412D61FA30C697A322F9C9A75BAF9F67513CCBA4347C70FC4A919B9`).
- Обновлена LLM-конфигурация OpenAI-compatible провайдера: base URL `https://byesu.com/v1`, модель `gpt-5.6-terra`, reasoning effort `high`. Реальный запрос через `LLMRouter` завершился успешно.
- Выполнен короткий проверочный запрос к AI: `gpt-5.6-terra` вернула русское приветствие через настроенный Responses-маршрут.
- Сверена документация Byesu для Codex CLI: используется `https://byesu.com/v1`, `wire_api=responses`, модель `gpt-5.6-terra` и `reasoning=high`. В PSR включено хранение Responses (`OPENAI_DISABLE_RESPONSE_STORAGE=false`) и сохранение истории Market Assistant между сообщениями; проверочный запрос снова вернул «Здравствуйте!».
- Market Assistant переведён с отправки 1,56 МБ базы одним prompt на Codex-подобный Responses tool loop. Модель получает инструменты обзора, поиска, точного чтения raw-карточек и полного анализа; локальная SQLite/JSON-база остаётся источником истины.
- Полный анализ обрабатывает каждую из 250 сохранённых карточек через map-reduce без отбрасывания записей. Map-результаты кэшируются на диске по хэшу вопроса и точного содержимого чанка, поэтому после сетевого сбоя повторяются только недостающие части.
- Настроено эффективное использование окна: три чанка по 506–523 тыс. символов (примерно 200–220 тыс. входных токенов), map-модель `gpt-5.6-luna` с reasoning `high`, финальная модель `gpt-5.6-terra` с reasoning `high`, параллельность 3. Реальный smoke-тест большого чанка использовал 207 996 входных токенов; полный durable job успешно проанализировал все 250 карточек.
- Проверки нового AI-пайплайна: `63 passed`, `ruff check` — успешно.

## 2026-07-13 - Kwork registration implementation

- Реализован email-only signup flow Kwork: preflight, checkemail, username, checklogin, multipart signup и fallback на simplesignup.
- Добавлен REST-клиент Firstmail с Bearer/X-API-KEY и email/password auth, нормализацией писем, поиском Kwork-ссылки и фильтрацией устаревших писем по времени запуска.
- Активация больше не считается подтверждённой по одному HTTP 2xx: ссылка проверяется по allowlist/path, затем выполняется read-only login и `actor`-проверка. Финальный `ok=true` возможен только после этого доказательства.
- Убрана выдача plaintext Kwork-пароля из результата регистрации; runtime metadata хранит только маску. Добавлены статусы CAPTCHA/pending/activation_failed и HTTP status signup/fallback.
- Добавлен отдельный desktop-раздел `Регистрация Kwork` с маршрутом `/kwork-registration`, формой Firstmail/Kwork, CAPTCHA/dry-run и явным отображением статуса активации.
- Добавлены unit-тесты username/form/no-phone, Firstmail link/envelope/stale-message и post-activation actor proof.
- Проверка реальной регистрации по `почты.txt` пока не может быть завершена: найденный файл содержит 512 Gmail-адресов без паролей Firstmail, а не валидные пары email/password для Firstmail. Данные в репозиторий не копировались.

## 2026-07-13 - Kwork registration plan audit (docs only)

- Сопоставлены `docs/kwork_registration_implementation_plan.md` и `docs/kwork_registration_api_analysis_20260713.md` с исходным firstmail-документом на рабочем столе, `docs/kwork_js_endpoint_snapshot_2026-07-08T144640Z.json`, CAPTCHA-диагностикой и VPNTE-заметками.
- Зафиксированы обязательные поля email-signup: `action_after`, `track_client_id`, `userType`, `user_email`, `user_username`, `user_password`, `user_promo`, `jsub=1`, `tz`, optional `is_subscribed=1`, `signup_mode=email`; CAPTCHA token передаётся как `smart-token` или `g-recaptcha-response`.
- Уточнены семантики `checkemail` (existing account/stop-list), `checklogin` (form POST + suggested login), firstmail auth (Bearer и X-API-KEY + email/password), tolerant message schema и безопасные фильтры без destructive action.
- Документы не подтверждают успешный live signup, обязательную email-активацию или cookies после регистрации; implementation должна возвращать pending/structured failure при captcha, mail timeout, activation missing и не маркировать аккаунт активированным по generic HTTP 200.
- Найдена устаревшая статическая ссылка на proxy `17990` в `docs/kwork_proxy_rotation_notes.md`; регистрация должна использовать актуальный `proxyUrl` из VPNTE `/instances` через `vpnte_proxy`.
- Добавлены рекомендации по тестам: exact multipart/no-phone, firstmail auth/parser, CAPTCHA/`/not_access.php`, route error mapping, no secrets in logs/runtime. Изменения кода в рамках первоначального аудита не вносились.

## 2026-07-13 - Kwork registration implementation audit

- Проверена реализация по `docs/kwork_registration_implementation_plan.md`: backend flow в `src/platforms/kwork.py`, Firstmail REST-клиент, FastAPI `/api/kwork/register`, отдельный desktop-раздел и тесты.
- Локальные проверки: `python -m pytest tests/unit/test_kwork_registration.py tests/unit/test_firstmail.py -q` -> 7 passed; `npm run build` в `desktop/` -> Vite и Electron-builder завершились успешно.
- Живой signup не запускался: в `C:\Users\Redmi\Desktop\сюда ложи все txt с раб стола\почты.txt` найден пул из 512 gmail-адресов, а не firstmail-ящиков; на рабочем столе отсутствует безопасный подтверждённый пул firstmail email/password для end-to-end прогона.
- Зафиксированные остаточные риски: нет доказанного live `POST /api/user/signup`/CAPTCHA обхода; вход после активации требует отдельного `_create_api_client` и может быть заблокирован Kwork; `KworkService` не использует `RatePacer` внутри signup; runtime metadata хранит только маскированный Kwork пароль и не содержит mail credentials/cookies; полноценного списка/Session Hub импорта созданных аккаунтов нет.
- Live-проверка с предоставленными credentials: `GET /api/user/checkemail` вернул `success=true` и `currentAppAttached=true`, то есть email уже связан с Kwork. Скриншот signup также показывает frontend-ошибку «email некорректен или является временным» для `bekommenmail.com`; домен добавлен в локальный preflight blocklist.
- Повторная read-only авторизация Kwork с указанным паролем создала клиент библиотеки, но `actor` и `getActorInfo` вернули «Логин или пароль указаны неверно». Firstmail REST через текущий proxy завершился `ConnectTimeout`. Новый аккаунт не создавался, так как Kwork остановил поток до signup.

## 2026-07-13 - CatchMail registration continuation

- Исследован официальный публичный API `https://catchmail.io/docs` прямыми запросами без Tavily: новый ящик создаётся лениво по уникальному адресу, чтение списка идёт через `GET /api/v1/mailbox?address=...`, а полный текст письма — через `GET /api/v1/message/{id}?mailbox=...`. API не требует пароль или key и ограничен одним запросом в секунду на IP.
- Добавлен server-side `CatchmailClient`: генерация Kwork-совместимого случайного адреса, pacing не быстрее 1.1 секунды, чтение message detail и поиск Kwork activation URL в HTML/text. Renderer к CatchMail не обращается, поэтому CORS сервиса не влияет на desktop UI.
- Регистрация переведена на provider contract: CatchMail является значением по умолчанию и генерирует адрес при пустом email; Firstmail остаётся явным legacy-режимом. В API добавлен `mail_provider`, а пароль ящика обязателен только для Firstmail.
- Добавлен endpoint продолжения `/api/kwork/register/verify`: он повторно проверяет ящик, активирует найденную ссылку и выполняет read-only `actor` proof без повторного signup. Token activation link и signup tokens не возвращаются в UI.
- Отдельный пункт `Регистрация Kwork` сохранён в навигации и обновлён: CatchMail выбирается сегментом по умолчанию, пароль Firstmail не требуется, а pending/failed activation можно продолжить кнопкой проверки.
- Проверки реализации: `12 passed` (`test_catchmail`, `test_kwork_registration`, `test_firstmail`), `ruff check` без замечаний и `npm run build` завершился успешно. Реальный dry-run с новым случайным CatchMail адресом прошёл Kwork preflight (`checkemail` и `checklogin`).
- Следующий шаг: выполнить live signup с новым CatchMail ящиком и считать задачу завершённой только после activation + authenticated `actor` proof.

## 2026-07-13 - Kwork session-bound endpoint activation

- Добавлено SQLite-хранилище `data/runtime/kwork_accounts.db`: в нём сохраняются generated Kwork password, полный cookie jar (domain/path/expiry/secure/HttpOnly), signup token/CSRF data, точный proxy URL, статусы и IP регистрации/активации.
- `POST /api/kwork/register` всегда генерирует пароль на сервере, создаёт `registration_id` после успешного signup и не возвращает пароль, signup token или cookies в UI/API.
- CatchMail polling после signup начинает с конфигурируемой задержки 5-10 секунд (`KWORK_REGISTRATION_MAIL_INITIAL_DELAY=7` по умолчанию), затем соблюдает pacing публичного API.
- `POST /api/kwork/register/verify` принимает только `registration_id` (и legacy Firstmail credentials при необходимости), восстанавливает сохранённый HTTPX session/proxy и открывает email link в той же авторизованной сессии. После link выполняется password-free `/inbox` session proof; actor login остаётся вторичным доказательством.
- UI больше не просит Kwork password; в результате показывает сохранённый IP и количество cookies без раскрытия секретов.
- Живой endpoint-run завершён: `POST /api/kwork/register` создал CatchMail-аккаунт, дождался письма, активировал `/confirmemail` в исходной сессии и вернул `status=activated`, `session_cookie_count=8`, совпадающие signup/activation IP и успешный `/inbox` session proof. Повторный `POST /api/kwork/register/verify` по одному `registration_id` вернул сохранённую активированную запись без передачи пароля; отдельный новый Python-процесс восстановил её из SQLite и снова прошёл `/inbox` только по сохранённым cookies.
- Проверки: 37 адресных pytest-тестов, `ruff check` и `desktop/npm run build` завершились успешно.

## 2026-07-14 - Kwork registration credentials and validation

- Логин больше не формируется из CatchMail local part: используется читаемая комбинация двух ASCII-слов и четырёх цифр (`brightmosaic1234`), без дефисов, подчёркиваний и других спецсимволов; Kwork `checklogin` по-прежнему подбирает свободный вариант, а его предложения нормализуются до букв и цифр.
- Пароль создаётся только после окончательного `checklogin`, всегда отличается от логина и содержит ASCII-буквы, цифры и один из разрешённых символов `! @ # $ %`.
- `recaptcha_show` больше не маскирует ошибки валидации signup: при возвращённых `error`/`errors` API отдаёт `signup_failed` с сообщением Kwork вместо ложного статуса CAPTCHA.
- Проверки: 40 pytest-тестов, `ruff check` и новая NSIS-сборка завершились успешно.

## 2026-07-14 - Kwork credentials display

- Проверена запись активированного CatchMail-аккаунта: SQLite record содержит generated password, 8 cookies, signup/activation IP и session data.
- Добавлен `POST /api/kwork/register/credentials`: по `registration_id` он возвращает локально сохранённые email, login и password, но не возвращает cookies, token или proxy.
- В карточку результата регистрации добавлена строка пароля с кнопкой показа/скрытия; после создания пароль загружается и отображается автоматически.

## 2026-07-14 - Kwork clear-text storage and transport IP

- Password, cookies и auth-data перенесены из Fernet ciphertext в открытые колонки SQLite `password` и `session_json`; старые cipher-поля текущих записей очищены. Новые регистрации пишутся в открытом формате сразу.
- Реальный API signup после изменения создал и активировал аккаунт с логином `smartmosaic6843`; в ответе и новой SQLite-записи сохранены `signup_ip=94.185.83.5` и `activation_ip=94.185.83.5`, по тому же HTTP transport, которым шли Kwork-запросы.

## 2026-07-14 - Kwork batch registration through VPNTE

- Desktop-регистрация получает активные экземпляры через тот же VPNTE Control API `/instances`, что и сетевой раздел: стандартный endpoint `http://127.0.0.1:17873`, а фактический URL может быть взят из VPNTE endpoint-файла или `VPNTE_CONTROL_URL`.
- Добавлены выбор работающих VPNTE-слотов и количество аккаунтов (1-100). Выбранные `proxyUrl` назначаются по кругу; одновременно запускается до 5 регистраций, причём одна волна не повторяет один маршрут.
- `POST /api/kwork/register/batch` валидирует слоты по live `/instances`. CatchMail, signup, activation и IP probe для каждого аккаунта используют один `proxyUrl`, а в результате и SQLite-записи сохраняются слот, IP, cookies и пароль.
- В результате пачки выводятся login, пароль, использованный VPNTE slot, IP и статус каждого аккаунта.
- Read-only IP probe через все пять текущих VPNTE-маршрутов подтвердил пять различных публичных выходов; первая волна может запускать пять регистраций одновременно.
- Проверки: 45 адресных pytest-тестов, `ruff check`, production-сборка desktop и NSIS-установщик выполнены успешно.

## 2026-07-14 - VPNTE route preflight and optional historical IP filter

- Разбор пачки из 100 аккаунтов подтвердил, что `running=true` означает только запущенный локальный proxy-процесс: VPNTE-логи показали DNS timeout и EOF внутри удалённых туннелей, а 82 регистрации завершились транспортной ошибкой.
- Перед signup выбранные слоты теперь параллельно проверяются через Kwork и внешний IP. В одну пачку допускаются только отвечающие маршруты с разными текущими IP; поздние слоты используются вместо нерабочих ранних, а лишние рабочие маршруты становятся реальным резервом.
- Если уникальных рабочих IP меньше количества аккаунтов, пачка останавливается до первого signup и возвращает количество недоступных, повторяющихся и ранее использованных маршрутов.
- Фильтр IP, уже сохранённых в локальной базе аккаунтов, вынесен в отдельный UI-переключатель и по умолчанию выключен. Уникальность IP внутри текущей пачки остаётся обязательной всегда.
- Проверки: 44 адресных pytest-теста, `ruff check`, production-сборка desktop и NSIS-установщик выполнены успешно.

## 2026-07-15 - Per-account activation retry

- В таблице сохранённых Kwork-аккаунтов для статусов `activation_pending` и `activation_failed` добавлена отдельная кнопка повторной активации с индикатором выполнения.
- Действие повторно опрашивает почтовый ящик после времени исходной регистрации, восстанавливает сохранённые cookies и proxy, открывает activation link и повторяет проверку авторизованной сессии именно для выбранного аккаунта.
- После запроса таблица перечитывается из локальной базы; backend пишет в лог начало, ошибку mailbox polling и итоговый статус повторной активации.
- Проверки: 44 адресных pytest-теста, `ruff check`, production-сборка desktop и NSIS-установщик выполнены успешно.

## 2026-07-15 - Kwork market black screen fix

- Исправлен runtime `ReferenceError` в таблице команды аккаунтов: `sharedSignupIpCount` передавался в строку аккаунта, но не извлекался из props перед использованием.
- Сбой проявлялся после успешной загрузки пула с хотя бы одним аккаунтом и обрывал рендер всей вкладки рынка Kwork.
- Production-сборка Vite после исправления завершилась успешно; общий `tsc --noEmit` по-прежнему сообщает о ранее существовавших ошибках в других экранах проекта.

## 2026-07-15 - VPNTE health-contract compatibility

- Разобран каскад ошибок Kwork в 00:51-00:53: `VPNTE_PROXY_ROTATE_ON_NEXT=true` заставлял каждый путь создания Kwork API-клиента перезапускать очередной уже healthy слот. За три минуты PSR инициировал 18 rotate для слотов 1-11.
- Новая версия VPNTE после rotate возвращает `processRunning=true`, `health=checking`, `running=false`. Старый PSR немедленно вызывал `/instances`, считал это отсутствием подтверждения и переходил к следующему слоту.
- Логи VPNTE подтвердили race: 17 из 18 новых процессов были проверены через 6-31 мс после `started` и получили `ECONNREFUSED`, хотя VPNTE декларирует initial health delay 750 мс. Затем собственный recovery VPNTE запускал trigger/restart и часть слотов отключалась после трёх неудач.
- Обычная инициализация Kwork и transport recovery теперь явно используют только уже healthy маршрут с `rotate=False`, независимо от legacy-переключателя. В текущем `.env`, шаблоне и safe mode `VPNTE_PROXY_ROTATE_ON_NEXT` выключен.
- Явные start/rotate/connect/trigger адаптированы к новому контракту: PSR ждёт warmup, вызывает slot-specific `/healthcheck`, принимает маршрут только после `running=true` и выводит фактический `lastError` вместо ложной ошибки авторизации.
- Добавлены настройки `VPNTE_PROXY_HEALTH_TIMEOUT=20` и `VPNTE_PROXY_HEALTH_WARMUP=1`.
- Live read-only проверка с намеренно принудительным `VPNTE_PROXY_ROTATE_ON_NEXT=true`: выбран healthy proxy slot 14, Kwork `actor` успешно ответил, новых VPNTE rotate-событий не появилось. Проверки: 60 pytest-тестов, `ruff check` и production Vite build прошли успешно.
- Финальная проверка расширена до 104 pytest-тестов. Собран NSIS-установщик `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe` размером 83 034 494 байта, SHA256 `BCD9A399DF9514E3C5E3E166A46998ECDDD6A16F58A7AD77C8B148F971604C01`.

## 2026-07-15 - Kwork Market parallel collection and final report

- База последнего завершённого запуска `job_300de51f558e416d8b2f63038ecd1a33` подтвердила дефект: при `desired_workers=13` был создан один shard, а 22 web-catalog запроса выполнялись последовательно. Следующий запрос начинался только после завершения предыдущего; число workers не задавало реальную ширину сетевого фронта.
- Выбор команды аккаунтов в форме исправлен: после выбора всех доступных аккаунтов поле workers автоматически принимает подтверждённую ёмкость выбранной команды. Профиль больше не возвращает значение к пресету `2`, если команда уже выбрана; ручное уменьшение остаётся доступно.
- Планировщик теперь ограничивает стартовую ширину числом настроенных workers и распределяет общий request budget между этой стартовой волной. Для параллельных lane используются непересекающиеся price-band filters из `catalogFilters.priceLimits`; classification filters остаются fallback.
- Partition-кандидаты больше не проверяются последовательными запросами одного mapping worker. Их первый contract-valid fetch ставится в общую durable-очередь и выполняется отдельным worker с его аккаунтом и уникальным VPNTE IP. Неэффективный фильтр, вернувший fingerprint корневой выдачи, фиксируется и закрывается без загрязнения карточек.
- Адаптивная рекомендация concurrency больше не уменьшает явно заданное число workers. Фактический пул ограничивается только жёсткой доступностью выбранных аккаунтов и уникальных IP; рекомендации по timeout/protection сохраняются как advisory event.
- Добавлены durable события `request.started`/`request.finished` с worker, аккаунтом, transport, текущим и пиковым числом одновременных запросов. API результата считает фактический `peak_parallel_requests`, число использованных workers, аккаунтов, маршрутов и IP.
- Исправлен пустой журнал после повторного открытия задачи: UI отдельно загружает последние 1000 durable events через tail API и объединяет их с live WebSocket. В журнале отображаются worker/account/IP, active/peak concurrency, длительность и исход запроса.
- Итоговый checkpoint теперь содержит `semantic_analysis` и сохранённый `ai_verdict`. AI получает только bounded semantic dossier, а локальные таблицы строятся по всем durable enriched listings. Ошибка AI не скрывает таблицы и сохраняется отдельным статусом.
- Новый итоговый экран показывает фактическую параллельность, цены и квантили, повторяемость продавцов, таблицу смысловых групп со спросом/конкуренцией/барьером, AI-вердикт и его доказательства, а также распределение цен по shard.
- Планирование дополнительно усилено после диагностического live-run: корневой shard получает полный расчётный budget до целевого количества карточек, а остальные workers получают по стартовому запросу. Поэтому пустые или проигнорированные classification filters больше не обрывают сбор раньше цели.
- Первая волна использует barrier: сетевые fetch-операции ждут готовности всей назначенной команды. Выдача VPNTE routes больше не повторяет синхронный `/instances` для каждого кандидата; один актуальный snapshot кэшируется, а `acquire_slot` работает без повторного control-plane запроса. Повторное получение собственного slot стало идемпотентным.
- Исправлены stale leases: при release/rebind освобождаются все локальные маршруты worker, освобождение сохраняется в `market_transports`, а refresh очищает leases, которым больше не соответствует active durable binding. Перед финальным прогоном подтверждено: active bindings `0`, transport leases `0`, effective capacity `40`.
- Долгие операции теперь продлевают operation lease во время выполнения. Это устранило повторный захват semantic analysis после исходных 60 секунд. Terminal analysis больше не ставит новый export, а results API выбирает завершённый export-checkpoint даже при наличии позднего analysis-checkpoint в старых данных.
- Обязательный live-run выполнен на `job_847c2861a7c7473d9534a95dc5696d30`: target `1000`, получено `1008` уникальных карточек, `81` fetch request, collection request span `92` секунды. Первая волна зафиксировала `40` готовых workers, `40` аккаунтов, `40` transport IDs, `40` подтверждённых egress IP и реальный `peak_parallel_requests=40`.
- Live enrichment успешно завершён для `974` карточек; `34` записи исключены price/data gate. Локальный semantic analysis создал `476` кластеров и bounded dossier из `100` групп. Во время CPU-heavy анализа job API продолжал отвечать, контрольный GET занял `139 ms`.
- Финальный AI verdict завершён со `status=ok`, `market_verdict=mixed`, confidence `78`. Вывод: массовые логотипы и визитки перегреты; наиболее доказанная точка входа — кириллическая/русская адаптация существующего логотипа или шрифта с передачей векторных файлов.
- Итоговые таблицы подтверждены через results API: price p25/p50/p75 `1000/1500/3000`, `736` уникальных продавцов, `150` повторяющихся продавцов, repeat share `0.418651`, seller HHI `0.001992`, semantic group metrics и shard price distribution. Execution summary возвращает configured workers `40`, peak `40`, distinct workers/transports/accounts/IP `40/40/40/40`.
- Durable журнал содержит `4707` событий; tail API корректно возвращает последние `1000` событий после повторного открытия. Финальная задача не имеет active operations, active bindings или transport leases.
- Экспорт проверен на диске: `listings.jsonl.gz` 994 701 байт, SHA256 `9E7268313B0F1AD3133A83435482BCC4D10AC0B15F31C0155543595A7FAB8A23`; `observations.jsonl.gz` 91 446 байт, SHA256 `A85EDF352CA89271F80076E8FAAA3AEC6C7795B495C033ECBE22C873F2651AB1`; `summary.json` 2 569 815 байт, SHA256 `0D55D4F29CC5E7C80DB5199B96A5B0AD22794E0649D0EF3766FF7666B9873924`.
- Финальные проверки: `191 passed, 1 skipped, 276 deselected`, `ruff check` без замечаний, Vite production build и Electron NSIS завершены успешно. Новый установщик: `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe`, 83 056 865 байт, SHA256 `D9462BC99CCE3667BE37E07753A7274466BD84BB66DD3A818D056ACF08AAA07C`.

## 2026-07-15 - Kwork Market job workspace redesign

- Детальная страница запуска полностью перестроена в рабочее пространство с четырьмя независимыми режимами: «Обзор», «Выводы», «Данные» и «Выполнение». Активная вкладка сохраняется на время сессии отдельно для каждого job.
- В «Обзоре» теперь есть прогресс, реальные параметры параллельного выполнения, итоговый AI-вывод, сильные возможности, стадии запуска и краткое состояние workers/IP без бесконечных таблиц.
- AI-результат переработан в decision brief: confidence-индикатор, краткий вердикт, причины, карточки возможностей, ценовой коридор, насыщенность продавцов и компактный список лидеров.
- 476 смысловых групп больше не выводятся одной простынёй: добавлены поиск, фильтры по вердикту, сортировка по уверенности/спросу/конкуренции/размеру, поэтапная загрузка 20 строк и раскрытие деталей группы. UI очищает технические токены `strong`, `bull`, `mdash` и похожие остатки из подписей.
- Технические таблицы получили ограниченную высоту, sticky headers, поиск и фильтры. Fleet показывает активные привязки во время запуска и историю привязок после завершения, вместо пустого состояния.
- Новый визуальный язык отделён от старого оранжевого dashboard chrome: графитовые поверхности, cyan-сигнал, зелёный success, amber warning и красный error, без вложенных массивных карточек.
- Проверки: production `npm run build` и Electron NSIS завершены успешно. Установщик: `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe`, 83 058 713 байт, SHA256 `96629FA095CB64B46B068F96D37F21C937CC6D8E685BA8C5B4F31B4DE95EB364`.

## 2026-07-15 - Kwork Market publication handoff and diagnostic timeline

- Пустая третья ячейка в блоках рыночных возможностей заменена рабочей карточкой с готовым PSR-логотипом и прямым переходом к публикации. Сетка больше не рисует серые placeholders при неполном ряду.
- В итоговый анализ встроен durable-контур «рекомендация → обязательные поля Kwork → черновик и обложка → dry-run → подтверждённая live-публикация». Рекомендация, исходный cluster/evidence, handoff и опубликованный Kwork ID сохраняются и восстанавливаются после повторного открытия задачи.
- Backend пишет отдельные события `publication.state_changed` для создания и отклонения рекомендации, загрузки/подтверждения полей, генерации черновика, dry-run и публикации.
- Журнал запуска группирует массовые одинаковые переходы workers, скрывает завершённые пары `request.started`, показывает исход и длительность запроса, аккаунт, slot/IP, transport, пик параллельности и раскрываемые детали. Добавлен отдельный фильтр публикации.
- Новые события worker содержат предыдущее и текущее состояние, цель, generation, текущую операцию, ошибку и безопасные данные привязанного аккаунта/маршрута.
- Проверки: 15 целевых pytest-тестов, `ruff check`, проверка TypeScript изменённых Kwork Market-файлов, Vite production build и Electron NSIS завершены успешно. Установщик: `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe`, 83 066 142 байта, SHA256 `297031D0B2134CD65B18816404DF0EB93F379DC8BDB5EA21DB0E643073C9A692`.

## 2026-07-15 - Kwork Market publication pipeline: current implementation and live hardening (in progress)

- Эта запись фиксирует актуальное состояние и заменяет промежуточное описание выше про отдельную третью CTA-карточку. Переход к публикации вынесен из сетки рекомендаций в отдельную вкладку/рабочее пространство, чтобы сетка корректно показывала 2, 4 и любое другое число реальных возможностей без искусственных пустых ячеек.
- Страница завершённого Market job переработана в операционный интерфейс с отдельными представлениями обзора, выводов, данных, выполнения и публикации. Итоговые рыночные метрики, локальные группы, AI-вердикт, fleet и выполнение остаются доступны после завершения задачи.
- `EventTimeline` переведён с технической ленты `on/off` на диагностический журнал. События получают понятные заголовки и исходы, длительность, worker/account, VPNTE slot, egress/signup IP, transport/persona, текущую операцию и раскрываемые детали. Массовые одинаковые переходы группируются; доступны фильтры предупреждений, сети, workers и публикации.
- Реализован durable-контур `market recommendation -> draft handoff -> Kwork attribute manifest/selection -> generated draft -> confirmed live publish -> published listing`. Recommendation, cluster/evidence IDs, выбранные обязательные поля, черновик, creative assets и опубликованный Kwork ID хранятся в SQLite и восстанавливаются после повторного открытия вкладки.
- Backend публикует `publication.state_changed` для создания/отклонения рекомендации, загрузки и подтверждения полей, генерации черновика, dry-run, ошибки и успешной публикации. UI показывает текущий этап и сохранённое состояние, а не локальный одноразовый progress.
- Серверная генерация сама разрешает evidence IDs в реальные карточки конкурентов из Market DB. Для каждого конкурента собираются title/price/description, URL обложки и до четырёх portfolio images; frontend больше не обязан передавать изображения вручную.
- Подтверждено, что анализатор действительно получает изображения конкурентов: для текущего handoff использованы четыре реальные карточки `listing_9348`, `listing_9743`, `listing_9698`, `listing_9826`; cover pipeline сохранил число просмотренных, скачанных и отправленных vision-модели изображений и текстовый visual style brief.
- Cover pipeline использует vision-анализ конкурентов и недавнюю историю собственных обложек, затем формирует отдельный prompt для GPT Image. Добавлены параметры провайдера/model/detail, управление текстовым overlay и защита от повторения недавних композиций. Анализ конкурентов ещё требуется вынести в явный UI toggle `on/off`.
- Для текущего handoff создана GPT Image обложка `data/runtime/proposal_assets/kwork_autopublish/1784120842-f38b4a0858.png`. После визуальной проверки признано, что композиция слишком абстрактная и текст плохо подходит фактическому crop Kwork; перед финальной публикацией требуется новая предметная генерация с реальным результатом услуги в кадре.
- Реализована генерация пяти разных portfolio boards для дизайн-категорий. Для текущего handoff сохранены `1784120842-f38b4a0858-portfolio-01.jpg` ... `-05.jpg`, каждый файл уникален и имеет размер 88-121 КБ. Assets сохраняются в durable draft и не исчезают при повторном подтверждении manifest/selection.
- Web-auth Kwork переведён на официальный mobile web auth token refresh. Свежие cookies кэшируются и дополнительно сохраняются в `data/runtime/kwork_manual_cookies.json`, потому что Session Hub `/update` в текущем runtime отвечает 404. После рестарта backend cookies продолжают использоваться.
- Реализован точный Kwork form contract для категории 25: `attribute[1624]=401928` (`Логотипы`) и `attribute[401930]=401969` (`Доработка лого`). Manifest строится с динамическим probing дочерних controls, потому что `has_child` в ответах Kwork неполон.
- Разобран фактический контракт portfolio upload. Kwork принимает поле `portfolio` в единственном числе; cover использует `idPortfolioMedia`, а item требует нормализованный crop `{x:0,y:0,w:1,h:1}`. Live pipeline загружает пять изображений через `/portfolio/upload_image` и формирует browser-compatible payload.
- Разобран динамический пакетный контракт Kwork через `/package_filter/get_single_package_filters/25`. Для базового пакета добавлены описание, объём, срок, параметр `Исходники` и `Количество логотипов: 1`; это устранило серверную ошибку `У пакета Эконом должен быть выбран минимум 1 параметр`.
- Убраны устаревшие искусственные интервалы публикации; mailbox activation и Kwork form requests выполняются по фактической готовности данных. Для classification, `/new`, cover upload и portfolio upload добавлены ограниченные transport retries с диагностическим кодом вместо необработанного HTTP 500.
- Live preflight текущего handoff успешно прошёл: manifest/selection валидны, обязательные поля, cover и portfolio присутствуют, confirmation token выдан. Реальная карточка пока не опубликована.
- Первый live publish дошёл до загрузки основной обложки и завершился `httpx.ReadError` старого VPNTE runtime. Второй дошёл до проверки web-сессии и завершился `RemoteProtocolError` на `GET /new`. После этих прогонов добавлены retries для cover upload и открытия формы. Пользователь сообщил, что VPNTE исправлен; следующий шаг - повторить полный live publish без временного proxy bypass, затем найти карточку в кабинете и проверить строку `market_published_listings`.
- Добавлены/расширены unit-тесты на form payload, точные attribute selections, пять уникальных portfolio assets, browser-compatible portfolio payload, загрузку портфолио, retries `/new` и cover upload, сохранение cookies и durable creative assets. Последние адресные прогоны: 29 тестов успешно, `ruff check` успешно.
- Открытые пункты: исправить Electron asset URLs и битый PSR icon; проверить responsive grid на 2/4/большее число рекомендаций; завершить ручной выбор конкурентов после рубрики; кэшировать manifest/classification и убрать блокирующие ожидания UI; сделать vision-анализ конкурентов опциональным; поддержать генерацию 1-3 карточек; заменить абстрактные обложки конкретными предметными визуалами; завершить live publish, UI QA, полный test/build, обновить этот журнал и пересобрать NSIS installer.

## 2026-07-16 - Kwork Market publication pipeline completed

- Закрыты все пункты из промежуточной записи: Electron и Vite используют импортируемый PSR icon и корректные asset URLs; сетка выводов проверена на 2, 4 и 6 элементах без пустых placeholders; добавлен ручной выбор конкурентов после выбора рубрики; classification/manifest кэшируются и не блокируют остальной UI.
- Publication workspace поддерживает 1-3 серверных варианта, переключение варианта, число вариантов при перегенерации и явный toggle анализа изображений конкурентов. Variant hash, index и generation options хранятся в durable draft.
- Исправлен multimodal контракт `LLMRouter`: при `wire_api=responses` изображения отправляются в `/responses` как `input_image`, а chat-completions сохраняет свой формат. Для cover vision/prompt закреплена модель `gpt-5.4`, которая реально вернула visual brief по четырём карточкам: `listing_9348`, `listing_9743`, `listing_9698`, `listing_9826`.
- Окончательная предметная обложка: `data/runtime/proposal_assets/kwork_autopublish/1784149269-d14e7392fd.png`, 1536x1024, с читаемым русским overlay и сценой before/after. К ней созданы пять уникальных 1200x800 portfolio files `1784149269-d14e7392fd-portfolio-01.jpg` ... `-05.jpg`; все визуально проверены и имеют разные SHA256.
- Live hardening доведён по фактическим ответам Kwork. Numeric keys `bundle_extra_standard_value[category][190/191]` больше не превращаются в разреженный JSON-массив, а portfolio `cover.crop` получает нормализованный crop того же media item. Это убрало и `unknown option`, и пять ошибок `Необходимо загрузить обложку`.
- Реальный publish через текущий VPNTE завершён без bypass: Kwork save вернул `success`, post-save verifier нашёл заголовок на HTTP 200, карточка доступна по `https://kwork.ru/logo/53548662/adaptiruyu-logotip-i-shrift-pod-kirillitsu`. Загружены основная обложка и пять portfolio media IDs `35933265`, `35933267`, `35933269`, `35933270`, `35933272`.
- Durable DB proof: `market_published_listings` содержит одну запись `published_277361fc07144bae9bc7adc5bbbc20cd` для `job_847c2861a7c7473d9534a95dc5696d30`, handoff `handoff_9f0c3a7c948d46a1aeed8ab06934cedd`, cluster `cluster_6d0beed186e265b8`, `kwork_id=53548662`, draft hash `140ad33f7aae23f3ad7a0b7ca5a3a4be30407932bce2414e14f046b85d5862e6`. Парсер опубликованного ID теперь также извлекает ID из verified/redirect URL, если Kwork не вернул его отдельным полем.
- Финальный headless UI QA: 3 variant tabs, смена выбранного заголовка, toggle `true -> false`, выбор regeneration count `2`, пять portfolio previews, published listing, 0 broken images и 0 console errors. Screenshot: `C:/Users/Redmi/AppData/Local/Temp/psr-ui-qa/publication-variants.png`.
- Проверки: `173 passed` в целевом regression suite; дополнительные адресные portfolio/serialization/ID tests прошли; `ruff check` для всех изменённых и новых Python-файлов успешен. Repo-wide ruff отдельно показывает 15 старых замечаний в неизменённых `chat.py`, `telegram.py`, `kwork_parser.py` и старых smoke/tests; они не связаны с publication pipeline.
- Production `npm run build` завершил Vite и Electron-builder/NSIS. Новый установщик: `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe`, 83 141 211 байт, SHA256 `3E856195CBC1550E826CED103D308063C92469A6FF5B24953595BC263CE8F4C5`.
