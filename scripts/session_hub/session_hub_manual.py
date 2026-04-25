"""
Простой Session Hub сервер с ручным вводом кук.
Запуск: python session_hub_manual.py

Инструкция:
1. Залогиньтесь в Chrome на kwork.ru / fl.ru
2. Откройте DevTools (F12) → Application → Cookies
3. Скопируйте все куки в файл cookies.json (см. пример ниже)
4. Запустите этот сервер
5. Запускайте основной проект - он получит куки отсюда
"""

from flask import Flask, request, jsonify
import json
import os
from datetime import datetime

app = Flask(__name__)

COOKIES_FILE = "session_hub_cookies.json"


def load_cookies():
    """Загружает куки из файла."""
    if not os.path.exists(COOKIES_FILE):
        return {}

    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cookies(data):
    """Сохраняет куки в файл."""
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@app.route("/cookies")
def get_cookies():
    """
    Получить куки для домена.
    GET /cookies?domain=kwork.ru
    """
    domain = request.args.get("domain", "").lower().strip()

    if not domain:
        return jsonify({"status": "error", "error": "Параметр 'domain' обязателен", "count": 0, "cookies": []}), 400

    all_cookies = load_cookies()
    domain_cookies = all_cookies.get(domain, [])

    # Добавляем метаданные если их нет
    for cookie in domain_cookies:
        if "browser" not in cookie:
            cookie["browser"] = "chrome"
        if "profile" not in cookie:
            cookie["profile"] = "Default"
        if "profile_name" not in cookie:
            cookie["profile_name"] = "Manual"

    return jsonify(
        {
            "status": "ok",
            "count": len(domain_cookies),
            "domain": domain,
            "updated_at": all_cookies.get(f"_{domain}_updated", "unknown"),
            "cookies": domain_cookies,
        }
    )


@app.route("/update", methods=["POST"])
def update_cookies():
    """
    Обновить куки для домена.
    POST /update
    Body: {"domain": "kwork.ru", "cookies": [...]}

    Или просто отправьте массив кук из DevTools.
    """
    data = request.get_json()

    if not data:
        return jsonify({"status": "error", "error": "JSON body обязателен"}), 400

    # Поддержка двух форматов:
    # 1. {"domain": "...", "cookies": [...]}
    # 2. Массив кук напрямую (тогда domain из первой куки)

    if isinstance(data, list):
        # Массив кук напрямую
        if not data:
            return jsonify({"status": "error", "error": "Пустой массив кук"}), 400
        domain = data[0].get("domain", "").lstrip(".").lower()
        cookies = data
    else:
        # Объект с domain и cookies
        domain = data.get("domain", "").lower()
        cookies = data.get("cookies", [])

    if not domain:
        return jsonify({"status": "error", "error": "Не удалось определить домен"}), 400

    all_cookies = load_cookies()
    all_cookies[domain] = cookies
    all_cookies[f"_{domain}_updated"] = datetime.now().isoformat()
    save_cookies(all_cookies)

    return jsonify({"status": "ok", "domain": domain, "count": len(cookies), "message": f"Куки для {domain} обновлены"})


@app.route("/list")
def list_domains():
    """Показать все домены с куками."""
    all_cookies = load_cookies()
    domains = [k for k in all_cookies.keys() if not k.startswith("_")]

    result = {}
    for domain in domains:
        result[domain] = {
            "count": len(all_cookies.get(domain, [])),
            "updated": all_cookies.get(f"_{domain}_updated", "unknown"),
        }

    return jsonify({"status": "ok", "domains": result})


@app.route("/health")
def health():
    """Проверка работоспособности."""
    return jsonify({"status": "ok", "service": "session-hub-manual"})


@app.route("/")
def index():
    """Инструкция."""
    return """
    <h1>Session Hub (Manual)</h1>
    <p>Простой сервер для хранения кук из Chrome.</p>
    
    <h2>Быстрый старт:</h2>
    <ol>
        <li>Залогиньтесь на kwork.ru в Chrome</li>
        <li>DevTools (F12) → Application → Cookies → https://kwork.ru</li>
        <li>Скопируйте все куки (правый клик → Copy all)</li>
        <li>Сохраните в файл session_hub_cookies.json или отправьте POST /update</li>
    </ol>
    
    <h2>Endpoints:</h2>
    <ul>
        <li><code>GET /cookies?domain=kwork.ru</code> - получить куки</li>
        <li><code>POST /update</code> - обновить куки</li>
        <li><code>GET /list</code> - список доменов</li>
        <li><code>GET /health</code> - проверка</li>
    </ul>
    
    <h2>Пример обновления кук:</h2>
    <pre>
curl -X POST http://127.0.0.1:8669/update \\
  -H "Content-Type: application/json" \\
  -d '{
    "domain": "kwork.ru",
    "cookies": [
      {"name": "slrememberme", "value": "...", "domain": "kwork.ru", "path": "/"},
      {"name": "userId", "value": "12345", "domain": "kwork.ru", "path": "/"}
    ]
  }'
    </pre>
    """


if __name__ == "__main__":
    print("=" * 60)
    print("Session Hub (Manual) - Сервер для хранения кук")
    print("=" * 60)
    print("URL: http://127.0.0.1:8669")
    print(f"Файл кук: {COOKIES_FILE}")
    print("=" * 60)

    if not os.path.exists(COOKIES_FILE):
        print("\n⚠️  Файл кук не найден!")
        print("Создайте его или используйте POST /update для добавления кук")

        # Создаем пример файла
        example = {
            "kwork.ru": [
                {
                    "name": "slrememberme",
                    "value": "ЗАМЕНИТЕ_НА_РЕАЛЬНОЕ_ЗНАЧЕНИЕ",
                    "domain": "kwork.ru",
                    "path": "/",
                    "secure": True,
                }
            ],
            "_kwork.ru_updated": "2024-01-01T00:00:00",
        }
        save_cookies(example)
        print(f"\nСоздан пример файла: {COOKIES_FILE}")
        print("Замените значения кук на реальные из Chrome DevTools!")
    else:
        cookies = load_cookies()
        domains = [k for k in cookies.keys() if not k.startswith("_")]
        print(f"\nЗагружены куки для: {', '.join(domains) if domains else 'нет доменов'}")

    print("\nНажмите Ctrl+C для остановки")
    print("=" * 60)

    app.run(host="127.0.0.1", port=8669, debug=False)
