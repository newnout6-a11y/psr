# Session Hub - Основной источник кук

## Что изменилось

Теперь **Session Hub** (порт 127.0.0.1:8669) является **основным и приоритетным** источником куки для авторизации на фриланс-платформах.

## Как это работает

```
Приоритет получения кук:
┌─────────────────────────────────────┐
│ 1. Session Hub (127.0.0.1:8669)     │ ← Основной источник
│    └─> Свежие куки из Chrome         │
├─────────────────────────────────────┤
│ 2. .env файл (fallback)              │ ← Запасной вариант
│    └─> Ручное копирование из DevTools│
└─────────────────────────────────────┘
```

## Настройка Session Hub

### 1. Запустите Session Hub сервер

Session Hub должен быть доступен по адресу `http://127.0.0.1:8669`.

Пример простого сервера на Python:

```python
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/cookies')
def get_cookies():
    domain = request.args.get('domain', '')

    # Получите куки из Chrome/Chromium для этого домена
    # Это пример - замените на реальную логику получения кук

    cookies = [
        {
            "name": "slrememberme",
            "value": "20354236_$2y$10$...",
            "domain": "kwork.ru",
            "path": "/",
            "secure": True,
            "browser": "chrome",
            "profile": "Profile 2",
            "profile_name": "Пользователь 1"
        },
        # ... другие куки
    ]

    return jsonify({
        "status": "ok",
        "count": len(cookies),
        "cookies": cookies
    })

if __name__ == '__main__':
    app.run(port=8669)
```

### 2. Настройте .env (опционально)

```bash
# Session Hub URL (по умолчанию: http://127.0.0.1:8669/cookies)
SESSION_HUB_URL=http://127.0.0.1:8669/cookies
```

### 3. Логи при работе

При успешном получении кук из Hub:
```
BrowserManager: ✅ 14/14 кук из Session Hub (chrome/Пользователь 1) для kwork.ru
BrowserManager: авторизация на kwork.ru подтверждена
```

При недоступности Hub (fallback на .env):
```
BrowserManager: Session Hub недоступен, используем .env как fallback для kwork.ru
BrowserManager: ⚠️ fallback на .env: 4 кук для kwork.ru
```

## Формат ответа Session Hub

Обязательные поля:
```json
{
  "status": "ok",
  "count": 14,
  "cookies": [
    {
      "name": "имя_куки",
      "value": "значение_куки",
      "domain": "kwork.ru",
      "path": "/",
      "secure": true,
      "browser": "chrome",
      "profile": "Profile 2",
      "profile_name": "Пользователь 1"
    }
  ]
}
```

Поля `browser`, `profile`, `profile_name` используются только для логирования.

## Fallback на .env

Если Session Hub недоступен, система автоматически переключится на куки из `.env`:

```bash
# Kwork
KWORK_COOKIE_REMEMBERME=...
KWORK_COOKIE_USERID=...
KWORK_COOKIE_PHPSESSID=...
KWORK_COOKIE_CSRF=...

# FL.ru
FL_RU_COOKIE_SESSION=...
FL_RU_COOKIE_ID=...
FL_RU_COOKIE_PWD=...
FL_RU_XSRF_TOKEN=...
```

## Преимущества Session Hub

1. **Свежие куки** - всегда актуальные сессии из Chrome
2. **Автообновление** - куки обновляются автоматически при работе в браузере
3. **Мультипрофиль** - поддержка нескольких профилей Chrome
4. **Без ручного копирования** - не нужно обновлять .env каждый раз

## Устранение неполадок

### Session Hub не отвечает

```
BrowserManager: Session Hub недоступен (Connection refused)
```

**Решение:**
1. Проверьте что сервер запущен: `curl http://127.0.0.1:8669/cookies?domain=kwork.ru`
2. Проверьте порт в `.env`: `SESSION_HUB_URL=http://127.0.0.1:8669/cookies`
3. Убедитесь что куки есть в `.env` как fallback

### Неверный формат ответа

```
BrowserManager: Session Hub вернул пустой список кук
```

**Решение:**
Проверьте что ответ содержит поле `cookies` (массив) и `status: "ok"`.

### Авторизация не подтверждена

```
BrowserManager: авторизация на kwork.ru НЕ подтверждена!
```

**Решение:**
1. Проверьте что куки актуальны (не истекли)
2. Проверьте что в ответе Session Hub есть основные куки (`slrememberme`, `userId` для Kwork)
3. Вручную залогиньтесь в Chrome и проверьте что сессия активна
