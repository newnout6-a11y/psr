"""
Пример Session Hub сервера для предоставления кук из Chrome.
Запуск: python session_hub_example.py

Требования:
- pip install flask browser-cookie3
- Chrome должен быть закрыт (или использовать --profile-directory)
"""

from flask import Flask, request, jsonify
import browser_cookie3
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)


def get_domain_cookies(domain: str):
    """
    Получает куки из Chrome для указанного домена.

    Требования:
    - Chrome должен быть закрыт (или использовать копию профиля)
    - Установите: pip install browser-cookie3
    """
    try:
        # Пробуем получить куки из Chrome
        cj = browser_cookie3.chrome(domain_name=domain)

        cookies = []
        for cookie in cj:
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "secure": cookie.secure,
                    "browser": "chrome",
                    "profile": "Default",
                    "profile_name": "Default",
                }
            )

        return cookies
    except Exception as e:
        logger.error(f"Ошибка получения кук из Chrome: {e}")
        return None


@app.route("/cookies")
def cookies_endpoint():
    """
    Endpoint для получения кук.

    Query params:
        domain: домен для которого нужны куки (например, kwork.ru)

    Returns:
        {
            "status": "ok" | "error",
            "count": int,
            "cookies": [...]
        }
    """
    domain = request.args.get("domain", "")

    if not domain:
        return jsonify({"status": "error", "error": "Параметр 'domain' обязателен", "count": 0, "cookies": []}), 400

    logger.info(f"Запрос кук для домена: {domain}")

    # Пробуем получить из Chrome
    cookies = get_domain_cookies(domain)

    if cookies is None:
        # Fallback: вернуть пример кук (замените на реальные)
        logger.warning(f"Не удалось получить кук из Chrome для {domain}, используем fallback")

        # Здесь можно подгрузить куки из файла или другого источника
        return jsonify(
            {
                "status": "error",
                "error": "Chrome недоступен. Закройте Chrome и попробуйте снова.",
                "count": 0,
                "cookies": [],
            }
        )

    if not cookies:
        return jsonify(
            {
                "status": "ok",
                "count": 0,
                "cookies": [],
                "message": f"Куки для {domain} не найдены. Возможно, вы не залогинены.",
            }
        )

    logger.info(f"Возвращаем {len(cookies)} кук для {domain}")

    return jsonify({"status": "ok", "count": len(cookies), "cookies": cookies})


@app.route("/health")
def health_check():
    """Health check endpoint."""
    return jsonify({"status": "ok", "service": "session-hub"})


if __name__ == "__main__":
    print("=" * 60)
    print("Session Hub сервер")
    print("=" * 60)
    print("URL: http://127.0.0.1:8669")
    print("Endpoints:")
    print("  GET /cookies?domain=kwork.ru - получить куки")
    print("  GET /health - проверка работоспособности")
    print("=" * 60)
    print("\nВАЖНО: Для получения кук Chrome должен быть ЗАКРЫТ!")
    print("Или используйте отдельный профиль Chrome.")
    print("=" * 60)

    app.run(host="127.0.0.1", port=8669, debug=False)
